# coding=utf-8
"""Repository for arr_instances rows (multiple Sonarr/Radarr instances, #156).

This repository is the single boundary where per-instance Sonarr/Radarr API
keys are encrypted at rest. arr_instances rows live outside config.yaml's
Fernet-encrypted settings, so the key is encrypted here on write via
``secret_store.encrypt_secret`` and decrypted on read via ``decrypt_secret``.
API-facing callers use :func:`to_safe_dict`, which never carries the key.
"""
import re

from sqlalchemy import select

from app.database import TableArrInstances

VALID_KINDS = ("sonarr", "radarr")
_DEFAULT_PORTS = {"sonarr": 8989, "radarr": 7878}


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
        """Return the plaintext API key for runtime use, or None if no row."""
        from secret_store import decrypt_secret

        row = self.get(instance_id)
        if row is None:
            return None
        return decrypt_secret(row.api_key or "")

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
            api_key=encrypt_secret(api_key or ""),
            options=options,
            path_mappings=path_mappings,
            schedule=schedule,
        )
        self._session.add(row)
        self._session.flush()
        return row

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
    }
