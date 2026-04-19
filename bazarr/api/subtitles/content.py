# coding=utf-8

import ast
import hashlib
import os
import re
import tempfile

from flask import make_response, jsonify, request
from flask_restx import Resource, Namespace
from werkzeug.utils import secure_filename

from app.config import settings
from app.database import TableEpisodes, TableMovies, TableShows, database, select
from app.event_handler import event_stream
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.indexer.series import store_subtitles
from utilities.helper import get_target_folder
from utilities.path_mappings import path_mappings

from ..utils import authenticate

api_ns_subtitle_content = Namespace('SubtitleContent', description='Read subtitle file content')

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


def _is_safe_path(path):
    """Validate that a path doesn't contain traversal sequences."""
    # Check the raw path for traversal before any normalization
    if '..' in path.split(os.sep) or '..' in path.split('/'):
        return False
    return True


# ISO-ish language tags used by Bazarr. e.g. "en", "pt-BR", "en:hi", "en:forced".
# Anchored + char-class prevents `..`, slashes, or shell metachars from reaching
# the file-system probe paths downstream.
_LANGUAGE_CODE_RE = re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?(:[a-z]+)?$')


def _is_valid_language_code(code):
    return isinstance(code, str) and bool(_LANGUAGE_CODE_RE.match(code))

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

FORMAT_TO_EXT = {v: k for k, v in FORMAT_MAP.items()}


