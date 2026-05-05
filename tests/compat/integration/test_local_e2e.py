from unittest.mock import patch, MagicMock

import pytest
from flask import Flask


@pytest.fixture
def app():
    from compat.routes import compat_bp
    a = Flask(__name__)
    a.register_blueprint(compat_bp, url_prefix="/api/v1")
    return a


def test_e2e_search_then_download_then_stream(app, tmp_path, monkeypatch):
    """A library-only flow: imdb hits the local library, no provider,
    /download issues a stream token, /download/stream returns the SRT."""
    from compat import cache as C
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "token", "a" * 32)
    monkeypatch.setattr(settings.compat_endpoint, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings.compat_endpoint, "file_id_secret", "f" * 32)
    C.invalidate_all()

    media_dir = tmp_path / "Inception (2010)"
    media_dir.mkdir()
    sub = media_dir / "Inception.en.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    raw_subs = repr([["en", str(sub)]])

    fake_movie = MagicMock(
        radarrId=99, path=str(media_dir / "Inception.mkv"),
        subtitles=raw_subs,
        title="Inception", year="2010", imdbId="tt1375666",
    )

    def _resolve(imdb_id, season, episode, media_type, query, moviehash):
        return ("movie", 99) if imdb_id == "tt1375666" else None

    def _fetch(media_type, media_id):
        return fake_movie if (media_type, media_id) == ("movie", 99) else None

    with patch("compat.local_subs._resolve_media", side_effect=_resolve), \
         patch("compat.local_subs._fetch_media_row", side_effect=_fetch), \
         patch("compat.local_subs.path_mappings") as pm, \
         patch("compat.service._get_compat_pool") as gp, \
         patch("compat.service.list_all_subtitles_parallel", return_value={MagicMock(): []}):
        pm.path_replace_movie.side_effect = lambda p: p
        gp.return_value.providers = []
        gp.return_value.discarded_providers = set()

        client = app.test_client()
        # 1. Search
        r = client.get("/api/v1/subtitles?imdb_id=tt1375666&languages=en",
                       headers={"Api-Key": "a" * 32})
        assert r.status_code == 200, r.data
        body = r.get_json()
        assert body["data"], "expected at least one local entry"
        local_attrs = body["data"][0]["attributes"]
        assert local_attrs["from_trusted"] is True
        file_id = local_attrs["files"][0]["file_id"]

        # 2. Mint a JWT for the download step
        login_r = client.post("/api/v1/login",
                               headers={"Api-Key": "a" * 32})
        token = login_r.get_json()["token"]

        # 3. Download
        d = client.post("/api/v1/download",
                        headers={"Api-Key": "a" * 32,
                                 "Authorization": f"Bearer {token}"},
                        json={"file_id": file_id})
        assert d.status_code == 200, d.data
        link = d.get_json()["link"]
        assert "/download/stream/" in link

        # 4. Stream
        stream_path = link.split("/api/v1", 1)[1]
        s = client.get(f"/api/v1{stream_path}",
                       headers={"Api-Key": "a" * 32})
        assert s.status_code == 200, s.data
        assert b"Hi" in s.data
        assert s.mimetype == "application/x-subrip"

    C.invalidate_all()
