from __future__ import annotations

import pytest

from agentis_slack.agentis_client import AgentisClient


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"result": {"ok": True}}


def test_user_rpc_uses_user_token_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr("agentis_slack.agentis_client.requests.post", fake_post)

    client = AgentisClient(
        "https://agentis.test/api/rpc", "user-token", service_token="service-token"
    )
    client.save_task({"title": "hello"})

    headers = calls[0][1]["headers"]
    assert headers == {
        "X-Auth-Token": "user-token",
        "Authorization": "Bearer user-token",
    }


def test_service_rpc_uses_service_token_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr("agentis_slack.agentis_client.requests.post", fake_post)

    client = AgentisClient(
        "https://agentis.test/api/rpc", "user-token", service_token="service-token"
    )
    client.add_question([], external_id="ext-1")

    assert calls[0][1]["headers"] == {"X-Service-Token": "service-token"}


def test_service_rpc_requires_service_token() -> None:
    client = AgentisClient("https://agentis.test/api/rpc", "user-token")

    with pytest.raises(RuntimeError, match="AGENTIS_SERVICE_TOKEN"):
        client.get_question_result("ext-1")
