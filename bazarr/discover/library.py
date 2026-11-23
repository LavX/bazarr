"""Bounded scalar library projections. Reads never adopt a media file."""
from copy import deepcopy
import hashlib
import json
import os
import re
import stat

from sqlalchemy import and_, case, false, func, or_, select

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
    if _is_source_title(left) or _is_source_title(right):
        for item, identity in ((left, a), (right, b)):
            if tmdb := _source_tmdb(item):
                identity["tmdb_id"] = tmdb
        if not {"tmdb_id", "imdb_id"} & a.keys() & b.keys():
            return False
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
            existing = {_copy_key(copy) for copy in group.get("copies", [])}
            group.setdefault("copies", []).extend(deepcopy(copy) for copy in item.get("copies", [])
                                                  if _copy_key(copy) not in existing)
            group["copies_truncated"] = group.get("copies_truncated", False) or item.get("copies_truncated", False)
        groups.append(group)
    return groups


def merge_candidates(items):
    return _merge_groups(items, _identity_groups(items))


def _model(kind):
    from app.database import TableMovies, TableShows
    return TableMovies if kind == "movie" else TableShows


def _copy_key(copy):
    return copy.get("source_id"), copy["local_id"]


def _source_model(kind):
    from app.database import TableSourceGroups, TableSourceItems
    return TableSourceGroups.__table__ if kind == "show" else TableSourceItems.__table__


def _source_library(table, kind, *columns):
    from app.database import TableMediaSourceLibraries, TableMediaSources
    library, source = TableMediaSourceLibraries.__table__, TableMediaSources.__table__
    return (select(*columns).select_from(table)
            .join(library, and_(library.c.id == table.c.library_id, library.c.source_id == table.c.source_id))
            .join(source, source.c.id == table.c.source_id)
            .where(source.c.enabled == 1, library.c.enabled == 1, library.c.content_kind == kind))


def source_items(kind, *columns):
    """Select live file rows of one kind, with source ownership in scope."""
    table = _source_model(kind)
    return (_source_library(table, "movie" if kind == "movie" else "series", *columns)
            .where(table.c.kind == kind, table.c.arr_shadow == 0))


def source_titles(kind, *columns):
    """Select movie files and show groups, excluding files owned by an Arr."""
    if kind == "movie":
        return source_items(kind, *columns)
    group, episode = _source_model("show"), _source_model("episode")
    episodes = (select(episode.c.id).where(episode.c.group_id == group.c.id, episode.c.kind == "episode",
                                          episode.c.library_id == group.c.library_id,
                                          episode.c.source_id == group.c.source_id).correlate(group))
    return _source_library(group, "series", *columns).where(
        group.c.kind == "show", or_(~episodes.exists(), episodes.where(episode.c.arr_shadow == 0).exists()))


def _is_source_title(item):
    return str(item.get("source_id", "")).startswith("source:")


def _source_tmdb(item):
    return _positive(item.get("id") if item.get("source") == "tmdb" else item.get("tmdb_id"))


def _source_id_columns(table):
    return (func.nullif(func.lower(func.trim(table.c.imdb_id)), ""),
            func.nullif(func.ltrim(func.trim(table.c.tmdb_id), "0"), ""))


def _source_identity_predicate(table, imdb, tmdb, *, exact=False):
    imdb_column, tmdb_column = _source_id_columns(table)
    ids = [(column, value) for column, value in ((imdb_column, imdb), (tmdb_column, str(tmdb) if tmdb else None))
           if value is not None]
    if not ids:
        return false()
    same = or_(*(column == value for column, value in ids))
    return and_(same, *(or_(column.is_(None), column == value) for column, value in ids)) if exact else same


def _source_title_projection(kind):
    from app.database import TableMediaSources
    table = _source_model(kind)
    return source_titles(kind, table.c.id, table.c.source_id, TableMediaSources.name.label("source_name"),
                         table.c.title, table.c.year, table.c.imdb_id, table.c.tmdb_id, table.c.tvdb_id,
                         table.c.updated_at)


