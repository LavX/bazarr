# coding=utf-8
"""Phase 1d backfill (#156): represent the existing single-instance Sonarr/
Radarr scalar config as the default arr_instances rows and stamp existing owned
rows with arr_instance_id. Idempotent and non-destructive.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 1).
"""
from types import SimpleNamespace

from sqlalchemy import insert, select


def _settings(use_sonarr=True, use_radarr=True):
    return SimpleNamespace(
        general=SimpleNamespace(use_sonarr=use_sonarr, use_radarr=use_radarr),
        sonarr=SimpleNamespace(ip="10.0.0.5", port=8989, base_url="/", ssl=False,
                               verify_ssl=False, http_timeout=60,
                               apikey="sonarr-key"),
        radarr=SimpleNamespace(ip="10.0.0.6", port=7878, base_url="/", ssl=True,
                               verify_ssl=False, http_timeout=90,
                               apikey="radarr-key"),
    )


def test_backfill_creates_default_instances_from_scalar_config(schema_session):
    from app.database import TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/m/a.mkv", title="A", tmdbId="10"))

    backfill_default_instances(schema_session, _settings())

    repo = ArrInstanceRepository(schema_session)
    s = repo.get_default("sonarr")
    r = repo.get_default("radarr")
    assert s is not None and s.ip == "10.0.0.5" and s.port == 8989 and s.is_default == 1
    assert r is not None and r.ssl == 1 and r.http_timeout == 90
    # the scalar apikey is encrypted into the instance, decryptable for runtime
    assert repo.get_decrypted_api_key(s.id) == "sonarr-key"
    assert repo.get_decrypted_api_key(r.id) == "radarr-key"


def test_backfill_stamps_existing_owned_rows(schema_session):
    from app.database import TableEpisodes, TableMovies, TableShows
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/a", title="A"))
    schema_session.execute(insert(TableEpisodes).values(
        sonarrEpisodeId=1, sonarrSeriesId=1, season=1, episode=1,
        path="/tv/a/s01e01.mkv", title="P"))
    schema_session.execute(insert(TableMovies).values(
        radarrId=1, path="/m/a.mkv", title="A", tmdbId="10"))

    backfill_default_instances(schema_session, _settings())
    repo = ArrInstanceRepository(schema_session)
    sid = repo.get_default("sonarr").id
    rid = repo.get_default("radarr").id

    assert schema_session.execute(select(TableShows)).scalar_one().arr_instance_id == sid
    assert schema_session.execute(select(TableEpisodes)).scalar_one().arr_instance_id == sid
    assert schema_session.execute(select(TableMovies)).scalar_one().arr_instance_id == rid


def test_backfill_is_idempotent(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    backfill_default_instances(schema_session, _settings())
    backfill_default_instances(schema_session, _settings())  # second run no-ops

    repo = ArrInstanceRepository(schema_session)
    assert len(repo.list("sonarr")) == 1
    assert len(repo.list("radarr")) == 1


def test_backfill_skips_kind_with_nothing_to_own(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    backfill_default_instances(
        schema_session, _settings(use_sonarr=False, use_radarr=True))

    repo = ArrInstanceRepository(schema_session)
    assert repo.get_default("sonarr") is None
    assert repo.get_default("radarr") is not None


def test_backfill_does_not_clobber_existing_default(schema_session):
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    existing = repo.create("sonarr", "My Sonarr", api_key="manual", ip="1.1.1.1")

    backfill_default_instances(schema_session, _settings())

    assert repo.get_default("sonarr").id == existing.id
    assert repo.get_decrypted_api_key(existing.id) == "manual"


def test_backfill_does_not_resurrect_after_default_demoted(schema_session):
    # Regression: demoting/disabling the only instance left the kind with no
    # default, and a second backfill used to resurrect a duplicate. Backfill
    # must skip a kind that already has ANY instance, not just a default.
    from arr_instances.backfill import backfill_default_instances
    from arr_instances.repository import ArrInstanceRepository

    repo = ArrInstanceRepository(schema_session)
    backfill_default_instances(schema_session, _settings())
    sonarr = repo.get_default("sonarr")
    repo.update(sonarr.id, enabled=False)  # no default for sonarr now

    backfill_default_instances(schema_session, _settings())

    assert len(repo.list("sonarr")) == 1
