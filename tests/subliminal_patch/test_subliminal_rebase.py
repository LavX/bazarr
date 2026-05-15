import subliminal
import subliminal_patch
from subzero.language import Language


EXPECTED_PROVIDER_NAMES = [
    "addic7ed",
    "animekalesi",
    "animesubinfo",
    "animetosho",
    "assrt",
    "avistaz",
    "betaseries",
    "bsplayer",
    "cinemaz",
    "embeddedsubtitles",
    "gestdown",
    "greeksubs",
    "greeksubtitles",
    "hdbits",
    "hosszupuska",
    "jimaku",
    "karagarga",
    "ktuvit",
    "legendasdivx",
    "legendasnet",
    "napiprojekt",
    "napisy24",
    "nekur",
    "opensubtitles",
    "opensubtitlescom",
    "pipocas",
    "podnapisi",
    "prijevodionline",
    "regielive",
    "shooter",
    "soustitreseu",
    "subclub",
    "subdl",
    "subf2m",
    "subs4free",
    "subs4series",
    "subsarr",
    "subscenter",
    "subsource",
    "subsro",
    "subssabbz",
    "subsunacs",
    "subsynchro",
    "subtis",
    "subtitrarinoi",
    "subtitriid",
    "subtitulamostv",
    "subx",
    "supersubtitles",
    "titlovi",
    "titrari",
    "titulky",
    "turkcealtyaziorg",
    "tvsubtitles",
    "whisperai",
    "wizdom",
    "xsubs",
    "yavkanet",
    "yifysubtitles",
    "zimuku",
]

EXPECTED_SUBLIMINAL_PROVIDER_NAMES = [
    "addic7ed",
    "bsplayer",
    "gestdown",
    "napiprojekt",
    "opensubtitles",
    "opensubtitlescom",
    "opensubtitlescomvip",
    "opensubtitlesvip",
    "podnapisi",
    "subtis",
    "subtitulamos",
    "tvsubtitles",
]


def test_subliminal_version_is_rebased_to_260():
    assert subliminal.__version__ == "2.6.0"


def test_subliminal_patch_monkey_patches_core_surface():
    assert subliminal.subtitle.Subtitle is subliminal_patch.subtitle.Subtitle
    assert subliminal.subtitle.guess_matches is subliminal_patch.subtitle.guess_matches

    assert subliminal.scan_video is subliminal_patch.core.scan_video
    assert subliminal.core.scan_video is subliminal_patch.core.scan_video
    assert subliminal.save_subtitles is subliminal_patch.core.save_subtitles
    assert subliminal.core.save_subtitles is subliminal_patch.core.save_subtitles
    assert subliminal.refine is subliminal_patch.core.refine
    assert subliminal.core.refine is subliminal_patch.core.refine
    assert subliminal.list_all_subtitles is subliminal_patch.core.list_all_subtitles
    assert subliminal.core.list_all_subtitles is subliminal_patch.core.list_all_subtitles
    assert subliminal.download_best_subtitles is subliminal_patch.core.download_best_subtitles
    assert subliminal.core.download_best_subtitles is subliminal_patch.core.download_best_subtitles
    assert subliminal.core.search_external_subtitles is subliminal_patch.core.search_external_subtitles

    assert subliminal.video.Video is subliminal_patch.video.Video
    assert subliminal.Video is subliminal_patch.video.Video
    assert issubclass(subliminal.video.Episode, subliminal_patch.video.Video)
    assert issubclass(subliminal.video.Movie, subliminal_patch.video.Video)


def test_provider_registry_keeps_current_provider_ids():
    from subliminal_patch.extensions import provider_registry

    assert sorted(provider_registry.names()) == EXPECTED_PROVIDER_NAMES


def test_provider_registry_uses_patch_provider_classes():
    from subliminal_patch.extensions import provider_registry

    for provider_name in provider_registry.names():
        provider = provider_registry[provider_name]

        assert provider.__module__.startswith("subliminal_patch.providers."), provider_name


def test_provider_registry_languages_use_subzero_language_objects():
    from subliminal_patch.extensions import provider_registry

    for provider_name in provider_registry.names():
        provider = provider_registry[provider_name]
        provider_languages = getattr(provider, "languages", None)

        if provider_languages is None:
            continue

        assert all(isinstance(language, Language) for language in provider_languages), provider_name


def test_provider_registry_subtitle_classes_use_patch_subtitle_base():
    from subliminal_patch.extensions import provider_registry

    for provider_name in provider_registry.names():
        provider = provider_registry[provider_name]
        subtitle_class = getattr(provider, "subtitle_class", None)

        if subtitle_class is None:
            continue

        assert issubclass(subtitle_class, subliminal_patch.subtitle.Subtitle), provider_name


