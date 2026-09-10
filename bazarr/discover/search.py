"""Explicit title and release-query searches independent of Distribution Hub clients."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import re
import time
import uuid
from threading import Lock

from dogpile.cache.api import NO_VALUE
from babelfish.exceptions import LanguageReverseError
from subzero.language import Language

from app.config import settings
from app import activity
from app import get_providers
from compat import cache, service
from subliminal_patch.extensions import provider_registry
from subliminal_patch.score import ComputeScore, MAX_SCORES

from .handles import mint_result, resolve_result

_IMDB = re.compile(r"tt[0-9]{7,10}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{2,4})?\Z")
_STATE_LOCK = Lock()
_COMPLETE = {"success", "empty"}


@dataclass(frozen=True)
class SearchRequest:
    context: dict
    refresh: bool
    metadata_valid_until: float | None = None
    # The resolved copy carries the mapped path and content hash, so it stays
    # out of repr and never travels with the serialized context.
    copy: dict | None = field(default=None, repr=False)


class SearchAdmissionExpired(ValueError):
    """Authoritative metadata expired before provider work could start."""


def _admit_provider_search(request):
    from . import metadata
    try:
        metadata._require_dependency_validity(request.metadata_valid_until)
    except metadata.UpstreamFailure:
        raise SearchAdmissionExpired(
            "Episode metadata expired before subtitle search started. Reload details and try again.") from None


def _iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _language(code):
    from languages.custom_lang import CustomLanguage

    custom = CustomLanguage.from_value(code) or CustomLanguage.from_value(code, "alpha2")
    if custom is not None:
        return custom.subzero_language()
    try:
        return Language.fromietf(code)
    except ValueError:
        return Language.fromalpha3b(code)


def validate_context(payload) -> SearchRequest:
    if not isinstance(payload, dict):
        raise ValueError("A search context is required.")
    mode = payload.get("mode", "title")
    if mode not in ("title", "release"):
        raise ValueError("Choose an identified title or an advanced release-name search.")
    allowed = ({"mode", "query", "language", "refresh"} if mode == "release" else
               {"mode", "media_type", "imdb_id", "language", "season", "episode", "title", "year", "refresh", "episode_identity", "manual_confirmed", "show_id", "copy_id"})
    if set(payload) - allowed:
        raise ValueError("Unsupported search context. Choose a title and subtitle language.")
    media_type = payload.get("media_type")
    imdb_id = payload.get("imdb_id")
    language = payload.get("language")
    if mode == "title" and media_type not in ("movie", "episode"):
        raise ValueError("Choose a movie or an exact episode.")
    if mode == "title" and (not isinstance(imdb_id, str) or not _IMDB.fullmatch(imdb_id.strip().lower())):
        raise ValueError("Enter a valid IMDb ID, for example tt0133093.")
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        raise ValueError("Choose one subtitle language.")
    try:
        lang = _language(language)
    except (ValueError, KeyError, LanguageReverseError):
        raise ValueError("Choose a supported subtitle language.") from None
    # Keep explicit regional/script variants. No profile or locale defaults.
    canonical = str(lang)
    refresh = payload.get("refresh", False)
    if type(refresh) is not bool:
        raise ValueError("Invalid refresh option.")
    if mode == "release":
        query = payload.get("query")
        if (not isinstance(query, str) or len(query) > 500
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in query)
                or not any(c.isalnum() for c in query)):
            raise ValueError("Enter a release name containing letters or numbers, up to 500 characters.")
        service._build_release_query_video(query.strip())
        return SearchRequest({"mode": "release", "query": query.strip(), "language": canonical,
                              "matching_mode": "release"}, refresh)
    context = {"media_type": media_type, "imdb_id": imdb_id.strip().lower(),
               "language": canonical, "matching_mode": "title"}
    if media_type == "episode":
        for key, minimum in (("season", 0), ("episode", 1)):
            value = payload.get(key)
            if type(value) is not int or not minimum <= value <= 9999:
                raise ValueError("Choose an exact season and episode.")
            context[key] = value
    elif any(payload.get(key) is not None for key in ("season", "episode")):
        raise ValueError("Movie searches cannot contain an episode.")
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or len(title) > 300 or any(ord(c) < 32 for c in title):
            raise ValueError("Invalid title.")
        if title.strip():
            context["title"] = title.strip()
    if payload.get("year") is not None:
        year = payload["year"]
        if type(year) is not int or not 1870 <= year <= 2200:
            raise ValueError("Invalid title year.")
        context["year"] = year
    metadata_valid_until = None
    if media_type == "episode":
        metadata_valid_until = _reconcile_episode(payload, context)
    elif any(key in payload for key in ("episode_identity", "manual_confirmed", "show_id")):
        raise ValueError("Movie searches cannot contain episode identity.")
    return SearchRequest(context, refresh, metadata_valid_until, _reconcile_copy(payload, context))


def _reconcile_copy(payload, context):
    """Resolve an explicitly chosen library copy against the confirmed target.

    The client supplies one opaque identity and nothing else. The path, the
    release facts and the physical revision are all read here, server side,
    from the row that identity actually names within the current instance
    scope. A copy that no longer resolves raises instead of falling back, so a
    stale choice can never be answered with a different file.
    """
    from .library import copy_context, resolve_copy

    chosen = payload.get("copy_id")
    if chosen is None:
        return None
    if not isinstance(chosen, str) or len(chosen) > 64:
        raise ValueError("Choose a library copy from the offered list.")
    facts = resolve_copy(chosen, context)
    context["copy_id"] = facts["copy_id"]
    context["file_revision"] = facts["file_revision"]
    context["copy"] = copy_context(facts)
    return facts



def _reconcile_episode(payload, context):
    from . import metadata

    manual = payload.get("manual_confirmed", False)
    if type(manual) is not bool:
        raise ValueError("Confirm the manual series and episode identity.")
    identity = payload.get("episode_identity")
    show_id = payload.get("show_id")
    valid_until = None
    if show_id is not None and not metadata._positive_id(show_id):
        raise ValueError("Invalid show identity.")
    if identity is not None:
        if (not isinstance(identity, dict) or identity.get("source") != "tmdb"
                or not metadata._positive_id(identity.get("show_id"))
                or not metadata._positive_id(identity.get("id"))
                or not metadata._number(identity.get("season"))
                or not metadata._number(identity.get("episode"), 1)):
            raise ValueError("Choose an exact source episode.")
        if show_id is not None and show_id != identity["show_id"]:
            raise ValueError("Conflicting show identity. Choose the episode again.")
        response, valid_until = metadata._episode_details_with_validity(
            str(identity["show_id"]), str(identity["season"]), str(identity["episode"]))
        canonical = response["data"].get("episode")
        if canonical is None or valid_until is None:
            raise ValueError("Episode metadata is unavailable. Retry details before searching this selection.")
        if canonical != identity:
            raise ValueError("Episode mapping changed. Reload episode details and confirm the selection again.")
        if canonical["identity_status"] == "conflict":
            raise ValueError("Conflicting source episode identity. Resolve the mapping before searching.")
        if not manual and canonical["identity_status"] != "resolved":
            raise ValueError("Episode numbering is unverified. Confirm a manual episode to search.")
        if not manual and (context["season"], context["episode"]) != (canonical["target_season"], canonical["target_episode"]):
            raise ValueError("Conflicting target numbers. Choose the episode again.")
        if (context["imdb_id"], context.get("title"), context.get("year")) != (
                canonical["show_imdb_id"], canonical["show_title"], canonical["show_year"]):
            raise ValueError("Conflicting series identity. Choose the show again.")
        context["episode_identity"] = copy.deepcopy(canonical)
        show_id = canonical["show_id"]
    else:
        if not manual:
            raise ValueError("Confirm the series identity and manual episode numbers before searching.")
        if show_id is not None:
            response, valid_until = metadata._show_details_with_validity(str(show_id))
            show = response["data"].get("item")
            if not show or valid_until is None or (context["imdb_id"], context.get("title"), context.get("year")) != (
                    show["imdb_id"], show["title"], show["year"]):
                raise ValueError("Confirm the show identity in details before entering a manual episode.")
    if show_id is not None:
        context["show_id"] = show_id
    if manual:
        context["manual_confirmed"] = True
    return valid_until


def _pool_state(pool):
    with _STATE_LOCK:
        if not hasattr(pool, "_discover_state"):
            pool._discover_state = {"namespace": uuid.uuid4().hex, "cooldowns": {}}
        return pool._discover_state


def _coverage(pool, state):
    # Registration performs catalog validation. Check the actual class too:
    # historical registration IDs can outlive a removed/rejected installation.
    available = set(get_providers.get_providers_sorted() or [])
    # Resolved here rather than at module import. api/__init__.py eagerly
    # imports every namespace, so a module-level import of the Hub registry and
    # the provider health tracker makes the whole API surface depend on deep
    # provider internals: importing one API module then drags in
    # subliminal_patch.providers and subliminal_patch.provider_health, which is
    # exactly what three existing API tests cannot satisfy when they replace
    # subliminal_patch with a bounded stub. Neither name is needed until a
    # search actually computes provider coverage.
    from provider_hub import registry
    from subliminal_patch.provider_health import get_tracker

    trusted = {item.provider_id for item in registry.active_installations() if item.trusted}
    discarded = get_tracker().currently_discarded() | set(pool.discarded_providers)
    now = time.time()
    planned, outcomes = [], {}
    with _STATE_LOCK:
        cooldowns = dict(state["cooldowns"])
    for name in sorted(set(settings.general.enabled_providers or [])):
        cls = provider_registry[name]
        status, reason, retry_at = None, None, None
        if cls is None:
            status, reason = "setup_required", "provider_unavailable"
        elif not issubclass(cls, registry.HubProxyProvider) or name not in trusted:
            status, reason = "skipped", "not_catalog_provider"
        elif name in service._SKIP_FOR_VIRTUAL_VIDEO:
            status, reason = "skipped", "requires_file"
        elif name in cooldowns and cooldowns[name][0] > now:
            until, prior = cooldowns[name]
            outcomes[name] = {**prior, "result_count": 0, "retry_at": _iso(until)}
            continue
        elif name not in available or name in discarded:
            status, reason = "cooldown", "provider_cooldown"
            throttle = get_providers.tp.get(name)
            if throttle and throttle[1]:
                status = {"AuthenticationError": "authentication_required",
                          "ConfigurationError": "setup_required",
                          "ServiceUnavailable": "unreachable",
                          "ConnectTimeout": "timeout", "ReadTimeout": "timeout"}.get(throttle[0], "cooldown")
                reason = status
                retry_at = throttle[1].astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        elif name not in pool.providers:
            status, reason = "setup_required", "provider_unavailable"
        if status:
            outcomes[name] = {"provider": name, "status": status, "reason": reason,
                              "result_count": 0, "elapsed_ms": 0, "retry_at": retry_at}
        else:
            planned.append(name)
    return planned, outcomes


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if float("-inf") < value < float("inf") else None
    return None


_COMPATIBILITY_FACTS = (("source", "source"), ("resolution", "screen_size"),
                        ("video_codec", "video_codec"), ("audio_codec", "audio_codec"),
                        ("release_group", "release_group"), ("edition", "edition"))


def _comparable(value):
    if isinstance(value, (list, tuple, set)):
        return frozenset(_comparable(item) for item in value if _comparable(item) is not None) or None
    if isinstance(value, str):
        return value.strip().lower() or None
    return str(value).strip().lower() if value is not None else None


def _copy_compatibility(video, sub, parsed):
    """Three-state evidence from the compared facts, not from score keys.

    A scoring match set counts two absent optional fields as agreement and
    cannot tell a missing fact from a contradicted one. This compares what the
    chosen copy actually states against what the offered release actually
    states, and says "unknown" whenever either side is silent. None of it is
    evidence of synchronization.
    """
    release = getattr(sub, "release_info", None) or ""
    if release not in parsed:
        parsed[release] = service._copy_release_hints(release)
    hints = parsed[release]
    result = {}
    for attribute, key in _COMPATIBILITY_FACTS:
        mine, theirs = _comparable(getattr(video, attribute, None)), _comparable(hints.get(key))
        result[attribute] = ("unknown" if mine is None or theirs is None
                             else "match" if mine == theirs else "conflict")
    return result


def _result(sub, video, context, search_id, checked_at, ttl, parsed=None):
    result_id, expires = mint_result(sub, context, search_id, ttl)
    language = sub.language
    matches = None
    if context.get("mode") != "release":
        try:
            matches = sorted(set(sub.get_matches(video)))
        except Exception:
            pass
    score = None
    if matches is not None:
        try:
            score, _ = ComputeScore()(set(matches), sub, video)
        except Exception:
            pass
    forced = getattr(sub, "_reported_forced", None)
    if getattr(language, "forced", False):
        forced = True
    hi = getattr(sub, "_reported_hearing_impaired", None)
    if getattr(language, "hi", False) or getattr(sub, "hearing_impaired", False):
        hi = True
    variant = "-".join(str(part) for part in (language.country, language.script) if part) or None
    return {
        "id": result_id, "search_id": search_id, "provider": sub.provider_name,
        "language": str(language), "language_variant": variant,
        "release": getattr(sub, "release_info", None) or None,
        "uploader": getattr(sub, "uploader", None),
        "scope": "forced" if forced is True else "full" if forced is False else "unknown",
        "hearing_impaired": hi,
        "matches": matches, "compatibility_score": _number(score),
        "compatibility_score_max": MAX_SCORES.get(context.get("media_type")),
        "rating": _number(getattr(sub, "rating", None)),
        "copy_compatibility": (_copy_compatibility(video, sub, parsed)
                               if context.get("copy_id") and parsed is not None else None),
        "checked_at": checked_at, "expires_at": _iso(expires), "stale": False,
    }


def search(request: SearchRequest) -> dict:
    context, refresh = request.context, request.refresh
    pool = service._get_compat_pool(restore_available=True)
    state = _pool_state(pool)
    language = _language(context["language"])
    planned, initial = _coverage(pool, state)
    key = "discover:" + state["namespace"] + ":" + cache.build_key(
        context.get("media_type"), context.get("imdb_id"), context.get("season"), context.get("episode"),
        [language], sorted(set(settings.general.enabled_providers or [])),
        query=context.get("query") if context.get("mode") == "release" else context.get("title"),
        matching_mode=context["matching_mode"], year=context.get("year"),
        requested_languages=[context["language"]],
    )
    key += ":" + hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = cache.compat_region.get(key, ignore_expiration=True)
    # Provider eligibility changes, including catalog removal and throttling,
    # must not reuse a cached claim of complete coverage.
    eligibility = [(name, id(provider_registry[name])) for name in planned]
    eligibility += [(name, item["status"], item["reason"]) for name, item in sorted(initial.items())]
    previous_exists = previous is not NO_VALUE
    if previous_exists and previous.get("_eligibility") != eligibility:
        refresh = True
    if previous_exists and any(resolve_result(row["id"]) is None for row in previous["results"]):
        refresh = True
    ttl = max(1, min(int(settings.compat_endpoint.cache_ttl_seconds),
                     int(settings.compat_endpoint.file_id_ttl_seconds), 1800))
    partial_ttl = max(1, min(ttl, int(settings.compat_endpoint.get("cache_ttl_partial_seconds", 15))))
    expiration = partial_ttl if previous_exists and previous["status"] != "complete" else ttl
    created = False

    def create():
        nonlocal created
        created = True
        # Recompute under the cache creator lock so a preceding search's
        # cooldown applies even to a concurrent explicit refresh.
        providers, outcomes = _coverage(pool, state)
        now = time.time()
        checked = _iso(now)
        search_id = uuid.uuid4().hex
        raw = context.get("mode") == "release"
        video = service._build_video(
            context.get("imdb_id"), context.get("season"), context.get("episode"), context.get("media_type", "movie"),
            query=context["query"] if raw else context.get("title"), year=context.get("year"),
            title_only=not raw, release_query=raw,
            episode_identity=context.get("episode_identity") if not context.get("manual_confirmed") else None,
            copy_path=request.copy["path"] if request.copy is not None else None,
        )
        if request.copy is not None:
            # Only the explicitly chosen copy contributes a file name, size,
            # hash and release description. Confirmed identity is untouched.
            service.refine_video_with_copy(video, request.copy)

        def on_outcome(outcome, elapsed):
            if outcome.provider not in providers:
                return
            unverified = raw and outcome.status == "empty"
            item = {"provider": outcome.provider, "status": "unverified" if unverified else outcome.status,
                    "reason": "query_support_unverified" if unverified else outcome.reason,
                    "result_count": len(outcome.subtitles), "elapsed_ms": elapsed, "retry_at": None}
            if outcome.status not in _COMPLETE and outcome.status != "skipped":
                delay = outcome.retry_after or (300 if outcome.status in {"authentication_required", "setup_required"}
                                                else 60)
                until = time.time() + delay
                item["retry_at"] = _iso(until)
                with _STATE_LOCK:
                    state["cooldowns"][outcome.provider] = (until, dict(item))
            outcomes[outcome.provider] = item

        # This is admission to one provider operation. Cache-creator and
        # preparation waits precede it; admitted work may finish after expiry.
        _admit_provider_search(request)
        # A Discover search never becomes a queue job, so it is observed here or
        # it is invisible to a status reader while it is the running work.
        with activity.observed_operation(
                "discover_search", scope_kind="request", language=context["language"],
                media_type=context.get("media_type"),
                title=context.get("title") or context.get("query") or context.get("imdb_id"),
                season=context.get("season"), episode=context.get("episode")):
            subtitles = service.search_title(video, [language], pool, providers, on_outcome)
        parsed = {}
        rows = [_result(sub, video, context, search_id, checked, ttl, parsed) for sub in subtitles]
        # Failed refreshes retain usable rows only for providers that failed.
        # Successful empty searches replace their earlier results.
        if previous_exists:
            rows.extend({**row, "stale": True} for row in previous["results"]
                        if outcomes.get(row["provider"], {}).get("status") not in _COMPLETE
                        and resolve_result(row["id"]) is not None)
        completed = sum(item["status"] in _COMPLETE for item in outcomes.values())
        incomplete = len(outcomes) - completed
        status = "complete" if completed and not incomplete else "partial" if completed or rows else "failed"
        stale = any(row["stale"] for row in rows)
        return {
            "search_id": search_id, "context": dict(context), "status": status,
            "checked_at": previous["checked_at"] if stale and not completed else checked,
            "attempted_at": checked, "cache_status": "stale" if stale else "fresh",
            "coverage": {"providers": sorted(outcomes.values(), key=lambda item: item["provider"]),
                         "complete": status == "complete", "configured_count": len(outcomes),
                         "completed_count": completed},
            "results": rows, "_eligibility": eligibility,
        }

    snapshot = copy.deepcopy(cache.compat_region.get_or_create(
        key, create, expiration_time=0 if refresh else expiration,
    ))
    snapshot.pop("_eligibility", None)
    if not created and snapshot["cache_status"] != "stale":
        snapshot["cache_status"] = "cached"
    return snapshot
