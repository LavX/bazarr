"""Take the host's secret values out of text before it leaves the worker.

The channel remembers every secret the host has sent this worker, from any
request, in every escaped spelling. Text a plugin can influence (progress
messages, error messages, reasons, field names) is passed through here in full
before it is cut to size, so a secret that straddles a cut cannot leave a
prefix behind.
"""

from __future__ import annotations

import logging
import traceback

from typing import Any


def held_secrets(channel: Any) -> list[str]:
    """The secrets a channel has been sent, longest first. A stand-in with none gives ``[]``."""
    values = getattr(channel, "_secret_values", None)
    if not values:
        return []
    lock = getattr(channel, "_lock", None)
    if lock is None:
        return sorted(values, key=len, reverse=True)
    with lock:
        return sorted(values, key=len, reverse=True)


def channel_secrets(operation: Any) -> list[str]:
    """The secrets of the channel an operation belongs to."""
    return held_secrets(getattr(operation, "_channel", None))


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def scrubbed_traceback(error: BaseException, secrets: list[str]) -> str:
    """The traceback text of ``error`` with the secrets taken out."""
    return scrub("".join(traceback.format_exception(type(error), error, error.__traceback__)), secrets)


class SecretFilter(logging.Filter):
    """A log filter that takes the secrets of one channel out of every record it passes.

    Install it on the handler, so records from every logger (the runner's, asyncio's, a
    plugin's) are cleaned: the message and the exception text, after formatting and
    before anything is cut or written.
    """

    def __init__(self) -> None:
        super().__init__()
        self.channel: Any = None

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = held_secrets(self.channel)
        if not secrets:
            return True
        record.msg = scrub(record.getMessage(), secrets)
        record.args = None
        if record.exc_info:
            record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)), secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info, secrets)
        return True
