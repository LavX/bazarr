import inspect
import logging

import pytest
from subliminal_patch.core import Language

from app import get_providers


def test_get_providers_auth():
    for val in get_providers.get_providers_auth().values():
        assert isinstance(val, dict)


def test_get_providers_auth_with_provider_registry():
    """Make sure all providers will be properly initialized with bazarr
    configs"""
    from subliminal_patch.extensions import provider_registry

    auths = get_providers.get_providers_auth()
    for key, val in auths.items():
        provider = provider_registry[key]
        sign = inspect.signature(provider.__init__)
        for sub_key in val.keys():
            if sub_key not in sign.parameters:
                raise ValueError(f"'{sub_key}' parameter not present in {provider}")

            assert sign.parameters[sub_key] is not None


def test_get_providers_auth_embeddedsubtitles():
    item = get_providers.get_providers_auth()["embeddedsubtitles"]
    assert isinstance(item["included_codecs"], list)
    assert isinstance(item["hi_fallback"], bool)
    assert isinstance(item["cache_dir"], str)
    assert isinstance(item["ffprobe_path"], str)
    assert isinstance(item["ffmpeg_path"], str)
    assert isinstance(item["timeout"], int)
    assert isinstance(item["unknown_as_fallback"], bool)
    assert isinstance(item["fallback_lang"], str)


def test_get_providers_auth_karagarga():
    item = get_providers.get_providers_auth()["karagarga"]
    assert item["username"] is not None
    assert item["password"] is not None
    assert item["f_username"] is not None
    assert item["f_password"] is not None


def test_get_language_equals_default_settings():
    assert isinstance(get_providers.get_language_equals(), list)


def test_get_language_equals_injected_settings_invalid():
    config = get_providers.settings
    config.set("general.language_equals", ["invalid"])
    assert not get_providers.get_language_equals(config)


def test_get_language_equals_injected_settings_valid():
    config = get_providers.settings
    config.set("general.language_equals", ["spa:spa-MX"])

    result = get_providers.get_language_equals(config)
    assert result == [(Language("spa"), Language("spa", "MX"))]


@pytest.mark.parametrize(
    "config_value,expected",
    [
        (["spa:spl"], (Language("spa"), Language("spa", "MX"))),
        (["por:pob"], (Language("por"), Language("por", "BR"))),
        (["zho:zht"], (Language("zho"), Language("zho", "TW"))),
    ],
)
def test_get_language_equals_injected_settings_custom_lang_alpha3(
    config_value, expected
):
    config = get_providers.settings

    config.set("general.language_equals", config_value)

    result = get_providers.get_language_equals(config)
    assert result == [expected]


def test_get_language_equals_injected_settings_multiple():
    config = get_providers.settings

    config.set(
        "general.language_equals",
        ["eng@hi:eng", "spa:spl", "spa@hi:spl", "spl@hi:spl"],
    )

    result = get_providers.get_language_equals(config)
    assert len(result) == 4


def test_get_language_equals_injected_settings_valid_multiple():
    config = get_providers.settings
    config.set("general.language_equals", ["spa:spa-MX", "spa-MX:spa"])

    result = get_providers.get_language_equals(config)
    assert result == [
        (Language("spa"), Language("spa", "MX")),
        (Language("spa", "MX"), Language("spa")),
    ]


def test_get_language_equals_injected_settings_hi():
    config = get_providers.settings
    config.set("general.language_equals", ["eng@hi:eng"])

    result = get_providers.get_language_equals(config)
    assert result == [(Language("eng", hi=True), Language("eng"))]


def test_get_provider_language_exclusions_parses_configured_languages():
    config = get_providers.settings
    original = getattr(config.general, "provider_languages", {})
    config.set(
        "general.provider_languages",
        {
            "opensubtitlescom": ["eng", "bul"],
            "subsunacs": [],
        },
    )

    try:
        result = get_providers.get_provider_language_exclusions(config)
    finally:
        config.set("general.provider_languages", original)

    assert result == {
        "opensubtitlescom": {Language("eng"), Language("bul")},
    }


def test_get_provider_language_hook_returns_configured_exclusions():
    config = get_providers.settings
    original = getattr(config.general, "provider_languages", {})
    config.set(
        "general.provider_languages",
        {
            "opensubtitlescom": ["eng"],
        },
    )

    try:
        hook = get_providers.get_provider_language_hook(config)
        result = hook("opensubtitlescom")
        unrestricted = hook("subsunacs")
    finally:
        config.set("general.provider_languages", original)

    assert result == {Language("eng")}
    assert unrestricted is None


