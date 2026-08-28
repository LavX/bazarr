# coding=utf-8

import ast
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile

from flask import request, send_file
from flask_restx import Resource, Namespace

from app.database import TableEpisodes, TableMovies, TableShows, database, select
from arr_instances.resolution import scoped
from utilities.helper import get_target_folder
from utilities.path_mappings import path_mappings

from .content import (
    LANGUAGE_BASE_TAG_FRAGMENT,
    SUBTITLE_EXTENSIONS,
    _language_base,
    _request_arr_instance_id,
    resolve_subtitle_path,
)
from ..utils import authenticate

api_ns_subtitle_download = Namespace('SubtitleDownload', description='Download subtitle files')

# Uncompressed ceiling for a bundle, enforced while streaming each file (a
# file growing after collection cannot overshoot it).
MAX_BUNDLE_UNCOMPRESSED_SIZE = 256 * 1024 * 1024

# The archive is spooled: it stays in memory up to this size and spills to a
# temp file beyond it, so a large bundle never holds hundreds of MB resident.
BUNDLE_SPOOL_MEMORY_CEILING = 16 * 1024 * 1024

_BUNDLE_COPY_CHUNK = 1 << 20

# Per-entry staging buffer: spills to disk past this, so reading a source is
# fully guarded before anything touches the archive.
_ENTRY_STAGE_MEMORY_CEILING = 4 * 1024 * 1024

# Base language filter for bundles: "en", "pt-BR". Variants (hi/forced/sync)
# of the base language are included by design, so modifiers are not accepted.
# The fragment is owned by content.py so the grammar cannot drift; \Z (not $)
# so a trailing newline does not pass.
_LANGUAGE_FILTER_RE = re.compile(r'^' + LANGUAGE_BASE_TAG_FRAGMENT + r'\Z')

# Zip timestamps can only represent 1980..2107; files outside that range
# (epoch-0 after restores/rsync/SMB, or corrupt future metadata) get clamped
# instead of failing the whole bundle.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_LAST_TIMESTAMP = (2107, 12, 31, 23, 59, 58)


class BundleTooLargeError(Exception):
    pass


def safe_filename_component(value, fallback):
    """Reduce a title to something usable inside a download filename.

    The strip set includes ';' because the frontend parses the plain quoted
    Content-Disposition form, where an embedded semicolon ends the parameter
    for naive parsers.
    """
    cleaned = re.sub(r'[\\/:;*?"<>|\x00-\x1f]', '_', str(value or ''))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().strip('.')
    return cleaned or fallback


def sanitize_arcname_component(name):
    """Neutralize separators in a single archive-entry component.

    os.path.basename does not split on backslash on POSIX, so a literal
    backslash-bearing filename (or an unmapped Windows path) would otherwise
    produce entries some extractors treat as nested paths.
    """
    return re.sub(r'[\\/]', '_', name)


def unique_arcname(used, arcname):
    """Return arcname, suffixed with ' (n)' before the extension if taken.

    Collisions are tracked case-insensitively: names differing only by case
    would overwrite each other when the zip is extracted on a
    case-insensitive filesystem."""
    if arcname.casefold() not in used:
        used.add(arcname.casefold())
        return arcname
    stem, ext = os.path.splitext(arcname)
    counter = 2
    while f'{stem} ({counter}){ext}'.casefold() in used:
        counter += 1
    result = f'{stem} ({counter}){ext}'
    used.add(result.casefold())
    return result


def iter_external_subtitles(raw_subtitles):
    """Parse a subtitles DB column into (language, reversed_path) pairs.

    Embedded tracks carry a null/empty path and are skipped: there is no file
    on disk to hand out.
    """
    if not raw_subtitles:
        return []
    try:
        parsed = ast.literal_eval(raw_subtitles)
    except (ValueError, SyntaxError):
        return []
    if not isinstance(parsed, list):
        return []
    return [
        (item[0], item[1])
        for item in parsed
        if (isinstance(item, list)
            and len(item) >= 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
            and item[1])
    ]


