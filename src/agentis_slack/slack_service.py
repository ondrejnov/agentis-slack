from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from agentis_slack.agentis_client import AgentisClient

from .config import Settings
from .guards import EventDeduper, GlobalRateLimiter, should_ignore_event
from .text import (
    markdown_to_slack_mrkdwn,
    normalize_slack_text,
    slack_history_to_context,
)


ReplyBuilder = Callable[[str, dict[str, Any]], str]


def _remove_slack_reaction(
    slack_client: Any, channel_id, message_ts, name: str
) -> None:
    if not channel_id or not message_ts:
        return
    try:
        slack_client.reactions_remove(
            channel=channel_id, timestamp=message_ts, name=name
        )
    except Exception:
        return


def plaintext_to_lexical(text: str) -> dict:
    return {
        "root": {
            "type": "root",
            "format": "",
            "indent": 0,
            "version": 1,
            "children": [
                {
                    "type": "paragraph",
                    "format": "",
                    "indent": 0,
                    "version": 1,
                    "children": [
                        {
                            "type": "text",
                            "text": text,
                            "format": 0,
                            "style": "",
                            "mode": "normal",
                            "detail": 0,
                            "version": 1,
                        }
                    ],
                    "direction": None,
                    "textFormat": 0,
                    "textStyle": "",
                }
            ],
            "direction": None,
        }
    }


