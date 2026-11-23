"""History scope, paging, upgrades and retained events through the real list routes."""

import os
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from media_source_helpers import Lib, add_group, add_item, add_source, stamped_video, video
import test_media_sources_no_change as legacy
from test_media_sources_no_change import HEADERS

arr_library = legacy.arr_library
client = legacy.client
everything_uses_the_test_database = legacy.everything_uses_the_test_database


@pytest.fixture(params=[
    ('movie', '/api/movies/history', 2, 'title'),
    ('episode', '/api/episodes/history', 1, 'episodeTitle'),
])
def history_kind(request):
    return request.param


def _get(client, endpoint, params=None):
    response = client.get(endpoint, headers=HEADERS, query_string=params)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _source_item(session, tmp_path, kind, **source_args):
    source_id, library_id, root = add_source(
        session, tmp_path, content_kind='series' if kind == 'episode' else 'movie', profile_id=1, **source_args)
    group_id = add_group(session, source_id, library_id, title='Live Group') if kind == 'episode' else None
    media = video(root / 'Local.mkv')
    subtitle = media.with_suffix('.en.srt')
    subtitle.write_bytes(b'local subtitles')
    item_id = add_item(session, source_id, library_id, media, kind=kind, group_id=group_id,
                       title='Live Item', season=1, episode=3, subtitles=repr([['en', str(subtitle), 15]]))
    return source_id, item_id, group_id, media, subtitle


def _event(session, kind, source_id, item_id, media, subtitle, timestamp, **changes):
    from media_sources import repository

    event = dict(source_id=source_id, item_id=item_id, item_kind=kind, item_title='Recorded Item',
                 group_title='Recorded Group' if kind == 'episode' else None, source_name='Recorded folder',
                 action=1, description='Source downloaded', provider='fake', subs_id='release',
                 language='en', score=60, score_out_of=120, timestamp=timestamp,
                 video_path=str(media), subtitles_path=str(subtitle), matched="['hash']")
    return repository.insert_history(session, **{**event, **changes})


