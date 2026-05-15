from subliminal_patch.refiners.filebot import _parse_filebot_output
from subliminal_patch.refiners import filebot


def test_filebot_refiner_parses_original_filename_from_xattr_output():
    assert (
        _parse_filebot_output('net.filebot.filename="Original.Show.S01E01.mkv"\n')
        == "Original.Show.S01E01.mkv"
    )


def test_filebot_refiner_parses_original_filename_from_attr_output():
    assert (
        _parse_filebot_output("Attribute net.filebot.filename:\nOriginal.Movie.mkv\n")
        == "Original.Movie.mkv"
    )


def test_filebot_refiner_treats_any_nonzero_return_as_missing(monkeypatch):
    class Proc:
        returncode = 2
        stdout = "not usable"
        stderr = "failed"

    monkeypatch.setattr(filebot, "_default_xattr_command", lambda filename: ["filebot", filename])
    monkeypatch.setattr(filebot.subprocess, "run", lambda *args, **kwargs: Proc())

    assert filebot.get_filebot_attrs("/media/Show.S01E01.mkv") is None


def test_filebot_refiner_uses_bounded_subprocess_timeout(monkeypatch):
    calls = {}

    class Proc:
        returncode = 0
        stdout = 'net.filebot.filename="Original.Show.S01E01.mkv"\n'
        stderr = ""

    def run(*args, **kwargs):
        calls.update(kwargs)
        return Proc()

    monkeypatch.setattr(filebot, "_default_xattr_command", lambda filename: ["filebot", filename])
    monkeypatch.setattr(filebot.subprocess, "run", run)

    assert filebot.get_filebot_attrs("/media/Show.S01E01.mkv") == "Original.Show.S01E01.mkv"
    assert calls["timeout"] == filebot.FILEBOT_XATTR_TIMEOUT
