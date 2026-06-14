#!/usr/bin/env python3
"""Tee JSON Lines eventů agenta + průběžný text streaming do Slacku.

Použití ve workflow kroku (viz `.agentis/workflows/slack.yaml`):

    agentiscode --json ... | python3 scripts/slack_stream.py

Stdin (JSON Lines z `agentiscode --json`) se beze změny propouští na stdout,
takže log kroku zůstává kompletní. Po cestě se z eventů skládají jednořádkové
kroky („právě běží nástroj X“, poslední reasoning) a ty se přilepují jako
streamovací řádky do samostatné AI-agent streaming zprávy podle
https://docs.slack.dev/ai/developing-agents — `chat.startStream` ji založí,
`chat.appendStream` doplňuje další řádky a `chat.stopStream` ji na konci uzavře.

Konfigurace přes env (chybějící hodnoty = čistý tee, žádné volání Slacku):

- ``TASK_HEADER_SLACK_CHANNEL`` / ``TASK_HEADER_SLACK_THREAD_TS`` — adresát a
  kořen threadu, pod který se streamovaná zpráva pověsí (předává bridge v task
  headers; ``THREAD_TS`` fallbackuje na ``MESSAGE_TS``),
- ``TASK_HEADER_SLACK_USER`` / ``TASK_HEADER_SLACK_TEAM`` — recipient pro stream
  do kanálu (Slack je u kanálových streamů vyžaduje; v DM/assistant threadu se
  vynechají),
- ``SLACK_BOT_TOKEN`` — ze sourcovaného ``slack.env``,
- ``SLACK_STREAM_INTERVAL`` — minimální odstup appendů v sekundách (default 3).
  ``chat.appendStream`` je Slack rate-limit Tier 4 (100+/min), takže prostor je;
  throttle jen sdružuje řádky, ať append nestřílíme po jednom znaku.

Updater nikdy neshazuje pipeline: chyby Slacku jen loguje na stderr a stream
propouští dál. Finální odpověď posílá jako samostatnou zprávu až následný krok
workflow — tady se streamuje jen průběh.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

STREAM_HEADER = "⏳ _Pracuju na tom…_"
SNIPPET_LIMIT = 150


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


def _slack_api(method: str, token: str, payload: dict) -> dict | None:
    """Zavolej Slack Web API metodu JSONem. Nikdy nevyhazuje — chyby loguje.

    Vrací rozparsované tělo odpovědi (i když ``ok`` je false, ať si volající
    může vytáhnout např. ``ts``), nebo ``None`` při síťové/parsovací chybě.
    """
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — updater nesmí shodit běh agenta
        print(f"slack-stream: {method} selhal: {exc}", file=sys.stderr)
        return None
    if not body.get("ok"):
        print(f"slack-stream: {method} odmítnut: {body.get('error')}", file=sys.stderr)
    return body


def _start_stream(
    channel: str,
    thread_ts: str,
    token: str,
    *,
    recipient_user_id: str = "",
    recipient_team_id: str = "",
    text: str = "",
) -> str | None:
    """Založ AI-agent streaming zprávu (`chat.startStream`) a vrať její ``ts``.

    ``recipient_*`` Slack vyžaduje jen u streamu do kanálu; v DM/assistant
    threadu jsou prázdné a vynecháme je. Při selhání vrací ``None`` (volající si
    pak streaming vypne a doběhne jako čistý tee).
    """
    payload: dict = {"channel": channel, "thread_ts": thread_ts}
    if text:
        payload["markdown_text"] = text
    if recipient_user_id:
        payload["recipient_user_id"] = recipient_user_id
    if recipient_team_id:
        payload["recipient_team_id"] = recipient_team_id
    body = _slack_api("chat.startStream", token, payload)
    if not body or not body.get("ok"):
        return None
    return body.get("ts")


def _append_stream(
    channel: str, ts: str, thread_ts: str, token: str, text: str
) -> None:
    """Přilep další kus textu do běžícího streamu (`chat.appendStream`).

    ``markdown_text`` se k dosavadní zprávě připojuje, posíláme tedy jen delta
    (nové řádky), ne celý log znovu. Limit pole je 12 000 znaků na volání — naše
    řádky jsou krátké, takže s rezervou stačí.
    """
    _slack_api(
        "chat.appendStream",
        token,
        {"channel": channel, "ts": ts, "thread_ts": thread_ts, "markdown_text": text},
    )


def _stop_stream(channel: str, ts: str, thread_ts: str, token: str) -> None:
    """Uzavři stream (`chat.stopStream`) — Slack zprávu finalizuje a sundá z ní
    streamovací indikátor. Žádný další ``markdown_text`` už nepřidáváme; finální
    odpověď posílá samostatně následný krok workflow."""
    _slack_api(
        "chat.stopStream",
        token,
        {"channel": channel, "ts": ts, "thread_ts": thread_ts},
    )


def _post_status(
    channel: str,
    thread_ts: str,
    token: str,
    status: str,
) -> None:
    """Nastav nativní stav assistant threadu přes `assistant.threads.setStatus`.

    Slack ho zobrazí jako „<App> <status>“ v hlavičce threadu. Má ~2min timeout,
    po kterém zmizí sám; prázdný ``status`` ho smaže ručně. Volitelné
    ``loading_messages`` (max 10) Slack sám rotuje jako loading animaci, dokud
    neznáme konkrétní krok.
    """
    payload: dict = {"channel_id": channel, "thread_ts": thread_ts, "status": status}
    _slack_api("assistant.threads.setStatus", token, payload)


def _status_from_steps(steps: list[tuple[str, str]]) -> tuple[str, list[str] | None]:
    """Nativní stav threadu z posledního dění: ``(status, loading_messages)``.

    Bere poslední ne-`text` krok (finální odpověď stav nepopisuje) a zbaví ho
    mrkdwn backticků, ať se ve stavovém řádku nezobrazí doslova — ten vrací jako
    konkrétní ``status`` bez rotace. Když ještě žádný krok není, vrátí první
    devops hlášku jako ``status`` a celý pool jako ``loading_messages``, mezi
    kterými pak Slack rotuje sám.
    """
    return "is working...", None
    # for kind, line in reversed(steps):
    #     if kind != "text":
    #         return line.replace("`", ""), list(LOADING_MESSAGES)
    # return LOADING_MESSAGES[0], list(LOADING_MESSAGES)


def _step_from_event(event: dict) -> tuple[str, str] | None:
    """Slož jeden řádek logu z eventu `agentiscode --json`.

    Eventy jsou ploché — pole (`name`, `status`, `title`, `text`, …) jsou přímo
    na top-levelu eventu, žádný vnořený `data` klíč není. Vrací dvojici
    ``(kind, řádek)`` bez hlavičky; tu přidává start streamu jednou na začátku.
    ``kind`` rozlišuje typ kroku, aby volající poznal koncový `text` (finální
    odpověď) a nepřilepil ho do streamu — tu posílá samostatně až následný krok
    workflow.
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
        if name == "Bash":
            return "tool", f"🔧 `{name}`{suffix}"
        if name == "Read":
            return "tool", f"📖 `{name}`{suffix}"
        elif name == "Edit" or name == "Write":
            return "tool", f"✏️ `{name}`{suffix}"
        elif name == "Webfetch":
            return "tool", f"🌐 `{name}`{suffix}"
        elif name.startswith("mcp__"):
            return "tool", f"⛏️ `{name}`{suffix}"
        else:
            return "tool", f"⚙ `{name}`{suffix}"

    if event_type == "text":
        text = (event.get("text") or "").strip()
        if text:
            return "text", f"💭 {text.splitlines()[0][:SNIPPET_LIMIT]}"
    if event_type == "error":
        message = (event.get("message") or "chyba").strip()
        return "error", f"✗ {message.splitlines()[0][:SNIPPET_LIMIT]}"
    return None


