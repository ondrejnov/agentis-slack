"""Stdio MCP server, který klade otázky uživateli přes Slack.

Stejný tool surface jako ``agentis_sdk.mcp_server`` (nástroje ``question`` a
``approve``), takže pro adaptér je to drop-in náhrada – liší se jen I/O vrstva:
otázka se neřeší ve webovém UI Agentisu, ale rovnou v tom Slack threadu, ze
kterého task vznikl ("varianta B"):

1. Otázku zaregistrujeme na backendu (``task.add_question``) – tím se naváže na
   běh/task a vznikne ``external_id``, přes který se pak spáruje odpověď.
2. Do Slack threadu pošleme prompt s tlačítkem „Odpovědět"
   (:func:`agentis_slack.slack_blocks.question_prompt_blocks`). Klik a vyplnění
   modalu obsluhuje bolt app (viz ``main.py`` / ``slack_service.py``), která
   výsledek zapíše přes ``task.question_reply``.
3. Tady jen pollujeme ``task.get_question_result`` než odpověď dorazí, a vrátíme
   ji agentovi ve stejném formátu jako SDK MCP (``"otázka=odpověď; …"``).

Slack adresát se bere z env proměnných běhu (adaptér je plní z task headers –
viz ``agentis-adapter/common/models.py``): ``TASK_HEADER_SLACK_CHANNEL_ID`` a
``TASK_HEADER_SLACK_THREAD_TS`` (fallback ``…_MESSAGE_TS``), token z
``SLACK_BOT_TOKEN``. Když Slack kontext chybí, otázka se jen zaregistruje a
pollujeme (lze odpovědět ve webu) – běh se nikdy neshodí kvůli Slacku.

Spouští se přes stdio (z MCP configu adaptéru)::

    agentis-slack-mcp
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .agentis_client import AgentisClient, AgentisRpcError
from .config import Settings
from .slack_blocks import question_prompt_blocks

logger = logging.getLogger("agentis_slack.question_mcp")

APPROVE_LABEL = "Schválit"
REJECT_LABEL = "Zamítnout"


# -- vstupní modely (zrcadlí agentis_sdk.mcp_server) -----------------------


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    label: str = Field(..., min_length=1, max_length=255, description="Short option label.")
    description: str = Field(
        ..., min_length=1, max_length=1000, description="What this option means."
    )

    @field_validator("label", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


class QuestionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    question: str = Field(..., min_length=1, max_length=4000, description="The question text.")
    header: str = Field(..., min_length=1, max_length=30, description="Short header / topic chip.")
    options: list[QuestionOption] = Field(
        default_factory=list, description="Predefined answer options (may be empty)."
    )
    multiple: bool = Field(default=False, description="Allow selecting multiple options.")
    allow_freeform_input: bool = Field(default=True, description="Allow a free-form custom answer.")

    @field_validator("question", "header")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @model_validator(mode="after")
    def _require_answer_path(self) -> "QuestionInfo":
        if not self.options and not self.allow_freeform_input:
            raise ValueError("question must provide options or allow a custom answer")
        return self

    def to_agentis(self) -> dict:
        return {
            "question": self.question,
            "header": self.header,
            "options": [option.model_dump() for option in self.options],
            "multiple": self.multiple,
            "allowFreeformInput": self.allow_freeform_input,
        }


# -- session resolution (stejné jako agentis_sdk.mcp_server) ---------------


def _find_claude_pid(bin: str = "claude") -> int | None:
    """Vylez stromem procesů a najdi rodičovský proces Claude Code."""
    try:
        import psutil
    except ImportError:
        logger.warning("psutil not installed; cannot resolve session pid")
        return None
    try:
        process = psutil.Process(os.getpid())
        while process.parent() is not None:
            process = process.parent()
            try:
                if bin in process.name().lower():
                    return process.pid
                cmdline = " ".join(process.cmdline())
                if bin in cmdline and "code" in cmdline.lower():
                    return process.pid
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except Exception as exc:  # pragma: no cover - defenzivní
        logger.warning("failed to walk process tree: %s", exc)
    return None


def resolve_session_id() -> str | None:
    """Zjisti původní session id ze souboru session nebo z env."""
    pid = _find_claude_pid("claude") or _find_claude_pid("opencode")
    if pid is not None:
        session_file = f"/tmp/claude-session-{pid}"
        try:
            with open(session_file) as handle:
                session_id = handle.read().strip()
            if session_id:
                logger.info("resolved session id %s from %s", session_id, session_file)
                return session_id
        except OSError as exc:
            logger.info("no session file at %s: %s", session_file, exc)
    fallback = (os.environ.get("AGENTIS_DEFAULT_SESSION_ID") or "").strip()
    return fallback or None


# -- Slack kontext + odeslání promptu --------------------------------------


def _slack_context() -> dict[str, str]:
    get = os.environ.get
    return {
        "channel": get("TASK_HEADER_SLACK_CHANNEL_ID") or get("TASK_HEADER_SLACK_CHANNEL", ""),
        "thread_ts": (
            get("TASK_HEADER_SLACK_THREAD_TS") or get("TASK_HEADER_SLACK_MESSAGE_TS", "")
        ),
        "token": os.environ.get("SLACK_BOT_TOKEN", ""),
    }


def _post_prompt(questions: list[QuestionInfo], external_id: str, task_id: str | None) -> None:
    """Pošli do Slack threadu prompt s tlačítkem „Odpovědět".

    Bez Slack kontextu (nejde o slack task) jen tiše skončí – pollovat
    ``get_question_result`` se dá i tak (odpověď přijde z webu).
    """
    context = _slack_context()
    if not (context["channel"] and context["thread_ts"] and context["token"]):
        logger.info("slack context missing; skipping prompt, will poll backend only")
        return

    first = questions[0]
    blocks = question_prompt_blocks(
        header=first.header,
        summary=first.question,
        count=len(questions),
        external_id=external_id,
        task_id=task_id,
    )
    try:
        from slack_sdk import WebClient

        WebClient(token=context["token"]).chat_postMessage(
            channel=context["channel"],
            thread_ts=context["thread_ts"],
            text=f"{first.header}: {first.question}",
            blocks=blocks,
        )
    except Exception as exc:  # noqa: BLE001 - Slack chyba nesmí shodit běh
        logger.warning("failed to post Slack question prompt: %s", exc)


def _format_answers(questions: list[QuestionInfo], answers: list) -> str:
    pairs: list[str] = []
    for index, question in enumerate(questions):
        answer = answers[index] if index < len(answers) else []
        if isinstance(answer, list):
            answer_text = ", ".join(str(item) for item in answer)
        else:
            answer_text = str(answer)
        pairs.append(f"{question.question}={answer_text}")
    return "; ".join(pairs)


# -- logging / config helpers ----------------------------------------------


def _setup_logger() -> None:
    if logger.handlers:
        return
    log_path = os.environ.get("AGENTIS_SLACK_MCP_LOG_FILE", "/tmp/agentis-slack-mcp.log")
    logger.setLevel(logging.DEBUG)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def _float_env(key: str, default: float) -> float:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _wait_for_answer(
    client: AgentisClient, external_id: str, *, poll_interval: float, timeout: float
) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        result = client.get_question_result(external_id)
        if not result.get("pending", True):
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Otázka {external_id} nebyla zodpovězena do {timeout:.0f} s.")
        time.sleep(max(poll_interval, 0.1))


# -- server ----------------------------------------------------------------


def build_server() -> FastMCP:
    _setup_logger()
    settings = Settings.from_env()
    client = AgentisClient(settings.agentis_api_url, settings.agentis_token)
    answer_timeout = _float_env("AGENTIS_MCP_ANSWER_TIMEOUT", 900.0)
    poll_interval = _float_env("AGENTIS_MCP_POLL_INTERVAL", 1.0)
    logger.info("Agentis Slack MCP starting (base_url=%s)", settings.agentis_api_url)

    server = FastMCP("agentis-slack")

    @server.tool(
        name="question",
        description=(
            "Ask the user one or more questions and wait for the answers. "
            "Always use this for user's interaction"
        ),
    )
    def question(
        ctx: Context,
        questions: list[QuestionInfo] = Field(..., description="List of questions to ask the user."),
    ) -> str:
        session_id = resolve_session_id()
        external_id = uuid.uuid4().hex
        payload = [item.to_agentis() for item in questions]
        try:
            registered = client.add_question(payload, external_id=external_id, session_id=session_id)
        except AgentisRpcError as exc:
            logger.exception("add_question failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        task_id = registered.get("task_id") if isinstance(registered, dict) else None
        _post_prompt(questions, external_id, task_id)

        result = _wait_for_answer(
            client, external_id, poll_interval=poll_interval, timeout=answer_timeout
        )
        formatted = _format_answers(questions, result.get("answers") or [])
        logger.info("question answered: %s", formatted)
        return formatted

    @server.tool(
        name="approve",
        description=(
            "Ask the user for a yes/no approval. Returns APPROVED or REJECTED."
        ),
    )
    def approve(
        ctx: Context,
        title: str = Field(..., description="Short name of the approval."),
        description: str = Field(..., description="What the user is approving or rejecting."),
    ) -> str:
        approval = QuestionInfo(
            question=description,
            header=title[:30],
            options=[
                QuestionOption(label=APPROVE_LABEL, description="Schválit a pokračovat."),
                QuestionOption(label=REJECT_LABEL, description="Zamítnout."),
            ],
            multiple=False,
            allow_freeform_input=False,
        )
        session_id = resolve_session_id()
        external_id = uuid.uuid4().hex
        try:
            registered = client.add_question(
                [approval.to_agentis()], external_id=external_id, session_id=session_id
            )
        except AgentisRpcError as exc:
            logger.exception("add_question (approve) failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        task_id = registered.get("task_id") if isinstance(registered, dict) else None
        _post_prompt([approval], external_id, task_id)

        result = _wait_for_answer(
            client, external_id, poll_interval=poll_interval, timeout=answer_timeout
        )
        answers = result.get("answers") or [[]]
        first = answers[0] if answers else []
        labels = [str(item) for item in (first if isinstance(first, list) else [first])]
        decision = "APPROVED" if APPROVE_LABEL in labels else "REJECTED"
        logger.info("approve decided: %s", decision)
        return decision

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
