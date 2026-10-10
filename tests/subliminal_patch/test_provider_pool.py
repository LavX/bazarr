# -*- coding: utf-8 -*-
"""Guards for SZProviderPool.__getitem__ provider adoption.

A download can name a provider the pool was not built with, so __getitem__
adopts registered providers instead of raising. These tests pin that behaviour
and the ordered-list shape of pool.providers that it relies on.
"""

import logging
from types import SimpleNamespace

import pytest
import requests
from subliminal.exceptions import (AuthenticationError, ConfigurationError, DownloadLimitExceeded,
                                   ServiceUnavailable)
from subliminal_patch.exceptions import (ForbiddenError, IPAddressBlocked, ParseResponseError,
                                         SearchLimitReached, TooManyRequests)
from provider_hub.worker import WorkerError

from subliminal_patch import core


class _FakeProvider:
    languages = set()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initialized = False

    @classmethod
    def check(cls, video):
        return True

    def initialize(self):
        self.initialized = True

    def terminate(self):
        pass


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(
        core, "provider_registry", {"alpha": _FakeProvider, "beta": _FakeProvider}
    )


def test_pool_providers_is_an_ordered_list(registry):
    pool = core.SZProviderPool(["beta", "alpha", "beta"], {})

    assert pool.providers == ["beta", "alpha"]

    pool.update(["alpha", "beta"], {}, [], {"must_contain": [], "must_not_contain": []})

    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_adopts_registered_provider_missing_from_pool(registry):
    pool = core.SZProviderPool(["alpha"], {})

    provider = pool["beta"]

    assert isinstance(provider, _FakeProvider)
    assert provider.initialized
    # appended, not inserted, so the configured priority order survives
    assert pool.providers == ["alpha", "beta"]
    # and it is only initialized once
    assert pool["beta"] is provider
    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_rejects_unregistered_provider(registry):
    pool = core.SZProviderPool(["alpha"], {})

    with pytest.raises(KeyError):
        pool["nosuchprovider"]

    assert pool.providers == ["alpha"]


def test_pool_getitem_never_resurrects_a_discarded_provider(registry):
    # Absence from pool.providers is how the pool encodes a discarded
    # provider; adoption must not undo that.
    pool = core.SZProviderPool(["alpha"], {})
    pool.discarded_providers.add("beta")

    with pytest.raises(KeyError):
        pool["beta"]

    assert pool.providers == ["alpha"]


def test_pool_getitem_honors_adoption_gate_veto(registry):
    # The gate is the caller's enabled-and-not-throttled check: a registered
    # provider the configuration currently excludes must not be adopted.
    pool = core.SZProviderPool(["alpha"], {}, adoption_gate=lambda name: False)

    with pytest.raises(KeyError):
        pool["beta"]

    assert pool.providers == ["alpha"]
    assert "beta" not in pool.initialized_providers


def test_pool_getitem_adopts_when_gate_allows(registry):
    pool = core.SZProviderPool(["alpha"], {}, adoption_gate=lambda name: name == "beta")

    provider = pool["beta"]

    assert isinstance(provider, _FakeProvider)
    assert pool.providers == ["alpha", "beta"]


def test_pool_getitem_gate_not_consulted_for_configured_providers(registry):
    # The gate only guards adoption; providers the pool was built with
    # initialize regardless (get_providers already filtered them).
    calls = []
    pool = core.SZProviderPool(
        ["alpha"], {}, adoption_gate=lambda name: calls.append(name) or False
    )

    assert isinstance(pool["alpha"], _FakeProvider)
    assert calls == []


def test_excluded_download_neither_throttles_nor_discards(registry):
    # A vetoed adoption during download must be a quiet no-op: routing it
    # through the generic handler would call throttle_callback with an
    # unmapped exception, REPLACING an existing long backoff with the
    # 10-minute default.
    throttled = []
    pool = core.SZProviderPool(
        ["alpha"],
        {},
        throttle_callback=lambda name, exc, ids=None, language=None: throttled.append(name),
        adoption_gate=lambda name: False,
    )
    subtitle = type(
        "FakeSubtitle", (), {"provider_name": "beta", "language": None}
    )()

    assert pool.download_subtitle(subtitle) is False
    assert throttled == []
    assert "beta" not in pool.discarded_providers


def test_excluded_search_neither_throttles_nor_discards(registry):
    throttled = []
    pool = core.SZProviderPool(
        ["alpha"],
        {},
        throttle_callback=lambda name, exc, ids=None, language=None: throttled.append(name),
        adoption_gate=lambda name: False,
    )
    video = type("FakeVideo", (), {})()
    language = core.Language("eng")
    _FakeProvider.languages = {language}
    try:
        assert pool.list_subtitles_provider("beta", video, {language}) is None
    finally:
        _FakeProvider.languages = set()
    assert throttled == []
    assert "beta" not in pool.discarded_providers


@pytest.mark.parametrize("sports", [False, True])
@pytest.mark.parametrize("error_type", [core.APIThrottled, OSError])
@pytest.mark.parametrize("callback_kind", ["legacy", "context", "keyword_only", "kwargs", "varargs", "positional_only"])
@pytest.mark.parametrize("operation", ["search", "download"])
def test_provider_errors_keep_legacy_and_context_throttle_callbacks(monkeypatch, sports, error_type, callback_kind, operation):
    from types import SimpleNamespace
    from subzero.language import Language

    language = Language("eng")
    error = error_type("fixture provider error")
    class Provider(_FakeProvider):
        languages = {language}
        def list_subtitles(self, video, languages):
            raise error
        def download_subtitle(self, subtitle):
            raise error
    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []
    def legacy(name, exc, ids=None, language=None):
        calls.append((name, exc, ids, language, None))
    def context(name, exc, ids=None, language=None, sports_context=None):
        calls.append((name, exc, ids, language, sports_context))
    def keyword_only(name, exc, *, ids=None, language=None, sports_context=None):
        calls.append((name, exc, ids, language, sports_context))
    def varargs(name, exc, *sports_context, ids=None, language=None):
        assert sports_context == ()
        calls.append((name, exc, ids, language, None))
    def positional_only(name, exc, sports_context=None, /, ids=None, language=None):
        assert sports_context is None
        calls.append((name, exc, ids, language, None))
    def keywords(name, exc, **kwargs):
        calls.append((name, exc, kwargs["ids"], kwargs["language"], kwargs.get("sports_context")))
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback={
        "legacy": legacy, "context": context, "keyword_only": keyword_only,
        "kwargs": keywords, "varargs": varargs, "positional_only": positional_only}[callback_kind])
    video = SimpleNamespace(sports_context="owned fixture" if sports else None)
    if operation == "search":
        result = pool.list_subtitles_provider("alpha", video, {language}, detailed=True)
        assert result.status != "success"
    else:
        subtitle = SimpleNamespace(provider_name="alpha", language=language, sports_context=video.sports_context)
        assert pool.download_subtitle(subtitle) is False
    assert len(calls) == 1
    assert calls[0][0:2] == ("alpha", error)
    assert calls[0][-1] == ("owned fixture" if sports and callback_kind in ("context", "keyword_only", "kwargs") else None)


