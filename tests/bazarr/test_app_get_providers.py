import datetime
import inspect
import logging
import socket
from types import SimpleNamespace

import pytest
import requests
from provider_hub.worker import WorkerError
from subliminal.exceptions import (AuthenticationError, ConfigurationError, DownloadLimitExceeded,
                                  ServiceUnavailable)
from subliminal_patch import core as patch_core
from subliminal_patch.core import Language
from subliminal_patch.exceptions import (APIThrottled, ForbiddenError, IPAddressBlocked, SearchLimitReached,
                                         TooManyRequests)

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

    def test_disabled_provider_becomes_usable_when_reenabled(self, monkeypatch):
        monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [])
        monkeypatch.setattr(
            get_providers.settings.general, "disabled_providers", ["podnapisi"], raising=False
        )
        monkeypatch.setattr(get_providers, "tp", {})

        assert not get_providers.provider_is_usable("podnapisi")

        get_providers.settings.general.enabled_providers = ["podnapisi"]
        get_providers.settings.general.disabled_providers = []

        assert get_providers.provider_is_usable("podnapisi")

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
            "running_installations",
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

    def test_an_empty_section_collection_is_not_laid_into_the_worker_config(self, monkeypatch):
        # An empty list or dict at a declared field is an absent value, not
        # a supplied one: laying it over the stored config would hand the
        # plugin the empty collection in place of its configured value.
        config = get_providers.settings
        for empty in ([], {}):
            config.set("overlayhub", {"apikey": empty})
            self._patch_hub(monkeypatch, "overlayhub", {"apikey": "stale-stored-key"})
            try:
                auth = get_providers.get_providers_auth()
            finally:
                config.set("overlayhub", {})

            assert auth["overlayhub"]["apikey"] == "stale-stored-key"


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


# A provider failure that takes the provider out of rotation is one WARNING
# line, written where the backoff is decided: the provider, the operation,
# the reason in words, and when the provider is used again. The backoff
# itself does not move.


