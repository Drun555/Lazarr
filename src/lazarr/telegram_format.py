"""Safe, composable Telegram MarkdownV2 fragments."""

import re


SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape(value):
    """Escape arbitrary text for Telegram's MarkdownV2 parser."""
    return SPECIAL.sub(r"\\\1", str(value))


def bold(value):
    return f"*{escape(value)}*"


def quote(value):
    """Render arbitrary, possibly multiline text as a Telegram blockquote."""
    return "\n".join(f"> {escape(line)}" for line in str(value).splitlines())