@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_provider_failure_names_the_instance_of_its_video(monkeypatch, operation):
    """The id dict tells the throttle callback which item a failure was for,
    and a release a provider demands be excluded is recorded against it. It
    names the item's instance as well, so the exclusion keeps its owner even
    when the item's row is gone by the time it is recorded."""
    from types import SimpleNamespace
    from subzero.language import Language

    language = Language("eng")
    error = core.MustGetBlacklisted("fixture-id", "movie")

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            raise error

        def download_subtitle(self, subtitle):
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=lambda name, exc, ids=None, language=None:
                               calls.append(ids))
    item = dict(radarrId=7, arr_instance_id=4, sports_context=None)
    if operation == "search":
        pool.list_subtitles_provider("alpha", SimpleNamespace(**item), {language}, detailed=True)
    else:
        pool.download_subtitle(SimpleNamespace(provider_name="alpha", language=language, **item))

    assert calls == [{"radarrId": 7, "sonarrSeriesId": None, "sonarrEpisodeId": None,
                      "arr_instance_id": 4}]


def test_a_listed_subtitle_carries_the_instance_of_its_video(monkeypatch):
    """The download of a listed subtitle builds its id dict from the subtitle,
    so the listing copies the instance onto it with the other ids."""
    from types import SimpleNamespace
    from subzero.language import Language

    language = Language("eng")
    listed = SimpleNamespace(id="fixture-sub", language=language, hearing_impaired=False,
                             provider_name="alpha", release_info="")

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            return [listed]

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    pool = core.SZProviderPool(["alpha"], {})
    video = SimpleNamespace(radarrId=7, arr_instance_id=4, sports_context=None, fps=None)

    assert pool.list_subtitles_provider("alpha", video, {language}) == [listed]
    assert (listed.radarrId, listed.arr_instance_id) == (7, 4)


def test_throttle_callback_internal_typeerror_is_not_retried():
    calls = []
    def callback(name, exc, ids=None, language=None):
        calls.append(name)
        raise TypeError("inside callback")
    pool = core.SZProviderPool([], {}, throttle_callback=callback)
    with pytest.raises(TypeError, match="inside callback"):
        pool.throttle_callback("alpha", OSError(), sports_context=object())
    assert calls == ["alpha"]


# A throttle callback runs inside the pool's error handlers. When it raises,
# the provider's own failure must still decide the outcome, the handler's
# remaining bookkeeping must still run, and the callback's failure must be
# logged rather than lost.

class _CallbackBroke(Exception):
    pass


def _raising_callback(calls, error_type=_CallbackBroke):
    def callback(name, exc, ids=None, language=None):
        calls.append((name, exc))
        raise error_type("throttle callback fixture failure")
    return callback


def _callback_failures(caplog):
    return [record for record in caplog.records
            if record.name == core.logger.name and record.getMessage().startswith("Throttle callback failed")]


@pytest.mark.parametrize("error, status", [
    (core.APIThrottled("fixture throttle"), "cooldown"),
    (requests.ConnectionError("fixture unreachable"), "unreachable"),
    (RuntimeError("fixture failure"), "error"),
])
def test_raising_throttle_callback_keeps_the_search_failure(monkeypatch, caplog, error, status):
    from types import SimpleNamespace

    language = core.Language("eng")
    searches = []

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            searches.append(languages)
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []
    # A TypeError inside the callback is the case the adapter must not retry
    # without sports_context, so it doubles as the not-retried check here.
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=_raising_callback(calls, TypeError))
    video = SimpleNamespace(sports_context="owned fixture")

    with caplog.at_level("ERROR", logger=core.logger.name):
        result = pool.list_subtitles_provider("alpha", video, {language}, detailed=True)

    assert (result.provider, result.status, result.subtitles) == ("alpha", status, [])
    assert calls == [("alpha", error)]
    assert len(searches) == 1
    failures = _callback_failures(caplog)
    assert len(failures) == 1
    # The record carries no exc_info, since a formatter would print the
    # messages from it; the callback's exception type is in the text instead.
    assert failures[0].exc_info is None
    assert failures[0].getMessage().rstrip().endswith("TypeError")


def test_raising_throttle_callback_still_discards_the_failed_search_provider(monkeypatch):
    from types import SimpleNamespace

    language = core.Language("eng")

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            raise RuntimeError("fixture failure")

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=_raising_callback(calls))

    assert pool.list_subtitles(SimpleNamespace(), {language}) == []
    assert "alpha" in pool.discarded_providers
    assert len(calls) == 1


@pytest.mark.parametrize("error, attempts, discarded, recorded", [
    (requests.ConnectionError("fixture unreachable"), core.DOWNLOAD_TRIES, True, True),
    (core.rarfile.BadRarFile("fixture archive"), 1, False, False),
    (core.MustGetBlacklisted("fixture-id", "movie"), 1, False, False),
    (RuntimeError("fixture failure"), 1, True, True),
])
def test_raising_throttle_callback_keeps_the_download_outcome(monkeypatch, caplog, error, attempts, discarded,
                                                              recorded):
    from types import SimpleNamespace

    downloads = []

    class Provider(_FakeProvider):
        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)
    calls = []
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=_raising_callback(calls))
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    with caplog.at_level("ERROR", logger=core.logger.name):
        assert pool.download_subtitle(subtitle) is False

    assert len(downloads) == attempts
    assert calls == [("alpha", error)] * attempts
    assert len(_callback_failures(caplog)) == attempts
    assert ("alpha" in pool.discarded_providers) is discarded
    if recorded:
        assert subtitle.download_error is error