def test_native_subtitle_pool_uses_provider_language_hook(monkeypatch):
    from subtitles import pool as subtitle_pool

    captured = {}

    class CapturingPool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(subtitle_pool, "provider_pool", lambda: CapturingPool)
    monkeypatch.setattr(subtitle_pool, "get_providers_sorted", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(subtitle_pool, "get_providers_auth", lambda: {})
    monkeypatch.setattr(subtitle_pool, "get_blacklist", lambda: [])
    monkeypatch.setattr(subtitle_pool, "get_blacklist_movie", lambda: [])
    monkeypatch.setattr(
        subtitle_pool,
        "get_ban_list",
        lambda profile_id: {"must_contain": [], "must_not_contain": []},
    )
    monkeypatch.setattr(subtitle_pool, "get_language_equals", lambda: [])
    monkeypatch.setattr(
        subtitle_pool,
        "get_provider_language_hook",
        lambda: lambda provider: {Language("eng")},
    )

    subtitle_pool._init_pool("movie")

    assert callable(captured["language_hook"])
    assert captured["language_hook"]("opensubtitlescom") == {Language("eng")}


def _get_error():
    try:
        raise ValueError("Some error" * 100)
    except ValueError as error:
        return error


def test_get_traceback_info():
    error_ = _get_error()

    if error_ is not None:
        msg = get_providers._get_traceback_info(error_)
        assert len(msg) == 100


class TestProviderIsUsable:
    """provider_is_usable is the pools' adoption gate: a stale cached search
    result must not resurrect a provider the user disabled or the backoff
    throttled (see SZProviderPool.__getitem__)."""

    def test_disabled_provider_is_not_usable(self, monkeypatch):
        monkeypatch.setattr(
            get_providers.settings.general, "enabled_providers", ["opensubtitlescom"]
        )
        monkeypatch.setattr(get_providers, "tp", {})
        assert not get_providers.provider_is_usable("podnapisi")

    def test_enabled_unthrottled_provider_is_usable(self, monkeypatch):
        monkeypatch.setattr(
            get_providers.settings.general, "enabled_providers", ["opensubtitlescom"]
        )
        monkeypatch.setattr(get_providers, "tp", {})
        assert get_providers.provider_is_usable("opensubtitlescom")

    def test_throttled_provider_is_not_usable(self, monkeypatch):
        import datetime

        monkeypatch.setattr(
            get_providers.settings.general, "enabled_providers", ["opensubtitlescom"]
        )
        until = datetime.datetime.now() + datetime.timedelta(hours=1)
        monkeypatch.setattr(
            get_providers, "tp", {"opensubtitlescom": ("Throttled", until, "1h")}
        )
        assert not get_providers.provider_is_usable("opensubtitlescom")

    def test_expired_throttle_is_usable_again(self, monkeypatch):
        import datetime

        monkeypatch.setattr(
            get_providers.settings.general, "enabled_providers", ["opensubtitlescom"]
        )
        until = datetime.datetime.now() - datetime.timedelta(minutes=1)
        monkeypatch.setattr(
            get_providers, "tp", {"opensubtitlescom": ("Throttled", until, "1h")}
        )
        assert get_providers.provider_is_usable("opensubtitlescom")

    def test_a_throttled_provider_says_so_above_debug(self, monkeypatch, caplog):
        """Skipping a provider changes what the reader gets, so the line has to be
        readable without general.debug. It was logging.debug, and a report that
        "provider X does nothing" is exactly what that line answers. Pinned here
        rather than in the level table, because the root logger would accept an
        INFO record either way: only the call site says which level it is emitted
        at."""
        import datetime

        monkeypatch.setattr(
            get_providers.settings.general, "enabled_providers", ["opensubtitlescom"]
        )
        until = datetime.datetime.now() + datetime.timedelta(hours=1)
        monkeypatch.setattr(
            get_providers, "tp", {"opensubtitlescom": ("WorkerError", until, "5 minutes")}
        )
        # The hub registration gate is once per process, and the provider-hub tests
        # in this same pytest process need it still unconsumed so their state file is
        # read when they set it. This test is about the level of the throttle line.
        monkeypatch.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)

        with caplog.at_level(logging.INFO):
            # None, not [], is how an empty provider list comes back.
            assert get_providers.get_providers() is None

        record = next(
            record for record in caplog.records
            if "Not using opensubtitlescom" in record.getMessage()
        )
        assert record.levelno == logging.INFO


