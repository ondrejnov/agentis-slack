from __future__ import annotations

import re
from collections.abc import Callable


MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
LINK_RE = re.compile(r"<([^>|]+)\|([^>]+)>")
RAW_LINK_RE = re.compile(r"<([^>|]+)>")
FENCED_CODE_RE = re.compile(r"```[^\n`]*\n?(.*?)```", flags=re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


def _split_markdown_table_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(
        TABLE_SEPARATOR_CELL_RE.match(cell.replace(" ", "")) for cell in cells
    )


def _is_markdown_table_row(line: str) -> bool:
    value = line.strip()
    return "|" in value and not value.startswith("```")


def _format_markdown_table(lines: list[str], escape: Callable[[str], str]) -> str:
    rows = [_split_markdown_table_row(line) for line in lines]
    rows = [row for index, row in enumerate(rows) if index != 1]
    rows = [[escape(cell) for cell in row] for row in rows]
    column_count = max(len(row) for row in rows)
    normalized = [row + [""] * (column_count - len(row)) for row in rows]
    widths = [
        max(len(row[index]) for row in normalized) for index in range(column_count)
    ]

    output: list[str] = []
    for index, row in enumerate(normalized):
        output.append(
            " | ".join(
                cell
                if column_index == column_count - 1
                else cell.ljust(widths[column_index])
                for column_index, cell in enumerate(row)
            )
        )
        if index == 0:
            output.append("-|-".join("-" * width for width in widths))
    return "\n".join(output)


def _convert_markdown_tables(
    text: str, protect: Callable[[str], str], escape: Callable[[str], str]
) -> str:
    lines = text.split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        if (
            index + 1 >= len(lines)
            or not _is_markdown_table_row(lines[index])
            or not _is_markdown_table_separator(lines[index + 1])
        ):
            output.append(lines[index])
            index += 1
            continue

        table_lines = [lines[index], lines[index + 1]]
        index += 2
        while index < len(lines) and _is_markdown_table_row(lines[index]):
            table_lines.append(lines[index])
            index += 1

        output.append(
            protect(f"```\n{_format_markdown_table(table_lines, escape)}\n```")
        )

    return "\n".join(output)


def normalize_slack_text(text: str, *, bot_user_id: str | None = None) -> str:
    if bot_user_id:
        text = text.replace(f"<@{bot_user_id}>", "")
    text = MENTION_RE.sub("", text)
    text = LINK_RE.sub(lambda match: f"{match.group(2)} ({match.group(1)})", text)
    text = RAW_LINK_RE.sub(lambda match: match.group(1), text)
    return " ".join(text.split()).strip()


def markdown_to_slack_mrkdwn(text: str) -> str:
    tokens: list[str] = []

    def protect(value: str) -> str:
        tokens.append(value)
        return f"SLACKMDTOKEN{len(tokens) - 1}END"

    def restore(value: str) -> str:
        previous = None
        while value != previous:
            previous = value
            for index, token in enumerate(tokens):
                value = value.replace(f"SLACKMDTOKEN{index}END", token)
        return value

    def escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def fenced_code(match: re.Match[str]) -> str:
        code = escape(match.group(1))
        if code.endswith("\n"):
            return protect(f"```\n{code}```")
        return protect(f"```\n{code}\n```")

    def inline_code(match: re.Match[str]) -> str:
        return protect(f"`{escape(match.group(1))}`")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = FENCED_CODE_RE.sub(fenced_code, text)
    text = _convert_markdown_tables(text, protect, escape)
    text = INLINE_CODE_RE.sub(inline_code, text)
    text = escape(text)

    text = IMAGE_RE.sub(
        lambda match: protect(f"<{match.group(2)}|{match.group(1) or match.group(2)}>"),
        text,
    )
    text = MARKDOWN_LINK_RE.sub(
        lambda match: protect(f"<{match.group(2)}|{match.group(1)}>"), text
    )
    text = re.sub(
        r"\*\*([^\n]+?)\*\*", lambda match: protect(f"*{match.group(1)}*"), text
    )
    text = re.sub(r"__([^\n]+?)__", lambda match: protect(f"*{match.group(1)}*"), text)
    text = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        lambda match: protect(f"_{match.group(1)}_"),
        text,
    )
    text = re.sub(r"~~([^\n]+?)~~", lambda match: protect(f"~{match.group(1)}~"), text)
    text = re.sub(
        r"^(#{1,6})\s+(.+)$",
        lambda match: protect(f"*{match.group(2)}*"),
        text,
        flags=re.MULTILINE,
    )

    return restore(text)


def slack_history_to_context(messages: list[dict]) -> str:
    lines: list[str] = []
    for message in messages:
        profile = message.get("user_profile") or {}
        user = (
            message.get("user_real_name")
            or profile.get("real_name")
            or profile.get("display_name")
            or message.get("username")
            or message.get("user")
            or "unknown"
        )
        text = normalize_slack_text(str(message.get("text") or ""))
        if text:
            lines.append(f"[{user}] {text}")
    return "\n".join(lines)
