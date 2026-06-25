# Agentis Slack

Slack bridge pro Agentis. Služba poslouchá Slack události přes Bolt Socket Mode, převádí zprávy a zmínky na Agentis tasky a umožňuje agentovi ptát se uživatele přímo ve Slack threadu přes stdio MCP server.

## Co projekt řeší

- Vytváří Agentis task z `app_mention` nebo běžné Slack zprávy.
- Přidává k tasku Slack metadata: tým, kanál, thread, autor a historii threadu.
- Spouští běh tasku v Agentisu a přidá reakci `eyes` ke zdrojové zprávě.
- Poskytuje MCP server `question` / `approve`, který posílá otázky do Slack threadu a čeká na odpověď.
- Obsluhuje Slack modal pro dávku otázek a zapisuje odpověď zpět do Agentisu.
- Umí průběžně streamovat JSONL události agenta do Slacku přes Slack AI-agent streaming API.

## Jak to funguje

1. Uživatel napíše zprávu nebo zmíní Slack aplikaci.
2. `agentis-slack` přijme událost přes Socket Mode.
3. Služba načte historii threadu, normalizuje text a vytvoří Agentis task přes JSON-RPC.
4. Agentis spustí běh tasku.
5. Když agent potřebuje interakci, zavolá MCP nástroj `question` nebo `approve`.
6. `agentis-slack-mcp` zaregistruje otázku v Agentisu, pošle do Slack threadu tlačítko a polluje odpověď.
7. Bolt aplikace po kliknutí otevře modal, uloží odpověď přes `task.question_reply` a MCP vrátí výsledek agentovi.

Podrobnosti k MCP toku jsou v `docs/slack-question-mcp.md`.

## Požadavky

- Python `>=3.13`
- Poetry `>=2`
- Slack aplikace se zapnutým Socket Mode a Interactivity
- Agentis backend dostupný přes JSON-RPC API

Slack aplikace potřebuje minimálně:

- app-level token s oprávněním `connections:write`
- bot token s oprávněním `chat:write`
- event subscriptions pro `app_mention` a `message`, podle toho, které vstupy chcete zpracovávat
- Interactivity zapnutou pro tlačítka a modaly

## Instalace

```bash
poetry install
```

Lokálně lze použít i existující virtuální prostředí v `.venv`, pokud už je připravené:

```bash
. .venv/bin/activate
```

## Konfigurace

