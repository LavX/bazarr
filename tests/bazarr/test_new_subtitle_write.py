# coding=utf-8
"""write_new_subtitle: the write the editor's create route and Extract to file share.

Never over an existing file, a pair whole or not at all, one publication for
the media servers, and the configured permissions on every file written.
"""
import os
import stat
from contextlib import contextmanager

import pytest


@pytest.fixture
def published(monkeypatch):
    events = []
    monkeypatch.setattr("media_servers.events.notify_subtitle_mutation", events.append)
    return events


def test_writes_bytes_and_copies_files_then_publishes_once(tmp_path, published):
    from subtitles.new_subtitle import write_new_subtitle

    video = tmp_path / "Film.mkv"
    source = tmp_path / "cache.sub"
    source.write_bytes(b"\x00\x00\x01\xba pack")

    written = write_new_subtitle(str(video), [(str(tmp_path / "Film.hu.idx"), b"# VobSub index"),
                                              (str(tmp_path / "Film.hu.sub"), str(source))],
                                 "movie", 7, operation="extract")

    assert written == [str(tmp_path / "Film.hu.idx"), str(tmp_path / "Film.hu.sub")]
    assert (tmp_path / "Film.hu.idx").read_bytes() == b"# VobSub index"
    assert (tmp_path / "Film.hu.sub").read_bytes() == b"\x00\x00\x01\xba pack"
    assert [(e.media_type, e.video_path, e.subtitle_path, e.operation, e.arr_instance_id) for e in published] == [
        ("movie", str(video), str(tmp_path / "Film.hu.idx"), "extract", 7)]


@pytest.mark.parametrize("taken", ["Film.hu.idx", "Film.hu.sub", "Subs/Film.hu.idx"])
def test_one_taken_name_writes_nothing(tmp_path, published, taken):
    from subtitles.new_subtitle import SubtitleFileExists, write_new_subtitle

    (tmp_path / "Subs").mkdir()
    (tmp_path / taken).write_text("mine")

    with pytest.raises(SubtitleFileExists) as refused:
        write_new_subtitle(str(tmp_path / "Film.mkv"),
                           [(str(tmp_path / "Film.hu.idx"), b"i"), (str(tmp_path / "Film.hu.sub"), b"s")],
                           "movie", 7, also_check=[str(tmp_path / "Subs" / "Film.hu.idx")])

    assert refused.value.path == str(tmp_path / taken)
    assert str(refused.value) == f"A subtitle file named {os.path.basename(taken)} already exists"
    assert (tmp_path / taken).read_text() == "mine"
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == [taken]
    assert published == []


def test_a_failed_second_file_takes_the_first_back(tmp_path, published, monkeypatch):
    from subtitles import new_subtitle

    def no_space(path, source, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(new_subtitle, "copy_file_atomically", no_space)
    with pytest.raises(OSError):
        new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"),
                                        [(str(tmp_path / "Film.hu.idx"), b"i"),
                                         (str(tmp_path / "Film.hu.sub"), str(tmp_path / "cache.sub"))],
                                        "movie", 7)
    assert list(tmp_path.iterdir()) == []
    assert published == []


def test_the_sports_guard_wraps_the_write_with_the_first_path(tmp_path, published):
    from subtitles.new_subtitle import write_new_subtitle

    seen = []

    @contextmanager
    def guard(*, output_path=None):
        seen.append(("enter", output_path, os.path.exists(output_path)))
        yield
        seen.append(("exit", output_path, os.path.exists(output_path)))

    write_new_subtitle(str(tmp_path / "event.mkv"), [(str(tmp_path / "event.fr.srt"), b"1")], "sports", 3,
                       publication_guard=guard)

    assert seen == [("enter", str(tmp_path / "event.fr.srt"), False), ("exit", str(tmp_path / "event.fr.srt"), True)]


def test_chmod_applies_to_every_file(tmp_path, published, monkeypatch):
    from app.config import settings
    from subtitles.new_subtitle import write_new_subtitle

    monkeypatch.setattr(settings.general, "chmod_enabled", True)
    monkeypatch.setattr(settings.general, "chmod", "0600")

    write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.idx"), b"i"),
                                                     (str(tmp_path / "Film.hu.sub"), b"s")], "movie", 7)

    assert {oct((tmp_path / name).stat().st_mode & 0o777) for name in ("Film.hu.idx", "Film.hu.sub")} == {"0o600"}