# Kept out of the raising lines, which the callback failure log prints, so
# these texts can only reach the log as exception messages.
_CALLBACK_MESSAGE = "callback fixture failure"
_INNER_MESSAGE = "inner fixture failure"


def _raise_plain(exc):
    raise _CallbackBroke(_CALLBACK_MESSAGE)


def _raise_while_handling_its_own_error(exc):
    try:
        raise KeyError(_INNER_MESSAGE)
    except KeyError:
        raise _CallbackBroke(_CALLBACK_MESSAGE)


def _raise_from_its_own_error(exc):
    # The shape of a database error wrapped by its driver layer.
    try:
        raise KeyError(_INNER_MESSAGE)
    except KeyError as inner:
        raise _CallbackBroke(_CALLBACK_MESSAGE) from inner


def _raise_from_the_provider_error(exc):
    raise _CallbackBroke(_CALLBACK_MESSAGE) from exc


def _raise_with_the_provider_message(exc):
    raise _CallbackBroke(f"recording failed: {exc}")


def _raise_with_the_provider_message_in_a_note(exc):
    callback_error = _CallbackBroke(_CALLBACK_MESSAGE)
    callback_error.add_note(f"while recording {exc}")
    raise callback_error


def _raise_a_group_holding_the_provider_error(exc):
    raise ExceptionGroup(_CALLBACK_MESSAGE, [exc])


def _reraise_the_provider_error(exc):
    raise exc


@pytest.mark.parametrize("raise_from_callback, logged_types", [
    (_raise_plain, ["_CallbackBroke"]),
    (_raise_while_handling_its_own_error, ["KeyError", "_CallbackBroke"]),
    (_raise_from_its_own_error, ["KeyError", "_CallbackBroke"]),
    (_raise_from_the_provider_error, ["_CallbackBroke"]),
    (_raise_with_the_provider_message, ["_CallbackBroke"]),
    (_raise_with_the_provider_message_in_a_note, ["_CallbackBroke"]),
    (_raise_a_group_holding_the_provider_error, ["ExceptionGroup"]),
    (_reraise_the_provider_error, []),
])
def test_raising_throttle_callback_log_leaves_out_the_provider_message(monkeypatch, caplog, raise_from_callback,
                                                                        logged_types):
    from types import SimpleNamespace

    # The connection error handler names only the provider, so the callback
    # failure record is the one place this message could reach the log.
    error = requests.ConnectionError("https://example.invalid/sub?token=fixture-secret")

    class Provider(_FakeProvider):
        def download_subtitle(self, subtitle):
            raise error

    def callback(name, exc, ids=None, language=None):
        raise_from_callback(exc)

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    monkeypatch.setattr(core, "DOWNLOAD_TRIES", 1)
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=callback)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    with caplog.at_level("ERROR", logger=core.logger.name):
        assert pool.download_subtitle(subtitle) is False

    failures = _callback_failures(caplog)
    assert len(failures) == 1
    assert failures[0].exc_info is None
    assert "fixture-secret" not in caplog.text
    # A callback can copy the provider message into its own, so no message
    # in the callback's chain is logged; its types and raise sites are.
    assert _CALLBACK_MESSAGE not in caplog.text
    assert _INNER_MESSAGE not in caplog.text
    logged = failures[0].getMessage()
    assert "ConnectionError" in logged
    for name in logged_types:
        assert name in logged
    if logged_types:
        assert f"in {raise_from_callback.__name__}" in logged
    # The provider error the pool keeps is left as it was raised.
    assert subtitle.download_error is error
    assert str(error) == "https://example.invalid/sub?token=fixture-secret"


@pytest.mark.parametrize("error", [
    requests.Timeout("fixture timeout"),
    RuntimeError("fixture teardown failure"),
])
def test_raising_throttle_callback_still_drops_the_torn_down_provider(monkeypatch, caplog, error):
    terminated = []

    class Provider(_FakeProvider):
        def terminate(self):
            terminated.append(self)
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    calls = []
    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=_raising_callback(calls))
    pool["alpha"]

    with caplog.at_level("ERROR", logger=core.logger.name):
        del pool["alpha"]

    assert "alpha" not in pool.initialized_providers
    assert len(terminated) == 1
    assert calls == [("alpha", error)]
    assert len(_callback_failures(caplog)) == 1


def test_update_terminates_every_removed_provider_when_a_teardown_callback_raises(monkeypatch):
    terminated = []

    class Provider(_FakeProvider):
        def terminate(self):
            terminated.append(self)
            raise RuntimeError("fixture teardown failure")

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider, "beta": Provider, "gamma": Provider})
    calls = []
    pool = core.SZProviderPool(["alpha", "beta", "gamma"], {"gamma": {"token": "old"}},
                               throttle_callback=_raising_callback(calls))
    removed = {pool["alpha"], pool["beta"]}

    pool.update(["gamma"], {"gamma": {"token": "new"}}, [("gamma", "fixture-id")],
                {"must_contain": [], "must_not_contain": []})

    assert set(terminated) == removed
    assert sorted(name for name, _ in calls) == ["alpha", "beta"]
    assert pool.providers == ["gamma"]
    # The rest of update() still ran after the teardowns: gamma's config
    # change restarted it and the new blacklist is in place.
    assert list(pool.initialized_providers) == ["gamma"]
    assert pool.initialized_providers["gamma"].kwargs == {"token": "new"}
    assert ("gamma", "fixture-id") in pool.blacklist


