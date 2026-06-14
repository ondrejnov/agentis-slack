"""Block Kit buildery pro otázky položené přes Slack.

Tok (varianta B – „interactive bridge"):

1. MCP server (:mod:`agentis_slack.question_mcp`) zaregistruje otázku na backendu
   (``task.add_question``) a do Slack threadu pošle prompt s tlačítkem
   „Odpovědět" – :func:`question_prompt_blocks`. Tlačítko nese ``external_id`` a
   ``task_id`` v ``value``.
2. Klik na tlačítko zachytí bolt app (``@app.action(QUESTION_OPEN_ACTION)``),
   z backendu si přes ``task.fetch`` natáhne celou dávku otázek (s UUID otázek i
   options) a otevře modal – :func:`build_question_modal`.
3. Submit modalu (``@app.view(QUESTION_SUBMIT_CALLBACK)``) se přes
   :func:`parse_modal_submission` přeloží na ``results`` pro ``task.question_reply``.

Modal řeší celou dávku najednou, protože backend ``question_reply`` vyžaduje
odpovědi na všechny otázky v dávce v jednom volání.
"""

from __future__ import annotations

import json
from typing import Any

# action_id tlačítka „Odpovědět" v promptu i callback_id modalu – bolt app na ně
# váže handlery, MCP server je používá při skládání promptu.
QUESTION_OPEN_ACTION = "agentis_q_open"
QUESTION_SUBMIT_CALLBACK = "agentis_q_submit"

# action_id prvků uvnitř modalu a suffix block_id pro volný text. block_id
# options bloku je rovnou UUID otázky; freeform blok má suffix, ať jdou po submitu
# spárovat zpět ke stejné otázce.
_OPTION_ACTION = "opt"
_FREEFORM_ACTION = "free"
_FREEFORM_SUFFIX = "::free"

