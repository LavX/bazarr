# coding=utf-8
"""Who a group or an item is, worked out from its name.

The scanner asks here whenever a name is read: a new item, a re-pathed one, a group that is new or has moved.
For now the answer is the parse of the name; a lookup against an online index refines it later without the
scanner changing. A group is identified once per move or creation, never once per item.
"""
import os

from .parse import parse_episode_file, parse_movie_file, parse_show_directory


def identify_group(directory):
    """The columns a show's row takes from its directory. Everything the user or an earlier lookup set on the
    identity is replaced: the new name may be another show."""
    parse = parse_show_directory(os.path.basename(directory.rstrip('/\\')) or directory)
    return dict(title=parse.title, sort_title=parse.title.lower(), parsed_title=parse.parsed_title,
                year=str(parse.year) if parse.year else None, identity_source='filename',
                identity_state=parse.identity_state, identity_candidates=None, tvdb_id=None, tmdb_id=None,
                imdb_id=None, poster=None, identify_pending=0)


def identify_item(content_kind, path, group_directory=None, group_state='parsed'):
    """The columns an item takes from its file name: the parse fields, ``scene_name`` and the identity."""
    if content_kind == 'movie':
        parse = parse_movie_file(path)
        return dict(title=parse.title, sort_title=parse.title.lower(), parsed_title=parse.parsed_title,
                    year=str(parse.year) if parse.year else None, identity_source='filename',
                    identity_state=parse.identity_state, scene_name=parse.scene_name, identify_pending=0)
    parse = parse_episode_file(path, group_directory)
    state = 'parsed' if parse.complete and group_state in ('parsed', 'identified') else 'unmatched'
    return dict(title=parse.title, sort_title=parse.title.lower(), parsed_title=parse.parsed_title,
                episode_title=parse.episode_title, season=parse.season, episode=parse.episode,
                identity_source='filename', identity_state=state, scene_name=parse.scene_name,
                identify_pending=0)
