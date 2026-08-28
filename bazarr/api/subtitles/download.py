# coding=utf-8

import ast
import io
import os
import re
import zipfile

from flask import request, send_file
from flask_restx import Resource, Namespace

from app.database import TableEpisodes, TableMovies, TableShows, database, select
from utilities.path_mappings import path_mappings

from .content import resolve_subtitle_path
from ..utils import authenticate

api_ns_subtitle_download = Namespace('SubtitleDownload', description='Download subtitle files')

# Uncompressed ceiling for a bundle. Text subtitles are tiny; only a library
# full of PGS (.sup) files could approach this, and those should not be
# bundled into memory anyway.
MAX_BUNDLE_UNCOMPRESSED_SIZE = 256 * 1024 * 1024

# Base language filter for bundles: "en", "pt-BR". Variants (hi/forced/sync)
# of the base language are included by design, so modifiers are not accepted.
_LANGUAGE_FILTER_RE = re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,4})?$')


class BundleTooLargeError(Exception):
    pass


def safe_filename_component(value, fallback):
    """Reduce a title to something usable inside a download filename."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value or ''))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().strip('.')
    return cleaned or fallback


def unique_arcname(used, arcname):
    """Return arcname, suffixed with ' (n)' before the extension if taken."""
    if arcname not in used:
        used.add(arcname)
        return arcname
    stem, ext = os.path.splitext(arcname)
    counter = 2
    while f'{stem} ({counter}){ext}' in used:
        counter += 1
    result = f'{stem} ({counter}){ext}'
    used.add(result)
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


def matches_language(language_value, language_filter):
    """True when the entry's base language matches the filter (or no filter).

    The DB language label can carry modifiers ("en:hi", "en:forced",
    "en:sync-ffsubsync"); a filter of "en" includes all of them.
    """
    if not language_filter:
        return True
    return language_value.split(':', 1)[0].lower() == language_filter.lower()


def collect_series_bundle_entries(episode_rows, season=None, language=None):
    """Build (arcname, disk_path) pairs for a series bundle.

    episode_rows: iterables with .season, .subtitles, .arr_instance_id.
    Entries are grouped under "Season NN/" folders in the archive.
    """
    entries = []
    for row in episode_rows:
        if season is not None and row.season != season:
            continue
        for language_value, reversed_path in iter_external_subtitles(row.subtitles):
            if not matches_language(language_value, language):
                continue
            disk_path = path_mappings.path_replace_instance(
                reversed_path, row.arr_instance_id, 'episode')
            arcname = f'Season {row.season:02d}/{os.path.basename(disk_path)}'
            entries.append((arcname, disk_path))
    return entries


def collect_movie_bundle_entries(movie_row, language=None):
    """Build (arcname, disk_path) pairs for one movie's subtitle files."""
    entries = []
    for language_value, reversed_path in iter_external_subtitles(movie_row.subtitles):
        if not matches_language(language_value, language):
            continue
        disk_path = path_mappings.path_replace_instance(
            reversed_path, movie_row.arr_instance_id, 'movie')
        entries.append((os.path.basename(disk_path), disk_path))
    return entries


def build_subtitle_bundle(entries, max_total_size=MAX_BUNDLE_UNCOMPRESSED_SIZE):
    """Zip the given (arcname, disk_path) pairs into an in-memory buffer.

    Files missing on disk are skipped (the index can be stale). Returns None
    when nothing could be added; raises BundleTooLargeError past the cap.
    """
    buffer = io.BytesIO()
    used_names = set()
    total_size = 0
    added = 0
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for arcname, disk_path in entries:
            try:
                if not os.path.isfile(disk_path):
                    continue
                total_size += os.path.getsize(disk_path)
            except OSError:
                continue
            if total_size > max_total_size:
                raise BundleTooLargeError
            archive.write(disk_path, unique_arcname(used_names, arcname))
            added += 1
    if added == 0:
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


