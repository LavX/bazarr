"""Opaque UI result handles backed by the existing bounded subtitle store."""
from __future__ import annotations

import hmac
import re
import secrets
import time

from compat.file_id_store import get_store


def mint_result(subtitle, context: dict, search_id: str, ttl: int) -> tuple[str, float]:
    nonce = secrets.token_urlsafe(24)
    expires_at = time.time() + ttl
    file_id = get_store().put({
        "discover_nonce": nonce, "subtitle": subtitle,
        "context": dict(context), "search_id": search_id, "expires_at": expires_at,
    }, ttl_seconds=ttl)
    return f"d1.{file_id}.{nonce}", expires_at


def resolve_result(result_id: str, search_id: str | None = None) -> dict | None:
    if not isinstance(result_id, str) or not re.fullmatch(r"d1\.[1-9][0-9]{0,19}\.[A-Za-z0-9_-]{32}", result_id):
        return None
    parts = result_id.split(".")
    if len(parts) != 3 or parts[0] != "d1":
        return None
    found, record = get_store().get(parts[1])
    if not found or not hmac.compare_digest(record.get("discover_nonce", ""), parts[2]):
        return None
    if search_id is not None and record.get("search_id") != search_id:
        return None
    return record
