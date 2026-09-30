"""
Agente de automação: Gmail (IMAP) -> PDF -> resumo com Claude -> Telegram.

Fluxo:
    1. Conecta ao Gmail via IMAP e busca e-mails NÃO LIDOS com anexo PDF.
    2. Baixa cada PDF em memória e extrai o texto com pdfplumber.
    3. Envia o texto para a API da Anthropic, que devolve 3 tópicos acionáveis.
    4. Envia o assunto do e-mail + resumo para o Telegram via Bot API.
    5. Marca o e-mail como lido somente se tudo deu certo.

Todas as credenciais vêm do arquivo .env (python-dotenv).
"""

from __future__ import annotations

import email
import imaplib
import io
import logging
import os
import sys
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message

import anthropic
import pdfplumber
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("email_pdf_agent")

TELEGRAM_MAX_CHARS = 4096

SYSTEM_PROMPT = (
    "Você é um assistente que resume documentos para um profissional ocupado. "
    "Leia o documento fornecido e responda em português do Brasil com exatamente "
    "3 tópicos diretos e acionáveis, um por linha, cada um começando com '• '. "
    "Cada tópico deve dizer o que importa e, quando houver, o que precisa ser feito "
    "(prazos, valores, responsáveis). Não inclua introdução nem conclusão."
)


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    gmail_user: str
    gmail_app_password: str
    imap_host: str
    anthropic_api_key: str
    anthropic_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    poll_interval: int


def load_config() -> Config:
    """Carrega e valida as variáveis do .env. Encerra se faltar alguma obrigatória."""
    load_dotenv()

    required = [
        "GMAIL_USER",
        "GMAIL_APP_PASSWORD",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        log.error("Variáveis ausentes no .env: %s", ", ".join(missing))
        sys.exit(1)

    return Config(
        gmail_user=os.environ["GMAIL_USER"],
        # Senhas de app do Google às vezes são copiadas com espaços.
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""),
        imap_host=os.getenv("IMAP_HOST", "imap.gmail.com"),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "0")),
    )


# --------------------------------------------------------------------------- #
# E-mail (IMAP)
# --------------------------------------------------------------------------- #
def connect_imap(cfg: Config) -> imaplib.IMAP4_SSL:
    """Abre conexão SSL com o Gmail e seleciona a caixa de entrada."""
    conn = imaplib.IMAP4_SSL(cfg.imap_host)
    conn.login(cfg.gmail_user, cfg.gmail_app_password)
    conn.select("INBOX")
    return conn


def fetch_unseen_with_attachments(conn: imaplib.IMAP4_SSL) -> list[bytes]:
    """Retorna os UIDs dos e-mails não lidos que têm anexo PDF.

    Usa a extensão X-GM-RAW do Gmail para filtrar no servidor
    (mesma sintaxe da busca do Gmail).
    """
    status, data = conn.uid(
        "SEARCH", None, "UNSEEN", "X-GM-RAW", '"has:attachment filename:pdf"'
    )
    if status != "OK":
        raise RuntimeError(f"Falha na busca IMAP: {data}")
    return data[0].split()


def get_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> Message:
    """Baixa o e-mail sem marcá-lo como lido (BODY.PEEK)."""
    status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Falha ao baixar e-mail UID {uid!r}")
    return email.message_from_bytes(data[0][1])


def mark_as_read(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")


def decode_str(value: str | None) -> str:
    """Decodifica cabeçalhos MIME (ex.: '=?UTF-8?B?...?=')."""
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def extract_pdf_attachments(msg: Message) -> list[tuple[str, bytes]]:
    """Retorna [(nome_do_arquivo, bytes_do_pdf), ...] do e-mail."""
    pdfs: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_str(part.get_filename())
        is_pdf = part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            pdfs.append((filename or "anexo.pdf", payload))
    return pdfs


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrai o texto de todas as páginas do PDF, direto da memória."""
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n\n".join(pages).strip()


# --------------------------------------------------------------------------- #
# IA (Anthropic)
# --------------------------------------------------------------------------- #
def summarize_text(client: anthropic.Anthropic, model: str, text: str, title: str) -> str:
    """Envia o texto para o Claude e devolve o resumo em 3 tópicos."""
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "medium"},
        # Se o modelo recusar por política, a API tenta outro modelo automaticamente.
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Título do documento: {title}\n\n"
                    f"<documento>\n{text}\n</documento>"
                ),
            }
        ],
    )

    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise RuntimeError(f"O modelo recusou o resumo (categoria: {category})")

    summary = "".join(block.text for block in response.content if block.type == "text").strip()
    if not summary:
        raise RuntimeError(f"Resposta vazia do modelo (stop_reason={response.stop_reason})")
    return summary


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram_message(cfg: Config, text: str) -> None:
    """Envia texto ao chat, dividindo em blocos se passar do limite do Telegram."""
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    for start in range(0, len(text), TELEGRAM_MAX_CHARS):
        chunk = text[start : start + TELEGRAM_MAX_CHARS]
        resp = requests.post(
            url,
            json={"chat_id": cfg.telegram_chat_id, "text": chunk},
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"Telegram respondeu {resp.status_code}: {resp.text}")


def format_notification(subject: str, sender: str, filename: str, summary: str) -> str:
    return (
        f"📧 {subject or '(sem assunto)'}\n"
        f"👤 {sender}\n"
        f"📎 {filename}\n\n"
        f"{summary}"
    )


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #
def process_email(conn: imaplib.IMAP4_SSL, uid: bytes, cfg: Config, client: anthropic.Anthropic) -> None:
    """Processa um e-mail. Só marca como lido se todos os PDFs forem enviados."""
    msg = get_message(conn, uid)
    subject = decode_str(msg.get("Subject"))
    sender = decode_str(msg.get("From"))
    log.info("Processando: %s", subject)

    pdfs = extract_pdf_attachments(msg)
    if not pdfs:
        log.info("Nenhum PDF encontrado; marcando como lido.")
        mark_as_read(conn, uid)
        return

    for filename, pdf_bytes in pdfs:
        text = extract_text_from_pdf(pdf_bytes)
        if not text:
            summary = "⚠️ Não foi possível extrair texto deste PDF (pode ser uma imagem escaneada)."
        else:
            summary = summarize_text(client, cfg.anthropic_model, text, filename)
        send_telegram_message(cfg, format_notification(subject, sender, filename, summary))
        log.info("Resumo de '%s' enviado ao Telegram.", filename)

    mark_as_read(conn, uid)


def run_once(cfg: Config, client: anthropic.Anthropic) -> None:
    conn = connect_imap(cfg)
    try:
        uids = fetch_unseen_with_attachments(conn)
        log.info("%d e-mail(s) não lido(s) com PDF.", len(uids))
        for uid in uids:
            try:
                process_email(conn, uid, cfg, client)
            except anthropic.APIConnectionError as exc:
                log.error("Erro de rede com a Anthropic (UID %s): %s", uid.decode(), exc)
            except anthropic.APIStatusError as exc:
                log.error("Erro da API Anthropic %s (UID %s): %s", exc.status_code, uid.decode(), exc.message)
            except Exception:
                # O e-mail continua não lido e será tentado na próxima execução.
                log.exception("Falha ao processar UID %s", uid.decode())
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> None:
    cfg = load_config()
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    if cfg.poll_interval <= 0:
        run_once(cfg, client)
        return

    log.info("Monitorando a cada %ds. Ctrl+C para parar.", cfg.poll_interval)
    while True:
        try:
            run_once(cfg, client)
        except (imaplib.IMAP4.error, OSError):
            log.exception("Falha na conexão IMAP; tentando no próximo ciclo.")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
