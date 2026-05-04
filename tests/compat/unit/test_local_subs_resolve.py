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
    assert result == ("episode", 42)


def test_resolve_by_imdb_movie_hits():
    from compat import local_subs
    with patch("compat.local_subs.database") as db:
        db.execute.return_value.first.return_value = _movie_row()
        result = local_subs._resolve_media(
            imdb_id="tt1375666", season=None, episode=None,
            media_type="movie", query=None, moviehash=None,
        )
    assert result == ("movie", 99)


def test_resolve_imdb_miss_no_other_keys_returns_none():
    from compat import local_subs
    with patch("compat.local_subs.database") as db:
        db.execute.return_value.first.return_value = None
        result = local_subs._resolve_media(
            imdb_id="tt0000000", season=1, episode=2,
            media_type="episode", query=None, moviehash=None,
        )
    assert result is None


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
    assert result == ("episode", 42)


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
    assert result == ("movie", 22)


def test_resolve_query_unparseable_returns_none():
    from compat import local_subs
    with patch("compat.local_subs._guessit_filename", return_value={}):
        result = local_subs._resolve_media(
            imdb_id="", season=None, episode=None, media_type="episode",
            query="garbage.dat", moviehash=None,
        )
    assert result is None