def test_history_pages_union_keep_orders_owner_filters_and_origins(client, arr_library, tmp_path, monkeypatch,
                                                                  history_kind):
    from app.config import settings
    from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies
    from media_sources import repository

    kind, endpoint, arr_owner, title_key = history_kind
    monkeypatch.setattr(settings.general, 'upgrade_subs', True)
    monkeypatch.setattr(settings.general, 'days_to_upgrade_subs', 7)
    monkeypatch.setattr(settings.general, 'use_embedded_subs', False)
    now = datetime.now().replace(microsecond=123456)
    arr_history, arr_media = (TableHistory, TableEpisodes) if kind == 'episode' else (TableHistoryMovie, TableMovies)
    arr_video = video(tmp_path / 'arr' / 'Arr.mkv')
    arr_subtitle = arr_video.with_suffix('.en.srt')
    arr_subtitle.write_bytes(b'Arr subtitles')
    arr_library.execute(sa.update(arr_media).where(arr_media.id == 1).values(
        path=str(arr_video), subtitles=repr([['en', str(arr_subtitle), 13]])))
    arr_library.execute(sa.update(arr_history).values(
        timestamp=now, video_path=str(arr_video), subtitles_path=str(arr_subtitle), score_out_of=120))
    legacy = client.get(endpoint, headers=HEADERS).data
    assert client.get(endpoint, headers=HEADERS, query_string={'sources': 1}).data == legacy

    source_id, item_id, group_id, media, subtitle = _source_item(arr_library, tmp_path, kind)
    assert item_id == 1
    original = _event(arr_library, kind, source_id, item_id, media, subtitle, now)
    upgrade = _event(arr_library, kind, source_id, item_id, media, subtitle, now,
                     action=3, score=80, upgradedFromId=original)
    embedded = _event(arr_library, kind, source_id, item_id, media, subtitle, now + timedelta(days=1), action=7)
    repository.insert_blacklist(arr_library, item_id=item_id, source_id=source_id, item_kind=kind,
                                provider='fake', subs_id='release', language='en')
    assert client.get(endpoint, headers=HEADERS).data == legacy

    page = _get(client, endpoint, {'sources': 1})
    assert page['total'] == 3
    assert page['source_keys'] == [f'source:{source_id}']
    assert [(row['origin'], row['history_id']) for row in page['data']] == [
        ('arr', original), ('source', upgrade), ('source', original)]
    arr, better, before = page['data']
    assert arr['source_key'] == f'arr:{arr_owner}'
    assert arr['id'] == original == item_id
    assert not arr['blacklisted']
    assert arr['upgradable'] and arr['upgradedFromId'] is None
    assert better['upgradedFromId'] == better['original_id'] == original
    assert better['original'] == {
        'origin': 'source', 'source_key': f'source:{source_id}', 'history_id': original,
        'timestamp_iso': now.isoformat(), 'action': 1, 'provider': 'fake', 'description': 'Source downloaded',
    }
    assert better['upgradable'] and not before['upgradable']
    assert better['score'] == '66.67%'
    assert better['language']['code2'] == 'en'
    assert better['matches'] == ['hash']
    assert better['blacklisted']
    assert better['video_path'] == str(media)
    assert better['subtitles_path'] == str(subtitle)
    assert better[title_key] == 'Recorded Item'
    assert better['source_key'] == f'source:{source_id}'
    assert better['item_id'] == better['id'] == item_id
    assert better['arr_instance_id'] is None
    if kind == 'episode':
        assert better['series_id'] == group_id
        assert better['seriesTitle'] == 'Recorded Group'
        assert better['episode_number'] == '1x3'
        assert better['sonarrEpisodeId'] is None
    else:
        assert better['radarrId'] is None

    for index, row in enumerate(page['data']):
        one = _get(client, endpoint, {'sources': 1, 'start': index, 'length': 1})
        assert one['total'] == 3
        assert one['data'] == [row]
    assert _get(client, endpoint, {'sources': 1, 'include_embedded': 'true', 'length': 1})['data'][0][
        'history_id'] == embedded
    source_key = f'source:{source_id}'
    arr_key = f'arr:{arr_owner}'
    filtered = _get(client, endpoint, [('sources', 1), ('source_key[]', source_key), ('include_upgradable', 'false')])
    assert filtered['total'] == 2
    assert all(row['origin'] == 'source' and not row['upgradable'] for row in filtered['data'])
    assert _get(client, endpoint, [('sources', 1), ('source_key[]', arr_key), ('source_key[]', source_key)]) == page
    for ignored in (source_key, 'source:removed', 'arr:123', 'bad'):
        assert client.get(endpoint, headers=HEADERS, query_string={'source_key[]': ignored}).data == legacy
        assert client.get(endpoint, headers=HEADERS, query_string={'sources': 0, 'source_key[]': ignored}).data == legacy
    assert _get(client, endpoint, {'sources': 1, 'id': item_id})['total'] == 1
    assert client.get(endpoint, headers=HEADERS, query_string={'sources': 1, 'source_key[]': 'bad'}).status_code == 400


