# coding=utf-8
"""Reading an embedded subtitle track the way the editor reads a file.

View, Edit and Download on an embedded track row ask the usual content and
download routes with ``?source=embedded``. The track is pulled out of the
container into the extraction cache under the config directory, the cache
translate already uses, and nothing is written next to the video. The editor's
Save is what creates the file, through the create route, in the subtitle
folder when one is set and beside the video otherwise.

The stored subtitles list decides which tracks exist. A language key with no
embedded entry is refused before ffmpeg runs, so a hidden track, a track an
ignore switch drops and a key the indexer never saw stay unreadable here, just
as they are unlisted on the page. The row's codec, when the request carries it,
picks between two text tracks that share one language key, and the extraction
cache keeps each track under its own name.
"""

import ast
import os
from dataclasses import dataclass

from flask import jsonify, make_response

from app.database import TableEpisodes, TableMovies, TableSportsEvents, database, select
from subtitles.indexer.entries import entry_codec, normalize_codec
from subtitles.tools.embedded_extract import ExtractionError, publication_scope, resolve_track
from subtitles.tools.embedded_streams import is_bitmap_codec
from subtitles.tools.translate.batch import extract_embedded_subtitle

from .content import (
    EMBEDDED_SOURCE,
    _get_media_metadata,
    _is_valid_language_code,
    _language_base,
    _language_modifiers,
    new_subtitle_filename,
    read_subtitle_file,
)

_CODEC_LABELS = {
    'pgs': 'PGS',
    'hdmv_pgs_subtitle': 'PGS',
    'vobsub': 'VobSub',
    'dvd_subtitle': 'VobSub',
    'dvd': 'VobSub',
    'dvbsub': 'DVB',
}

# Where each media type's stored subtitles list lives, by the local row id.
_TABLES = {
    'movie': TableMovies,
    'episode': TableEpisodes,
    'sports': TableSportsEvents,
}


@dataclass(frozen=True)
class EmbeddedText:
    """A track read out of the container into the extraction cache."""
    path: str
    filename: str
    codec: str | None
    arr_instance_id: int | None


def split_embedded_language(language_code):
    """(base, hi, forced) for a key an embedded track can carry, else None.

    Embedded tracks are told apart by language, HI and forced only, so a sync
    or combined modifier names a file and never a track.
    """
    if not _is_valid_language_code(language_code):
        return None
    modifiers = [modifier.lower() for modifier in _language_modifiers(language_code)]
    if any(modifier not in ('hi', 'forced') for modifier in modifiers):
        return None
    return _language_base(language_code), 'hi' in modifiers, 'forced' in modifiers


def codec_label(codec):
    return _CODEC_LABELS.get(codec or '', (codec or 'unknown').upper())


def _bitmap_message(codec):
    return (f'This embedded track is image-based ({codec_label(codec)}). '
            'Extract it to a file, or OCR it to text, first.')


def _embedded_entries(raw_subtitles, key):
    """The stored embedded entries under ``key``; None if unreadable.

    ``key`` is the parsed ``(base, hi, forced)`` the request names. Comparing
    the parsed keys rather than the strings as sent is what makes ``en:HI``
    name the stored ``en:hi``: the modifiers are accepted in any case.
    """
    try:
        entries = ast.literal_eval(raw_subtitles or '[]')
    except (ValueError, SyntaxError):
        return None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and not entry[1]
            and split_embedded_language(entry[0]) == key]


