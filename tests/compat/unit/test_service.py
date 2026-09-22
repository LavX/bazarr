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