def _throttle_test_state(monkeypatch, tmp_path, name="fixture_provider"):
    """Isolated throttle state: the dat file lands in tmp_path, and no badge
    event or hub registration escapes the test."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [name])
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: [name])
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
    return name


# One row per expected condition, built-in form and plugin form, with the
# backoff it earns today: the reason class the table stores, the words the
# one line says, and the duration. The count gate holds only TooManyRequests,
# ServiceUnavailable and APIThrottled: VALID_COUNT_EXCEPTIONS also lists the
# requests timeout classes and socket.timeout, but as class objects, and the
# gate compares a class-name string against that tuple, so a timeout never
# matches and always records its backoff at once.
_COUNT_GATED_REASONS = {"TooManyRequests", "ServiceUnavailable", "APIThrottled"}


@pytest.mark.parametrize("error,words,description,seconds,reason", [
    (DownloadLimitExceeded("fixture quota"), "download quota reached", "3 hours", 3 * 3600,
     "DownloadLimitExceeded"),
    (TooManyRequests("fixture rate"), "rate limited", "1 hour", 3600, "TooManyRequests"),
    (APIThrottled("fixture throttle"), "rate limited", "10 minutes", 600, "APIThrottled"),
    (ServiceUnavailable("fixture outage"), "service unavailable", "20 minutes", 1200, "ServiceUnavailable"),
    (AuthenticationError("fixture auth"), "authentication failed", "12 hours", 12 * 3600, "AuthenticationError"),
    (ConfigurationError("fixture config"), "configuration invalid", "12 hours", 12 * 3600, "ConfigurationError"),
    (requests.exceptions.Timeout("fixture timeout"), "timed out", "1 hour", 3600, "Timeout"),
    (socket.timeout("fixture socket timeout"), "timed out", "1 hour", 3600, "TimeoutError"),
    (requests.exceptions.ConnectionError("fixture connection"), "connection failure", "10 minutes", 600,
     "ConnectionError"),
    (ForbiddenError("fixture refused"), "request refused by the provider", "10 minutes", 600, "ForbiddenError"),
    (IPAddressBlocked("fixture blocked address"), "address blocked by the provider", "10 minutes", 600,
     "IPAddressBlocked"),
    (SearchLimitReached("fixture search limit"), "search limit reached", "10 minutes", 600, "SearchLimitReached"),
    (WorkerError("fixture plugin quota", remote_class_name="DownloadLimitExceeded", code="provider"),
     "download quota reached", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture refused login", remote_class_name="PermissionError", code="provider"),
     "access refused by the provider", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture blocked", remote_class_name="IPAddressBlocked", code="provider"),
     "address blocked by the provider", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture search limit", remote_class_name="SearchLimitReached", code="provider"),
     "search limit reached", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture blocked by cloudflare", remote_class_name="CloudflareBlockedError", code="provider"),
     "automated access blocked", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture plugin timeout", remote_class_name="TimeoutError", code="provider"),
     "timed out", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture unreachable", remote_class_name="URLError", code="provider"),
     "connection failure", "10 minutes", 600, "WorkerError"),
    (WorkerError("fixture deadline", code="timeout"), "timed out", "1 hour", 3600, "Timeout"),
    (WorkerError("fixture worker connection", remote_class_name="ConnectionError", code="provider"),
     "connection failure", "10 minutes", 600, "ConnectionError"),
])
def test_an_expected_failure_writes_one_line_and_the_unchanged_backoff(
        monkeypatch, tmp_path, caplog, error, words, description, seconds, reason):
    name = _throttle_test_state(monkeypatch, tmp_path)
    if reason in _COUNT_GATED_REASONS:
        # Counted classes record only on their fifth failure in two minutes;
        # stand on the fourth so the row exercises the recording path.
        get_providers.throttle_count[name] = {
            "count": 4, "time": datetime.datetime.now() + datetime.timedelta(seconds=120)}

    before = datetime.datetime.now()
    with caplog.at_level(logging.WARNING):
        assert get_providers.provider_throttle(name, error, operation="search") is True
    after = datetime.datetime.now()

    stored_reason, until, stored_description = get_providers.tp[name]
    assert stored_reason == reason
    assert stored_description == description
    expected_delay = datetime.timedelta(seconds=seconds)
    assert before + expected_delay <= until <= after + expected_delay, "the backoff moved"

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, "an expected failure wrote more than its one line"
    record = warnings[0]
    assert record.exc_info is None
    message = record.getMessage()
    assert f"Provider {name} search failed: {words}" in message
    assert "Not using it" in message
    assert description in message
    assert until.strftime("%y/%m/%d %H:%M") in message


@pytest.mark.parametrize("error,words,class_name", [
    (TooManyRequests("fixture rate"), "rate limited", "TooManyRequests"),
    (ServiceUnavailable("fixture outage"), "service unavailable", "ServiceUnavailable"),
    (APIThrottled("fixture throttle"), "rate limited", "APIThrottled"),
], ids=["rate_limit", "service_unavailable", "api_throttle"])
@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_count_gated_failure_writes_one_line_and_no_backoff(monkeypatch, tmp_path, caplog, operation, error,
                                                               words, class_name):
    """While the gate has not tripped, the one line says the reason in words
    with its class, the attempt count and that the pool leaves the provider
    out until its next refresh; the wait itself is the gate's timing and does
    not change."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    monkeypatch.setattr(get_providers.time, "sleep", lambda seconds: None)

    with caplog.at_level(logging.WARNING):
        assert get_providers.provider_throttle(name, error, operation=operation) is False

    assert name not in get_providers.tp
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"Provider {name} {operation} failed: {words} ({class_name})" in message
    assert "count 1 of 5" in message
    assert "no backoff" in message
    assert "next refresh" in message