def _entry_mtime(disk_path):
    try:
        return os.path.getmtime(disk_path)
    except OSError:
        return -1.0


def dedupe_language_entries(pairs, to_disk_path):
    """Keep one path per exact language label, preferring the newest file.

    Mirrors resolve_subtitle_path: a combined output can be indexed twice for
    one language when a stale file in another format lingers next to the
    current output; the most recently written file is the current one.
    """
    by_language = {}
    for language_value, reversed_path in pairs:
        existing = by_language.get(language_value)
        if existing is None:
            by_language[language_value] = reversed_path
        elif existing != reversed_path:
            if _entry_mtime(to_disk_path(reversed_path)) > _entry_mtime(to_disk_path(existing)):
                by_language[language_value] = reversed_path
    return list(by_language.items())


def matches_language(language_value, language_filter):
    """True when the entry's base language matches the filter (or no filter).

    The DB language label can carry modifiers ("en:hi", "en:forced",
    "en:sync-ffsubsync"); a filter of "en" includes all of them.
    """
    if not language_filter:
        return True
    return _language_base(language_value).lower() == language_filter.lower()


def trusted_roots_for(video_disk_path):
    """The directories a media item's subtitle files may legitimately live in:
    the media directory and the configured subtitle target folder."""
    roots = []
    media_dir = os.path.dirname(video_disk_path)
    if media_dir:
        roots.append(os.path.realpath(media_dir))
    try:
        # Read-only: a bundle request must not create subtitle folders
        # across the library as a side effect.
        target = get_target_folder(video_disk_path, create=False)
    except Exception:
        target = None
    if target and isinstance(target, str):
        real_target = os.path.realpath(target)
        if real_target not in roots:
            roots.append(real_target)
    return roots


def resolve_bundle_path(disk_path, trusted_roots):
    """Containment + allowlist barrier for bundle entries, mirroring the
    guarantees resolve_subtitle_path gives the single-file endpoint: the
    realpath (symlinks resolved) must sit under a trusted directory and carry
    a recognized subtitle extension. Returns the realpath, or None."""
    try:
        real = os.path.realpath(disk_path)
    except OSError:
        return None
    # The allowlist runs on the RESOLVED path: an indexed .srt that is really
    # a symlink to Movie.nfo must not pass on the link's own extension.
    if os.path.splitext(real)[1].lower() not in SUBTITLE_EXTENSIONS:
        return None
    for root in trusted_roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return real
        except ValueError:
            continue
    return None


