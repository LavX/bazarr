# coding=utf-8
"""Local stream analysis and the cache owned by a source item."""
import logging
import os
import pickle

from app.config import settings
from utilities import video_analyzer
from utilities.binaries import BinaryNotFound

from .identity import FileStat, file_content_stamp, stored_content_stamp, unsigned


UNKNOWN_STREAM_COLUMNS = dict(ffprobe_cache=None, audio_language=None, audio_codec=None, video_codec=None,
                              resolution=None)


def read_metadata(blob):
    if blob is not None:
        try:
            data = pickle.loads(blob)
        except (AttributeError, ImportError, TypeError, ValueError, EOFError, pickle.UnpicklingError):
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _content_stamp(stat):
    return file_content_stamp(FileStat(unsigned(stat.st_dev), unsigned(stat.st_ino), stat.st_size, stat.st_mtime_ns))


def stamp_index(data, stamp):
    return pickle.dumps(data | {'source_file': stamp}, pickle.HIGHEST_PROTOCOL)


def index_matches_file(item, stat):
    """Does the stored subtitle index describe the current file?

    A search can analyze a replacement before the scanner records its file identity.
    Use the probe stamp when present and the admitted file stamp for external-only indexes.
    """
    if item.ffprobe_cache is not None:
        return bool(cached_metadata(item, stat))
    return _matches_admission(item, stat)


def _matches_admission(item, stat):
    admitted = stored_content_stamp(item)
    return all(recorded is None or recorded == actual for recorded, actual in zip(admitted, _content_stamp(stat)))


def failed_probe_values(item, stat):
    """Record the file of a new external-only listing when it differs from the admitted file."""
    stamp = _content_stamp(stat)
    if not _matches_admission(item, stat):
        return UNKNOWN_STREAM_COLUMNS | {'ffprobe_cache': stamp_index({}, stamp)}
    if read_metadata(item.ffprobe_cache).get('source_file') not in (None, stamp):
        return UNKNOWN_STREAM_COLUMNS
    return {}


def cached_metadata(item, stat):
    data = read_metadata(item.ffprobe_cache)
    return data if data.get('source_file') == _content_stamp(stat) else {}


def probe_item(item, stat):
    """Return cached or newly parsed streams, with a blob only when a probe succeeded."""
    cached = cached_metadata(item, stat)
    parser = settings.general.embedded_subtitles_parser
    if cached.get(parser):
        return cached, None
    # A source file is a local container. The Arr parser can also resolve a .strm
    # file to a stream URL, which is outside the scope of this local scan.
    if item.path.lower().endswith('.strm'):
        return cached, None
    try:
        data = video_analyzer.parse_video_metadata(item.path, stat.st_size, use_cache=False)
    except (OSError, BinaryNotFound):
        logging.warning('BAZARR could not analyze the source file %s', item.path, exc_info=True)
        return cached, None
    if not data or not data.get(parser):
        return cached, None
    return data, stamp_index(data, _content_stamp(stat))


def local_stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def streams(data):
    return data.get('ffprobe') or data.get('mediainfo') or {}
