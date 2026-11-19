# coding=utf-8
"""What a file name says about the movie it holds.

A title read from a file name is a guess, so an item is ``parsed`` only when the parse is complete (a title and a
year). Anything less is ``unmatched``: it is listed, and it can be searched by hand, but a guessed title alone
never drives an automatic search.
"""
import os
from dataclasses import dataclass
from typing import Optional

from guessit import guessit


@dataclass(frozen=True)
class MovieParse:
    title: str
    parsed_title: Optional[str]
    year: Optional[int]
    scene_name: str
    identity_state: str


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def parse_movie_file(path) -> MovieParse:
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    try:
        guessed = guessit(name, options={'type': 'movie'})
    except Exception:
        guessed = {}
    parsed_title = _first(guessed.get('title'))
    year = _first(guessed.get('year'))
    year = year if isinstance(year, int) and 1800 <= year <= 2200 else None
    parsed_title = str(parsed_title).strip() if parsed_title else None
    # Never empty: the display title falls back to the file stem.
    title = parsed_title or stem
    return MovieParse(title=title, parsed_title=parsed_title, year=year, scene_name=stem,
                      identity_state='parsed' if parsed_title and year else 'unmatched')


@dataclass(frozen=True)
class EpisodeParse:
    title: str                   # the display title: the episode's own title, else the file stem
    parsed_title: Optional[str]  # the show title the file name carries, if any
    episode_title: Optional[str]
    season: Optional[int]
    episode: Optional[int]
    scene_name: str
    multi_episode: bool

    @property
    def complete(self):
        """A season and one episode number. Multi-episode files are not complete in this version."""
        return self.season is not None and self.episode is not None and not self.multi_episode


@dataclass(frozen=True)
class GroupParse:
    title: str                   # never empty: the parsed title, else the directory name
    parsed_title: Optional[str]
    year: Optional[int]
    identity_state: str


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_episode_file(path, group_directory=None) -> EpisodeParse:
    """What the file name says about the episode it holds.

    The part of the path below the show directory is read, so ``Season 2/E05.mkv`` has its season; the show's own
    title comes from the directory, not from here."""
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    relative = name
    if group_directory:
        below = os.path.relpath(path, group_directory)
        if not below.startswith('..'):
            relative = below.replace(os.sep, '/')
    try:
        guessed = guessit(relative, options={'type': 'episode'})
    except Exception:
        guessed = {}
    parsed_title = _first(guessed.get('title'))
    parsed_title = str(parsed_title).strip() if parsed_title else None
    episode_title = _first(guessed.get('episode_title'))
    episode_title = str(episode_title).strip() if episode_title else None
    episodes = guessed.get('episode')
    multi = isinstance(episodes, (list, tuple)) and len(episodes) > 1
    seasons = guessed.get('season')
    if isinstance(seasons, (list, tuple)):
        multi = multi or len(seasons) > 1
    return EpisodeParse(title=episode_title or stem, parsed_title=parsed_title, episode_title=episode_title,
                        season=_number(_first(seasons)), episode=_number(_first(episodes)), scene_name=stem,
                        multi_episode=multi)


def parse_show_directory(name) -> GroupParse:
    """The show a first-level directory names. A title read from a directory name is a guess like any other:
    the group is ``parsed`` when one was found, ``unmatched`` when the name gives none (a bare year)."""
    name = os.path.basename(name.rstrip('/\\')) or name
    try:
        guessed = guessit(name, options={'type': 'episode'})
    except Exception:
        guessed = {}
    parsed_title = _first(guessed.get('title'))
    parsed_title = str(parsed_title).strip() if parsed_title else None
    year = _first(guessed.get('year'))
    year = year if isinstance(year, int) and 1800 <= year <= 2200 else None
    return GroupParse(title=parsed_title or name, parsed_title=parsed_title, year=year,
                      identity_state='parsed' if parsed_title else 'unmatched')