def test_history_keeps_deleted_item_and_removed_source_snapshots(client, arr_library, tmp_path, history_kind):
    from media_sources import repository

    kind, endpoint, _, title_key = history_kind
    source_id, item_id, _, media, subtitle = _source_item(arr_library, tmp_path, kind)
    event = _event(arr_library, kind, source_id, item_id, media, subtitle, datetime.now())
    item_endpoint = f'/api/sources/items/{item_id}/history'
    retained = _get(client, endpoint, {'sources': 1, 'source_key[]': f'source:{source_id}'})
    assert retained['data'][0]['id'] == item_id
    assert _get(client, item_endpoint)['total'] == 1

    repository.delete_items([item_id], arr_library)
    retained = _get(client, endpoint, {'sources': 1, 'source_key[]': f'source:{source_id}'})
    assert retained['total'] == 1
    row = retained['data'][0]
    assert row['history_id'] == event
    assert row['id'] is row['item_id'] is None
    assert row[title_key] == 'Recorded Item'
    assert row['video_path'] == str(media)
    assert row['source_name'] == 'Recorded folder'
    if kind == 'episode':
        assert row['series_id'] is None
        assert row['seriesTitle'] == 'Recorded Group'
    assert client.get(item_endpoint, headers=HEADERS).status_code == 404

    repository.delete_source(source_id, arr_library)
    removed = _get(client, endpoint, {'sources': 1, 'source_key[]': 'source:removed'})
    assert removed['total'] == 1
    assert removed['source_keys'] == ['source:removed']
    retained = removed['data'][0]
    assert retained['source_key'] == 'source:removed'
    assert retained['source_id'] is retained['id'] is None
    assert retained['source_name'] == 'Recorded folder'
    assert retained[title_key] == 'Recorded Item'
    assert retained['video_path'] == str(media)
    assert _get(client, endpoint, {'sources': 1, 'source_key[]': f'source:{source_id}'})['total'] == 0
    assert _get(client, '/api/system/media-sources', {'availability': 1})['availability'][
        'movie' if kind == 'movie' else 'series']['retained'] is True


def test_source_item_history_has_its_own_scope_sort_and_paging(client, arr_library, tmp_path, history_kind):
    kind, _, _, _ = history_kind
    source_id, item_id, _, media, subtitle = _source_item(arr_library, tmp_path, kind)
    other = _source_item(arr_library, tmp_path, kind, name='Other folder', folder='Other')
    now = datetime.now()
    first = _event(arr_library, kind, source_id, item_id, media, subtitle, now)
    second = _event(arr_library, kind, source_id, item_id, media, subtitle, now + timedelta(minutes=1),
                    action=3, upgradedFromId=first)
    _event(arr_library, kind, other[0], other[1], other[3], other[4], now + timedelta(minutes=2))
    embedded = _event(arr_library, kind, source_id, item_id, media, subtitle, now + timedelta(minutes=3), action=7)
    endpoint = f'/api/sources/items/{item_id}/history'
    full = _get(client, endpoint)
    assert full['total'] == 2
    assert [row['history_id'] for row in full['data']] == [second, first]
    assert full['data'][0]['upgradedFromId'] == first
    assert all(row['item_id'] == item_id and row['origin'] == 'source' for row in full['data'])
    page = _get(client, endpoint, {'length': 1, 'start': 1, 'include_embedded': 'true'})
    assert page['total'] == 3
    assert page['data'][0]['history_id'] == second
    assert page['data'][0]['original'] == full['data'][0]['original']
    assert page['data'][0]['original']['history_id'] == first
    assert _get(client, endpoint, {'include_embedded': 'true'})['data'][0]['history_id'] == embedded


