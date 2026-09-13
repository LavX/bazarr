# coding=utf-8
"""Translating a missing sports language from a subtitle already on disk.

The series and movies wanted scans translate instead of searching when the
profile says a language comes from another and that source already exists.
Sports only ever translated from a FRESH download, via trigger_saved, so an
event whose source subtitle was indexed off disk rather than downloaded by
Bazarr never got its translation. It stayed missing forever and every
subsequent wanted scan re-searched providers for it.
"""


def test_the_sports_hook_exists():
    from sportarr import profile_hooks

    assert hasattr(profile_hooks, "translate_from_existing")


def test_the_search_loop_translates_before_searching_providers():
    """Falling through to a provider search after queueing a translation would
    download the very subtitle the translation is producing."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    translate_at = source.index("translate_from_existing(context, code")
    pool_at = source.index('_init_pool("sports"')
    assert translate_at < pool_at
    # A queued translation must end the iteration for that language.
    following = source[translate_at : translate_at + 400]
    assert "continue" in following


def test_only_the_wanted_scan_translates_not_an_upgrade():
    """language is not None for upgrades and replacements; those are asking for
    a specific provider result, not a translation."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    translate_at = source.index("translate_from_existing(context, code")
    guard = source.rindex("if language is None:", 0, translate_at)
    between = source[guard:translate_at]
    assert "_init_pool" not in between


def test_a_translate_failure_falls_back_to_a_provider_search():
    """A broken translator must not make the language unobtainable."""
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    translate_at = source.index("translate_from_existing(context, code")
    following = source[translate_at:]
    assert "except Exception:" in following[: following.index("language_set =")]


def test_cancellation_is_not_swallowed_as_a_translate_failure():
    import inspect

    from sportarr import automatic

    source = inspect.getsource(automatic.search_event)
    translate_at = source.index("translate_from_existing(context, code")
    following = source[translate_at:]
    window = following[: following.index("language_set =")]
    assert window.index("except JobCancelled:") < window.index("except Exception:")


def test_the_source_score_guard_uses_the_movie_scale():
    """Sports is scored on the movie scale, so the percentage the threshold is
    compared against has to be computed from MAX_SCORES['movie']."""
    import inspect

    from sportarr import profile_hooks

    source = inspect.getsource(profile_hooks._source_score_below_threshold)
    assert 'MAX_SCORES["movie"]' in source
    assert "settings.translator.min_source_score" in source


def test_no_history_row_proceeds_with_the_translation():
    """A hand-placed subtitle predates history. The series path treats that as
    exactly at threshold rather than silently falling back to providers."""
    import inspect

    from sportarr import profile_hooks

    source = inspect.getsource(profile_hooks._source_score_below_threshold)
    assert "if not record or not record.score:\n        return False" in source


def test_a_deleted_translation_can_be_requeued():
    """Checking the history row alone would suppress the replacement forever
    once the translated file was removed."""
    import inspect

    from sportarr import profile_hooks

    source = inspect.getsource(profile_hooks._already_translated_on_disk)
    assert "os.path.exists(local)" in source
    assert "action == 6" in source or "action == 6" in source.replace(" ", " ")


def test_both_history_guards_are_scoped_to_the_owning_instance():
    import inspect

    from sportarr import profile_hooks

    for fn in (
        profile_hooks._source_score_below_threshold,
        profile_hooks._already_translated_on_disk,
    ):
        source = inspect.getsource(fn)
        assert "TableHistorySports.arr_instance_id == context.arr_instance_id" in source