def test_a_worker_connection_failure_earns_the_providers_own_connection_backoff(monkeypatch, tmp_path):
    """A plugin worker's ConnectionError reaches the table as a WorkerError
    carrying the remote class name. It has to read the same per-provider
    duration the same failure raised in process would, not the generic one."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    monkeypatch.setattr(get_providers, "provider_throttle_map", lambda: {
        "default": {},
        name: {requests.exceptions.ConnectionError: (datetime.timedelta(minutes=5), "5 minutes")}})

    error = WorkerError("fixture worker connection", remote_class_name="ConnectionError", code="provider")
    assert get_providers.provider_throttle(name, error) is True

    reason, _until, description = get_providers.tp[name]
    assert (reason, description) == ("ConnectionError", "5 minutes")


def _legacy_entry(reason):
    return (reason, datetime.datetime.now() + datetime.timedelta(hours=12), "12 hours")


def test_correcting_credentials_clears_a_throttle_stored_under_an_auth_subclass(monkeypatch, tmp_path):
    """A throttle is stored under the concrete class name, so a provider that
    raised a subclass of AuthenticationError sits in the table under that
    name. The credential reset has to match the hierarchy, not three literals,
    and an entry written by an older build must be matched the same way."""
    from subliminal_patch.providers.opensubtitles import NoSession, Unauthorized

    name = _throttle_test_state(monkeypatch, tmp_path)
    for subclass in (Unauthorized, NoSession):
        monkeypatch.setitem(get_providers.tp, name, _legacy_entry(subclass.__name__))
        assert get_providers.reset_throttled_provider(name) is True
        assert name not in get_providers.tp


def test_the_bulk_credential_reset_drops_auth_subclasses_and_keeps_the_rest(monkeypatch, tmp_path):
    _throttle_test_state(monkeypatch, tmp_path)
    names = ["subclassed", "base", "paid", "rate", "unknown"]
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", names)
    monkeypatch.setattr(get_providers.provider_registry, "names", lambda: names)
    get_providers.tp.update({
        "subclassed": _legacy_entry("Unauthorized"),
        "base": _legacy_entry("AuthenticationError"),
        "paid": _legacy_entry("PaymentRequired"),
        "rate": _legacy_entry("TooManyRequests"),
        "unknown": _legacy_entry("SomethingElseEntirely"),
    })

    get_providers.reset_throttled_providers(only_auth_or_conf_error=True)

    assert sorted(get_providers.tp) == ["rate", "unknown"]


def test_a_throttle_file_from_an_older_build_still_reads_after_the_reset_change(monkeypatch, tmp_path):
    name = _throttle_test_state(monkeypatch, tmp_path)
    until = datetime.datetime.now() + datetime.timedelta(hours=12)
    (tmp_path / "config" / "throttled_providers.dat").write_text(
        '{"%s": ["Unauthorized", "%s", "12 hours"]}' % (name, until.isoformat()))

    loaded = get_providers.get_throttled_providers()

    assert loaded[name] == ("Unauthorized", until, "12 hours")
    monkeypatch.setattr(get_providers, "tp", loaded)
    assert get_providers.reset_throttled_provider(name) is True


def test_a_no_wait_rate_limit_writes_the_throttling_line_only(monkeypatch, tmp_path, caplog):
    """The compat pool never retries, so it records at once. The count line
    drops below the default filter: the recorded-backoff line follows it."""
    name = _throttle_test_state(monkeypatch, tmp_path)

    with caplog.at_level(logging.DEBUG):
        assert get_providers.provider_throttle(name, TooManyRequests("fixture rate"), wait=False) is True

    counted = [record for record in caplog.records if "recorded without waiting" in record.getMessage()]
    assert len(counted) == 1
    assert counted[0].levelno == logging.DEBUG
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Not using it for 1 hour" in warnings[0].getMessage()


def test_an_unannounced_backoff_waits_for_the_download_to_say_it(monkeypatch, tmp_path, caplog):
    """A download's attempts all pass announce=False, so their recording stays
    below the default filter; the one line comes once the download stops."""
    name = _throttle_test_state(monkeypatch, tmp_path)

    with caplog.at_level(logging.DEBUG):
        assert get_providers.provider_throttle(name, DownloadLimitExceeded("fixture quota"),
                                               operation="download", announce=False) is True

    assert name in get_providers.tp
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    throttling = [record for record in caplog.records if "Throttling" in record.getMessage()]
    assert len(throttling) == 1
    assert throttling[0].levelno == logging.DEBUG


def test_nothing_recorded_returns_false(monkeypatch, tmp_path):
    """False is the caller's signal that there is nothing to announce: a
    count-gate wait, a blacklist demand and a pickle cache reset record no
    backoff."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    monkeypatch.setattr(get_providers.time, "sleep", lambda seconds: None)
    # The count gate.
    assert get_providers.provider_throttle(name, TooManyRequests("fixture rate")) is False
    # A blacklist demand.
    blacklisted = []
    monkeypatch.setattr(get_providers, "blacklist_log_movie",
                        lambda *args, **kwargs: blacklisted.append(args))
    error = get_providers.MustGetBlacklisted("fixture-id", "movie")
    ids = {"radarrId": 7, "sonarrSeriesId": None, "sonarrEpisodeId": None, "arr_instance_id": None}
    assert get_providers.provider_throttle(name, error, ids=ids, language=Language("eng")) is False
    assert blacklisted == [(7, name, "fixture-id", "en")]
    # A pickle cache reset. The region's backend is a memoized property that
    # raises while unconfigured, so stand the whole backend in; an instance
    # __dict__ entry shadows it and reading the property is what raises.
    monkeypatch.setitem(get_providers.subliminal_cache_region.__dict__, "backend",
                         SimpleNamespace(all_filenames=[]))
    assert get_providers.provider_throttle(name, ValueError("unsupported pickle protocol 5")) is False
    assert get_providers.tp == {}