def _project_source(row, kind):
    imdb, tmdb = _imdb(row.imdb_id), _positive(row.tmdb_id)
    if not (imdb or tmdb):
        return None
    year = _positive(row.year)
    copy = {"source_id": row.source_id, "source_name": row.source_name, "local_id": row.id,
            "arr_instance_id": None, "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "episode_count": None}
    return {"source": "local", "source_id": f"source:{kind}:{row.id}", "id": f"source:{row.id}",
            "media_type": kind, "title": row.title[:500], "year": year if year and 1000 <= year <= 9999 else None,
            "imdb_id": imdb, "tmdb_id": tmdb, "tvdb_id": _positive(row.tvdb_id) if kind == "show" else None,
            "mapping_status": "resolved" if imdb else "unresolved", "overview": "",
            "poster_url": None, "backdrop_url": None, **({"seasons": None} if kind == "show" else {}),
            "copies": [copy], "copies_truncated": False}


def _live(kind):
    """The rows of this kind that are still the reader's library.

    Rows outlive the integration being switched off and their instance being
    disabled. Matched anyway, a retired row labelled a title as in the library,
    linked to it and held back a Seerr request for a copy the reader no longer
    has. This is the scope the Discover library totals use: nothing while the
    integration is off, and no row a disabled instance owns. An instance that
    still owns rows cannot be deleted, so every other owner is an enabled one.
    """
    from app.config import settings
    from app.database import TableArrInstances
    table = _model(kind)
    if not getattr(settings.general, "use_radarr" if kind == "movie" else "use_sonarr", False):
        return false()
    disabled = select(TableArrInstances.id).where(TableArrInstances.enabled == 0)
    return or_(table.arr_instance_id.is_(None), table.arr_instance_id.not_in(disabled))


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
    ids = [copy["local_id"] for copy in item.get("copies", []) if copy.get("source_id") is None]
    predicates = [table.id.in_(ids)] if ids else []
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
    copies = [copy for item in items if item["media_type"] == "show" for copy in item.get("copies", [])]
    ids = {copy["local_id"] for copy in copies if copy.get("source_id") is None}
    source_groups = {copy["local_id"] for copy in copies if copy.get("source_id") is not None}
    counts = {}
    seasons = {}
    if ids:
        # One grouped query stands in for the former pair (an episode count and
        # a distinct-seasons read) that shared the same join and filter: each
        # (series, season) row's count folds into the per-series total, and its
        # season, when not null, joins that series' owned-seasons set.
        statement = (select(Episode.series_id, Episode.season, func.count(Episode.id))
                     .join(Show, Show.id == Episode.series_id)
                     .where(Episode.series_id.in_(ids), Show.arr_instance_id.is_not(None),
                            Episode.arr_instance_id == Show.arr_instance_id)
                     .group_by(Episode.series_id, Episode.season))
        for series_id, season, count in connection.execute(statement).all():
            counts[(None, series_id)] = counts.get((None, series_id), 0) + count
            if season is not None:
                seasons.setdefault((None, series_id), set()).add(int(season))
    if source_groups:
        episode = _source_model("episode")
        group = _source_model("show")
        statement = (source_items("episode", episode.c.source_id, episode.c.group_id, episode.c.season, func.count())
                     .join(group, and_(group.c.id == episode.c.group_id, group.c.library_id == episode.c.library_id,
                                      group.c.source_id == episode.c.source_id))
                     .where(episode.c.group_id.in_(source_groups))
                     .group_by(episode.c.source_id, episode.c.group_id, episode.c.season))
        for source_id, group_id, season, count in connection.execute(statement):
            key = (source_id, group_id)
            counts[key] = counts.get(key, 0) + count
            if season is not None:
                seasons.setdefault(key, set()).add(season)
    for item in items:
        if item["media_type"] != "show":
            continue
        for copy in item.get("copies", []):
            owned = copy["arr_instance_id"] is not None or copy.get("source_id") is not None
            copy["episode_count"] = counts.get(_copy_key(copy), 0) if owned else None
        item["ownership"] = {"episode_count": sum(copy["episode_count"] or 0 for copy in item.get("copies", [])),
                             "unknown_owners": any(copy["episode_count"] is None for copy in item.get("copies", [])),
                             "truncated": item.get("copies_truncated", False),
                             "selected_episode_owned": None, "complete_series": None,
                             "seasons_owned": sorted(set().union(*(seasons.get(_copy_key(copy), set())
                                                                   for copy in item.get("copies", []))))}


def _expand(connection, items, *, merge=False):
    candidate_groups = []
    for kind in ("movie", "show"):
        selected_indices = [i for i, item in enumerate(items) if item["media_type"] == kind]
        selected = [items[i] for i in selected_indices]
        table = _model(kind)
        predicates = [predicate for item in selected for predicate in _identity_predicates(item, table)]
        source_table = _source_model(kind)
        source_predicates = [_source_identity_predicate(source_table, item.get("imdb_id"), _source_tmdb(item))
                             for item in selected if item.get("imdb_id") or _source_tmdb(item)]
        if not predicates and not source_predicates:
            candidate_groups.extend([i] for i in selected_indices)
            continue
        seed_ids = {copy["local_id"] for item in selected for copy in item.get("copies", [])
                    if copy.get("source_id") is None}
        order = (case((table.id.in_(seed_ids), 0), else_=1), table.id) if seed_ids else (table.id,)
        rows = connection.execute(select(*_columns(kind)).where(or_(*predicates), _live(kind))
                                  .order_by(*order).limit(COPY_LIMIT + 1)).all() if predicates else []
        source_rows = connection.execute(_source_title_projection(kind).where(or_(*source_predicates))
                                         .order_by(source_table.c.id).limit(COPY_LIMIT + 1)).all() if source_predicates else []
        if not predicates and not source_rows:
            candidate_groups.extend([i] for i in selected_indices)
            continue
        projected = [_project(row, kind) for row in rows[:COPY_LIMIT]]
        projected.extend(item for row in source_rows[:COPY_LIMIT] if (item := _project_source(row, kind)))
        truncated = len(rows) > COPY_LIMIT or len(source_rows) > COPY_LIMIT
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
            copies = {_copy_key(copy): deepcopy(copy) for copy in item.get("copies", [])}
            compatible = [evidence[i] for i in assignments[index] if i >= len(selected)]
            if truncated:
                # An unseen row might contradict a partial completion. Exact
                # complete identities need no inference from a missing row.
                compatible = [other for other in compatible
                              if len(_identity(item)) >= 2 and _identity(item) == _identity(other)]
            for other in compatible:
                for copy in other["copies"]:
                    copies.setdefault(_copy_key(copy), deepcopy(copy))
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
                func.lower(func.coalesce(table.title, "")).like("%" + escaped + "%", escape="\\"),
                _live(kind))
                .order_by(table.id).limit(SHORTLIST_LIMIT + 1)).all()
            truncated |= len(rows) > SHORTLIST_LIMIT
            items.extend(_project(row, kind) for row in rows[:SHORTLIST_LIMIT])
            source_table = _source_model(kind)
            imdb, tmdb = _source_id_columns(source_table)
            rows = connection.execute(_source_title_projection(kind).where(
                func.lower(source_table.c.title).like("%" + escaped + "%", escape="\\"),
                or_(imdb.is_not(None), tmdb.is_not(None)))
                .order_by(source_table.c.id).limit(SHORTLIST_LIMIT + 1)).all()
            truncated |= len(rows) > SHORTLIST_LIMIT
            items.extend(item for row in rows[:SHORTLIST_LIMIT] if (item := _project_source(row, kind)))
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
    if (media_type not in ("movie", "show") or not isinstance(local_id, str)
            or not re.fullmatch(r"(?:source:)?[1-9]\d{0,12}", local_id)):
        raise ValueError("Invalid local identity.")
    with engine.connect() as connection:
        if local_id.startswith("source:"):
            table = _source_model(media_type)
            row = connection.execute(_source_title_projection(media_type)
                                     .where(table.c.id == int(local_id.removeprefix("source:")))).first()
            item = _project_source(row, media_type) if row else None
        else:
            table = _model(media_type)
            row = connection.execute(select(*_columns(media_type)).where(table.id == int(local_id))).first()
            item = _project(row, media_type) if row else None
        return _expand(connection, [item])[0] if item else None


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
_COPY_ID = re.compile(r"c1\.(movie|episode)\.([1-9]\d{0,12})\.(0|s?[1-9]\d{0,12}|x)")


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
    is_source = owner.startswith("s")
    return match.group(1), int(match.group(2)), None if owner == "x" else int(owner.removeprefix("s")), is_source


