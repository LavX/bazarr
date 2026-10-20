# coding=utf-8

import ast
import contextlib
import hmac
import logging
import re

from functools import wraps
from flask import g, jsonify, request, abort, after_this_request
from operator import itemgetter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app import activity
from app.config import settings, base_url
from languages.get_languages import language_from_alpha2, alpha3_from_alpha2
from app.database import get_audio_profile_languages, get_desired_languages, select, func
from sqlalchemy import case
from utilities.path_mappings import path_mappings, _apply_mapping, _instance_path_mapping
from subtitles.indexer.entries import entry_codec

None_Keys = ['null', 'undefined', '', None]

False_Keys = ['False', 'false', '0']

# A subtitle file uploaded on its own. A VobSub .sub or an .ass carrying its
# fonts runs to tens of megabytes, so the ceiling is generous.
MAX_SUBTITLE_UPLOAD_SIZE = 150 * 1024 * 1024  # 150 MiB

# Room for the multipart boundaries and the small form fields sent beside the
# file, so a file right at its ceiling is not refused by the declared length.
UPLOAD_FORM_ALLOWANCE = 64 * 1024

_UPLOAD_COPY_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    pass


def upload_too_large_message(what, limit):
    return f'{what} is too large: the limit is {limit // (1024 * 1024)} MiB.'


def upload_declared_too_large(limit):
    """Whether the request declares a body too large for a file of ``limit`` bytes.

    Checked before anything touches ``request.files``, so an oversized upload
    is refused before its body is parsed.
    """
    length = request.content_length
    return bool(length) and length > limit + UPLOAD_FORM_ALLOWANCE


def read_bounded_upload(upload, limit):
    """The uploaded file's bytes, reading at most one byte past ``limit``.

    Raises UploadTooLarge instead of buffering an oversized file whole.
    """
    data = upload.read(limit + 1)
    if len(data) > limit:
        raise UploadTooLarge()
    return data


def copy_bounded_upload(upload, destination, limit):
    """Copy the uploaded file into ``destination`` in chunks, and return its size.

    Raises UploadTooLarge as soon as more than ``limit`` bytes have arrived;
    whatever was written by then is the caller's to discard.
    """
    size = 0
    while True:
        chunk = upload.read(min(_UPLOAD_COPY_CHUNK, limit + 1 - size))
        if not chunk:
            return size
        size += len(chunk)
        if size > limit:
            raise UploadTooLarge()
        destination.write(chunk)


def image_proxy_path_with_instance(path, arr_instance_id):
    if arr_instance_id is None:
        return path

    parsed = urlsplit(path)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != 'arr_instance_id'
    ]
    query.append(('arr_instance_id', str(arr_instance_id)))
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(query),
        parsed.fragment,
    ))


def _subtitle_language_details(language_code):
    language = language_code.split(':')
    modifiers = language[1:]
    sync_modifier = next((modifier for modifier in modifiers if modifier.startswith('sync-')), None)
    combined_modifier = next((modifier for modifier in modifiers if modifier.startswith('combined-')), None)

    return {
        "base": language[0],
        "full": language_code,
        "forced": any(modifier.lower() == 'forced' for modifier in modifiers),
        "hi": any(modifier.lower() == 'hi' for modifier in modifiers),
        "modifier": sync_modifier or combined_modifier,
    }


def _safe_apikey_compare(provided, expected):
    if not provided:
        return False
    if not expected:
        # An empty stored key authenticates nothing, by rule. The compare
        # below would not give that rule by itself: compare_digest answers
        # that two empty strings match, so a boot that withholds an
        # undecryptable key as an empty string would otherwise pair with an
        # empty presented key and let the request through. The guard is
        # defense in depth on top of the presented-side check, which already
        # refuses an empty or missing key before the stored one is read.
        return False
    return hmac.compare_digest(str(provided), str(expected))


