"""Bounded, server-authenticated movie metadata, independent of subtitle retrieval."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import threading
import time
import unicodedata
import uuid

import requests
from dogpile.cache import make_region
from dogpile.cache.api import NO_VALUE

from utilities.locked_lru import LockedLRU

SOURCE = "tmdb"
API_ROOT = "https://api.themoviedb.org/3"
MAX_RESPONSE_BYTES = 1024 * 1024
FRESH_SECONDS = 300
STALE_SECONDS = 3600
CONFIG_LOCK = threading.RLock()
_cache = make_region().configure("dogpile.cache.memory", expiration_time=STALE_SECONDS,
                                arguments={"cache_dict": LockedLRU(maxsize=256)})
_fetch_locks = [threading.Lock() for _ in range(32)]
_current = None
_cooldowns = LockedLRU(maxsize=32)


@dataclass(frozen=True)
class Configuration:
    token: str = field(repr=False)
    locale: str
    revision: str


def configuration():
    """Snapshot the effective configuration; revision never derives from credential bytes.

    Discover authenticates with the application's own built-in TMDB v3 key, so
    it works with no reader configuration at all. A reader who stores their own
    key overrides it; a stored value that cannot be used as a v3 key, which is
    what a token from the v4 era is, is ignored in favour of the built-in one
    rather than allowed to fail every request.

    The revision stays a random identifier. It is regenerated only when the
    effective credential or locale actually changes within this process, so a
    built-in default does not make every process believe the configuration
    changed, and no part of the key ever reaches the revision.
    """
    from app.config import settings
    from app.tmdb import api_key
    global _current
    with CONFIG_LOCK:
        stored = settings.get("discover.tmdb_access_token", "")
        stored = stored if isinstance(stored, str) and not stored.startswith("enc:") else ""
        token = api_key(stored)
        locale = settings.get("discover.locale", "en-US")
        if not isinstance(locale, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", locale):
            locale = "en-US"
        if _current is None or (_current.token, _current.locale) != (token, locale):
            _current = Configuration(token, locale, uuid.uuid4().hex)
            _cache.invalidate(hard=True)
        return _current


def reader_token_stored():
    """Whether the reader has stored a key of their own, without revealing it.

    Discover works on the built-in key, so "a key is usable" is no longer the
    same fact as "the reader saved one". Only the second one can be offered for
    removal, and only the second one is exposed to the client.
    """
    from app.config import settings
    stored = settings.get("discover.tmdb_access_token", "")
    return bool(isinstance(stored, str) and not stored.startswith("enc:") and stored.strip())


def invalidate_metadata():
    # Called only after successful persistence. The same comparison also handles
    # externally reloaded settings without making a no-op save retire requests.
    configuration()


def normalize_query(query):
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("Enter a movie title of up to 200 characters.")
    decomposed = unicodedata.normalize("NFKD", query).lower()
    # Apostrophes within words carry no search distinction. Other punctuation
    # separates words. Keep Unicode letters/numbers rather than ASCII-only text.
    folded = "".join(char for char in decomposed if not unicodedata.category(char).startswith("M") and char not in "'’ʼ")
    normalized = " ".join("".join(char if char.isalnum() else " " for char in folded).split())
    if not normalized:
        raise ValueError("Enter a movie title.")
    return normalized


def validate_token(value):
    if (not isinstance(value, str) or len(value) > 4096 or value.startswith("enc:")
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError("Invalid TMDB access token.")
    return value


class UpstreamFailure(Exception):
    def __init__(self, status="unavailable"):
        super().__init__(status)
        self.status = status


class DependencyUnavailable(UpstreamFailure):
    """Keep browseable source facts separate from a failed identity dependency."""
    def __init__(self, dependency, payload, known, status="unavailable"):
        super().__init__(status)
        self.dependency = dependency
        self.payload = payload
        self.known = known

    def agrees_with(self, previous):
        return all(isinstance(previous.get(kind), dict)
                   and all(previous[kind].get(key) == value for key, value in fields.items())
                   for kind, fields in self.known.items())


def _now():
    return datetime.now(timezone.utc).isoformat()


def _contains_credential(value, token):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and token in item:
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _request(config, path, params=None):
    if time.monotonic() < _cooldowns.get(config.revision, 0):
        raise UpstreamFailure()
    try:
        # Per-request sessions avoid mutating shared authorization headers across
        # simultaneous saved-token and draft-token checks. Environment proxies
        # remain available through requests' normal transport configuration.
        with requests.Session() as session:
            # v3 authentication: the key is a query parameter, never a header and
            # never part of the cache key or the revision.
            with session.get(API_ROOT + path, params={**(params or {}), "api_key": config.token},
                             headers={"Accept": "application/json"},
                             timeout=(3.05, 8), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    raise UpstreamFailure("authentication_failed")
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "30")
                    try:
                        seconds = int(retry)
                    except ValueError:
                        try:
                            seconds = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 30
                    _cooldowns[config.revision] = time.monotonic() + max(1, min(seconds, 86400))
                    raise UpstreamFailure()
                if response.status_code != 200:
                    raise UpstreamFailure()
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise UpstreamFailure()
                content = bytearray()
                deadline = time.monotonic() + 10
                for chunk in response.iter_content(chunk_size=16384):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise UpstreamFailure()
                # Never serialize a response that echoed the bearer credential.
                if config.token and config.token.encode() in content:
                    raise UpstreamFailure()
                result = json.loads(content)
                if (not isinstance(result, dict)
                        or (config.token and _contains_credential(result, config.token))):
                    raise UpstreamFailure()
                return result
    except UpstreamFailure:
        raise
    except Exception:
        raise UpstreamFailure() from None


_MESSAGES = {
    "unconfigured": "TMDB metadata is unavailable in this build. IMDb subtitle search remains available.",
    "available": "TMDB is available.",
    "authentication_failed": "TMDB rejected the key in use. Check the key in Discover settings, or clear it to use the built-in one.",
    "unavailable": "TMDB is temporarily unavailable. Try again shortly.",
    "cached": "Showing cached TMDB metadata with its original fetch time.",
}


def _envelope(config, status, *, fetched_at=None, checked_at=None, message=None, **payload):
    return {"data": {"source": SOURCE, "status": status, "configured": bool(config.token),
                     "revision": config.revision, "locale": config.locale,
                     "message": message or _MESSAGES[status], "checked_at": checked_at,
                     "fetched_at": fetched_at, **payload}}


def connection_status(candidate=None, *, use_saved=True):
    from app.tmdb import is_v3_key
    config = configuration()
    if not use_saved:
        draft = validate_token(candidate)
        if draft and not is_v3_key(draft):
            # Say so rather than quietly checking the built-in key and reporting
            # that everything is fine: this entry would never be sent.
            return _envelope(config, "authentication_failed",
                             checked_at=_now(),
                             message="This is not a TMDB v3 API key, so it will not be used. "
                                     "A v3 key is 32 characters. Clear the field to use the built-in key.")
        config = Configuration(draft or config.token, config.locale, uuid.uuid4().hex)
    if not config.token:
        return _envelope(config, "unconfigured")
    checked = _now()
    try:
        if _request(config, "/authentication").get("success") is not True:
            raise UpstreamFailure()
        status = "available"
    except UpstreamFailure as error:
        status = error.status
    if use_saved and configuration().revision != config.revision:
        return _envelope(configuration(), "unavailable")
    return _envelope(config, status, checked_at=checked)


def _require_dependency_validity(valid_until):
    if valid_until is not None and time.monotonic() >= valid_until:
        raise UpstreamFailure()


def _cached(config, operation, loader, *, dependency_valid_until=None):
    key = (SOURCE, config.revision, config.locale, operation)
    # A fixed lock stripe set coalesces requests without an unbounded key-lock map.
    with _fetch_locks[hash(key) % len(_fetch_locks)]:
        _require_dependency_validity(dependency_valid_until)
        previous = _cache.get(key)
        now = time.monotonic()
        previous_valid_until = (min(previous["time"] + STALE_SECONDS,
                                    dependency_valid_until if dependency_valid_until is not None else float("inf"))
                                if previous is not NO_VALUE else None)
        if previous is not NO_VALUE and now - previous["time"] < FRESH_SECONDS:
            return deepcopy(previous["payload"]), "cached", previous["fetched_at"], {}, previous_valid_until
        try:
            payload = loader()
        except UpstreamFailure as error:
            if (isinstance(error, DependencyUnavailable) and previous is not NO_VALUE
                    and not error.agrees_with(previous["payload"])):
                # An observed change retires the old identity even if the next
                # attempt fails before it can observe those same source facts.
                _cache.delete(key)
                _require_dependency_validity(dependency_valid_until)
                raise
            _require_dependency_validity(dependency_valid_until)
            if (error.status == "unavailable" and previous is not NO_VALUE
                    and time.monotonic() < previous_valid_until):
                availability = {"service_status": "unavailable"}
                if isinstance(error, DependencyUnavailable):
                    availability["unavailable_dependency"] = error.dependency
                return deepcopy(previous["payload"]), "cached", previous["fetched_at"], availability, previous_valid_until
            raise
        fetched = _now()
        with CONFIG_LOCK:
            if configuration().revision != config.revision:
                raise UpstreamFailure()
            _require_dependency_validity(dependency_valid_until)
            stored_at = time.monotonic()
            _cache.set(key, {"payload": deepcopy(payload), "time": stored_at, "fetched_at": fetched})
        valid_until = min(stored_at + STALE_SECONDS,
                          dependency_valid_until if dependency_valid_until is not None else float("inf"))
        return payload, "available", fetched, {}, valid_until


def _image_config(config):
    key = (SOURCE, config.revision, "images")
    cached = _cache.get(key)
    if cached is not NO_VALUE:
        return cached
    result = _request(config, "/configuration").get("images", {})
    if (not isinstance(result, dict)
            or result.get("secure_base_url") != "https://image.tmdb.org/t/p/"
            or any(not isinstance(result.get(key), list)
                   or not all(isinstance(size, str) for size in result[key])
                   for key in ("poster_sizes", "backdrop_sizes"))):
        return {}
    result = {key: result[key] for key in ("secure_base_url", "poster_sizes", "backdrop_sizes")}
    with CONFIG_LOCK:
        if configuration().revision == config.revision:
            _cache.set(key, result)
    return result


def _image(images, path, kind):
    size = "w342" if kind == "poster" else "w780"
    if (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_-]+\.(jpg|png)", path)
            or size not in images.get(kind + "_sizes", [])):
        return None
    return images["secure_base_url"] + size + path


def _movie(raw, images):
    if not isinstance(raw, dict) or type(raw.get("id")) is not int or not 0 < raw["id"] < 2**53:
        raise UpstreamFailure()
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise UpstreamFailure()
    # A date-shaped value is not a date. 2026-02-31 and 2026-13-01 both satisfy
    # the shape and would project a confident year from a value the source
    # cannot mean, so the calendar check decides it and unknown stays unknown.
    date = _date(raw.get("release_date", ""))
    year = int(date[:4]) if date else None
    imdb = raw.get("imdb_id")
    imdb = imdb if isinstance(imdb, str) and re.fullmatch(r"tt\d{7,10}", imdb) else None
    overview = raw.get("overview")
    return {"source": SOURCE, "source_id": f"tmdb:movie:{raw['id']}", "id": raw["id"],
            "media_type": "movie", "title": title[:500], "year": year, "imdb_id": imdb,
            "mapping_status": "resolved" if imdb else "unresolved",
            "poster_url": _image(images, raw.get("poster_path"), "poster"),
            "backdrop_url": _image(images, raw.get("backdrop_path"), "backdrop"),
            "overview": overview[:5000] if isinstance(overview, str) else ""}


def _browse_with_validity(operation, loader, empty, *, dependency_valid_until=None):
    """Return the envelope and its internal monotonic dependency cutoff."""
    config = configuration()
    if not config.token:
        return _envelope(config, "unconfigured", **empty), None
    try:
        payload, status, fetched, availability, valid_until = _cached(
            config, operation, lambda: loader(config), dependency_valid_until=dependency_valid_until)
        if configuration().revision != config.revision:
            return _envelope(configuration(), "unavailable", **empty), None
        _require_dependency_validity(valid_until)
        return _envelope(config, status, fetched_at=fetched, checked_at=fetched,
                         **{"service_status": None, **availability}, **payload), valid_until
    except UpstreamFailure as error:
        current = configuration()
        if dependency_valid_until is not None and time.monotonic() >= dependency_valid_until:
            return _envelope(current, "unavailable", service_status="unavailable", **empty), None
        if isinstance(error, DependencyUnavailable) and current.revision == config.revision:
            # A fresh unverified source can still anchor explicit manual
            # recovery, but only within its confirmed parent's lifetime.
            return _envelope(current, error.status, service_status="unavailable",
                             unavailable_dependency=error.dependency, **error.payload), dependency_valid_until
        return _envelope(current, error.status if current.revision == config.revision else "unavailable", **empty), None


def _browse(operation, loader, empty):
    return _browse_with_validity(operation, loader, empty)[0]


def search_movies(query):
    query = normalize_query(query)

    def load(config):
        result = _request(config, "/search/movie", {"query": query, "language": config.locale,
                                                   "include_adult": "false", "page": 1})
        rows = result.get("results")
        if not isinstance(rows, list):
            raise UpstreamFailure()
        rows = rows[:12]
        images = {}
        if any(isinstance(row, dict) and row.get("poster_path") for row in rows):
            try:
                images = _image_config(config)
            except UpstreamFailure:
                pass
        return {"items": [_movie(row, images) for row in rows]}

    return _browse(("search", query), load, {"items": []})


def movie_details(movie_id):
    if not isinstance(movie_id, str) or not re.fullmatch(r"[1-9]\d{0,12}", movie_id):
        raise ValueError("Invalid movie identity.")

    def load(config):
        result = _request(config, "/movie/" + movie_id, {"language": config.locale})
        if result.get("id") != int(movie_id):
            raise UpstreamFailure()
        images = {}
        if result.get("poster_path") or result.get("backdrop_path"):
            try:
                images = _image_config(config)
            except UpstreamFailure:
                pass
        return {"item": _movie(result, images)}

    return _browse(("details", movie_id), load, {"item": None})


# TVDB's existing client owns project authentication. Bound admission and caller
# wait here because that client's token lock and socket timeouts have no total
# deadline. Daemon workers can finish late, but cannot publish late cache data.
_TVDB_SLOTS = threading.BoundedSemaphore(4)
TVDB_WAIT_SECONDS = 6


def _positive_id(value):
    return type(value) is int and 0 < value < 2**53


def _number(value, minimum=0):
    return type(value) is int and minimum <= value <= 9999


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _imdb(value):
    return value if isinstance(value, str) and re.fullmatch(r"tt\d{7,10}", value) else None


def _path_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{0,12}", value):
        raise ValueError("Invalid show identity.")
    return value


def _path_number(value, minimum=0):
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9]\d{0,3}", value)
            or not _number(int(value), minimum)):
        raise ValueError("Choose an exact season and episode.")
    return value


def _show(raw, images, external=None):
    if not isinstance(raw, dict):
        raise UpstreamFailure()
    external = external or {}
    result = _movie({**raw, "title": raw.get("name"), "release_date": raw.get("first_air_date"),
                     "imdb_id": external.get("imdb_id")}, images)
    result.update(media_type="show", source_id=f"tmdb:show:{result['id']}",
                  tvdb_id=external.get("tvdb_id") if _positive_id(external.get("tvdb_id")) else None)
    result["title"] = result["title"][:300]
    if "seasons" in raw:
        seasons = raw["seasons"]
        if not isinstance(seasons, list) or len(seasons) > 1000:
            raise UpstreamFailure()
        projected = []
        seen = set()
        for season in seasons:
            if (not isinstance(season, dict) or not _positive_id(season.get("id"))
                    or not _number(season.get("season_number")) or season["season_number"] in seen):
                raise UpstreamFailure()
            seen.add(season["season_number"])
            title = season.get("name")
            projected.append({"id": season["id"], "season": season["season_number"],
                              "title": title[:300] if isinstance(title, str) else f"Season {season['season_number']}",
                              "episode_count": season.get("episode_count") if _number(season.get("episode_count")) else None})
        result["seasons"] = projected
    else:
        result["seasons"] = None
    return result


def _artwork(config, raw):
    if raw.get("poster_path") or raw.get("backdrop_path"):
        try:
            return _image_config(config)
        except UpstreamFailure:
            pass
    return {}


def search_shows(query):
    query = normalize_query(query)

    def load(config):
        result = _request(config, "/search/tv", {"query": query, "language": config.locale,
                                                "include_adult": "false", "page": 1})
        rows = result.get("results")
        if not isinstance(rows, list):
            raise UpstreamFailure()
        rows = rows[:12]
        images = _artwork(config, {"poster_path": any(isinstance(row, dict) and row.get("poster_path") for row in rows)})
        return {"items": [_show(row, images) for row in rows]}

    return _browse(("search", "show", query), load, {"items": []})


def _show_details_with_validity(show_id):
    show_id = _path_id(show_id)

    def load(config):
        raw = _request(config, "/tv/" + show_id, {"language": config.locale})
        if type(raw.get("id")) is not int or raw["id"] != int(show_id):
            raise UpstreamFailure()
        try:
            external = _request(config, "/tv/" + show_id + "/external_ids")
        except UpstreamFailure as error:
            item = _show(raw, _artwork(config, raw))
            known = {key: item[key] for key in ("source_id", "id", "title", "year", "seasons")}
            raise DependencyUnavailable("show_external_ids", {"item": item}, {"item": known}, error.status) from None
        if type(external.get("id")) is not int or external["id"] != int(show_id):
            external = {}
        return {"item": _show(raw, _artwork(config, raw), external)}

    return _browse_with_validity(("details", "show", show_id), load, {"item": None})


def show_details(show_id):
    return _show_details_with_validity(show_id)[0]


def _source_episode(raw, show_id, season, season_id=None):
    if (not isinstance(raw, dict) or not _positive_id(raw.get("id"))
            or type(raw.get("season_number")) is not int or raw["season_number"] != int(season)
            or not _number(raw.get("episode_number"), 1)
            or not isinstance(raw.get("name"), str) or not raw["name"].strip()):
        raise UpstreamFailure()
    return {"source": SOURCE, "source_id": f"tmdb:show:{show_id}:episode:{raw['id']}",
            "show_id": int(show_id), "season_id": season_id, "id": raw["id"],
            "season": raw["season_number"], "episode": raw["episode_number"], "title": raw["name"][:300],
            "air_date": _date(raw.get("air_date"))}


def season_details(show_id, season):
    show_id, season = _path_id(show_id), _path_number(season)

    def load(config):
        raw = _request(config, f"/tv/{show_id}/season/{season}", {"language": config.locale})
        if (not _positive_id(raw.get("id")) or type(raw.get("season_number")) is not int
                or raw["season_number"] != int(season) or not isinstance(raw.get("episodes"), list)
                or len(raw["episodes"]) > 1000):
            raise UpstreamFailure()
        episodes = [_source_episode(item, show_id, season, raw["id"]) for item in raw["episodes"]]
        if (len({item["id"] for item in episodes}) != len(episodes)
                or len({item["episode"] for item in episodes}) != len(episodes)):
            raise UpstreamFailure()
        return {"season": {"id": raw["id"], "season": int(season), "episodes": episodes}}

    return _browse(("season", show_id, season), load, {"season": None})


def _tvdb_episode(episode_id):
    if not _positive_id(episode_id) or not _TVDB_SLOTS.acquire(blocking=False):
        return None
    from queue import Queue, Empty
    result = Queue(maxsize=1)

    def fetch():
        try:
            from subliminal_patch.refiners.tvdb_v4 import get_client
            raw = get_client().get_episode(episode_id)
            # Project only fields needed for reconciliation; never retain a
            # client response, authentication payload or exception in metadata.
            if not isinstance(raw, dict):
                result.put(None)
            else:
                result.put({key: raw.get(key) for key in ("id", "seriesId", "seasonNumber", "number", "lastUpdated", "absoluteNumber")})
        except Exception:
            result.put(None)
        finally:
            _TVDB_SLOTS.release()

    threading.Thread(target=fetch, daemon=True, name="discover-episode-metadata").start()
    try:
        return result.get(timeout=TVDB_WAIT_SECONDS)
    except Empty:
        return None



def _source_updated_at(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _episode_details_with_validity(show_id, season, episode):
    show_id, season, episode = _path_id(show_id), _path_number(season), _path_number(episode, 1)
    # Fetch parents before taking the episode cache stripe. Nested cache locks
    # can map to the same stripe, so loaders never recursively call _browse.
    show_response, parent_valid_until = _show_details_with_validity(show_id)
    show_data = show_response["data"]
    show = show_data.get("item")
    if not show:
        return {"data": {**show_data, "episode": None}}, None

    def load(config):
        if config.revision != show_data["revision"]:
            raise UpstreamFailure()
        path = f"/tv/{show_id}/season/{season}/episode/{episode}"
        raw = _request(config, path, {"language": config.locale})
        season_record = next((item for item in show.get("seasons") or [] if item["season"] == int(season)), None)
        item = _source_episode(raw, show_id, season, season_record["id"] if season_record else None)
        if item["episode"] != int(episode):
            raise UpstreamFailure()
        item.update(imdb_id=None, tvdb_id=None, show_imdb_id=show["imdb_id"], show_tvdb_id=show["tvdb_id"],
                    show_title=show["title"], show_year=show["year"], target_season=None, target_episode=None,
                    numbering=None, identity_status="unverified", absolute_episode=None, tvdb_absolute_number=None, mapping_updated_at=None)
        known = {key: value for key, value in item.items() if key not in (
            "imdb_id", "tvdb_id", "target_season", "target_episode", "numbering", "identity_status",
            "absolute_episode", "tvdb_absolute_number", "mapping_updated_at")}
        try:
            external = _request(config, path + "/external_ids")
        except UpstreamFailure as error:
            raise DependencyUnavailable("episode_external_ids", {"episode": item}, {"episode": known}, error.status) from None
        if type(external.get("id")) is not int or external["id"] != item["id"]:
            item["identity_status"] = "conflict"
            return {"episode": item}
        item.update(imdb_id=_imdb(external.get("imdb_id")),
                    tvdb_id=external.get("tvdb_id") if _positive_id(external.get("tvdb_id")) else None)
        known.update(imdb_id=item["imdb_id"], tvdb_id=item["tvdb_id"])
        if show_data.get("service_status") or show_data["status"] in ("unavailable", "authentication_failed"):
            raise DependencyUnavailable(show_data.get("unavailable_dependency", "show_external_ids"),
                                        {"episode": item}, {"episode": known})
        if not item["show_imdb_id"] or not item["show_tvdb_id"] or not item["tvdb_id"]:
            return {"episode": item}
        target = _tvdb_episode(item["tvdb_id"])
        if not target:
            raise DependencyUnavailable("tvdb_episode", {"episode": item}, {"episode": known})
        if (not _positive_id(target.get("id")) or not _positive_id(target.get("seriesId"))
                or target["id"] != item["tvdb_id"] or target["seriesId"] != item["show_tvdb_id"]):
            item["identity_status"] = "conflict"
        elif _number(target.get("seasonNumber")) and _number(target.get("number"), 1):
            item.update(target_season=target["seasonNumber"], target_episode=target["number"],
                        numbering="tvdb_default", identity_status="resolved",
                        tvdb_absolute_number=target.get("absoluteNumber")
                        if type(target.get("absoluteNumber")) is int and 1 <= target["absoluteNumber"] <= 999999 else None,
                        mapping_updated_at=_source_updated_at(target.get("lastUpdated")))
        return {"episode": item}

    # The parent identity is part of the operation so a changed show mapping
    # cannot inherit an older, still-fresh episode reconciliation.
    parent = (show["imdb_id"], show["tvdb_id"], show["title"], show["year"],
              tuple((row["id"], row["season"]) for row in show.get("seasons") or []))
    # Every lookup rechecks the parent and carries its original cutoff. A newer
    # episode cache timestamp cannot extend that parent's evidence.
    response, valid_until = _browse_with_validity(("episode", show_id, season, episode, parent), load, {"episode": None},
                                                dependency_valid_until=parent_valid_until)
    if show_data.get("service_status"):
        response["data"]["service_status"] = "unavailable"
        if show_data.get("unavailable_dependency"):
            response["data"]["unavailable_dependency"] = show_data["unavailable_dependency"]
    return response, valid_until


def episode_details(show_id, season, episode):
    return _episode_details_with_validity(show_id, season, episode)[0]


# OMDB has its own effective credential and no locale or regional catalog.
_omdb_current = None
_omdb_cache = make_region().configure("dogpile.cache.memory", expiration_time=STALE_SECONDS,
                                     arguments={"cache_dict": LockedLRU(maxsize=256)})
_omdb_slots = threading.BoundedSemaphore(4)
OMDB_WAIT_SECONDS = 12
OMDB_RESULT_LIMIT = 10


def omdb_configuration():
    from subliminal_patch.refiners.omdb import _resolve_omdb_apikey
    global _omdb_current
    token = _resolve_omdb_apikey()
    token = token.strip() if isinstance(token, str) else ""
    with CONFIG_LOCK:
        if _omdb_current is None or _omdb_current.token != token:
            _omdb_current = Configuration(token, "", uuid.uuid4().hex)
            _omdb_cache.invalidate(hard=True)
        return _omdb_current


class OMDBFailure(UpstreamFailure):
    def __init__(self, reason):
        super().__init__()
        self.reason = reason if reason in ("quota", "timeout") else None


def _omdb_deadline(deadline):
    if time.monotonic() >= deadline:
        raise OMDBFailure("timeout")


def _omdb_snapshot(deadline):
    if not CONFIG_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise OMDBFailure("timeout")
    try:
        return omdb_configuration(), configuration()
    finally:
        CONFIG_LOCK.release()


def _omdb_request(config, params, deadline=None):
    """One fixed HTTPS request, with no credential-bearing request logger."""
    import http.client
    from urllib.parse import urlencode

    deadline = time.monotonic() + OMDB_WAIT_SECONDS if deadline is None else deadline
    _omdb_deadline(deadline)
    if time.monotonic() < _cooldowns.get(config.revision, 0):
        raise OMDBFailure("quota")
    if not _omdb_slots.acquire(blocking=False):
        raise UpstreamFailure()
    complete = threading.Event()
    outcome = {}

    def fetch():
        connection = None
        try:
            _omdb_deadline(deadline)
            connection = http.client.HTTPSConnection("www.omdbapi.com", timeout=min(3.05, deadline - time.monotonic()))
            # http.client debug output prints the request target. Explicitly
            # disable it on this private connection, including inherited debug.
            connection.set_debuglevel(0)
            connection.request("GET", "/?" + urlencode({**params, "apikey": config.token}),
                               headers={"Accept": "application/json"})
            if connection.sock:
                connection.sock.settimeout(min(8, max(.01, deadline - time.monotonic())))
            response = connection.getresponse()
            if response.status in (401, 403):
                raise UpstreamFailure("authentication_failed")
            if response.status == 429:
                _cooldowns[config.revision] = time.monotonic() + 30
                raise OMDBFailure("quota")
            if response.status != 200:
                raise UpstreamFailure()
            length = response.getheader("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise UpstreamFailure()
            content = bytearray()
            while True:
                _omdb_deadline(deadline)
                if connection.sock:
                    connection.sock.settimeout(min(8, max(.001, deadline - time.monotonic())))
                chunk = response.read1(16384)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise UpstreamFailure()
            result = json.loads(content)
            if not isinstance(result, dict) or _contains_credential(result, config.token):
                raise UpstreamFailure()
            if result.get("Response") == "False":
                reason = result.get("Error", "")
                if reason in ("Movie not found!", "Series not found!", "Episode not found!"):
                    outcome["result"] = {"Response": "False", "not_found": True}
                    return
                if isinstance(reason, str) and "api key" in reason.lower():
                    raise UpstreamFailure("authentication_failed")
                if reason == "Request limit reached!":
                    _cooldowns[config.revision] = time.monotonic() + 30
                    raise OMDBFailure("quota")
                raise UpstreamFailure()
            if result.get("Response") != "True":
                raise UpstreamFailure()
            outcome["result"] = result
        except UpstreamFailure as error:
            outcome["status"] = error.status
            outcome["reason"] = getattr(error, "reason", None)
        except TimeoutError:
            outcome.update(status="unavailable", reason="timeout")
        except Exception:
            outcome["status"] = "unavailable"
        finally:
            try:
                if connection:
                    connection.close()
            finally:
                _omdb_slots.release()
                complete.set()

    threading.Thread(target=fetch, daemon=True).start()
    if not complete.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        raise OMDBFailure("timeout")
    if "result" not in outcome:
        if outcome.get("reason"):
            raise OMDBFailure(outcome["reason"])
        raise UpstreamFailure(outcome.get("status", "unavailable"))
    return outcome["result"]


def _omdb_title(raw, kind):
    if not isinstance(raw, dict) or raw.get("Type") != ("series" if kind == "show" else "movie"):
        raise UpstreamFailure()
    imdb = _imdb(raw.get("imdbID"))
    title = raw.get("Title")
    if not imdb or not isinstance(title, str) or not title.strip() or title == "N/A":
        raise UpstreamFailure()
    date = raw.get("Year")
    # A series may report a run range. Its first year is a source fact only
    # when the entire value has a documented year/range shape.
    year = int(date[:4]) if isinstance(date, str) and re.fullmatch(r"[1-9]\d{3}(?:[–-](?:[1-9]\d{3})?)?", date) else None
    plot = raw.get("Plot")
    return {"source": "omdb", "source_id": f"omdb:{kind}:{imdb}", "id": imdb, "media_type": kind,
            "title": title[:500], "year": year, "imdb_id": imdb, "mapping_status": "resolved",
            "overview": plot[:5000] if isinstance(plot, str) and plot != "N/A" else "",
            "poster_url": None, "backdrop_url": None, **({"tvdb_id": None, "seasons": None} if kind == "show" else {})}


def _omdb_browse(operation, loader, empty):
    deadline = time.monotonic() + OMDB_WAIT_SECONDS
    config = _omdb_current or Configuration("", "", uuid.uuid4().hex)
    primary = _current or Configuration("", "en-US", uuid.uuid4().hex)
    status, fetched, availability, payload = "unconfigured", None, {}, empty
    previous, key, published = NO_VALUE, None, False
    lock = None
    acquired = False
    try:
        config, primary = _omdb_snapshot(deadline)
        if config.token:
            _omdb_deadline(deadline)
            key = ("omdb", config.revision, operation)
            lock = _fetch_locks[hash(key) % len(_fetch_locks)]
            acquired = lock.acquire(timeout=max(0, deadline - time.monotonic()))
            if not acquired:
                raise OMDBFailure("timeout")
            _omdb_deadline(deadline)
            previous = _omdb_cache.get(key)
            current, primary = _omdb_snapshot(deadline)
            if current.revision != config.revision:
                raise UpstreamFailure()
            _omdb_deadline(deadline)
            if previous is not NO_VALUE and time.monotonic() - previous["time"] < FRESH_SECONDS:
                payload, fetched, status = deepcopy(previous["payload"]), previous["fetched_at"], "cached"
            else:
                payload = loader(config, deadline)
                _omdb_deadline(deadline)
                if not CONFIG_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
                    raise OMDBFailure("timeout")
                try:
                    if omdb_configuration().revision != config.revision:
                        raise UpstreamFailure()
                    _omdb_deadline(deadline)
                    fetched, status = _now(), "available"
                    _omdb_cache.set(key, {"payload": deepcopy(payload), "fetched_at": fetched, "time": time.monotonic()})
                    published = True
                    _omdb_deadline(deadline)
                finally:
                    CONFIG_LOCK.release()
            current, primary = _omdb_snapshot(deadline)
            if current.revision != config.revision:
                raise UpstreamFailure()
            _omdb_deadline(deadline)
    except UpstreamFailure as error:
        if published:
            _omdb_cache.delete(key)
        elif previous is NO_VALUE and key is not None:
            previous = _omdb_cache.get(key)
        payload, fetched, status = empty, None, error.status
        reason = getattr(error, "reason", None)
        if reason:
            availability["failure_reason"] = reason
        try:
            current, primary = _omdb_snapshot(deadline)
            compatible = current.revision == config.revision
        except UpstreamFailure:
            compatible = False
        if (error.status == "unavailable" and compatible and previous is not NO_VALUE
                and time.monotonic() - previous["time"] < STALE_SECONDS):
            payload, fetched, status = deepcopy(previous["payload"]), previous["fetched_at"], "cached"
            availability["service_status"] = "unavailable"
    finally:
        if acquired:
            lock.release()
    message = {"unconfigured": "Configure OMDB or TMDB for global title identities.",
               "available": "OMDB title identities are available.",
               "cached": "Showing cached OMDB metadata with its original fetch time.",
               "authentication_failed": "OMDB rejected the configured API key.",
               "unavailable": "OMDB is temporarily unavailable. Retry metadata."}[status]
    reason = availability.get("failure_reason")
    if reason:
        message = {"quota": "OMDB quota is exhausted. Check your allowance before retrying.",
                   "timeout": "OMDB request timed out. Retry metadata."}[reason]
    current = _omdb_current or config
    return {"data": {"source": "omdb", "status": status, "configured": bool(current.token),
                     "revision": primary.revision, "fallback_revision": current.revision,
                     "locale": "", "checked_at": fetched, "fetched_at": fetched,
                     "message": message, **availability, **payload}}


def omdb_search(query, media_type="movie"):
    query = normalize_query(query)
    if media_type not in ("movie", "show"):
        raise ValueError("Invalid media type.")

    def load(config, deadline):
        params = {"i": query} if _imdb(query) else {"s": query, "page": 1}
        params["type"] = "series" if media_type == "show" else "movie"
        raw = _omdb_request(config, params, deadline)
        if raw.get("not_found"):
            return {"items": [], "truncated": False}
        rows = [raw] if "i" in params else raw.get("Search")
        if not isinstance(rows, list):
            raise UpstreamFailure()
        items = []
        seen = set()
        for row in rows[:OMDB_RESULT_LIMIT]:
            item = _omdb_title(row, media_type)
            if item["source_id"] not in seen:
                items.append(item)
                seen.add(item["source_id"])
        total = raw.get("totalResults")
        return {"items": items, "truncated": len(rows) > OMDB_RESULT_LIMIT or
                (isinstance(total, str) and total.isdecimal() and int(total) > len(rows))}
    return _omdb_browse(("search", media_type, query), load, {"items": [], "truncated": False})


def candidates(query, media_type="movie", local_query=None):
    from discover.library import combined_candidates
    normalize_query(query)
    if media_type not in ("movie", "show"):
        raise ValueError("Invalid media type.")
    primary = (search_shows(query) if media_type == "show" else search_movies(query))["data"]
    selected = primary
    fallback = None
    if primary["status"] not in ("available", "cached") or primary.get("service_status") or not primary.get("items"):
        fallback = omdb_search(query, media_type)["data"]
        if fallback.get("items"):
            selected = fallback
    for item in selected.get("items", []):
        if item["source"] == "tmdb" and item["media_type"] == "movie":
            item["tmdb_id"] = item["id"]
    local = combined_candidates(selected.get("items", []), query if local_query is None else local_query, media_type)
    items = local["items"]
    for item in items:
        item["provenance"] = "cached" if item["source"] != "local" and selected["status"] == "cached" else item["source"]
    return {"data": {**selected, "items": items[:24],
                     "primary": {key: primary.get(key) for key in ("status", "configured", "message", "service_status")},
                     "fallback": {key: fallback.get(key) for key in ("status", "configured", "message", "failure_reason")} if fallback else None,
                     "fallback_revision": omdb_configuration().revision,
                     "local_truncated": local["truncated"], "truncated": len(items) > 24 or selected.get("truncated", False),
                     "local_match_scope": local["match_scope"]}}


def title_details(identity, media_type="movie", source="tmdb"):
    from discover.library import attach_local_copies, local_details
    if media_type not in ("movie", "show") or source not in ("tmdb", "omdb", "local"):
        raise ValueError("Invalid metadata source.")
    if source == "local":
        item = local_details(identity, media_type)
        result = _envelope(configuration(), "available", item=item)
        result["data"].update(source="local", message="Local library title. No file has been selected.", locale="")
        return result
    if source == "omdb":
        if not _imdb(identity):
            raise ValueError("Invalid IMDb identity.")
        def load(config, deadline):
            raw = _omdb_request(config, {"i": identity, "type": "series" if media_type == "show" else "movie"}, deadline)
            if raw.get("not_found"):
                return {"item": None}
            item = _omdb_title(raw, media_type)
            if item["imdb_id"] != identity:
                raise UpstreamFailure()
            return {"item": item}
        result = _omdb_browse(("details", media_type, identity), load, {"item": None})
    else:
        result = show_details(identity) if media_type == "show" else movie_details(identity)
    item = result["data"].get("item")
    if item:
        if source == "tmdb" and media_type == "movie":
            item["tmdb_id"] = item["id"]
        result["data"]["item"] = attach_local_copies([item])[0]
    return result
