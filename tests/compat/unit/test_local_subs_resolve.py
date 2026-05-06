from unittest.mock import patch, MagicMock


def _show_row(sonarrSeriesId=1, imdbId="tt0903747"):
    return MagicMock(sonarrSeriesId=sonarrSeriesId, imdbId=imdbId)


def _episode_row(sonarrEpisodeId=42):
    return MagicMock(sonarrEpisodeId=sonarrEpisodeId)


def _movie_row(radarrId=99, imdbId="tt1375666", year="2010"):
    return MagicMock(radarrId=radarrId, imdbId=imdbId, year=year)


def test_resolve_by_imdb_episode_hits():
    from compat import local_subs
    with patch("compat.local_subs.database") as db:
        db.execute.side_effect = [
            MagicMock(first=lambda: _show_row()),
            MagicMock(first=lambda: _episode_row()),
        ]
        result = local_subs._resolve_media(
            imdb_id="tt0903747", season=1, episode=2,
            media_type="episode", query=None, moviehash=None,
        )
    assert result[:2] == ("episode", 42)


def test_resolve_by_imdb_movie_hits():
    from compat import local_subs
    with patch("compat.local_subs.database") as db:
        db.execute.return_value.first.return_value = _movie_row()
        result = local_subs._resolve_media(
            imdb_id="tt1375666", season=None, episode=None,
            media_type="movie", query=None, moviehash=None,
        )
    assert result[:2] == ("movie", 99)


def test_resolve_imdb_miss_no_other_keys_returns_none():
    from compat import local_subs
    with patch("compat.local_subs.database") as db:
        db.execute.return_value.first.return_value = None
        result = local_subs._resolve_media(
            imdb_id="tt0000000", season=1, episode=2,
            media_type="episode", query=None, moviehash=None,
        )
    assert result is None


def test_imdb_candidates_includes_zero_padded_forms():
    """Jellyfin and other OS-compat clients strip leading zeros on imdb_id.
    Bazarr's DB stores the Sonarr/Radarr-supplied form, which keeps them.
    The candidate set must include both for the lookup to succeed."""
    from compat.local_subs import _imdb_candidates
    cands = _imdb_candidates("481369")
    assert "tt481369" in cands
    assert "tt0481369" in cands  # 7-digit pad — the actual stored form for tt0481369
    # Should also tolerate the zero-padded request
    cands2 = _imdb_candidates("tt0481369")
    assert "tt0481369" in cands2
    assert "tt481369" in cands2  # bare-digit form too


def test_resolve_by_imdb_movie_with_zero_stripped_request_hits_padded_db_row():
    """Plugin sends 481369 (no leading zero); DB has tt0481369. Lookup
    must succeed — Codex/Jellyfin contract."""
    from compat import local_subs
    from unittest.mock import patch, MagicMock
    fake_movie = MagicMock(radarrId=99, imdbId="tt0481369", year="2007")
    with patch("compat.local_subs.database") as db:
        db.execute.return_value.first.return_value = fake_movie
        result = local_subs._resolve_media(
            imdb_id="481369",  # zero-stripped, as Jellyfin sends it
            season=None, episode=None, media_type="movie",
            query=None, moviehash=None,
        )
    assert result[:2] == ("movie", 99)
    # Verify candidates list was used (IN clause)
    call_args = db.execute.call_args[0][0]
    compiled = str(call_args.compile(compile_kwargs={"literal_binds": True}))
    assert "tt0481369" in compiled and "tt481369" in compiled


def test_resolve_by_guessit_episode_title_exact():
    from compat import local_subs
    fake_guess = {"title": "Breaking Bad", "season": 1, "episode": 2}
    with patch("compat.local_subs._guessit_filename", return_value=fake_guess), \
         patch("compat.local_subs.database") as db:
        db.execute.side_effect = [
            MagicMock(first=lambda: _show_row()),
            MagicMock(first=lambda: _episode_row()),
        ]
        result = local_subs._resolve_media(
            imdb_id="", season=None, episode=None, media_type="episode",
            query="Breaking.Bad.S01E02.1080p.mkv", moviehash=None,
        )
    assert result[:2] == ("episode", 42)


def test_resolve_by_guessit_movie_year_match():
    from compat import local_subs
    fake_guess = {"title": "Inception", "year": 2010}
    with patch("compat.local_subs._guessit_filename", return_value=fake_guess), \
         patch("compat.local_subs.database") as db:
        wrong_year = _movie_row(radarrId=11, year="2009")
        right_year = _movie_row(radarrId=22, year="2010")
        db.execute.return_value.all.return_value = [wrong_year, right_year]
        result = local_subs._resolve_media(
            imdb_id="", season=None, episode=None, media_type="movie",
            query="Inception.2010.mkv", moviehash=None,
        )
    assert result[:2] == ("movie", 22)
    assert result[2] == "query"


def test_resolve_query_unparseable_returns_none():
    from compat import local_subs
    with patch("compat.local_subs._guessit_filename", return_value={}):
        result = local_subs._resolve_media(
            imdb_id="", season=None, episode=None, media_type="episode",
            query="garbage.dat", moviehash=None,
        )
    assert result is None


def test_resolve_by_moviehash_iterates_library_files(tmp_path):
    from compat import local_subs
    f = tmp_path / "video.mkv"
    f.write_bytes(b"\x00" * (1 << 20))

    fake_episode = MagicMock(sonarrEpisodeId=7, path=str(f))

    with patch("compat.local_subs.database") as db, \
         patch("compat.local_subs._hash_cache") as hc, \
         patch("compat.local_subs.path_mappings") as pm:
        db.execute.side_effect = [
            MagicMock(all=lambda: [fake_episode]),
        ]
        pm.path_replace.side_effect = lambda p: p
        pm.path_replace_movie.side_effect = lambda p: p
        hc.get.return_value = "deadbeefcafebabe"
        result = local_subs._resolve_media(
            imdb_id=None, season=None, episode=None, media_type="episode",
            query=None, moviehash="deadbeefcafebabe",
        )
    assert result[:2] == ("episode", 7)
    assert result[2] == "moviehash"


def test_resolve_by_moviehash_no_match_returns_none(tmp_path):
    from compat import local_subs
    f = tmp_path / "video.mkv"
    f.write_bytes(b"\x00" * 1024)
    fake_episode = MagicMock(sonarrEpisodeId=7, path=str(f))
    with patch("compat.local_subs.database") as db, \
         patch("compat.local_subs._hash_cache") as hc, \
         patch("compat.local_subs.path_mappings") as pm:
        db.execute.side_effect = [
            MagicMock(all=lambda: [fake_episode]),
        ]
        pm.path_replace.side_effect = lambda p: p
        pm.path_replace_movie.side_effect = lambda p: p
        hc.get.return_value = "0000000000000001"
        result = local_subs._resolve_media(
            imdb_id=None, season=None, episode=None, media_type="episode",
            query=None, moviehash="ffffffffffffffff",
        )
    assert result is None