def _user_request_scope():
    """Mark the request thread as a user's own for the authenticated handler.

    A webhook is a media server's machine call, not a user's, so its requests
    stay unmarked: the jobs one triggers deeper in the chain announce nothing.
    """
    if '/webhooks/' in request.path:
        return contextlib.nullcontext()
    return activity.user_action()


def authenticate(actual_method):
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        apikey_settings = settings.auth.apikey
        apikey_header = request.headers.get('X-API-KEY')

        if _safe_apikey_compare(apikey_header, apikey_settings):
            with _user_request_scope():
                return actual_method(*args, **kwargs)

        # Legacy: accept API key from query string or an urlencoded form body, with
        # a deprecation warning. Suppress the warning for webhook endpoints (Plex
        # webhooks use ?apikey= in callback URLs)
        # The form is read only for an urlencoded body. Reading request.form parses
        # the whole request, whatever its content type, so a multipart upload was
        # parsed (and spooled) just to look for one field: before this 401, and
        # before the route's own declared-length refusal. An urlencoded body is
        # the form a client posts a key in, so that fallback is kept.
        apikey_get = request.args.get('apikey')
        apikey_post = (request.form.get('apikey')
                       if request.mimetype == 'application/x-www-form-urlencoded' else None)
        if _safe_apikey_compare(apikey_get, apikey_settings) or _safe_apikey_compare(apikey_post, apikey_settings):
            if '/webhooks/' not in request.path:
                logging.warning(
                    'API key passed via query string or an urlencoded form body is deprecated. '
                    'Use the X-API-KEY header instead. '
                    'Endpoint: %s %s', request.method, request.path
                )
            with _user_request_scope():
                return actual_method(*args, **kwargs)

        return abort(401)

    return wrapper


def _never_cacheable(response):
    """The response finalizer of the HLS and token routes: never cacheable.

    It is registered before authentication, so it runs on every answer
    those routes produce, including the 500 the framework builds from an
    unhandled exception: no playlist, segment or token answer may be
    stored by a cache. It adds no marker: the app-wide hook below is what
    marks the token route's answers, so even that 500 carries the marker.
    """
    response.headers['Cache-Control'] = 'no-store'
    return response


