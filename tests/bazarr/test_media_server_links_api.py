# coding=utf-8
"""The link endpoints: authenticated, strict about what they are asked, never a key."""

import pytest
from flask import Flask

HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
ROOT = '/api/system/media-server-links'
TARGET = {'instance_id': 'emby-A', 'kind': 'emby', 'display_name': 'Emby'}


@pytest.fixture
def links_api(monkeypatch):
    from api import api_bp
    from app.config import settings as app_settings
    from media_servers import links
    monkeypatch.setitem(app_settings.auth, 'apikey', 'synthetic-bazarr-key')
    monkeypatch.setattr(links, 'destination_rows', lambda: [TARGET])
    monkeypatch.setattr(links, 'candidate_rows', lambda media_type, local_id: None if local_id == 404 else [TARGET])
    monkeypatch.setattr(links, 'resolve', lambda instance_id, media_type, local_id: None if local_id == 404 else {
        'instance_id': instance_id, 'state': 'resolved', 'url': 'https://emby.example.com/web/index.html#!/item?id=1'})
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


@pytest.mark.parametrize('path', ['/destinations', '/candidates?media_type=movie&id=1',
                                  '/resolve?instance_id=emby-A&media_type=movie&id=1'])
def test_every_endpoint_needs_authentication(links_api, path):
    assert links_api.get(ROOT + path).status_code == 401


def test_the_answers_carry_labels_states_and_urls_and_never_a_key(links_api):
    assert links_api.get(ROOT + '/destinations', headers=HEADERS).json == {'data': [TARGET]}
    assert links_api.get(ROOT + '/candidates?media_type=episode&id=9', headers=HEADERS).json == {'data': [TARGET]}
    answer = links_api.get(ROOT + '/resolve?instance_id=emby-A&media_type=series&id=5', headers=HEADERS)
    assert answer.status_code == 200 and answer.json['state'] == 'resolved'
    for path in ('/destinations', '/candidates?media_type=movie&id=1',
                 '/resolve?instance_id=emby-A&media_type=movie&id=1'):
        body = links_api.get(ROOT + path, headers=HEADERS).get_data(as_text=True)
        assert 'api_key' not in body and 'apikey' not in body and 'synthetic-key' not in body


@pytest.mark.parametrize('query', ['media_type=show&id=1', 'media_type=movie&id=x', 'media_type=movie&id=0',
                                   'media_type=movie&id=%C2%B2', 'media_type=movie&id=-1', 'media_type=movie',
                                   'id=1', 'media_type=movie&id=12345678901'])
def test_a_request_that_does_not_name_one_row_is_refused(links_api, query):
    assert links_api.get(ROOT + '/candidates?' + query, headers=HEADERS).status_code == 400
    assert links_api.get(ROOT + '/resolve?instance_id=emby-A&' + query, headers=HEADERS).status_code == 400


def test_a_resolve_without_a_destination_is_refused_and_an_unknown_row_is_404(links_api):
    assert links_api.get(ROOT + '/resolve?media_type=movie&id=1', headers=HEADERS).status_code == 400
    assert links_api.get(ROOT + '/candidates?media_type=movie&id=404', headers=HEADERS).status_code == 404
    assert links_api.get(ROOT + '/resolve?instance_id=emby-A&media_type=movie&id=404',
                         headers=HEADERS).status_code == 404