def _request_arr_instance_id():
    return request.args.get('arr_instance_id', type=int)


def _validated_language_filter():
    """Return (language_or_None, error_response_or_None)."""
    language = request.args.get('language')
    if language and not _LANGUAGE_FILTER_RE.match(language):
        return None, ('Invalid language filter', 400)
    return language, None


def _send_single_subtitle(media_type, media_id, language_code):
    result = resolve_subtitle_path(media_type, media_id, language_code,
                                   arr_instance_id=_request_arr_instance_id())
    if isinstance(result[1], int):
        return result
    subtitle_path = result[0]
    return send_file(subtitle_path,
                     as_attachment=True,
                     download_name=os.path.basename(subtitle_path),
                     max_age=0)


def _send_bundle(entries, download_name):
    try:
        buffer = build_subtitle_bundle(entries)
    except BundleTooLargeError:
        return 'Subtitle bundle too large', 413
    if buffer is None:
        return 'No subtitle files found', 404
    return send_file(buffer,
                     mimetype='application/zip',
                     as_attachment=True,
                     download_name=download_name,
                     max_age=0)


@api_ns_subtitle_download.route('episodes/<int:sonarrEpisodeId>/subtitles/<language>/download')
class EpisodeSubtitleFileDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(description='Download one episode subtitle file')
    def get(self, sonarrEpisodeId, language):
        return _send_single_subtitle('episode', sonarrEpisodeId, language)


@api_ns_subtitle_download.route('movies/<int:radarrId>/subtitles/<language>/download')
class MovieSubtitleFileDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(description='Download one movie subtitle file')
    def get(self, radarrId, language):
        return _send_single_subtitle('movie', radarrId, language)


@api_ns_subtitle_download.route('series/<int:seriesId>/subtitles/download')
class SeriesSubtitleBundleDownload(Resource):
    @authenticate
    @api_ns_subtitle_download.doc(
        description='Download a zip of the series subtitle files, optionally '
                    'filtered by season and/or base language')
    def get(self, seriesId):
        season = request.args.get('season', type=int)
        language, error = _validated_language_filter()
        if error:
            return error
        arr_instance_id = _request_arr_instance_id()

        series_query = (
            select(TableShows.title, TableShows.arr_instance_id)
            .where(TableShows.sonarrSeriesId == seriesId)
        )
        if arr_instance_id is not None:
            series_query = series_query.where(TableShows.arr_instance_id == arr_instance_id)
        series_row = database.execute(series_query).first()
        if not series_row:
            return 'Series not found', 404

        episodes_query = (
            select(TableEpisodes.season, TableEpisodes.subtitles, TableEpisodes.arr_instance_id)
            .where(TableEpisodes.sonarrSeriesId == seriesId)
        )
        # Scope episodes to the owning instance: upstream ids are only unique
        # per Sonarr server (#156). Fall back to the matched series row's
        # instance when the caller did not pass one.
        effective_instance_id = (arr_instance_id if arr_instance_id is not None
                                 else series_row.arr_instance_id)
        if effective_instance_id is not None:
            episodes_query = episodes_query.where(
                TableEpisodes.arr_instance_id == effective_instance_id)
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
    def get(self, radarrId):
        language, error = _validated_language_filter()
        if error:
            return error
        arr_instance_id = _request_arr_instance_id()

        movie_query = (
            select(TableMovies.title, TableMovies.subtitles, TableMovies.arr_instance_id)
            .where(TableMovies.radarrId == radarrId)
        )
        if arr_instance_id is not None:
            movie_query = movie_query.where(TableMovies.arr_instance_id == arr_instance_id)
        movie_row = database.execute(movie_query).first()
        if not movie_row:
            return 'Movie not found', 404

        entries = collect_movie_bundle_entries(movie_row, language=language)
        download_name = bundle_download_name(
            safe_filename_component(movie_row.title, 'movie'), language=language)
        return _send_bundle(entries, download_name)
