# Agente: Gmail → PDF → Resumo (Claude) → Telegram

Monitora e-mails **não lidos com PDF** no Gmail, extrai o texto do anexo, gera um resumo em
3 tópicos acionáveis com o Claude e envia para o seu Telegram. Depois marca o e-mail como lido.

## Instalação

```bash
cd email_pdf_agent
python3 -m venv .venv && source .venv/bin/activate   # Python 3.10+
pip install -r requirements.txt
cp .env.example .env   # e preencha as credenciais
python main.py
```

## Credenciais (.env)

| Variável | Onde obter |
|---|---|
| `GMAIL_USER` | Seu e-mail Gmail |
| `GMAIL_APP_PASSWORD` | Conta Google → Segurança → Verificação em duas etapas → **Senhas de app** (16 letras) |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |
| `TELEGRAM_BOT_TOKEN` | No Telegram, fale com **@BotFather** → `/newbot` |
| `TELEGRAM_CHAT_ID` | Mande qualquer mensagem ao seu bot e abra `https://api.telegram.org/bot<TOKEN>/getUpdates` → `"chat":{"id": ...}` |

O IMAP precisa estar habilitado no Gmail (Configurações → Encaminhamento e POP/IMAP).

## Modos de execução

- `POLL_INTERVAL_SECONDS=300` → fica rodando e verifica a cada 5 minutos.
- `POLL_INTERVAL_SECONDS=0` → roda uma vez e sai (use com cron: `*/5 * * * * cd /caminho && .venv/bin/python main.py`).

## Comportamento

- O e-mail é lido com `BODY.PEEK`, então **só vira "lido" depois** que o resumo chega ao Telegram.
  Se algo falhar (rede, API), ele continua não lido e é tentado de novo no próximo ciclo.
- PDFs escaneados (só imagem) não têm texto extraível; o bot avisa em vez de resumir.
- Mensagens longas são divididas no limite de 4096 caracteres do Telegram.
