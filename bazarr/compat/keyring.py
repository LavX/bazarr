"""Named API-key store for the Distribution Hub.

The full token is never persisted - only its sha256 (key_hash) and an 8-char
prefix for UI identification. Resolution is a hash lookup with a short
in-memory cache. The legacy shared `compat_endpoint.token` is migrated in as
an is_legacy, Unlimited "Default" key so existing integrations keep working.

Intra-package imports drop the `bazarr.` prefix (see compat/routes.py header):
the rest of bazarr resolves modules from `bazarr/` as the sys.path root.
"""
from __future__ import annotations
import hashlib
import json
import secrets
import time
from datetime import datetime
from threading import Lock

from app.database import (database, select, insert, update as sa_update,
                          delete as sa_delete, TableCompatApiKeys)

_PREFIX = "bzr_"
_cache_lock = Lock()
_cache: dict[str, tuple[float, dict | None]] = {}   # key_hash -> (expiry, rec|None)
_CACHE_TTL = 30.0


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return (token, prefix, key_hash). Token is shown to the user once."""
    token = _PREFIX + secrets.token_urlsafe(32)
    return token, token[:8], hash_token(token)


_SCALAR_COLS = ("id", "name", "key_prefix", "key_hash", "tier", "enabled",
                "is_legacy", "timeout_seconds", "created_at", "last_used_at",
                "note")


def _safe_json(raw):
    """Tolerate a corrupt/hand-edited JSON column: fall back to None rather
    than letting one poisoned row break auth for that key."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _row_to_dict(row) -> dict:
    d = {c: getattr(row, c) for c in _SCALAR_COLS}
    d["custom_limits"] = _safe_json(row.custom_limits)
    d["excluded_providers"] = _safe_json(row.excluded_providers)
    d["allowed_providers"] = _safe_json(row.allowed_providers)
    return d


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def resolve(token: str) -> dict | None:
    """Resolve a presented Api-Key to its key record, or None.

    Disabled keys resolve to None (treated as unknown). Cached for a short
    TTL; invalidated on any CRUD mutation."""
    if not token:
        return None
    h = hash_token(token)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(h)
        if hit and hit[0] > now:
            return hit[1]
    row = database.execute(
        select(TableCompatApiKeys).where(TableCompatApiKeys.key_hash == h)
    ).scalar_one_or_none()
    rec = _row_to_dict(row) if (row is not None and int(row.enabled) == 1) else None
    with _cache_lock:
        _cache[h] = (now + _CACHE_TTL, rec)
    return rec


def get(key_id: int) -> dict | None:
    row = database.execute(
        select(TableCompatApiKeys).where(TableCompatApiKeys.id == int(key_id))
    ).scalar_one_or_none()
    return _row_to_dict(row) if row is not None else None


def create(name: str, tier: str = "free", *, custom_limits=None,
           excluded_providers=None, allowed_providers=None,
           timeout_seconds=None, note=None,
           is_legacy: int = 0) -> tuple[int, str]:
    """Create a key. Returns (id, full_token). The token is returned once."""
    token, prefix, h = generate_token()
    database.execute(insert(TableCompatApiKeys).values(
        name=name, key_prefix=prefix, key_hash=h, tier=tier,
        custom_limits=json.dumps(custom_limits) if custom_limits else None,
        excluded_providers=(json.dumps(excluded_providers)
                            if excluded_providers else None),
        allowed_providers=(json.dumps(allowed_providers)
                           if allowed_providers else None),
        timeout_seconds=timeout_seconds, enabled=1, is_legacy=is_legacy,
        created_at=datetime.now(), note=note))
    new_id = database.execute(
        select(TableCompatApiKeys.id).where(TableCompatApiKeys.key_hash == h)
    ).scalar_one()
    invalidate_cache()
    return int(new_id), token


def rotate(key_id: int) -> str:
    """Issue a fresh token for an existing key (id + usage preserved)."""
    token, prefix, h = generate_token()
    database.execute(sa_update(TableCompatApiKeys)
                     .where(TableCompatApiKeys.id == int(key_id))
                     .values(key_hash=h, key_prefix=prefix))
    invalidate_cache()
    return token


