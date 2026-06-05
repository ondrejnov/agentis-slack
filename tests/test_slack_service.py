from agentis_slack.config import Settings
from agentis_slack.slack_service import SlackMentionService


def make_settings() -> Settings:
    return Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test")


class FakeSlackClient:
    def __init__(self):
        self.messages = []
        self.reactions = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "ts": "2.0"}

    def conversations_replies(self, **kwargs):
        return {"messages": []}

    def reactions_add(self, **kwargs):
        self.reactions.append(kwargs)
        return {"ok": True}


class FakeAgentisClient:
    def save_task(self, payload):
        self.saved_payload = payload
        return {"form": {"id": "task-1"}}


def test_handle_app_mention_replies_in_thread_with_formatted_text():
    slack = FakeSlackClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=object(),
        settings=make_settings(),
        bot_user_id="Ubot",
        reply_builder=lambda message, event: f"**Reply:** {message}",
    )

    result = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.0", "text": "<@Ubot> hello"},
        event_id="evt-1",
    )

    assert result["replied"] is True
    assert slack.messages == [
        {"channel": "C1", "thread_ts": "1.0", "text": "*Reply:* hello", "mrkdwn": True}
    ]


def test_handle_app_mention_ignores_duplicate_event():
    slack = FakeSlackClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=object(),
        settings=make_settings(),
        bot_user_id="Ubot",
    )
    event = {"team": "T1", "channel": "C1", "ts": "1.0", "text": "<@Ubot> hello"}

    service.handle_app_mention(event, event_id="evt-1")
    result = service.handle_app_mention(event, event_id="evt-1")

    assert result == {"ignored": True, "reason": "duplicate"}
    assert len(slack.messages) == 1


def test_handle_message_runs_agentiscode_with_saved_task_id(monkeypatch):
    slack = FakeSlackClient()
    agentis = FakeAgentisClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=agentis,
        settings=make_settings(),
        bot_user_id="Ubot",
    )
    calls = []

    def fake_run_agentiscode(task_id, prompt):
        calls.append((task_id, prompt))
        return "final answer"

    monkeypatch.setattr(service, "run_agentiscode", fake_run_agentiscode)

    result = service.handle_message(
        {"team": "T1", "channel": "C1", "ts": "1.0", "text": "hello"},
        event_id="evt-1",
    )

    assert result == {"ok": True, "final_message": "final answer"}
    assert calls == [("task-1", "hello")]
    assert slack.reactions == [{"channel": "C1", "timestamp": "1.0", "name": "eyes"}]


def test_build_agentiscode_args_passes_task_id_and_prompt():
    settings = Settings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        agentis_api_url="https://agentis.example/api",
        agentis_token="token-1",
        default_effort="high",
        agentiscode_command="/usr/local/bin/agentiscode",
        agentiscode_adapter="claude",
    )
    service = SlackMentionService(
        slack_client=FakeSlackClient(),
        agentis_client=object(),
        settings=settings,
    )

    assert service.build_agentiscode_args("task-1", "udelej X") == [
        "/usr/local/bin/agentiscode",
        "--adapter",
        "claude",
        "--json",
        "--task-id",
        "task-1",
        "--agentis-api",
        "https://agentis.example/api",
        "--agentis-token",
        "token-1",
        "--effort",
        "high",
        "udelej X",
    ]
