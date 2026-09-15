# coding=utf-8
"""An upgraded sports subtitle must not leave its predecessor on disk.

subtitles/download.py deletes the superseded file when an upgrade writes the
replacement under a different name. The sports path had no equivalent: it
passed previous_artifact, but that only proved the old file had not changed
during the search. Nothing removed it, so an upgrade that changed the format or
the language-variant suffix left the lower-scored file behind, where the next
index picked it up as an additional subtitle for the event.
"""
import os

import pytest


@pytest.fixture
def remove_superseded():
    from sportarr.subtitles import _remove_superseded_sports_subtitle

    return _remove_superseded_sports_subtitle


def test_the_replaced_file_is_deleted(tmp_path, remove_superseded, monkeypatch):
    old = tmp_path / "race.en.srt"
    new = tmp_path / "race.en.ass"
    old.write_text("old")
    new.write_text("new")

    import contextlib

    monkeypatch.setattr(
        "subtitles.tools.subsync_engines.subtitle_mutation",
        lambda *a, **k: contextlib.nullcontext(),
    )
    remove_superseded(str(tmp_path / "race.mkv"), (str(old), "proof"), [str(new)], True)

    assert not old.exists()
    assert new.exists()


def test_a_rewrite_in_place_is_not_a_replacement(tmp_path, remove_superseded, monkeypatch):
    """Deleting here would destroy the subtitle that was just written."""
    same = tmp_path / "race.en.srt"
    same.write_text("rewritten")

    import contextlib

    monkeypatch.setattr(
        "subtitles.tools.subsync_engines.subtitle_mutation",
        lambda *a, **k: contextlib.nullcontext(),
    )
    remove_superseded(
        str(tmp_path / "race.mkv"), (str(same), "proof"), [str(same)], True
    )

    assert same.exists()


def test_paths_are_compared_by_realpath(tmp_path, remove_superseded, monkeypatch):
    """A symlink to the file just written is still that file."""
    real = tmp_path / "race.en.srt"
    real.write_text("written")
    link = tmp_path / "link.en.srt"
    os.symlink(real, link)

    import contextlib

    monkeypatch.setattr(
        "subtitles.tools.subsync_engines.subtitle_mutation",
        lambda *a, **k: contextlib.nullcontext(),
    )
    remove_superseded(
        str(tmp_path / "race.mkv"), (str(link), "proof"), [str(real)], True
    )

    assert real.exists()
    assert link.exists()


def test_nothing_happens_when_it_is_not_an_upgrade(tmp_path, remove_superseded):
    old = tmp_path / "race.en.srt"
    old.write_text("old")

    remove_superseded(
        str(tmp_path / "race.mkv"), (str(old), "proof"), [str(tmp_path / "new.srt")], False
    )

    assert old.exists()


def test_a_missing_previous_file_is_survivable(tmp_path, remove_superseded, monkeypatch):
    import contextlib

    monkeypatch.setattr(
        "subtitles.tools.subsync_engines.subtitle_mutation",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Must not raise: the file may already be gone.
    remove_superseded(
        str(tmp_path / "race.mkv"),
        (str(tmp_path / "gone.srt"), "proof"),
        [str(tmp_path / "new.srt")],
        True,
    )


def test_it_runs_before_the_publication_is_marked_done():
    import inspect

    from sportarr import subtitles

    source = inspect.getsource(subtitles.save_sports_subtitle)
    remove_at = source.index("_remove_superseded_sports_subtitle(")
    published_at = source.index('state["published"] = True')
    assert remove_at < published_at