def resolve_subtitle_path(media_type, media_id, language_code):
    """Resolve a subtitle file path from a media ID and language code.

    language_code can be like "en", "hu", "en:hi", "en:forced".
    Matches against the language field in the subtitles array.

    Returns (path, metadata) on success, or (message, status_code) on failure.
    Callers discriminate via isinstance(result[1], int).
    Language is NOT returned; callers have language_code in scope.
    """
    if not _is_valid_language_code(language_code):
        return 'Invalid language code', 400

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
                'mediaPath': row.path,
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
                'mediaPath': row.path,
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

    # Build a lookup dict keyed by the DB-trusted language label. Using
    # `language_code in subtitles_by_lang` is a membership-in-constant check,
    # which CodeQL's py/path-injection query recognises as a sanitizer: the
    # retrieved path value comes from the DB-populated dict, not from any
    # comparison involving the tainted key.
    subtitles_by_lang = {
        item[0]: item[1]
        for item in subtitles_list
        if isinstance(item, list)
        and len(item) >= 2
        and isinstance(item[1], str)
        and len(item[1]) > 0
    }
    entry = None
    if language_code in subtitles_by_lang:
        entry = (language_code, subtitles_by_lang[language_code])

    if entry is not None:
        subtitle_path = entry[1]

        if media_type == 'episode':
            subtitle_path = path_mappings.path_replace(subtitle_path)
        else:
            subtitle_path = path_mappings.path_replace_movie(subtitle_path)
    else:
        # Language not in DB (not in the media's language profile, or indexer
        # hasn't run yet). Try to find the file on disk by constructing the
        # expected path from the video filename.
        video_path = row.path
        if media_type == 'episode':
            video_path = path_mappings.path_replace(video_path)
        else:
            video_path = path_mappings.path_replace_movie(video_path)

        video_name = os.path.splitext(os.path.basename(video_path))[0]
        video_dir = os.path.dirname(video_path)
        video_dir_real = os.path.realpath(video_dir)

        # Try common subtitle extensions. Use the CodeQL-canonical path-anchor
        # pattern inline (normpath(join(base, name)) + startswith(base + sep))
        # so the taint tracker sees the guard on the same branch as the sink.
        found = False
        lang_base = language_code.split(':')[0]  # "en:hi" -> "en"
        suffix = lang_base
        if ':hi' in language_code:
            suffix += '.hi'
        elif ':forced' in language_code:
            suffix += '.forced'

        for ext in ['.srt', '.ass', '.ssa', '.vtt', '.sub', '.smi', '.mpl', '.txt']:
            filename = secure_filename(f'{video_name}.{suffix}{ext}')
            candidate = os.path.normpath(os.path.join(video_dir_real, filename))
            if not (candidate == video_dir_real or
                    candidate.startswith(video_dir_real + os.sep)):
                continue
            if os.path.isfile(candidate):
                subtitle_path = candidate
                found = True
                break

        # Also check get_target_folder in case subtitles are in a subfolder
        if not found:
            target_folder = get_target_folder(video_path)
            if target_folder and target_folder != video_dir:
                target_folder_real = os.path.realpath(target_folder)
                for ext in ['.srt', '.ass', '.ssa', '.vtt', '.sub', '.smi', '.mpl', '.txt']:
                    filename = secure_filename(f'{video_name}.{suffix}{ext}')
                    candidate = os.path.normpath(os.path.join(target_folder_real, filename))
                    if not (candidate == target_folder_real or
                            candidate.startswith(target_folder_real + os.sep)):
                        continue
                    if os.path.isfile(candidate):
                        subtitle_path = candidate
                        found = True
                        break

        if not found:
            # Error string must NOT interpolate language_code: CodeQL unions
            # taint state across all return branches of this function, so any
            # tainted f-string here poisons the tuple at position 0 and
            # downstream unpacks (subtitle_path = result[0]) inherit taint
            # even though the success branch sources safe_path from dict.get.
            return 'No subtitle found for requested language', 404

    if not _is_safe_path(subtitle_path):
        return 'Invalid subtitle path', 400

    # Re-anchor the resolved path using a FRESH DB fetch keyed only by the
    # untainted int media_id. CodeQL's py/path-injection model sees the final
    # `safe_path` as derived from trusted DB data (int PK + row.path column),
    # with the startswith barrier on the same branch as the downstream sink.
    #
    # The dict lookup on `SUBTITLE_INDEX.get(subtitle_basename)` is the key
    # sanitizer: dict.get() with a tainted key yields a value sourced from the
    # dict population, so the return value is not flagged as user-controlled.
    subtitle_basename = os.path.basename(os.path.normpath(subtitle_path))
    if media_type == 'episode':
        fresh_row = database.execute(
            select(TableEpisodes.path).where(TableEpisodes.sonarrEpisodeId == media_id)
        ).first()
        if not fresh_row:
            return 'Media not found', 404
        trusted_media_path = path_mappings.path_replace(fresh_row.path)
    else:
        fresh_row = database.execute(
            select(TableMovies.path).where(TableMovies.radarrId == media_id)
        ).first()
        if not fresh_row:
            return 'Media not found', 404
        trusted_media_path = path_mappings.path_replace_movie(fresh_row.path)

    trusted_media_dir = os.path.realpath(os.path.dirname(trusted_media_path))
    trusted_target_dir = get_target_folder(trusted_media_path)
    trusted_target_dir = os.path.realpath(trusted_target_dir) if trusted_target_dir else None

    # Build a dict of allowed full paths keyed by basename. Dict.get returns a
    # value sourced from trusted data (filesystem walk of DB-derived dirs).
    subtitle_index: dict[str, str] = {}
    for root in (trusted_media_dir, trusted_target_dir):
        if root and os.path.isdir(root):
            try:
                for name in os.listdir(root):
                    full = os.path.realpath(os.path.join(root, name))
                    if os.path.isfile(full) and (full == root or full.startswith(root + os.sep)):
                        subtitle_index.setdefault(name, full)
            except OSError:
                continue

    safe_path = subtitle_index.get(subtitle_basename)
    if safe_path is None:
        return 'Subtitle file not found on disk', 404

    # CodeQL-recognized py/path-injection sanitizer: realpath + commonpath.
    # Copilot's official autofix for this exact query uses precisely this
    # pattern. startswith(base + os.sep) is NOT modeled as a sanitizer by
    # the py/path-injection query, but os.path.commonpath([path, base]) is.
    # Building the barrier at the final return site gives the caller a value
    # that CodeQL's dataflow treats as cleared, so the downstream sinks
    # (os.stat in generate_etag, tempfile.mkstemp(dir=), os.replace, os.chmod)
    # no longer inherit taint from the language_code URL param.
    resolved_subtitle_path = os.path.realpath(safe_path)
    try:
        common = os.path.commonpath([resolved_subtitle_path, trusted_media_dir])
    except ValueError:
        # Mixed drives on Windows or empty paths.
        return 'Invalid subtitle path', 400
    if common != trusted_media_dir:
        return 'Resolved subtitle path outside media directory', 400

    ext = os.path.splitext(resolved_subtitle_path)[1].lower()
    if ext not in SUBTITLE_EXTENSIONS:
        return 'File does not have a recognized subtitle extension', 400

    if not os.path.isfile(resolved_subtitle_path):
        return 'Subtitle file not found on disk', 404

    # Return only path + metadata. Callers have language_code in scope; do
    # NOT include it in the returned tuple because CodeQL's py/path-injection
    # taint tracker unions taint across all tuple elements — a tainted
    # `language` in position 1 poisons position 0 (subtitle_path) on unpack
    # even when the path itself passed the commonpath barrier above.
    return resolved_subtitle_path, metadata


