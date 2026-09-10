"""Global weekly metadata feeds, independent of local media and provider search."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
import threading
import time

from utilities.locked_lru import LockedLRU
from . import metadata

FRESH_SECONDS = 300
STALE_SECONDS = 3600
RETRY_SECONDS = 30
CALL_SECONDS = 12
MAX_JOBS = 2
MAX_ITEMS = 20
_cache = LockedLRU(maxsize=64)
_jobs = {}
_lock = threading.Lock()


@dataclass
class _Job:
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "unavailable"


def _now():
    return datetime.now(timezone.utc)


def _load(config, media_type, deadline):
    kind = {"all": "all", "movie": "movie", "series": "tv"}[media_type]
    raw = metadata._request(config, f"/trending/{kind}/week", {"language": config.locale, "page": 1})
    rows = raw.get("results")
    if not isinstance(rows, list):
        raise metadata.UpstreamFailure()
    if time.monotonic() >= deadline:
        raise metadata.UpstreamFailure()
    # One configuration request at most, no per-title hydration or local lookup.
    images = metadata._image_config(config) if rows else {}
    items = []
    seen = set()
    for rank, row in enumerate(rows[:MAX_ITEMS], 1):
        if not isinstance(row, dict):
            raise metadata.UpstreamFailure()
        row_kind = row.get("media_type", kind if kind != "all" else None)
        if row_kind == "person" and kind == "all":
            continue
        if row_kind not in ("movie", "tv") or (kind != "all" and row_kind != kind):
            raise metadata.UpstreamFailure()
        adapted = dict(row)
        if row_kind == "tv":
            adapted.update(title=row.get("name"), release_date=row.get("first_air_date"))
        title = metadata._movie(adapted, images)
        source_id = f"tmdb:{'show' if row_kind == 'tv' else 'movie'}:{title['id']}"
        if source_id in seen:
            continue
        seen.add(source_id)
        items.append({"source_id": source_id, "id": title["id"], "rank": rank,
                      "media_type": "series" if row_kind == "tv" else "movie",
                      **{key: title[key] for key in ("title", "year", "overview", "poster_url", "backdrop_url")}})
    return items


def _fetch(key, config, media_type, job):
    try:
        items = _load(config, media_type, job.deadline)
        stored = time.monotonic()
        if stored >= job.deadline or metadata.configuration().revision != config.revision:
            raise metadata.UpstreamFailure()
        fetched = _now()
        with _lock:
            _cache[key] = {"items": items, "time": stored, "fetched_at": fetched.isoformat(),
                           "expires_at": (fetched + timedelta(seconds=FRESH_SECONDS)).isoformat(),
                           "stale_until": (fetched + timedelta(seconds=STALE_SECONDS)).isoformat(),
                           "retry_at": 0, "failure": None}
        job.status = "live"
    except Exception as error:
        job.status = error.status if isinstance(error, metadata.UpstreamFailure) else "unavailable"
        with _lock:
            previous = _cache.get(key, {})
            _cache[key] = {**previous, "retry_at": time.monotonic() + RETRY_SECONDS, "failure": job.status}
    finally:
        with _lock:
            _jobs.pop(key, None)
            job.done.set()


def trending(media_type="all"):
    """Return a dated weekly feed; wait at most CALL_SECONDS for shared work.

    A timed-out transport can finish in the background. The two-job limit also
    counts those jobs, and a late response cannot become fresh cached data.
    """
    if media_type not in ("all", "movie", "series"):
        raise ValueError("Choose All media, Movies or Series.")
    deadline = time.monotonic() + CALL_SECONDS
    config = metadata.configuration()
    key = ("tmdb", config.revision, config.locale, "trending", "week", media_type, 1)
    attempted = _now().isoformat()
    result = {"source": "tmdb", "period": "week", "scope": "global", "media_type": media_type,
              "revision": config.revision, "locale": config.locale, "configured": bool(config.token),
              "status": "unconfigured", "service_status": None, "items": [], "fetched_at": None,
              "expires_at": None, "stale_until": None, "last_success": None, "attempted_at": attempted}
    if not config.token:
        return result
    creator = False
    with _lock:
        previous = _cache.get(key, {})
        fresh = "time" in previous and time.monotonic() < previous["time"] + FRESH_SECONDS
        job = _jobs.get(key)
        if not fresh and time.monotonic() >= previous.get("retry_at", 0) and job is None and len(_jobs) < MAX_JOBS:
            job = _Job(deadline)
            _jobs[key] = job
            creator = True
        elif not fresh and job is None and len(_jobs) >= MAX_JOBS:
            result["retry_after_ms"] = 1000
    if creator:
        worker = threading.Thread(target=_fetch, args=(key, config, media_type, job), daemon=True,
                                  name="discover-trending")
        try:
            worker.start()
        except RuntimeError:
            with _lock:
                _jobs.pop(key, None)
                job.done.set()
    if not fresh and job:
        job.done.wait(max(0, deadline - time.monotonic()))
    current = metadata.configuration()
    if current.revision != config.revision:
        return {**result, "revision": current.revision, "locale": current.locale,
                "configured": bool(current.token), "status": "unavailable" if current.token else "unconfigured"}
    with _lock:
        entry = deepcopy(_cache.get(key, {}))
    status = "cached" if fresh else (job.status if job and job.done.is_set() else entry.get("failure") or "unavailable")
    usable = "time" in entry and time.monotonic() < entry["time"] + STALE_SECONDS
    result.update(status=status, last_success=entry.get("fetched_at"))
    if usable and status != "authentication_failed":
        result.update({key: entry[key] for key in ("items", "fetched_at", "expires_at", "stale_until")})
        result["status"] = "live" if status == "live" and creator else "cached"
        result["service_status"] = "unavailable" if status == "unavailable" else None
        if not result["items"]:
            result["status"] = "empty"
    elif status == "live":
        result["status"] = "unavailable"
    return result


@dataclass
class _DigitalJob(_Job):
    payload: dict | None = None
    sealed: bool = False
    admission_failed: bool = False


def _digital_progress(job, items, coverage):
    """Retain only work completed within the original operation deadline."""
    with _lock:
        if job.sealed or time.monotonic() >= job.deadline:
            raise metadata.UpstreamFailure()
        previous = job.payload or {}
        fetched = _now()
        job.payload = {
            "items": sorted(deepcopy(items), key=lambda item: (-date.fromisoformat(item["release_date"]).toordinal(), item["id"])),
            "coverage": deepcopy(coverage),
            "time": previous.get("time", time.monotonic()),
            "fetched_at": previous.get("fetched_at", fetched.isoformat()),
            "expires_at": previous.get("expires_at", (fetched + timedelta(seconds=FRESH_SECONDS)).isoformat()),
            "stale_until": previous.get("stale_until", (fetched + timedelta(seconds=STALE_SECONDS)).isoformat()),
        }


def _regional_release(raw, identity, region, start, end):
    if raw.get("id") != identity or type(raw.get("id")) is not int or not isinstance(raw.get("results"), list):
        raise metadata.UpstreamFailure()
    found = False
    qualifying = []
    for country in raw["results"]:
        if not isinstance(country, dict):
            raise metadata.UpstreamFailure()
        if country.get("iso_3166_1") != region:
            continue
        found = True
        records = country.get("release_dates")
        if not isinstance(records, list):
            raise metadata.UpstreamFailure()
        for record in records:
            if not isinstance(record, dict) or type(record.get("type")) is not int or record["type"] not in range(1, 7):
                raise metadata.UpstreamFailure()
            if record["type"] != 4:
                continue
            value = record.get("release_date")
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
                    raise ValueError()
                datetime.fromisoformat(value)
                day = date.fromisoformat(value[:10])
            except ValueError:
                raise metadata.UpstreamFailure() from None
            # Source release dates are calendar labels. Never shift their day
            # through a browser or server timezone, including offset records.
            if start <= day <= end:
                qualifying.append((day.isoformat(), value))
    return (max(qualifying) if qualifying else None), found


def _load_digital(config, region, start, end, job):
    raw = metadata._request(config, "/discover/movie", {
        "language": config.locale, "region": region, "with_release_type": 4,
        "release_date.gte": start.isoformat(), "release_date.lte": end.isoformat(),
        "sort_by": "popularity.desc", "page": 1,
    })
    rows = raw.get("results")
    if not isinstance(rows, list):
        raise metadata.UpstreamFailure()
    coverage = {"candidate_limit": MAX_ITEMS, "candidates": 0, "checked": 0,
                "missing_region": 0, "failed": 0,
                "truncated": len(rows) > MAX_ITEMS or type(raw.get("total_pages")) is not int or raw["total_pages"] not in (0, 1),
                "complete": False}
    candidates = {}
    rejected = set()
    unidentified = 0
    for row in rows[:MAX_ITEMS]:
        identity = row.get("id") if isinstance(row, dict) else None
        if type(identity) is not int or not 0 < identity < 2**53:
            unidentified += 1
            continue
        try:
            metadata._movie(row, {})
        except metadata.UpstreamFailure:
            rejected.add(identity)
            continue
        candidates.setdefault(identity, row)
    # A usable duplicate recovers the same identity in either order. Rows
    # without an identity cannot be deduplicated and each remain a failed unit.
    coverage["candidates"] = len(candidates.keys() | rejected) + unidentified
    coverage["failed"] = len(rejected - candidates.keys()) + unidentified
    job.admission_failed = bool(rows) and not candidates
    items = []
    _digital_progress(job, items, coverage)
    for identity, row in candidates.items():
        if time.monotonic() >= job.deadline:
            raise metadata.UpstreamFailure()
        try:
            regional = metadata._request(config, f"/movie/{identity}/release_dates")
            qualified, found = _regional_release(regional, identity, region, start, end)
            if time.monotonic() >= job.deadline:
                raise metadata.UpstreamFailure()
            coverage["checked"] += 1
            if not found:
                coverage["missing_region"] += 1
            if qualified:
                day, original = qualified
                title = metadata._movie(row, {})
                items.append({**{key: title[key] for key in ("source_id", "id", "media_type", "title", "year", "overview", "poster_url", "backdrop_url")},
                              "region": region, "release_type": "digital", "release_date": day,
                              "provenance": {"source": "tmdb", "path": f"/movie/{identity}/release_dates",
                                             "region": region, "type": 4, "release_date": original}})
        except metadata.UpstreamFailure as error:
            if error.status == "authentication_failed":
                raise
            coverage["failed"] += 1
        _digital_progress(job, items, coverage)
    coverage["complete"] = not (coverage["truncated"] or coverage["missing_region"] or coverage["failed"])
    _digital_progress(job, items, coverage)
    # Artwork is optional and comes after provenance so it cannot consume the
    # release-check budget or discard already verified films.
    if items and time.monotonic() < job.deadline:
        images = metadata._image_config(config)
        for item in items:
            row = candidates[item["id"]]
            item["poster_url"] = metadata._image(images, row.get("poster_path"), "poster")
            item["backdrop_url"] = metadata._image(images, row.get("backdrop_path"), "backdrop")
        _digital_progress(job, items, coverage)


def _seal_digital(key, job, status):
    """Seal once, including at caller timeout; late transports cannot publish."""
    if job.sealed:
        return
    job.sealed = True
    previous = _cache.get(key, {})
    payload = deepcopy(job.payload)
    if payload is not None and status != "authentication_failed" and not job.admission_failed:
        coverage = payload["coverage"]
        coverage["failed"] = max(coverage["failed"], coverage["candidates"] - coverage["checked"])
        coverage["complete"] = not (coverage["failed"] or coverage["missing_region"] or coverage["truncated"])
        _cache[key] = {**payload, "failure": None,
                       "service_status": "unavailable" if status == "unavailable" or coverage["failed"] else None,
                       "retry_at": time.monotonic() + RETRY_SECONDS if coverage["failed"] else 0}
        job.status = "live"
    else:
        status = "unavailable" if status == "live" else status
        # Failure coverage belongs to this attempt, not the retained payload.
        # Never promote failed admission timestamps into a successful cache age.
        failure_coverage = payload["coverage"] if payload is not None and job.admission_failed and status != "authentication_failed" else None
        _cache[key] = {**previous, "failure": status, "failure_coverage": failure_coverage,
                       "retry_at": time.monotonic() + RETRY_SECONDS}
        job.status = status


def _fetch_digital(key, config, region, start, end, job):
    status = "live"
    try:
        _load_digital(config, region, start, end, job)
    except Exception as error:
        status = error.status if isinstance(error, metadata.UpstreamFailure) else "unavailable"
    finally:
        current = metadata.configuration()
        with _lock:
            if current.revision != config.revision:
                job.payload = None
                status = "unavailable"
            _seal_digital(key, job, status)
            _jobs.pop(key, None)
            job.done.set()


def digital_releases(region="US", today=None):
    """Verified digital films from UTC today minus 29 through today, inclusive.

    Admission is the first 20 rows of one regional discovery page. Each unique
    identity needs a selected-country type 4 record. Primary movie dates are
    presentation years only. Partial coverage and original cache age survive
    failures. The caller and all hydration share one 12-second budget.
    """
    from babelfish import Country
    try:
        if not isinstance(region, str) or not re.fullmatch(r"[A-Z]{2}", region):
            raise ValueError()
        Country(region)
    except ValueError:
        raise ValueError("Choose a valid two-letter film region.") from None
    end = today if today is not None else _now().date()
    if type(end) is not date:
        raise ValueError("The release window requires a date.")
    start = end - timedelta(days=29)
    deadline = time.monotonic() + CALL_SECONDS
    config = metadata.configuration()
    key = ("tmdb", config.revision, config.locale, "digital", region, start.isoformat(), end.isoformat(), 4, 1)
    result = {"source": "tmdb", "region": region, "release_type": "digital",
              "window": {"start": start.isoformat(), "end": end.isoformat()},
              "revision": config.revision, "locale": config.locale, "configured": bool(config.token),
              "status": "unconfigured", "service_status": None, "items": [],
              "coverage": {"candidate_limit": MAX_ITEMS, "candidates": 0, "checked": 0,
                           "missing_region": 0, "failed": 0, "truncated": False, "complete": False},
              "fetched_at": None, "expires_at": None, "stale_until": None, "last_success": None,
              "attempted_at": _now().isoformat()}
    if not config.token:
        return result
    creator = False
    with _lock:
        previous = _cache.get(key, {})
        fresh = ("time" in previous and not previous.get("failure")
                 and time.monotonic() < previous["time"] + (RETRY_SECONDS if previous.get("service_status") else FRESH_SECONDS))
        job = _jobs.get(key)
        if not fresh and time.monotonic() >= previous.get("retry_at", 0) and job is None and len(_jobs) < MAX_JOBS:
            job = _DigitalJob(deadline)
            _jobs[key] = job
            creator = True
        elif not fresh and job is None and len(_jobs) >= MAX_JOBS:
            result["retry_after_ms"] = 1000
    if creator:
        try:
            threading.Thread(target=_fetch_digital, args=(key, config, region, start, end, job),
                             daemon=True, name="discover-digital").start()
        except RuntimeError:
            with _lock:
                _seal_digital(key, job, "unavailable")
                _jobs.pop(key, None)
                job.done.set()
    if not fresh and job:
        job.done.wait(max(0, deadline - time.monotonic()))
    current = metadata.configuration()
    if current.revision != config.revision:
        return {**result, "revision": current.revision, "locale": current.locale,
                "configured": bool(current.token), "status": "unavailable" if current.token else "unconfigured"}
    with _lock:
        if job and not job.done.is_set() and time.monotonic() >= job.deadline:
            _seal_digital(key, job, "unavailable")
        entry = deepcopy(_cache.get(key, {}))
    status = entry.get("failure") or ("live" if creator and job and job.status == "live" else "cached")
    usable = "time" in entry and time.monotonic() < entry["time"] + STALE_SECONDS
    result.update(status=status if status != "cached" else "unavailable", last_success=entry.get("fetched_at"))
    if usable and status != "authentication_failed":
        result.update({name: entry[name] for name in ("items", "coverage", "fetched_at", "expires_at", "stale_until")})
        result["status"] = ("live" if status == "live" else "cached") if entry["items"] else "empty"
        result["service_status"] = "unavailable" if status == "unavailable" else entry.get("service_status")
    elif status != "authentication_failed" and entry.get("failure_coverage") is not None:
        result.update(coverage=entry["failure_coverage"], service_status="unavailable")
    return result


RECENT_SHOWS = 6
RECENT_SEASONS = 3
RECENT_RECORDS = 1000
RECENT_OUTPUT = 120


@dataclass
class _RecentJob(_Job):
    payload: dict | None = None
    sealed: bool = False
    source_time: datetime | None = None


def _recent_coverage():
    return {"show_limit": RECENT_SHOWS, "shows": 0, "shows_checked": 0,
            "season_limit": RECENT_SEASONS, "seasons": 0, "seasons_checked": 0,
            "episode_limit": RECENT_RECORDS, "output_limit": RECENT_OUTPUT,
            "episodes_checked": 0, "missing_dates": 0, "failed": 0,
            "truncated": False, "complete": False}


def _recent_deadline(job):
    if job.sealed or time.monotonic() >= job.deadline:
        raise metadata.UpstreamFailure()


def _recent_record(config, path, job, coverage, admit):
    """Share the bounded metadata LRU, without changing retrieval validation.

    Feed-only raw records tolerate malformed siblings. They must never populate
    the stricter show/season/episode identity caches used for provider admission.

    `admit` runs before the body reaches the shared region. A body that cannot
    carry the records this feed asks for is not a cheaper answer to keep: caching
    it would make Refresh reproduce the same partial coverage for the whole
    freshness window without even reaching the source again.
    """
    _recent_deadline(job)
    def load():
        _recent_deadline(job)
        raw = metadata._request(config, path, {"language": config.locale, **({"page": 1} if "trending" in path else {})})
        _recent_deadline(job)
        admit(raw)
        return raw
    raw, status, fetched, availability, _valid_until = metadata._cached(config, ("recent-source", path), load)
    _recent_deadline(job)
    fetched = datetime.fromisoformat(fetched)
    job.source_time = min(job.source_time or fetched, fetched)
    if availability.get("service_status"):
        coverage["failed"] += 1
    return raw


def _recent_progress(job, items, coverage):
    with _lock:
        _recent_deadline(job)
        fetched = job.source_time or _now()
        # Composition does not make an older source observation newer.
        age = max(0, (_now() - fetched).total_seconds())
        job.payload = {"items": sorted(deepcopy(items), key=lambda item: (-date.fromisoformat(item["air_date"]).toordinal(), item["source_id"])),
                       "coverage": deepcopy(coverage), "time": time.monotonic() - age,
                       "fetched_at": fetched.isoformat(),
                       "expires_at": (fetched + timedelta(seconds=FRESH_SECONDS)).isoformat(),
                       "stale_until": (fetched + timedelta(seconds=STALE_SECONDS)).isoformat()}


def _recent_trending_shape(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise metadata.UpstreamFailure()


def _recent_show_shape(raw, identity):
    if (not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] != identity
            or not isinstance(raw.get("seasons"), list)):
        raise metadata.UpstreamFailure()


def _recent_season_shape(raw, season, season_id):
    if (not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] != season_id
            or type(raw.get("season_number")) is not int or raw["season_number"] != season
            or not isinstance(raw.get("episodes"), list)):
        raise metadata.UpstreamFailure()


def _recent_shows(config, job, coverage):
    # Reuse the actual weekly series observation when the homepage has it.
    key = ("tmdb", config.revision, config.locale, "trending", "week", "series", 1)
    with _lock:
        entry = deepcopy(_cache.get(key, {}))
    if ("time" in entry and not entry.get("failure")
            and time.monotonic() < entry["time"] + FRESH_SECONDS):
        job.source_time = datetime.fromisoformat(entry["fetched_at"])
        coverage["truncated"] = True  # That DTO does not establish pagination.
        # The homepage feed already resolved these posters, so there is no
        # artwork left to hydrate on this path.
        return entry["items"], {}
    raw = _recent_record(config, "/trending/tv/week", job, coverage, _recent_trending_shape)
    # After the cache as well as before it, matching the show and season paths.
    # The key has one producer today, which is not an invariant worth resting on.
    _recent_trending_shape(raw)
    rows = raw["results"]
    coverage["truncated"] = (len(rows) > MAX_ITEMS or type(raw.get("total_pages")) is not int
                             or raw["total_pages"] not in (0, 1))
    shows = {}
    artwork = {}
    rejected = set()
    unidentified = 0
    for row in rows[:MAX_ITEMS]:
        identity = row.get("id") if isinstance(row, dict) else None
        if not metadata._positive_id(identity):
            unidentified += 1
            continue
        try:
            if row.get("media_type", "tv") != "tv":
                raise metadata.UpstreamFailure()
            item = metadata._show(row, {})
            if shows.setdefault(identity, item) is item:
                artwork[identity] = row.get("poster_path")
        except metadata.UpstreamFailure:
            rejected.add(identity)
    coverage["failed"] += unidentified + len(rejected - shows.keys())
    if rows and not shows:
        raise metadata.UpstreamFailure()
    return list(shows.values()), artwork


def _recent_artwork(config, job, shows, artwork):
    """Fill in show posters last, from the shared image configuration.

    Artwork is optional, so it comes after the records are verified and only
    with budget left: it can neither consume the record checks nor discard shows
    that already qualified.

    This function only ever fills a poster in. It never clears one, because it
    is not the only producer: the warm path returns shows the homepage feed
    already resolved and hands over an empty path map, and an unconditional
    assignment there replaced every resolved URL with None. A hydrator that can
    destroy what another producer resolved is a hydrator that has to be called
    in exactly the right order forever, which is not a property worth relying on.
    """
    paths = {identity: path for identity, path in artwork.items() if path}
    if not shows or not paths or time.monotonic() >= job.deadline:
        return
    # The shared convention: one configuration request at most, and none at all
    # when no row actually carries a poster to build.
    images = metadata._artwork(config, {"poster_path": True})
    for identity, path in paths.items():
        item = shows.get(identity)
        resolved = metadata._image(images, path, "poster")
        if item is not None and resolved:
            item["poster_url"] = resolved


def _recent_seasons(raw, identity, coverage):
    _recent_show_shape(raw, identity)
    rows = raw["seasons"]
    coverage["truncated"] |= len(rows) > RECENT_RECORDS
    seasons = {}
    for row in rows[:RECENT_RECORDS]:
        if (not isinstance(row, dict) or not metadata._positive_id(row.get("id"))
                or not metadata._number(row.get("season_number"))):
            coverage["failed"] += 1
            continue
        number = row["season_number"]
        if number in seasons and seasons[number] != row["id"]:
            coverage["failed"] += 1
        seasons.setdefault(number, row["id"])
    numbers = sorted((number for number in seasons if number != 0), reverse=True)
    chosen = numbers[:RECENT_SEASONS - (0 in seasons)] + ([0] if 0 in seasons else [])
    coverage["truncated"] |= len(chosen) < len(seasons)
    return [(number, seasons[number]) for number in chosen]


def _recent_episodes(raw, show, season, season_id, start, end, coverage):
    _recent_season_shape(raw, season, season_id)
    rows = raw["episodes"]
    coverage["truncated"] |= len(rows) > RECENT_RECORDS
    episodes = {}
    numbers = {}
    conflicts = set()
    for row in rows[:RECENT_RECORDS]:
        coverage["episodes_checked"] += 1
        try:
            item = metadata._source_episode(row, str(show["id"]), str(season), season_id)
        except metadata.UpstreamFailure:
            coverage["failed"] += 1
            continue
        identity = item["id"]
        if identity in episodes and episodes[identity] != item:
            conflicts.add(identity)
        episodes.setdefault(identity, item)
        numbers.setdefault(item["episode"], set()).add(identity)
    for identities in numbers.values():
        if len(identities) > 1:
            conflicts.update(identities)
    if conflicts:
        coverage["failed"] += len(conflicts)
    result = []
    for identity, item in episodes.items():
        if item["air_date"] is None:
            coverage["missing_dates"] += 1
            continue
        if not start.isoformat() <= item["air_date"] <= end.isoformat():
            continue
        result.append({**item, "show_title": show["title"], "show_year": show["year"],
                       "poster_url": show["poster_url"],
                       "identity_status": "conflict" if identity in conflicts else "unverified",
                       "provenance": {"source": "tmdb", "path": f"/tv/{show['id']}/season/{season}",
                                      "air_date": item["air_date"]}})
    return result


def _load_recent(config, start, end, job):
    coverage = _recent_coverage()
    shows, artwork = _recent_shows(config, job, coverage)
    coverage["shows"] = min(len(shows), RECENT_SHOWS)
    coverage["truncated"] |= len(shows) > RECENT_SHOWS
    items = {}
    _recent_progress(job, [], coverage)
    for show in shows[:RECENT_SHOWS]:
        _recent_deadline(job)
        try:
            raw = _recent_record(config, f"/tv/{show['id']}", job, coverage,
                                 lambda body: _recent_show_shape(body, show["id"]))
            seasons = _recent_seasons(raw, show["id"], coverage)
            coverage["shows_checked"] += 1
        except metadata.UpstreamFailure as error:
            if error.status == "authentication_failed":
                raise
            coverage["failed"] += 1
            _recent_progress(job, list(items.values()), coverage)
            continue
        coverage["seasons"] += len(seasons)
        for number, identity in seasons:
            _recent_deadline(job)
            try:
                raw = _recent_record(config, f"/tv/{show['id']}/season/{number}", job, coverage,
                                     lambda body: _recent_season_shape(body, number, identity))
                records = _recent_episodes(raw, show, number, identity, start, end, coverage)
                coverage["seasons_checked"] += 1
                for item in records:
                    old = items.get(item["source_id"])
                    if old and old != item:
                        old["identity_status"] = "conflict"
                        coverage["failed"] += 1
                    elif not old and len(items) < RECENT_OUTPUT:
                        items[item["source_id"]] = item
                    elif not old:
                        coverage["truncated"] = True
            except metadata.UpstreamFailure as error:
                if error.status == "authentication_failed":
                    raise
                coverage["failed"] += 1
            _recent_progress(job, list(items.values()), coverage)
    coverage["complete"] = not (coverage["truncated"] or coverage["failed"] or coverage["missing_dates"])
    # Artwork is optional, so it spends what is left of the budget and never any
    # of what the records need. Running it before the loop meant a slow
    # configuration request could exhaust the deadline with zero seasons
    # checked, which seals the whole feed as failed rather than partial.
    _recent_artwork(config, job, {show["id"]: show for show in shows[:RECENT_SHOWS]}, artwork)
    # Carry whatever the show ended up with onto its episodes, whichever producer
    # resolved it, and never overwrite an item's own poster with nothing.
    by_show = {show["id"]: show for show in shows}
    for item in items.values():
        poster = (by_show.get(item["show_id"]) or {}).get("poster_url")
        if poster:
            item["poster_url"] = poster
    _recent_progress(job, list(items.values()), coverage)


def _seal_recent(key, job, status):
    if job.sealed:
        return
    job.sealed = True
    previous = _cache.get(key, {})
    payload = deepcopy(job.payload)
    # A wholly failed hydration is not a newer successful empty observation.
    usable_progress = payload is not None and (payload["coverage"]["seasons_checked"]
                                               or (status == "live" and not payload["coverage"]["failed"]))
    if usable_progress and status != "authentication_failed":
        coverage = payload["coverage"]
        if status != "live":
            coverage["failed"] += 1
            coverage["complete"] = False
        _cache[key] = {**payload, "failure": None,
                       "service_status": "unavailable" if coverage["failed"] else None,
                       "retry_at": time.monotonic() + RETRY_SECONDS if coverage["failed"] else 0}
        job.status = "live"
    else:
        failure_coverage = deepcopy(payload["coverage"]) if payload else None
        if failure_coverage is not None:
            failure_coverage["failed"] = max(1, failure_coverage["failed"])
            failure_coverage["complete"] = False
        _cache[key] = {**previous, "failure": status if status != "live" else "unavailable",
                       "failure_coverage": failure_coverage,
                       "retry_at": time.monotonic() + RETRY_SECONDS}
        job.status = _cache[key]["failure"]


def _fetch_recent(key, config, start, end, job):
    status = "live"
    try:
        _load_recent(config, start, end, job)
    except Exception as error:
        status = error.status if isinstance(error, metadata.UpstreamFailure) else "unavailable"
    finally:
        current = metadata.configuration()
        with _lock:
            if current.revision != config.revision:
                job.payload = None
                status = "unavailable"
            _seal_recent(key, job, status)
            _jobs.pop(key, None)
            job.done.set()


def recent_episodes(today=None):
    """Recent original air dates from a bounded weekly trending-show sample.

    At most 20 source shows are scanned, six shows hydrated and three seasons
    per show scanned (1000 rows each). The newest regular seasons and specials
    are included. Hydration is serial within one of two shared active feed jobs;
    all work and caller waiting share 12 seconds. No episode identity/provider
    reconciliation or database work occurs until explicit detail selection.
    """
    end = today if today is not None else _now().date()
    if type(end) is not date:
        raise ValueError("The recent episode window requires a date.")
    start = end - timedelta(days=29)
    config = metadata.configuration()
    deadline = time.monotonic() + CALL_SECONDS
    key = ("tmdb", config.revision, config.locale, "recent-episodes", "week", start.isoformat(), end.isoformat())
    result = {"source": "tmdb", "period": "week", "scope": "trending_shows",
              "window": {"start": start.isoformat(), "end": end.isoformat()},
              "revision": config.revision, "locale": config.locale, "configured": bool(config.token),
              "status": "unconfigured", "service_status": None, "items": [], "coverage": _recent_coverage(),
              "fetched_at": None, "expires_at": None, "stale_until": None, "last_success": None,
              "attempted_at": _now().isoformat()}
    if not config.token:
        return result
    creator = False
    with _lock:
        previous = _cache.get(key, {})
        fresh = ("time" in previous and not previous.get("failure")
                 and time.monotonic() < previous["time"] + (RETRY_SECONDS if previous.get("service_status") else FRESH_SECONDS))
        job = _jobs.get(key)
        if not fresh and time.monotonic() >= previous.get("retry_at", 0) and job is None and len(_jobs) < MAX_JOBS:
            job = _RecentJob(deadline)
            _jobs[key] = job
            creator = True
        elif not fresh and job is None and len(_jobs) >= MAX_JOBS:
            result["retry_after_ms"] = 1000
    if creator:
        try:
            threading.Thread(target=_fetch_recent, args=(key, config, start, end, job),
                             daemon=True, name="discover-recent").start()
        except RuntimeError:
            with _lock:
                _seal_recent(key, job, "unavailable")
                _jobs.pop(key, None)
                job.done.set()
    if not fresh and job:
        job.done.wait(max(0, deadline - time.monotonic()))
    current = metadata.configuration()
    if current.revision != config.revision:
        return {**result, "revision": current.revision, "locale": current.locale,
                "configured": bool(current.token), "status": "unavailable" if current.token else "unconfigured"}
    with _lock:
        if job and not job.done.is_set() and time.monotonic() >= job.deadline:
            _seal_recent(key, job, "unavailable")
        entry = deepcopy(_cache.get(key, {}))
    status = entry.get("failure") or ("live" if creator and job and job.status == "live" else "cached")
    result.update(status=status if status != "cached" else "unavailable", last_success=entry.get("fetched_at"))
    if ("time" in entry and time.monotonic() < entry["time"] + STALE_SECONDS
            and status != "authentication_failed"):
        result.update({name: entry[name] for name in ("items", "coverage", "fetched_at", "expires_at", "stale_until")})
        result["status"] = ("live" if status == "live" else "cached") if entry["items"] else "empty"
        result["service_status"] = "unavailable" if status == "unavailable" else entry.get("service_status")
    elif status != "authentication_failed" and entry.get("failure_coverage") is not None:
        result.update(coverage=entry["failure_coverage"], service_status="unavailable")
    return result
