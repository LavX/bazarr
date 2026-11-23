"""Date, round, session and title from a recording in a league folder."""

import os
import re
from dataclasses import dataclass
from datetime import date

from subliminal_patch.core import guessit


_DATE = re.compile(r'(?<!\d)(\d{4})([-._ ])(\d{2})\2(\d{2})(?!\d)')
_DAY_FIRST = re.compile(r'(?<!\d)\d{2}[-._ ]\d{2}[-._ ]\d{4}(?!\d)')
_ROUND = re.compile(
    r'(?<![^\W_])(?:S(?P<season>\d{1,4})E(?P<serial>\d{1,4})|'
    r'(?:Round[ ._-]+|Week[ ._-]+|R)(?P<number>\d{1,4}))(?![^\W_])', re.IGNORECASE)
_SEASON_DIRECTORY = re.compile(r'(?:Season[ ._-]+)?(\d{1,4})(?:-(\d{4}))?', re.IGNORECASE)
_SESSIONS = ('Qualifying', 'Sprint', 'Race', 'Practice', 'FP1', 'FP2', 'FP3', 'Pre-Race', 'Post-Race')
_CANONICAL_SESSIONS = {value.lower(): (value, number) for number, value in enumerate(_SESSIONS, 1)}
_PART = re.compile(r'(?:^|[\s-])(?:Part[ -]+(?P<part>\d{1,4})|Pt(?P<short>\d{1,4})|'
                   r'(?P<session>Pre[- ]Race|Post[- ]Race|Qualifying|Sprint|Race|Practice|FP[123]))$',
                   re.IGNORECASE)
_ATTRIBUTES = frozenset({'screen_size', 'source', 'video_codec', 'audio_codec', 'audio_channels',
                         'audio_profile', 'video_profile', 'color_depth', 'release_group', 'streaming_service',
                         'other', 'bit_rate', 'frame_rate'})


@dataclass(frozen=True)
class SportsParse:
    title: str
    parsed_title: str | None
    year: int | None
    season: int | None
    round: int | None
    event_date: str | None
    part_name: str | None
    part_number: int | None
    scene_name: str

    @property
    def identity_state(self):
        complete = self.event_date or (self.year is not None and self.round is not None)
        return 'parsed' if self.parsed_title and complete else 'unmatched'


def season_year(value):
    number = int(value)
    return number if 1900 <= number <= 2100 else None


def _event_date(stem):
    for match in _DATE.finditer(stem):
        year, _, month, day = match.groups()
        if season_year(year) is None:
            continue
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def _season_directory(path, league_directory):
    if not league_directory:
        return None
    parts = os.path.relpath(path, league_directory).split(os.sep)
    if len(parts) < 2 or parts[0] == '..':
        return None
    match = _SEASON_DIRECTORY.fullmatch(parts[0])
    if match is None:
        return None
    value, end = match.groups()
    if end is not None and (len(value) != 4 or not season_year(value) or not season_year(end)):
        return None
    if len(value) == 4:
        return season_year(value)
    return int(value) if match[0].lower().startswith('season') else None


def _without_release_attributes(filename, stem):
    try:
        attributes = guessit(filename, options={'type': 'movie', 'advanced': True})
    except Exception:
        return stem
    text = list(stem)
    for name in _ATTRIBUTES.intersection(attributes):
        matches = attributes[name]
        for match in matches if isinstance(matches, list) else (matches,):
            start, end = match.span
            text[start:min(end, len(stem))] = [' '] * max(0, min(end, len(stem)) - start)
    return ''.join(text)


def _spaces(value):
    return re.sub(r'[\s._]+', ' ', value).strip(' -[]()')


def _without_league(stem, league):
    words = re.findall(r'[^\W_]+', league or '')
    if not words:
        return stem
    pattern = r'(?<!\w)' + r'[\s.-]*'.join(map(re.escape, words)) + r'(?!\w)'
    return _spaces(re.sub(pattern, ' ', stem, flags=re.IGNORECASE))


def part_details(value):
    """Return the canonical session name and ordinal for a recognized part."""
    if not value or not (match := _PART.fullmatch(value)):
        return value, None
    if match['session']:
        key = match['session'].lower().replace(' ', '-')
        return _CANONICAL_SESSIONS[key]
    number = int(match['part'] or match['short'])
    return f'Part {number}', number


def parse_sports_file(path, league_directory=None):
    filename = os.path.basename(path)
    stem = os.path.splitext(filename)[0]
    calendar_date = _event_date(stem)
    round_match = _ROUND.search(stem)
    round_number = int(round_match['serial'] or round_match['number']) if round_match else None
    serial = round_match['season'] if round_match else None
    directory_season = _season_directory(path, league_directory)
    year = season_year(serial) if serial and len(serial) == 4 else None
    if year is None and directory_season is not None:
        year = season_year(directory_season)
    if year is None and calendar_date:
        year = calendar_date.year

    text = _without_release_attributes(filename, stem)
    text = _ROUND.sub(' ', _DAY_FIRST.sub(' ', _DATE.sub(' ', text)))
    league = os.path.basename(os.path.normpath(league_directory)) if league_directory else None
    title = _without_league(_spaces(text), league)
    part = _PART.search(title)
    name, number = part_details(part[0].strip(' -')) if part else (None, None)
    if part:
        title = _spaces(title[:part.start()])
    return SportsParse(title or stem, title or None, year, directory_season if directory_season is not None else year,
                       round_number, calendar_date.isoformat() if calendar_date else None, name, number, stem)