def editor_no_store(response):
    """The app-wide response hook of the editor's no-store routes.

    The finalizer above is registered only when a decorated view runs, but
    the router itself answers an automatic OPTIONS request and refuses an
    unsupported method with a 405 before any view is dispatched, so those
    answers would otherwise carry no cache header at all. The hook is
    scoped to the two routes the lane keeps no-store, the same way the
    decorators scope them by path, and marks whatever those paths answer
    with no-store, whatever built the answer. On the token route it also
    restates the route marker, so the router-built OPTIONS and 405 and
    even the unhandled-exception 500 reach the client marked, the way the
    route's deliberate answers are marked. It is registered with the api
    blueprint, so it runs after the finalizer and only re-states a header
    a running view has already set.
    """
    path = request.path.rstrip('/')
    if path.endswith('/api/editor/stream-token'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Bazarr-Stream-Token-Route'] = '1'
    elif '/api/editor/hls/' in request.path:
        response.headers['Cache-Control'] = 'no-store'
    return response


def register_editor_no_store(blueprint):
    """Register the no-store hook above on the api blueprint, app-wide.

    Production calls this once from the api package, so the tests that
    need the hook wire it the same way, through this registration, rather
    than by installing the hook function by hand.
    """
    blueprint.after_app_request(editor_no_store)


def _editor_unauthorized_response():
    """The editor routes' 401: never cacheable, and marked on the token route.

    The token route's marker tells the frontend that Bazarr itself answered,
    rather than a reverse proxy returning the same status while Bazarr is
    down or restarting.
    """
    response = jsonify({'message': 'Not authenticated'})
    response.status_code = 401
    response.headers['Cache-Control'] = 'no-store'
    if request.path.rstrip('/').endswith('/api/editor/stream-token'):
        response.headers['X-Bazarr-Stream-Token-Route'] = '1'
    return response


def _editor_request_carries_url_apikey():
    """Whether the request carries an apikey outside the header.

    The form is read only for the body types that carry form fields,
    urlencoded and multipart, because reading request.form parses the
    whole request for them. The answer says only that a key is present,
    not that it is valid: the editor routes refuse an apikey in a URL or
    a form whatever else the request presents, so a valid header
    credential beside it changes nothing.
    """
    if 'apikey' in request.args:
        return True
    if request.mimetype not in ('application/x-www-form-urlencoded',
                                'multipart/form-data'):
        return False
    return 'apikey' in request.form


def authenticate_header_only(actual_method):
    """Guard the editor routes: the API key in the header, nowhere else.

    The editor routes exist for the frontend, which sends the key in the
    X-API-KEY header. Keeping the query-string fallback there would let a
    key regress into a URL unnoticed, so a script still passing apikey= in
    a URL fails at once with a 401 instead of passing with a deprecation
    warning nobody reads. The rest of the API keeps the fallback. The
    refusal is checked before the header is read, so an apikey in the URL
    is refused even beside a valid header key.
    """
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        if request.path.rstrip('/').endswith('/api/editor/stream-token'):
            # Every answer of the token route is no-store, including one
            # the framework builds from an unhandled exception. The
            # finalizer is registered before authentication, so nothing
            # this route produces can bypass it.
            after_this_request(_never_cacheable)
        if _editor_request_carries_url_apikey():
            return _editor_unauthorized_response()
        if _safe_apikey_compare(request.headers.get('X-API-KEY'), settings.auth.apikey):
            with _user_request_scope():
                return actual_method(*args, **kwargs)
        return _editor_unauthorized_response()

    return wrapper


def authenticate_header_or_stream_token(actual_method):
    """The HLS route's variant: the header, or a valid st in the query string.

    A native player (Safari, iOS) cannot add headers to the segment requests
    it makes itself, so the playlist URL it is given carries a media-scoped
    stream token as st instead of the API key. The token is verified against
    the route's own media type, media id, audio track and arr instance. An
    apikey in the query or form is refused before either credential is read,
    the same way the header-only routes refuse it.
    """
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        # Every answer of the HLS route is no-store, including one the
        # framework builds from an unhandled exception. The finalizer is
        # registered before authentication, so nothing this route produces
        # can bypass it.
        after_this_request(_never_cacheable)
        if _editor_request_carries_url_apikey():
            return _editor_unauthorized_response()
        if _safe_apikey_compare(request.headers.get('X-API-KEY'), settings.auth.apikey):
            with _user_request_scope():
                return actual_method(*args, **kwargs)
        token = request.args.get('st')
        if token:
            from api.editor import stream_token
            arr_instance_id = request.args.get('arr_instance_id')
            if arr_instance_id in (None, ''):
                arr_instance_id = None
            else:
                try:
                    arr_instance_id = int(arr_instance_id)
                except (TypeError, ValueError):
                    return _editor_unauthorized_response()
            if stream_token.verify_stream_token(
                token,
                kwargs.get('media_type'),
                kwargs.get('media_id'),
                arr_instance_id,
                kwargs.get('audio_track'),
            ):
                # The token was verified against this route's own scope, so
                # the handler may echo it into the playlist it returns. Only
                # the verified token reaches it: a st that merely rode along
                # on a header-authenticated call is dropped, not echoed back
                # into URIs unescaped.
                g.editor_hls_stream_token = token
                with _user_request_scope():
                    return actual_method(*args, **kwargs)
        return _editor_unauthorized_response()

    return wrapper


def postprocess(item):
    # Remove ffprobe_cache
    if item.get('radarrId'):
        path_replace = path_mappings.path_replace_movie
    else:
        path_replace = path_mappings.path_replace
    if item.get('ffprobe_cache'):
        del item['ffprobe_cache']

    # Parse audio language
    if item.get('audio_language'):
        item['audio_language'] = get_audio_profile_languages(item['audio_language'])
    else:
        item['audio_language'] = []

    # Make sure profileId is a valid None value
    if item.get('profileId') in None_Keys:
        item['profileId'] = None

    # Parse alternate titles
    if item.get('alternativeTitles'):
        item['alternativeTitles'] = ast.literal_eval(item['alternativeTitles'])
    else:
        item['alternativeTitles'] = []

    # Parse subtitles
    if item.get('subtitles'):
        item['subtitles'] = ast.literal_eval(item['subtitles'])
        for i, subs in enumerate(item['subtitles']):
            language = _subtitle_language_details(subs[0])
            file_size = subs[2] if len(subs) > 2 else 0
            item['subtitles'][i] = {"path": path_replace(subs[1]),
                                    "name": language_from_alpha2(language["base"]),
                                    "code2": language["base"],
                                    "code3": alpha3_from_alpha2(language["base"]),
                                    "language": language["full"],
                                    "modifier": language["modifier"],
                                    "forced": language["forced"],
                                    "hi": language["hi"],
                                    "file_size": file_size,
                                    "codec": entry_codec(subs)}
        if settings.general.embedded_subs_show_desired and item.get('profileId'):
            desired_lang_list = get_desired_languages(item['profileId'])
            item['subtitles'] = [x for x in item['subtitles'] if x['code2'] in desired_lang_list or x['path']]
        item['subtitles'] = sorted(item['subtitles'], key=itemgetter('name', 'forced'))
    else:
        item['subtitles'] = []

    # Parse missing subtitles
    if item.get('missing_subtitles'):
        item['missing_subtitles'] = ast.literal_eval(item['missing_subtitles'])
        for i, subs in enumerate(item['missing_subtitles']):
            language = subs.split(':')
            item['missing_subtitles'][i] = {"name": language_from_alpha2(language[0]),
                                            "code2": language[0],
                                            "code3": alpha3_from_alpha2(language[0]),
                                            "forced": False,
                                            "hi": False}
            if len(language) > 1:
                item['missing_subtitles'][i].update(
                    {
                        "forced": language[1] == 'forced',
                        "hi": language[1] == 'hi',
                    }
                )
    else:
        item['missing_subtitles'] = []

    # Parse tags
    if item.get('tags') is not None:
        item['tags'] = ast.literal_eval(item.get('tags', '[]'))
    else:
        item['tags'] = []
    if item.get('monitored'):
        item['monitored'] = item.get('monitored') == 'True'
    else:
        item['monitored'] = False
    if item.get('hearing_impaired'):
        item['hearing_impaired'] = item.get('hearing_impaired') == 'True'
    else:
        item['hearing_impaired'] = False

    if item.get('language'):
        if item['language'] == 'None':
            item['language'] = None
        if item['language'] is not None:
            splitted_language = item['language'].split(':')
            item['language'] = {
                "name": language_from_alpha2(splitted_language[0]),
                "code2": splitted_language[0],
                "code3": alpha3_from_alpha2(splitted_language[0]),
                "forced": bool(item['language'].endswith(':forced')),
                "hi": bool(item['language'].endswith(':hi')),
            }

    if item.get('path'):
        item['path'] = path_replace(item['path'])

    if item.get('video_path'):
        # Provide mapped video path for history
        item['video_path'] = path_replace(item['video_path'])

    if item.get('subtitles_path'):
        # Provide mapped subtitles path
        item['subtitles_path'] = path_replace(item['subtitles_path'])

    if item.get('external_subtitles'):
        # Provide mapped external subtitles paths for history
        if isinstance(item['external_subtitles'], str):
            item['external_subtitles'] = ast.literal_eval(item['external_subtitles'])
        for i, subs in enumerate(item['external_subtitles']):
            item['external_subtitles'][i] = path_replace(subs)

    # map poster and fanart to server proxy. Carry the owning instance (#156)
    # so the proxy fetches the cover from THAT Sonarr/Radarr instead of the
    # default one (a non-default instance's cover does not exist on the default
    # server). No suffix when there is no owner -> byte-identical default path.
    media = 'movies' if item.get('radarrId') else 'series'
    arr_instance_id = item.get('arr_instance_id')

    def _proxy_image(path):
        if not path:
            return None
        path = image_proxy_path_with_instance(path, arr_instance_id)
        return f"{base_url}/images/{media}{path}"

    if item.get('poster') is not None:
        item['poster'] = _proxy_image(item['poster'])

    if item.get('fanart') is not None:
        item['fanart'] = _proxy_image(item['fanart'])

    return item


def chunked(items, size=900):
    """Yield ``items`` in successive lists of at most ``size`` entries.

    Used to keep an ``IN (...)`` clause within SQLite's bound-parameter limit
    (999 on older builds) when a fetchAll id list can grow with the library.
    Postgres has no such limit, so a single large batch would work there, but
    chunking is behaviour-equivalent for both back ends.
    """
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _canonical_embedded_language(language):
    """Reduce a possibly multi-variant embedded language code to the
    hi-priority single variant that action-7 history rows store.

    The raw ``subtitles`` column can hold a combined code like
    ``"en:hi:forced"`` for a track that is both HI and forced, while
    ``_log_embedded_history_movie`` writes the canonical single variant
    ``"en:hi"``. Applying the same reduction here (base, plus ``":hi"`` when HI,
    else ``":forced"`` when forced, else the base) lets the two match.
    """
    parts = str(language).split(':')
    base = parts[0]
    variants = set(parts[1:])
    if 'hi' in variants:
        return base + ':hi'
    if 'forced' in variants:
        return base + ':forced'
    return base


# A Windows path: a drive letter followed by either separator, a UNC prefix, or
# any path carrying a backslash, which Unix paths never do.
_WINDOWS_PATH = re.compile(r'^(?:[A-Za-z]:[\\/]|//)')


def _subtitle_file_name(path):
    """Return a subtitle path's file name and whether it is a Windows path.

    History rows and the indexed subtitles column can carry the same file under
    different mapped folders or separator styles: a translation logs the
    globally mapped path while the indexer records the instance's, and one side
    can be a Windows path where the other is the mapped Linux path. The file
    name is what stays stable, so the score lookup compares that, split on both
    separators. Windows resolves names case-insensitively, so a name that
    reached one side through a Windows path matches the other side's name
    whatever its case; two Unix names that differ only by case are two files on
    a case-sensitive filesystem and stay apart.
    """
    text = str(path)
    name = text.replace('\\', '/').rsplit('/', 1)[-1]
    return name, _is_windows_path(text)


def _is_windows_path(text):
    # A drive letter or a UNC prefix is Windows whichever separator it uses.
    return '\\' in text or bool(_WINDOWS_PATH.match(text))


def _map_media_path(path, mapping):
    """Map a video or subtitle path remote to local as ``_apply_mapping`` does,
    except that a Windows path matches a mapping's root whatever its separators
    and case, as Windows resolves it. Unix paths keep the exact, case-sensitive
    match."""
    if not _is_windows_path(path):
        return _apply_mapping(path, mapping, reverse=False)
    normalized = path.replace('\\', '/')
    for pair in mapping:
        if len(pair) < 2 or pair[0] == pair[1] or '' in pair:
            continue
        match = re.search(re.escape(pair[0].replace('\\', '/')), normalized, re.IGNORECASE)
        if match:
            return normalized[:match.start()] + pair[1] + normalized[match.end():]
    return normalized


def _media_path_key(path, mapping, fold):
    """A path in one comparable spelling: mapped to local by ``mapping`` (None
    for the path as stored), with '/' separators, case-folded on request."""
    if mapping is not None:
        path = _map_media_path(path, mapping)
    path = path.replace('\\', '/')
    return path.casefold() if fold else path


def _same_media_path(current, stored, instance_mapping, writer_mapping):
    """Whether a history path names the item's current file: equal as stored,
    or equal once each is mapped to local with the mapping that produced it.
    A stored path never compares with a mapped one, since two roots can
    overlap. As with subtitle names, a Windows path on either side folds case."""
    if not current or not stored:
        return False
    fold = _is_windows_path(current) or _is_windows_path(stored)
    return (_media_path_key(current, None, fold) == _media_path_key(stored, None, fold)
            or _media_path_key(current, instance_mapping, fold)
            == _media_path_key(stored, writer_mapping, fold))


def lowest_subtitle_scores(db, subtitles_by_id, id_column, history_table, *, videos_by_id, media_type):
    """Lowest current-subtitle score (0-100 %) for each item id.

    Mirrors what the detail pages show. For every item, per current subtitle:
      * file subtitles -> the newest replacement/deletion record for a current
        file; only downloads (action 1/2/3) supply a score. Sync (5) preserves it;
      * embedded tracks -> the newest source record (action 7) whose language
        the item still has as an embedded track (a subtitles entry with an
        empty path).
    Each counted record scores ``round(score * 100 / score_out_of, 2)``; records
    without a usable score/score_out_of are skipped. The item's value is the
    minimum over the counted records, absent from the result when nothing counts.

    ``subtitles_by_id`` maps id -> raw ``subtitles`` column. Paths compare by
    their file name, the identity that survives the path mappings a history row
    and the indexer can apply to the same file; Windows names also match
    case-insensitively, while Unix names keep their case. One history query
    runs for the whole page (no per-item query) and returns only the newest
    record per item and file or embedded track.

    ``videos_by_id`` maps id -> (video path, arr_instance_id). A file record
    counts only when it names the item's current file: its subtitle path is a
    current subtitle path, as the detail views key a score, or its video path
    is the item's current video path. So a record left in an item's old folder
    does not score a same-named subtitle after a move, while a subtitle kept in
    place (a custom subtitle folder, a new video container) keeps its score.
    Paths compare as stored, or both mapped to local with the mapping each was
    reversed with: the owning instance's effective mapping for the item and
    most records, the global mapping for ``media_type`` ('movie' or 'series')
    for translations. Embedded records are logged once per track and stay
    valid for the item wherever it lives.
    """
    ids = list(subtitles_by_id.keys())
    if not ids:
        return {}

    global_mapping = (path_mappings.path_mapping_movies if media_type == 'movie'
                      else path_mappings.path_mapping_series)
    instance_mappings = {}
    item_mappings = {}
    current_videos = {}
    for item_id, (video_path, arr_instance_id) in videos_by_id.items():
        if arr_instance_id not in instance_mappings:
            mapping = _instance_path_mapping(arr_instance_id, media_type)
            # The same choice path_replace_instance makes.
            instance_mappings[arr_instance_id] = global_mapping if mapping is None else mapping
        item_mappings[item_id] = instance_mappings[arr_instance_id]
        current_videos[item_id] = video_path

    # Current file-subtitle paths and embedded-track languages per item, read
    # straight from the raw subtitles column.
    current_paths = {}
    current_subtitle_paths = {}
    embedded_langs = {}
    for item_id, raw in subtitles_by_id.items():
        paths = {}
        full_paths = []
        langs = set()
        if raw:
            for entry in ast.literal_eval(raw):
                if entry[1]:
                    name, windows = _subtitle_file_name(entry[1])
                    paths[name] = paths.get(name, False) or windows
                    full_paths.append(entry[1])
                else:
                    langs.add(_canonical_embedded_language(entry[0]))
        current_paths[item_id] = paths
        current_subtitle_paths[item_id] = full_paths
        embedded_langs[item_id] = langs

    file_matches = {}

    def same_file(item_id, action, subtitles_path, video_path):
        # Translations reverse their paths with the global mapping, every other
        # file record with the owning instance's.
        translated = action == 6
        key = (item_id, translated, subtitles_path, video_path)
        if key not in file_matches:
            instance_mapping = item_mappings.get(item_id)
            writer_mapping = global_mapping if translated else instance_mapping
            file_matches[key] = (
                any(_same_media_path(current, subtitles_path, instance_mapping, writer_mapping)
                    for current in current_subtitle_paths.get(item_id, ()))
                or _same_media_path(current_videos.get(item_id), video_path,
                                    instance_mapping, writer_mapping))
        return file_matches[key]

    # Chunk the id list so the IN (...) clause stays within SQLite's
    # bound-parameter limit for large libraries. Each id lives in exactly one
    # chunk, so every row for a given item keeps its timestamp/id ordering and
    # the newest-record-wins seen-set below still holds across the merge.
    #
    # Only the newest record per item and file (or embedded track) can win, so
    # the database keeps just that one and history depth never reaches Python.
    # Rows sharing a partition always pass or fail the checks below together
    # and resolve to the same identity, which keeps the result unchanged.
    # A translation's paths go through another mapping than every other file
    # record's, so the same stored paths can pass for one and fail for the
    # other; translations get partitions of their own.
    embedded = history_table.action == 7
    translated = history_table.action == 6
    rows = []
    for chunk in chunked(ids):
        ranked = (
            select(id_column.label('item_id'),
                   history_table.id.label('history_id'),
                   history_table.timestamp,
                   history_table.action,
                   history_table.language,
                   history_table.subtitles_path,
                   history_table.video_path,
                   history_table.score,
                   history_table.score_out_of,
                   func.row_number().over(
                       partition_by=(id_column,
                                     case((embedded, 1), (translated, 2), else_=0),
                                     history_table.language,
                                     case((embedded, None), else_=history_table.subtitles_path),
                                     case((embedded, None), else_=history_table.video_path)),
                       order_by=(history_table.timestamp.desc(), history_table.id.desc()),
                   ).label('position'))
            .where(id_column.in_(chunk))
            .where(history_table.action.in_([0, 1, 2, 3, 4, 6, 7, 8]))
            .subquery()
        )
        rows.extend(db.execute(
            select(ranked.c.item_id,
                   ranked.c.action,
                   ranked.c.language,
                   ranked.c.subtitles_path,
                   ranked.c.video_path,
                   ranked.c.score,
                   ranked.c.score_out_of)
            .where(ranked.c.position == 1)
            .order_by(ranked.c.timestamp.desc(), ranked.c.history_id.desc())
        ).all())

    lowest = {}
    # (item_id, kind, key) already resolved: the newest record wins, so an older
    # record for the same subtitle is ignored even when the newest had no score.
    seen = set()
    for row in rows:
        if row.action == 7:
            if row.language not in embedded_langs.get(row.item_id, ()):
                continue
            key = (row.item_id, 'embedded', row.language)
        else:
            name, windows = _subtitle_file_name(row.subtitles_path)
            names = current_paths.get(row.item_id, {})
            if name in names:
                current_name = name
            else:
                folded = [other for other in names if other.casefold() == name.casefold()]
                # A Windows path resolves its name case-insensitively; two Unix
                # paths that differ only by case are two different files.
                if not folded or not (windows or any(names[other] for other in folded)):
                    continue
                current_name = sorted(folded)[0]
            if not same_file(row.item_id, row.action, row.subtitles_path, row.video_path):
                continue
            # The current file is the canonical identity: a row that matched it
            # by exact name and one that matched it case-insensitively belong to
            # the same subtitle, so the newest one wins.
            key = (row.item_id, 'file', current_name)
        if key in seen:
            continue
        seen.add(key)
        # Uploads/translations may carry a synthetic score, but they replace
        # the downloaded file and must also shadow its old provider score. A
        # file extracted from an embedded track (8) replaces it too, and its
        # score is the track's, as on the detail pages.
        if row.action not in (1, 2, 3, 7, 8) or row.score is None:
            continue
        if row.score == 0:
            # Older history writers leave score_out_of unset for a real zero.
            percent = 0.0
        elif not row.score_out_of:
            continue
        else:
            percent = round(row.score * 100 / row.score_out_of, 2)
        current = lowest.get(row.item_id)
        if current is None or percent < current:
            lowest[row.item_id] = percent
    return lowest