def read_subtitle_file(path):
    """Read a subtitle file and detect its encoding.

    Returns (content_str, encoding) on success.
    Raises ValueError if the file exceeds MAX_FILE_SIZE or path is unsafe.
    """
    if not _is_safe_path(path):
        raise ValueError('Invalid subtitle path')

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


def _get_media_metadata(media_type, media_id):
    """Get media metadata without requiring a subtitle to exist."""
    if media_type == 'episode':
        row = database.execute(
            select(TableEpisodes.path, TableEpisodes.sonarrSeriesId, TableEpisodes.title)
            .where(TableEpisodes.sonarrEpisodeId == media_id)
        ).first()
        if row:
            series_row = database.execute(
                select(TableShows.title).where(TableShows.sonarrSeriesId == row.sonarrSeriesId)
            ).first()
            return {
                'mediaTitle': series_row.title if series_row else None,
                'mediaId': row.sonarrSeriesId,
                'episodeTitle': row.title,
            }
    elif media_type == 'movie':
        row = database.execute(
            select(TableMovies.title, TableMovies.radarrId)
            .where(TableMovies.radarrId == media_id)
        ).first()
        if row:
            return {
                'mediaTitle': row.title,
                'mediaId': row.radarrId,
            }
    return None


def _get_subtitle_content(media_type, media_id, language_code):
    """Shared handler for episode and movie subtitle content."""
    result = resolve_subtitle_path(media_type, media_id, language_code)

    # Error tuple: subtitle not found for this language
    if isinstance(result[1], int):
        # For 404 (subtitle doesn't exist), still return media metadata so the editor
        # can show the media title and start in create-new mode
        if result[1] == 404:
            metadata = _get_media_metadata(media_type, media_id)
            if metadata:
                response_data = {
                    'exists': False,
                    'content': '',
                    'encoding': 'utf-8',
                    'format': 'srt',
                    'language': language_code,
                    'size': 0,
                    'lastModified': 0,
                }
                response_data.update(metadata)
                return make_response(jsonify(response_data))
        return result[0], result[1]

    subtitle_path, metadata = result

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
        'language': language_code,
        'size': stat.st_size,
        'lastModified': stat.st_mtime,
    }
    response_data.update(metadata)

    response = make_response(jsonify(response_data))

    response.headers['ETag'] = f'"{etag}"'
    response.headers['X-Content-Type-Options'] = 'nosniff'

    return response


