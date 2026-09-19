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
