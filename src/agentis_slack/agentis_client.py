from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import requests


class AgentisRpcError(RuntimeError):
    pass


class AgentisClient:
    def __init__(
        self,
        api_url: str,
        api_token: str,
        *,
        service_token: str = "",
        timeout: float = 60.0,
    ) -> None:
        self.api_url = api_url
        self.api_token = api_token
        self.service_token = service_token
        self.timeout = timeout
        self._ids = itertools.count(1)

    def _headers(self, *, use_service_token: bool) -> dict[str, str]:
        if use_service_token:
            if not self.service_token:
                raise RuntimeError("Missing AGENTIS_SERVICE_TOKEN for service RPC")
            return {"X-Service-Token": self.service_token}
        return {
            "X-Auth-Token": self.api_token,
            "Authorization": f"Bearer {self.api_token}",
        }

    def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        use_service_token: bool = False,
    ) -> Any:
        response = requests.post(
            self.api_url,
            json={
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": params or {},
            },
            headers=self._headers(use_service_token=use_service_token),
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise AgentisRpcError(body["error"].get("message") or "Agentis RPC error")
        return body.get("result")

    def find_by_external_ref(self, filters: dict[str, str]) -> dict | None:
        return self.rpc("task.find_by_external_ref", {"filters": filters}).get("item")

    def save_task(self, data: dict[str, Any]) -> dict:
        return self.rpc("task.save", {"data": data})

    def add_comment(self, **params: Any) -> dict:
        return self.rpc("task.add_comment", params)

    def start_run(self, task_id: str, *, start_adapter: bool = True) -> dict:
        return self.rpc(
            "task.start_run", {"id": task_id, "start_adapter": start_adapter}
        )

    def add_agent_comment(self, **params: Any) -> dict:
        return self.rpc("task.add_agent_comment", params, use_service_token=True)

    def store_session_id(self, run_id: str, session_id: str) -> dict:
        return self.rpc(
            "run.store_session_id", {"run_id": run_id, "session_id": session_id}
        )

    def store_activity_log(self, session_id: str, messages: list[Any]) -> dict:
        return self.rpc(
            "session.store_activity_log",
            {"session_id": session_id, "messages": messages},
            use_service_token=True,
        )

    def adapter_event(
        self,
        run_id: str,
        *,
        kind: str,
        status: str,
        message: str | None = None,
        data: dict | None = None,
    ) -> dict:
        return self.rpc(
            "run.adapter_event",
            {
                "run_id": run_id,
                "kind": kind,
                "status": status,
                "message": message,
                "data": data or {},
            },
        )

    def add_question(
        self,
        questions: list[dict],
        *,
        external_id: str,
        session_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"id": external_id, "questions": questions}
        if session_id:
            params["session_id"] = session_id
        return self.rpc("task.add_question", params, use_service_token=True)

    def get_question_result(self, external_id: str) -> dict:
        return self.rpc(
            "task.get_question_result",
            {"external_id": external_id},
            use_service_token=True,
        )

    def fetch_task(self, task_id: str) -> dict:
        return self.rpc("task.fetch", {"id": task_id})

    def question_reply(self, external_id: str, results: list[dict]) -> dict:
        return self.rpc(
            "task.question_reply", {"external_id": external_id, "results": results}
        )

    def upload_file(self, path: Path, *, label: str | None = None) -> dict:
        with path.open("rb") as file_obj:
            response = requests.post(
                self.api_url.rsplit("/api", 1)[0] + "/api/file/upload",
                files={"file": (label or path.name, file_obj)},
                headers={
                    "X-Auth-Token": self.api_token,
                },
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()
