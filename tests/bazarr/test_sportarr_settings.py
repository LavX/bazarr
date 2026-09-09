# coding=utf-8
"""Global Sportarr settings, their validators, and the three-layer resolution.

Sportarr shipped with every setting stored per-instance. This suite pins the
replacement: dynaconf validators hold the defaults, the global config section is
where an operator normally works, and the instance blob is an optional override.
"""


def test_global_defaults_exist_with_expected_values():
    from app.config import settings

    assert settings.general.use_sportarr is False
    assert settings.general.minimum_score_sports == 70
    assert settings.general.wanted_search_frequency_sports == 6
    assert list(settings.general.path_mappings_sports) == []

    assert settings.sportarr.sports_sync == 60
    assert settings.sportarr.full_update == "Daily"
    assert settings.sportarr.full_update_day == 6
    assert settings.sportarr.full_update_hour == 4
    assert settings.sportarr.only_monitored is False
    assert settings.sportarr.sync_only_monitored_leagues is False
    assert settings.sportarr.sync_only_monitored_events is False
    assert list(settings.sportarr.excluded_tags) == []
    assert list(settings.sportarr.excluded_sports) == []
    assert settings.sportarr.search_on_sync is True
    assert settings.sportarr.use_ffprobe_cache is True
    assert settings.sportarr.enable_reconciled is False


def test_no_connection_scalars_are_declared():
    # Sportarr was born multi-instance and has no len==1 compat path, so the
    # scalar-mirror invariant that forces sonarr.ip / radarr.ip to exist does
    # not apply. Declaring them would import a maintenance burden for no caller.
    from app.config import settings

    section = {key.lower() for key in dict(settings.sportarr)}
    for absent in ("ip", "port", "base_url", "ssl", "apikey", "verify_ssl", "http_timeout"):
        assert absent not in section


def test_saving_a_sportarr_key_triggers_a_scheduler_refresh():
    # A settings change that does not re-register jobs appears to save while
    # doing nothing until restart, which is the failure mode hardest to notice.
    import inspect
    from app import config

    source = inspect.getsource(config.save_settings)
    for key in (
        "settings-sportarr-sports_sync",
        "settings-sportarr-full_update",
        "settings-sportarr-full_update_day",
        "settings-sportarr-full_update_hour",
        "settings-general-wanted_search_frequency_sports",
        "settings-general-use_sportarr",
    ):
        assert key in source, f"{key} missing from the scheduler-refresh key list"


def test_saving_a_sports_exclusion_key_invalidates_exclusions():
    import inspect
    from app import config

    source = inspect.getsource(config.save_settings)
    for key in (
        "settings-sportarr-excluded_tags",
        "settings-sportarr-excluded_sports",
        "settings-sportarr-only_monitored",
    ):
        assert key in source, f"{key} missing from the exclusion-invalidation key list"


def test_saving_sports_path_mappings_updates_the_path_map():
    import inspect
    from app import config

    assert "settings-general-path_mappings_sports" in inspect.getsource(config.save_settings)