def test_subliminal_provider_manager_stays_vanilla_upstream_26():
    assert sorted(subliminal.provider_manager.names()) == EXPECTED_SUBLIMINAL_PROVIDER_NAMES


def test_video_compatibility_surface_survives_rebase():
    assert ".strm" in subliminal.video.VIDEO_EXTENSIONS

    video = subliminal.video.Episode(
        "Show.S01E02.mkv",
        series="Show",
        season=1,
        episode=2,
        absolute_episode=12,
        subtitle_languages={Language("eng")},
        edition="Director's Cut",
        other="Proper",
    )

    assert video.episode == 2
    assert video.episodes == [2]
    assert video.absolute_episode == 12
    assert Language("eng") in video.subtitle_languages
    assert video.edition == "Director's Cut"
    assert video.other == "Proper"

    video.episode = 3
    assert video.episodes == [3]

    movie = subliminal.video.Movie(
        "Movie.2024.mkv",
        title="Movie",
        anilist_id=123,
        subtitle_languages={Language("spa")},
    )

    assert movie.anilist_id == 123
    assert Language("spa") in movie.subtitle_languages


def test_video_subtitle_languages_remains_mutable_for_subzero_updates():
    video = subliminal.video.Episode(
        "Show.S01E02.mkv",
        series="Show",
        season=1,
        episode=2,
    )

    video.subtitle_languages.add(Language("eng"))
    video.subtitle_languages.update({Language("spa")})

    assert video.subtitle_languages == {Language("eng"), Language("spa")}

    video.subtitle_languages = {Language("hun")}

    assert video.subtitle_languages == {Language("hun")}


def test_provider_pool_keeps_configured_priority_order_for_early_stop(monkeypatch):
    from subliminal_patch.core import SZProviderPool

    class FakeSubtitle:
        id = "preferred-english"
        language = Language("eng")
        provider_name = "preferred"

        def get_matches(self, video):
            return {"series", "season", "episode", "title"}

    calls = []

    def fake_list_subtitles_provider(self, provider, video, languages):
        calls.append(provider)
        return [FakeSubtitle()] if provider == "preferred" else []

    monkeypatch.setattr(SZProviderPool, "list_subtitles_provider", fake_list_subtitles_provider)

    video = subliminal.video.Episode("Show.S01E02.mkv", series="Show", season=1, episode=2)
    pool = SZProviderPool(providers=["preferred", "fallback"])
    subtitles = pool.list_subtitles_prioritized(
        video,
        {Language("eng")},
        min_score=1,
        compute_score=lambda matches, subtitle, video, hearing_impaired: (1, None),
    )

    assert calls == ["preferred"]
    assert len(subtitles) == 1


def test_provider_pool_continues_after_wrong_language_or_low_score(monkeypatch):
    from subliminal_patch.core import SZProviderPool

    class FakeSubtitle:
        def __init__(self, subtitle_id, language):
            self.id = subtitle_id
            self.language = language
            self.provider_name = subtitle_id

        def get_matches(self, video):
            return {"series", "season", "episode", "title"}

    calls = []

    def fake_list_subtitles_provider(self, provider, video, languages):
        calls.append(provider)
        if provider == "wrong-language":
            return [FakeSubtitle("wrong-language", Language("spa"))]
        if provider == "low-score":
            return [FakeSubtitle("low-score", Language("eng"))]
        return [FakeSubtitle("fallback", Language("eng"))]

    def fake_score(matches, subtitle, video, hearing_impaired):
        return (0 if subtitle.id == "low-score" else 1, None)

    monkeypatch.setattr(SZProviderPool, "list_subtitles_provider", fake_list_subtitles_provider)

    video = subliminal.video.Episode("Show.S01E02.mkv", series="Show", season=1, episode=2)
    pool = SZProviderPool(providers=["wrong-language", "low-score", "fallback"])
    subtitles = pool.list_subtitles_prioritized(
        video,
        {Language("eng")},
        min_score=1,
        compute_score=fake_score,
    )

    assert calls == ["wrong-language", "low-score", "fallback"]
    assert [subtitle.id for subtitle in subtitles] == ["wrong-language", "low-score", "fallback"]


