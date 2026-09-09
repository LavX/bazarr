# coding=utf-8
"""Global Sportarr settings, their validators, and the three-layer resolution.

Sportarr shipped with every setting stored per-instance. This suite pins the
replacement: dynaconf validators hold the defaults, the global config section is
where an operator normally works, and the instance blob is an optional override.
"""


def test_global_defaults_exist_with_expected_values():
    # Asserted against the DECLARED validator defaults, not the live settings
    # object. dynaconf attributes do not round-trip through monkeypatch, so a
    # sibling test that flips a flag (the enable reconcile does exactly that)
    # leaks into the live values and made this order-dependent. The declared
    # schema is what this test actually means, and nothing can mutate it.
    from app.config import validators

    declared = {}
    for validator in validators:
        for name in getattr(validator, "names", ()):
            declared[name] = validator.default

    assert declared["general.use_sportarr"] is False
    assert declared["general.minimum_score_sports"] == 70
    assert declared["general.wanted_search_frequency_sports"] == 6
    assert declared["general.path_mappings_sports"] == []

    assert declared["sportarr.sports_sync"] == 60
    assert declared["sportarr.full_update"] == "Daily"
    assert declared["sportarr.full_update_day"] == 6
    assert declared["sportarr.full_update_hour"] == 4
    assert declared["sportarr.only_monitored"] is False
    assert declared["sportarr.sync_only_monitored_leagues"] is False
    assert declared["sportarr.sync_only_monitored_events"] is False
    assert declared["sportarr.excluded_tags"] == []
    assert declared["sportarr.excluded_sports"] == []
    assert declared["sportarr.search_on_sync"] is True
    assert declared["sportarr.use_ffprobe_cache"] is True
    assert declared["sportarr.enable_reconciled"] is False


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


def test_resolution_falls_through_global_then_override(schema_session, monkeypatch):
    from app.config import settings
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.settings import get_sports_settings, merge_sports_settings

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sportarr", "Main", api_key="fixture-secret")

    # Layer 1: validator defaults, no global change, no override.
    assert get_sports_settings(instance)["minimum_score"] == 70
    assert get_sports_settings(instance)["sports_sync"] == 60

    # Layer 2: the global value wins over the default.
    monkeypatch.setattr(settings.general, "minimum_score_sports", 55)
    monkeypatch.setattr(settings.sportarr, "sports_sync", 180)
    assert get_sports_settings(instance)["minimum_score"] == 55
    assert get_sports_settings(instance)["sports_sync"] == 180

    # Layer 3: the instance override wins over the global.
    instance.options = merge_sports_settings(instance.options, {"minimum_score": 90})
    assert get_sports_settings(instance)["minimum_score"] == 90
    # An unoverridden key still resolves globally.
    assert get_sports_settings(instance)["sports_sync"] == 180


def test_override_is_isolated_to_its_instance(schema_session):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.settings import get_sports_settings, merge_sports_settings

    repo = ArrInstanceRepository(schema_session)
    first = repo.create("sportarr", "Main", api_key="a")
    second = repo.create("sportarr", "Second", api_key="b")

    first.options = merge_sports_settings(first.options, {"only_monitored": True})
    assert get_sports_settings(first)["only_monitored"] is True
    assert get_sports_settings(second)["only_monitored"] is False


def test_renamed_keys_replace_the_old_names(schema_session):
    from arr_instances.repository import ArrInstanceRepository
    from sportarr.settings import GLOBAL_SOURCES, get_sports_settings

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sportarr", "Main", api_key="fixture-secret")
    resolved = get_sports_settings(instance)

    for new in ("sports_sync", "full_update", "full_update_day", "full_update_hour"):
        assert new in resolved
    for old in ("sync_interval", "full_scan", "full_scan_day", "full_scan_hour"):
        assert old not in resolved
        assert old not in GLOBAL_SOURCES


