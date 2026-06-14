from agentis_slack.config import Settings
from agentis_slack.slack_service import SlackMentionService


def make_settings() -> Settings:
    return Settings(slack_bot_token="xoxb-test", slack_app_token="xapp-test")


class FakeBoltClient:
    def __init__(self):
        self.opened = []
        self.updates = []

    def views_open(self, **kwargs):
        self.opened.append(kwargs)
        return {"ok": True}

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


class FakeAgentisClient:
    def __init__(self, group):
        self._group = group
        self.replies = []

    def fetch_task(self, task_id):
        self.task_id = task_id
        return {"questions": [self._group]}

    def question_reply(self, external_id, results):
        self.replies.append((external_id, results))
        return {"ok": True}


def _group():
    return {
        "external_id": "ext-1",
        "questions": [
            {
                "id": "q-1",
                "header": "Env",
                "question": "Kam?",
                "multiple": False,
                "allowFreeformInput": False,
                "options": [
                    {"id": "o-prod", "label": "prod", "description": "produkce", "selected": False}
                ],
            }
        ],
    }


def _service(agentis):
    return SlackMentionService(
        slack_client=FakeBoltClient(),
        agentis_client=agentis,
        settings=make_settings(),
        bot_user_id="Ubot",
    )


def test_open_modal_fetches_group_and_opens_view():
    agentis = FakeAgentisClient(_group())
    service = _service(agentis)
    client = FakeBoltClient()
    body = {
        "actions": [{"value": '{"e": "ext-1", "t": "task-1"}'}],
        "channel": {"id": "C1"},
        "message": {"ts": "111.0"},
        "trigger_id": "trig-1",
    }
    service.open_question_modal(body, client)

    assert agentis.task_id == "task-1"
    assert len(client.opened) == 1
    view = client.opened[0]["view"]
    assert view["private_metadata"] == '{"e": "ext-1", "c": "C1", "ts": "111.0"}'


def test_submit_modal_replies_and_marks_answered():
    agentis = FakeAgentisClient(_group())
    service = _service(agentis)
    client = FakeBoltClient()
    acks = []
    view = {
        "private_metadata": '{"e": "ext-1", "c": "C1", "ts": "111.0"}',
        "state": {"values": {"q-1": {"opt": {"selected_option": {"value": "o-prod"}}}}},
    }
    service.submit_question_modal(lambda **kw: acks.append(kw), view, client)

    assert acks == [{}]  # prázdný ack() = zavřít modal, žádné errors
    assert agentis.replies == [
        ("ext-1", [{"question_id": "q-1", "selected_options": ["o-prod"], "answer_text": None}])
    ]
    assert len(client.updates) == 1
    assert client.updates[0]["ts"] == "111.0"


def test_submit_modal_returns_errors_without_reply():
    agentis = FakeAgentisClient(
        {
            "external_id": "ext-1",
            "questions": [
                {
                    "id": "q-1",
                    "header": "X",
                    "question": "?",
                    "multiple": False,
                    "allowFreeformInput": True,
                    "options": [
                        {"id": "o-1", "label": "a", "description": "a", "selected": False}
                    ],
                }
            ],
        }
    )
    service = _service(agentis)
    acks = []
    view = {
        "private_metadata": '{"e": "ext-1", "c": "C1", "ts": "111.0"}',
        "state": {
            "values": {
                "q-1": {"opt": {"selected_option": None}},
                "q-1::free": {"free": {"value": ""}},
            }
        },
    }
    service.submit_question_modal(lambda **kw: acks.append(kw), view, FakeBoltClient())

    assert acks[0]["response_action"] == "errors"
    assert "q-1" in acks[0]["errors"]
    assert agentis.replies == []
