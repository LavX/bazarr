# coding=utf-8
"""Derive identities from filenames, source hints and TMDB metadata."""
import json
import os

from discover import metadata

from .parse import parse_episode_file, parse_movie_file, parse_show_directory


_EMPTY_IDENTITY = dict(identity_source='filename', identity_candidates=None, tmdb_id=None, imdb_id=None,
                       tvdb_id=None, external_id=None, poster=None, identify_pending=0)
_HEALTHY = ('available', 'cached')


def _named(title, year):
    return dict(title=title, sort_title=title.lower(), year=str(year) if year else None)


def _normalized(value):
    try:
        return metadata.normalize_query(value)
    except ValueError:
        return None


def _from_episode_files(directory, paths):
    titles = {}
    years = set()
    for path in sorted(paths):
        episode = parse_episode_file(path, directory)
        title = episode.parsed_title
        key = _normalized(title)
        if key:
            titles.setdefault(key, title)
            if episode.year is not None:
                years.add(episode.year)
        if len(titles) > 1:
            return None, None
    title = next(iter(titles.values()), None)
    year = next(iter(years)) if len(years) == 1 else None
    return title, year


def identify_group(directory, paths=(), *, directory_key=True):
    """Seed a show from the directory first, then from agreeing episode filenames."""
    name = os.path.basename(directory.rstrip('/\\')) or directory
    parsed = parse_show_directory(name) if directory_key else None
    title = parsed.parsed_title if parsed else None
    year = parsed.year if parsed else None
    if not title:
        title, file_year = _from_episode_files(directory if directory_key else None, paths)
        year = file_year or year
    return {**_EMPTY_IDENTITY, **_named(title or name, year), 'parsed_title': title,
            'identity_state': 'parsed' if title else 'unmatched', 'identify_pending': int(bool(title))}


def has_provider_id(hints):
    return bool(hints and (hints.imdb_id or hints.tmdb_id or hints.tvdb_id))


def _apply_hints(values, hints):
    if not hints:
        return values
    values = {**values, 'external_id': hints.external_id}
    if not has_provider_id(hints):
        return values
    title = (hints.title or '').strip() or values['title']
    values.update(_named(title, hints.year or values.get('year')))
    values.update(identity_source='media_server', identity_state='identified', identity_candidates=None,
                  identify_pending=0, imdb_id=hints.imdb_id, tmdb_id=str(hints.tmdb_id) if hints.tmdb_id else None,
                  tvdb_id=hints.tvdb_id, poster=hints.poster)
    return values


def group_hints(row, hints):
    values = dict(title=row.title, year=row.year, parsed_title=row.parsed_title)
    return _apply_hints(values, hints)


def identify_item(content_kind, path, group_directory=None, group_state='parsed', hints=None):
    """Seed an item from a filename and item-level hints."""
    if content_kind == 'movie':
        parsed = parse_movie_file(path)
        values = {**_EMPTY_IDENTITY, **_named(parsed.title, parsed.year), 'parsed_title': parsed.parsed_title,
                  'identity_state': parsed.identity_state, 'scene_name': parsed.scene_name,
                  'identify_pending': int(bool(parsed.parsed_title))}
        return _apply_hints(values, hints)

    parsed = parse_episode_file(path, group_directory)
    values = {**_EMPTY_IDENTITY, **_named(parsed.title, parsed.year), 'parsed_title': parsed.parsed_title,
              'episode_title': parsed.episode_title, 'season': parsed.season, 'episode': parsed.episode,
              'scene_name': parsed.scene_name}
    values = _apply_hints(values, hints)
    if hints:
        if hints.title and hints.title.strip():
            values.update(_named(hints.title.strip(), hints.year or parsed.year), episode_title=hints.title.strip())
        for number in ('season', 'episode'):
            if getattr(hints, number) is not None:
                values[number] = getattr(hints, number)
        if hints.season is not None and hints.episode is not None:
            values['identity_source'] = 'media_server'
    if parsed.multi_episode:
        values.update(season=None, episode=None)
    complete = values['season'] is not None and values['episode'] is not None
    values['identity_state'] = episode_state(group_state, complete, values['identity_source'])
    return values


def episode_state(group_state, complete, source):
    if not complete or group_state == 'unmatched':
        return 'unmatched'
    if group_state == 'ambiguous':
        return 'ambiguous'
    return 'identified' if source in ('media_server', 'user') else 'parsed'


def candidates_from_json(value):
    try:
        items = json.loads(value) if value else []
    except (ValueError, TypeError):
        return []
    return items if isinstance(items, list) else []


def _answered(payload):
    return payload['status'] in _HEALTHY and not payload.get('service_status')


def _candidate(item):
    return {key: item.get(key) for key in ('id', 'title', 'year', 'poster_url', 'imdb_id', 'tvdb_id')}


def candidate_identity(candidate, kind, *, user=False):
    lookup = metadata.movie_details if kind == 'movie' else metadata.show_details
    details = lookup(str(candidate['id']))['data']
    item = details.get('item')
    if _answered(details) and item and item.get('id') == candidate['id']:
        candidate = {**candidate, **{key: value for key, value in _candidate(item).items() if value is not None}}
    elif not _answered(details) and not user:
        return {'identify_pending': 1}
    return {**_named(candidate['title'], candidate.get('year')), 'identity_state': 'identified',
            'identity_source': 'user' if user else 'tmdb', 'identity_candidates': None, 'identify_pending': 0,
            'tmdb_id': str(candidate['id']), 'imdb_id': candidate.get('imdb_id'), 'tvdb_id': candidate.get('tvdb_id'),
            'poster': candidate.get('poster_url')}


def confirm_title(title, year, kind):
    """Return the changes after checking the parsed title. An outage leaves the parse intact."""
    normalized = _normalized(title)
    if not normalized:
        return {'identify_pending': 0}
    search = metadata.search_movies if kind == 'movie' else metadata.search_shows
    original = 'original_title' if kind == 'movie' else 'original_name'
    payload = search(title, for_identification=True)['data']
    if not _answered(payload):
        return {'identify_pending': 1}
    year = int(year) if year else None
    plausible = {}
    for item in payload['items']:
        if normalized not in (_normalized(item['title']), _normalized(item.get(original))):
            continue
        if year is not None and (item['year'] is None or abs(item['year'] - year) > 1):
            continue
        plausible.setdefault(item['id'], _candidate(item))
    if len(plausible) == 1 and not payload.get('truncated'):
        return candidate_identity(next(iter(plausible.values())), kind)
    if len(plausible) > 1:
        return {'identity_state': 'ambiguous', 'identity_candidates': json.dumps(list(plausible.values())),
                'identify_pending': 0}
    return {'identify_pending': 0}