def test_global_layer_hands_back_copies_not_live_config():
    # A caller mutating the resolved dict must not write back into dynaconf.
    from app.config import settings
    from sportarr.settings import global_sports_settings

    resolved = global_sports_settings()
    resolved["excluded_tags"].append("scratch")
    assert list(settings.sportarr.excluded_tags) == []


def test_validate_rejects_unknown_and_malformed_overrides():
    import pytest

    from sportarr.settings import validate_sports_settings

    assert validate_sports_settings(None) == {}
    assert validate_sports_settings({}) == {}
    assert validate_sports_settings({"only_monitored": True}) == {"only_monitored": True}

    with pytest.raises(ValueError):
        validate_sports_settings({"sync_interval": 60})
    with pytest.raises(ValueError):
        validate_sports_settings({"full_update": "Hourly"})
    with pytest.raises(ValueError):
        validate_sports_settings({"minimum_score": 0})
    with pytest.raises(ValueError):
        validate_sports_settings({"excluded_tags": "notalist"})
    with pytest.raises(ValueError):
        validate_sports_settings({"only_monitored": "yes"})
    with pytest.raises(ValueError):
        validate_sports_settings("notadict")


def test_non_sportarr_instance_is_rejected(schema_session):
    import pytest

    from arr_instances.repository import ArrInstanceRepository
    from sportarr.settings import get_sports_settings

    repo = ArrInstanceRepository(schema_session)
    sonarr = repo.create("sonarr", "Main")
    with pytest.raises(ValueError):
        get_sports_settings(sonarr)


def test_list_valued_sports_keys_are_registered_as_arrays():
    """A list key missing from array_keys is unwrapped to a scalar on save.

    save_settings turns a single-element form list into a bare value unless the
    key is registered here. The validator then rejects it for not being a list,
    which fails the WHOLE settings POST with a 406 and silently discards every
    other change in the same save. Caught by clicking Save in a browser, not by
    any unit test, so it is pinned here.
    """
    from app.config import array_keys

    assert "excluded_sports" in array_keys
    assert "path_mappings_sports" in array_keys
    # excluded_tags and path_mappings are matched on the bare sub-key, so the
    # sportarr section reuses the entries the sonarr/radarr sections added.
    assert "excluded_tags" in array_keys
    assert "path_mappings" in array_keys


def test_sports_path_mappings_are_parsed_as_pairs():
    """Path mapping values arrive as "remote,local" strings and must be split.

    Without the key in that list the sports mappings persist as flat strings
    instead of [remote, local] pairs, so read_sports_mappings sees nothing
    usable and every mapped path silently fails to resolve.
    """
    import inspect
    from app import config

    source = inspect.getsource(config.save_settings)
    marker = "'path_mappings', 'path_mappings_movie', 'path_mappings_sports'"
    assert marker in source, "path_mappings_sports missing from the pair-splitting list"


def test_api_serves_the_override_blob_not_the_resolved_values(schema_session):
    """to_safe_dict must expose only what the instance actually overrides.

    The settings UI renders one row per setting and reads a present key as
    "this instance overrides the global". Handing it the resolved merge showed
    all thirteen as overridden, and saving that form would have frozen them as
    real overrides that no longer track Connections and Scheduler settings.
    Caught by opening the instance modal in a browser.
    """
    from arr_instances.repository import ArrInstanceRepository, to_safe_dict
    from sportarr.settings import GLOBAL_SOURCES, merge_sports_settings

    repo = ArrInstanceRepository(schema_session)
    instance = repo.create("sportarr", "Main", api_key="fixture-secret")

    # Nothing overridden yet: the blob is empty, every row inherits.
    assert to_safe_dict(instance)["sports_settings"] == {}

    instance.options = merge_sports_settings(instance.options, {"only_monitored": True})
    served = to_safe_dict(instance)["sports_settings"]
    assert served == {"only_monitored": True}
    assert len(served) < len(GLOBAL_SOURCES), "the resolved merge leaked into the API"
