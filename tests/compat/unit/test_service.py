import pytest
from bazarr.compat import service


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