def test_raising_throttle_callback_on_config_restart_keeps_restarting(monkeypatch, caplog):
    init_error = ConnectionError("fixture restart failure")

    class Provider(_FakeProvider):
        def initialize(self):
            if self.kwargs.get("token") == "broken":
                raise init_error
            super().initialize()

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider, "beta": Provider})
    calls = []
    pool = core.SZProviderPool(["alpha", "beta"], {"alpha": {"token": "old"}, "beta": {"token": "old"}},
                               throttle_callback=_raising_callback(calls))
    pool["alpha"]
    old_beta = pool["beta"]

    with caplog.at_level("ERROR", logger=core.logger.name):
        pool.provider_configs.update({"alpha": {"token": "broken"}, "beta": {"token": "new"}})

    assert calls == [("alpha", init_error)]
    assert len(_callback_failures(caplog)) == 1
    assert "alpha" not in pool.initialized_providers
    assert pool.provider_configs["alpha"] == {"token": "broken"}
    # beta comes after the failing restart and is still replaced.
    assert pool.initialized_providers["beta"] is not old_beta
    assert pool.initialized_providers["beta"].kwargs == {"token": "new"}


def test_teardown_drops_the_provider_even_when_terminate_aborts(monkeypatch):
    # Anything escaping terminate(), not only what the handlers catch, must
    # still leave the pool without the instance it was deleting.
    class _Abort(BaseException):
        pass

    class Provider(_FakeProvider):
        def terminate(self):
            raise _Abort()

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    pool = core.SZProviderPool(["alpha"], {})
    pool["alpha"]

    with pytest.raises(_Abort):
        del pool["alpha"]

    assert "alpha" not in pool.initialized_providers


# A provider failure in the pool carries its traceback on the log record,
# exactly once: the message stays a single clean line, and the traceback text
# reaches the log through the record, where the file sinks' redaction sees it.

def _pool_error_records(caplog):
    return [record for record in caplog.records
            if record.name == core.logger.name and record.levelname == "ERROR"]


def test_a_language_reverse_failure_records_its_traceback_once(monkeypatch, caplog):
    """The LanguageReverseError handler used to paste format_exc() into a
    logger.exception call, which already appends the traceback, so the log
    wrote it twice."""
    from types import SimpleNamespace

    pool = core.SZProviderPool(["alpha"], {})

    def reversing(provider, video, languages, detailed=False, discard_on_failure=False):
        raise core.LanguageReverseError("fixture reverse failure")

    monkeypatch.setattr(pool, "list_subtitles_provider", reversing)

    with caplog.at_level("ERROR", logger=core.logger.name):
        assert pool.list_subtitles(SimpleNamespace(), {core.Language("eng")}) == []

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    # One record, one traceback, and the message carries neither.
    assert record.getMessage() == "Unexpected language reverse error in alpha, skipping"
    assert "\n" not in record.getMessage()
    assert "Traceback" not in record.getMessage()
    assert record.exc_info and record.exc_info[0] is core.LanguageReverseError
    assert "fixture reverse failure" in record.exc_text
    assert caplog.text.count("Traceback (most recent call last)") == 1


class _MatchComputationFails:
    """A listed candidate whose match computation fails; the handler logs it
    and the search moves on to the next candidate."""

    def __init__(self):
        self.id = "fixture-sub"
        self.language = core.Language("eng")
        self.hearing_impaired = False
        self.provider_name = "alpha"
        self.release_info = ""

    def get_matches(self, video):
        raise AttributeError("fixture match failure")


@pytest.mark.parametrize("path", ["prioritized_listing", "scoring"])
def test_a_match_computation_failure_records_its_traceback_once(monkeypatch, caplog, path):
    """The "Match computation failed" handlers used to bury format_exc() in
    an error message instead of attaching the traceback to the record."""
    from types import SimpleNamespace

    broken = _MatchComputationFails()
    language = broken.language

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            return [broken]

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    pool = core.SZProviderPool(["alpha"], {})
    video = SimpleNamespace(radarrId=7, arr_instance_id=4, sports_context=None, fps=None)

    with caplog.at_level("ERROR", logger=core.logger.name):
        if path == "prioritized_listing":
            assert pool.list_subtitles_prioritized(video, {language}) == [broken]
        else:
            assert core.SZProviderPool._score_subtitles([broken], video, {language}, "no") == []

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    # One record, one traceback, and the message is a single clean line.
    message = record.getMessage()
    assert message.endswith(": Match computation failed")
    assert "\n" not in message
    assert "Traceback" not in message
    assert record.exc_info and record.exc_info[0] is AttributeError
    assert "fixture match failure" in record.exc_text
    assert caplog.text.count("Traceback (most recent call last)") == 1


# The pool rechecks a provider it already holds just before it calls it, so a
# provider that went on backoff after the search started is not called again.

class _Candidate:
    hash_verifiable = False
    hearing_impaired = False
    hearing_impaired_verifiable = False
    release_info = None

    def __init__(self, provider_name, sub_id):
        self.provider_name = provider_name
        self.id = sub_id
        self.language = core.Language("eng")

    def get_matches(self, video):
        return {"series", "year", "season", "episode"}

    def is_valid(self):
        return True

    def normalize(self):
        pass


def _raising(error):
    def call(*args, **kwargs):
        raise error
    return call


def _gated_pool(monkeypatch, download=None, usable=None):
    """Two held providers, alpha and beta, recording every call they get."""
    calls = []

    class Provider(_FakeProvider):
        languages = {core.Language("eng")}

        def list_subtitles(self, video, languages):
            calls.append(("list", self.name))
            return []

        def download_subtitle(self, subtitle):
            calls.append(("download", subtitle.id))
            if download is not None and subtitle.provider_name == "alpha":
                raise download

    class Alpha(Provider):
        name = "alpha"

    class Beta(Provider):
        name = "beta"

    monkeypatch.setattr(core, "provider_registry", {"alpha": Alpha, "beta": Beta})
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)
    throttled = []
    usable = usable if usable is not None else {"alpha": True, "beta": True}
    pool = core.SZProviderPool(
        ["alpha", "beta"], {},
        throttle_callback=lambda name, error, **context: throttled.append(name),
        adoption_gate=lambda name: usable[name])
    pool["alpha"], pool["beta"]
    return pool, calls, throttled, usable


def test_a_held_provider_on_backoff_is_neither_searched_nor_downloaded_from(monkeypatch):
    pool, calls, throttled, usable = _gated_pool(monkeypatch)
    usable["alpha"] = False
    language = core.Language("eng")
    subtitle = _Candidate("alpha", "a1")

    assert pool.list_subtitles_provider("alpha", object(), {language}) is None
    detailed = pool.list_subtitles_provider("alpha", object(), {language}, detailed=True)
    assert (detailed.status, detailed.reason) == ("skipped", "provider_excluded")
    assert pool.download_subtitle(subtitle) is False
    assert subtitle.skip_reason == "provider_excluded"
    assert calls == []
    assert throttled == []


