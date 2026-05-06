import pytest


def test_stream_branches_to_serve_local(tmp_path):
    from compat import service, auth
    sub = tmp_path / "movie.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")

    file_id = auth.mint_local_file_id(
        path=str(sub), lang="en", modifier=None, fmt="srt",
        media_type="movie", media_id=99, media_dir=str(tmp_path),
    )
    stream_token = auth.mint_file_stream_token(file_id)

    blob, ctype = service.serve_subtitle_content(stream_token)
    assert ctype == "application/x-subrip"
    assert b"Hi" in blob


def test_stream_local_file_missing_raises_filenotfound(tmp_path):
    from compat import service, auth
    file_id = auth.mint_local_file_id(
        path=str(tmp_path / "ghost.srt"), lang="en", modifier=None,
        fmt="srt", media_type="movie", media_id=99,
        media_dir=str(tmp_path),
    )
    stream_token = auth.mint_file_stream_token(file_id)
    with pytest.raises(FileNotFoundError):
        service.serve_subtitle_content(stream_token)
