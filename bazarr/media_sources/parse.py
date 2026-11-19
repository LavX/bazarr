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