@pytest.mark.parametrize("busy", [True, False])
def test_a_busy_or_excluded_provider_costs_the_round_one_attempt(monkeypatch, caplog, busy):
    pool, calls, throttled, usable = _gated_pool(
        monkeypatch, download=core.ProviderBusyError("alpha") if busy else None)
    if not busy:
        usable["alpha"] = False
    video = core.Episode("/m/Show.S01E01.mkv", "Show", 1, 1)
    candidates = [_Candidate("alpha", f"a{n}") for n in (1, 2, 3)] + [_Candidate("beta", "b1")]

    with caplog.at_level("INFO", logger=core.logger.name):
        downloaded = pool.download_best_subtitles(candidates, video, {core.Language("eng")}, min_score=1)

    assert [subtitle.id for subtitle in downloaded] == ["b1"]
    assert calls == ([("download", "a1")] if busy else []) + [("download", "b1")]
    assert "alpha" not in pool.discarded_providers
    assert throttled == []
    assert len([r for r in caplog.records if "remaining candidates of provider 'alpha'" in r.getMessage()]) == 1


@pytest.mark.parametrize("error, listed", [
    (core.ProviderBusyError("alpha"), []),
    (core.ProviderExcludedWhileQueuedError("alpha"), None),
])
def test_a_refused_turn_is_not_a_provider_failure(monkeypatch, error, listed):
    pool, calls, throttled, usable = _gated_pool(monkeypatch)
    pool.initialized_providers["alpha"].list_subtitles = _raising(error)
    language = core.Language("eng")

    assert pool.list_subtitles_provider("alpha", object(), {language}, discard_on_failure=True) == listed
    detailed = pool.list_subtitles_provider("alpha", object(), {language}, detailed=True)
    assert detailed.status == "skipped"
    assert detailed.reason == ("provider_busy" if listed == [] else "provider_excluded")
    assert throttled == []
    # Excluded means discarded for the pool, like any exclusion; busy does not.
    assert ("alpha" in pool.discarded_providers) is (listed is None)


@pytest.mark.parametrize("operation", ["search", "download"])
def test_the_outcome_is_reported_after_the_throttle_and_the_discard(monkeypatch, operation):
    pool, calls, throttled, usable = _gated_pool(monkeypatch, download=RuntimeError("fixture failure"))
    events = []
    provider = pool.initialized_providers["alpha"]
    provider.list_subtitles = _raising(RuntimeError("fixture failure"))
    provider.outcome_recorded = lambda: events.append(("recorded", list(throttled),
                                                       "alpha" in pool.discarded_providers))

    if operation == "search":
        pool.list_subtitles(object(), {core.Language("eng")})
    else:
        pool.download_subtitle(_Candidate("alpha", "a1"))

    assert events == [("recorded", ["alpha"], True)]


def test_a_provider_rebuilt_by_a_config_change_stays_gated(monkeypatch):
    pool, calls, throttled, usable = _gated_pool(monkeypatch)
    pool.provider_configs["alpha"] = {"option": "old"}
    old = pool.initialized_providers["alpha"]

    pool.provider_configs.update({"alpha": {"option": "new"}})
    rebuilt = pool.initialized_providers["alpha"]

    assert rebuilt is not old
    assert rebuilt.admit() is True
    usable["alpha"] = False
    assert rebuilt.admit() is False


def test_a_connection_error_that_records_a_backoff_keeps_its_retries(monkeypatch):
    pool, calls, throttled, usable = _gated_pool(monkeypatch, download=requests.ConnectionError("fixture"))
    pool.throttle_callback = lambda name, error, **context: usable.update({name: False})

    assert pool.download_subtitle(_Candidate("alpha", "a1")) is False
    assert calls == [("download", "a1")] * core.DOWNLOAD_TRIES


def test_a_broken_admission_check_lets_the_call_through(monkeypatch, caplog):
    pool, calls, throttled, usable = _gated_pool(monkeypatch)
    usable.clear()

    with caplog.at_level("WARNING", logger=core.logger.name):
        assert pool.download_subtitle(_Candidate("alpha", "a1")) is True

    assert calls == [("download", "a1")]
    assert len([r for r in caplog.records if "Admission check" in r.getMessage()]) == 1


def test_a_detailed_search_skips_a_held_provider_the_pool_discarded(monkeypatch):
    # Discover fans out to both providers with detailed results. A provider
    # the pool holds but another search discarded while this one was running
    # is skipped, not called: the recheck covers held providers, which the
    # adoption path only ever looked at once, when it first admitted them.
    pool, calls, throttled, usable = _gated_pool(monkeypatch)
    pool.discarded_providers.add("alpha")
    language = core.Language("eng")
    video = core.Episode("/m/Show.S01E01.mkv", "Show", 1, 1)

    alpha = pool.list_subtitles_provider("alpha", video, {language}, detailed=True)
    beta = pool.list_subtitles_provider("beta", video, {language}, detailed=True)

    assert (alpha.status, alpha.reason) == ("skipped", "provider_excluded")
    assert (beta.status, beta.reason) == ("empty", None)
    assert pool.list_subtitles_provider("alpha", video, {language}) is None
    assert calls == [("list", "beta")]
    assert throttled == []


# Expected provider conditions are routine operation, not defects: the
# handlers log nothing of their own for them, and the throttle callback
# writes the one line where the backoff is decided. Everything else keeps an
# ERROR record and a traceback, exactly once, and a plugin's unexpected
# exception carries the plugin's own traceback from the worker envelope
# rather than the host transport's.


def _plugin_error(remote, message="plugin fixture failure", **fields):
    return WorkerError(message, remote_class_name=remote, code="provider", **fields)


