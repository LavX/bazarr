# coding=utf-8
"""What each file an extraction wrote looked like when it was written.

History says that an extraction once wrote a subtitle at a path, not that the
file there now is still that one: it may have been deleted outside Bazarr+ and
another file renamed onto the name. The upgrade job needs to tell the two
apart, so every file logged as extracted (history action 8) has its content
fingerprint kept here, keyed by its owner and the path history stores.
"""

import hashlib
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_STORE_NAME = ".extracted-files.json"


def _store_path():
    from subtitles.tools.translate.batch import extracted_subtitles_dir

    return os.path.join(extracted_subtitles_dir(), _STORE_NAME)


def _key(arr_instance_id, db_path):
    return f"{arr_instance_id}:{db_path}"


def fingerprint(path):
    """The SHA-256 of the file's content, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _load():
    try:
        with open(_store_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(arr_instance_id, db_path, local_path):
    """Remember the content of ``local_path``, logged as extracted at ``db_path``."""
    value = fingerprint(local_path)
    if value is None:
        return
    with _lock:
        data = _load()
        data[_key(arr_instance_id, db_path)] = value
        store = _store_path()
        os.makedirs(os.path.dirname(store), exist_ok=True)
        temporary = f"{store}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporary, store)
        except OSError:
            logger.warning("BAZARR could not record the extracted subtitle %s", db_path, exc_info=True)
            try:
                os.unlink(temporary)
            except OSError:
                pass


def is_unchanged(arr_instance_id, db_path, local_path):
    """Whether the file at ``local_path`` is still the one extracted to ``db_path``."""
    with _lock:
        expected = _load().get(_key(arr_instance_id, db_path))
    return expected is not None and fingerprint(local_path) == expected
