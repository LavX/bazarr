# coding=utf-8
"""Apply a user's media identity edit while keeping the filename parse available."""
from . import identify, repository
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
        if type(year) is not int or not 1800 <= year <= 2200:
            raise SourceError('Enter a year from 1800 to 2200.')
        values['year'] = str(year)
    if row.kind == 'episode':
        for name in ('season', 'episode'):
            number = body.get(name, getattr(row, name))
            if type(number) is not int or not 0 <= number < 2**31:
                raise SourceError('Enter one season and one episode number.')
            values[name] = number
    if row.kind == 'movie' and not values.get('year', row.year):
        raise SourceError('A movie identity needs a title and a year.')
    return values


def _pick(row, candidate_id):
    return next((candidate for candidate in identify.candidates_from_json(row.identity_candidates)
                 if candidate['id'] == candidate_id), None)


def _already_selected(row, candidate_id):
    return row.identity_source == 'user' and row.tmdb_id == str(candidate_id)


def override_identity(row_id, body, *, group=False):
    getter = repository.get_group if group else repository.get_item
    if not isinstance(body, dict) or not body:
        raise SourceError('Provide a JSON object with an identity correction.')
    candidate_id = candidate = None
    if 'candidate_id' in body:
        candidate_id = body['candidate_id']
        if len(body) != 1 or type(candidate_id) is not int or candidate_id < 1:
            raise SourceError('Select one TMDB candidate or enter its identity by hand.')
        current = getter(row_id)
        if current is None:
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
        if row is None:
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
            if row.kind not in ('movie', 'show', 'episode') or body.keys() - allowed:
                raise SourceError('This title does not support one or more of those identity fields.')
            values = _manual_values(body, row)
        if group:
            repository.update_show_identity(row_id, connection, **values)
        else:
            repository.update_item(row_id, connection, **values)
            if row.kind == 'episode':
                repository.sync_episode_identities(connection, item_id=row_id)
        return getter(row_id, connection)
