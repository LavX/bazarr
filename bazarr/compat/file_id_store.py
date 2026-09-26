from __future__ import annotations
import threading
import time
from typing import Tuple


class FileIdStore:
    """In-memory TTL store mapping monotonic int ids to provider+native_id payloads.

    Exists because OS.com-compat clients (VLSub, Jellyfin, Kodi, Stremio) model
    `files[].file_id` as an integer. A signed HMAC token string breaks their
    deserializers. Instead, we mint a small monotonic int, stash the payload
    in-process with a TTL matching file_id_ttl_seconds, and resolve back on
    /download. Restart flushes the store; clients re-search, which matches
    how they use file_ids (never persisted across sessions).
    """

    def __init__(self, max_entries: int = 10000):
        self._lock = threading.Lock()
        self._counter = 0
        self._store: dict[int, tuple[float, dict]] = {}
        self._max = max_entries

    def put(self, payload: dict, ttl_seconds: int) -> int:
        with self._lock:
            self._counter += 1
            fid = self._counter
            self._store[fid] = (time.time() + ttl_seconds, dict(payload))
            if len(self._store) > self._max:
                self._gc_locked()
            return fid

    def renew(self, fid, ttl_seconds: int) -> float | None:
        """Restart a live entry's lifetime, keeping its id and payload.

        For a caller that handed an id out before it had finished assembling
        the answer that id belongs to. Minting a replacement would give the
        entry a full lifetime too, but it would retire an id the caller has
        already offered someone. Returns the new expiry, or None when the
        entry is gone. Never shortens: a renewal that lands behind the current
        expiry leaves it alone.
        """
        try:
            key = int(fid)
        except (TypeError, ValueError):
            return None
        now = time.time()
        with self._lock:
            row = self._store.get(key)
            if row is None:
                return None
            exp, payload = row
            if exp < now:
                self._store.pop(key, None)
                return None
            expires = max(exp, now + ttl_seconds)
            payload = {**payload, "expires_at": expires} if "expires_at" in payload else payload
            self._store[key] = (expires, payload)
            return expires

    def get(self, fid) -> Tuple[bool, dict]:
        try:
            key = int(fid)
        except (TypeError, ValueError):
            return False, {}
        with self._lock:
            row = self._store.get(key)
            if row is None:
                return False, {}
            exp, payload = row
            if exp < time.time():
                self._store.pop(key, None)
                return False, {}
            return True, dict(payload)

    def _gc_locked(self) -> None:
        now = time.time()
        for k in [k for k, (exp, _) in self._store.items() if exp < now]:
            self._store.pop(k, None)
        if len(self._store) > self._max:
            to_drop = len(self._store) - self._max
            for k in list(self._store.keys())[:to_drop]:
                self._store.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._counter = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


_singleton = FileIdStore()


def get_store() -> FileIdStore:
    return _singleton


def reset_store() -> None:
    _singleton.clear()
