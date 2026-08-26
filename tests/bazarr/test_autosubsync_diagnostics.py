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
