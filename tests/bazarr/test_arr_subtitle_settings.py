# coding=utf-8
# Coverage for per-instance subtitle settings validation + options storage.
# See https://github.com/LavX/bazarr/issues/227
import json

import pytest

from arr_instances.repository import ArrInstanceRepository
from arr_instances.resolution import (
    clear_subtitle_settings_cache,
    resolve_subtitle_setting,
)
from arr_instances.subtitle_settings import (
    merge_subtitle_settings_into_options,
    read_subtitle_settings,
    validate_subtitle_settings,
)


def test_validate_accepts_known_settings():
    blob = {
        "general": {
            "use_postprocessing": True,
            "postprocessing_cmd": "/scripts/x.sh",
            "postprocessing_threshold": 80,
            "subzero_mods": ["remove_HI(keep_lyrics=1)", "common"],
            "subzero_mods_keep_lyrics": True,
        },
        "subsync": {
            "use_subsync": True,
            "subsync_threshold": 75,
            "enabled_engines": ["ffsubsync", "alass"],
            "max_offset_seconds": 120,
        },
    }
    assert validate_subtitle_settings(blob) == blob


def test_validate_empty_and_none():
    assert validate_subtitle_settings(None) == {}
    assert validate_subtitle_settings({}) == {}
    # empty sections are dropped
    assert validate_subtitle_settings({"general": {}}) == {}


def test_validate_rejects_unknown_section():
    with pytest.raises(ValueError):
        validate_subtitle_settings({"bogus": {"x": 1}})


def test_validate_rejects_not_allowed_key():
    # a real global key that is intentionally NOT per-instance
    with pytest.raises(ValueError):
        validate_subtitle_settings({"subsync": {"debug": True}})


@pytest.mark.parametrize(
    "blob",
    [
        {"general": {"postprocessing_threshold": 150}},   # out of range
        {"general": {"postprocessing_threshold": True}},   # bool is not an int pct
        {"general": {"use_postprocessing": "yes"}},        # not a bool
        {"general": {"subzero_mods": "remove_HI"}},        # not a list
        {"subsync": {"max_offset_seconds": 99}},           # not an allowed choice
        {"subsync": {"enabled_engines": ["bogus"]}},       # unknown engine
        {"subsync": {"enabled_engines": "ffsubsync"}},     # not a list
    ],
)
def test_validate_rejects_bad_values(blob):
    with pytest.raises(ValueError):
        validate_subtitle_settings(blob)


def test_read_subtitle_settings():
    blob = {"subsync": {"subsync_threshold": 80}}
    options = json.dumps({"subtitle_settings": blob, "other": 1})
    assert read_subtitle_settings(options) == blob
    assert read_subtitle_settings(None) == {}
    assert read_subtitle_settings("") == {}
    assert read_subtitle_settings("{not json") == {}
    assert read_subtitle_settings(json.dumps({"other": 1})) == {}


def test_merge_into_options_round_trip():
    blob = {"subsync": {"subsync_threshold": 80}}
    options = merge_subtitle_settings_into_options(None, blob)
    assert read_subtitle_settings(options) == blob


def test_merge_preserves_other_option_keys():
    existing = json.dumps({"some_other_option": "keep-me"})
    blob = {"general": {"use_postprocessing": True}}
    options = merge_subtitle_settings_into_options(existing, blob)
    parsed = json.loads(options)
    assert parsed["some_other_option"] == "keep-me"
    assert parsed["subtitle_settings"] == blob


def test_merge_empty_blob_clears_key():
    existing = json.dumps({"subtitle_settings": {"general": {"use_postprocessing": True}}})
    # clearing the only key returns None (nothing left to store)
    assert merge_subtitle_settings_into_options(existing, {}) is None
    # clearing while other options remain keeps them, drops subtitle_settings
    existing2 = json.dumps(
        {"subtitle_settings": {"general": {"use_postprocessing": True}}, "keep": 1}
    )
    options = merge_subtitle_settings_into_options(existing2, {})
    assert read_subtitle_settings(options) == {}
    assert json.loads(options) == {"keep": 1}


def _make_instance(session, kind, name, port, blob=None):
    options = merge_subtitle_settings_into_options(None, blob) if blob else None
    row = ArrInstanceRepository(session).create(kind, name, port=port, options=options)
    clear_subtitle_settings_cache()
    return row


def test_resolve_returns_override(schema_session):
    row = _make_instance(schema_session, "sonarr", "Main", 8989,
                         {"subsync": {"subsync_threshold": 80}})
    assert resolve_subtitle_setting(
        row.id, "subsync.subsync_threshold", 90, session=schema_session) == 80


def test_resolve_missing_key_and_section_fall_back_to_global(schema_session):
    row = _make_instance(schema_session, "sonarr", "Main", 8989,
                         {"subsync": {"subsync_threshold": 80}})
    # key absent within an overridden section
    assert resolve_subtitle_setting(
        row.id, "subsync.use_subsync", False, session=schema_session) is False
    # section not overridden at all
    assert resolve_subtitle_setting(
        row.id, "general.use_postprocessing", True, session=schema_session) is True


def test_resolve_none_instance_returns_global(schema_session):
    assert resolve_subtitle_setting(
        None, "subsync.subsync_threshold", 90, session=schema_session) == 90


def test_resolve_instance_without_options_returns_global(schema_session):
    row = _make_instance(schema_session, "radarr", "Movies", 7878)
    assert resolve_subtitle_setting(
        row.id, "subsync.subsync_threshold", 90, session=schema_session) == 90


def test_resolve_reflects_change_after_cache_clear(schema_session):
    row = _make_instance(schema_session, "sonarr", "S", 8989,
                         {"subsync": {"subsync_threshold": 80}})
    assert resolve_subtitle_setting(
        row.id, "subsync.subsync_threshold", 90, session=schema_session) == 80
    ArrInstanceRepository(schema_session).update(
        row.id,
        options=merge_subtitle_settings_into_options(None, {"subsync": {"subsync_threshold": 50}}))
    clear_subtitle_settings_cache()
    assert resolve_subtitle_setting(
        row.id, "subsync.subsync_threshold", 90, session=schema_session) == 50