class SlackMentionService:
    def __init__(
        self,
        *,
        slack_client: Any,
        agentis_client: AgentisClient,
        settings: Settings,
        bot_user_id: str | None = None,
        rate_limit_max_events: int = 30,
        rate_limit_window_seconds: int = 60,
    ) -> None:
        self.slack_client = slack_client
        self.agentis_client = agentis_client
        self.settings = settings
        self.bot_user_id = bot_user_id
        self.deduper = EventDeduper()
        self.rate_limiter = GlobalRateLimiter(
            rate_limit_max_events, rate_limit_window_seconds
        )

    def handle_app_mention(
        self, event: dict[str, Any], *, event_id: str | None = None
    ) -> dict[str, Any]:
        if should_ignore_event(event, bot_user_id=self.bot_user_id):
            return {"ignored": True, "reason": "bot"}

        channel_id = str(event.get("channel") or "")
        message_ts = str(event.get("ts") or "")
        thread_ts = str(event.get("thread_ts") or message_ts)
        dedupe_key = event_id or f"{event.get('team')}:{channel_id}:{message_ts}"

        if self.deduper.seen_before(dedupe_key):
            return {"ignored": True, "reason": "duplicate"}
        if not self.rate_limiter.allow():
            return {"ignored": True, "reason": "rate_limited"}
        if not channel_id or not message_ts:
            return {"ignored": True, "reason": "missing_channel_or_ts"}

        text = normalize_slack_text(
            str(event.get("text") or ""), bot_user_id=self.bot_user_id
        )
        print(text)
        history = self.fetch_thread_history(channel_id, thread_ts)
        # attachments, file_refs = self.sync_event_files(history)
        headers = self.build_headers(event, thread_ts=thread_ts, files=[])
        context_text = slack_history_to_context(history)
        body = (
            text
            if not context_text
            else f"{text}\n\nSlack thread history:\n{context_text}"
        )

        task_data = {
            "title": text[:80] or "Slack mention",
            "description": plaintext_to_lexical(body),
            "headers": headers,
            # "attachments": attachments,
            "project": self.settings.default_project,
            "agent": self.settings.default_agent,
            "model": {
                "id": self.settings.default_model,
                "effort": self.settings.default_effort,
            }
            if self.settings.default_model
            else None,
            "adapter": self.settings.default_adapter,
            "adapter_options": {"engine": self.settings.default_adapter_engine}
            if self.settings.default_adapter_engine
            else None,
            "environment": self.settings.default_environment,
            # "labels": ["019e4eb9-6a52-7bbd-bcd6-fd9f7482263a"],
            "worktree": False,
        }
        saved = self.agentis_client.save_task(
            {key: value for key, value in task_data.items() if value is not None}
        )
        task_id = saved["form"]["id"]
        print(task_id)
        # run = self.agentis.start_run(task_id, start_adapter=True)
        self.slack_client.reactions_add(
            channel=channel_id,
            timestamp=message_ts,
            name="eyes",
        )
        final_message = self.run_agentiscode(task_id, body)

        reply = markdown_to_slack_mrkdwn(final_message)
        posted = self.slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=reply,
            mrkdwn=True,
        )
        _remove_slack_reaction(self.slack_client, channel_id, message_ts, "eyes")

        self.agentis_client.add_agent_comment(
            task=task_id, body=final_message, status=5
        )

        return {"replied": True, "message": posted}

    def handle_message(self, event: dict[str, Any], event_id) -> dict[str, Any]:
        if should_ignore_event(event, bot_user_id=self.bot_user_id):
            return {"ignored": True, "reason": "bot"}

        channel_id = str(event.get("channel") or "")
        message_ts = str(event.get("ts") or "")
        thread_ts = str(event.get("thread_ts") or message_ts)
        dedupe_key = event_id or f"{event.get('team')}:{channel_id}:{message_ts}"

        if self.deduper.seen_before(dedupe_key):
            return {"ignored": True, "reason": "duplicate"}
        if not self.rate_limiter.allow():
            return {"ignored": True, "reason": "rate_limited"}
        if not channel_id or not message_ts:
            return {"ignored": True, "reason": "missing_channel_or_ts"}

        text = normalize_slack_text(
            str(event.get("text") or ""), bot_user_id=self.bot_user_id
        )
        print(text)
        history = self.fetch_thread_history(channel_id, thread_ts)
        # attachments, file_refs = self.sync_event_files(history)
        headers = self.build_headers(event, thread_ts=thread_ts, files=[])
        context_text = slack_history_to_context(history)
        body = (
            text
            if not context_text
            else f"{text}\n\nSlack thread history:\n{context_text}"
        )

        task_data = {
            "title": text[:80] or "Slack mention",
            "description": plaintext_to_lexical(body),
            "headers": headers,
            # "attachments": attachments,
            "project": self.settings.default_project,
            "agent": self.settings.default_agent,
            "model": {
                "id": self.settings.default_model,
                "effort": self.settings.default_effort,
            }
            if self.settings.default_model
            else None,
            "adapter": self.settings.default_adapter,
            "adapter_options": {"engine": self.settings.default_adapter_engine}
            if self.settings.default_adapter_engine
            else None,
            "environment": self.settings.default_environment,
            # "labels": ["019e4eb9-6a52-7bbd-bcd6-fd9f7482263a"],
            "worktree": False,
        }
        saved = self.agentis_client.save_task(
            {key: value for key, value in task_data.items() if value is not None}
        )
        task_id = saved["form"]["id"]
        print(task_id)
        # run = self.agentis.start_run(task_id, start_adapter=True)
        self.slack_client.reactions_add(
            channel=channel_id,
            timestamp=message_ts,
            name="eyes",
        )
        final_message = self.run_agentiscode(task_id, body)

        reply = markdown_to_slack_mrkdwn(final_message)
        posted = self.slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=reply,
            mrkdwn=True,
        )
        _remove_slack_reaction(self.slack_client, channel_id, message_ts, "eyes")

        self.agentis_client.add_agent_comment(
            task=task_id, body=final_message, status=5
        )

        return {"replied": True, "message": posted}

    def run_agentiscode(self, task_id: str, prompt: str) -> str:
        last_text_message = ""
        proc = subprocess.Popen(
            self.build_agentiscode_args(task_id, prompt),
            cwd=self.settings.agentiscode_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None

        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            payload = self._parse_json_line(line)
            if payload and payload.get("type") == "text":
                last_text_message = str(payload.get("text") or "")

        proc.wait()
        if proc.returncode:
            raise RuntimeError(f"agentiscode exited with code {proc.returncode}")
        return last_text_message.strip()

    def build_agentiscode_args(self, task_id: str, prompt: str) -> list[str]:
        args = [
            *shlex.split(self.settings.agentiscode_command),
            "--adapter",
            self.settings.agentiscode_adapter,
            "--json",
            "--task-id",
            task_id,
        ]
        if self.settings.agentis_api_url:
            args.extend(["--agentis-api", self.settings.agentis_api_url])
        if self.settings.agentis_token:
            args.extend(["--agentis-token", self.settings.agentis_token])
        if self.settings.default_effort:
            args.extend(["--effort", self.settings.default_effort])
        args.append(prompt)
        return args

    @staticmethod
    def _parse_json_line(line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def fetch_thread_history(
        self, channel_id: str, thread_ts: str
    ) -> list[dict[str, Any]]:
        response = self.slack_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=200
        )
        messages = list(response.get("messages") or [])
        self._add_user_real_names(messages)
        return messages

    def _add_user_real_names(self, messages: list[dict[str, Any]]) -> None:
        user_ids = sorted(
            {str(message.get("user")) for message in messages if message.get("user")}
        )
        names: dict[str, str] = {}
        for user_id in user_ids:
            try:
                response = self.slack_client.users_info(user=user_id)
            except Exception:
                continue

            user = response.get("user") or {}
            profile = user.get("profile") or {}
            name = (
                profile.get("real_name")
                or profile.get("display_name")
                or user.get("real_name")
                or user.get("name")
            )
            if name:
                names[user_id] = str(name)

        for message in messages:
            user_id = str(message.get("user") or "")
            if user_id in names:
                message["user_real_name"] = names[user_id]

    def build_headers(
        self, event: dict[str, Any], *, thread_ts: str, files: list[dict]
    ) -> dict:
        return {
            "source": "slack",
            "slack": {
                "team_id": event.get("team") or event.get("team_id"),
                "channel_id": event.get("channel"),
                "thread_ts": thread_ts,
                "message_ts": event.get("ts"),
                "user_id": event.get("user"),
                "user_name": event.get("username"),
                "permalink": event.get("permalink"),
                "files": files,
            },
        }
