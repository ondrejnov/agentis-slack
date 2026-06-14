#!/usr/bin/env python3
"""Tee JSON Lines eventů agenta + průběžná editace pending zprávy ve Slacku.

Použití ve workflow kroku (viz `.agentis/workflows/slack.yaml`):

    agentiscode --json ... | python3 scripts/slack_stream.py

Stdin (JSON Lines z `agentiscode --json`) se beze změny propouští na stdout,
takže log kroku zůstává kompletní. Po cestě se z eventů skládá jednořádkový
stav („právě běží nástroj X“, poslední reasoning) a throttlovaně se jím
edituje pending zpráva ve Slacku přes `chat.update`.

Konfigurace přes env (chybějící hodnoty = čistý tee, žádné volání Slacku):

- ``TASK_HEADER_SLACK_CHANNEL`` / ``TASK_HEADER_SLACK_MESSAGE_TS`` — adresát,
  ts pending zprávy posílá bridge v task headers,
- ``SLACK_BOT_TOKEN`` — ze sourcovaného ``slack.env``,
- ``SLACK_STREAM_INTERVAL`` — minimální odstup editací v sekundách (default 3;
  chat.update je Slack rate-limit Tier 3, ~50/min, default drží ~20/min).

Updater nikdy neshazuje pipeline: chyby Slacku jen loguje na stderr a stream
propouští dál. Finální odpověď do pending zprávy zapisuje až následný krok
workflow — tady se řeší jen průběh.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

PENDING_PREFIX = "⏳ _Pracuju na tom…_"
SNIPPET_LIMIT = 150
# Kolik posledních kroků držet v logu. chat.update má strop ~4000 znaků,
# tohle drží zprávu pohodlně pod ním i s dlouhými řádky.
MAX_LOG_LINES = 30


def _read_env_file(name: str) -> str:
    """Najdi proměnnou ``name`` v nejbližším ``.env`` od tohoto souboru nahoru.

    Vrátí prázdný řetězec, když soubor nebo klíč neexistuje. Slouží jako
    fallback, když proměnná není v prostředí (např. lokální spuštění mimo
    workflow, které jinak sourcuje ``slack.env``).
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / ".env"
        if not candidate.is_file():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip("\"'")
        except OSError:
            return ""
        break
    return ""


def _resolve_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN") or _read_env_file("SLACK_BOT_TOKEN")


def _post_update(channel: str, ts: str, token: str, text: str) -> None:
    payload = {"channel": channel, "ts": ts, "text": text}
    request = urllib.request.Request(
        "https://slack.com/api/chat.update",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            print(
                f"slack-stream: chat.update odmítnut: {body.get('error')}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — updater nesmí shodit běh agenta
        print(f"slack-stream: chat.update selhal: {exc}", file=sys.stderr)


def _step_from_event(event: dict) -> tuple[str, str] | None:
    """Slož jeden řádek logu z eventu `agentiscode --json`.

    Eventy jsou ploché — pole (`name`, `status`, `title`, `text`, …) jsou přímo
    na top-levelu eventu, žádný vnořený `data` klíč není. Vrací dvojici
    ``(kind, řádek)`` bez hlavičky; tu přidává `_render_log` jednou na začátek
    zprávy. ``kind`` rozlišuje typ kroku, aby `_render_log` poznal koncový
    `text` (finální odpověď) a nezdvojoval ho — tu zapisuje samostatně až
    následný krok workflow.
    """
    event_type = event.get("type") or ""
    # Skill invokace přijde buď jako vlastní `type: "skill"` s atributy přímo na
    # eventu, nebo jako tool event (`name: "Skill"`), jehož vstup je vnořený
    # objekt `input: {"skill": ..., "args": ...}`. V obou případech bereme jméno
    # ze `skill` (top-level i z `input`), ať se nezobrazí generické "Skill".
    skill_input = event.get("input") if isinstance(event.get("input"), dict) else {}
    skill_name = event.get("skill") or skill_input.get("skill")
    if skill_name:
        args = (event.get("args") or skill_input.get("args") or "").strip()
        suffix = f" {args[:SNIPPET_LIMIT]}" if args else ""
        return "skill", f"🎓 `skill` {skill_name}"
    if event_type == "tool" and event.get("status") == "running":
        name = event.get("name") or "tool"
        title = event.get("title") or ""
        suffix = f" {title[:SNIPPET_LIMIT]}" if title and title != name else ""
        return "tool", f"⚙ `{name}`{suffix}"

    if event_type == "text":
        text = (event.get("text") or "").strip()
        if text:
            return "text", f"💭 {text.splitlines()[0][:SNIPPET_LIMIT]}"
    if event_type == "error":
        message = (event.get("message") or "chyba").strip()
        return "error", f"✗ {message.splitlines()[0][:SNIPPET_LIMIT]}"
    return None


def _render_log(steps: list[tuple[str, str]]) -> str:
    """Slož celou pending zprávu: hlavička + dosavadní kroky, každý na řádku.

    Drží jen posledních `MAX_LOG_LINES` kroků, aby zpráva nepřerostla strop
    chat.update; oříznutí naznačí vodorovné tři tečky.

    Koncový `text` krok se nezobrazuje: je to finální odpověď, kterou jako
    samostatnou zprávu zapisuje až následný krok workflow — jinak by ve Slacku
    visela dvakrát (tady v logu i tam).
    """
    visible = steps[:-1] if steps and steps[-1][0] == "text" else steps
    shown = visible[-MAX_LOG_LINES:]
    lines = [PENDING_PREFIX]
    if len(visible) > len(shown):
        lines.append("…")
    lines.extend(line for _, line in shown)
    return "\n".join(lines)


def main() -> int:
    channel = os.environ.get("TASK_HEADER_SLACK_CHANNEL", "")
    message_ts = os.environ.get("TASK_HEADER_SLACK_MESSAGE_TS", "")
    token = _resolve_token()
    min_interval = float(os.environ.get("SLACK_STREAM_INTERVAL", "3"))
    enabled = bool(channel and message_ts and token)
    if not enabled:
        print(
            "slack-stream: chybí channel/ts/token, běžím jen jako tee", file=sys.stderr
        )

    steps: list[tuple[str, str]] = []
    dirty = False
    last_sent = 0.0
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
        if not enabled:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        step = _step_from_event(event)
        if step and (not steps or steps[-1] != step):
            steps.append(step)
            # `text` krok se zatím nerenderuje (může být koncová odpověď, kterou
            # `_render_log` zahazuje), tak kvůli němu netrigujeme chat.update —
            # ale nesmažeme dirty z dřívějšího kroku, ať se stihne doflushnout.
            if step[0] != "text":
                dirty = True
        if dirty and time.monotonic() - last_sent >= min_interval:
            _post_update(channel, message_ts, token, _render_log(steps))
            last_sent = time.monotonic()
            dirty = False

    # Doraz poslední krok i když throttle okno ještě neuplynulo, ať je log
    # kompletní pro toho, kdo se kouká až po doběhnutí.
    if enabled and dirty:
        _post_update(channel, message_ts, token, _render_log(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