# One row per expected condition, in its built-in form and in the form a
# Provider Hub plugin's failure arrives in: a rebuilt class, or a WorkerError
# carrying the remote class name for everything the boundary does not rebuild.
_EXPECTED_SEARCH_FAILURES = [
    DownloadLimitExceeded("fixture quota"),
    TooManyRequests("fixture rate"),
    core.APIThrottled("fixture throttle"),
    ServiceUnavailable("fixture outage"),
    AuthenticationError("fixture auth"),
    ConfigurationError("fixture config"),
    requests.exceptions.Timeout("fixture timeout"),
    TimeoutError("fixture socket"),
    requests.exceptions.ConnectionError("fixture connection"),
    IPAddressBlocked("fixture blocked address"),
    SearchLimitReached("fixture search limit"),
    ForbiddenError("fixture refused"),
    _plugin_error("DownloadLimitExceeded"),
    _plugin_error("PermissionError"),
    _plugin_error("IPAddressBlocked"),
    _plugin_error("SearchLimitReached"),
    _plugin_error("ForbiddenError"),
    _plugin_error("CloudflareBlockedError"),
    _plugin_error("URLError"),
    _plugin_error("TimeoutError"),
    _plugin_error("Timeout"),
    _plugin_error("ConnectTimeout"),
    _plugin_error("ReadTimeout"),
    _plugin_error("ConnectionError"),
    WorkerError("fixture deadline", code="timeout"),
]


def _loud_records(caplog):
    return [record for record in caplog.records
            if record.name == core.logger.name and record.levelno >= logging.WARNING]


def _searching_provider(monkeypatch, error):
    """Register a provider whose search raises error. Returns the language to
    ask it for."""
    language = core.Language("eng")

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    return language


def _failing_provider(monkeypatch, error):
    """Register a provider that raises error on both operations. Returns the
    language to ask it for, and the list its downloads are counted in."""
    language = core.Language("eng")
    downloads = []

    class Provider(_FakeProvider):
        languages = {language}

        def list_subtitles(self, video, languages):
            raise error

        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    return language, downloads


def _run_operation(pool, operation, language):
    if operation == "search":
        return pool.list_subtitles_provider("alpha", SimpleNamespace(), {language})
    return pool.download_subtitle(SimpleNamespace(provider_name="alpha", language=language,
                                                   sports_context=None))


@pytest.mark.parametrize("error", _EXPECTED_SEARCH_FAILURES,
                         ids=lambda error: type(error).__name__)
def test_an_expected_search_failure_logs_no_line_of_its_own(monkeypatch, caplog, error):
    language = _searching_provider(monkeypatch, error)
    calls = []
    pool = core.SZProviderPool(["alpha"], {},
                               throttle_callback=lambda name, exc, **context: calls.append(name) or True)

    with caplog.at_level("DEBUG", logger=core.logger.name):
        detailed = pool.list_subtitles_provider("alpha", SimpleNamespace(), {language}, detailed=True,
                                                discard_on_failure=True)

    assert calls == ["alpha"]
    assert (detailed.provider, detailed.subtitles) == ("alpha", [])
    assert _loud_records(caplog) == []
    assert "Unexpected error" not in caplog.text


def test_an_api_throttled_search_logs_no_line_of_its_own(monkeypatch, caplog):
    """APIThrottled has its own handler; its WARNING line would be a second
    record for the one event, so the one line is the callback's alone."""
    _searching_provider(monkeypatch, core.APIThrottled("fixture throttle"))
    calls = []
    pool = core.SZProviderPool(["alpha"], {},
                               throttle_callback=lambda name, exc, **context: calls.append(name) or True)
    language = core.Language("eng")

    with caplog.at_level("DEBUG", logger=core.logger.name):
        assert pool.list_subtitles_provider("alpha", SimpleNamespace(), {language}) is None

    assert calls == ["alpha"]
    assert _loud_records(caplog) == []
    assert "throttled" not in caplog.text


# The download handler retries only the connection tuple; every other
# expected failure is one attempt and a discard.
_GENERIC_DOWNLOAD_FAILURES = [
    DownloadLimitExceeded("fixture quota"),
    TooManyRequests("fixture rate"),
    core.APIThrottled("fixture throttle"),
    ServiceUnavailable("fixture outage"),
    AuthenticationError("fixture auth"),
    ConfigurationError("fixture config"),
    IPAddressBlocked("fixture blocked address"),
    SearchLimitReached("fixture search limit"),
    ForbiddenError("fixture refused"),
    _plugin_error("DownloadLimitExceeded"),
    _plugin_error("PermissionError"),
    _plugin_error("IPAddressBlocked"),
    _plugin_error("SearchLimitReached"),
    _plugin_error("ForbiddenError"),
    _plugin_error("CloudflareBlockedError"),
    _plugin_error("TimeoutError"),
    _plugin_error("URLError"),
    _plugin_error("Timeout"),
    _plugin_error("ConnectTimeout"),
    _plugin_error("ReadTimeout"),
    _plugin_error("ConnectionError"),
    WorkerError("fixture deadline", code="timeout"),
]


def _downloading_provider(monkeypatch, error):
    downloads = []

    class Provider(_FakeProvider):
        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    return downloads


@pytest.mark.parametrize("error", _GENERIC_DOWNLOAD_FAILURES,
                         ids=lambda error: type(error).__name__)
def test_an_expected_download_failure_logs_no_line_of_its_own(monkeypatch, caplog, error):
    downloads = _downloading_provider(monkeypatch, error)
    calls = []
    pool = core.SZProviderPool(["alpha"], {},
                               throttle_callback=lambda name, exc, **context: calls.append(name) or True)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    with caplog.at_level("DEBUG", logger=core.logger.name):
        assert pool.download_subtitle(subtitle) is False

    assert len(downloads) == 1
    assert calls == ["alpha"]
    assert "alpha" in pool.discarded_providers
    assert subtitle.download_error is error
    assert _loud_records(caplog) == []
    assert "Unexpected error" not in caplog.text


def _announcing_pool(monkeypatch, error, callback):
    """A pool recording every throttle callback and announce call, in order."""
    downloads = _downloading_provider(monkeypatch, error)
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)
    events = []
    pool = core.SZProviderPool(
        ["alpha"], {},
        throttle_callback=lambda name, exc, **context: events.append(("throttle", context.get("announce")))
        or callback(),
        throttle_announce=lambda name, exc, operation=None: events.append(("announce", name, exc, operation)))
    return pool, downloads, events


