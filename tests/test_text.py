from agentis_slack.text import markdown_to_slack_mrkdwn, normalize_slack_text


def test_normalize_slack_text_removes_bot_mention_and_keeps_links_readable():
    text = normalize_slack_text("<@Ubot> check <https://example.com|Example> and <#C1|general>", bot_user_id="Ubot")

    assert text == "check Example (https://example.com) and general (#C1)"


def test_markdown_to_slack_mrkdwn_converts_common_formatting():
    text = "# Title\nUse **bold**, __also bold__, *italic*, ~~gone~~, and [docs](https://example.com?a=1&b=2)."

    result = markdown_to_slack_mrkdwn(text)

    assert result == "*Title*\nUse *bold*, *also bold*, _italic_, ~gone~, and <https://example.com?a=1&amp;b=2|docs>."


def test_markdown_to_slack_mrkdwn_converts_tables_to_code_blocks():
    text = "| Server | IP |\n|---|---|\n| `web-01` | 192.168.1.1 |"

    result = markdown_to_slack_mrkdwn(text)

    assert result == "```\nServer   | IP\n---------|------------\n`web-01` | 192.168.1.1\n```"
