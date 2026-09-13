"""Exact owned sports-library lookup for the OpenSubtitles-compatible Hub."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import logging
import os
import re
import stat
import unicodedata

from . import local_subs as local

logger = logging.getLogger(__name__)


def valid_moviehash(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{16}", str(value or "").strip()))


def _basename(value):
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize(
        "NFC", value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    ).casefold()


@lru_cache(maxsize=5000)
def _hash_file(path, stamp):
    value = local._opensubtitles_hash(path)
    if tuple(_file_stat(os.stat(path))) != stamp:
        raise ValueError("Sports file changed during hash lookup")
    return value


def _digest(value):
    return hashlib.sha256(repr(value).encode()).hexdigest()


@dataclass(frozen=True)
class SportsMatch:
    context: object
    signature: tuple
    title: str
    year: int
    hash_matched: bool
    candidates: list

    def cache_key(self):
        # Re-resolve before the cache so owner/default/path/index edits cannot
        # keep returning the previous sports file IDs. Native keys stay intact.
        return _digest(
            (
                self.context,
                self.signature,
                self.title,
                self.year,
                self.hash_matched,
                self.candidates,
            )
        )


def _candidates(session, context, row):
    from app.database import TableArrInstances
    from sportarr.output import SportsOutputNamespace, validate_read_path
    from utilities.path_mappings import apply_sports_mapping, read_sports_mappings

    owner = session.get(
        TableArrInstances, context.arr_instance_id, populate_existing=True
    )
    mappings = read_sports_mappings(owner.path_mappings)
    mapped = [
        [item[0], apply_sports_mapping(item[1], mappings)]
        for item in local._parse_subtitles_blob(row.subtitles)
        if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], str)
    ]
    # Request all indexed languages here; search_entries applies client languages.
    languages = [
        item[0].split(":", 1)[0] for item in mapped if isinstance(item[0], str)
    ]
    candidates = local._select_local_subs(
        repr(mapped),
        os.path.dirname(context.mapped_path),
        languages,
        media_path=context.mapped_path,
        create_target=False,
    )
    result = []
    namespaces = {}
    for candidate in candidates:
        try:
            validate_read_path(context, candidate["path"])
            directory = os.path.dirname(candidate["path"])
            if directory not in namespaces:
                namespaces[directory] = SportsOutputNamespace(
                    context, session, read_path=candidate["path"]
                )
            namespaces[directory].validate(session)
            candidate["stat"] = _file_stat(os.stat(candidate["path"]))
        except (OSError, ValueError):
            continue
        result.append(candidate)
    return result


def resolve_for_request(
    imdb_id, season, episode, media_type, query, moviehash, moviehash_match=None
):
    """Return the one deterministic sports file, retaining native IMDb precedence."""
    if not query and not valid_moviehash(moviehash):
        return None
    if imdb_id and local._resolve_by_imdb(imdb_id, season, episode, media_type):
        return None
    if moviehash_match == "only" and not valid_moviehash(moviehash):
        return None
    from app.database import database, select, TableArrInstances, TableSportsEvents
    from sportarr.identity import resolve_event_in_session
    from sportarr.subtitles import candidate_signature
    from utilities.path_mappings import apply_sports_mapping, read_sports_mappings

    try:
        rows = database.execute(
            select(
                TableSportsEvents.id,
                TableSportsEvents.path,
                TableSportsEvents.sceneName,
                TableArrInstances.id.label("owner_id"),
                TableArrInstances.path_mappings,
                TableArrInstances.is_default,
                TableArrInstances.stable_key,
            )
            .join(
                TableArrInstances,
                TableSportsEvents.arr_instance_id == TableArrInstances.id,
            )
            .where(TableArrInstances.kind == "sportarr", TableArrInstances.enabled == 1)
        ).all()
        target = str(moviehash).strip().lower() if valid_moviehash(moviehash) else None
        filename = _basename(query)
        hits = []
        for row in rows:
            try:
                mapped = apply_sports_mapping(
                    row.path, read_sports_mappings(row.path_mappings)
                )
                agrees = bool(
                    filename
                    and filename
                    in {
                        _basename(row.path),
                        _basename(mapped),
                        _basename(row.sceneName),
                    }
                )
                if not target and not agrees:
                    continue
                if not os.path.isfile(mapped):
                    continue
                physical = os.path.realpath(mapped)
                stamp = tuple(_file_stat(os.stat(physical)))
                hashed = bool(target and _hash_file(physical, stamp) == target)
            except (OSError, ValueError):
                # An unavailable recording must not hide other owners' files.
                continue
            if not hashed and (not agrees or moviehash_match == "only"):
                continue
            order = (
                not hashed,
                not agrees,
                not bool(row.is_default),
                row.stable_key,
                row.id,
            )
            hits.append((order, row, hashed, physical, stamp))
        if not hits:
            return None
        _, row, hashed, physical, stamp = min(hits, key=lambda hit: hit[0])
        context = resolve_event_in_session(database, row.id, row.owner_id)
        row = database.execute(
            select(TableSportsEvents)
            .where(
                TableSportsEvents.id == context.event_id,
                TableSportsEvents.arr_instance_id == context.arr_instance_id,
            )
            .execution_options(populate_existing=True)
        ).scalar_one()
        signature = candidate_signature(context, database)
        if (
            os.path.realpath(context.mapped_path) != physical
            or tuple(_file_stat(os.stat(context.mapped_path))) != stamp
            or candidate_signature(context, database) != signature
        ):
            return None
        candidates = _candidates(database, context, row)
        date = row.eventDate or row.broadcastDate or ""
        year = int(str(date)[:4]) if str(date)[:4].isdigit() else 0
        return SportsMatch(context, signature, row.title, year, hashed, candidates)
    except (OSError, ValueError):
        return None
    except Exception:
        # A missing/unmigrated sports table must not break native Hub searches.
        logger.debug("Sports library resolution unavailable", exc_info=True)
        return None


def _file_stat(value):
    return [
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    ]


def _read_bounded(path):
    # Do not block on a replaced FIFO or follow a final symlink. The path itself
    # was resolved by the owner validator, and is checked again after the read.
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > local._MAX_SUB_BYTES:
            raise ValueError("Sports subtitle is not a bounded regular file")
        raw = stream.read(local._MAX_SUB_BYTES + 1)
        if len(raw) > local._MAX_SUB_BYTES or _file_stat(before) != _file_stat(
            os.fstat(stream.fileno())
        ):
            raise ValueError("Sports subtitle changed during read")
    return raw, _file_stat(before)


def _artifact(context, signature, path):
    from sportarr.artifacts import validate_artifact_stat
    from sportarr.output import validate_read_path

    validate_read_path(context, path)
    raw, stamp = _read_bounded(path)
    # Use the sports artifact contract, with a bounded read for Hub requests.
    proof = json.dumps(
        {
            "version": 1,
            "video": _digest(signature),
            "path": os.path.realpath(path),
            "stat": stamp,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        sort_keys=True,
    )
    validate_artifact_stat(proof, path)
    return proof


def search_entries(match, languages):
    from . import auth, response_mapper

    requested = [local._parse_request_bcp47(code) for code in languages if code]
    language_map = local._build_request_to_lang_map(languages)
    context = match.context
    result = []
    for candidate in match.candidates:
        lang = candidate["lang"]
        if not any(
            local._lang_matches(lang, base, region) for base, region in requested
        ):
            continue
        try:
            proof = _artifact(context, match.signature, candidate["path"])
            sports = {
                "context": context,
                "signature": match.signature,
                "artifact": proof,
                "binding": {
                    "event_id": context.event_id,
                    "arr_instance_id": context.arr_instance_id,
                    "file": _digest(
                        (
                            match.signature,
                            proof,
                            lang,
                            candidate["modifier"],
                            candidate["fmt"],
                        )
                    ),
                },
            }
            payload = {
                "sports": sports,
                "path": candidate["path"],
                "lang": lang,
                "modifier": candidate["modifier"],
                "fmt": candidate["fmt"],
                "media_type": "sports",
                "media_id": context.event_id,
                "arr_instance_id": context.arr_instance_id,
            }
            validate_payload(payload)
            media_dir = os.path.realpath(os.path.dirname(context.mapped_path))
            fid = auth.mint_local_file_id(
                path=candidate["path"],
                lang=lang,
                modifier=candidate["modifier"],
                fmt=candidate["fmt"],
                media_type="sports",
                media_id=context.event_id,
                media_dir=media_dir,
                sports=sports,
                allowed_roots=local._allowed_subtitle_roots(
                    media_dir, context.mapped_path, create_target=False
                ),
            )
            result.append(
                response_mapper.local_to_os_entry(
                    file_id=fid,
                    lang=lang,
                    modifier=candidate["modifier"],
                    filename=candidate["filename"],
                    upload_mtime=candidate["mtime"],
                    media_type="sports",
                    media_id=context.event_id,
                    requested_language=language_map.get(lang.split("-", 1)[0].lower()),
                    title=match.title,
                    year=match.year,
                    hash_matched=match.hash_matched,
                )
            )
        except (OSError, ValueError):
            continue
    return result


def validate_payload(payload):
    from app.database import database, select, TableSportsEvents
    from sportarr.artifacts import validate_artifact_stat
    from sportarr.output import SportsOutputNamespace
    from sportarr.subtitles import candidate_signature

    sports = payload["sports"]
    context = sports["context"]
    if (
        payload["media_type"] != "sports"
        or payload["media_id"] != context.event_id
        or payload["arr_instance_id"] != context.arr_instance_id
        or candidate_signature(context, database) != sports["signature"]
    ):
        raise ValueError("Sports file identity changed")
    SportsOutputNamespace(context, database, read_path=payload["path"]).validate(database)
    row = database.execute(
        select(TableSportsEvents)
        .where(
            TableSportsEvents.id == context.event_id,
            TableSportsEvents.arr_instance_id == context.arr_instance_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one()
    candidates = _candidates(database, context, row)
    if not any(
        (candidate["path"], candidate["lang"], candidate["modifier"], candidate["fmt"])
        == (payload["path"], payload["lang"], payload["modifier"], payload["fmt"])
        for candidate in candidates
    ):
        raise ValueError("Sports subtitle is no longer indexed for this owner")
    validate_artifact_stat(sports["artifact"], payload["path"])
    binding = {
        "event_id": context.event_id,
        "arr_instance_id": context.arr_instance_id,
        "file": _digest(
            (
                sports["signature"],
                sports["artifact"],
                payload["lang"],
                payload["modifier"],
                payload["fmt"],
            )
        ),
    }
    if sports["binding"] != binding:
        raise ValueError("Sports capability identity changed")


def serve(payload):
    try:
        validate_payload(payload)
        raw, stamp = _read_bounded(payload["path"])
        proof = json.loads(payload["sports"]["artifact"])
        if stamp != proof["stat"] or hashlib.sha256(raw).hexdigest() != proof["sha256"]:
            raise ValueError("Sports subtitle artifact changed")
        validate_payload(payload)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FileNotFoundError("Sports subtitle is no longer available") from exc
    if payload["fmt"] == "srt":
        return local._normalize_srt(raw), "application/x-subrip"
    return local._convert_to_srt(raw, payload["fmt"]), "application/x-subrip"
