import pytest
from compat import service


def test_guessit_returns_native_dict():
    r = service.guessit_filename("The.Matrix.1999.1080p.BluRay.x264.mkv")
    assert r["title"].lower().startswith("the matrix")
    assert r.get("year") == 1999


def test_guessit_rejects_null_bytes():
    with pytest.raises(ValueError):
        service.guessit_filename("bad\x00file.mkv")


def test_guessit_caps_length():
    with pytest.raises(ValueError):
        service.guessit_filename("x" * 2000)


def test_title_search_cache_is_separate_from_hub_and_title_year_context():
    from compat.cache import build_key
    from subzero.language import Language

    args = ("movie", "tt0133093", None, None, [Language("eng")], ["example"])
    hub = build_key(*args, query="1917")
    title = build_key(*args, query="1917", matching_mode="title", year=2019)
    assert hub != title
    assert title != build_key(*args, query="1917", matching_mode="title", year=2020)


def test_pool_restoration_is_opt_in_lazy_and_preserves_discards(monkeypatch):
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry

    names = ["discover_restore", "discover_discard"]
    for name in names:
        monkeypatch.setitem(provider_registry.providers, name, HubProxyProvider)
    available = []
    monkeypatch.setattr(service, "_compat_pool", None)
    # The pool now carries the same adoption gate the library pool does, and
    # that gate reads the enabled list rather than this stub, exactly as it
    # does in production where get_providers_sorted() is derived from it.
    monkeypatch.setattr(settings.general, "enabled_providers", available)
    monkeypatch.setattr(service, "get_providers_sorted", lambda: list(available))
    monkeypatch.setattr(service, "get_providers_auth", lambda: {names[0]: {"fixture_setting": "current"}})
    monkeypatch.setattr(service, "get_provider_language_hook", lambda: None)
    pool = service._get_compat_pool()
    pool.discarded_providers.add(names[1])
    available.extend(names)
    # Legacy Hub acquisition does not reconcile or initialize any providers.
    assert service._get_compat_pool() is pool
    assert pool.providers == []
    assert service._get_compat_pool(restore_available=True) is pool
    assert pool.providers == [names[0]]
    assert pool.initialized_providers == {}
    assert pool.provider_configs[names[0]] == {"fixture_setting": "current"}
    assert pool.discarded_providers == {names[1]}


def _fixture_provider(monkeypatch, name, error):
    """Register a catalog-shaped provider whose search raises ``error``."""
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    def list_subtitles(self, video, languages):
        raise error

    cls = type(f"{name}FixtureProvider", (HubProxyProvider,),
               {"provider_name": name, "languages": {Language("eng")},
                "list_subtitles": list_subtitles})
    monkeypatch.setitem(provider_registry.providers, name, cls)
    return name


