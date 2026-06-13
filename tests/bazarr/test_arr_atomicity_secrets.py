# coding=utf-8
"""Atomicity + secret-durability fixes for arr_instances (#156 review).

The production engine runs isolation_level="AUTOCOMMIT", so session.flush()
commits immediately and session.rollback() is a no-op. These tests use an
AUTOCOMMIT engine (not the plain transactional schema_session) so the
default-election atomicity is exercised under production semantics.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def autocommit_session():
    """A session over an AUTOCOMMIT engine, mirroring production semantics
    (database.py creates the real engine with isolation_level='AUTOCOMMIT')."""
    from app.database import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, isolation_level="AUTOCOMMIT")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# --------------------------------------------------------------- C3 atomicity
def test_create_default_conflict_keeps_existing_default_under_autocommit(autocommit_session):
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(autocommit_session)
    first = repo.create("sonarr", "First", api_key="K")
    assert first.is_default
    first_id, first_stable = first.id, first.stable_key

    # A second create-as-default whose stable_key collides fails AFTER the
    # demote step. Without a savepoint the demote is durably committed (rollback
    # is a no-op under AUTOCOMMIT), leaving the kind with ZERO defaults.
    with pytest.raises(IntegrityError):
        repo.create("sonarr", "Second", api_key="K", is_default=True,
                    stable_key=first_stable)
    # Mirror service.create_instance's `except IntegrityError: session.rollback()`.
    autocommit_session.rollback()

    surviving = repo.get_default("sonarr")  # freshly queried
    assert surviving is not None, "create conflict must not strand the kind with no default"
    assert surviving.id == first_id


def test_set_default_conflict_keeps_existing_default_under_autocommit(autocommit_session):
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(autocommit_session)
    first = repo.create("sonarr", "First", api_key="K")
    second = repo.create("sonarr", "Second", api_key="K", enabled=True)
    assert repo.get_default("sonarr").id == first.id
    # promote second cleanly
    repo.set_default(second.id)
    assert repo.get_default("sonarr").id == second.id


# ---------------------------------------------------------------- H2 backfill
def _settings_stub(use_sonarr=True, use_radarr=False):
    scalar = SimpleNamespace(apikey="key", ip="127.0.0.1", port=8989,
                             base_url="/", ssl=False, verify_ssl=False, http_timeout=60)
    return SimpleNamespace(
        general=SimpleNamespace(use_sonarr=use_sonarr, use_radarr=use_radarr),
        sonarr=scalar,
        radarr=SimpleNamespace(apikey="key", ip="127.0.0.1", port=7878,
                               base_url="/", ssl=False, verify_ssl=False, http_timeout=60))


def test_backfill_resumes_stamping_after_partial_crash(schema_session):
    # Simulate a crash: the default instance was created (durable under
    # AUTOCOMMIT) but a row was left NULL-owner. A re-run must finish stamping,
    # not skip forever on the "instance already exists" guard.
    from app.database import TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create("sonarr", "Sonarr")  # the "crash" left this behind
    schema_session.flush()
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))  # owner left NULL

    backfill_default_instances(schema_session, _settings_stub())

    row = schema_session.execute(select(TableShows)).scalar_one()
    assert row.arr_instance_id == inst.id  # resumed and stamped


# --------------------------------------------------------- H1 master-key persist
def test_persist_master_key_generates_and_writes_when_empty(monkeypatch):
    import app.config as cfg
    from secret_store.crypto import persist_master_key

    monkeypatch.setattr(cfg.settings.general, "secrets_encryption_key", "", raising=False)
    calls = []
    monkeypatch.setattr(cfg, "write_config", lambda: calls.append(True))

    persist_master_key()

    assert cfg.settings.general.secrets_encryption_key  # generated in-memory
    assert calls == [True]  # and persisted exactly once


def test_persist_master_key_noop_when_already_persisted(monkeypatch):
    import app.config as cfg
    from secret_store.crypto import persist_master_key

    monkeypatch.setattr(cfg.settings.general, "secrets_encryption_key", "already-here", raising=False)
    calls = []
    monkeypatch.setattr(cfg, "write_config", lambda: calls.append(True))

    persist_master_key()

    assert calls == []  # nothing newly generated -> no write


# ------------------------------------------------ M9/L2 decrypt-failure handling
def _store_undecryptable_key(session, repo_inst):
    # Re-encrypt the stored api_key under a DIFFERENT master key so the real
    # master key cannot decrypt it (mirrors a rotated/changed secrets key).
    from secret_store.crypto import encrypt_secret
    repo_inst.api_key = encrypt_secret("secret", master_key="a-totally-different-master-key")
    session.flush()


def test_from_row_tolerates_undecryptable_key(schema_session):
    from arr_instances.client import ArrClientFactory
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create("sonarr", "S", api_key="secret")
    schema_session.flush()
    _store_undecryptable_key(schema_session, inst)

    client = ArrClientFactory(repo).from_row(inst)
    assert client is not None
    assert client.api_key == ""  # could not decrypt -> empty, not a crash


def test_get_decrypted_api_key_tolerates_undecryptable_key(schema_session):
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create("sonarr", "S", api_key="secret")
    schema_session.flush()
    _store_undecryptable_key(schema_session, inst)

    assert repo.get_decrypted_api_key(inst.id) is None  # clean None, not a raise


def test_test_connection_for_instance_handles_undecryptable_key(schema_session):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    inst = repo.create("sonarr", "S", api_key="secret")
    schema_session.flush()
    _store_undecryptable_key(schema_session, inst)

    body, status = service.test_connection_for_instance(schema_session, inst.id)
    assert status == 200
    assert body["ok"] is False
    assert body["error"] == "decrypt_failed"  # clean structured error, not a 500