def test_a_file_another_program_creates_while_writing_is_never_replaced(tmp_path, published, monkeypatch):
    """The locks hold Bazarr+'s own writers back, not an arr importing extras.
    A name taken between the check and the publish refuses the write."""
    from subtitles import new_subtitle

    staging = new_subtitle._new_staging_file

    def foreign_writer_meanwhile(*args, **kwargs):
        made = staging(*args, **kwargs)
        (tmp_path / "Film.hu.srt").write_text("imported by another program")
        return made

    monkeypatch.setattr(new_subtitle, "_new_staging_file", foreign_writer_meanwhile)
    with pytest.raises(new_subtitle.SubtitleFileExists) as refused:
        new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.srt"), b"mine")],
                                        "movie", 7)

    assert refused.value.path == str(tmp_path / "Film.hu.srt")
    assert (tmp_path / "Film.hu.srt").read_text() == "imported by another program"
    assert [path.name for path in tmp_path.iterdir()] == ["Film.hu.srt"]
    assert published == []


@pytest.mark.parametrize("taken", [False, True], ids=["free", "taken-meanwhile"])
def test_without_hard_links_the_name_is_still_never_replaced(tmp_path, published, monkeypatch, taken):
    import errno

    from subtitles import new_subtitle

    def no_links(source, target):
        if taken:
            (tmp_path / "Film.hu.srt").write_text("imported by another program")
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(new_subtitle.os, "link", no_links)
    if taken:
        with pytest.raises(new_subtitle.SubtitleFileExists):
            new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"),
                                            [(str(tmp_path / "Film.hu.srt"), b"mine")], "movie", 7)
        assert (tmp_path / "Film.hu.srt").read_text() == "imported by another program"
        assert published == []
    else:
        new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.srt"), b"mine")],
                                        "movie", 7)
        assert (tmp_path / "Film.hu.srt").read_bytes() == b"mine"
        assert len(published) == 1
    assert [path.name for path in tmp_path.iterdir()] == ["Film.hu.srt"]


def test_a_short_write_never_publishes_a_truncated_file(tmp_path, published, monkeypatch):
    from subtitles import new_subtitle

    write = os.write
    monkeypatch.setattr(new_subtitle.os, "write", lambda fd, data: write(fd, bytes(data[:3])))
    payload = b"1\n00:00:01,000 --> 00:00:02,000\nHello there\n"

    new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.srt"), payload)],
                                    "movie", 7)
    new_subtitle.write_bytes_atomically(str(tmp_path / "Film.de.srt"), payload)

    assert (tmp_path / "Film.hu.srt").read_bytes() == payload
    assert (tmp_path / "Film.de.srt").read_bytes() == payload


def test_a_new_file_gets_the_mode_a_download_gets(tmp_path, published):
    """mkstemp's 0600 would hide the file from a media server running as
    another user; a new subtitle gets 0666 less the umask, like a download.
    The kernel applies the umask: nothing here reads or sets it."""
    from subtitles.new_subtitle import write_new_subtitle

    source = tmp_path / "cache.sub"
    source.write_bytes(b"pack")
    previous = os.umask(0o027)
    try:
        write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.idx"), b"i"),
                                                         (str(tmp_path / "Film.hu.sub"), str(source))], "movie", 7)
        (tmp_path / "plain.srt").write_bytes(b"download")
    finally:
        os.umask(previous)

    modes = {name: oct((tmp_path / name).stat().st_mode & 0o777) for name in ("Film.hu.idx", "Film.hu.sub",
                                                                              "plain.srt")}
    assert modes == {"Film.hu.idx": "0o640", "Film.hu.sub": "0o640", "plain.srt": "0o640"}


def test_the_write_never_touches_the_process_umask_or_needs_fchmod(tmp_path, published, monkeypatch):
    """The umask is process-wide, so a thread setting it races every other
    file created meanwhile; os.fchmod does not exist on Windows before 3.13."""
    from subtitles import new_subtitle

    def forbidden(*args):
        raise AssertionError("the new-subtitle write must not call this")

    monkeypatch.setattr(new_subtitle.os, "umask", forbidden)
    monkeypatch.delattr(new_subtitle.os, "fchmod", raising=False)
    monkeypatch.delattr(new_subtitle.os, "chmod", raising=False)

    new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.hu.srt"), b"mine")],
                                    "movie", 7)

    assert (tmp_path / "Film.hu.srt").read_bytes() == b"mine"


