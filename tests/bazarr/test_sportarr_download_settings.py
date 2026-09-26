# coding=utf-8
"""Settings the sports automatic download path used to ignore.

Each of these was silent: the UI reported the setting as on, and the sports
code either hardcoded the opposite or never read it at all.
"""


def test_whisper_fallback_follows_the_setting():
    """It was hardcoded False, on the media type with the worst provider
    coverage, while the UI toggle said otherwise."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic)
    assert "fallback_allowed=settings.general.use_whisper_fallback" in source
    assert "fallback_allowed=False" not in source


def test_utf8_encode_is_applied_before_the_search():
    """subliminal reads SZ_KEEP_ENCODING out of the environment at download
    time. Without setting it, sports inherited whatever a previous non-sports
    search had left behind, or nothing at all on a sports-only install."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic)
    assert 'os.environ["SZ_KEEP_ENCODING"]' in source
    env_at = source.index('os.environ["SZ_KEEP_ENCODING"]')
    download_at = source.index("download_best_subtitles(")
    assert env_at < download_at


def test_the_encoding_polarity_matches_the_shared_paths():
    """utf8_encode on means DO NOT keep the original encoding."""
    import inspect

    from sportarr import automatic
    from subtitles import download

    sports = inspect.getsource(automatic)
    # The module, not the function: generate_subtitles is decorated, so
    # getsource on it returns the pool-timing wrapper instead of the body.
    shared = inspect.getsource(download)
    assert '"" if settings.general.utf8_encode else "True"' in sports
    # The shared path expresses the same mapping the long way round.
    assert 'os.environ["SZ_KEEP_ENCODING"] = ""' in shared
    assert 'os.environ["SZ_KEEP_ENCODING"] = "True"' in shared


def test_sports_shares_the_movie_post_processing_threshold_on_purpose():
    """Sports is scored on the movie scale throughout, so a percentage of that
    scale means the same for both. Pinned so the shared branch is not later
    mistaken for an oversight and 'fixed' into a separate key."""
    import inspect

    from subtitles import processing

    source = inspect.getsource(processing._postprocessing_config)
    assert "Movies AND sports" in source
    assert "general.use_postprocessing_threshold_movie" in source