def test_a_retried_connection_error_announces_once_after_the_retries(monkeypatch):
    error = requests.exceptions.ConnectionError("fixture connection")
    pool, downloads, events = _announcing_pool(monkeypatch, error, lambda: True)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    assert pool.download_subtitle(subtitle) is False
    assert len(downloads) == core.DOWNLOAD_TRIES
    # Every attempt still records its backoff, so the backoff is unchanged;
    # only the announce, with the last exception, is new.
    assert events == [("throttle", False)] * core.DOWNLOAD_TRIES + \
                     [("announce", "alpha", error, "download")]


def test_a_download_that_recovers_still_announces_the_backoff_in_force(monkeypatch):
    """A retry that succeeds leaves the failed attempts' backoff standing, so
    the one line is written with the deadline those attempts recorded."""
    error = requests.exceptions.ConnectionError("fixture connection")
    downloads = []
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)

    class Provider(_FakeProvider):
        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            if len(downloads) == 1:
                raise error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    events = []
    pool = core.SZProviderPool(
        ["alpha"], {},
        throttle_callback=lambda name, exc, **context: events.append(("throttle", context.get("announce"))) or True,
        throttle_announce=lambda name, exc, operation=None: events.append(("announce", name, exc, operation)))
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None,
                               is_valid=lambda: True, normalize=lambda: None)

    assert pool.download_subtitle(subtitle) is True
    assert len(downloads) == 2
    assert events == [("throttle", False), ("announce", "alpha", error, "download")]


@pytest.mark.parametrize("callback_result", [None, False])
def test_a_download_that_records_nothing_announces_nothing(monkeypatch, callback_result):
    """The announce reads the deadline the download's own attempts recorded,
    so a callback that recorded nothing leaves nothing to announce even when
    the table has an older entry for the provider."""
    pool, _downloads, events = _announcing_pool(monkeypatch, requests.exceptions.ConnectionError("fixture"),
                                                 lambda: callback_result)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    assert pool.download_subtitle(subtitle) is False
    assert events == [("throttle", False)] * core.DOWNLOAD_TRIES


def test_a_throttle_callback_that_raises_records_nothing_to_announce(monkeypatch):
    error = requests.exceptions.ConnectionError("fixture connection")

    def callback():
        raise _CallbackBroke("throttle callback fixture failure")

    pool, _downloads, events = _announcing_pool(monkeypatch, error, callback)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    assert pool.download_subtitle(subtitle) is False
    assert [event for event in events if event[0] == "announce"] == []


@pytest.mark.parametrize("second_error", [
    core.MustGetBlacklisted("fixture-id", "movie"),
    TooManyRequests("fixture rate"),
], ids=["blacklist_demand", "count_gated_rate_limit"])
def test_the_announce_names_the_exception_that_recorded_the_backoff(monkeypatch, second_error):
    """An attempt whose callback recorded nothing must not have its exception
    announced as the backoff an earlier attempt recorded: the words and the
    class in the line belong to the exception whose call recorded, or the
    line would mix one attempt's reason with another's duration."""
    first_error = requests.exceptions.ConnectionError("fixture connection")
    downloads = []
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)

    class Provider(_FakeProvider):
        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            raise first_error if len(downloads) == 1 else second_error

    monkeypatch.setattr(core, "provider_registry", {"alpha": Provider})
    events = []

    def callback(name, exc, **context):
        events.append(("throttle", exc))
        return exc is first_error

    pool = core.SZProviderPool(
        ["alpha"], {},
        throttle_callback=callback,
        throttle_announce=lambda name, exc, operation=None: events.append(("announce", name, exc)))
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    assert pool.download_subtitle(subtitle) is False
    assert len(downloads) == 2, "the second failure should have ended the retries"
    assert [event for event in events if event[0] == "announce"] == [("announce", "alpha", first_error)]


def test_a_broken_announce_callback_does_not_take_the_download_down(monkeypatch, caplog):
    error = requests.exceptions.ConnectionError("fixture connection")
    downloads = _downloading_provider(monkeypatch, error)
    monkeypatch.setattr(core, "DOWNLOAD_RETRY_SLEEP", 0)

    def broken_announce(name, exc, operation=None):
        raise _CallbackBroke("announce fixture failure")

    pool = core.SZProviderPool(["alpha"], {},
                               throttle_callback=lambda name, exc, **context: True,
                               throttle_announce=broken_announce)
    subtitle = SimpleNamespace(provider_name="alpha", language=core.Language("eng"), sports_context=None)

    with caplog.at_level("ERROR", logger=core.logger.name):
        assert pool.download_subtitle(subtitle) is False

    assert len(downloads) == core.DOWNLOAD_TRIES
    failures = [record for record in caplog.records
                if record.getMessage().startswith("Throttle announce failed")]
    assert len(failures) == 1


@pytest.mark.parametrize("operation", ["search", "download"])
def test_the_handler_tells_the_callback_which_operation_failed(monkeypatch, operation):
    """A modern callback learns the operation and whether it may announce, so
    it can write the one line for the path that failed."""
    error = core.APIThrottled("fixture throttle")
    language, _downloads = _failing_provider(monkeypatch, error)
    received = []

    def callback(name, exc, ids=None, language=None, sports_context=None, operation=None, announce=None):
        received.append((operation, announce))
        return True

    pool = core.SZProviderPool(["alpha"], {}, throttle_callback=callback)
    _run_operation(pool, operation, language)

    assert received == [("search", True) if operation == "search" else ("download", False)]


def test_the_legacy_adapter_forwards_only_the_keywords_the_caller_passed():
    """A callback's own defaults must survive a caller that omits a keyword:
    the adapter forwards what it was handed, never an explicit None that
    would override the default."""
    seen = []

    def callback(name, exc, operation="search", announce=True):
        seen.append((name, operation, announce))
        return announce

    adapted = core._adapt_throttle_callback(callback)

    # The callback takes operation and announce but not sports_context, so it
    # is wrapped; an empty call leaves both defaults alone.
    assert adapted("alpha", ValueError("fixture")) is True
    assert seen == [("alpha", "search", True)]
    assert adapted("alpha", ValueError("fixture"), announce=False) is False
    assert seen[-1] == ("alpha", "search", False)
    assert adapted("alpha", ValueError("fixture"), operation="download") is True
    assert seen[-1] == ("alpha", "download", True)


