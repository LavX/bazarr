# coding=utf-8

import ast
import hashlib
import os

from flask import make_response, jsonify, request
from flask_restx import Resource, Namespace

from app.database import TableEpisodes, TableMovies, TableShows, database, select
from utilities.path_mappings import path_mappings

from ..utils import authenticate

api_ns_subtitle_content = Namespace('SubtitleContent', description='Read subtitle file content')

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

SUBTITLE_EXTENSIONS = {
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
    '.vtt', '.dfxp', '.ttml', '.smi', '.mpl', '.txt',
}

FORMAT_MAP = {
    '.srt': 'srt',
    '.ass': 'ass',
    '.ssa': 'ssa',
    '.vtt': 'vtt',
    '.sub': 'sub',
    '.dfxp': 'dfxp',
    '.ttml': 'ttml',
    '.smi': 'smi',
    '.mpl': 'mpl',
    '.txt': 'txt',
}


def resolve_subtitle_path(media_type, media_id, language_code):
    """Resolve a subtitle file path from a media ID and language code.

    language_code can be like "en", "hu", "en:hi", "en:forced".
    Matches against the language field in the subtitles array.

    Returns (path, language, metadata) on success, or (message, status_code) on failure.
    """
    metadata = {}
    if media_type == 'episode':
        row = database.execute(
            select(TableEpisodes.subtitles, TableEpisodes.path, TableEpisodes.sonarrSeriesId, TableEpisodes.title)
            .where(TableEpisodes.sonarrEpisodeId == media_id)
        ).first()
        if row:
            series_row = database.execute(
                select(TableShows.title).where(TableShows.sonarrSeriesId == row.sonarrSeriesId)
            ).first()
            metadata = {
                'mediaTitle': series_row.title if series_row else None,
                'mediaId': row.sonarrSeriesId,
                'episodeTitle': row.title,
            }
    elif media_type == 'movie':
        row = database.execute(
            select(TableMovies.subtitles, TableMovies.path, TableMovies.title, TableMovies.radarrId)
            .where(TableMovies.radarrId == media_id)
        ).first()
        if row:
            metadata = {
                'mediaTitle': row.title,
                'mediaId': row.radarrId,
            }
    else:
        return 'Invalid media type', 400

    if not row:
        return 'Media not found', 404

    raw_subtitles = row.subtitles
    if not raw_subtitles:
        return 'No subtitles found for this media', 404

    try:
        subtitles_list = ast.literal_eval(raw_subtitles)
    except (ValueError, SyntaxError):
        return 'Failed to parse subtitles data', 500

    if not isinstance(subtitles_list, list):
        return 'Invalid subtitles data', 500

    # Find the subtitle entry matching the language code
    entry = None
    for item in subtitles_list:
        if isinstance(item, list) and len(item) >= 2 and item[0] == language_code:
            # Must have a file path (not an embedded track)
            if item[1] and isinstance(item[1], str) and len(item[1]) > 0:
                entry = item
                break

    if entry is None:
        return f'No subtitle found for language "{language_code}"', 404

    language = entry[0]
    subtitle_path = entry[1]

    if media_type == 'episode':
        subtitle_path = path_mappings.path_replace(subtitle_path)
    else:
        subtitle_path = path_mappings.path_replace_movie(subtitle_path)

    ext = os.path.splitext(subtitle_path)[1].lower()
    if ext not in SUBTITLE_EXTENSIONS:
        return f'File does not have a recognized subtitle extension: {ext}', 400

    if not os.path.isfile(subtitle_path):
        return 'Subtitle file not found on disk', 404

    return subtitle_path, language, metadata


def read_subtitle_file(path):
    """Read a subtitle file and detect its encoding.

    Returns (content_str, encoding) on success.
    Raises ValueError if the file exceeds MAX_FILE_SIZE.
    """
    file_size = os.path.getsize(path)
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f'Subtitle file too large ({file_size} bytes, max {MAX_FILE_SIZE})')

    with open(path, 'rb') as f:
        raw = f.read()

    # BOM detection
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw[3:].decode('utf-8'), 'utf-8-sig'
    if raw.startswith(b'\xff\xfe'):
        return raw[2:].decode('utf-16-le'), 'utf-16-le'
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be'), 'utf-16-be'

    # Try UTF-8
    try:
        return raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        pass

    # charset_normalizer
    try:
        import charset_normalizer
        result = charset_normalizer.from_bytes(raw).best()
        if result is not None:
            return str(result), result.encoding
    except Exception:
        pass

    # Fallback
    return raw.decode('cp1252', errors='replace'), 'cp1252'


def detect_subtitle_format(path):
    """Map a subtitle file extension to a format string."""
    ext = os.path.splitext(path)[1].lower()
    return FORMAT_MAP.get(ext, 'unknown')


def generate_etag(path):
    """Generate an ETag from file mtime and size."""
    stat = os.stat(path)
    tag_input = f"{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.md5(tag_input.encode()).hexdigest()


def _get_subtitle_content(media_type, media_id, language_code):
    """Shared handler for episode and movie subtitle content."""
    result = resolve_subtitle_path(media_type, media_id, language_code)

    # Error tuple
    if isinstance(result[1], int):
        return result[0], result[1]

    subtitle_path, language, metadata = result

    etag = generate_etag(subtitle_path)

    # ETag-based caching
    if_none_match = request.headers.get('If-None-Match')
    if if_none_match and if_none_match.strip('"') == etag:
        return '', 304

    try:
        content, encoding = read_subtitle_file(subtitle_path)
    except ValueError as e:
        return str(e), 413

    stat = os.stat(subtitle_path)
    fmt = detect_subtitle_format(subtitle_path)

    response_data = {
        'content': content,
        'encoding': encoding,
        'format': fmt,
        'language': language,
        'size': stat.st_size,
        'lastModified': stat.st_mtime,
    }
    response_data.update(metadata)

    response = make_response(jsonify(response_data))

    response.headers['ETag'] = f'"{etag}"'
    response.headers['X-Content-Type-Options'] = 'nosniff'

    return response


@api_ns_subtitle_content.route('episodes/<int:sonarrEpisodeId>/subtitles/<language>/content')
class EpisodeSubtitleContent(Resource):
    @authenticate
    def get(self, sonarrEpisodeId, language):
        return _get_subtitle_content('episode', sonarrEpisodeId, language)


@api_ns_subtitle_content.route('movies/<int:radarrId>/subtitles/<language>/content')
class MovieSubtitleContent(Resource):
    @authenticate
    def get(self, radarrId, language):
        return _get_subtitle_content('movie', radarrId, language)
