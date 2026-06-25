# coding=utf-8
"""Repository for arr_instances rows (multiple Sonarr/Radarr instances, #156).

This repository is the single boundary where per-instance Sonarr/Radarr API
keys are encrypted at rest. arr_instances rows live outside config.yaml's
Fernet-encrypted settings, so the key is encrypted here on write via
``secret_store.encrypt_secret`` and decrypted on read via ``decrypt_secret``.
API-facing callers use :func:`to_safe_dict`, which never carries the key.
"""
import re
from datetime import datetime

from sqlalchemy import select

from app.database import TableArrInstances

from .subtitle_settings import read_subtitle_settings

VALID_KINDS = ("sonarr", "radarr")
_DEFAULT_PORTS = {"sonarr": 8989, "radarr": 7878}
_UNSET = object()


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "instance"


class ArrInstanceRepository:
    """CRUD + encryption boundary for arr_instances.

    Takes an explicit SQLAlchemy session so it is trivially testable against
    an in-memory database and never reaches for a global handle.
    """

    def __init__(self, session):
        self._session = session

    # ------------------------------------------------------------------ reads
    def get(self, instance_id):
        return self._session.get(TableArrInstances, instance_id)

    def get_by_key(self, kind, stable_key):
        return self._session.execute(
            select(TableArrInstances).where(
                TableArrInstances.kind == kind,
                TableArrInstances.stable_key == stable_key,
            )
        ).scalar_one_or_none()

    def list(self, kind=None, enabled_only=False):
        stmt = select(TableArrInstances)
        if kind is not None:
            stmt = stmt.where(TableArrInstances.kind == kind)
        if enabled_only:
            stmt = stmt.where(TableArrInstances.enabled == 1)
        stmt = stmt.order_by(TableArrInstances.kind, TableArrInstances.id)
        return list(self._session.execute(stmt).scalars().all())

    def get_default(self, kind):
        return self._session.execute(
            select(TableArrInstances).where(
                TableArrInstances.kind == kind,
                TableArrInstances.is_default == 1,
                TableArrInstances.enabled == 1,
            )
        ).scalar_one_or_none()

    def get_decrypted_api_key(self, instance_id):
        """Return the plaintext API key for runtime use, or None if no row or the
        stored key cannot be decrypted (a rotated/changed master key)."""
        from secret_store import decrypt_secret

        row = self.get(instance_id)
        if row is None:
            return None
        try:
            return decrypt_secret(row.api_key or "")
        except ValueError:
            import logging
            logging.error(
                "Cannot decrypt API key for %s instance id=%s (master key changed?); "
                "re-enter the key in Settings.", row.kind, instance_id)
            return None

    # ----------------------------------------------------------------- writes
    def create(self, kind, name, *, api_key="", ip="127.0.0.1", port=None,
               base_url="/", ssl=False, verify_ssl=False, http_timeout=60,
               enabled=True, is_default=None, stable_key=None,
               options=None, path_mappings=None, schedule=None):
        from secret_store import encrypt_secret

        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {kind!r}")
        if port is None:
            port = _DEFAULT_PORTS[kind]
        if stable_key is None:
            stable_key = self._unique_stable_key(kind, _slugify(name))

        enabled_i = 1 if enabled else 0

        # The first enabled instance of a kind becomes its default; an explicit
        # is_default also wins and demotes the previous default. A default must
        # be enabled (mirrors the DB-level check).
        existing_default = self.get_default(kind)
        if is_default is None:
            is_default = existing_default is None and enabled_i == 1
        is_default_i = 1 if is_default else 0
        if is_default_i and not enabled_i:
            raise ValueError("a default instance must be enabled")

        encrypted_key = encrypt_secret(api_key or "")

        # The demote + insert is one SAVEPOINT so a failed insert (e.g. a
        # stable_key conflict) cannot strand the kind with a demoted-but-not-
        # replaced default. The engine runs in AUTOCOMMIT, so session.rollback()
        # cannot undo the already-committed demote - but begin_nested() issues a
        # real SAVEPOINT whose rollback does.
        with self._session.begin_nested():
            if is_default_i and existing_default is not None:
                existing_default.is_default = 0
                self._session.flush()

            row = TableArrInstances(
                kind=kind,
                stable_key=stable_key,
                name=name,
                enabled=enabled_i,
                is_default=is_default_i,
                ip=ip,
                port=port,
                base_url=base_url,
                ssl=1 if ssl else 0,
                verify_ssl=1 if verify_ssl else 0,
                http_timeout=http_timeout,
                api_key=encrypted_key,
                options=options,
                path_mappings=path_mappings,
                schedule=schedule,
            )
            self._session.add(row)
            self._session.flush()
        return row

    def update(self, instance_id, *, name=_UNSET, enabled=_UNSET,
               is_default=_UNSET, ip=_UNSET, port=_UNSET, base_url=_UNSET,
               ssl=_UNSET, verify_ssl=_UNSET, http_timeout=_UNSET,
               api_key=_UNSET, clear_api_key=False, options=_UNSET,
               path_mappings=_UNSET, schedule=_UNSET):
        """Update mutable fields. ``kind`` and ``stable_key`` are immutable.

        API-key policy: ``clear_api_key=True`` wipes the key; otherwise a
        non-empty ``api_key`` replaces it (encrypted); an omitted or empty
        ``api_key`` preserves the existing one (so a UI that never received
        the key cannot accidentally erase it).
        """
        row = self.get(instance_id)
        if row is None:
            return None

        if name is not _UNSET:
            row.name = name
        if ip is not _UNSET:
            row.ip = ip
        if port is not _UNSET:
            row.port = port
        if base_url is not _UNSET:
            row.base_url = base_url
        if ssl is not _UNSET:
            row.ssl = 1 if ssl else 0
        if verify_ssl is not _UNSET:
            row.verify_ssl = 1 if verify_ssl else 0
        if http_timeout is not _UNSET:
            row.http_timeout = http_timeout
        if options is not _UNSET:
            row.options = options
        if path_mappings is not _UNSET:
            row.path_mappings = path_mappings
        if schedule is not _UNSET:
            row.schedule = schedule

        # api-key policy
        if clear_api_key:
            row.api_key = ""
        elif api_key is not _UNSET and api_key:
            from secret_store import encrypt_secret
            row.api_key = encrypt_secret(api_key)

        # Persist the plain field updates first (durable under AUTOCOMMIT); the
        # multi-step default election below runs in its own SAVEPOINT.
        self._session.flush()

        # enabled/default interaction (mirrors DB check: is_default=0 OR enabled=1).
        # One SAVEPOINT so a conflict mid-election can't strand the kind with zero
        # defaults (session.rollback() is a no-op under the AUTOCOMMIT engine).
        if enabled is not _UNSET or is_default is not _UNSET:
            with self._session.begin_nested():
                if enabled is not _UNSET:
                    row.enabled = 1 if enabled else 0
                    if not row.enabled:
                        row.is_default = 0
                if is_default is not _UNSET:
                    if is_default:
                        self._promote_default(row)
                    else:
                        row.is_default = 0
                self._session.flush()
                self._reconcile_default(row.kind, demoted_id=instance_id)

        row.updated_at = datetime.now()
        self._session.flush()
        return row

    def _reconcile_default(self, kind, demoted_id=None):
        """Keep a kind with any enabled instance owning exactly one default.

        No-op when an enabled default already exists. Otherwise it promotes an
        enabled instance, preferring one other than the row just demoted so a
        deliberate "unset default" hands off to a sibling instead of bouncing
        back. If the kind has no enabled instance, it is left with no default.
        """
        if self.get_default(kind) is not None:
            return
        candidates = self._session.execute(
            select(TableArrInstances)
            .where(TableArrInstances.kind == kind, TableArrInstances.enabled == 1)
            .order_by(TableArrInstances.id)
        ).scalars().all()
        if not candidates:
            return
        chosen = next((c for c in candidates if c.id != demoted_id), None) or candidates[0]
        chosen.is_default = 1
        self._session.flush()

    def _promote_default(self, row):
        """Make ``row`` its kind's default, demoting the previous. The caller owns
        the transaction/SAVEPOINT (demote-then-promote is not atomic on its own)."""
        current = self.get_default(row.kind)
        if current is not None and current.id != row.id:
            current.is_default = 0
            self._session.flush()
        row.is_default = 1
        self._session.flush()

    def set_default(self, instance_id):
        """Promote one instance to its kind's default, demoting the previous."""
        row = self.get(instance_id)
        if row is None:
            return None
        if not row.enabled:
            raise ValueError("cannot make a disabled instance the default")
        # SAVEPOINT: the demote then promote is two statements; a failure between
        # them must not leave the kind with no default (rollback is a no-op under
        # the AUTOCOMMIT engine, but a SAVEPOINT rolls back).
        with self._session.begin_nested():
            self._promote_default(row)
        return row

    def delete(self, instance_id):
        """Delete an instance. Refuse while it still owns any rows."""
        row = self.get(instance_id)
        if row is None:
            return False
        if self._has_owned_rows(instance_id):
            raise ValueError("cannot delete an instance that still owns rows")
        kind = row.kind
        # SAVEPOINT: delete + re-elect a default is multi-step; keep it atomic so
        # a mid-step failure can't leave the kind defaultless under AUTOCOMMIT.
        with self._session.begin_nested():
            self._session.delete(row)
            self._session.flush()
            self._reconcile_default(kind, demoted_id=instance_id)
        return True

    def _has_owned_rows(self, instance_id):
        from app.database import (
            TableBlacklist, TableBlacklistMovie, TableEpisodes, TableHistory,
            TableHistoryMovie, TableMovies, TableMoviesRootfolder, TableShows,
            TableShowsRootfolder,
        )
        owned = (
            TableShows, TableEpisodes, TableMovies, TableHistory,
            TableHistoryMovie, TableBlacklist, TableBlacklistMovie,
            TableShowsRootfolder, TableMoviesRootfolder,
        )
        for model in owned:
            hit = self._session.execute(
                select(model.arr_instance_id)
                .where(model.arr_instance_id == instance_id)
                .limit(1)
            ).first()
            if hit is not None:
                return True
        return False

    def _unique_stable_key(self, kind, base):
        candidate = base
        n = 2
        while self.get_by_key(kind, candidate) is not None:
            candidate = f"{base}-{n}"
            n += 1
        return candidate


def to_safe_dict(row):
    """API-safe view of an arr_instances row: never carries the API key.

    ``api_key_set`` tells the UI whether a key exists so it can show a masked
    placeholder without ever receiving the secret.
    """
    return {
        "id": row.id,
        "kind": row.kind,
        "stable_key": row.stable_key,
        "name": row.name,
        "display_name": row.name,  # name (stable_key) disambiguation added later
        "enabled": bool(row.enabled),
        "is_default": bool(row.is_default),
        "ip": row.ip,
        "port": row.port,
        "base_url": row.base_url,
        "ssl": bool(row.ssl),
        "verify_ssl": bool(row.verify_ssl),
        "http_timeout": row.http_timeout,
        "api_key_set": bool(row.api_key),
        "subtitle_settings": read_subtitle_settings(row.options),
    }