def main() -> int:
    channel = os.environ.get("TASK_HEADER_SLACK_CHANNEL", "")
    message_ts = os.environ.get("TASK_HEADER_SLACK_MESSAGE_TS", "")
    # Stream věšíme pod kořen threadu; u top-level mention je shodný s message_ts
    # (viz slack_service.build_headers).
    thread_ts = os.environ.get("TASK_HEADER_SLACK_THREAD_TS", "") or message_ts
    # Recipient potřebuje Slack jen u streamu do kanálu; v DM zůstanou prázdné.
    recipient_user_id = os.environ.get("TASK_HEADER_SLACK_USER", "") or os.environ.get(
        "TASK_HEADER_SLACK_USER_ID", ""
    )
    recipient_team_id = os.environ.get("TASK_HEADER_SLACK_TEAM", "") or os.environ.get(
        "TASK_HEADER_SLACK_TEAM_ID", ""
    )
    token = _resolve_token()
    min_interval = float(os.environ.get("SLACK_STREAM_INTERVAL", "3"))
    enabled = bool(channel and thread_ts and token)
    if not enabled:
        print(
            "slack-stream: chybí channel/thread_ts/token, běžím jen jako tee",
            file=sys.stderr,
        )

    steps: list[tuple[str, str]] = []
    # Řádky čekající na přilepení do streamu (delta od posledního appendu).
    pending: list[str] = []
    # Poslední `text` krok držíme stranou: může to být finální odpověď, kterou do
    # streamu nechceme. Přilepíme ho, až dorazí další krok (čímž se ukáže, že
    # finální nebyl); pokud žádný další nepřijde, na konci ho zahodíme.
    held_text: str | None = None
    stream_ts: str | None = None
    stream_failed = False
    last_sent = 0.0

    def flush() -> None:
        nonlocal stream_ts, stream_failed
        if not pending or stream_failed:
            return
        chunk = "\n".join(pending)
        if stream_ts is None:
            stream_ts = _start_stream(
                channel,
                thread_ts,
                token,
                recipient_user_id=recipient_user_id,
                recipient_team_id=recipient_team_id,
                text=f"{STREAM_HEADER}\n{chunk}",
            )
            if stream_ts is None:
                # Start streamu selhal (typicky chybí recipient_* u kanálu) — víc
                # to nezkoušej, ať nezahltíš stderr, a doběhni jako čistý tee.
                stream_failed = True
                pending.clear()
                return
        else:
            # appendStream připojuje, tak před delta řádky dej odřádkování.
            _append_stream(channel, stream_ts, thread_ts, token, f"\n{chunk}")
        pending.clear()
        # Vedle streamu drž i nativní stav threadu, ať Slack ukazuje
        # „<App> is working…“ i mimo tělo zprávy. Throttlujeme stejně.
        status, loading_messages = _status_from_steps(steps)
        _post_status(channel, thread_ts, token, status)

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
            if held_text is not None:
                # Dorazil novější krok, takže držený `text` nebyl finální —
                # přilep ho a uvolni místo.
                pending.append(held_text)
                held_text = None
            if step[0] == "text":
                held_text = step[1]
            else:
                pending.append(step[1])
        if pending and time.monotonic() - last_sent >= min_interval:
            flush()
            last_sent = time.monotonic()

    # Doraz poslední nasbírané řádky i když throttle okno ještě neuplynulo, ať je
    # stream kompletní; `held_text` (případná finální odpověď) zahazujeme.
    if enabled:
        flush()
        if stream_ts is not None:
            _stop_stream(channel, stream_ts, thread_ts, token)
        # Smaž nativní stav threadu — agent doběhl, prázdný status sundá
        # „is working…“.
        _post_status(channel, thread_ts, token, "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