class TestProviderHubSettingsOverlay:
    """The pool overlay copies a plugin's declared config keys out of the
    settings section named after the plugin. Settings are decrypted in memory,
    so a plugin whose id names one of Bazarr's own sections would be handed that
    section's credentials on every search and download (validate_manifest
    refuses such an id; this is the overlay's own half of that)."""

    @staticmethod
    def _installation(provider_id):
        from types import SimpleNamespace

        return SimpleNamespace(
            provider_id=provider_id,
            manifest={
                "config_schema": {
                    "type": "object",
                    "properties": {"apikey": {"type": "string"}},
                },
            },
        )

    def _patch_hub(self, monkeypatch, provider_id, stored_config):
        import provider_hub.service as hub_service
        import provider_hub.state as hub_state

        monkeypatch.setattr(
            hub_state,
            "active_installations",
            lambda: [self._installation(provider_id)],
        )
        monkeypatch.setattr(
            hub_service,
            "runtime_provider_configs",
            lambda: {provider_id: dict(stored_config)},
        )

    def test_plugin_never_receives_another_sections_secret(self, monkeypatch):
        config = get_providers.settings
        original = config.sonarr.apikey
        config.set("sonarr.apikey", "sonarr-api-key")  # pragma: allowlist secret
        self._patch_hub(monkeypatch, "sonarr", {"apikey": "plugin-own-key"})
        try:
            auth = get_providers.get_providers_auth()
        finally:
            config.set("sonarr.apikey", original)

        assert auth["sonarr"]["apikey"] == "plugin-own-key"

    def test_a_blocked_plugin_is_reported_once_per_process(self, monkeypatch, caplog):
        # The overlay runs on every search and download, so reporting per call
        # would have one bad install writing the same line forever.
        monkeypatch.setattr(get_providers, "_REPORTED_RESERVED_SECTION_PLUGINS", set())
        self._patch_hub(monkeypatch, "plex", {"apikey": "plugin-own-key"})

        with caplog.at_level(logging.ERROR):
            get_providers.get_providers_auth()
            get_providers.get_providers_auth()

        reports = [
            record for record in caplog.records
            if "Refusing to read the plex settings section" in record.getMessage()
        ]
        assert len(reports) == 1

    def test_plugin_reads_its_own_settings_section(self, monkeypatch):
        config = get_providers.settings
        config.set("overlayhub", {"apikey": "typed-into-the-settings-card"})
        self._patch_hub(monkeypatch, "overlayhub", {"apikey": "stale-stored-key"})
        try:
            auth = get_providers.get_providers_auth()
        finally:
            config.set("overlayhub", {})

        assert auth["overlayhub"]["apikey"] == "typed-into-the-settings-card"