def _read(track, language_code, codec=None):
    """Read the row's track under ``language_code``, or an error tuple.

    ``codec`` is the codec the row showed, as the embedded track actions
    already send it: two text tracks with one language key and different
    codecs are two rows, and each reads its own track. Without it a key with
    any text entry reads the first one.
    """
    table = _TABLES[track.media_type]
    raw_subtitles = database.execute(
        select(table.subtitles).where(table.id == track.local_id)).scalar()
    entries = _embedded_entries(raw_subtitles, split_embedded_language(language_code))
    if entries is None:
        return 'Failed to parse subtitles data', 500
    if not entries:
        return 'No embedded track for this language', 404
    # From here on the key is the stored one, not the request's: the two name
    # the same track, and the cache file name is built from the stored one.
    base, hi, forced = split_embedded_language(entries[0][0])
    codecs = [entry_codec(entry) for entry in entries]
    if codec:
        if codec not in codecs:
            return 'No embedded track for this language', 404
        if is_bitmap_codec(codec):
            return _bitmap_message(codec), 415
        row_codec = normalize_codec(codec)
    else:
        text_codecs = [entry for entry in codecs if not is_bitmap_codec(entry)]
        if not text_codecs:
            bitmap_codecs = [entry for entry in codecs if is_bitmap_codec(entry)]
            return _bitmap_message(bitmap_codecs[0] if bitmap_codecs else None), 415
        row_codec = text_codecs[0]
    if not os.path.isfile(track.video_path):
        return 'Video file not found', 404
    extracted = extract_embedded_subtitle(track.video_path, base, track.media_type, hi=hi,
                                          forced=forced, arr_instance_id=track.arr_instance_id,
                                          exact=True, codec=row_codec)
    if not extracted:
        return ('Could not read this embedded track as text. It may be image-based, which '
                'Scan Disk finds out, or ffmpeg failed; the log has the details.'), 422
    return EmbeddedText(path=extracted,
                        filename=new_subtitle_filename(track.video_path, base, hi, forced, '.srt'),
                        codec=row_codec, arr_instance_id=track.arr_instance_id)


def _read_sports(track, language_code, codec=None):
    """_read inside the manual operation every other sports file route uses."""
    from sportarr.errors import SportsNotFound

    try:
        with publication_scope(track) as (scoped_track, _guard, _context):
            return _read(scoped_track, language_code, codec)
    except SportsNotFound:
        return 'Media not found', 404
    except FileNotFoundError:
        return 'Sports video file not found', 404
    except ValueError as exc:
        return str(exc), 409


def read_embedded_track(media_type, media_id, language_code, arr_instance_id=None, codec=None):
    """An EmbeddedText, or a (message, status) tuple.

    The row comes from resolve_track(), the resolver Extract to file uses: the
    owner, the video through that owner's mapping, and a 400 for an upstream
    id two instances share when the request names no owner.
    """
    parts = split_embedded_language(language_code)
    if parts is None:
        return 'Embedded tracks are read by language, HI and forced only', 400
    if media_type not in _TABLES:
        return 'Invalid media type', 400
    try:
        track = resolve_track(media_type, media_id, arr_instance_id, *parts)
    except ExtractionError as error:
        return str(error), error.status
    codec = normalize_codec(codec)
    if media_type == 'sports':
        return _read_sports(track, language_code, codec)
    return _read(track, language_code, codec)


def get_embedded_content(media_type, media_id, language_code, arr_instance_id=None, codec=None):
    """The content route's answer for ``?source=embedded``.

    ``exists`` is False even when an external file with the same language key
    is on disk: the editor opens in create mode, prefilled, and Save goes to
    the create route. No ETag, and no caching, because the answer is not a
    file the next request could conditionally match.
    """
    track = read_embedded_track(media_type, media_id, language_code, arr_instance_id, codec=codec)
    if not isinstance(track, EmbeddedText):
        return track
    try:
        content, encoding = read_subtitle_file(track.path)
        size = os.path.getsize(track.path)
    except ValueError as exc:
        return str(exc), 413
    except OSError:
        return 'Could not read the extracted track', 404
    response_data = {
        'exists': False,
        'source': EMBEDDED_SOURCE,
        'content': content,
        'encoding': encoding,
        'format': 'srt',
        'language': language_code,
        'size': size,
        'lastModified': 0,
        'codec': track.codec,
        'fileName': track.filename,
    }
    response_data.update(
        _get_media_metadata(media_type, media_id, arr_instance_id=track.arr_instance_id) or {})
    response = make_response(jsonify(response_data))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
