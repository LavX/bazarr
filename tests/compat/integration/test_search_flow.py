import pytest
from unittest.mock import patch, MagicMock
from babelfish import Language


@pytest.fixture(autouse=True)
def _set_file_id_secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "file_id_secret", "f" * 32)


def test_search_calls_parallel_fanout_and_caches():
    from compat import service, cache as C
    C.invalidate_all()
    fake_sub = MagicMock(
        provider_name="opensubtitlescom", id="123",
        language=Language("eng"), release_info="Movie.2020.1080p",
        download_count=500, hearing_impaired=False, matches={"hash"},
    )
    with patch("compat.service._get_compat_pool") as gp, \
         patch("compat.service.list_all_subtitles_parallel") as lf:
        lf.return_value = {MagicMock(): [fake_sub]}
        gp.return_value.providers = ["opensubtitlescom"]
        gp.return_value.discarded_providers = set()
        result = service.search(imdb_id="tt12345", season=1, episode=2,
                                languages=[Language("eng")],
                                media_type="episode")
        assert result["data"]
        # Second call hits cache, fanout not re-invoked.
        lf.reset_mock()
        result2 = service.search(imdb_id="tt12345", season=1, episode=2,
                                  languages=[Language("eng")],
                                  media_type="episode")
        assert result == result2
        lf.assert_not_called()
    C.invalidate_all()


def test_subtitles_endpoint_requires_api_key(monkeypatch):
    from flask import Flask
    from compat.routes import compat_bp
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    r = app.test_client().get("/api/v1/subtitles?imdb_id=tt1&languages=en")
    assert r.status_code == 403
    assert r.headers["x-reason"] == "auth"


def test_subtitles_requires_languages(monkeypatch):
    from flask import Flask
    from compat.routes import compat_bp
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    r = app.test_client().get("/api/v1/subtitles", headers={"Api-Key": "a" * 32})
    assert r.status_code == 400


def test_subtitles_passes_moviebytesize_to_search(monkeypatch):
    from flask import Flask
    from compat.routes import compat_bp
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)
    search_mock = MagicMock(return_value={"data": []})
    monkeypatch.setattr("compat.routes.service.search", search_mock)
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")

    response = app.test_client().get(
        "/api/v1/subtitles?imdb_id=tt1&languages=en&type=movie"
        "&moviehash=8e245d9679d31e12&moviebytesize=123456789",
        headers={"Api-Key": "a" * 32},
    )

    assert response.status_code == 200
    _, kwargs = search_mock.call_args
    assert kwargs["moviehash"] == "8e245d9679d31e12"
    assert kwargs["moviebytesize"] == 123456789


def test_subtitles_passes_anidb_ids_to_search(monkeypatch):
    from flask import Flask
    from compat.routes import compat_bp
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)
    search_mock = MagicMock(return_value={"data": []})
    monkeypatch.setattr("compat.routes.service.search", search_mock)
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")

    response = app.test_client().get(
        "/api/v1/subtitles?query=Solo.Leveling.S01E12.mkv&type=episode"
        "&season_number=1&episode_number=12&languages=en"
        "&series_anidb_id=17495&series_anidb_episode_id=277518",
        headers={"Api-Key": "a" * 32},
    )

    assert response.status_code == 200
    _, kwargs = search_mock.call_args
    assert kwargs["series_anidb_id"] == 17495
    assert kwargs["series_anidb_episode_id"] == 277518