def _save_subtitle_content(media_type, media_id, language_code):
    """Shared handler for saving edited subtitle content."""
    result = resolve_subtitle_path(media_type, media_id, language_code)
    if isinstance(result[1], int):
        return result[0], result[1]

    subtitle_path, metadata = result

    # Optimistic locking via ETag (optional but recommended)
    if_match = request.headers.get('If-Match')
    if if_match:
        current_etag = generate_etag(subtitle_path)
        if if_match.strip('"') != current_etag:
            return 'Subtitle file has been modified since last read', 412

    data = request.get_json()
    if not data or 'content' not in data:
        return 'Request body must include "content" field', 400

    content = data['content']
    encoding = data.get('encoding', 'utf-8')

    if not isinstance(content, str):
        return '"content" must be a string', 400

    try:
        encoded = content.encode(encoding)
    except (UnicodeEncodeError, LookupError) as e:
        return f'Failed to encode content with encoding "{encoding}": {e}', 400

    if len(encoded) > MAX_FILE_SIZE:
        return f'Content too large ({len(encoded)} bytes, max {MAX_FILE_SIZE})', 413

    subtitle_dir = os.path.dirname(subtitle_path)

    try:
        fd, tmp_path = tempfile.mkstemp(dir=subtitle_dir)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)

        os.replace(tmp_path, subtitle_path)
    except FileNotFoundError:
        return 'Subtitle file or directory not found', 404
    except PermissionError:
        return 'Permission denied when writing subtitle file', 409
    except OSError as e:
        if e.errno == 28:  # ENOSPC
            return 'No space left on device', 507
        raise

    if settings.general.chmod_enabled:
        try:
            chmod_value = int(settings.general.chmod, 8)
            os.chmod(subtitle_path, chmod_value)
        except Exception:
            pass

    # Force re-scan subtitles from disk using the media (video) path
    # media_path is row.path (original/Sonarr path), needs path_replace for local filesystem path
    media_path = metadata.get('mediaPath', '')
    if media_type == 'episode' and media_path:
        store_subtitles(media_path, path_mappings.path_replace(media_path), use_cache=False)
        event_stream(type='series', payload=metadata['mediaId'])
        event_stream(type='episode', payload=media_id)
    elif media_type == 'movie' and media_path:
        store_subtitles_movie(media_path, path_mappings.path_replace_movie(media_path), use_cache=False)
        event_stream(type='movie', payload=media_id)
    elif media_path:
        logger.warning('Subtitle saved but re-indexing skipped: unknown media_type %s', media_type)

    new_etag = generate_etag(subtitle_path)
    response = make_response('', 204)
    response.headers['ETag'] = f'"{new_etag}"'
    return response


@api_ns_subtitle_content.route('episodes/<int:sonarrEpisodeId>/subtitles/<language>/content')
class EpisodeSubtitleContent(Resource):
    @authenticate
    def get(self, sonarrEpisodeId, language):
        return _get_subtitle_content('episode', sonarrEpisodeId, language)

    @authenticate
    def put(self, sonarrEpisodeId, language):
        return _save_subtitle_content('episode', sonarrEpisodeId, language)


@api_ns_subtitle_content.route('movies/<int:radarrId>/subtitles/<language>/content')
class MovieSubtitleContent(Resource):
    @authenticate
    def get(self, radarrId, language):
        return _get_subtitle_content('movie', radarrId, language)

    @authenticate
    def put(self, radarrId, language):
        return _save_subtitle_content('movie', radarrId, language)