# Slack limity: label volby (plain_text) ~75 znaků a hlavně option.description
# je taky max 75 znaků – proto celé popisy renderujeme jako mrkdwn legendu v
# section bloku (text až ~3000 znaků), ne přes option.description.
_OPTION_LABEL_LIMIT = 75
_SECTION_TEXT_LIMIT = 2900


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _legend_sections(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Section bloky s plnými popisy voleb (``*label* — popis``).

    Slack ořezává ``option.description`` na 75 znaků; v sekci se vejde ~3000, tak
    sem dáme celé popisy a u samotného radia/checkboxů necháme jen label. Řádky
    chunkujeme, aby žádná sekce nepřekročila limit.
    """
    lines: list[str] = []
    for option in options:
        label = (option.get("label") or "").strip()
        description = (option.get("description") or "").strip()
        lines.append(f"*{label}* — {description}" if description else f"*{label}*")

    blocks: list[dict[str, Any]] = []
    chunk: list[str] = []
    length = 0
    for line in lines:
        if chunk and length + len(line) + 1 > _SECTION_TEXT_LIMIT:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}}
            )
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
    return blocks


def question_prompt_blocks(
    *,
    header: str,
    summary: str,
    count: int,
    external_id: str,
    task_id: str | None,
) -> list[dict[str, Any]]:
    """Blocky thread zprávy, která uživatele vyzve k odpovědi tlačítkem.

    ``external_id`` + ``task_id`` se vezou v ``value`` tlačítka; bolt si z nich
    při kliku dotáhne dávku z backendu. ``channel``/``ts`` zprávy embedovat
    nemusíme – ty bolt dostane přímo v action payloadu.
    """
    value = json.dumps({"e": external_id, "t": task_id})
    lines = [f"*{header}*" if header else "*Otázka*", summary]
    if count > 1:
        lines.append(f"_(+ dalších {count - 1} otázek)_")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "❓ Odpovědět", "emoji": True},
                    "action_id": QUESTION_OPEN_ACTION,
                    "value": value,
                }
            ],
        },
    ]


def build_question_modal(
    group: dict[str, Any], *, channel: str, message_ts: str
) -> dict[str, Any]:
    """Sestav modal pro celou dávku otázek z ``task.fetch`` ``questions`` skupiny.

    ``group`` má tvar z ``get_pending_question_groups_for_task``::

        {"external_id": ..., "questions": [
            {"id", "header", "question", "multiple", "allowFreeformInput",
             "options": [{"id", "label", "description", "selected"}]}
        ]}

    Pravidla povinnosti: jsou-li u otázky options, výběr je povinný, ledaže je
    povolený i volný text (pak je volitelné obojí a hlídá se „aspoň jedno" až při
    submitu). Bez options je povinný volný text.
    """
    blocks: list[dict[str, Any]] = []
    for question in group.get("questions") or []:
        qid = str(question["id"])
        header = (question.get("header") or "").strip()
        if header:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{header}*"}]}
            )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": question.get("question") or ""}}
        )

        options = question.get("options") or []
        allow_free = bool(question.get("allowFreeformInput"))
        multiple = bool(question.get("multiple"))

        if options:
            # Plné popisy jdou do legendy nad výběrem (Slack by option.description
            # ořízl na 75 znaků); u voleb necháme jen label.
            if any((option.get("description") or "").strip() for option in options):
                blocks.extend(_legend_sections(options))
            elements = [
                {
                    "text": {
                        "type": "plain_text",
                        "text": _truncate(option.get("label") or "", _OPTION_LABEL_LIMIT),
                    },
                    "value": str(option["id"]),
                }
                for option in options
            ]
            blocks.append(
                {
                    "type": "input",
                    "block_id": qid,
                    "optional": allow_free,
                    "label": {
                        "type": "plain_text",
                        "text": "Vyber možnosti" if multiple else "Vyber možnost",
                    },
                    "element": {
                        "type": "checkboxes" if multiple else "radio_buttons",
                        "action_id": _OPTION_ACTION,
                        "options": elements,
                    },
                }
            )

        if allow_free:
            blocks.append(
                {
                    "type": "input",
                    "block_id": qid + _FREEFORM_SUFFIX,
                    "optional": bool(options),
                    "label": {"type": "plain_text", "text": "Vlastní odpověď"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": _FREEFORM_ACTION,
                        "multiline": True,
                    },
                }
            )

    private_metadata = json.dumps(
        {"e": str(group.get("external_id")), "c": channel, "ts": message_ts}
    )
    return {
        "type": "modal",
        "callback_id": QUESTION_SUBMIT_CALLBACK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Odpověď"},
        "submit": {"type": "plain_text", "text": "Odeslat"},
        "close": {"type": "plain_text", "text": "Zrušit"},
        "blocks": blocks,
    }


def parse_modal_submission(
    view: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, Any]], dict[str, str]]:
    """Přelož submit modalu na ``(external_id, channel, message_ts, results, errors)``.

    ``results`` je přímo vstup pro ``task.question_reply``. ``errors`` (block_id →
    hláška) je neprázdné jen u otázky, kde je výběr i volný text volitelný a
    uživatel nevyplnil ani jedno – ostatní povinnost vynutí Slack sám.
    """
    metadata = json.loads(view.get("private_metadata") or "{}")
    external_id = str(metadata.get("e") or "")
    channel = str(metadata.get("c") or "")
    message_ts = str(metadata.get("ts") or "")

    state = (view.get("state") or {}).get("values") or {}
    per_question: dict[str, dict[str, Any]] = {}
    for block_id, actions in state.items():
        if block_id.endswith(_FREEFORM_SUFFIX):
            qid = block_id[: -len(_FREEFORM_SUFFIX)]
            value = (actions.get(_FREEFORM_ACTION) or {}).get("value")
            entry = per_question.setdefault(qid, {"selected": [], "text": None})
            entry["text"] = (value or "").strip() or None
        else:
            qid = block_id
            action = actions.get(_OPTION_ACTION) or {}
            selected: list[str] = []
            if "selected_option" in action:
                chosen = action.get("selected_option")
                if chosen:
                    selected = [str(chosen["value"])]
            elif "selected_options" in action:
                selected = [str(option["value"]) for option in action.get("selected_options") or []]
            entry = per_question.setdefault(qid, {"selected": [], "text": None})
            entry["selected"] = selected

    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for qid, data in per_question.items():
        selected = data.get("selected") or []
        text = data.get("text")
        if not selected and not text:
            errors[qid] = "Vyber možnost nebo napiš vlastní odpověď."
        results.append(
            {"question_id": qid, "selected_options": selected, "answer_text": text}
        )

    return external_id, channel, message_ts, results, errors