def open_validated_subtitle(path):
    """Open a previously validated path for reading, refusing a replacement
    symlink at the final component (O_NOFOLLOW where the platform has it) and
    anything that is not a regular file. Validation and opening are separate
    operations, so a writer in the media directory could swap the file in the
    window between them; this closes that window at the open."""
    flags = (os.O_RDONLY
             | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_BINARY', 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f'not a regular file: {path}')
        if not hasattr(os, 'O_NOFOLLOW'):
            # No O_NOFOLLOW on this platform (Windows): require the directory
            # entry to be the very file that was opened, so a replacement
            # symlink cannot smuggle in an outside target.
            entry = os.lstat(path)
            if stat.S_ISLNK(entry.st_mode) or (
                    (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
                raise OSError(f'path was replaced: {path}')
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


def _collect_bundle_entries(rows, media_type, language=None, season=None):
    """Build (arcname, disk_path) pairs for rows with .path, .subtitles and
    .arr_instance_id (episode rows also carry .season)."""
    entries = []
    mapped = {}

    def map_path(raw_path, arr_instance_id):
        # One mapping resolution per unique path: multi-episode files share a
        # path across rows, and the underlying lookup is uncached.
        key = (raw_path, arr_instance_id)
        if key not in mapped:
            mapped[key] = path_mappings.path_replace_instance(
                raw_path, arr_instance_id, media_type)
        return mapped[key]

    for row in rows:
        if season is not None and getattr(row, 'season', None) != season:
            continue
        pairs = dedupe_language_entries(
            iter_external_subtitles(row.subtitles),
            lambda raw, instance=row.arr_instance_id: map_path(raw, instance))
        if not pairs:
            continue
        if not row.path:
            continue
        roots = trusted_roots_for(map_path(row.path, row.arr_instance_id))
        for language_value, reversed_path in pairs:
            if not matches_language(language_value, language):
                continue
            real_path = resolve_bundle_path(
                map_path(reversed_path, row.arr_instance_id), roots)
            if real_path is None:
                continue
            component = sanitize_arcname_component(os.path.basename(real_path))
            if media_type == 'episode':
                arcname = f'Season {row.season:02d}/{component}'
            else:
                arcname = component
            entries.append((arcname, real_path))
    return entries


def collect_series_bundle_entries(episode_rows, season=None, language=None):
    return _collect_bundle_entries(episode_rows, 'episode',
                                   language=language, season=season)


def collect_movie_bundle_entries(movie_row, language=None):
    return _collect_bundle_entries([movie_row], 'movie', language=language)


def _zip_date_time(mtime):
    try:
        parts = time.localtime(mtime)
    except (OSError, OverflowError, ValueError):
        return _ZIP_EPOCH
    if parts.tm_year < 1980:
        return _ZIP_EPOCH
    if parts.tm_year > 2107:
        return _ZIP_LAST_TIMESTAMP
    return (parts.tm_year, parts.tm_mon, parts.tm_mday,
            parts.tm_hour, parts.tm_min, parts.tm_sec)


def build_subtitle_bundle(entries, max_total_size=MAX_BUNDLE_UNCOMPRESSED_SIZE):
    """Zip the given (arcname, disk_path) pairs into an in-memory buffer.

    Files that vanish or error between collection and read are skipped (the
    index can be stale); duplicate disk paths (multi-episode files share one
    row path) are added once. The size budget is enforced while reading, so a
    file growing after its stat cannot overshoot it. Returns None when nothing
    could be added; raises BundleTooLargeError past the cap.
    """
    buffer = tempfile.SpooledTemporaryFile(max_size=BUNDLE_SPOOL_MEMORY_CEILING)
    used_names = set()
    seen_paths = set()
    total_size = 0
    added = 0
    try:
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for arcname, disk_path in entries:
                if disk_path in seen_paths:
                    continue
                try:
                    source = open_validated_subtitle(disk_path)
                except OSError:
                    continue
                # Each source is staged in full (bounded memory, spills to
                # disk) BEFORE anything touches the archive: a read failing
                # mid-way (NAS I/O error) skips the entry instead of leaving
                # a truncated member or failing the request.
                with source, tempfile.SpooledTemporaryFile(
                        max_size=_ENTRY_STAGE_MEMORY_CEILING) as staging:
                    mtime = os.fstat(source.fileno()).st_mtime
                    entry_size = 0
                    try:
                        while True:
                            chunk = source.read(_BUNDLE_COPY_CHUNK)
                            if not chunk:
                                break
                            entry_size += len(chunk)
                            if total_size + entry_size > max_total_size:
                                raise BundleTooLargeError
                            staging.write(chunk)
                    except OSError:
                        continue
                    seen_paths.add(disk_path)
                    total_size += entry_size
                    # An explicit ZipInfo: archive.write would raise on
                    # out-of-range mtimes; here the timestamp is clamped.
                    info = zipfile.ZipInfo(unique_arcname(used_names, arcname),
                                           date_time=_zip_date_time(mtime))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    staging.seek(0)
                    with archive.open(info, 'w') as target:
                        shutil.copyfileobj(staging, target)
                added += 1
    except BaseException:
        buffer.close()
        raise
    if added == 0:
        buffer.close()
        return None
    buffer.seek(0)
    return buffer


def bundle_download_name(title_component, season=None, language=None):
    parts = [title_component]
    if season is not None:
        parts.append(f'Season {season:02d}')
    if language:
        parts.append(language.lower())
    parts.append('subtitles')
    return ' - '.join(parts) + '.zip'


def _validated_language_filter():
    """Return (language_or_None, error_response_or_None)."""
    language = request.args.get('language')
    if language and not _LANGUAGE_FILTER_RE.match(language):
        return None, ('Invalid language filter', 400)
    return language, None


def _validated_season_filter():
    """Return (season_or_None, error_response_or_None). A malformed value is
    rejected rather than silently meaning 'all seasons'."""
    raw = request.args.get('season')
    if raw is None:
        return None, None
    # Length-bounded: isdigit alone lets a huge digit string past, and int()
    # would then raise on Python's conversion limit instead of returning 400.
    if not raw.isdigit() or len(raw) > 4:
        return None, ('Invalid season filter', 400)
    return int(raw), None


def _with_nosniff(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _ambiguous_media_error(media_type, media_id, arr_instance_id):
    """400 tuple when the upstream id matches more than one instance's row
    and the caller did not disambiguate; None otherwise. Mirrors the bundle
    endpoints: resolve_subtitle_path would silently .first() one of them."""
    if media_type == 'episode':
        query = scoped(
            select(TableEpisodes.id)
            .where(TableEpisodes.sonarrEpisodeId == media_id),
            TableEpisodes.arr_instance_id, arr_instance_id)
        label = 'Sonarr episode'
    else:
        query = scoped(
            select(TableMovies.id)
            .where(TableMovies.radarrId == media_id),
            TableMovies.arr_instance_id, arr_instance_id)
        label = 'Radarr movie'
    rows = database.execute(query).all()
    if len(rows) > 1:
        return f'Ambiguous {label} ID; pass arr_instance_id', 400
    return None


def _send_single_subtitle(media_type, media_id, language_code):
    arr_instance_id = _request_arr_instance_id()
    ambiguous = _ambiguous_media_error(media_type, media_id, arr_instance_id)
    if ambiguous:
        return ambiguous
    result = resolve_subtitle_path(media_type, media_id, language_code,
                                   arr_instance_id=arr_instance_id)
    if isinstance(result[1], int):
        return result
    subtitle_path = result[0]
    try:
        # Open before handing off, refusing a replacement symlink: send_file
        # on a pathname would follow whatever sits there by the time it opens.
        handle = open_validated_subtitle(subtitle_path)
    except OSError:
        # Deleted or swapped in the window since resolve_subtitle_path.
        return 'Subtitle file or directory not found', 404
    basename = os.path.basename(subtitle_path)
    try:
        response = send_file(handle,
                             as_attachment=True,
                             download_name=basename,
                             mimetype=(mimetypes.guess_type(basename)[0]
                                       or 'application/octet-stream'),
                             max_age=0)
    except Exception:
        handle.close()
        raise
    return _with_nosniff(response)


def _send_bundle(entries, download_name):
    try:
        buffer = build_subtitle_bundle(entries)
    except BundleTooLargeError:
        return 'Subtitle bundle too large', 413
    if buffer is None:
        return 'No subtitle files found', 404
    return _with_nosniff(send_file(buffer,
                                   mimetype='application/zip',
                                   as_attachment=True,
                                   download_name=download_name,
                                   max_age=0))


@api_ns_subtitle_download.route('episodes/<int:sonarrEpisodeId>/subtitles/<language>/download')
class EpisodeSubtitleFileDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(description='Download one episode subtitle file')
    @api_ns_subtitle_download.response(200, 'The subtitle file')
    @api_ns_subtitle_download.response(400, 'Invalid language code')
    @api_ns_subtitle_download.response(401, 'Not authenticated')
    @api_ns_subtitle_download.response(404, 'Media or subtitle not found')
    def get(self, sonarrEpisodeId, language):
        return _send_single_subtitle('episode', sonarrEpisodeId, language)


@api_ns_subtitle_download.route('movies/<int:radarrId>/subtitles/<language>/download')
class MovieSubtitleFileDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(description='Download one movie subtitle file')
    @api_ns_subtitle_download.response(200, 'The subtitle file')
    @api_ns_subtitle_download.response(400, 'Invalid language code')
    @api_ns_subtitle_download.response(401, 'Not authenticated')
    @api_ns_subtitle_download.response(404, 'Media or subtitle not found')
    def get(self, radarrId, language):
        return _send_single_subtitle('movie', radarrId, language)


@api_ns_subtitle_download.route('series/<int:seriesId>/subtitles/download')
class SeriesSubtitleBundleDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(
        description='Download a zip of the series subtitle files, optionally '
                    'filtered by season and/or base language')
    @api_ns_subtitle_download.response(200, 'Zip archive of subtitle files')
    @api_ns_subtitle_download.response(400, 'Invalid filter or ambiguous series id')
    @api_ns_subtitle_download.response(401, 'Not authenticated')
    @api_ns_subtitle_download.response(404, 'Series not found or no subtitle files')
    @api_ns_subtitle_download.response(413, 'Bundle exceeds the size limit')
    def get(self, seriesId):
        season, season_error = _validated_season_filter()
        if season_error:
            return season_error
        language, language_error = _validated_language_filter()
        if language_error:
            return language_error
        arr_instance_id = _request_arr_instance_id()

        series_query = scoped(
            select(TableShows.title, TableShows.arr_instance_id)
            .where(TableShows.sonarrSeriesId == seriesId),
            TableShows.arr_instance_id, arr_instance_id)
        series_rows = database.execute(series_query).all()
        if not series_rows:
            return 'Series not found', 404
        if len(series_rows) > 1:
            # Upstream ids are only unique per instance; refuse to guess.
            return 'Ambiguous Sonarr series ID; pass arr_instance_id', 400
        series_row = series_rows[0]

        effective_instance_id = (arr_instance_id if arr_instance_id is not None
                                 else series_row.arr_instance_id)
        episodes_query = scoped(
            select(TableEpisodes.season, TableEpisodes.path,
                   TableEpisodes.subtitles, TableEpisodes.arr_instance_id)
            .where(TableEpisodes.sonarrSeriesId == seriesId),
            TableEpisodes.arr_instance_id, effective_instance_id)
        episode_rows = database.execute(episodes_query).all()

        entries = collect_series_bundle_entries(episode_rows, season=season, language=language)
        download_name = bundle_download_name(
            safe_filename_component(series_row.title, 'series'),
            season=season, language=language)
        return _send_bundle(entries, download_name)


@api_ns_subtitle_download.route('movies/<int:radarrId>/subtitles/download')
class MovieSubtitleBundleDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(
        description='Download a zip of the movie subtitle files, optionally '
                    'filtered by base language')
    @api_ns_subtitle_download.response(200, 'Zip archive of subtitle files')
    @api_ns_subtitle_download.response(400, 'Invalid filter or ambiguous movie id')
    @api_ns_subtitle_download.response(401, 'Not authenticated')
    @api_ns_subtitle_download.response(404, 'Movie not found or no subtitle files')
    @api_ns_subtitle_download.response(413, 'Bundle exceeds the size limit')
    def get(self, radarrId):
        language, language_error = _validated_language_filter()
        if language_error:
            return language_error
        arr_instance_id = _request_arr_instance_id()

        movie_query = scoped(
            select(TableMovies.title, TableMovies.path,
                   TableMovies.subtitles, TableMovies.arr_instance_id)
            .where(TableMovies.radarrId == radarrId),
            TableMovies.arr_instance_id, arr_instance_id)
        movie_rows = database.execute(movie_query).all()
        if not movie_rows:
            return 'Movie not found', 404
        if len(movie_rows) > 1:
            # Upstream ids are only unique per instance; refuse to guess.
            return 'Ambiguous Radarr movie ID; pass arr_instance_id', 400
        movie_row = movie_rows[0]

        entries = collect_movie_bundle_entries(movie_row, language=language)
        download_name = bundle_download_name(
            safe_filename_component(movie_row.title, 'movie'), language=language)
        return _send_bundle(entries, download_name)