def _isolated_compat_pool(monkeypatch, tmp_path, names):
    """A real compat pool, with the throttle table and its file redirected into
    tmp_path so the write path can be asserted without touching /config."""
    from app import get_providers
    from app.config import settings

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_providers.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(get_providers, "tp", {})
    # provider_throttle ends by emitting a badge event, which needs a socketio
    # the bare test process has not created. Recording the backoff is what is
    # under test here, not announcing it.
    monkeypatch.setattr(get_providers, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(settings.general, "enabled_providers", list(names))
    monkeypatch.setattr(get_providers, "get_providers_sorted", lambda: list(names))
    monkeypatch.setattr(service, "get_providers_sorted", lambda: list(names))
    monkeypatch.setattr(service, "get_providers_auth", lambda: {})
    monkeypatch.setattr(service, "get_provider_language_hook", lambda: None)
    monkeypatch.setattr(service, "_compat_pool", None)
    return service._get_compat_pool()


def _search(pool, name, on_outcome=lambda outcome, elapsed: None):
    from subliminal.video import Movie
    from subzero.language import Language

    return service.search_title(Movie("", "The Matrix", imdb_id="tt0133093"),
                                [Language("eng")], pool, [name], on_outcome)


def test_compat_pool_records_a_hard_upstream_failure_in_the_throttle_table(monkeypatch, tmp_path):
    """The compat pool was built without throttle_callback, so every
    per-exception cool-off in provider_throttle_map() was dead code on this
    path: throttled_providers.dat was never even created, and a site answering
    500 to every request was asked again on the very next search.

    Asserting the file, not only the in-memory dict, is deliberate. The absence
    of that file on a live instance with four recorded provider failures was
    the original giveaway that nothing wrote here.
    """
    import json
    from app import get_providers

    name = _fixture_provider(monkeypatch, "compat_throttle", RuntimeError("upstream returned 500"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    outcomes = []
    _search(pool, name, lambda outcome, elapsed: outcomes.append(outcome))

    assert [outcome.status for outcome in outcomes] == ["error"]
    recorded, until, description = get_providers.tp[name]
    assert recorded == "RuntimeError" and until is not None
    dat = tmp_path / "config" / "throttled_providers.dat"
    assert dat.exists(), "the compat pool still does not write the throttle table"
    assert json.loads(dat.read_text())[name][0] == "RuntimeError"
    # An upstream failure now earns the backoff its exception class is worth,
    # instead of the flat minute Discover used to wait before asking again.
    assert description == "10 minutes"


def test_compat_pool_will_not_re_adopt_a_provider_serving_out_a_backoff(monkeypatch, tmp_path):
    """The adoption gate the library pool has always carried. Without it the
    availability restore puts a throttled provider straight back into the pool
    and the backoff it just earned is never served."""
    import datetime as dt
    from app import get_providers

    name = _fixture_provider(monkeypatch, "compat_gate", RuntimeError("upstream returned 500"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    pool.providers.remove(name)
    get_providers.tp[name] = ("RuntimeError", dt.datetime.now() + dt.timedelta(minutes=10), "10 minutes")
    assert service._get_compat_pool(restore_available=True) is pool
    assert pool.providers == []


def test_search_title_feeds_the_health_tracker_it_reads_from(monkeypatch, tmp_path):
    """search_title passed on_outcome but never on_result, so health.record was
    never called on this path while discover.search read currently_discarded()
    from the same tracker. Read side present, write side absent, so the
    escalating discard in provider_health could never engage."""
    from subliminal_patch.provider_health import FAILURES_TO_DISCARD, get_tracker, reset_tracker

    name = _fixture_provider(monkeypatch, "compat_health", RuntimeError("upstream returned 500"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    # The throttle table is the other half of the backoff and has its own test.
    # Left live it would gate this provider out of the pool after the first
    # failure, and the adoption refusal is not the escalation under test here.
    pool.throttle_callback = lambda *args, **kwargs: None
    reset_tracker()
    try:
        for _ in range(FAILURES_TO_DISCARD):
            _search(pool, name)
        assert get_tracker().snapshot()[name]["discard_level"] == 1
        assert name in get_tracker().currently_discarded()
    finally:
        reset_tracker()


def test_a_throttled_pool_member_is_not_searched_again_while_the_backoff_runs(monkeypatch, tmp_path):
    """The adoption gate only guards the way in, and a throttled provider is
    already inside: this pool lives for the process, so nothing ever takes it
    back out. Recording the backoff therefore removed the provider from the
    library search and from Discover's coverage while this path went on asking
    it on every single request."""
    import datetime as dt
    import types
    from collections import defaultdict
    from app import get_providers
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    asked = []
    name = "compat_still_asked"
    cls = type("StillAskedFixtureProvider", (HubProxyProvider,),
               {"provider_name": name, "languages": {Language("eng")},
                "list_subtitles": lambda self, video, languages: asked.append(video) or []})
    monkeypatch.setitem(provider_registry.providers, name, cls)
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr(service, "_build_video",
                        lambda *args, **kwargs: types.SimpleNamespace(name="/no/such/file.mkv"))
    monkeypatch.setattr(service, "list_all_subtitles_parallel",
                        lambda videos, languages, pool_instance, **kw: (
                            searched.update(exclude=set(kw.get("exclude_providers") or ())),
                            defaultdict(list))[1])
    searched = {}

    service._do_fanout("tt0133093", None, None, [], "movie", timeout_seconds=8)
    assert name not in searched["exclude"], "the provider was excluded before it was throttled"

    get_providers.tp[name] = ("RuntimeError", dt.datetime.now() + dt.timedelta(minutes=10), "10 minutes")
    service._do_fanout("tt0133093", None, None, [], "movie", timeout_seconds=8)
    assert name in searched["exclude"]
    assert pool.providers == [name], "the fanout must skip the member, not mutate the shared pool"


def test_the_throttled_provider_really_stops_receiving_requests(monkeypatch, tmp_path):
    """The same thing said end to end, through the real fanout: the provider's
    own list_subtitles is what must stop being called."""
    import datetime as dt
    from app import get_providers
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from subliminal.video import Movie
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    asked = []
    name = "compat_real_fanout"
    cls = type("RealFanoutFixtureProvider", (HubProxyProvider,),
               {"provider_name": name, "languages": {Language("eng")},
                "list_subtitles": lambda self, video, languages: asked.append(video) or []})
    monkeypatch.setitem(provider_registry.providers, name, cls)
    _isolated_compat_pool(monkeypatch, tmp_path, [name])
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr(service, "_build_video",
                        lambda *args, **kwargs: Movie("/no/such/file.mkv", "The Matrix",
                                                      imdb_id="tt0133093"))

    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert len(asked) == 1

    get_providers.tp[name] = ("RuntimeError", dt.datetime.now() + dt.timedelta(minutes=10), "10 minutes")
    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert len(asked) == 1, "a provider serving out its backoff was asked again"


def test_a_search_in_flight_cannot_re_throttle_credentials_that_were_just_corrected(monkeypatch, tmp_path):
    """Dropping the pool does not cancel the searches running on it.

    Correcting a provider's credentials clears its twelve-hour backoff and
    drops the pool, but a call already in flight is still presenting the old
    password. Its AuthenticationError judged the credentials the user has just
    replaced, so recording it writes the backoff straight back over the one the
    save cleared and the corrected provider is skipped again. A failure the
    provider owns, a rate limit or an outage, is unaffected by what was saved
    locally and is still recorded.
    """
    from app import get_providers
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from subliminal.exceptions import AuthenticationError
    from subliminal.video import Movie
    from subliminal_patch.exceptions import TooManyRequests
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    name = "compat_superseded"
    saves = []

    def list_subtitles(self, video, languages):
        if saves:
            # The credential save lands while this call is in flight.
            service.reset_compat_pool()
            raise AuthenticationError("the password the user has just replaced")
        raise TooManyRequests("slow down")

    cls = type("SupersededFixtureProvider", (HubProxyProvider,),
               {"provider_name": name, "languages": {Language("eng")},
                "list_subtitles": list_subtitles})
    monkeypatch.setitem(provider_registry.providers, name, cls)
    _isolated_compat_pool(monkeypatch, tmp_path, [name])
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr(service, "_build_video",
                        lambda *args, **kwargs: Movie("/no/such/file.mkv", "The Matrix",
                                                      imdb_id="tt0133093"))

    saves.append("saved")
    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert name not in get_providers.tp, "the superseded call re-throttled the corrected credentials"

    saves.clear()
    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert get_providers.tp[name][0] == "TooManyRequests"


def test_a_pool_rebuilt_during_a_backoff_asks_the_provider_again_once_it_lifts(monkeypatch, tmp_path):
    """The way back into the pool, which only Discover used to ask for.

    A pool reset is served by the next fanout building a fresh pool from the
    providers that are searchable at that moment, so a provider part way
    through a backoff is not in it. The fanout re-checks membership on the way
    out on every call and nothing re-checked it on the way in, so the provider
    stayed out of every compat search long after its backoff expired: one
    unrelated settings or Hub credential save was enough to lose it for the
    life of the process.
    """
    import datetime as dt
    from app import get_providers
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from subliminal.video import Movie
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    asked = []
    name = "compat_cold_rebuild"
    cls = type("ColdRebuildFixtureProvider", (HubProxyProvider,),
               {"provider_name": name, "languages": {Language("eng")},
                "list_subtitles": lambda self, video, languages: asked.append(video) or []})
    monkeypatch.setitem(provider_registry.providers, name, cls)
    _isolated_compat_pool(monkeypatch, tmp_path, [name])
    # Production derives the constructor's provider list from the throttle
    # table, which is the whole reason a rebuilt pool can come up short.
    monkeypatch.setattr(service, "get_providers_sorted",
                        lambda: [item for item in [name] if get_providers.provider_is_usable(item)])
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr(service, "_build_video",
                        lambda *args, **kwargs: Movie("/no/such/file.mkv", "The Matrix",
                                                      imdb_id="tt0133093"))

    get_providers.tp[name] = ("RuntimeError", dt.datetime.now() + dt.timedelta(minutes=10), "10 minutes")
    service.reset_compat_pool()
    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert asked == [], "a provider serving out its backoff was asked anyway"

    get_providers.tp.pop(name)
    service._do_fanout("tt0133093", None, None, [Language("eng")], "movie", timeout_seconds=8)
    assert len(asked) == 1, "the rebuilt pool never took the provider back"


def test_recording_a_rate_limit_does_not_sleep_inside_the_fanout(monkeypatch, tmp_path):
    """provider_throttle pauses between the first few rate-limit events, which
    is right where it came from: the library search retries the same provider
    in the same call. This pool never retries, and the callback runs inside the
    provider future, so the pause would be charged to the wall the remaining
    providers are still searching against. At the smallest valid five-second
    wall the default five-second pause alone is enough to have this provider
    reported as abandoned instead of cooling down."""
    import time
    from app import get_providers
    from subliminal_patch.exceptions import APIThrottled

    name = _fixture_provider(monkeypatch, "compat_rate_limited", APIThrottled("slow down"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    monkeypatch.setattr(get_providers, "throttle_count", {})
    outcomes = []
    started = time.monotonic()
    _search(pool, name, lambda outcome, elapsed: outcomes.append(outcome))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the throttle bookkeeping slept inside the fanout deadline"
    assert [outcome.status for outcome in outcomes] == ["cooldown"]
    # Recorded all the same, and on the first event: the pause is what is
    # dropped, not the backoff. Waiting for a fifth event would have this path
    # re-ask a rate-limited provider on its next four requests, with the
    # per-provider cool-offs never applying to it at all.
    assert get_providers.throttle_count[name]["count"] == 1
    recorded, until, description = get_providers.tp[name]
    assert recorded == "APIThrottled" and description == "10 minutes"


def test_a_worker_that_blew_its_deadline_is_recorded_as_a_timeout(monkeypatch, tmp_path):
    """The transport cannot carry the provider's exception class, only a code.
    provider_search_failure honours that code, the throttle table did not, so
    the outcome said "timeout" while the table said "WorkerError" with the
    generic default duration. The table is what Discover reads back for the
    cooldown it shows and what outranks the timeout floor, so the two have to
    agree."""
    from app import get_providers
    from provider_hub.worker import WorkerError

    name = _fixture_provider(monkeypatch, "compat_worker_timeout",
                             WorkerError("worker exceeded 30.0s deadline", code="timeout"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    outcomes = []
    _search(pool, name, lambda outcome, elapsed: outcomes.append(outcome))

    assert [outcome.status for outcome in outcomes] == ["timeout"]
    recorded, until, description = get_providers.tp[name]
    assert recorded == "Timeout", "the throttle table still disagrees with the reported outcome"
    assert description == "1 hour"
    # And that is a cause Discover can name rather than a bare cooldown.
    from discover.search import _THROTTLE_CAUSE
    assert _THROTTLE_CAUSE[recorded] == ("timeout", "timeout")


def test_a_provider_raised_timeout_from_a_worker_is_recorded_as_a_timeout(monkeypatch, tmp_path):
    """The hard-deadline kill is not the only timeout the transport flattens.
    A provider that raises one itself comes back as code="provider" plus the
    remote class name, which the search outcome already reads. Classifying only
    the deadline case leaves the table saying WorkerError with the generic
    default about a failure the UI is calling a timeout, and _retry_delay then
    prefers that mismatched deadline."""
    from app import get_providers
    from provider_hub.worker import WorkerError

    name = _fixture_provider(monkeypatch, "compat_remote_timeout",
                             WorkerError("provider failed", code="provider",
                                         remote_class_name="ReadTimeout"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    outcomes = []
    _search(pool, name, lambda outcome, elapsed: outcomes.append(outcome))

    assert [outcome.status for outcome in outcomes] == ["timeout"]
    recorded, until, description = get_providers.tp[name]
    assert recorded == "ReadTimeout" and description == "1 hour"


def test_a_provider_error_the_transport_cannot_name_keeps_its_own_class(monkeypatch, tmp_path):
    """Only the timeout names are translated. Anything else stays what it was,
    so the envelope cannot quietly relabel unrelated provider failures."""
    from app import get_providers
    from provider_hub.worker import WorkerError

    name = _fixture_provider(monkeypatch, "compat_remote_other",
                             WorkerError("provider failed", code="provider",
                                         remote_class_name="SomeProviderError"))
    pool = _isolated_compat_pool(monkeypatch, tmp_path, [name])
    _search(pool, name)
    assert get_providers.tp[name][0] == "WorkerError"