def test_history_keeps_previous_owner_snapshot_after_scanner_handover(
        client, arr_library, tmp_path, history_kind, monkeypatch):
    from media_sources import repository

    monkeypatch.setattr('media_sources.indexer.probe_item', lambda *args, **kwargs: ({}, None))
    kind, endpoint, _, title_key = history_kind
    content_kind = 'series' if kind == 'episode' else 'movie'
    first = Lib(arr_library, tmp_path, name='First', folder='First', content_kind=content_kind)
    second = Lib(arr_library, tmp_path, name='Second', folder='Second', content_kind=content_kind)
    relative = 'Show/Show - S01E01.mkv' if kind == 'episode' else 'Movie.2026.mkv'
    media = stamped_video(first.root / relative)
    assert first.scan().inserted == 1
    item = first.items()[str(media)]
    assert item.id == 1  # The Arr fixture has the same local ID.
    now = datetime.now()
    previous = _event(arr_library, kind, first.source_id, item.id, media, media.with_suffix('.en.srt'), now)
    assert _get(client, f'/api/sources/items/{item.id}/history')['total'] == 1

    arr_library.execute(sa.update(repository.Sources).where(repository.Sources.c.id == first.source_id)
                        .values(enabled=0, name='Renamed First'))
    adopted_path = second.root / relative
    adopted_path.parent.mkdir(parents=True, exist_ok=True)
    os.link(media, adopted_path)
    assert second.scan().handed_over == 1
    adopted = second.items()[str(adopted_path)]
    assert (adopted.id, adopted.source_id) == (item.id, second.source_id)
    repository.update_item(item.id, arr_library, identity_state='identified', shadow_owner='New owner shadow',
                           monitored=0, season=9, episode=4)
    current = _event(arr_library, kind, second.source_id, item.id, adopted_path, adopted_path.with_suffix('.en.srt'),
                     now + timedelta(minutes=1), action=3, upgradedFromId=previous)
    assert previous == 1  # A source upgrade must not resolve the Arr history event at the same ID.

    item_history = _get(client, f'/api/sources/items/{item.id}/history')
    assert item_history['total'] == 1
    [live] = item_history['data']
    assert live['history_id'] == current
    assert live['item_id'] == item.id
    assert live['identity_state'] == 'identified'
    assert live['shadowed_by'] == 'New owner shadow'
    assert live['monitored'] is False
    assert live['original'] is live['upgradedFromId'] is None
    if kind == 'episode':
        assert live['series_id'] == adopted.group_id
        assert live['episode_number'] == '9x4'

    for owner_key in (f'source:{first.source_id}', 'source:removed'):
        if owner_key == 'source:removed':
            repository.delete_source(first.source_id, arr_library)
            assert repository.get_item(item.id, arr_library).source_id == second.source_id
        page = _get(client, endpoint, {'sources': 1, 'source_key[]': owner_key})
        assert page['total'] == 1
        [old] = page['data']
        assert old['history_id'] == previous
        assert old['source_key'] == owner_key
        assert old['source_name'] == 'Recorded folder'
        assert old[title_key] == 'Recorded Item'
        assert old['video_path'] == str(media)
        for key in ('id', 'item_id', 'identity_state', 'shadowed_by', 'monitored', 'source_kind'):
            assert old[key] is None
        assert old['upgradable'] is False
        if kind == 'episode':
            assert old['seriesTitle'] == 'Recorded Group'
            assert old['series_id'] is old['episode_number'] is None
        assert _get(client, f'/api/sources/items/{item.id}/history') == item_history


def test_arr_history_hydrates_only_a_matching_original_off_page(client, arr_library, tmp_path, history_kind):
    from app.database import TableHistory, TableHistoryMovie

    kind, endpoint, owner, _ = history_kind
    history = TableHistory if kind == 'episode' else TableHistoryMovie
    source_id, item_id, _, media, subtitle = _source_item(arr_library, tmp_path, kind)
    now = datetime.now()
    source_original = _event(arr_library, kind, source_id, item_id, media, subtitle, now - timedelta(minutes=1))
    assert source_original == 1
    values = arr_library.execute(sa.select(history.__table__).where(history.id == 1)).mappings().one()
    arr_library.execute(sa.insert(history).values(
        {**values, 'id': 2, 'timestamp': now, 'action': 3, 'upgradedFromId': 1}))
    args = {'sources': 1, 'length': 1, 'include_upgradable': 'false'}
    [upgrade] = _get(client, endpoint, args)['data']
    assert upgrade['original'] == {
        'origin': 'arr', 'source_key': f'arr:{owner}', 'history_id': 1,
        'timestamp_iso': values['timestamp'].isoformat(), 'action': 1, 'provider': 'alpha', 'description': 'downloaded',
    }

    arr_library.execute(sa.update(history).where(history.id == 1).values(arr_instance_id=99))
    [upgrade] = _get(client, endpoint, args)['data']
    assert upgrade['original'] is upgrade['upgradedFromId'] is None
