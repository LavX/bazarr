# coding=utf-8
"""Apply a user's media identity edit while keeping the filename parse available."""
from datetime import date

from . import identify, repository
from .sports_parse import part_details
from .db import source_transaction
from .service import SourceError


_BASE = dict(identity_state='identified', identity_source='user', identity_candidates=None, identify_pending=0,
             tmdb_id=None, imdb_id=None, tvdb_id=None, poster=None)


def _manual_values(body, row):
    values = dict(_BASE)
    if 'title' in body:
        title = body['title']
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500 or '\0' in title:
            raise SourceError('Enter a title with 1 to 500 characters.')
        title = title.strip()
        values.update(title=title, sort_title=title.lower())
        if row.kind == 'episode':
            values['episode_title'] = title
    if 'year' in body:
        year = body['year']
        if isinstance(year, str) and len(year) == 4 and year.isascii() and year.isdecimal():
            year = int(year)
        lower, upper = (1900, 2100) if row.kind == 'sports_event' else (1800, 2200)
        if type(year) is not int or not lower <= year <= upper:
            raise SourceError(f'Enter a year from {lower} to {upper}.')
        values['year'] = str(year)
    if row.kind == 'episode':
        for name in ('season', 'episode'):
            number = body.get(name, getattr(row, name))
            if type(number) is not int or not 0 <= number < 2**31:
                raise SourceError('Enter one season and one episode number.')
            values[name] = number
    if row.kind == 'sports_event':
        _sports_values(body, row, values)
    if row.kind == 'movie' and not values.get('year', row.year):
        raise SourceError('A movie identity needs a title and a year.')
    return values


def _sports_values(body, row, values):
    if 'event_date' in body:
        value = body['event_date']
        if value is not None:
            try:
                day = date.fromisoformat(value)
                if day.isoformat() != value or not 1900 <= day.year <= 2100:
                    raise ValueError
            except (TypeError, ValueError):
                raise SourceError('Enter an event date as YYYY-MM-DD, from 1900 to 2100.') from None
            if values.get('year', row.year) is None:
                values['year'] = str(day.year)
            if row.season is None and 'season' not in body:
                values['season'] = day.year
        values['event_date'] = value
    for name, column in (('round', 'episode'), ('season', 'season')):
        if name in body:
            number = body[name]
            if number is not None and (type(number) is not int or not 0 <= number < 2**31):
                raise SourceError(f'Enter a nonnegative integer for the {name}.')
            values[column] = number
    if 'part_name' in body:
        part = body['part_name']
        if part is not None and (not isinstance(part, str) or len(part.strip()) > 500 or '\0' in part):
            raise SourceError('Enter a part name with up to 500 characters.')
        name, number = part_details(part.strip() or None if part is not None else None)
        values.update(part_name=name, part_number=number or 0)
    known_title = values.get('title') or row.parsed_title or (row.identity_source == 'user' and row.title)
    complete = values.get('event_date', row.event_date) or (
        values.get('year', row.year) and values.get('episode', row.episode) is not None)
    if not known_title or not complete:
        raise SourceError('A sports event needs a title and a date, or a title, season year and round.')


def _pick(row, candidate_id):
    return next((candidate for candidate in identify.candidates_from_json(row.identity_candidates)
                 if candidate['id'] == candidate_id), None)


def _already_selected(row, candidate_id):
    return row.identity_source == 'user' and row.tmdb_id == str(candidate_id)


def override_identity(row_id, body, *, group=False, expected_kind=None):
    getter = repository.get_group if group else repository.get_item
    if not isinstance(body, dict) or not body:
        raise SourceError('Provide a JSON object with an identity correction.')
    candidate_id = candidate = None
    if 'candidate_id' in body:
        candidate_id = body['candidate_id']
        if len(body) != 1 or type(candidate_id) is not int or candidate_id < 1:
            raise SourceError('Select one TMDB candidate or enter its identity by hand.')
        current = getter(row_id)
        if current is None or (expected_kind is not None and current.kind != expected_kind):
            raise SourceError('No such source item or group.', 404, 'not_found')
        if current.kind not in ('movie', 'show'):
            raise SourceError('This item does not have a TMDB candidate list.')
        if not _already_selected(current, candidate_id):
            if not (candidate := _pick(current, candidate_id)):
                raise SourceError('Choose a TMDB candidate listed for this title.')
            # Recheck the selection when writing. The lookup runs before the transaction.
            values = identify.candidate_identity(candidate, current.kind, user=True)

    with source_transaction() as connection:
        previous = getter(row_id, connection) if not group else None
        if previous is not None and previous.kind == 'episode':
            repository.get_group(previous.group_id, connection, for_update=True)
        row = getter(row_id, connection, for_update=True)
        if row is None or (expected_kind is not None and row.kind != expected_kind):
            raise SourceError('No such source item or group.', 404, 'not_found')
        if row.kind == 'episode' and (previous is None or row.group_id != previous.group_id):
            raise SourceError('The episode moved during the identity correction. Try again.', 409, 'conflict')
        if candidate_id is not None:
            if _already_selected(row, candidate_id):
                return row
            if candidate is None or _pick(row, candidate_id) != candidate:
                raise SourceError('The candidate list changed. Refresh the title and choose again.', 409, 'conflict')
        else:
            allowed = {'title', 'year'}
            if row.kind == 'episode':
                allowed.update(('season', 'episode'))
            if row.kind == 'sports_event':
                allowed.update(('event_date', 'round', 'season', 'part_name'))
            if row.kind not in ('movie', 'show', 'episode', 'sports_event') or body.keys() - allowed:
                raise SourceError('This title does not support one or more of those identity fields.')
            values = _manual_values(body, row)
        if group:
            repository.update_show_identity(row_id, connection, **values)
        else:
            repository.update_item(row_id, connection, **values)
            if row.kind == 'episode':
                repository.sync_episode_identities(connection, item_id=row_id)
        return getter(row_id, connection)
