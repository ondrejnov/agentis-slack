from __future__ import annotations

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .agentis_client import AgentisClient
from .config import Settings
from .slack_blocks import QUESTION_OPEN_ACTION, QUESTION_SUBMIT_CALLBACK
from .slack_service import SlackMentionService


def create_app(settings: Settings) -> App:
    app = App(token=settings.slack_bot_token)
    auth = app.client.auth_test()
    service = SlackMentionService(
        slack_client=app.client,
        agentis_client=AgentisClient(settings.agentis_api_url, settings.agentis_token),
        settings=settings,
        bot_user_id=auth.get("user_id"),
        rate_limit_max_events=settings.rate_limit_max_events,
        rate_limit_window_seconds=settings.rate_limit_window_seconds,
    )

    @app.event("app_mention")
    def on_app_mention(event, body, ack):
        print(event)
        ack()
        service.handle_app_mention(event, event_id=body.get("event_id"))

    @app.event("message")
    def on_message(event, body, ack):
        print(event)
        ack()
        service.handle_message(event, event_id=body.get("event_id"))

    @app.action(QUESTION_OPEN_ACTION)
    def on_question_open(ack, body, client):
        ack()
        service.open_question_modal(body, client)

    @app.view(QUESTION_SUBMIT_CALLBACK)
    def on_question_submit(ack, body, view, client):
        service.submit_question_modal(ack, view, client)

    return app


def main() -> None:
    settings = Settings.from_env()
    settings.validate()
    SocketModeHandler(create_app(settings), settings.slack_app_token).start()


if __name__ == "__main__":
    main()
