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


def test_serve_local_accepts_path_in_absolute_target_root(tmp_path):
    """When the file_id was minted with an allowed_roots list (e.g. for
    subs in general.subfolder=='absolute' folders), serve_local must
    accept paths inside any of those roots — not just media_dir.
    Codex P1 follow-on."""
    from compat.local_subs import serve_local
    media_dir = tmp_path / "Movies" / "Inception (2010)"
    media_dir.mkdir(parents=True)
    abs_target = tmp_path / "Subtitles"
    abs_target.mkdir()
    sub = abs_target / "Inception.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")

    payload = {
        "kind": "local",
        "path": str(sub),
        "lang": "en",
        "modifier": None,
        "fmt": "srt",
        "media_type": "movie",
        "media_id": 1,
        "media_dir": str(media_dir),
        "allowed_roots": [str(media_dir), str(abs_target)],
    }
    blob, ctype = serve_local(payload)
    assert ctype == "application/x-subrip"
    assert b"Hi" in blob


def test_serve_local_rejects_path_outside_all_roots(tmp_path):
    from compat.local_subs import serve_local
    media_dir = tmp_path / "Movies"
    media_dir.mkdir()
    abs_target = tmp_path / "Subtitles"
    abs_target.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sub = elsewhere / "evil.srt"
    sub.write_text("x")
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "srt", "media_type": "movie", "media_id": 1,
        "media_dir": str(media_dir),
        "allowed_roots": [str(media_dir), str(abs_target)],
    }
    with pytest.raises(FileNotFoundError):
        serve_local(payload)


def test_serve_local_back_compat_payload_uses_media_dir_fallback(tmp_path):
    """Pre-migration payloads (no allowed_roots key) still serve correctly
    by falling back to [media_dir]."""
    from compat.local_subs import serve_local
    sub = tmp_path / "movie.en.srt"
    sub.write_text("Hi")
    payload = {
        "kind": "local", "path": str(sub), "lang": "en", "modifier": None,
        "fmt": "srt", "media_type": "movie", "media_id": 1,
        "media_dir": str(tmp_path),
        # No allowed_roots key
    }
    blob, ctype = serve_local(payload)
    assert b"Hi" in blob
