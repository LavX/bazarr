# coding=utf-8
"""ArrInstanceRepository: the encryption + CRUD boundary for arr_instances (#156).

The repository is the single place where per-instance Sonarr/Radarr API keys
are encrypted at rest. arr_instances rows live outside config.yaml's Fernet
encryption, so the key is encrypted here on write and decrypted on read.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 2).
"""
import pytest


def _repo(session):
    from arr_instances.repository import ArrInstanceRepository

    return ArrInstanceRepository(session)


def test_create_stores_instance_and_returns_it(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="secret-key", port=8989)

    assert inst.id is not None
    assert inst.kind == "sonarr"
    assert inst.name == "Main"
    assert inst.port == 8989


def test_api_key_is_encrypted_at_rest(schema_session):
    from secret_store import is_encrypted

    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="plaintext-key")

    # the stored column is ciphertext, never the plaintext
    assert inst.api_key != "plaintext-key"
    assert is_encrypted(inst.api_key)
    # and the repository can decrypt it back for runtime use
    assert repo.get_decrypted_api_key(inst.id) == "plaintext-key"


def test_empty_api_key_stays_empty(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("radarr", "Main", api_key="")
    assert inst.api_key == ""
    assert repo.get_decrypted_api_key(inst.id) == ""


def test_stable_key_is_slugified_from_name(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Anime 4K!", api_key="k")
    assert inst.stable_key == "anime-4k"


def test_stable_key_collisions_get_suffixed(schema_session):
    repo = _repo(schema_session)
    a = repo.create("sonarr", "Main", api_key="k")
    b = repo.create("sonarr", "Main", api_key="k", is_default=False)
    assert a.stable_key == "main"
    assert b.stable_key == "main-2"


def test_first_enabled_instance_of_a_kind_becomes_default(schema_session):
    repo = _repo(schema_session)
    first = repo.create("sonarr", "Main", api_key="k")
    second = repo.create("sonarr", "Anime", api_key="k")
    assert first.is_default == 1
    assert second.is_default == 0


def test_explicit_default_demotes_the_previous_default(schema_session):
    repo = _repo(schema_session)
    first = repo.create("sonarr", "Main", api_key="k")
    second = repo.create("sonarr", "Anime", api_key="k", is_default=True)
    schema_session.refresh(first)
    assert first.is_default == 0
    assert second.is_default == 1


def test_default_per_kind_is_independent(schema_session):
    repo = _repo(schema_session)
    s = repo.create("sonarr", "S", api_key="k")
    r = repo.create("radarr", "R", api_key="k")
    assert s.is_default == 1
    assert r.is_default == 1


def test_radarr_default_port(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("radarr", "Main", api_key="k")
    assert inst.port == 7878


def test_invalid_kind_raises(schema_session):
    repo = _repo(schema_session)
    with pytest.raises(ValueError):
        repo.create("plex", "Nope", api_key="k")


def test_get_default_and_lookups(schema_session):
    repo = _repo(schema_session)
    main = repo.create("sonarr", "Main", api_key="k")
    repo.create("sonarr", "Anime", api_key="k")

    assert repo.get_default("sonarr").id == main.id
    assert repo.get_by_key("sonarr", "main").id == main.id
    assert {i.name for i in repo.list("sonarr")} == {"Main", "Anime"}
    assert repo.list("radarr") == []


def test_to_safe_dict_never_exposes_api_key(schema_session):
    from arr_instances.repository import to_safe_dict

    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="super-secret")
    safe = to_safe_dict(inst)

    assert "api_key" not in safe
    assert safe["api_key_set"] is True
    assert "super-secret" not in str(safe)
    assert safe["kind"] == "sonarr"
    assert safe["is_default"] is True

    empty = repo.create("radarr", "R", api_key="")
    assert to_safe_dict(empty)["api_key_set"] is False