def test_concurrent_recorders_do_not_race_on_the_staging_file(monkeypatch, tmp_path):
    """Every writer stages through the same throttled_providers.dat.tmp. The
    compat fanout now records throttles from inside its provider futures, so
    two providers failing at once reach this function together: without
    serialization the second os.replace finds the file the first already moved
    and the FileNotFoundError surfaces out of a provider's error handler, which
    the fanout reports in place of the failure it was recording."""
    import datetime
    import os
    import threading
    import time

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    real_replace = os.replace

    def slow_replace(src, dst):
        # Widen the window the lock has to close. Without it the interleaving
        # is real but rare enough to pass by luck on a quiet machine.
        time.sleep(0.01)
        return real_replace(src, dst)

    monkeypatch.setattr(get_providers.os, "replace", slow_replace)
    until = datetime.datetime.now() + datetime.timedelta(minutes=10)
    failures = []

    def record(index):
        try:
            get_providers.set_throttled_providers(
                {f"provider_{index}": ("APIThrottled", until, "10 minutes")})
        except Exception as error:
            # Catching everything is the point: none must escape.
            failures.append(error)

    threads = [threading.Thread(target=record, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert (tmp_path / "config" / "throttled_providers.dat").exists()


def test_recording_a_throttle_never_walks_a_table_another_thread_is_resizing(monkeypatch, tmp_path):
    """The staging file is only half of it. provider_throttle hands the shared
    table straight to the writer, so a second recorder adding its own provider
    mid-serialization raises "dictionary changed size during iteration", and
    that escapes the first provider's error handler for the fanout to report in
    place of its real failure. Both the mutation and the write belong under one
    lock."""
    import threading
    from subliminal_patch.exceptions import APIThrottled

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    names = [f"racing_provider_{index}" for index in range(12)]
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", names)
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: list(names))
    failures = []

    def record(name):
        try:
            get_providers.provider_throttle(name, APIThrottled("slow down"), wait=False)
        except Exception as error:
            # Catching everything is the point: none must escape.
            failures.append(error)

    threads = [threading.Thread(target=record, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert set(get_providers.tp) == set(names)


def test_a_fresh_backoff_recorded_mid_sweep_is_not_released_by_the_stale_read(monkeypatch, tmp_path):
    """get_providers() reads the table, finds an entry expired, then removes
    it. A provider future recording a fresh backoff between those two steps had
    its deadline deleted on the strength of the one it replaced, and the
    provider went straight back into the next search."""
    import datetime
    import threading

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    name = "sweep_racer"
    expired = datetime.datetime.now() - datetime.timedelta(minutes=1)
    fresh = datetime.datetime.now() + datetime.timedelta(minutes=30)
    monkeypatch.setattr(get_providers, "tp", {name: ("APIThrottled", expired, "10 minutes")})
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [name])
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: [name])
    monkeypatch.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)

    recorded = threading.Event()
    real_lock = get_providers._THROTTLE_LOCK

    class RecordingLock:
        """Stand in for the real lock and, the first time the sweep reaches
        for it, let a recorder write a fresh deadline first."""

        def __enter__(self):
            if not recorded.is_set():
                recorded.set()
                get_providers.tp[name] = ("TooManyRequests", fresh, "30 minutes")
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    monkeypatch.setattr(get_providers, "_THROTTLE_LOCK", RecordingLock())

    assert get_providers.get_providers() is None, "the fresh backoff was not honoured"
    assert get_providers.tp[name][0] == "TooManyRequests"


def test_a_provider_asking_for_longer_than_its_class_gets_it(monkeypatch, tmp_path):
    """The map says what an exception class is worth in general. A provider
    that sent a Retry-After has said when it will answer again, and that is a
    floor the class's duration must not undercut: the header used to choose
    only how long to pause between retries, so a site asking for an hour was
    re-queried after the class's ten minutes."""
    import datetime
    from subliminal_patch.exceptions import APIThrottled

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["patient_provider"])
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: ["patient_provider"])

    get_providers.provider_throttle("patient_provider", APIThrottled(retry_after=3600), wait=False)

    _reason, until, description = get_providers.tp["patient_provider"]
    remaining = (until - datetime.datetime.now()).total_seconds()
    assert 3400 < remaining < 3700, "the class duration undercut what the provider asked for"
    assert description == "60 minutes"


def test_a_short_retry_after_does_not_undercut_the_class_duration(monkeypatch, tmp_path):
    """The longer of the two, not the header unconditionally."""
    import datetime
    from subliminal_patch.exceptions import APIThrottled

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["hasty_provider"])
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: ["hasty_provider"])

    get_providers.provider_throttle("hasty_provider", APIThrottled(retry_after=5), wait=False)

    _reason, until, description = get_providers.tp["hasty_provider"]
    remaining = (until - datetime.datetime.now()).total_seconds()
    assert 500 < remaining < 620 and description == "10 minutes"


def test_the_later_of_two_concurrent_deadlines_is_the_one_kept(monkeypatch, tmp_path):
    """Two searches can have the same provider in flight and compute their
    deadlines before either records one, so the second writer is not
    necessarily the one with the most to say. An hour the provider asked for
    must not be replaced by a generic ten minutes from the other request."""
    import datetime
    from subliminal_patch.exceptions import APIThrottled

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", ["two_at_once"])
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: ["two_at_once"])

    get_providers.provider_throttle("two_at_once", APIThrottled(retry_after=3600), wait=False)
    get_providers.provider_throttle("two_at_once", APIThrottled("slow down"), wait=False)

    _reason, until, description = get_providers.tp["two_at_once"]
    remaining = (until - datetime.datetime.now()).total_seconds()
    assert 3400 < remaining < 3700, "a shorter concurrent record replaced the longer one"
    assert description == "60 minutes"
