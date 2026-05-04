import pytest


def test_serve_local_srt_passthrough(tmp_path):
    from compat.local_subs import serve_local
    sub = tmp_path / "movie.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "srt", "media_type": "movie", "media_id": 1,
        "media_dir": str(tmp_path),
    }
    blob, ctype = serve_local(payload)
    assert ctype == "application/x-subrip"
    assert b"Hi" in blob


def test_serve_local_converts_ass(tmp_path):
    from compat.local_subs import serve_local
    sub = tmp_path / "movie.en.ass"
    sub.write_bytes(
        b"[Script Info]\nScriptType: v4.00+\n\n"
        b"[V4+ Styles]\nFormat: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n"
        b"[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        b"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello\n"
    )
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "ass", "media_type": "movie", "media_id": 1,
        "media_dir": str(tmp_path),
    }
    blob, ctype = serve_local(payload)
    assert ctype == "application/x-subrip"
    assert b"Hello" in blob
    assert b"[Script Info]" not in blob


def test_serve_local_raises_on_missing_file(tmp_path):
    from compat.local_subs import serve_local
    payload = {
        "kind": "local", "path": str(tmp_path / "ghost.srt"),
        "lang": "en", "modifier": None, "fmt": "srt",
        "media_type": "movie", "media_id": 1, "media_dir": str(tmp_path),
    }
    with pytest.raises(FileNotFoundError):
        serve_local(payload)


def test_serve_local_raises_when_path_outside_media_dir(tmp_path):
    from compat.local_subs import serve_local
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sub = outside / "evil.srt"
    sub.write_text("x")
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "srt", "media_type": "movie", "media_id": 1,
        "media_dir": str(inside),
    }
    with pytest.raises(FileNotFoundError):
        serve_local(payload)


def test_serve_local_raises_on_oversized(tmp_path):
    from compat import local_subs
    sub = tmp_path / "big.srt"
    sub.write_bytes(b"x" * (6 * 1024 * 1024))
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "srt", "media_type": "movie", "media_id": 1,
        "media_dir": str(tmp_path),
    }
    with pytest.raises(FileNotFoundError):
        local_subs.serve_local(payload)
