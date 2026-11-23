"""Jellyfin and Emby collections through their real bounded HTTP readers."""
import json
from unittest.mock import Mock

import pytest
import requests

from media_servers.http import MediaServerError, MediaServerHTTP


class Response:
    status_code = 200

    def __init__(self, payload=None, *, body=None):
        self.body = json.dumps(payload).encode() if body is None else body
        self.headers = {'Content-Length': str(len(self.body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self._content)


@pytest.fixture(params=['jellyfin', 'emby'])
def client(request, monkeypatch):
    from emby import client as emby
    from jellyfin import client as jellyfin

    # Never permit an accidental network call in a reader fixture.
    monkeypatch.setattr(requests.Session, 'send', Mock(side_effect=AssertionError('Unexpected network request')))
    monkeypatch.setattr(emby, '_accepted_items', Mock(side_effect=AssertionError('Refresh-only helper used')))
    fetch = Mock()
    if request.param == 'jellyfin':
        client = jellyfin.JellyfinClient('http://server.invalid', 'fixture')
        monkeypatch.setattr(client.session, 'get', lambda url, **kwargs: fetch(url, kwargs['params']))
        cap = jellyfin._MAX_RESPONSE_BYTES
    else:
        client = emby.EmbyClient('http://server.invalid', 'fixture')
        monkeypatch.setattr(client.http.session, 'request', lambda method, url, **kwargs: fetch(url, kwargs['params']))
        cap = MediaServerHTTP.RESPONSE_LIMIT
    return client, fetch, cap


def _page(ids, total):
    return Response({'Items': [{'Id': str(item)} for item in ids], 'TotalRecordCount': total})


def test_page_boundary_uses_raw_offsets_and_requested_parent(client):
    browser, fetch, _ = client
    fetch.side_effect = [_page(range(500), 501), _page([500], 501)]
    result = browser.get_item_pages('library-a', ('Movie',))
    assert result.complete and not result.drifted and result.error is None
    assert len(result.items) == 501
    query = [call.args[1] for call in fetch.call_args_list]
    assert [call.args[0] for call in fetch.call_args_list] == ['http://server.invalid/Items'] * 2
    assert [row['StartIndex'] for row in query] == [0, 500]
    assert all(row['Limit'] == 500 and row['Recursive'] == 'true' and row['ParentId'] == 'library-a'
               and row['IncludeItemTypes'] == 'Movie' and row['SortBy'] == 'SortName' for row in query)


def test_paged_body_limit_accepts_exact_size_and_refuses_next_byte(client):
    browser, fetch, cap = client
    encoded = json.dumps({'Items': [{'Id': '1'}], 'TotalRecordCount': 1}).encode()
    body = encoded.ljust(cap, b' ')
    fetch.side_effect = [Response(body=body), Response(body=body + b' ')]
    assert browser.get_item_pages('library-a', ('Episode',)).complete
    rejected = browser.get_item_pages('library-a', ('Episode',))
    assert not rejected.complete and rejected.error and not rejected.items


def test_changed_totals_and_repeated_ids_mark_the_finished_listing_partial(client, monkeypatch):
    from media_servers import item_pages

    browser, fetch, _ = client
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    fetch.side_effect = [_page([10, 20], 4), _page([20, 30], 5), _page([50], 5)]
    result = browser.get_item_pages('library-a', ('Video',))
    assert result.complete and result.drifted and list(result.items) == ['10', '20', '30', '50']


@pytest.mark.parametrize('last_ids, totals', [
    ([10, 20], (4, 4)),
    ([20], (3, 3)),
    ([20], (4, 2)),
])
def test_repeated_terminal_page_cannot_prove_absence(client, monkeypatch, last_ids, totals):
    from media_servers import item_pages

    browser, fetch, _ = client
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    fetch.side_effect = [_page([10, 20], totals[0]), _page(last_ids, totals[1])]
    result = browser.get_item_pages('library-a', ('Movie',))
    assert not result.complete and result.drifted and result.error == 'incomplete_listing'
    assert list(result.items) == ['10', '20']
    assert [call.args[1]['StartIndex'] for call in fetch.call_args_list] == [0, 2]


@pytest.mark.parametrize('pages, expected_ids, drifted', [
    (([],), [], False),
    (([10, 20], []), ['10', '20'], True),
])
def test_empty_terminal_page_can_complete_the_reported_total(client, monkeypatch, pages, expected_ids, drifted):
    from media_servers import item_pages

    browser, fetch, _ = client
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    totals = (0,) if len(pages) == 1 else (3, 2)
    fetch.side_effect = [_page(ids, total) for ids, total in zip(pages, totals)]
    result = browser.get_item_pages('library-a', ('Movie',))
    assert result.complete and result.error is None and result.drifted is drifted
    assert list(result.items) == expected_ids


def test_short_or_failed_second_page_retains_first_page_without_proving_absence(client, monkeypatch):
    from media_servers import item_pages

    browser, fetch, _ = client
    monkeypatch.setattr(item_pages, 'PAGE_SIZE', 2)
    for failure in (_page([30], 4), MediaServerError('timeout')):
        fetch.side_effect = [_page([10, 20], 4), failure]
        result = browser.get_item_pages('library-a', ('Series',))
        assert not result.complete and result.error and set(result.items) >= {'10', '20'}


def test_ids_lookup_requires_a_complete_answer_scoped_to_requested_ids(client):
    browser, fetch, _ = client
    fetch.side_effect = [_page([30], 1), _page([30], 2), _page([50], 1), _page([20, 20], 2)]
    assert browser.lookup_item_ids(['20', '30']) == {'30'}
    assert fetch.call_args.args[1]['Ids'] == '20,30'
    for _ in range(3):
        with pytest.raises(MediaServerError, match='invalid_response'):
            browser.lookup_item_ids(['20', '30'])
