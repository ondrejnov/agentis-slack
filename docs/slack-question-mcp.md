# Slack question MCP

Stdio MCP server, který klade otázky uživateli **přímo do Slack threadu**, ze
kterého task vznikl. Stejný tool surface jako `agentis_sdk.mcp_server`
(`question`, `approve`), takže je to v adaptéru drop-in náhrada.

## Jak to funguje (varianta B – interactive bridge)

1. Agent zavolá `question` / `approve`.
2. MCP (`agentis_slack.question_mcp`) zaregistruje otázku na backendu
   (`task.add_question`) → vznikne `external_id` navázaný na běh/task.
3. MCP pošle do Slack threadu prompt s tlačítkem **„Odpovědět"**
   (`external_id` + `task_id` ve `value` tlačítka).
4. Bolt app (`agentis-slack`, Socket Mode) zachytí klik
   (`@app.action("agentis_q_open")`), natáhne dávku přes `task.fetch` a otevře
   **modal** s celou dávkou (radio / checkboxes / volný text).
5. Submit modalu (`@app.view("agentis_q_submit")`) se přeloží na `results` a
   zapíše přes `task.question_reply`.
6. MCP mezitím pollovala `task.get_question_result` a vrátí agentovi odpověď
   ve stejném formátu jako SDK MCP (`"otázka=odpověď; …"`).

Bez Slack kontextu se otázka jen zaregistruje a polluje se (odpovědět jde i ve
webu). Chyba Slacku nikdy neshodí běh agenta.

## Předpoklady

- Bolt service `agentis-slack` musí běžet (Socket Mode) – obsluhuje klik a modal.
- **Slack app musí mít zapnutou Interactivity** (Socket Mode → events
  `block_actions`, `view_submission` chodí automaticky; stačí mít app token se
  `connections:write` a scope `chat:write`, `commands` netřeba).

## Nastavení v adaptéru

V MCP configu adaptéru (`/var/www/agentis-adapter/opencode.json`, klíč `mcp`)
přepni `ask-question` na tenhle server:

```json
"mcp": {
  "ask-question": {
    "command": ["/var/www/agentis-slack/.venv/bin/python", "-m", "agentis_slack.question_mcp"],
    "cwd": "/var/www/agentis-slack",
    "type": "local"
  }
}
```

`cwd` = repo `agentis-slack`, ať se z jeho `.env` načte `AGENTIS_API_URL`,
`AGENTIS_TOKEN` a `SLACK_BOT_TOKEN` (přes `Settings.from_env`). Balík je
editable-installed, takže `-m agentis_slack.question_mcp` jede odkudkoli; lze
použít i console script `/var/www/agentis-slack/.venv/bin/agentis-slack-mcp`.

`TASK_HEADER_SLACK_CHANNEL_ID` / `TASK_HEADER_SLACK_THREAD_TS` doplňuje adaptér
z task headers automaticky (`common/models.py`).

## Env proměnné

| Proměnná | Význam | Default |
|---|---|---|
| `AGENTIS_API_URL`, `AGENTIS_TOKEN` | backend JSON-RPC (z `.env`) | – |
| `SLACK_BOT_TOKEN` | post promptu do threadu (z `.env`) | – |
| `AGENTIS_MCP_ANSWER_TIMEOUT` | timeout čekání na odpověď (s) | `900` |
| `AGENTIS_MCP_POLL_INTERVAL` | interval pollingu (s) | `1` |
| `AGENTIS_DEFAULT_SESSION_ID` | fallback session id | – |
| `AGENTIS_SLACK_MCP_LOG_FILE` | log soubor | `/tmp/agentis-slack-mcp.log` |

## Poznámky / omezení

- `approve` je modelované jako otázka se dvěma povinnými možnostmi
  (Schválit / Zamítnout) bez komentáře – reuse `question_reply`. Komentář u
  approve je možný budoucí rozšíření.
- Celá dávka otázek se vždy zodpoví naráz jedním modalem (backend
  `question_reply` to vyžaduje).
- Slack ořezává `option.description` na 75 znaků, takže plné popisy voleb
  renderujeme jako mrkdwn „legendu" v `section` bloku nad výběrem (až ~3000
  znaků); u samotného radia/checkboxů zůstává jen label.