def _copy_target(target):
    if not isinstance(target, dict):
        raise ValueError("Choose a movie or an exact episode.")
    media_type = target.get("media_type")
    imdb = _imdb(target.get("imdb_id"))
    tmdb = _positive(target.get("tmdb_id"))
    if (media_type not in ("movie", "episode") or not (imdb or tmdb)
            or (target.get("imdb_id") is not None and not imdb) or (target.get("tmdb_id") is not None and not tmdb)):
        raise ValueError("Choose a movie or an exact episode with a valid IMDb or TMDB ID.")
    season = episode = None
    if media_type == "episode":
        season, episode = target.get("season"), target.get("episode")
        if (type(season) is not int or type(episode) is not int
                or not 0 <= season <= 9999 or not 1 <= episode <= 9999):
            raise ValueError("Choose an exact season and episode.")
    return media_type, imdb, tmdb, season, episode


def _source_episode_predicate(item, season, episode, evidence):
    numeric = and_(item.c.season == season, item.c.episode == episode)
    if evidence is None:
        return numeric
    original = (evidence.get("season"), evidence.get("episode"))
    if (type(original[0]) is not int or type(original[1]) is not int
            or not 0 <= original[0] <= 9999 or not 1 <= original[1] <= 9999):
        raise ValueError("Choose an exact source season and episode.")
    imdb_column, tmdb_column = _source_id_columns(item)
    imdb, tmdb, tvdb = _imdb(evidence.get("imdb_id")), _positive(evidence.get("id")), _positive(evidence.get("tvdb_id"))
    if any(evidence.get(key) is not None and value is None
           for key, value in (("imdb_id", imdb), ("id", tmdb), ("tvdb_id", tvdb))):
        raise ValueError("Invalid source episode identity.")
    ids = [(column, value) for column, value in ((imdb_column, imdb), (tmdb_column, str(tmdb) if tmdb else None),
                                                (func.nullif(item.c.tvdb_id, 0), tvdb)) if value is not None]
    same = [column == value for column, value in ids]
    if original == (season, episode):
        same.append(numeric)
    # Source files can carry either numbering scheme. When the catalog and
    # Arr numbering differ, only an episode-level ID can establish a match.
    return and_(or_(false(), *same), *(or_(column.is_(None), column == value) for column, value in ids))


