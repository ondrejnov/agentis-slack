"""Read Markdown from stdin, convert it to Slack mrkdwn, write it to stdout.

Usage:
    echo "**hello**" | python -m agentis_slack.md2mrkdwn
    cat notes.md | python -m agentis_slack.md2mrkdwn
"""

import sys

from agentis_slack.text import markdown_to_slack_mrkdwn


def main() -> None:
    text = sys.stdin.read()
    sys.stdout.write(markdown_to_slack_mrkdwn(text))


if __name__ == "__main__":
    main()
