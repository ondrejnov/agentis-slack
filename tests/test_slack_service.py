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


def test_handle_app_mention_creates_task_and_acknowledges():
    slack = FakeSlackClient()
    agentis = FakeAgentisClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=agentis,
        settings=make_settings(),
        bot_user_id="Ubot",
    )

    result = service.handle_app_mention(
        {"team": "T1", "channel": "C1", "ts": "1.0", "text": "<@Ubot> hello"},
        event_id="evt-1",
    )

    assert result == {"created": True, "task_id": "task-1"}
    assert agentis.saved_payload["title"] == "hello"
    assert slack.reactions == [{"channel": "C1", "timestamp": "1.0", "name": "eyes"}]
    # The service only creates a task; it must not reply in the thread.
    assert slack.messages == []


def test_handle_app_mention_ignores_duplicate_event():
    slack = FakeSlackClient()
    agentis = FakeAgentisClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=agentis,
        settings=make_settings(),
        bot_user_id="Ubot",
    )
    event = {"team": "T1", "channel": "C1", "ts": "1.0", "text": "<@Ubot> hello"}

    service.handle_app_mention(event, event_id="evt-1")
    result = service.handle_app_mention(event, event_id="evt-1")

    assert result == {"ignored": True, "reason": "duplicate"}
    assert len(slack.reactions) == 1


def test_handle_message_creates_task_with_saved_task_id():
    slack = FakeSlackClient()
    agentis = FakeAgentisClient()
    service = SlackMentionService(
        slack_client=slack,
        agentis_client=agentis,
        settings=make_settings(),
        bot_user_id="Ubot",
    )

    result = service.handle_message(
        {"team": "T1", "channel": "C1", "ts": "1.0", "text": "hello"},
        event_id="evt-1",
    )

    assert result == {"created": True, "task_id": "task-1"}
    assert slack.reactions == [{"channel": "C1", "timestamp": "1.0", "name": "eyes"}]
    assert slack.messages == []