def update(key_id: int, **fields) -> None:
    vals: dict = {}
    for k in ("name", "tier", "enabled", "timeout_seconds", "note"):
        if k in fields:
            vals[k] = fields[k]
    if "custom_limits" in fields:
        cl = fields["custom_limits"]
        vals["custom_limits"] = json.dumps(cl) if cl else None
    if "excluded_providers" in fields:
        ep = fields["excluded_providers"]
        vals["excluded_providers"] = json.dumps(ep) if ep else None
    if "allowed_providers" in fields:
        ap = fields["allowed_providers"]
        vals["allowed_providers"] = json.dumps(ap) if ap else None
    if vals:
        database.execute(sa_update(TableCompatApiKeys)
                         .where(TableCompatApiKeys.id == int(key_id))
                         .values(**vals))
        invalidate_cache()


def delete(key_id: int) -> None:
    database.execute(sa_delete(TableCompatApiKeys)
                     .where(TableCompatApiKeys.id == int(key_id)))
    invalidate_cache()


def list_keys() -> list[dict]:
    rows = database.execute(
        select(TableCompatApiKeys).order_by(TableCompatApiKeys.id)).scalars().all()
    return [_row_to_dict(r) for r in rows]


def touch_last_used(key_id: int) -> None:
    # Deliberately does NOT invalidate_cache(): this runs on every metered
    # request, and busting the resolve cache here would defeat the hot-path
    # cache entirely (a DB read per request). last_used_at is display-only
    # (read from the DB by the management API, never from the resolve cache),
    # so a slightly stale cached value is harmless.
    database.execute(sa_update(TableCompatApiKeys)
                     .where(TableCompatApiKeys.id == int(key_id))
                     .values(last_used_at=datetime.now()))


def has_legacy_key() -> bool:
    """Whether an is_legacy row exists (any enabled state). Raises if the
    table is absent - the caller distinguishes 'absent' from 'not seeded'."""
    return database.execute(
        select(TableCompatApiKeys.id)
        .where(TableCompatApiKeys.is_legacy == 1)).first() is not None


def seed_legacy_key() -> None:
    """Migrate settings.compat_endpoint.token into an Unlimited Default key.

    Idempotent: there is at most one is_legacy row. If the shared token has
    been rotated since the last seed, re-point the legacy row at the new hash
    so existing integrations keep authorizing without manual intervention.

    Tolerates a missing table: on an upgrade where the compat blueprint is
    registered before the schema is created (create_all/migrate), this no-ops
    instead of crashing boot. The next request reseeds via the keyring once the
    table exists, and the legacy token keeps working via the auth fallback in
    the meantime."""
    from app.config import settings
    token = settings.compat_endpoint.token or ""
    if len(token) < 32:
        return
    h = hash_token(token)
    try:
        existing = database.execute(
            select(TableCompatApiKeys).where(TableCompatApiKeys.is_legacy == 1)
        ).scalar_one_or_none()
    except Exception:
        import logging
        logging.getLogger("bazarr.compat.keyring").info(
            "seed_legacy_key skipped: compat_api_keys table not ready yet")
        return
    if existing is not None:
        if existing.key_hash != h:
            database.execute(sa_update(TableCompatApiKeys)
                             .where(TableCompatApiKeys.id == existing.id)
                             .values(key_hash=h, key_prefix=token[:8]))
            invalidate_cache()
        return
    # The check-then-insert above is not atomic under AUTOCOMMIT. If two callers
    # race (e.g. boot seed + a concurrent /regenerate), the loser's insert hits
    # the unique key_hash index. Swallow it and treat as already-seeded so the
    # race can't fail boot or leave duplicate legacy rows.
    from sqlalchemy.exc import IntegrityError
    try:
        database.execute(insert(TableCompatApiKeys).values(
            name="Default (legacy token)", key_prefix=token[:8], key_hash=h,
            tier="unlimited", enabled=1, is_legacy=1, created_at=datetime.now(),
            note="Auto-migrated shared Api-Key. Existing integrations use this key."))
    except IntegrityError:
        pass
    invalidate_cache()
