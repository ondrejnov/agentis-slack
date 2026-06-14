from slack_bolt import App

from agentis_slack.config import Settings


def post_message(text: str, channel_id: str, thread_ts: str):
    settings = Settings.from_env()
    app = App(token=settings.slack_bot_token)
    posted = app.client.chat_postMessage(
        channel=channel_id, thread_ts=thread_ts, markdown_text=text
    )


# post_message(text, "D0B4UKMR30X", "1781384751.957369")