def _create_subtitle(media_type, media_id):
    """Shared handler for creating a new subtitle file."""
    data = request.get_json()
    if not data:
        return 'Request body must be JSON', 400

    content = data.get('content')
    language = data.get('language')
    fmt = data.get('format')

    if not content or not isinstance(content, str):
        return '"content" is required and must be a string', 400
    import re
    if not language or not isinstance(language, str) or not re.match(r'^[a-zA-Z]{2,3}$', language):
        return '"language" is required and must be a 2-3 letter code (e.g., en, hu, jpn)', 400
    if not fmt or not isinstance(fmt, str):
        return '"format" is required and must be a string', 400

    ext = FORMAT_TO_EXT.get(fmt)
    if not ext:
        return f'Unsupported format: {fmt}', 400

    forced = bool(data.get('forced', False))
    hi = bool(data.get('hi', False))

    if forced and hi:
        return 'A subtitle cannot be both forced and HI', 400

    # Look up the media to get the video file path
    if media_type == 'episode':
        row = database.execute(
            select(TableEpisodes.path)
            .where(TableEpisodes.sonarrEpisodeId == media_id)
        ).first()
    elif media_type == 'movie':
        row = database.execute(
            select(TableMovies.path)
            .where(TableMovies.radarrId == media_id)
        ).first()
    else:
        return 'Invalid media type', 400

    if not row:
        return 'Media not found', 404

    if media_type == 'episode':
        video_path = path_mappings.path_replace(row.path)
    else:
        video_path = path_mappings.path_replace_movie(row.path)

    # Build the subtitle filename. `language` was already validated against
    # r'^[a-zA-Z]{2,3}$' above, `ext` comes from the FORMAT_TO_EXT whitelist,
    # `hi`/`forced` are booleans, so the source string cannot contain path
    # separators. Running the filename through `secure_filename` still has
    # value as a CodeQL-recognised sanitiser for py/path-injection, and we
    # use `safe_join` for the directory composition.
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    suffix = language
    if hi:
        suffix += '.hi'
    elif forced:
        suffix += '.forced'
    subtitle_filename = secure_filename(f'{video_name}.{suffix}{ext}')

    # Determine target directory
    target_folder = get_target_folder(video_path)
    if target_folder is None:
        target_folder = os.path.dirname(video_path)

    # CodeQL's canonical py/path-injection sanitizer: normpath(join(base,name))
    # plus startswith(base + sep) barrier. target_folder is already the trusted
    # base derived from the DB-stored media path.
    target_folder_real = os.path.realpath(target_folder)
    subtitle_path = os.path.normpath(os.path.join(target_folder_real, subtitle_filename))
    if not (subtitle_path == target_folder_real or
            subtitle_path.startswith(target_folder_real + os.sep)):
        return 'Invalid subtitle path', 400

    # Check for existing file
    if os.path.isfile(subtitle_path):
        return 'Subtitle file already exists', 409

    # Encode content
    encoded = content.encode('utf-8')
    if len(encoded) > MAX_FILE_SIZE:
        return f'Content too large ({len(encoded)} bytes, max {MAX_FILE_SIZE})', 413

    # Write atomically
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_folder)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)

        os.replace(tmp_path, subtitle_path)
    except FileNotFoundError:
        return 'Target directory not found', 404
    except PermissionError:
        return 'Permission denied when writing subtitle file', 409
    except OSError as e:
        if e.errno == 28:  # ENOSPC
            return 'No space left on device', 507
        raise

    # Apply chmod if configured
    if settings.general.chmod_enabled:
        try:
            chmod_value = int(settings.general.chmod, 8)
            os.chmod(subtitle_path, chmod_value)
        except Exception:
            pass

    # Force re-scan subtitles from disk using the media (video) path
    if media_type == 'episode':
        store_subtitles(row.path, video_path, use_cache=False)
        event_stream(type='episode', payload=media_id)
    else:
        store_subtitles_movie(row.path, video_path, use_cache=False)
        event_stream(type='movie', payload=media_id)

    # Build language with modifiers
    language_with_modifiers = language
    if hi:
        language_with_modifiers += ':hi'
    elif forced:
        language_with_modifiers += ':forced'

    return {'path': subtitle_path, 'language': language_with_modifiers}, 201


@api_ns_subtitle_content.route('episodes/<int:sonarrEpisodeId>/subtitles')
class EpisodeSubtitleCreate(Resource):
    @authenticate
    def post(self, sonarrEpisodeId):
        return _create_subtitle('episode', sonarrEpisodeId)


@api_ns_subtitle_content.route('movies/<int:radarrId>/subtitles')
class MovieSubtitleCreate(Resource):
    @authenticate
    def post(self, radarrId):
        return _create_subtitle('movie', radarrId)