Konfigurace se načítá z proměnných prostředí. Soubor `.env` v kořeni projektu se načte automaticky, pokud proměnné nejsou nastavené v shellu.

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
AGENTIS_API_URL=https://agentis.example.com/api/jsonrpc
AGENTIS_TOKEN=...
AGENTIS_SERVICE_TOKEN=...
```

| Proměnná | Význam | Default |
|---|---|---|
| `SLACK_BOT_TOKEN` | Bot token pro Slack Web API | povinné |
| `SLACK_APP_TOKEN` | App-level token pro Slack Socket Mode | povinné |
| `AGENTIS_API_URL` | Agentis JSON-RPC endpoint | prázdné |
| `AGENTIS_TOKEN` | Uživatelský token pro běžné Agentis RPC | prázdné |
| `AGENTIS_SERVICE_TOKEN` | Service token pro otázky, polling a service RPC | prázdné |
| `SLACK_RATE_LIMIT_WINDOW_SECONDS` | Okno rate limitu Slack událostí | `60` |
| `SLACK_RATE_LIMIT_MAX_EVENTS` | Maximum událostí v okně | `30` |
| `AGENTIS_DEFAULT_PROJECT` | Výchozí projekt pro nové tasky | prázdné |
| `AGENTIS_DEFAULT_AGENT` | Výchozí agent pro nové tasky | prázdné |
| `AGENTIS_DEFAULT_MODEL` | Výchozí model | prázdné |
| `AGENTIS_DEFAULT_EFFORT` | Výchozí effort modelu | prázdné |
| `AGENTIS_DEFAULT_ADAPTER` | Výchozí adapter | prázdné |
| `AGENTIS_DEFAULT_ENVIRONMENT` | Výchozí prostředí tasku | prázdné |

## Spuštění Slack služby

```bash
poetry run agentis-slack
```

Alternativně:

```bash
poetry run python -m agentis_slack.main
```

Služba běží přes Slack Socket Mode, takže nepotřebuje veřejný HTTP endpoint.

## MCP server pro otázky

MCP server se spouští přes stdio:

```bash
poetry run agentis-slack-mcp
```

Typické zapojení v MCP konfiguraci adaptéru:

```json
{
  "mcp": {
    "ask-question": {
      "command": ["/var/www/agentis-slack/.venv/bin/python", "-m", "agentis_slack.question_mcp"],
      "cwd": "/var/www/agentis-slack",
      "type": "local"
    }
  }
}
```

MCP server používá navíc tyto proměnné:

| Proměnná | Význam | Default |
|---|---|---|
| `TASK_HEADER_SLACK_CHANNEL_ID` / `TASK_HEADER_SLACK_CHANNEL` | Slack kanál pro prompt otázky | prázdné |
| `TASK_HEADER_SLACK_THREAD_TS` / `TASK_HEADER_SLACK_MESSAGE_TS` | Slack thread pro prompt otázky | prázdné |
| `AGENTIS_MCP_ANSWER_TIMEOUT` | Timeout čekání na odpověď v sekundách | `900` |
| `AGENTIS_MCP_POLL_INTERVAL` | Interval pollingu odpovědi v sekundách | `1` |
| `AGENTIS_DEFAULT_SESSION_ID` | Fallback session id | prázdné |
| `AGENTIS_SLACK_MCP_LOG_FILE` | Log soubor MCP serveru | `/tmp/agentis-slack-mcp.log` |

> [!NOTE]
> Když chybí Slack kontext, MCP otázku pořád zaregistruje v Agentisu a čeká na odpověď z backendu. Slack chyba nemá shodit běh agenta.

## Slack streaming

`agentis_slack.slack_stream` funguje jako tee pro JSON Lines výstup agenta. Vstup propouští na stdout a zároveň z vybraných událostí skládá průběžnou Slack streaming zprávu.

```bash
agentiscode --json ... | poetry run python -m agentis_slack.slack_stream
```

Pro streaming se používají hlavně:

- `TASK_HEADER_SLACK_CHANNEL`
- `TASK_HEADER_SLACK_THREAD_TS` nebo `TASK_HEADER_SLACK_MESSAGE_TS`
- `TASK_HEADER_SLACK_USER` / `TASK_HEADER_SLACK_USER_ID`
- `TASK_HEADER_SLACK_TEAM` / `TASK_HEADER_SLACK_TEAM_ID`
- `SLACK_BOT_TOKEN`
- `SLACK_STREAM_INTERVAL`, default `3`

Pokud některá zásadní hodnota chybí, skript doběhne jako čistý tee bez volání Slacku.

## Vývoj

Spuštění testů:

```bash
poetry run pytest
```

Pokud v čistém prostředí `pytest` není nainstalovaný, doinstalujte ho do vývojového prostředí.

Užitečné entrypointy:

| Příkaz | Popis |
|---|---|
| `agentis-slack` | Spustí Slack Bolt Socket Mode službu |
| `agentis-slack-mcp` | Spustí stdio MCP server pro `question` a `approve` |
| `python -m agentis_slack.slack_stream` | Streamuje JSONL průběh agenta do Slacku |

## Struktura projektu

```text
src/agentis_slack/
  main.py             Slack Bolt aplikace a handlery
  slack_service.py    Převod Slack událostí na Agentis tasky a obsluha modalů
  question_mcp.py     Stdio MCP server pro otázky a schvalování
  slack_blocks.py     Slack Block Kit buildery pro otázky
  slack_stream.py     Tee a Slack streaming pro JSONL události agenta
  agentis_client.py   JSON-RPC klient pro Agentis
  config.py           Načítání .env a settings
tests/                Unit testy
docs/                 Doplňková dokumentace
```