def test_provider_pool_continues_until_all_requested_languages_are_satisfied(monkeypatch):
    from subliminal_patch.core import SZProviderPool

    class FakeSubtitle:
        def __init__(self, subtitle_id, language):
            self.id = subtitle_id
            self.language = language
            self.provider_name = subtitle_id

        def get_matches(self, video):
            return {"series", "season", "episode", "title"}

    calls = []

    def fake_list_subtitles_provider(self, provider, video, languages):
        calls.append(provider)
        if provider == "english":
            return [FakeSubtitle("english", Language("eng"))]
        return [FakeSubtitle("spanish", Language("spa"))]

    monkeypatch.setattr(SZProviderPool, "list_subtitles_provider", fake_list_subtitles_provider)

    video = subliminal.video.Episode("Show.S01E02.mkv", series="Show", season=1, episode=2)
    pool = SZProviderPool(providers=["english", "spanish"])
    subtitles = pool.list_subtitles_prioritized(
        video,
        {Language("eng"), Language("spa")},
        min_score=1,
        compute_score=lambda matches, subtitle, video, hearing_impaired: (1, None),
    )

    assert calls == ["english", "spanish"]
    assert [subtitle.id for subtitle in subtitles] == ["english", "spanish"]


def test_provider_pool_update_treats_priority_reorder_as_update():
    from subliminal_patch.core import SZProviderPool

    pool = SZProviderPool(providers=["first", "second"])

    updated = pool.update(
        providers=["second", "first"],
        provider_configs={},
        blacklist=[],
        ban_list={"must_contain": [], "must_not_contain": []},
    )

    assert updated is True
    assert pool.providers == ["second", "first"]


def test_cache_key_mangler_uses_fixed_size_sha1_keys():
    from subliminal.cache import sha1_key_mangler

    key = sha1_key_mangler("x" * 500)

    assert len(key) == 40
    assert key == sha1_key_mangler(("x" * 500).encode("utf-8"))


def test_patched_subtitle_accepts_upstream_26_constructor_shape():
    subtitle = subliminal_patch.subtitle.Subtitle(
        Language("eng"),
        "provider-id",
        hearing_impaired=True,
        page_link="https://example.test/subtitle",
        subtitle_format="ass",
        fps=23.976,
    )

    assert subtitle.subtitle_id == "provider-id"
    assert subtitle.hearing_impaired is True
    assert subtitle.page_link == "https://example.test/subtitle"
    assert subtitle.subtitle_format == "ass"
    assert subtitle.fps == 23.976


def test_tvsubtitles_patch_uses_upstream_26_search_request_shape():
    from subliminal_patch.providers.tvsubtitles import TVsubtitlesProvider
    from subliminal.cache import region

    region.configure("dogpile.cache.memory", replace_existing_backend=True)

    class Response:
        content = (
            b'<div class="left"><li><div><a href="/tvshow-123.html">'
            b"Upstream Probe (2020-2024)</a></div></li></div>"
        )

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, data, timeout):
            self.calls.append((url, data, timeout))
            return Response()

    provider = TVsubtitlesProvider()
    provider.session = Session()

    assert provider.search_show_id("Upstream Probe", 2020) == 123
    assert provider.session.calls == [
        ("https://www.tvsubtitles.net/search1.php", {"qs": "Upstream Probe"}, 10)
    ]


def test_tvsubtitles_patch_keeps_bazarr_subtitle_metadata_with_upstream_26_constructor():
    from subliminal_patch.providers.tvsubtitles import TVsubtitlesSubtitle

    subtitle = TVsubtitlesSubtitle(
        Language("eng"),
        "42",
        page_link="https://www.tvsubtitles.net/subtitle-42.html",
        series="Upstream Probe",
        season=1,
        episode=2,
        year=2020,
        rip="WEB",
        release="GROUP",
    )

    assert subtitle.subtitle_id == "42"
    assert subtitle.release_info == "WEB, GROUP"
    assert subtitle.matches == set()


def test_tvsubtitles_patch_converts_upstream_languages_for_bazarr_filtering():
    from subliminal_patch.providers.tvsubtitles import TVsubtitlesProvider

    class Response:
        content = (
            b'<a href="/subtitle-42.html"><div class="subtitlen">'
            b'<h5><img src="images/flags/en.gif"/> GROUP</h5>'
            b'<p title="rip">WEB</p>'
            b"</div></a>"
        )

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, timeout):
            return Response()

    provider = TVsubtitlesProvider()
    provider.session = Session()
    provider.search_show_id = lambda series, year=None: 123
    provider.get_episode_ids = lambda show_id, season: {2: 456}
    video = subliminal.video.Episode("Show.S01E02.mkv", series="Show", season=1, episode=2)

    subtitles = provider.list_subtitles(video, {Language("eng")})

    assert len(subtitles) == 1
    assert subtitles[0].language == Language("eng")
    assert type(subtitles[0].language) is Language


