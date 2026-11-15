# coding=utf-8
"""Translating a sports language from a subtitle already on disk.

Both helpers that decide whether an existing subtitle may be used as a
translation source queried `TableHistorySports.sportsEventId`. That column does
not exist; the history table keys on `event_id` like every other sports table.
The resulting AttributeError was swallowed by the broad `except Exception`
around translate_from_existing in search_event, which logs and falls back to a
provider search, so the feature never worked and never looked broken.

These tests run the queries against a real schema, so a wrong column name fails
here rather than being absorbed at runtime.
"""

from datetime import datetime

import pytest


@pytest.fixture
def sports_history(schema_session, monkeypatch):
    from app.database import (TableArrInstances, TableHistorySports,
                              TableSportsEvents, TableSportsLeagues)
    from sportarr import profile_hooks

    schema_session.add(TableArrInstances(id=42, kind='sportarr', stable_key='sportarr-1',
                                         name='Sportarr', enabled=1, port=1867, api_key='k'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=42, sportarrLeagueId=7,
                                          title='Formula 1', path='/sports'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(id=61, arr_instance_id=42, league_id=51,
                                         sportarrEventId=9, file_id=71,
                                         path='/sports/race.mkv', title='Race'))
    schema_session.flush()
    monkeypatch.setattr(profile_hooks, 'database', schema_session)
    return schema_session, TableHistorySports


def _context():
    from types import SimpleNamespace
    return SimpleNamespace(event_id=61, arr_instance_id=42, league_id=51)


def test_a_low_scoring_source_is_rejected(sports_history, monkeypatch):
    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=datetime.now(), language='hu', score=10))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu') is True


def test_a_good_source_is_accepted(sports_history, monkeypatch):
    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=datetime.now(), language='hu', score=360))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu') is False


def test_a_zero_score_source_is_rejected(sports_history, monkeypatch):
    """0 is a stored score, not a missing row. Treating it as no history let
    the worst possible source through a positive threshold."""
    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=datetime.now(), language='hu', score=0))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu') is True


@pytest.mark.parametrize('local_score', [0, 40])
def test_a_hand_upload_over_a_low_scoring_download_is_accepted(sports_history, monkeypatch, local_score):
    """An upload stores no score, so the older download's low score used to
    decide for a file the upload had replaced."""
    from datetime import timedelta

    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    now = datetime.now()
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=now - timedelta(hours=1), language='hu', score=10))
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=4,
                        timestamp=now, language='hu', score=None))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)
    monkeypatch.setattr(settings.general, 'local_subtitle_score', local_score)

    assert _source_score_below_threshold(_context(), 'hu') is False


def test_a_download_after_a_hand_upload_is_held_to_the_threshold(sports_history, monkeypatch):
    from datetime import timedelta

    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    now = datetime.now()
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=4,
                        timestamp=now - timedelta(hours=1), language='hu', score=None))
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=now, language='hu', score=10))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu') is True


def test_a_hand_upload_of_another_language_does_not_vouch_for_this_one(sports_history, monkeypatch):
    from datetime import timedelta

    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    now = datetime.now()
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=1,
                        timestamp=now - timedelta(hours=1), language='hu', score=10))
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=4,
                        timestamp=now, language='en', score=None))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu') is True


def test_no_history_row_is_treated_as_at_threshold(sports_history):
    """The series path proceeds rather than silently falling back to providers
    when a subtitle predates history tracking or was placed by hand."""
    from sportarr.profile_hooks import _source_score_below_threshold

    assert _source_score_below_threshold(_context(), 'hu') is False


def test_another_owners_history_does_not_count(sports_history, monkeypatch):
    """A second Sportarr's event carries its own history. Reading it would let
    one instance's provider score decide another instance's translation."""
    from app.config import settings
    from app.database import (TableArrInstances, TableSportsEvents,
                              TableSportsLeagues)
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    session.add(TableArrInstances(id=43, kind='sportarr', stable_key='sportarr-4k',
                                  name='Sportarr 4K', enabled=1, port=1868, api_key='k'))
    session.add(TableSportsLeagues(id=52, arr_instance_id=43, sportarrLeagueId=7,
                                   title='Formula 1', path='/sports'))
    session.flush()
    # A sports event id is a primary key, so it is globally unique and the
    # other owner's event gets its own. That is exactly why the history query
    # has to filter on the owner as well: the ids do not collide, but reading
    # a row without checking who owns it still crosses instances.
    session.add(TableSportsEvents(id=62, arr_instance_id=43, league_id=52,
                                  sportarrEventId=9, file_id=71,
                                  path='/sports/race.mkv', title='Race'))
    session.flush()
    session.add(history(event_id=62, league_id=52, arr_instance_id=43, action=1,
                        timestamp=datetime.now(), language='hu', score=10))
    session.commit()
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    # The only low score belongs to the other instance, so this one has no row.
    assert _source_score_below_threshold(_context(), 'hu') is False


def test_a_translation_whose_file_is_gone_does_not_block_a_retry(sports_history):
    """Checking the history row alone would suppress the replacement forever
    once the translated file was deleted or moved."""
    from sportarr.profile_hooks import _already_translated_on_disk

    session, history = sports_history
    session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=6,
                        timestamp=datetime.now(), language='en',
                        subtitles_path='/sports/race.en.srt'))
    session.commit()

    # The path is recorded but nothing is on disk.
    assert _already_translated_on_disk(_context(), 'en') is False


def test_no_translation_history_means_nothing_blocks_it(sports_history):
    from sportarr.profile_hooks import _already_translated_on_disk

    assert _already_translated_on_disk(_context(), 'en') is False


def _history_rows(session, history, rows):
    from datetime import timedelta

    now = datetime.now()
    for offset, (language, action, score, path) in enumerate(rows):
        session.add(history(event_id=61, league_id=51, arr_instance_id=42, action=action,
                            timestamp=now - timedelta(hours=len(rows) - offset), language=language,
                            score=score, subtitles_path=path))
    session.commit()


def test_a_forced_upload_does_not_exempt_a_plain_download_of_the_same_language(sports_history, monkeypatch):
    """Both files carry the language prefix hu. The newest record under that
    prefix is the upload of the forced file, which says nothing about the plain
    file the translation would read."""
    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    _history_rows(session, history, [
        ('hu', 1, 10, '/sports/race.hu.srt'),
        ('hu:forced', 4, None, '/sports/race.hu.forced.srt'),
    ])
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.srt') is True
    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.forced.srt') is False


def test_a_plain_upload_does_not_hide_the_low_score_of_a_forced_download(sports_history, monkeypatch):
    from app.config import settings
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    _history_rows(session, history, [
        ('hu:forced', 1, 10, '/sports/race.hu.forced.srt'),
        ('hu', 4, None, '/sports/race.hu.srt'),
    ])
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.forced.srt') is True
    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.srt') is False


def test_the_source_path_is_matched_through_the_instance_path_mapping(sports_history, monkeypatch):
    from app.config import settings
    from app.database import TableArrInstances
    from sportarr.profile_hooks import _source_score_below_threshold

    session, history = sports_history
    session.get(TableArrInstances, 42).path_mappings = '[["/remote/sports", "/sports"]]'
    _history_rows(session, history, [
        ('hu', 1, 10, '/remote/sports/race.hu.srt'),
        ('hu:forced', 4, None, '/remote/sports/race.hu.forced.srt'),
    ])
    monkeypatch.setattr(settings.translator, 'min_source_score', 90)

    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.srt') is True
    assert _source_score_below_threshold(_context(), 'hu', '/sports/race.hu.forced.srt') is False
