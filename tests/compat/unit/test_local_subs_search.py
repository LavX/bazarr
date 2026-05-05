from unittest.mock import patch, MagicMock


def test_search_local_returns_entries_for_imdb_match(tmp_path):
    from compat import local_subs
    media_dir = tmp_path / "Inception (2010)"
    media_dir.mkdir()
    sub = media_dir / "Inception.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    raw_subs = repr([["en", str(sub)]])

    fake_movie = MagicMock(
        radarrId=99, path=str(media_dir / "Inception.mkv"),
        subtitles=raw_subs,
    )
    with patch("compat.local_subs._resolve_media", return_value=("movie", 99)), \
         patch("compat.local_subs._fetch_media_row", return_value=fake_movie), \
         patch("compat.local_subs.path_mappings") as pm:
        pm.path_replace.side_effect = lambda p: p
        pm.path_replace_movie.side_effect = lambda p: p
        result = local_subs.search_local(
            imdb_id="tt1375666", season=None, episode=None,
            media_type="movie", languages=["en"],
            query=None, moviehash=None,
        )
    assert len(result) == 1
    a = result[0]["attributes"]
    assert a["language"] == "en"
    assert a["release"] == "Inception.en.srt"
    assert a["download_count"] == 999_999
    file_id = result[0]["attributes"]["files"][0]["file_id"]
    from compat.auth import parse_file_id
    ok, payload = parse_file_id(file_id)
    assert ok
    assert payload["kind"] == "local"


def test_search_local_returns_empty_on_resolve_miss():
    from compat import local_subs
    with patch("compat.local_subs._resolve_media", return_value=None):
        result = local_subs.search_local(
            imdb_id="tt0000000", season=None, episode=None,
            media_type="movie", languages=["en"],
            query=None, moviehash=None,
        )
    assert result == []


def test_search_local_returns_empty_on_no_subtitles_in_db(tmp_path):
    from compat import local_subs
    fake_movie = MagicMock(radarrId=99, path=str(tmp_path / "x.mkv"),
                           subtitles=None)
    with patch("compat.local_subs._resolve_media", return_value=("movie", 99)), \
         patch("compat.local_subs._fetch_media_row", return_value=fake_movie):
        result = local_subs.search_local(
            imdb_id="tt1", season=None, episode=None, media_type="movie",
            languages=["en"], query=None, moviehash=None,
        )
    assert result == []


def test_search_local_moviehash_only_skips_imdb_resolution(tmp_path):
    """moviehash_match=only must not surface locals resolved via imdb,
    because those aren't hash-validated. Codex P1: strict hash mode
    contract."""
    from compat import local_subs
    # Strict mode + no moviehash supplied -> empty list (can't certify).
    with patch("compat.local_subs._resolve_media") as rm, \
         patch("compat.local_subs._resolve_by_moviehash") as rmh:
        rm.return_value = ("movie", 99)  # imdb/query path would hit
        rmh.return_value = None
        result = local_subs.search_local(
            imdb_id="tt1", season=None, episode=None, media_type="movie",
            languages=["en"], query=None, moviehash=None,
            moviehash_match="only",
        )
    assert result == []
    rm.assert_not_called()  # took the strict branch


def test_search_local_moviehash_only_uses_hash_resolution(tmp_path):
    """moviehash_match=only WITH a moviehash uses the hash resolver, NOT
    the general resolver chain."""
    from compat import local_subs
    media_dir = tmp_path / "Inception (2010)"
    media_dir.mkdir()
    sub = media_dir / "Inception.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    raw_subs = repr([["en", str(sub)]])
    fake_movie = MagicMock(radarrId=99, path=str(media_dir / "Inception.mkv"),
                           subtitles=raw_subs)
    with patch("compat.local_subs._resolve_media") as rm, \
         patch("compat.local_subs._resolve_by_moviehash", return_value=("movie", 99)) as rmh, \
         patch("compat.local_subs._fetch_media_row", return_value=fake_movie), \
         patch("compat.local_subs.path_mappings") as pm:
        pm.path_replace_movie.side_effect = lambda p: p
        result = local_subs.search_local(
            imdb_id="tt1", season=None, episode=None, media_type="movie",
            languages=["en"], query=None, moviehash="deadbeefcafebabe",
            moviehash_match="only",
        )
    assert len(result) == 1
    rm.assert_not_called()         # general resolver bypassed
    rmh.assert_called_once()       # hash resolver used
