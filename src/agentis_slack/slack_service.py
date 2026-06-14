from __future__ import annotations

import json
from typing import Any

from agentis_slack.agentis_client import AgentisClient

from .config import Settings
from .guards import EventDeduper, GlobalRateLimiter, should_ignore_event
from .slack_blocks import (
    build_answered_blocks,
    build_question_modal,
    parse_modal_submission,
)
from .text import normalize_slack_text, slack_history_to_context


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
        return self._handle_event(event, event_id=event_id)

    def handle_message(
        self, event: dict[str, Any], event_id: str | None = None
    ) -> dict[str, Any]:
        return self._handle_event(event, event_id=event_id)

    def _handle_event(
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

        try:
            text = normalize_slack_text(
                str(event.get("text") or ""), bot_user_id=self.bot_user_id
            )
            history = self.fetch_thread_history(channel_id, thread_ts)
            headers = self.build_headers(event, thread_ts=thread_ts, files=[])
            context_text = slack_history_to_context(history)
            body = (
                text
                if not context_text
                else f"{text}\n\nSlack thread history:\n\n{context_text}"
            )

            task_data = {
                "title": text[:80] or "Slack mention",
                "description": plaintext_to_lexical(body),
                "headers": headers,
                "project": self.settings.default_project,
                "agent": self.settings.default_agent,
                "model": {
                    "id": self.settings.default_model,
                    "effort": self.settings.default_effort,
                }
                if self.settings.default_model
                else None,
                "adapter": self.settings.default_adapter,
                "adapter_options": [{"key": "workflow", "value": "slack"}],
                "environment": self.settings.default_environment,
                "worktree": False,
            }
            saved = self.agentis_client.save_task(
                {key: value for key, value in task_data.items() if value is not None}
            )
            task_id = saved["form"]["id"]
            self.agentis_client.start_run(task_id)
            self._add_slack_reaction(channel_id, message_ts, "eyes")
            return {"created": True, "task_id": task_id}

        except Exception as e:
            print(e)
            self._add_slack_reaction(channel_id, message_ts, "fail")

    def open_question_modal(self, body: dict[str, Any], client: Any) -> None:
        """Klik na „Odpovědět" → dotáhni dávku z backendu a otevři modal.

        ``external_id``/``task_id`` jsou ve ``value`` tlačítka; kanál a ts zprávy
        (pro pozdější update) bere modal z action payloadu přes private_metadata.
        """
        actions = body.get("actions") or []
        if not actions:
            return
        try:
            value = json.loads(actions[0].get("value") or "{}")
        except ValueError:
            return
        external_id = str(value.get("e") or "")
        task_id = value.get("t")
        if not external_id or not task_id:
            return

        group = self._find_question_group(task_id, external_id)
        if group is None:
            return

        channel = (body.get("channel") or {}).get("id") or ""
        message_ts = (body.get("message") or {}).get("ts") or ""
        view = build_question_modal(group, channel=channel, message_ts=message_ts)
        try:
            client.views_open(trigger_id=body.get("trigger_id"), view=view)
        except Exception as e:
            print(e)

    def submit_question_modal(self, ack: Any, view: dict[str, Any], client: Any) -> None:
        """Submit modalu → poskládej ``results`` a zapiš přes ``question_reply``."""
        external_id, channel, message_ts, results, errors = parse_modal_submission(view)
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        if not external_id:
            return
        try:
            self.agentis_client.question_reply(external_id, results)
        except Exception as e:
            print(e)
            return
        text, blocks = build_answered_blocks(view)
        self._mark_question_answered(channel, message_ts, client, text, blocks)

    def _find_question_group(self, task_id: str, external_id: str) -> dict[str, Any] | None:
        try:
            task = self.agentis_client.fetch_task(task_id)
        except Exception as e:
            print(e)
            return None
        for group in task.get("questions") or []:
            if str(group.get("external_id")) == external_id:
                return group
        return None

    def _mark_question_answered(
        self,
        channel: str,
        message_ts: str,
        client: Any,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        if not channel or not message_ts:
            return
        try:
            client.chat_update(
                channel=channel,
                ts=message_ts,
                text=text,
                blocks=blocks,
            )
        except Exception as e:
            print(e)

    def _add_slack_reaction(self, channel_id: str, message_ts: str, name: str) -> None:
        if not channel_id or not message_ts:
            return
        try:
            self.slack_client.reactions_add(
                channel=channel_id, timestamp=message_ts, name=name
            )
        except Exception:
            return

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