def _arr_movie_tmdb_matches(movie, tmdb):
    normalized = func.ltrim(func.trim(movie.tmdbId), "0")
    return or_(normalized.is_(None), normalized == "", func.length(normalized) > 13,
               func.ltrim(normalized, "0123456789") != "", normalized == str(tmdb))


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _copy_statement(media_type, imdb, season, episode, local_id=None, owner=None, *, tmdb=None):
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
        ).where(func.lower(func.trim(Movie.imdbId)) == imdb, _live("movie"))
        if tmdb is not None:
            statement = statement.where(_arr_movie_tmdb_matches(Movie, tmdb))
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
            # The pairing above gives the episode its show's owner, so the
            # show's library scope covers the episode too.
            _live("show"),
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


def _owning_titles(connection, media_type, imdb, tmdb):
    from app.database import TableMovies as Movie
    from app.database import TableShows as Show
    table = Movie if media_type == "movie" else Show
    kind = "movie" if media_type == "movie" else "show"
    statement = select(func.count(table.id)).where(func.lower(func.trim(table.imdbId)) == imdb, _live(kind))
    if kind == "movie" and tmdb is not None:
        statement = statement.where(_arr_movie_tmdb_matches(Movie, tmdb))
    return connection.execute(statement).scalar() or 0


def _source_copy_statement(media_type, imdb, tmdb, season, episode, *, episode_identity=None):
    from app.database import TableMediaSources
    item = _source_model(media_type)
    group = _source_model("show")
    title = item if media_type == "movie" else group
    statement = source_items(media_type, item.c.id, item.c.path, item.c.source_id, item.c.library_id,
                             TableMediaSources.name.label("source_name"), title.c.title, title.c.year,
                             item.c.group_id, item.c.season.label("source_season"), item.c.episode.label("source_episode"),
                             item.c.episode_title, item.c.scene_name, item.c.format,
                             item.c.resolution, item.c.video_codec, item.c.audio_codec, item.c.file_size,
                             item.c.updated_at, title.c.imdb_id, title.c.tmdb_id)
    if media_type == "episode":
        statement = (statement.join(group, and_(group.c.id == item.c.group_id, group.c.source_id == item.c.source_id,
                                               group.c.library_id == item.c.library_id, group.c.kind == "show"))
                     .where(_source_episode_predicate(item, season, episode, episode_identity)))
    return statement.where(_source_identity_predicate(title, imdb, tmdb, exact=True)).order_by(item.c.id)


