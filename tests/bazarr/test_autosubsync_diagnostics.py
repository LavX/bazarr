# coding=utf-8
"""autosubsync's own numbers, instead of an opaque refusal.

autosubsync runs to completion, writes a synchronised subtitle, and then judges
its own work against a hard-coded quality threshold of 0.75. Bazarr called it
without return_parameters, so all it got back was a bare False, and the message
it produced was:

    autosubsync completed but did not meet the quality threshold.

No quality number, no threshold, nothing to say whether the fit was 0.74 or
0.05. To the reporter that read as a broken installation rather than as a file
this engine could not align.

The gate itself is upstream's and stays: what changes is that the numbers reach
the user, the log and the sync report.
"""

import pytest


def _syncer(monkeypatch, tmp_path, result):
    from subtitles.tools import subsyncer as module

    monkeypatch.setattr(module, "_autosubsync_model_file", lambda: "model.bin")
    monkeypatch.setattr(module, "_run_autosubsync_api", lambda **kwargs: result)

    syncer = module.SubSyncer()
    syncer.srtin = str(tmp_path / "Movie.en.srt")
    syncer.reference = None
    return syncer, module


def test_a_decline_names_the_quality_and_the_threshold(monkeypatch, tmp_path):
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError

    syncer, _module = _syncer(monkeypatch, tmp_path, (False, 0.43, 1.0, -2.5))

    with pytest.raises(SyncEngineDeclinedError) as raised:
        syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                       video_path=str(tmp_path / "Movie.mkv"))

    message = str(raised.value)
    assert "0.43" in message
    assert "0.75" in message, "the threshold the quality is being judged against"


def test_a_successful_run_reports_the_numbers_it_measured(monkeypatch, tmp_path):
    syncer, _module = _syncer(monkeypatch, tmp_path, (True, 0.91, 1.0, -2.5))

    result = syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                            video_path=str(tmp_path / "Movie.mkv"))

    assert result["success"] is True
    assert result["quality_of_fit"] == 0.91
    assert result["skew"] == 1.0
    assert result["offset_seconds"] == -2.5


def test_the_measured_offset_is_held_to_the_configured_maximum(monkeypatch, tmp_path):
    """autosubsync reports its shift, so it can be held to the acceptance
    threshold like ffsubsync is, instead of being exempt for want of a number."""
    from subtitles.tools.subsync_engines import SyncResultRejectedError, validate_engine_result

    syncer, _module = _syncer(monkeypatch, tmp_path, (True, 0.91, 1.0, -900.0))

    result = syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                            video_path=str(tmp_path / "Movie.mkv"))

    with pytest.raises(SyncResultRejectedError):
        validate_engine_result("autosubsync", result, "60")


def test_an_older_autosubsync_that_returns_a_bare_bool_still_works(monkeypatch, tmp_path):
    """return_parameters is not ancient. A build without it returns a plain
    bool, and that has to keep working rather than raising a TypeError inside
    the sync."""
    syncer, _module = _syncer(monkeypatch, tmp_path, True)

    result = syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                            video_path=str(tmp_path / "Movie.mkv"))

    assert result["success"] is True
    assert result.get("quality_of_fit") is None


def test_a_bare_false_still_declines_with_a_usable_message(monkeypatch, tmp_path):
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError

    syncer, _module = _syncer(monkeypatch, tmp_path, False)

    with pytest.raises(SyncEngineDeclinedError) as raised:
        syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                       video_path=str(tmp_path / "Movie.mkv"))

    assert "autosubsync" in str(raised.value)


def test_the_history_entry_carries_the_quality_it_measured(monkeypatch, tmp_path):
    """The numbers have to survive into what the user reads later, or the run
    that succeeded is as opaque as the one that declined."""
    from types import SimpleNamespace

    from subtitles.tools import subsyncer as module

    recorded = []
    monkeypatch.setattr(module, "history_log", lambda *args, **kwargs: recorded.append(args))
    # languages_dict is only populated once the app has booted against a real
    # database; the label is not what this test is about.
    monkeypatch.setattr(module, "language_from_alpha2", lambda code: "English")
    monkeypatch.setattr(module, "path_mappings",
                        SimpleNamespace(path_replace_reverse=lambda p: p,
                                        path_replace_reverse_movie=lambda p: p))

    syncer = module.SubSyncer()
    syncer.srtin = str(tmp_path / "Movie.en.srt")

    result = SimpleNamespace(
        engine="autosubsync",
        output_path=str(tmp_path / "Movie.en.srt"),
        raw_result={"success": True, "offset_seconds": -2.5, "quality_of_fit": 0.91,
                    "skew": 1.0},
    )
    syncer.reference = str(tmp_path / "Movie.mkv")

    monkeypatch.setattr(module, "history_log",
                        lambda **kwargs: recorded.append(kwargs["result"].message))

    syncer._log_sync_history(result, "overwrite", "en", False, False,
                             sonarr_series_id=1, sonarr_episode_id=2)

    assert recorded, "nothing was written to history"
    message = recorded[0]
    assert "-2.5" in message
    assert "0.91" in message, f"the quality of fit is missing from: {message}"


def test_the_decline_notification_carries_the_numbers():
    """The whole point of measuring them: they have to reach the user, not just
    the log. The reason template had no {message} slot, so the sentence in the
    job card was the same fixed line whatever the engine measured."""
    from types import SimpleNamespace

    from subtitles.sync import _engine_outcome_sentence
    from subtitles.tools.subsync_engines import REASON_ENGINE_DECLINED

    sentence = _engine_outcome_sentence(SimpleNamespace(
        engine="autosubsync",
        reason=REASON_ENGINE_DECLINED,
        message=("autosubsync measured a quality of fit of 0.43, below its 0.75 "
                 "threshold; the subtitle may not match this audio."),
        success=False,
    ))

    assert "0.43" in sentence
    assert "0.75" in sentence