def test_podnapisi_patch_uses_upstream_26_json_search_endpoint():
    from subliminal_patch.providers.podnapisi import PodnapisiProvider

    class Response:
        text = (
            '{"data": [], "page": 1, "all_pages": 1}'
        )

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, dict(params), timeout))
            return Response()

    provider = PodnapisiProvider()
    provider.session = Session()

    assert provider.query(Language("eng"), "Upstream Probe", season=1, episode=2, year=2024) == []
    assert provider.session.calls == [
        (
            "https://www.podnapisi.net/subtitles/search/advanced",
            {
                "keywords": "Upstream Probe",
                "language": "en",
                "seasons": 1,
                "episodes": 2,
                "movie_type": ["tv-series", "mini-series"],
                "year": 2024,
            },
            10,
        )
    ]


def test_podnapisi_patch_maps_429_status_to_too_many_requests():
    from subliminal_patch.exceptions import TooManyRequests
    from subliminal_patch.providers.podnapisi import PodnapisiProvider

    class Response:
        status_code = 429
        text = "Too Many Requests"

        def raise_for_status(self):
            raise AssertionError("429 should be handled before generic HTTP errors")

    class Session:
        def get(self, url, params, timeout):
            return Response()

    provider = PodnapisiProvider()
    provider.session = Session()

    try:
        provider.query(Language("eng"), "Upstream Probe", season=1, episode=2, year=2024)
    except TooManyRequests:
        pass
    else:
        raise AssertionError("Expected TooManyRequests")


def test_podnapisi_patch_maps_429_text_to_too_many_requests():
    from subliminal_patch.exceptions import TooManyRequests
    from subliminal_patch.providers.podnapisi import PodnapisiProvider

    class Response:
        status_code = 200
        text = "429 Too Many Requests"

        def raise_for_status(self):
            return None

    class Session:
        def get(self, url, params, timeout):
            return Response()

    provider = PodnapisiProvider()
    provider.session = Session()

    try:
        provider.query(Language("eng"), "Upstream Probe", season=1, episode=2, year=2024)
    except TooManyRequests:
        pass
    else:
        raise AssertionError("Expected TooManyRequests")


def test_subtitulamostv_patch_keeps_bazarr_id_with_upstream_language_catalog():
    from subliminal_patch.providers.subtitulamostv import SubtitulamosTVProvider, SubtitulamosTVSubtitle

    language_codes = {(language.alpha3, language.country) for language in SubtitulamosTVProvider.languages}

    assert SubtitulamosTVSubtitle.provider_name == "subtitulamostv"
    assert {type(language) for language in SubtitulamosTVProvider.languages} == {Language}
    assert Language.fromietf("es") in SubtitulamosTVProvider.languages
    assert ("cat", None) in language_codes
    assert ("glg", None) in language_codes
    assert ("por", "BR") in language_codes
    assert ("spa", "MX") in language_codes


def test_subtitulamostv_patch_supports_bazarr_language_filtering(monkeypatch):
    from subliminal.video import Episode
    from subliminal_patch.core import SZProviderPool
    from subliminal_patch.providers.subtitulamostv import SubtitulamosTVProvider

    calls = []

    def fake_query(self, series=None, season=None, episode=None, year=None, languages=None):
        calls.append((series, season, episode, year, languages))
        return []

    monkeypatch.setattr(SubtitulamosTVProvider, "query", fake_query)

    languages = {Language.fromietf("es")}
    video = Episode("Show.S01E02.mkv", series="Show", season=1, episode=2, year=2024)

    with SZProviderPool(providers=["subtitulamostv"]) as pool:
        assert pool.list_subtitles_provider("subtitulamostv", video, languages) == []

    assert calls == [("Show", 1, 2, 2024, languages)]


def test_subtitulamostv_patch_uses_upstream_26_query_shape():
    from subliminal.video import Episode
    from subliminal_patch.providers.subtitulamostv import SubtitulamosTVProvider

    provider = SubtitulamosTVProvider()
    calls = []

    def fake_query(series=None, season=None, episode=None, year=None, languages=None):
        calls.append((series, season, episode, year, languages))
        return []

    provider.query = fake_query
    languages = {Language("spa", "MX")}
    video = Episode("Show.S01E02.mkv", series="Show", season=1, episode=2, year=2024)

    assert provider.list_subtitles(video, languages) == []
    assert calls == [("Show", 1, 2, 2024, languages)]


def test_subtitulamostv_patch_keeps_exact_series_match_guard():
    from subliminal_patch.providers.subtitulamostv import SubtitulamosTVProvider

    class Response:
        text = (
            '[{"show_id": 1, "show_name": "Dan Brown\\u0027s The Lost Symbol"},'
            '{"show_id": 2, "show_name": "Lost"}]'
        )

        def raise_for_status(self):
            return None

    provider = SubtitulamosTVProvider()
    provider._session_request = lambda *args, **kwargs: Response()

    assert provider._query_search("Lost (2004)") == [{"show_id": 2, "show_name": "Lost"}]