def test_the_announce_names_the_deadline_actually_in_force(monkeypatch, tmp_path, caplog):
    """A download announces once, after its retries, reading the entry the
    last attempt left: the deadline the pool will actually enforce."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    error = DownloadLimitExceeded("fixture quota")
    assert get_providers.provider_throttle(name, error, operation="download", announce=False) is True
    _reason, until, description = get_providers.tp[name]

    with caplog.at_level(logging.WARNING):
        get_providers.announce_provider_throttle(name, error, operation="download")

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.exc_info is None
    message = record.getMessage()
    assert f"Provider {name} download failed: download quota reached" in message
    assert "Not using it" in message
    assert description in message
    assert until.strftime("%y/%m/%d %H:%M") in message


@pytest.mark.parametrize("entry", [
    None,
    ("DownloadLimitExceeded", datetime.datetime.now() - datetime.timedelta(minutes=1), "3 hours"),
], ids=["no_entry", "expired_entry"])
def test_the_announce_writes_nothing_without_a_live_entry(monkeypatch, tmp_path, caplog, entry):
    """A download that recorded nothing announces nothing, and an expired
    entry names no deadline in force."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    if entry is not None:
        get_providers.tp[name] = entry

    with caplog.at_level(logging.WARNING):
        get_providers.announce_provider_throttle(name, DownloadLimitExceeded("fixture quota"),
                                                 operation="download")

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


_CONNECTION_ERROR = requests.exceptions.ConnectionError("fixture connection")


def _download_pool(monkeypatch, throttle_callback, throttle_announce, failures=patch_core.DOWNLOAD_TRIES,
                   error=_CONNECTION_ERROR):
    """A real pool over a fake registry, downloading from a provider that
    fails with `error` on its first `failures` attempts and succeeds after
    them."""
    monkeypatch.setattr(patch_core, "DOWNLOAD_RETRY_SLEEP", 0.05)
    attempts = []

    class Provider:
        languages = set()

        def __init__(self, **kwargs):
            pass

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def terminate(self):
            pass

        def download_subtitle(self, subtitle):
            attempts.append(subtitle)
            if len(attempts) <= failures:
                raise error

    monkeypatch.setattr(patch_core, "provider_registry", {"fixture_provider": Provider})
    pool = patch_core.SZProviderPool(["fixture_provider"], {},
                                     throttle_callback=throttle_callback,
                                     throttle_announce=throttle_announce)
    return pool, attempts


class _MinutesPassWhileRetrying:
    """Stand-in for the datetime module: every now() call moves the clock a
    few minutes on, so each attempt's backoff lands in a different minute and
    the deadline the last attempt recorded formats apart from the first's.
    The tz argument the throttle map's midnight resets pass is accepted and
    ignored: only the minutes between attempts matter here."""

    step = datetime.timedelta(minutes=2)

    def __init__(self):
        self._now = datetime.datetime.now()
        outer = self

        class _DateTime:
            @staticmethod
            def now(tz=None):
                outer._now += outer.step
                return outer._now

        self.datetime = _DateTime
        self.timedelta = datetime.timedelta


class _DownloadedSubtitle:
    def __init__(self):
        self.provider_name = "fixture_provider"
        self.language = Language("eng")
        self.sports_context = None

    def is_valid(self):
        return True

    def normalize(self):
        pass


