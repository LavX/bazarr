"""Bounded scalar library projections. Reads never adopt a media file."""
from copy import deepcopy
import re

from sqlalchemy import case, func, or_, select

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