_PLUGIN_TRACEBACK = ('Traceback (most recent call last):\n'
                     '  File "provider.py", line 3, in search\n'
                     '    raise KeyError("fixture plugin defect")\n'
                     "KeyError: 'fixture plugin defect'\n")

# The remote class, the message its failure carries, and the envelope fields.
# Constructed per test, so a run against a worker boundary without the fields
# reports the failing test, not a broken collection. The PermissionError rows
# are the two operating-system forms of the class, whose errno the envelope
# carries back: the split the classifier draws is between them and a plugin
# raising the class with a message of its own, never an errno, for a refused
# login.
_PLUGIN_UNEXPECTED_FAILURES = [
    ("KeyError", "plugin fixture failure", {"remote_traceback": _PLUGIN_TRACEBACK}),
    ("RuntimeError", "plugin fixture failure", {"remote_traceback": _PLUGIN_TRACEBACK}),
    ("BSPlayerServiceError", "plugin fixture failure", {"remote_traceback": _PLUGIN_TRACEBACK}),
    ("PermissionError", "[Errno 13] Permission denied",
     {"remote_errno": 13, "remote_traceback": _PLUGIN_TRACEBACK}),
    ("PermissionError", "[WinError 5] Access is denied",
     {"remote_errno": 5, "remote_traceback": _PLUGIN_TRACEBACK}),
]


@pytest.mark.parametrize("remote_name,message,fields", _PLUGIN_UNEXPECTED_FAILURES,
                         ids=["KeyError", "RuntimeError", "BSPlayerServiceError", "PermissionError-errno-13",
                              "PermissionError-winerror-5"])
@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_plugins_unexpected_failure_carries_the_plugins_traceback(monkeypatch, caplog, operation,
                                                                    remote_name, message, fields):
    """The host's own traceback ends in the transport for every plugin and
    every cause, so the record carries the plugin's instead, and no exc_info:
    one line, naming the plugin's class, message and frame."""
    error = _plugin_error(remote_name, message, **fields)
    language, downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    assert len(downloads) == (0 if operation == "search" else 1)
    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    assert record.exc_info is None
    record_message = record.getMessage()
    assert "\n" not in record_message
    assert record_message == (f"Unexpected error in provider 'alpha': {error.remote_class_name}: "
                              f"{message}. Plugin traceback: {_PLUGIN_TRACEBACK!r}")
    assert "provider.py" in record_message
    assert "_raise_worker_error" not in caplog.text


@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_plugin_failure_without_a_traceback_field_names_class_and_message(monkeypatch, caplog, operation):
    """An older worker, or a transport that lost the field, still gets its one
    ERROR record; there is simply no traceback to carry."""
    error = _plugin_error("KeyError")
    language, _downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    assert failures[0].exc_info is None
    assert failures[0].getMessage() == "Unexpected error in provider 'alpha': KeyError: plugin fixture failure"
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("fields", [
    {"remote_traceback": _PLUGIN_TRACEBACK},
    {},
], ids=["with_traceback", "without_traceback"])
@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_multiline_plugin_message_stays_one_log_line(monkeypatch, caplog, operation, fields):
    """A plugin supplies its class name and its message, and with no exc_info
    there is no formatter standing between the record and the file, so an
    embedded newline would split the one line across several physical ones.
    Both fields reach the log escaped."""
    error = _plugin_error("Key\nError", "first line\nsecond line\r\nthird line", **fields)
    language, _downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    message = failures[0].getMessage()
    assert "\n" not in message
    assert "\r" not in message
    assert "Key\\nError: first line\\nsecond line\\r\\nthird line" in message


@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_transport_failure_keeps_the_host_traceback(monkeypatch, caplog, operation):
    """A WorkerError without the provider code is Bazarr's own transport
    failing, so the host traceback is the informative one."""
    error = WorkerError("malformed JSON")
    language, _downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    assert record.getMessage() == "Unexpected error in provider 'alpha'"
    assert record.exc_info and record.exc_info[0] is WorkerError
    assert caplog.text.count("Traceback (most recent call last)") == 1


@pytest.mark.parametrize("operation", ["search", "download"])
def test_a_rebuilt_class_keeps_the_plugins_traceback(monkeypatch, caplog, operation):
    """A class the worker rebuilds arrives as the host class with the
    WorkerError kept as its __cause__. When that class is unexpected, the
    plugin's own traceback is still the informative one, so the record takes
    it from the cause and attaches no host exc_info."""
    error = ParseResponseError("plugin fixture failure")
    error.__cause__ = _plugin_error("ParseResponseError", remote_traceback=_PLUGIN_TRACEBACK)
    language, _downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    assert record.exc_info is None
    message = record.getMessage()
    assert message == (f"Unexpected error in provider 'alpha': ParseResponseError: plugin fixture failure. "
                       f"Plugin traceback: {_PLUGIN_TRACEBACK!r}")
    assert "provider.py" in message


@pytest.mark.parametrize("error", [
    KeyError("fixture defect"),
    ParseResponseError("fixture markup change"),
    BrokenPipeError("fixture pipe"),
], ids=lambda error: type(error).__name__)
@pytest.mark.parametrize("operation", ["search", "download"])
def test_an_unexpected_failure_keeps_one_error_record_with_one_traceback(monkeypatch, caplog, operation, error):
    """A genuinely unexpected exception is a defect worth reading: one ERROR
    record, the traceback attached once through exc_info, and never pasted
    into the message a second time. The built-in ConnectionError subclasses
    (a broken pipe, a reset or an aborted connection) are on this side too:
    Bazarr's own code can raise them, and classifying them as a routine
    provider connection failure would cost them their traceback."""
    language, _downloads = _failing_provider(monkeypatch, error)
    pool = core.SZProviderPool(["alpha"], {})

    with caplog.at_level("ERROR", logger=core.logger.name):
        _run_operation(pool, operation, language)

    failures = _pool_error_records(caplog)
    assert len(failures) == 1
    record = failures[0]
    assert record.getMessage() == "Unexpected error in provider 'alpha'"
    assert record.exc_info and record.exc_info[0] is type(error)
    assert "Traceback" not in record.getMessage()
    assert caplog.text.count("Traceback (most recent call last)") == 1
