"""Take the host's secret values out of text before it leaves the worker.

The channel remembers every secret the host has sent this worker, from any
request, in every escaped spelling. Text a plugin can influence (progress
messages, error messages, reasons, field names) is passed through here in full
before it is cut to size, so a secret that straddles a cut cannot leave a
prefix behind.
"""

from __future__ import annotations

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
