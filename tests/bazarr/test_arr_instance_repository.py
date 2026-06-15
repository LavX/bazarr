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


# --------------------------------------------------------------- update/delete

def test_update_changes_fields_and_preserves_api_key_when_omitted(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="orig-key", port=8989)

    repo.update(inst.id, name="Renamed", port=8990)
    schema_session.refresh(inst)

    assert inst.name == "Renamed"
    assert inst.port == 8990
    # api_key omitted -> preserved
    assert repo.get_decrypted_api_key(inst.id) == "orig-key"


def test_update_sets_new_api_key_when_provided(schema_session):
    from secret_store import is_encrypted

    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="orig-key")

    repo.update(inst.id, api_key="new-key")
    schema_session.refresh(inst)

    assert is_encrypted(inst.api_key)
    assert repo.get_decrypted_api_key(inst.id) == "new-key"


def test_update_clears_api_key_only_with_clear_flag(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="orig-key")

    # empty api_key without the flag does NOT clear (treated as "no change")
    repo.update(inst.id, api_key="")
    assert repo.get_decrypted_api_key(inst.id) == "orig-key"

    # explicit clear flag wipes it
    repo.update(inst.id, clear_api_key=True)
    assert repo.get_decrypted_api_key(inst.id) == ""


def test_set_default_demotes_previous_and_promotes_target(schema_session):
    repo = _repo(schema_session)
    main = repo.create("sonarr", "Main", api_key="k")      # default
    anime = repo.create("sonarr", "Anime", api_key="k")    # not default

    repo.set_default(anime.id)
    schema_session.refresh(main)
    schema_session.refresh(anime)

    assert main.is_default == 0
    assert anime.is_default == 1


def test_disabling_a_default_clears_its_default_flag(schema_session):
    # the DB CHECK requires is_default=0 OR enabled=1, so disabling a default
    # must also drop the default flag.
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="k")
    repo.update(inst.id, enabled=False)
    schema_session.refresh(inst)
    assert inst.enabled == 0
    assert inst.is_default == 0


def test_delete_removes_instance_when_no_owned_rows(schema_session):
    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="k")
    assert repo.delete(inst.id) is True
    assert repo.get(inst.id) is None


def test_delete_refused_when_owned_media_rows_exist(schema_session):
    from sqlalchemy import insert

    from app.database import TableShows

    repo = _repo(schema_session)
    inst = repo.create("sonarr", "Main", api_key="k")
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/show", title="Show", arr_instance_id=inst.id))

    with pytest.raises(ValueError):
        repo.delete(inst.id)
    assert repo.get(inst.id) is not None


# ----------------------------------------------- default invariant maintenance

def test_disabling_the_default_promotes_another_enabled_instance(schema_session):
    repo = _repo(schema_session)
    a = repo.create("sonarr", "A", api_key="k")   # default
    b = repo.create("sonarr", "B", api_key="k")   # enabled, not default

    repo.update(a.id, enabled=False)
    schema_session.refresh(a)
    schema_session.refresh(b)

    assert a.enabled == 0 and a.is_default == 0
    assert b.is_default == 1
    assert repo.get_default("sonarr").id == b.id


def test_clearing_default_flag_promotes_another_enabled_instance(schema_session):
    repo = _repo(schema_session)
    a = repo.create("sonarr", "A", api_key="k")   # default
    b = repo.create("sonarr", "B", api_key="k")

    repo.update(a.id, is_default=False)

    # the kind still has exactly one default, and it is the OTHER instance
    assert repo.get_default("sonarr").id == b.id


def test_disabling_the_only_instance_leaves_no_default_without_error(schema_session):
    repo = _repo(schema_session)
    a = repo.create("sonarr", "A", api_key="k")

    repo.update(a.id, enabled=False)
    schema_session.refresh(a)

    assert a.enabled == 0 and a.is_default == 0
    # all instances of the kind are disabled -> no active default is fine
    assert repo.get_default("sonarr") is None


def test_deleting_the_default_promotes_another_enabled_instance(schema_session):
    repo = _repo(schema_session)
    a = repo.create("sonarr", "A", api_key="k")   # default
    b = repo.create("sonarr", "B", api_key="k")

    repo.delete(a.id)

    assert repo.get_default("sonarr").id == b.id