def test_also_refuse_is_asked_under_the_locks_and_stops_the_write(tmp_path, published, monkeypatch):
    from subtitles import new_subtitle

    held = []
    real_locks = new_subtitle.subtitle_write_locks

    def tracking_locks(*args, **kwargs):
        held.append("locked")
        return real_locks(*args, **kwargs)

    monkeypatch.setattr(new_subtitle, "subtitle_write_locks", tracking_locks)
    with pytest.raises(new_subtitle.SubtitleFileExists, match="Film.ASS"):
        new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(tmp_path / "Film.en.srt"), b"x")],
                                        "movie", 7,
                                        also_refuse=lambda: held and str(tmp_path / "Film.ASS") or None)
    assert list(tmp_path.iterdir()) == [] and published == []


def test_a_failed_pair_never_takes_back_a_half_another_program_replaced(tmp_path, published, monkeypatch):
    """The .idx is published, then the .sub fails. If another program put its
    own .idx there meanwhile, that file stays: only what this write published
    is taken back."""
    from subtitles import new_subtitle

    def replaced_then_no_space(path, source, **kwargs):
        foreign = tmp_path / "foreign.tmp"
        foreign.write_text("imported by another program")
        os.replace(foreign, tmp_path / "Film.hu.idx")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(new_subtitle, "copy_file_atomically", replaced_then_no_space)
    with pytest.raises(OSError):
        new_subtitle.write_new_subtitle(str(tmp_path / "Film.mkv"),
                                        [(str(tmp_path / "Film.hu.idx"), b"i"),
                                         (str(tmp_path / "Film.hu.sub"), str(tmp_path / "cache.sub"))],
                                        "movie", 7)
    assert (tmp_path / "Film.hu.idx").read_text() == "imported by another program"
    assert published == []


def test_the_writers_return_the_identity_of_the_file_they_published(tmp_path):
    from subtitles import new_subtitle

    source = tmp_path / "cache.sub"
    source.write_bytes(b"pack")
    written = new_subtitle.write_bytes_atomically(str(tmp_path / "a.srt"), b"a", replace=False)
    copied = new_subtitle.copy_file_atomically(str(tmp_path / "b.sub"), str(source), replace=False)

    assert written == new_subtitle.file_identity(str(tmp_path / "a.srt")) is not None
    assert copied == new_subtitle.file_identity(str(tmp_path / "b.sub")) is not None


def test_a_replacement_keeps_the_mode_of_the_file_it_replaces(tmp_path, published, monkeypatch):
    """os.replace keeps the temporary file's mode. A replacement must publish
    with the mode the subtitle already had, or a media server running as
    another user can no longer read it."""
    from app.config import settings
    from subtitles.new_subtitle import write_new_subtitle

    monkeypatch.setattr(settings.general, "chmod_enabled", False)
    original = tmp_path / "Film.en.srt"
    original.write_bytes(b"old")
    os.chmod(original, 0o644)

    write_new_subtitle(str(tmp_path / "Film.mkv"), [(str(original), b"new")], "movie", 7,
                       operation="edit", replace=True)

    assert original.read_bytes() == b"new"
    assert stat.S_IMODE(original.stat().st_mode) == 0o644
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Film.en.srt"]


def test_a_rejected_replacement_puts_the_original_back(tmp_path, published):
    """The sports publication boundary validates on its way out, after the file
    was replaced. A rejection there must leave the original subtitle, not a
    failure message over the new file."""
    from subtitles.new_subtitle import write_new_subtitle

    original = tmp_path / "event.en.srt"
    original.write_bytes(b"old")

    @contextmanager
    def guard(*, output_path=None):
        yield
        raise ValueError("the recording changed")

    with pytest.raises(ValueError, match="the recording changed"):
        write_new_subtitle(str(tmp_path / "event.mkv"), [(str(original), b"new")], "sports", 3,
                           operation="edit", replace=True, publication_guard=guard)

    assert original.read_bytes() == b"old"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["event.en.srt"]
    assert published == []