def _source_copy_option(row, media_type):
    filename = os.path.basename(row.path) if row.path else None
    return {"copy_id": copy_identity(media_type, row.id, f"s{row.source_id}"),
            "media_type": media_type, "local_id": row.id, "arr_instance_id": None, "instance_name": None,
            "source_id": row.source_id, "source_name": _text(row.source_name, 200),
            "series_local_id": row.group_id, "title": _text(row.title), "episode_title": _text(row.episode_title),
            "release": _text(row.scene_name, 500), "filename": _text(filename, 4000),
            "source": _text(row.format, 100), "resolution": _text(row.resolution, 100),
            "video_codec": _text(row.video_codec, 100), "audio_codec": _text(row.audio_codec, 100),
            "file_size": row.file_size, "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "selectable": bool(filename), "unavailable_reason": None if filename else "no_stored_path"}


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
    media_type, imdb, tmdb, season, episode = _copy_target(target)
    with engine.connect() as connection:
        rows = connection.execute(
            _copy_statement(media_type, imdb, season, episode, tmdb=tmdb).limit(COPY_OPTION_LIMIT + 1)
        ).all() if imdb else []
        items = [_copy_option(row, media_type) for row in rows[:COPY_OPTION_LIMIT]]
        names = _instance_names(connection, {item["arr_instance_id"] for item in items})
        for item in items:
            item["instance_name"] = names.get(item["arr_instance_id"])
            if item["selectable"] and item["arr_instance_id"] not in names:
                item["selectable"], item["unavailable_reason"] = False, "instance_missing"
        source_rows = connection.execute(_source_copy_statement(
            media_type, imdb, tmdb, season, episode, episode_identity=target.get("episode_identity"))
            .limit(COPY_OPTION_LIMIT + 1 - len(items))).all()
        items.extend(_source_copy_option(row, media_type) for row in source_rows[:COPY_OPTION_LIMIT - len(items)])
        owning = _owning_titles(connection, media_type, imdb, tmdb) if imdb else 0
        kind = "movie" if media_type == "movie" else "show"
        owning += connection.execute(source_titles(kind, func.count()).where(
            _source_identity_predicate(_source_model(kind), imdb, tmdb, exact=True))).scalar() or 0
    return {"items": items, "truncated": len(rows) + len(source_rows) > COPY_OPTION_LIMIT,
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
    if facts.get("source_id") is not None:
        material.extend((facts["source_id"], facts["source_season"], facts["source_episode"], status.st_dev, status.st_ino))
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
    """Revalidate one selected file against its origin, owner and confirmed target."""
    from app.database import engine
    from utilities.path_mappings import path_mappings
    media_type, imdb, tmdb, season, episode = _copy_target(target)
    parsed = _parse_copy_identity(copy_id)
    if parsed is None or parsed[0] != media_type:
        raise CopyUnavailable(_RECOVERY, "copy_invalid")
    _, local_id, owner, is_source = parsed
    if owner is None:
        raise CopyUnavailable("This library copy has no owning instance, so it cannot be "
                              "verified. Search the title only.", "owner_unknown")
    if is_source:
        item = _source_model(media_type)
        evidence = None if target.get("manual_confirmed") else target.get("episode_identity")
        with engine.connect() as connection:
            row = connection.execute(_source_copy_statement(media_type, imdb, tmdb, season, episode,
                                                            episode_identity=evidence)
                                     .where(item.c.id == local_id, item.c.source_id == owner)).first()
        if row is None:
            raise CopyUnavailable(_RECOVERY, "copy_missing")
        facts = _source_copy_option(row, media_type)
        facts.update(file_id=row.library_id, imdb_id=_imdb(row.imdb_id), tmdb_id=_positive(row.tmdb_id),
                     year=_positive(row.year), source_season=row.source_season, source_episode=row.source_episode)
        path = row.path
    else:
        if not imdb:
            raise CopyUnavailable(_RECOVERY, "copy_missing")
        with engine.connect() as connection:
            row = connection.execute(
                _copy_statement(media_type, imdb, season, episode, local_id, owner, tmdb=tmdb).limit(2)).first()
            if row is None:
                raise CopyUnavailable(_RECOVERY, "copy_missing")
            name = _instance_names(connection, {owner}).get(owner)
        if name is None:
            raise CopyUnavailable(_RECOVERY, "instance_missing")
        facts = _copy_option(row, media_type)
        facts.update(instance_name=name, file_id=row.file_id)
        path = row.path
    if not facts["selectable"]:
        raise CopyUnavailable(_RECOVERY, facts["unavailable_reason"])
    facts["stored_path"] = row.path
    if not is_source:
        path = path_mappings.path_replace_instance(path, owner, "movies" if media_type == "movie" else "series")
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


def require_copy_revision(context):
    """Check that an explicit choice still names the physical file it described."""
    if context.get("copy_id"):
        facts = resolve_copy(context["copy_id"], context)
        if facts["file_revision"] != context.get("file_revision"):
            raise CopyUnavailable(_RECOVERY)


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
    context = {key: facts[key] for key in COPY_CONTEXT_FIELDS}
    if facts.get("source_id") is not None:
        context.update(source_id=facts["source_id"], source_name=facts["source_name"])
    return context
