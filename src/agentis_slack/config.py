from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = _unquote_env_value(value.strip())


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    rate_limit_window_seconds: int = 60
    rate_limit_max_events: int = 30
    agentis_api_url: str = ""
    agentis_token: str = ""
    default_project: str = ""
    default_agent: str = ""
    default_model: str = ""
    default_effort: str = ""
    default_adapter: str = ""
    default_adapter_engine: str = ""
    default_environment: str = ""
    agentiscode_command: str = "poetry run agentiscode"
    agentiscode_dir: str = "/var/www/agentis-adapter"
    agentiscode_adapter: str = "opencode"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.getenv("SLACK_APP_TOKEN", ""),
            rate_limit_window_seconds=int(
                os.getenv("SLACK_RATE_LIMIT_WINDOW_SECONDS", "60")
            ),
            rate_limit_max_events=int(os.getenv("SLACK_RATE_LIMIT_MAX_EVENTS", "30")),
            agentis_api_url=os.getenv("AGENTIS_API_URL", ""),
            agentis_token=os.getenv("AGENTIS_TOKEN", ""),
            default_project=os.getenv("AGENTIS_DEFAULT_PROJECT", ""),
            default_agent=os.getenv("AGENTIS_DEFAULT_AGENT", ""),
            default_model=os.getenv("AGENTIS_DEFAULT_MODEL", ""),
            default_effort=os.getenv("AGENTIS_DEFAULT_EFFORT", ""),
            default_adapter=os.getenv("AGENTIS_DEFAULT_ADAPTER", ""),
            default_adapter_engine=os.getenv("AGENTIS_DEFAULT_ADAPTER_ENGINE", ""),
            default_environment=os.getenv("AGENTIS_DEFAULT_ENVIRONMENT", ""),
            agentiscode_command=os.getenv(
                "AGENTISCODE_COMMAND", "poetry run agentiscode"
            ),
            agentiscode_dir=os.getenv("AGENTISCODE_DIR", "/var/www/agentis-adapter"),
            agentiscode_adapter=os.getenv(
                "AGENTISCODE_ADAPTER",
                os.getenv("AGENTIS_DEFAULT_ADAPTER_ENGINE", "opencode"),
            ),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "SLACK_BOT_TOKEN": self.slack_bot_token,
                "SLACK_APP_TOKEN": self.slack_app_token,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