@pytest.mark.parametrize("error,words,description", [
    (_CONNECTION_ERROR, "connection failure", "10 minutes"),
    (requests.exceptions.Timeout("fixture timeout"), "timed out", "1 hour"),
], ids=["connection_error", "requests_timeout"])
def test_a_retried_download_writes_one_line_after_the_last_attempt(monkeypatch, tmp_path, caplog, error, words,
                                                                   description):
    """Every attempt still records its backoff, so the entry after the last
    one is the deadline in force; the one line comes after the retries and
    names it, not the first attempt's. The clock moves several minutes per
    attempt so the two deadlines cannot format alike, and the retry class
    here is the one the handler retries: requests' Timeout, a built-in
    provider's own failure, with the real recording callback."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    monkeypatch.setattr(get_providers, "datetime", _MinutesPassWhileRetrying())
    deadlines = []
    real_throttle = get_providers.provider_throttle

    def recording_throttle(provider, throttle_error, **context):
        recorded = real_throttle(provider, throttle_error, **context)
        deadlines.append(get_providers.tp[provider][1])
        return recorded

    pool, attempts = _download_pool(monkeypatch, recording_throttle, get_providers.announce_provider_throttle,
                                    error=error)

    with caplog.at_level(logging.INFO):
        assert pool.download_subtitle(_DownloadedSubtitle()) is False

    assert len(attempts) == patch_core.DOWNLOAD_TRIES, "the retryable error did not keep its retries"
    assert len(deadlines) == patch_core.DOWNLOAD_TRIES
    first, last = deadlines[0], deadlines[-1]
    assert last > first, "the retries left no time between attempts"
    assert last.strftime("%y/%m/%d %H:%M") != first.strftime("%y/%m/%d %H:%M"), \
        "the stubbed clock did not move the retries across a minute boundary"
    stored_until = get_providers.tp[name][1]
    assert stored_until == last
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, "a retried download wrote more than its one line"
    record = warnings[0]
    assert record.exc_info is None
    message = record.getMessage()
    assert f"Provider {name} download failed: {words}" in message
    assert f"Not using it for {description}" in message
    assert last.strftime("%y/%m/%d %H:%M") in message
    assert first.strftime("%y/%m/%d %H:%M") not in message, "the line named a deadline no longer in force"
    # The one line already says the provider is out; the discard line stays
    # below the default filter.
    assert "Maximum retries" not in caplog.text


def test_a_connection_failure_then_a_count_gated_rate_limit_writes_each_failure_its_own_line(
        monkeypatch, tmp_path, caplog):
    """The mixed sequence writes two WARNING lines, and both belong there:
    the count gate's, for the rate limit it declined to record a backoff for,
    and the download's one announce, for the backoff the connection failure
    did record, naming that failure's exception and the deadline in force.
    Pinned with the real recording callback and the real announce, so the two
    lines are deliberate output rather than an accident of a stand-in."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    monkeypatch.setattr(get_providers.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(patch_core, "DOWNLOAD_RETRY_SLEEP", 0)
    attempts = []

    class Provider:
        languages = set()

        def __init__(self, **kwargs):
            pass

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def terminate(self):
            pass

        def download_subtitle(self, subtitle):
            attempts.append(subtitle)
            raise _CONNECTION_ERROR if len(attempts) == 1 else TooManyRequests("fixture rate")

    monkeypatch.setattr(patch_core, "provider_registry", {"fixture_provider": Provider})
    pool = patch_core.SZProviderPool(["fixture_provider"], {},
                                     throttle_callback=get_providers.provider_throttle,
                                     throttle_announce=get_providers.announce_provider_throttle)

    with caplog.at_level(logging.WARNING):
        assert pool.download_subtitle(_DownloadedSubtitle()) is False

    assert len(attempts) == 2, "the second failure should have ended the retries"
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2
    gate_line, announce_line = warnings
    assert "rate limited (TooManyRequests)" in gate_line
    assert "count 1 of 5" in gate_line and "no backoff" in gate_line
    assert "connection failure (ConnectionError)" in announce_line
    assert "Not using it for 10 minutes" in announce_line
    # The announce names the deadline the connection failure's record left.
    _reason, until, _description = get_providers.tp[name]
    assert until.strftime("%y/%m/%d %H:%M") in announce_line


def test_a_download_that_recovers_still_writes_the_line_once(monkeypatch, tmp_path, caplog):
    """A retry that succeeds leaves the failed attempt's backoff standing, so
    the line is written with that deadline, once."""
    name = _throttle_test_state(monkeypatch, tmp_path)
    pool, attempts = _download_pool(monkeypatch, get_providers.provider_throttle,
                                    get_providers.announce_provider_throttle, failures=1)

    with caplog.at_level(logging.INFO):
        assert pool.download_subtitle(_DownloadedSubtitle()) is True

    assert len(attempts) == 2
    stored_until = get_providers.tp[name][1]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert stored_until.strftime("%y/%m/%d %H:%M") in warnings[0].getMessage()
