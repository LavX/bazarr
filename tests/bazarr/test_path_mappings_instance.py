# coding=utf-8
"""Per-instance path_mappings are now applied, not silently ignored (#156).

Finding 11: the arr_instances ``path_mappings`` column was write-only. The
PathMappings singleton now resolves the per-instance mapping via
``path_replace_instance`` / ``path_replace_reverse_instance``, falling back to
the global mapping when an instance has none.
"""

import json

from types import SimpleNamespace


def _seed_instance(schema_session, instance_id, path_mappings_json):
    from app.database import TableArrInstances

    schema_session.add(TableArrInstances(
        id=instance_id, kind="sonarr", stable_key=f"k{instance_id}",
        name=f"i{instance_id}", enabled=1, is_default=0, ip="127.0.0.1",
        port=8989, base_url="/", ssl=0, verify_ssl=0, http_timeout=60,
        api_key="", path_mappings=path_mappings_json))
    schema_session.flush()


def test_path_replace_instance_uses_instance_mapping(schema_session, monkeypatch):
    import utilities.path_mappings as pm_mod
    from app.database import database as real_db  # noqa: F401

    # point the helper's DB at the schema_session
    monkeypatch.setattr("app.database.database", schema_session)

    _seed_instance(schema_session, 7, json.dumps({
        "series": [["/remote/tv", "/local/tv"]],
        "movies": [["/remote/movies", "/local/movies"]],
    }))

    pm = pm_mod.PathMappings()
    # no global mapping configured
    monkeypatch.setattr(pm_mod, "settings", SimpleNamespace(
        general=SimpleNamespace(path_mappings=[], path_mappings_movie=[])))
    pm.update()

    out = pm.path_replace_instance("/remote/tv/show/s01e01.mkv", 7, "series")
    assert out == "/local/tv/show/s01e01.mkv"

    out_movie = pm.path_replace_instance("/remote/movies/film.mkv", 7, "movie")
    assert out_movie == "/local/movies/film.mkv"


def test_path_replace_instance_reverse(schema_session, monkeypatch):
    import utilities.path_mappings as pm_mod

    monkeypatch.setattr("app.database.database", schema_session)
    _seed_instance(schema_session, 7, json.dumps({"series": [["/remote/tv", "/local/tv"]]}))

    pm = pm_mod.PathMappings()
    monkeypatch.setattr(pm_mod, "settings", SimpleNamespace(
        general=SimpleNamespace(path_mappings=[], path_mappings_movie=[])))
    pm.update()

    out = pm.path_replace_reverse_instance("/local/tv/show/s01e01.mkv", 7, "series")
    assert out == "/remote/tv/show/s01e01.mkv"


def test_path_replace_instance_falls_back_to_global_when_no_instance_mapping(schema_session, monkeypatch):
    import utilities.path_mappings as pm_mod

    monkeypatch.setattr("app.database.database", schema_session)
    # instance exists but has NO path_mappings configured
    _seed_instance(schema_session, 7, None)

    pm = pm_mod.PathMappings()
    monkeypatch.setattr(pm_mod, "settings", SimpleNamespace(
        general=SimpleNamespace(
            path_mappings=[["/g/remote", "/g/local"]], path_mappings_movie=[])))
    pm.update()

    out = pm.path_replace_instance("/g/remote/show/s01e01.mkv", 7, "series")
    assert out == "/g/local/show/s01e01.mkv", "must fall back to the global mapping"


def test_path_replace_instance_none_owner_uses_global(schema_session, monkeypatch):
    import utilities.path_mappings as pm_mod

    monkeypatch.setattr("app.database.database", schema_session)

    pm = pm_mod.PathMappings()
    monkeypatch.setattr(pm_mod, "settings", SimpleNamespace(
        general=SimpleNamespace(
            path_mappings=[["/g/remote", "/g/local"]], path_mappings_movie=[])))
    pm.update()

    out = pm.path_replace_instance("/g/remote/x.mkv", None, "series")
    assert out == "/g/local/x.mkv", "None owner => global mapping (legacy)"
