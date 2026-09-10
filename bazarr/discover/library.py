"""Bounded scalar library projections. Reads never adopt a media file."""
from copy import deepcopy
import hashlib
import json
import os
import re
import stat

from sqlalchemy import and_, case, func, or_, select

CANDIDATE_LIMIT = 12
SHORTLIST_LIMIT = 48
COPY_LIMIT = 120


def _imdb(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if re.fullmatch(r"tt\d{7,10}", value) else None


def _positive(value):
    value = str(value).strip() if type(value) in (int, str) else ""
    return int(value) if re.fullmatch(r"0*[1-9]\d{0,12}", value) and int(value) < 2**53 else None


def _identity(item):
    return {key: value for key in ("imdb_id", "tmdb_id", "tvdb_id") if (value := item.get(key)) is not None}


def matches(left, right):
    """Require one reliable same-kind match and reject any known disagreement."""
    if left["media_type"] != right["media_type"]:
        return False
    a, b = _identity(left), _identity(right)
    common = a.keys() & b.keys()
    return bool(common) and all(a[key] == b[key] for key in common)


def _consistent(items):
    known = {}
    for item in items:
        for key, value in _identity(item).items():
            if key in known and known[key] != value:
                return False
            known[key] = value
    return True


def _identity_groups(items):
    # Decide from immutable evidence. A partial identity that is compatible
    # with conflicting complete records cannot choose either record.
    neighbors = [{j for j, other in enumerate(items) if i == j or matches(item, other)}
                 for i, item in enumerate(items)]
    ambiguous = {i for i, peers in enumerate(neighbors) if not _consistent(items[j] for j in peers)}
    remaining = set(range(len(items)))
    groups = []
    while remaining:
        root = min(remaining)
        group, pending = set(), [root]
        while pending:
            index = pending.pop()
            if index in group:
                continue
            group.add(index)
            if index not in ambiguous:
                pending.extend(neighbors[index] - ambiguous - group)
        remaining -= group
        ordered = sorted(group)
        if _consistent(items[i] for i in ordered):
            groups.append(ordered)
        else:
            groups.extend([i] for i in ordered)
    return groups


def _merge_groups(items, assignments):
    groups = []
    for indices in assignments:
        group = deepcopy(items[indices[0]])
        for index in indices[1:]:
            item = items[index]
            for key, value in _identity(item).items():
                if group.get(key) is None:
                    group[key] = value
            existing = {copy["local_id"] for copy in group.get("copies", [])}
            group.setdefault("copies", []).extend(deepcopy(copy) for copy in item.get("copies", [])
                                                  if copy["local_id"] not in existing)
            group["copies_truncated"] = group.get("copies_truncated", False) or item.get("copies_truncated", False)
        groups.append(group)
    return groups


def merge_candidates(items):
    return _merge_groups(items, _identity_groups(items))


def _model(kind):
    from app.database import TableMovies, TableShows
    return TableMovies if kind == "movie" else TableShows


def _columns(kind):
    table = _model(kind)
    external = table.tmdbId if kind == "movie" else table.tvdbId
    return (table.id, table.arr_instance_id, table.title, table.year, table.imdbId,
            external.label("external_id"), table.updated_at_timestamp)


def _project(row, kind):
    year = _positive(row.year)
    imdb = _imdb(row.imdbId)
    return {"source": "local", "source_id": f"local:{kind}:{row.id}", "id": row.id,
            "media_type": kind, "title": row.title[:500], "year": year if year and 1000 <= year <= 9999 else None,
            "imdb_id": imdb, "tmdb_id" if kind == "movie" else "tvdb_id": _positive(row.external_id),
            "mapping_status": "resolved" if imdb else "unresolved", "overview": "",
            "poster_url": None, "backdrop_url": None,
            **({"seasons": None} if kind == "show" else {}),
            "copies": [{"local_id": row.id, "arr_instance_id": row.arr_instance_id,
                        "updated_at": row.updated_at_timestamp.isoformat() if row.updated_at_timestamp else None,
                        "episode_count": None}], "copies_truncated": False}


def _identity_predicates(item, table):
    predicates = [table.id.in_([copy["local_id"] for copy in item.get("copies", [])])] if item.get("copies") else []
    if item.get("imdb_id"):
        predicates.append(func.lower(func.trim(table.imdbId)) == item["imdb_id"])
    if item["media_type"] == "movie" and item.get("tmdb_id"):
        predicates.append(func.ltrim(func.trim(table.tmdbId), "0") == str(item["tmdb_id"]))
    if item["media_type"] == "show" and item.get("tvdb_id"):
        predicates.append(table.tvdbId == item["tvdb_id"])
    return predicates


def _ownership(connection, items):
    from app.database import TableEpisodes as Episode
    from app.database import TableShows as Show
    ids = {copy["local_id"] for item in items if item["media_type"] == "show" for copy in item.get("copies", [])}
    counts = {}
    if ids:
        statement = (select(Episode.series_id, func.count(Episode.id))
                     .join(Show, Show.id == Episode.series_id)
                     .where(Episode.series_id.in_(ids), Show.arr_instance_id.is_not(None),
                            Episode.arr_instance_id == Show.arr_instance_id)
                     .group_by(Episode.series_id))
        counts = dict(connection.execute(statement).all())
    for item in items:
        if item["media_type"] != "show":
            continue
        for copy in item.get("copies", []):
            copy["episode_count"] = counts.get(copy["local_id"], 0) if copy["arr_instance_id"] is not None else None
        item["ownership"] = {"episode_count": sum(copy["episode_count"] or 0 for copy in item.get("copies", [])),
                             "unknown_owners": any(copy["arr_instance_id"] is None for copy in item.get("copies", [])),
                             "truncated": item.get("copies_truncated", False),
                             "selected_episode_owned": None, "complete_series": None}


def _expand(connection, items, *, merge=False):
    candidate_groups = []
    for kind in ("movie", "show"):
        selected_indices = [i for i, item in enumerate(items) if item["media_type"] == kind]
        selected = [items[i] for i in selected_indices]
        table = _model(kind)
        predicates = [predicate for item in selected for predicate in _identity_predicates(item, table)]
        if not predicates:
            candidate_groups.extend([i] for i in selected_indices)
            continue
        seed_ids = {copy["local_id"] for item in selected for copy in item.get("copies", [])}
        order = (case((table.id.in_(seed_ids), 0), else_=1), table.id) if seed_ids else (table.id,)
        rows = connection.execute(select(*_columns(kind)).where(or_(*predicates)).order_by(*order).limit(COPY_LIMIT + 1)).all()
        projected = [_project(row, kind) for row in rows[:COPY_LIMIT]]
        truncated = len(rows) > COPY_LIMIT
        evidence = selected + projected
        groups = _identity_groups(evidence)
        assignments = {index: group for group in groups for index in group}
        for group in groups:
            selected_group = [i for i in group if i < len(selected)]
            if not selected_group:
                continue
            if truncated:
                exact = {}
                for i in selected_group:
                    identity = tuple(sorted(_identity(selected[i]).items()))
                    if len(identity) >= 2:
                        exact.setdefault(identity, []).append(selected_indices[i])
                    else:
                        candidate_groups.append([selected_indices[i]])
                candidate_groups.extend(exact.values())
            else:
                candidate_groups.append([selected_indices[i] for i in selected_group])
        for index, item in enumerate(selected):
            # Canonical seeds survive independently from optional association.
            copies = {copy["local_id"]: deepcopy(copy) for copy in item.get("copies", [])}
            compatible = [evidence[i] for i in assignments[index] if i >= len(selected)]
            if truncated:
                # An unseen row might contradict a partial completion. Exact
                # complete identities need no inference from a missing row.
                compatible = [other for other in compatible
                              if len(_identity(item)) >= 2 and _identity(item) == _identity(other)]
            for other in compatible:
                for copy in other["copies"]:
                    copies.setdefault(copy["local_id"], deepcopy(copy))
                for key, value in _identity(other).items():
                    if item.get(key) is None:
                        item[key] = value
            item["copies"] = list(copies.values())
            item["copies_truncated"] = item.get("copies_truncated", False) or truncated
    if merge:
        # Keep the full-evidence decision through the final composition. A
        # smaller display shortlist must not infer the association again.
        items = _merge_groups(items, sorted(candidate_groups, key=lambda group: group[0]))
    _ownership(connection, items)
    return items


def _candidate_titles(query, media_type, limit, catalog_items):
    """Return limited literal title matches; LIMIT does not bound physical scans."""
    from app.database import engine
    if (not isinstance(query, str) or not query.strip() or len(query) > 200
            or media_type not in (None, "movie", "show") or type(limit) is not int or not 1 <= limit <= CANDIDATE_LIMIT):
        raise ValueError("Invalid local title query.")
    escaped = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    items = deepcopy(catalog_items)
    truncated = False
    # A Core connection cannot autoflush unrelated pending ORM work.
    with engine.connect() as connection:
        for kind in ((media_type,) if media_type else ("movie", "show")):
            table = _model(kind)
            rows = connection.execute(select(*_columns(kind)).where(
                func.lower(func.coalesce(table.title, "")).like("%" + escaped + "%", escape="\\"))
                .order_by(table.id).limit(SHORTLIST_LIMIT + 1)).all()
            truncated |= len(rows) > SHORTLIST_LIMIT
            items.extend(_project(row, kind) for row in rows[:SHORTLIST_LIMIT])
        merged = _expand(connection, items, merge=True)
        local = [item for item in merged if item["source"] == "local"]
        catalog = [item for item in merged if item["source"] != "local"]
        truncated |= len(local) > limit
        result = catalog + local[:limit]
    return {"items": result, "truncated": truncated, "match_scope": "literal_title_contains"}


def local_candidates(query, media_type=None, limit=CANDIDATE_LIMIT):
    return _candidate_titles(query, media_type, limit, [])


def combined_candidates(catalog_items, query, media_type):
    """Compose bounded catalog and local seeds before resolving associations."""
    return _candidate_titles(query, media_type, CANDIDATE_LIMIT, catalog_items)


def attach_local_copies(items):
    from app.database import engine
    with engine.connect() as connection:
        return _expand(connection, deepcopy(items))


def local_details(local_id, media_type):
    from app.database import engine
    if media_type not in ("movie", "show") or not isinstance(local_id, str) or not re.fullmatch(r"[1-9]\d{0,12}", local_id):
        raise ValueError("Invalid local identity.")
    table = _model(media_type)
    with engine.connect() as connection:
        row = connection.execute(select(*_columns(media_type)).where(table.id == int(local_id))).first()
        return _expand(connection, [_project(row, media_type)])[0] if row else None


# --- Explicit, ownership-safe copy matching ---------------------------------
#
# Two entry points with different rules, so be exact about which is which.
# copy_options() runs whenever a confirmed target is on screen and adopts
# nothing: it projects columns and never opens a file. resolve_copy(), and
# everything that reaches a Video through it, runs only for a copy a reader
# explicitly chose. A title, an IMDb id or a library that happens to hold
# exactly one file never selects one.
#
# The native pipeline is deliberately not reused here. get_video() resolves a
# row by globally reverse-mapped path with no owner predicate, runs every
# registered refiner, probes media and writes the ffprobe cache. None of that
# is an ownership-safe read-only boundary for a copy the reader picked. What is
# reused is the validated scalar projection, the established bounded string
# parsing and the existing per-instance forward path-mapping seam.

COPY_OPTION_LIMIT = 24
COPY_HASH_MIN_BYTES = 10 * 1024 * 1024
_COPY_ID = re.compile(r"c1\.(movie|episode)\.([1-9]\d{0,12})\.(0|[1-9]\d{0,12}|x)")


class CopyUnavailable(ValueError):
    """The chosen copy does not resolve to the confirmed target.

    Not "no longer": a malformed identity and one naming no owner never
    resolved at all, and the same refusal covers both.

    Deliberately a ValueError so an unhandled path still refuses the search
    rather than falling through to some other file. Callers that can offer
    recovery read ``reason``.
    """

    def __init__(self, message, reason="copy_unavailable"):
        super().__init__(message)
        self.reason = reason


# One message for every refusal, so it has to be true of every one of them,
# including an identity that was never valid rather than one that expired.
_RECOVERY = ("This library copy cannot be used for this title. "
             "Choose another copy or search the title only.")


def copy_identity(media_type, local_id, arr_instance_id):
    """Opaque to the client: it is re-resolved and revalidated on every use."""
    return f"c1.{media_type}.{local_id}.{'x' if arr_instance_id is None else arr_instance_id}"


def _parse_copy_identity(copy_id):
    match = _COPY_ID.fullmatch(copy_id) if isinstance(copy_id, str) else None
    if match is None:
        return None
    owner = match.group(3)
    return match.group(1), int(match.group(2)), None if owner == "x" else int(owner)


def _copy_target(target):
    if not isinstance(target, dict):
        raise ValueError("Choose a movie or an exact episode.")
    media_type = target.get("media_type")
    imdb = _imdb(target.get("imdb_id"))
    if media_type not in ("movie", "episode") or not imdb:
        raise ValueError("Choose a movie or an exact episode with a valid IMDb ID.")
    season = episode = None
    if media_type == "episode":
        season, episode = target.get("season"), target.get("episode")
        if (type(season) is not int or type(episode) is not int
                or not 0 <= season <= 9999 or not 1 <= episode <= 9999):
            raise ValueError("Choose an exact season and episode.")
    return media_type, imdb, season, episode


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _copy_statement(media_type, imdb, season, episode, local_id=None, owner=None):
    from app.database import TableEpisodes as Episode
    from app.database import TableMovies as Movie
    from app.database import TableShows as Show
    if media_type == "movie":
        table = Movie
        statement = select(
            Movie.id, Movie.arr_instance_id, Movie.title, Movie.year, Movie.path,
            Movie.sceneName, Movie.format, Movie.resolution, Movie.video_codec,
            Movie.audio_codec, Movie.file_size, Movie.movie_file_id.label("file_id"),
            Movie.updated_at_timestamp,
        ).where(func.lower(func.trim(Movie.imdbId)) == imdb)
    else:
        table = Episode
        statement = select(
            Episode.id, Episode.arr_instance_id, Episode.title, Episode.path,
            Episode.sceneName, Episode.format, Episode.resolution, Episode.video_codec,
            Episode.audio_codec, Episode.file_size,
            Episode.episode_file_id.label("file_id"), Episode.updated_at_timestamp,
            Show.id.label("series_id"), Show.title.label("series_title"),
            Show.year.label("series_year"),
        ).join(Show, Show.id == Episode.series_id).where(
            func.lower(func.trim(Show.imdbId)) == imdb,
            Episode.season == season, Episode.episode == episode,
            # A show row owned by one instance never lends file context to an
            # episode row owned by another.
            or_(Episode.arr_instance_id == Show.arr_instance_id,
                and_(Episode.arr_instance_id.is_(None), Show.arr_instance_id.is_(None))),
        )
    if local_id is not None:
        statement = statement.where(table.id == local_id)
        statement = statement.where(table.arr_instance_id.is_(None) if owner is None
                                    else table.arr_instance_id == owner)
    return statement.order_by(table.arr_instance_id.is_(None), table.arr_instance_id, table.id)


def _copy_option(row, media_type):
    scene = _text(row.sceneName, 500)
    stored = _text(row.path, 4000)
    filename = os.path.basename(stored) if stored else None
    reason = ("owner_unknown" if row.arr_instance_id is None
              else "no_stored_path" if not filename else None)
    return {
        "copy_id": copy_identity(media_type, row.id, row.arr_instance_id),
        "media_type": media_type, "local_id": row.id,
        "arr_instance_id": row.arr_instance_id, "instance_name": None,
        "series_local_id": getattr(row, "series_id", None),
        "title": _text(getattr(row, "series_title", None) if media_type == "episode" else row.title),
        "episode_title": _text(row.title) if media_type == "episode" else None,
        "release": scene, "filename": filename,
        "source": _text(row.format, 100), "resolution": _text(row.resolution, 100),
        "video_codec": _text(row.video_codec, 100), "audio_codec": _text(row.audio_codec, 100),
        "file_size": row.file_size if isinstance(row.file_size, int) else None,
        "updated_at": row.updated_at_timestamp.isoformat() if row.updated_at_timestamp else None,
        "selectable": reason is None, "unavailable_reason": reason,
    }


def _instance_names(connection, owners):
    from app.database import TableArrInstances as Instance
    owners = {owner for owner in owners if owner is not None}
    if not owners:
        return {}
    rows = connection.execute(select(Instance.id, Instance.name)
                              .where(Instance.id.in_(sorted(owners)))).all()
    return {row.id: _text(row.name, 200) for row in rows}


def _owning_titles(connection, media_type, imdb):
    from app.database import TableMovies as Movie
    from app.database import TableShows as Show
    table = Movie if media_type == "movie" else Show
    return connection.execute(select(func.count(table.id))
                              .where(func.lower(func.trim(table.imdbId)) == imdb)).scalar() or 0


def copy_options(target):
    """Offer the exact copies of one confirmed target. Reads only.

    A show that is owned but has no row for this episode contributes nothing
    here: ``owning_titles`` reports it separately so an absent episode never
    reads as an absent series.

    The list is capped at COPY_OPTION_LIMIT and ``truncated`` says whether the
    cap was reached. A caller must not read absence from a truncated list as
    absence from the library: a copy beyond the cap is still resolvable.
    """
    from app.database import engine
    media_type, imdb, season, episode = _copy_target(target)
    with engine.connect() as connection:
        rows = connection.execute(
            _copy_statement(media_type, imdb, season, episode).limit(COPY_OPTION_LIMIT + 1)).all()
        items = [_copy_option(row, media_type) for row in rows[:COPY_OPTION_LIMIT]]
        names = _instance_names(connection, {item["arr_instance_id"] for item in items})
        for item in items:
            item["instance_name"] = names.get(item["arr_instance_id"])
            if item["selectable"] and item["arr_instance_id"] not in names:
                item["selectable"], item["unavailable_reason"] = False, "instance_missing"
        owning = _owning_titles(connection, media_type, imdb)
    return {"items": items, "truncated": len(rows) > COPY_OPTION_LIMIT,
            "owning_titles": owning, "match_scope": "exact_target_copy"}


def _physical_revision(facts, path, status, file_hash):
    """A digest of what is actually on disk right now, not of stored columns.

    Stored file id, size and update timestamp are all mutable metadata that can
    lag, repeat or change for unrelated reasons, and a local id can be reused
    after a delete. The observed size and modification time are the floor. The
    content hash, when hashing is permitted, is what raises the digest above
    metadata: it catches a same-size replacement that also preserves
    timestamps, which is what an archive restore or a preserving copy produces.

    Be precise about the limits rather than implying more.

    While a hash is present it is the OpenSubtitles digest, file size plus the
    first and last 64 KiB, so a replacement that keeps the size, keeps the
    timestamps and rewrites only bytes between those two windows is not
    detected. Detecting that would mean reading the whole file on every
    request.

    The hash is absent, leaving size and modification time as the whole digest,
    whenever _copy_hash returns None. That is: the reader has skip_hashing on;
    or the file is at or under COPY_HASH_MIN_BYTES, the same 10 MiB floor
    custom_libs/subliminal_patch/core.py uses before it computes provider
    hashes; or the digest could not be computed at all, which covers a read
    error, a file that vanished between the stat and the open, and anything
    else the helper declines. That last case is a catch-all on purpose: it
    stays true if another reason is added.
    """
    material = [facts["media_type"], facts["local_id"], facts["arr_instance_id"],
                facts["file_id"], facts["stored_path"], path, facts["release"],
                facts["file_size"], facts["updated_at"],
                status.st_size, status.st_mtime_ns, file_hash]
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()[:32]


def _copy_hash(path, size):
    """The OpenSubtitles digest of the file as it is right now.

    Deliberately not the module-level `hash_opensubtitles`: subliminal
    decorates it with functools.cache keyed on the path string alone, with no
    size, mtime or inode in the key, and nothing in this application ever
    clears it. Through that wrapper a second call for the same path returns the
    first call's digest for the life of the process, so the read would be paid
    and the replacement would still go unnoticed, and the hash handed to
    hash-capable providers would be the previous file's. `__wrapped__` is the
    same upstream algorithm without the memo, so the digest providers receive
    is unchanged while the observation becomes real.

    The shared memo is left alone for every other caller.

    Returns None, meaning the revision digest falls back to size and
    modification time, when hashing is off, when the file is at or under
    COPY_HASH_MIN_BYTES, or when the digest could not be computed for any
    reason at all.
    """
    from app.config import settings
    if settings.general.skip_hashing or size <= COPY_HASH_MIN_BYTES:
        return None
    try:
        from subliminal_patch.hashes import hash_opensubtitles
        return getattr(hash_opensubtitles, "__wrapped__", hash_opensubtitles)(path)
    except Exception:
        # A hash is an optimization for hash-capable providers and one more
        # revision input. Losing it must never lose the chosen copy.
        return None


def resolve_copy(copy_id, target):
    """Resolve one explicitly chosen copy, or refuse.

    Resolution is by local id plus owning instance, revalidated against the
    confirmed target, so a colliding upstream id can never redirect it.

    It refuses, with a reason, for exactly these causes and no others:
    copy_invalid for a malformed identity or one naming the wrong media kind;
    owner_unknown for an identity that names no owning instance; copy_missing
    when no row matches that local id and owner within this confirmed target,
    which is what a deleted row, a reused local id and a row that now belongs to
    another title all reduce to; instance_missing when the owning arr_instances
    row is gone; no_stored_path when the row records no file; and
    copy_unavailable when the mapped path cannot be stat-ed or is not a regular
    file. Every one of them asks for a new choice instead of quietly searching a
    different file. An invalid target itself raises a plain ValueError before
    any of this.

    _copy_option can also mark a row owner_unknown, but not on this path: the
    identity has already been required to name an owner and the query pins the
    row to it, so the only projection reason reachable here is no_stored_path.
    """
    from app.database import engine
    from utilities.path_mappings import path_mappings
    media_type, imdb, season, episode = _copy_target(target)
    parsed = _parse_copy_identity(copy_id)
    if parsed is None or parsed[0] != media_type:
        raise CopyUnavailable(_RECOVERY, "copy_invalid")
    _, local_id, owner = parsed
    if owner is None:
        raise CopyUnavailable("This library copy has no owning instance, so it cannot be "
                              "verified. Search the title only.", "owner_unknown")
    with engine.connect() as connection:
        row = connection.execute(
            _copy_statement(media_type, imdb, season, episode, local_id, owner).limit(2)).first()
        if row is None:
            raise CopyUnavailable(_RECOVERY, "copy_missing")
        name = _instance_names(connection, {owner}).get(owner)
    if name is None:
        raise CopyUnavailable(_RECOVERY, "instance_missing")
    facts = _copy_option(row, media_type)
    if not facts["selectable"]:
        raise CopyUnavailable(_RECOVERY, facts["unavailable_reason"])
    facts["instance_name"] = name
    facts["stored_path"] = row.path
    facts["file_id"] = row.file_id
    path = path_mappings.path_replace_instance(
        row.path, owner, "movies" if media_type == "movie" else "series")
    try:
        status = os.stat(path)
    except OSError:
        raise CopyUnavailable(_RECOVERY, "copy_unavailable") from None
    if not stat.S_ISREG(status.st_mode):
        raise CopyUnavailable(_RECOVERY, "copy_unavailable")
    file_hash = _copy_hash(path, status.st_size)
    facts["observed_size"] = status.st_size
    facts["file_revision"] = _physical_revision(facts, path, status, file_hash)
    # Private to the server. copy_context() is what may be serialized.
    facts["path"] = path
    facts["file_hash"] = file_hash
    return facts


COPY_CONTEXT_FIELDS = ("copy_id", "media_type", "local_id", "arr_instance_id", "instance_name",
                       "series_local_id", "title", "episode_title", "release", "filename",
                       "source", "resolution", "video_codec", "audio_codec", "file_size",
                       "observed_size", "updated_at")


def copy_context(facts):
    """The client-safe projection.

    Carries the file's basename, which the reader chose and needs to recognise.
    Never the directory, the stored path, the mapped path, the content hash or
    the upstream file id.
    """
    return {key: facts[key] for key in COPY_CONTEXT_FIELDS}