def test_a_decline_with_nothing_to_add_still_reads_as_a_sentence():
    """Not every engine measures a number, and the old wording is what those
    still need."""
    from types import SimpleNamespace

    from subtitles.sync import _engine_outcome_sentence
    from subtitles.tools.subsync_engines import REASON_ENGINE_DECLINED

    sentence = _engine_outcome_sentence(SimpleNamespace(
        engine="alass", reason=REASON_ENGINE_DECLINED, message="", success=False))

    assert sentence.endswith(".")
    assert "alass" in sentence.lower()


def test_the_fallback_is_only_for_an_unsupported_keyword(monkeypatch, tmp_path):
    """autosubsync does long work and writes its output before returning, so
    re-running the whole synchronization on any internal TypeError would repeat
    that work and rewrite the file. Only the signature mismatch is a reason."""
    from subtitles.tools import subsyncer as module

    calls = []

    def exploding(*args, **kwargs):
        calls.append(kwargs)
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

    monkeypatch.setattr(module, "synchronize", exploding, raising=False)

    import autosubsync.main
    monkeypatch.setattr(autosubsync.main, "synchronize", exploding)

    with pytest.raises(TypeError):
        module._run_autosubsync_api(reference="v.mkv", subtitle_file="s.srt",
                                    output_file="o.srt", model_file="m.bin",
                                    parallelism=1)

    assert len(calls) == 1, "the whole synchronization was run a second time"


def test_an_unsupported_keyword_does_fall_back(monkeypatch):
    from subtitles.tools import subsyncer as module

    calls = []

    def picky(*args, **kwargs):
        calls.append(kwargs)
        if "return_parameters" in kwargs:
            raise TypeError("synchronize() got an unexpected keyword argument "
                            "'return_parameters'")
        return True

    import autosubsync.main
    monkeypatch.setattr(autosubsync.main, "synchronize", picky)

    assert module._run_autosubsync_api(reference="v.mkv", subtitle_file="s.srt",
                                       output_file="o.srt", model_file="m.bin",
                                       parallelism=1) is True
    assert len(calls) == 2


def test_the_measured_skew_reaches_the_history_entry(monkeypatch, tmp_path):
    """End to end from the engine, because the two halves speak different
    vocabularies: autosubsync measures a skew, the history entry reads
    framerate_scale_factor and defaults it to zero. A run with a real skew was
    recorded as a scale factor of 0.00.
    """
    from types import SimpleNamespace

    from subtitles.tools import subsyncer as module

    syncer, _module = _syncer(monkeypatch, tmp_path, (True, 0.91, 1.04, -2.5))
    raw_result = syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                                video_path=str(tmp_path / "Movie.mkv"))

    recorded = []
    monkeypatch.setattr(module, "language_from_alpha2", lambda code: "English")
    monkeypatch.setattr(module, "history_log",
                        lambda **kwargs: recorded.append(kwargs["result"].message))
    monkeypatch.setattr(module, "path_mappings",
                        SimpleNamespace(path_replace_reverse=lambda p: p,
                                        path_replace_reverse_movie=lambda p: p))
    syncer.reference = str(tmp_path / "Movie.mkv")

    syncer._log_sync_history(
        SimpleNamespace(engine="autosubsync",
                        output_path=str(tmp_path / "Movie.en.srt"),
                        raw_result=raw_result),
        "overwrite", "en", False, False, sonarr_series_id=1, sonarr_episode_id=2)

    assert recorded
    assert "1.04" in recorded[0], f"the measured skew is missing from: {recorded[0]}"
    assert "0.91" in recorded[0], f"the quality of fit is missing from: {recorded[0]}"


@pytest.mark.parametrize("quality", [0.749, 0.7499, 0.74999, 0.7499999])
def test_a_near_miss_does_not_read_as_equal_to_the_threshold(monkeypatch, tmp_path, quality):
    """Two decimals turn 0.749 into "0.75, below its 0.75 threshold", which
    contradicts itself, and the near miss is exactly the case this message
    exists to explain."""
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError

    syncer, _module = _syncer(monkeypatch, tmp_path, (False, quality, 1.0, -2.5))

    with pytest.raises(SyncEngineDeclinedError) as raised:
        syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                       video_path=str(tmp_path / "Movie.mkv"))

    message = str(raised.value)
    if "quality of fit of " in message:
        measured, threshold = message.split("quality of fit of ")[1].split(", below its ")
        assert measured != threshold.split(" ")[0], message
    else:
        # Too close to separate at any sane precision, so the message says so
        # in words rather than quoting one number twice.
        assert "just below" in message, message


def test_a_clear_miss_stays_readable(monkeypatch, tmp_path):
    """Precision is only spent where it is needed: an ordinary number keeps its
    two decimals rather than growing a tail of zeros."""
    from subtitles.tools.subsync_engines import SyncEngineDeclinedError

    syncer, _module = _syncer(monkeypatch, tmp_path, (False, 0.43, 1.0, -2.5))

    with pytest.raises(SyncEngineDeclinedError) as raised:
        syncer._run_autosubsync_engine(output_path=tmp_path / "out.srt",
                                       video_path=str(tmp_path / "Movie.mkv"))

    assert "0.43," in str(raised.value)
