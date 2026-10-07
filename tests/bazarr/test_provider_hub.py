# coding=utf-8
import base64
import io
import json
import os
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest
from subzero.language import Language
from subliminal.video import Movie
from subliminal_patch.core import Episode

# Retired built-in ids that were still shipping when they were retired, so the
# claim/adoption behavior around them is what an upgrading install actually meets,
# plus the older ones that were deleted before RETIRED_BUILT_IN_PROVIDER_IDS
# existed. Shared with test_retired_provider_config_compat.py so the ids live in
# one place; test_retired_id_lists_partition_the_production_set below anchors both
# lists to the production set.
from retired_providers import (
    LEGACY_RETIRED_PROVIDER_IDS,
    RETIRED_PROVIDER_IDS_UNDER_TEST,
)


@pytest.fixture(autouse=True)
def _enable_provider_hub_auto_install(monkeypatch):
    # Startup auto-install is opt-in (default off). These tests predate that gate and
    # exercise the install path, so enable the flag for the whole module. The OFF behavior
    # is covered separately in test_provider_hub_auto_install_optin.py.
    from app.config import settings
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", True, raising=False)


def _sha256(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()


def _bundle_sha256(file_payloads):
    import hashlib
    digest = hashlib.sha256()
    for relative_path, content in sorted(file_payloads.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest(**overrides):
    provider_content = overrides.pop("provider_content", b"class ExampleProvider: pass\n")
    file_payloads = overrides.pop("file_payloads", {"provider.py": provider_content})
    files = overrides.pop("files", {path: _sha256(content) for path, content in file_payloads.items()})
    bundle_digest = overrides.pop("bundle_sha256", _bundle_sha256(file_payloads))
    manifest = {
        "schema_version": 1,
        "provider_id": "examplehub",
        "name": "Example Hub Provider",
        "version": "1.0.0",
        "api_version": "bazarr.provider-hub.v1",
        "entry_module": "provider",
        "entry_class": "ExampleProvider",
        "config_schema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "secret": True},
            },
        },
        "secret_fields": ["api_key"],
        "supported_media": ["movie", "episode"],
        "languages": ["eng", "spa"],
        "files": files,
        "bundle_sha256": bundle_digest,
        "source": {
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        },
        "dependencies": {
            "python": ">=3.12",
            "requirements": [
                {
                    "name": "cloudscraper",
                    "version": "1.2.58",
                    "hashes": ["sha256:" + ("c" * 64)],
                },
            ],
        },
    }
    manifest.update(overrides)
    return manifest


def _manifest_with_region(**overrides):
    """The standard test manifest plus a declared plain `region` field, for
    the tests that PATCH one through the update route."""
    manifest = _manifest(**overrides)
    manifest["config_schema"]["properties"]["region"] = {"type": "string"}
    return manifest


def _provider_zip(manifest, file_payloads, *, manifest_name="provider.json", prefix=""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{prefix}{manifest_name}", json.dumps(manifest))
        for path, content in file_payloads.items():
            archive.writestr(f"{prefix}{path}", content)
    return buffer.getvalue()


def _patch_local_install_env(monkeypatch, tmp_path):
    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            return self.root / "envs" / validated.provider_id / validated.version / "test"

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    monkeypatch.setattr("provider_hub.service._set_bazarr_provider_enabled", lambda provider_id, enabled: True)


class _FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_manifest_accepts_valid_github_bundle():
    from provider_hub.manifest import validate_manifest

    validated = validate_manifest(_manifest(), built_in_provider_ids={"opensubtitles"})

    assert validated.provider_id == "examplehub"
    assert validated.trusted is False
    assert validated.files == {"provider.py": _sha256(b"class ExampleProvider: pass\n")}
    assert validated.source_path == ""
    assert validated.dependency_requirements[0].pip_line == (
        "cloudscraper==1.2.58 --hash=sha256:" + ("c" * 64)
    )


def test_manifest_accepts_safe_github_source_path():
    from provider_hub.manifest import validate_manifest

    manifest = _manifest(
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "path": "providers/smoke",
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        }
    )

    validated = validate_manifest(manifest, built_in_provider_ids=set())

    assert validated.source_path == "providers/smoke"


@pytest.mark.parametrize("bad_source_path", ["../smoke", "/providers/smoke", "providers/../smoke", "providers\\smoke"])
def test_manifest_rejects_unsafe_github_source_path(bad_source_path):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "path": bad_source_path,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": False,
        }
    )

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


@pytest.mark.parametrize(
    "bad_path",
    [
        "../provider.py",
        "/tmp/provider.py",
        "provider.txt",
        "pkg/../provider.py",
        "pkg/native.so",
    ],
)
def test_manifest_rejects_unsafe_declared_files(bad_path):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(files={bad_path: "a" * 64})

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_secret_field_missing_from_config_schema():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(secret_fields=["missing_secret"])

    with pytest.raises(ManifestValidationError, match="secret field"):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_entry_module_not_declared():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(entry_module="missing")

    with pytest.raises(ManifestValidationError, match="entry_module"):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_manifest_rejects_built_in_provider_shadowing():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(provider_id="opensubtitles")

    with pytest.raises(ManifestValidationError, match="built-in"):
        validate_manifest(manifest, built_in_provider_ids={"opensubtitles"})


def test_manifest_rejects_provider_id_naming_a_bazarr_settings_section():
    # The pool overlay copies every key a plugin declares in its config_schema
    # out of the settings section named after the plugin, so an id that names one
    # of Bazarr's own sections is a credential handout: sonarr.apikey,
    # plex.token, auth.password, postgresql.password. The reserved set is derived
    # from the settings validators, never hand-kept here, so a section added
    # later is covered the day it is declared.
    from provider_hub.manifest import (
        ManifestValidationError,
        reserved_settings_sections,
        validate_manifest,
    )

    reserved = reserved_settings_sections()
    # Two floors under the loop below, because a loop over whatever the set
    # happens to hold stops testing a section the day it silently leaves it.
    # The literal one is the sections the leak was found on; the derived one is
    # every section the secret store keeps a credential in that no provider owns,
    # so the two cannot drift apart as the secret registry grows.
    assert {
        "anticaptcha",
        "auth",
        "captchaai",
        "compat_endpoint",
        "deathbycaptcha",
        "discover",
        "emby",
        "general",
        "jellyfin",
        "omdb",
        "plex",
        "postgresql",
        "proxy",
        "radarr",
        "seerr",
        "silo",
        "sonarr",
        "translator",
    } <= reserved
    assert _secret_bearing_non_provider_sections() <= reserved

    for section in sorted(reserved):
        with pytest.raises(ManifestValidationError, match="settings section"):
            validate_manifest(
                _manifest(provider_id=section, name=section.title()),
                built_in_provider_ids=set(),
            )


def _secret_bearing_non_provider_sections():
    """Sections the secret store keeps a credential in that no provider owns.

    Derived from secret_store.registry rather than listed here, so a credential
    added to a new section is covered by the reserved-section rule the day it is
    registered.
    """
    from provider_hub.migration import (
        MIGRATED_BUILT_IN_PROVIDER_IDS,
        RETIRED_BUILT_IN_PROVIDER_IDS,
    )
    from secret_store.registry import (
        IMPORT_ONLY_SECTIONS,
        SYSTEM_SECRETS,
        USER_VISIBLE_SECRET_LISTS,
        USER_VISIBLE_SECRETS,
        WRITE_ONLY_SECRETS,
    )
    from subliminal_patch.extensions import provider_registry

    secret_paths = (
        USER_VISIBLE_SECRETS
        | USER_VISIBLE_SECRET_LISTS
        | SYSTEM_SECRETS
        | WRITE_ONLY_SECRETS
    )
    sections = {path.split(".", 1)[0] for path in secret_paths} | set(IMPORT_ONLY_SECTIONS)
    return sections - (
        set(provider_registry.names())
        | MIGRATED_BUILT_IN_PROVIDER_IDS
        | RETIRED_BUILT_IN_PROVIDER_IDS
    )


def test_a_registered_hub_plugin_cannot_unreserve_a_section_for_itself():
    # Hub plugins register into the same provider registry as the built-ins, so
    # reading that registry back without subtracting them would let the very
    # plugin the rule exists to stop turn its id into a "provider-owned" section
    # and unreserve it, which is the same trap registry.py's shadow gate names.
    import provider_hub.registry as hub_registry
    from provider_hub.manifest import (
        ManifestValidationError,
        reserved_settings_sections,
        validate_manifest,
    )
    from subliminal_patch.extensions import provider_registry

    class ImpostorProvider:
        pass

    assert "sonarr" in reserved_settings_sections()

    provider_registry.register("sonarr", ImpostorProvider)
    hub_registry._REGISTERED_PROVIDER_HUB_IDS.add("sonarr")
    try:
        assert "sonarr" in reserved_settings_sections()
        with pytest.raises(ManifestValidationError, match="settings section"):
            validate_manifest(
                _manifest(provider_id="sonarr", name="Sonarr"),
                built_in_provider_ids=set(),
            )
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard("sonarr")
        if "sonarr" in provider_registry:
            del provider_registry["sonarr"]


def test_manifest_validation_refuses_when_the_reserved_set_cannot_be_derived(monkeypatch):
    # The reserved set is half of a security gate. Failing open would accept a
    # plugin calling itself sonarr and the pool overlay would then hand it the
    # section, so a derivation that cannot run has to refuse instead.
    import sys

    from provider_hub.manifest import (
        ManifestValidationError,
        reserved_settings_sections,
        validate_manifest,
    )

    monkeypatch.setitem(sys.modules, "app.config", None)

    with pytest.raises(ManifestValidationError, match="reserved"):
        reserved_settings_sections()
    with pytest.raises(ManifestValidationError, match="reserved"):
        validate_manifest(_manifest(), built_in_provider_ids=set())


def test_manifest_validation_refuses_an_empty_reserved_derivation(monkeypatch):
    # Same refusal for settings that import but declare nothing: an empty set
    # reserves nothing at all, which is indistinguishable from no rule.
    import sys
    from types import SimpleNamespace

    from provider_hub.manifest import ManifestValidationError, validate_manifest

    monkeypatch.setitem(sys.modules, "app.config", SimpleNamespace(validators=[]))

    with pytest.raises(ManifestValidationError, match="reserved"):
        validate_manifest(_manifest(), built_in_provider_ids=set())


def test_settings_section_provider_id_is_rejected_from_a_trusted_source():
    # No built-in provider owns one of these sections, so there is nothing for
    # the trusted shadow gate to grant: the rejection holds for every install
    # source, trusted catalog and local upload alike.
    from provider_hub.manifest import ManifestValidationError, validate_manifest
    from provider_hub.migration import validation_built_in_provider_ids
    from provider_hub.service import _built_in_provider_ids

    manifest = _manifest(provider_id="sonarr", name="Sonarr")
    manifest["source"] = dict(manifest["source"], trusted=True)

    with pytest.raises(ManifestValidationError, match="settings section"):
        validate_manifest(
            manifest,
            built_in_provider_ids=validation_built_in_provider_ids(
                "sonarr", _built_in_provider_ids(), trusted=True
            ),
        )


def test_reserved_settings_sections_leave_provider_owned_sections_alone():
    # Catalog plugins reuse built-in provider ids on purpose (subdl, whisperai,
    # titlovi and the rest of the migration allowlist), and every one of those
    # ids is also a settings section. Reserving them would reject those manifests
    # and drop the plugins from the pool on the next boot, so the reserved set is
    # only ever the sections no provider owns.
    from provider_hub.manifest import reserved_settings_sections, validate_manifest
    from provider_hub.migration import (
        MIGRATED_BUILT_IN_PROVIDER_IDS,
        RETIRED_BUILT_IN_PROVIDER_IDS,
    )
    from subliminal_patch.extensions import provider_registry

    provider_owned = (
        set(provider_registry.names())
        | MIGRATED_BUILT_IN_PROVIDER_IDS
        | RETIRED_BUILT_IN_PROVIDER_IDS
    )
    assert not reserved_settings_sections() & provider_owned

    for provider_id in ("subdl", "whisperai", "titlovi", "opensubtitlescom", "subf2m"):
        validate_manifest(
            _manifest(provider_id=provider_id, name=provider_id.title()),
            built_in_provider_ids=set(),
        )


def test_a_plugins_own_config_section_never_becomes_reserved():
    # Saving a plugin's Settings card writes its config into config.yaml under
    # the plugin's id, so the plugin's own section is in the live settings object
    # from the next boot on. Deriving the reserved set from the stored sections
    # instead of the validators would make the plugin reserve itself out of the
    # pool the moment its user configured it.
    from app.config import settings
    from provider_hub.manifest import reserved_settings_sections, validate_manifest

    settings.set("configuredhub", {"api_key": "configured-by-the-user"})
    try:
        assert "configuredhub" not in reserved_settings_sections()
        validate_manifest(
            _manifest(provider_id="configuredhub", name="Configured Hub"),
            built_in_provider_ids=set(),
        )
    finally:
        settings.set("configuredhub", {})


@pytest.mark.parametrize(
    "requirement",
    [
        {"name": "cloudscraper", "version": ">=1.2.58", "hashes": ["sha256:" + ("c" * 64)]},
        {"name": "cloudscraper", "version": "1.2.58", "hashes": []},
        {"name": "git+https://github.com/x/y", "version": "1.0.0", "hashes": ["sha256:" + ("c" * 64)]},
        {"name": "pkg; os_name=='posix'", "version": "1.0.0", "hashes": ["sha256:" + ("c" * 64)]},
    ],
)
def test_manifest_rejects_unlocked_or_unsafe_dependencies(requirement):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    manifest = _manifest(dependencies={"requirements": [requirement]})

    with pytest.raises(ManifestValidationError):
        validate_manifest(manifest, built_in_provider_ids=set())


def test_worker_protocol_round_trips_language_video_and_download_payload():
    from provider_hub.protocol import (
        candidate_from_worker,
        language_to_payload,
        video_to_payload,
        worker_download_to_content,
    )

    language = Language("eng", hi=True)
    movie = Movie(
        "/media/example.mkv",
        "Example Movie",
        year=2024,
        source="Web",
        release_group="GROUP",
        resolution="1080p",
        video_codec="H.264",
        audio_codec="AAC",
        imdb_id="tt1234567",
    )
    movie.hashes["opensubtitles"] = "abc123"
    movie.radarrId = 12
    movie.anilist_id = 12345
    episode = Episode(
        "/media/anime.mkv",
        "Solo Leveling",
        1,
        12,
        series_anidb_id=17495,
        series_anidb_episode_id=277518,
    )
    episode.anilist_id = 151807

    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "provider": "upstream",
            "id": "sub-1",
            "language": language_to_payload(language),
            "release_info": "Example.Movie.2024.1080p-GROUP",
            "filename": "example.srt",
            "matches": ["title", "year", "hash"],
            "score": 360,
            "score_without_hash": 300,
            "score_out_of": 360,
            "hash_verifiable": True,
            "hearing_impaired_verifiable": True,
            "display": {"download_count": 7, "ratings": 4.5},
            "provider_payload": {"provider": "upstream", "schema": 1, "data": {"file_id": "sub-1"}},
        },
    )

    assert language_to_payload(language)["hi"] is True
    assert video_to_payload(movie)["hashes"]["opensubtitles"] == "abc123"
    assert video_to_payload(movie)["media_ids"]["radarrId"] == 12
    assert video_to_payload(movie)["anilist_id"] == 12345
    assert video_to_payload(episode)["anilist_id"] == 151807
    assert video_to_payload(episode)["series_anidb_id"] == 17495
    assert video_to_payload(episode)["series_anidb_series_id"] == 17495
    assert video_to_payload(episode)["series_anidb_episode_id"] == 277518
    assert candidate.provider_name == "examplehub"
    assert candidate.source_provider == "upstream"
    assert candidate.id == "upstream:sub-1"
    assert candidate.matches == {"title", "year", "hash"}
    assert candidate.provider_payload["data"]["file_id"] == "sub-1"

    content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    worker_download_to_content(
        candidate,
        {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": _sha256(content),
            "content_type": "application/x-subrip",
            "empty": False,
        },
    )
    assert candidate.content == content


def test_worker_video_payload_sends_a_null_anilist_id_when_it_is_unknown():
    """An unrefined video still carries the key, as null, so every worker sees
    the same payload shape, and a video object without the attribute must not
    raise."""
    from provider_hub.protocol import video_to_payload

    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    episode = Episode("/media/show.mkv", "Example Show", 1, 2)
    bare = Episode("/media/show.mkv", "Example Show", 1, 2)
    del bare.anilist_id

    for video in (movie, episode, bare):
        payload = video_to_payload(video)
        assert "anilist_id" in payload
        assert payload["anilist_id"] is None
        json.dumps(payload)


def test_hub_subtitle_release_matches_persist_for_download_scoring():
    """Regression: the listing phase (list_subtitles_prioritized) scores a candidate
    via get_matches(), which augments the worker's lean matches with release-info
    matches; the download phase (download_best_subtitles) instead reuses the cached
    subtitle.matches. If get_matches doesn't persist the augmented set, the two phases
    score the SAME subtitle differently: listing sees the release matches and may flag
    the language 'satisfied' (early-exit), while download re-reads the lean cache,
    under-scores the subtitle, and rejects it. Net effect: search stops on a candidate
    download then discards. get_matches must persist the augmented matches so the cache
    download reads matches what listing scored."""
    from provider_hub.protocol import candidate_from_worker, language_to_payload

    movie = Movie(
        "/media/Clue.1985.1080p.BluRay.Remux.AVC.DTS-HD.MA.mkv",
        "Clue",
        year=1985,
        source="Blu-ray",
        video_codec="H.264",
        imdb_id="tt0088930",
    )
    candidate = candidate_from_worker(
        provider_name="opensubtitles",
        payload={
            "provider": "opensubtitles",
            "id": "sub-clue",
            "language": language_to_payload(Language("hun")),
            "release_info": "Clue 1985 720p BluRay x264 EbP",
            "matches": ["title", "year", "imdb_id"],  # lean worker-computed set
            "provider_payload": {"provider": "opensubtitles", "schema": 1},
        },
    )
    worker_matches = set(candidate.matches)

    # Listing phase: get_matches augments the worker set with release-based matches.
    listing_matches = candidate.get_matches(movie)
    assert listing_matches > worker_matches, (
        "release_info should add at least one match (e.g. source/video_codec); "
        f"got {listing_matches} from base {worker_matches}"
    )

    # Download phase reads the cached candidate.matches. It MUST equal the set the
    # listing phase scored, or the two phases disagree and the early-exit misfires.
    assert candidate.matches == listing_matches

    # The returned set is handed straight to compute_score, which mutates it in place
    # (adds hearing_impaired and equivalent matches). Those scoring-only mutations must
    # NOT leak into the cached candidate.matches the download phase reads, or e.g. a
    # force-HI skip in download_best_subtitles can be bypassed. The cache must be an
    # independent copy, not the same object handed to scoring.
    assert "hearing_impaired" not in listing_matches  # guard: not added by get_matches
    listing_matches.add("hearing_impaired")
    assert "hearing_impaired" not in candidate.matches, (
        "scoring mutations on the returned set leaked into the cached matches"
    )


def _hub_candidate(worker_id="sub-arch"):
    from provider_hub.protocol import candidate_from_worker, language_to_payload

    return candidate_from_worker(
        provider_name="examplehub",
        payload={
            "provider": "upstream",
            "id": worker_id,
            "language": language_to_payload(Language("ces")),
            "provider_payload": {"provider": "upstream", "schema": 1},
        },
    )


def test_worker_download_extracts_named_archive_member_host_side():
    from provider_hub.protocol import worker_download_to_content

    candidate = _hub_candidate()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Show.S01E01.ces.srt", b"1\r\n00:00:01,000 --> 00:00:02,000\r\nAhoj\r\n")
        archive.writestr("readme.txt", b"ignore me")
    archive_bytes = buffer.getvalue()

    worker_download_to_content(
        candidate,
        {
            "archive_b64": base64.b64encode(archive_bytes).decode("ascii"),
            "archive_sha256": _sha256(archive_bytes),
            "member": "Show.S01E01.ces.srt",
            "encoding": "latin-1",  # a wrong worker guess that must be ignored
        },
    )

    assert b"Ahoj" in candidate.content
    assert b"\r\n" not in candidate.content  # host applied fix_line_ending
    assert candidate.format == "srt"
    # Archive mode ignores any worker-supplied encoding and keeps the detect-friendly
    # default (utf-8 first, then chardet via Subtitle.normalize()), so a wrong latin-1
    # guess cannot lock in mojibake the way it did in per-provider download payloads.
    assert candidate.encoding != "latin-1"


def test_worker_download_auto_selects_single_archive_member():
    from provider_hub.protocol import worker_download_to_content

    candidate = _hub_candidate("sub-single")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("only.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    archive_bytes = buffer.getvalue()

    worker_download_to_content(
        candidate,
        {"archive_b64": base64.b64encode(archive_bytes).decode("ascii")},
    )

    assert b"Hi" in candidate.content


def test_worker_download_rejects_missing_member_and_bad_archive():
    from provider_hub.protocol import WorkerProtocolError, worker_download_to_content
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    candidate = _hub_candidate("sub-bad")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Show.S01E01.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    archive_bytes = buffer.getvalue()

    with pytest.raises(SubtitleCandidateRejected, match="Pinned subtitle member is absent"):
        worker_download_to_content(
            candidate,
            {
                "archive_b64": base64.b64encode(archive_bytes).decode("ascii"),
                "member": "does-not-exist.srt",
            },
        )

    with pytest.raises(WorkerProtocolError):
        worker_download_to_content(
            candidate,
            {"archive_b64": base64.b64encode(b"not an archive").decode("ascii")},
        )


def test_worker_download_rejects_decompression_bomb(monkeypatch):
    from provider_hub import protocol
    from provider_hub.protocol import WorkerProtocolError, worker_download_to_content

    candidate = _hub_candidate("sub-bomb")
    # 2 MiB member that compresses to almost nothing: a classic zip bomb shape.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Show.S01E01.srt", b"\0" * (2 * 1024 * 1024))
    archive_bytes = buffer.getvalue()
    assert len(archive_bytes) < 50 * 1024  # tiny on disk, huge decompressed

    monkeypatch.setattr(protocol, "_MAX_MEMBER_BYTES", 64 * 1024)
    with pytest.raises(WorkerProtocolError):
        worker_download_to_content(
            candidate, {"archive_b64": base64.b64encode(archive_bytes).decode("ascii")}
        )


def test_worker_download_rejects_oversized_archive(monkeypatch):
    from provider_hub import protocol
    from provider_hub.protocol import WorkerProtocolError, worker_download_to_content

    candidate = _hub_candidate("sub-big")
    monkeypatch.setattr(protocol, "_MAX_ARCHIVE_BYTES", 1024)
    payload_b64 = base64.b64encode(b"x" * 4096).decode("ascii")
    with pytest.raises(WorkerProtocolError):
        worker_download_to_content(candidate, {"archive_b64": payload_b64})


def test_worker_download_extracts_seven_zip_member_host_side():
    py7zr = pytest.importorskip("py7zr")
    from provider_hub.protocol import worker_download_to_content

    candidate = _hub_candidate("sub-7z")
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as archive:
        archive.writestr(b"1\r\n00:00:01,000 --> 00:00:02,000\r\nAhoj\r\n", "Show.S01E01.ces.srt")
    archive_bytes = buffer.getvalue()
    assert archive_bytes[:6] == b"7z\xbc\xaf\x27\x1c"

    worker_download_to_content(
        candidate,
        {"archive_b64": base64.b64encode(archive_bytes).decode("ascii"), "member": "Show.S01E01.ces.srt"},
    )

    assert b"Ahoj" in candidate.content
    assert b"\r\n" not in candidate.content  # host applied fix_line_ending
    assert candidate.format == "srt"


def test_worker_reader_caps_oversized_response_line(monkeypatch):
    # The reader thread must reject an oversized response at the transport layer
    # (a giant readline() line buffers in full before json.loads otherwise).
    import queue
    from types import SimpleNamespace
    from provider_hub import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_MAX_RESPONSE_LINE_BYTES", 8)
    monkeypatch.setattr(worker_mod, "_READ_CHUNK_CHARS", 4)

    class FakeStdout:
        def __init__(self, data):
            self.data = data
            self.pos = 0

        def readline(self, size=-1):
            if self.pos >= len(self.data):
                return ""
            window = len(self.data) if size is None or size < 0 else size
            end = min(self.pos + window, len(self.data))
            newline = self.data.find("\n", self.pos, end)
            if newline != -1:
                end = newline + 1
            chunk = self.data[self.pos:end]
            self.pos = end
            return chunk

    stdout_queue: queue.Queue = queue.Queue()
    # A long line with no newline until the very end exceeds the 8-byte cap.
    worker_mod.ProviderWorkerClient._enqueue_stdout(
        SimpleNamespace(stdout=FakeStdout("X" * 100 + "\n")), stdout_queue
    )
    items = []
    while True:
        item = stdout_queue.get_nowait()
        items.append(item)
        if item is None:
            break
    assert worker_mod._OVERSIZE_RESPONSE in items
    # The reader stopped early instead of buffering the whole 100-char line.
    buffered = sum(len(i) for i in items if isinstance(i, str))
    assert buffered <= worker_mod._MAX_RESPONSE_LINE_BYTES + worker_mod._READ_CHUNK_CHARS


def test_worker_consumer_kills_on_oversized_sentinel(monkeypatch):
    import queue
    from types import SimpleNamespace
    from provider_hub import worker as worker_mod
    from provider_hub.worker import ProviderWorkerClient, WorkerError

    killed = []
    monkeypatch.setattr(worker_mod.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    client = ProviderWorkerClient.__new__(ProviderWorkerClient)
    client.process = SimpleNamespace(
        pid=4242, stdout=object(), kill=lambda: None, wait=lambda timeout=None: None
    )
    client._stdout_queue = queue.Queue()
    client._stdout_queue.put(worker_mod._OVERSIZE_RESPONSE)
    with pytest.raises(WorkerError, match="exceeded"):
        client._read_line_with_deadline(5.0)
    assert killed == [(4242, worker_mod.signal.SIGKILL)], (
        "a worker discarded mid-protocol has to take its children with it")


def test_venv_installer_uses_isolated_hash_checked_pip(monkeypatch, tmp_path):
    from provider_hub.venv import PluginEnvironment
    from provider_hub.manifest import validate_manifest

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    env = PluginEnvironment(tmp_path)
    env.install(validate_manifest(_manifest(), built_in_provider_ids=set()))

    pip_calls = [
        cmd
        for cmd, _kwargs in calls
        if len(cmd) > 3 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert pip_calls, calls
    install_cmd = " ".join(pip_calls[-1])
    assert "--require-hashes" in install_cmd
    assert "--prefer-binary" in install_cmd
    assert "--only-binary=:all:" not in install_cmd
    assert "--no-warn-script-location" in install_cmd
    assert "-r" in install_cmd
    assert "/usr/local" not in install_cmd
    assert "custom_libs" not in install_cmd
    requirements_files = list(tmp_path.glob("envs/examplehub/1.0.0/*/requirements.txt"))
    assert len(requirements_files) == 1
    requirements_text = requirements_files[0].read_text(encoding="utf-8")
    assert "cloudscraper==1.2.58" in requirements_text
    assert "--hash=sha256:" + ("c" * 64) in requirements_text


def test_active_provider_hub_installation_registers_proxy(tmp_path, monkeypatch):
    import provider_hub.registry as hub_registry
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry

    provider_id = "examplehub"
    if provider_id in provider_registry:
        del provider_registry[provider_id]

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Example Hub Provider",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    register_active_provider_classes()

    try:
        assert provider_id in provider_registry.names()
        provider_cls = provider_registry[provider_id]
        assert provider_cls.provider_name == provider_id
        assert provider_cls.languages
    finally:
        # Do not leak this registration into the shared provider_registry, or the
        # subliminal rebase guard (run in the same pytest process in CI) fails.
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]


def test_active_trusted_migrated_provider_replaces_built_in(tmp_path, monkeypatch):
    import provider_hub.registry as hub_registry
    from provider_hub.registry import HubProxyProvider, register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "gestdown"
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class BuiltInGestdownProvider(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    if not had_existing:
        provider_registry.register(provider_id, BuiltInGestdownProvider)
        original_cls = BuiltInGestdownProvider

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Gestdown",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "trusted": True,
                        "manifest": _manifest(
                            provider_id=provider_id,
                            name="Gestdown",
                            dependencies={"requirements": []},
                        ),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        registered = register_active_provider_classes()

        assert registered == [provider_id]
        assert provider_registry[provider_id] is not original_cls
        assert issubclass(provider_registry[provider_id], HubProxyProvider)
        assert provider_registry[provider_id].provider_name == provider_id
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]
        if had_existing and original_cls is not None:
            provider_registry.register(provider_id, original_cls)


def _install(provider_id, *, trusted, manifest):
    from provider_hub.state import ProviderHubInstallation

    return ProviderHubInstallation(
        provider_id=provider_id,
        name=provider_id.title(),
        active_version="1.0.0",
        state="active",
        pending_restart=False,
        manifest=manifest,
        trusted=trusted,
    )


def test_active_trusted_non_allowlisted_provider_cannot_shadow_built_in():
    # The shadow gate is "trusted AND on the migration allowlist". Trust alone must
    # not be enough: a trusted provider whose id is NOT in MIGRATED_BUILT_IN_PROVIDER_IDS
    # must still be refused and the built-in left untouched. A regression that dropped
    # the allowlist check (e.g. `return bool(trusted)`) would silently fail this test.
    import provider_hub.registry as hub_registry
    from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "examplebuiltin"
    assert provider_id not in MIGRATED_BUILT_IN_PROVIDER_IDS
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class BuiltInExampleProvider(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    if not had_existing:
        provider_registry.register(provider_id, BuiltInExampleProvider)
        original_cls = BuiltInExampleProvider

    installation = _install(
        provider_id,
        trusted=True,
        manifest=_manifest(
            provider_id=provider_id,
            name="Example Built-in",
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert provider_id not in registered
        assert provider_registry[provider_id] is original_cls
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if not had_existing and provider_id in provider_registry:
            del provider_registry[provider_id]


def test_active_untrusted_migrated_provider_cannot_shadow_built_in():
    # The provider id IS on the migration allowlist, but the install is untrusted, so
    # register must skip it and leave the built-in class in place (last line of defense
    # against an untrusted state row that shadows a built-in).
    import provider_hub.registry as hub_registry
    from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "gestdown"
    assert provider_id in MIGRATED_BUILT_IN_PROVIDER_IDS
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class BuiltInGestdownProvider(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    if not had_existing:
        provider_registry.register(provider_id, BuiltInGestdownProvider)
        original_cls = BuiltInGestdownProvider

    installation = _install(
        provider_id,
        trusted=False,
        manifest=_manifest(
            provider_id=provider_id,
            name="Gestdown",
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert provider_id not in registered
        assert provider_registry[provider_id] is original_cls
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if not had_existing and provider_id in provider_registry:
            del provider_registry[provider_id]


def test_failed_proxy_build_preserves_shadowed_built_in(monkeypatch):
    # A trusted, allowlisted manifest that passes validation but blows up while the
    # proxy class is being built must not destroy the built-in it shadows: the build
    # happens before the registry is mutated, so on failure the built-in stays intact
    # and the exception does not propagate out of the loop.
    import provider_hub.registry as hub_registry
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "gestdown"
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class BuiltInGestdownProvider(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    if not had_existing:
        provider_registry.register(provider_id, BuiltInGestdownProvider)
        original_cls = BuiltInGestdownProvider

    def _boom(manifest, installation=None):
        raise RuntimeError("proxy class construction failed")

    monkeypatch.setattr(hub_registry, "_make_provider_class", _boom)

    installation = _install(
        provider_id,
        trusted=True,
        manifest=_manifest(
            provider_id=provider_id,
            name="Gestdown",
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert provider_id not in registered
        assert provider_registry[provider_id] is original_cls
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if not had_existing and provider_id in provider_registry:
            del provider_registry[provider_id]


def test_built_in_denylist_keeps_migrated_id_after_registration():
    # Once the trusted migration registers gestdown as a hub provider, gestdown is in
    # _REGISTERED_PROVIDER_HUB_IDS and is no longer a plain built-in class. It must still
    # be reported as a built-in id so a later untrusted install cannot shadow it.
    import provider_hub.registry as hub_registry
    from provider_hub.service import _built_in_provider_ids
    from subliminal_patch.extensions import provider_registry

    provider_id = "gestdown"
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    # Simulate post-migration state: registered as a hub id, absent as a built-in class.
    if had_existing:
        del provider_registry[provider_id]
    hub_registry._REGISTERED_PROVIDER_HUB_IDS.add(provider_id)

    try:
        assert provider_id in _built_in_provider_ids()
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if had_existing and original_cls is not None:
            provider_registry.register(provider_id, original_cls)


def test_untrusted_install_cannot_replace_registered_migrated_provider():
    # gestdown is already registered as a (trusted) hub provider. An untrusted gestdown
    # installation must still be skipped, leaving the registered provider untouched, even
    # though gestdown is no longer in the dynamic built-in set.
    import provider_hub.registry as hub_registry
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "gestdown"
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class RegisteredHubGestdown(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    provider_registry.register(provider_id, RegisteredHubGestdown)
    hub_registry._REGISTERED_PROVIDER_HUB_IDS.add(provider_id)

    installation = _install(
        provider_id,
        trusted=False,
        manifest=_manifest(
            provider_id=provider_id,
            name="Gestdown",
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert provider_id not in registered
        assert provider_registry[provider_id] is RegisteredHubGestdown
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]
        if had_existing and original_cls is not None:
            provider_registry.register(provider_id, original_cls)


def test_retired_id_lists_partition_the_production_set():
    # The two test lists together must BE the production set, not merely live inside
    # it. A retirement that adds an id to RETIRED_BUILT_IN_PROVIDER_IDS and forgets to
    # list it turns this red, instead of silently leaving that id with no coverage in
    # either this file or the config-compat boot.
    from provider_hub.migration import RETIRED_BUILT_IN_PROVIDER_IDS

    assert set(RETIRED_PROVIDER_IDS_UNDER_TEST).isdisjoint(LEGACY_RETIRED_PROVIDER_IDS)
    assert (
        set(RETIRED_PROVIDER_IDS_UNDER_TEST) | set(LEGACY_RETIRED_PROVIDER_IDS)
        == set(RETIRED_BUILT_IN_PROVIDER_IDS)
    )


def test_dead_origin_providers_are_not_builtin_replacements():
    # Providers whose upstream origin is dead must never be on the trusted-replacement
    # allowlist, so a catalog cannot resurrect them by shadowing a (removed) built-in.
    from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS

    assert set(RETIRED_PROVIDER_IDS_UNDER_TEST).isdisjoint(
        MIGRATED_BUILT_IN_PROVIDER_IDS
    )


def test_retired_built_in_ids_stay_claimed_and_are_trusted_only():
    # Retiring a built-in deletes its module, so the id leaves provider_registry and
    # the dynamic half of the shadow gate stops covering it. RETIRED_BUILT_IN_PROVIDER_IDS
    # keeps it claimed. It is a separate set from the migration allowlist, which carries
    # its own auto-install and rename semantics, but both are shadowable on the same
    # terms: trusted yes, untrusted no.
    from provider_hub.migration import (
        MIGRATED_BUILT_IN_PROVIDER_IDS,
        RETIRED_BUILT_IN_PROVIDER_IDS,
    )
    from subliminal_patch.extensions import provider_registry

    for provider_id in RETIRED_PROVIDER_IDS_UNDER_TEST:
        assert provider_id in RETIRED_BUILT_IN_PROVIDER_IDS
    assert RETIRED_BUILT_IN_PROVIDER_IDS.isdisjoint(MIGRATED_BUILT_IN_PROVIDER_IDS)
    for provider_id in RETIRED_BUILT_IN_PROVIDER_IDS:
        # A retired id has no provider class left, which is the whole reason the set
        # has to exist.
        assert provider_id not in provider_registry.names()


@pytest.mark.parametrize(
    "provider_id,trusted,expected",
    # Retired ids: a trusted catalog entry may adopt one, an untrusted one may not.
    # Generated from the shared list rather than spelled out, so the matrix cannot
    # drift from the ids the rest of the file (and the config-compat boot) drives.
    [
        (provider_id, trusted, trusted)
        for provider_id in RETIRED_PROVIDER_IDS_UNDER_TEST + LEGACY_RETIRED_PROVIDER_IDS
        for trusted in (True, False)
    ]
    + [
        # Migrated id: unchanged in both directions.
        ("gestdown", True, True),
        ("gestdown", False, False),
        # On neither set: the gate is pure set membership, so an id nobody claimed
        # is refused at every trust level rather than waved through.
        ("not_a_built_in_provider", True, False),
        ("not_a_built_in_provider", False, False),
    ],
)
def test_shadow_gate_matrix(provider_id, trusted, expected):
    from provider_hub.migration import can_shadow_built_in_provider

    assert can_shadow_built_in_provider(provider_id, trusted) is expected


@pytest.mark.parametrize("provider_id", RETIRED_PROVIDER_IDS_UNDER_TEST)
def test_retired_built_in_provider_id_cannot_be_claimed_by_an_untrusted_plugin(provider_id):
    # Before the built-in module was deleted, an untrusted catalog entry claiming its
    # id was rejected as shadowing a built-in. Deleting the module must not hand the
    # id to the next plugin that asks for it.
    import provider_hub.registry as hub_registry
    from provider_hub.registry import register_active_provider_classes
    from subliminal_patch.extensions import provider_registry

    assert provider_id not in provider_registry.names()

    installation = _install(
        provider_id,
        trusted=False,
        manifest=_manifest(
            provider_id=provider_id,
            name=provider_id.title(),
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert registered == []
        assert provider_id not in provider_registry
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]


@pytest.mark.parametrize("provider_id", RETIRED_PROVIDER_IDS_UNDER_TEST)
def test_retired_built_in_provider_id_can_be_claimed_by_a_trusted_plugin(provider_id):
    # Catalog plugins are the provider mechanism going forward, so a trusted catalog
    # entry adopting a retired built-in id is the intended path. Keeping the id claimed
    # must not lock the official catalog out of it.
    import provider_hub.registry as hub_registry
    from provider_hub.registry import HubProxyProvider, register_active_provider_classes
    from subliminal_patch.extensions import provider_registry

    assert provider_id not in provider_registry.names()

    installation = _install(
        provider_id,
        trusted=True,
        manifest=_manifest(
            provider_id=provider_id,
            name=provider_id.title(),
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert registered == [provider_id]
        assert issubclass(provider_registry[provider_id], HubProxyProvider)
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]


@pytest.mark.parametrize("provider_id", RETIRED_PROVIDER_IDS_UNDER_TEST)
def test_retired_built_in_provider_id_is_rejected_by_untrusted_manifest_validation(provider_id):
    # Same protection one layer earlier: the local-upload path validates the manifest
    # against the full built-in denylist, with no trusted discount, so an untrusted
    # package claiming a retired id never even gets staged. The trusted catalog path
    # discounts the id and validates.
    from provider_hub.manifest import ManifestValidationError, validate_manifest
    from provider_hub.migration import validation_built_in_provider_ids
    from provider_hub.service import _built_in_provider_ids

    assert provider_id in _built_in_provider_ids()

    with pytest.raises(ManifestValidationError):
        validate_manifest(
            _manifest(provider_id=provider_id, name=provider_id.title()),
            built_in_provider_ids=_built_in_provider_ids(),
        )

    validate_manifest(
        _manifest(provider_id=provider_id, name=provider_id.title()),
        built_in_provider_ids=validation_built_in_provider_ids(
            provider_id, _built_in_provider_ids(), trusted=True
        ),
    )


def test_active_trusted_provider_replaces_non_gestdown_built_in():
    # The allowlist covers the full built-in set, not just gestdown: a trusted catalog
    # entry for any allowlisted built-in (here addic7ed) replaces it with a hub proxy.
    import provider_hub.registry as hub_registry
    from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS
    from provider_hub.registry import HubProxyProvider, register_active_provider_classes
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider

    provider_id = "addic7ed"
    assert provider_id in MIGRATED_BUILT_IN_PROVIDER_IDS
    had_existing = provider_id in provider_registry
    original_cls = provider_registry[provider_id] if had_existing else None

    class BuiltInAddic7edProvider(Provider):
        provider_name = provider_id
        languages = {Language("eng")}
        video_types = (Movie,)

    if not had_existing:
        provider_registry.register(provider_id, BuiltInAddic7edProvider)
        original_cls = BuiltInAddic7edProvider

    installation = _install(
        provider_id,
        trusted=True,
        manifest=_manifest(
            provider_id=provider_id,
            name="Addic7ed",
            dependencies={"requirements": []},
        ),
    )

    try:
        registered = register_active_provider_classes(installations=[installation])

        assert registered == [provider_id]
        assert provider_registry[provider_id] is not original_cls
        assert issubclass(provider_registry[provider_id], HubProxyProvider)
    finally:
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]
        if had_existing and original_cls is not None:
            provider_registry.register(provider_id, original_cls)


def test_get_providers_registers_active_provider_hub_installation(tmp_path, monkeypatch):
    import provider_hub.registry as hub_registry
    from app import get_providers
    from subliminal_patch.extensions import provider_registry

    provider_id = "autohub"
    if provider_id in provider_registry:
        del provider_registry[provider_id]

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Auto Hub Provider",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [provider_id], raising=False)

    try:
        assert get_providers.get_providers() == [provider_id]
    finally:
        # Avoid leaking this registration into the shared provider_registry (see the
        # subliminal rebase guard, which runs in the same pytest process in CI).
        hub_registry._REGISTERED_PROVIDER_HUB_IDS.discard(provider_id)
        if provider_id in provider_registry:
            del provider_registry[provider_id]


def test_get_providers_retries_provider_hub_registration_after_failure(monkeypatch):
    from app import get_providers
    import provider_hub.registry as hub_registry

    calls = []

    def fail_then_succeed():
        calls.append("register")
        if len(calls) == 1:
            raise RuntimeError("temporary registration failure")

    monkeypatch.setattr(get_providers, "_PROVIDER_HUB_REGISTRATION_DONE", False)
    monkeypatch.setattr(hub_registry, "register_active_provider_classes", fail_then_succeed)

    get_providers._ensure_provider_hub_registered()

    assert get_providers._PROVIDER_HUB_REGISTRATION_DONE is False

    get_providers._ensure_provider_hub_registered()

    assert get_providers._PROVIDER_HUB_REGISTRATION_DONE is True
    assert calls == ["register", "register"]


def test_provider_hub_database_tables_are_registered():
    from app.database import (
        TableProviderHubCatalogEntry,
        TableProviderHubCatalogSource,
        TableProviderHubConfig,
        TableProviderHubInstallation,
        TableProviderHubInstallEvent,
        TableProviderHubJob,
        TableProviderHubSecret,
        metadata,
    )

    expected = {
        "provider_hub_catalog_sources": TableProviderHubCatalogSource,
        "provider_hub_catalog_entries": TableProviderHubCatalogEntry,
        "provider_hub_installations": TableProviderHubInstallation,
        "provider_hub_config": TableProviderHubConfig,
        "provider_hub_secrets": TableProviderHubSecret,
        "provider_hub_jobs": TableProviderHubJob,
        "provider_hub_install_events": TableProviderHubInstallEvent,
    }

    for table_name, model in expected.items():
        assert table_name in metadata.tables
        assert model.__tablename__ == table_name


def test_provider_hub_api_namespace_is_registered():
    from api import api_ns_list
    from api.provider_hub import api_ns_provider_hub

    assert any(api_ns_provider_hub in group for group in api_ns_list)


def test_provider_hub_scheduler_task_is_registered_on_scheduler():
    from app.scheduler import Scheduler
    from unittest.mock import MagicMock

    instance = Scheduler.__new__(Scheduler)
    instance.aps_scheduler = MagicMock()

    Scheduler._Scheduler__provider_hub_update_task(instance)

    _, args, kwargs = instance.aps_scheduler.add_job.mock_calls[0]
    assert args[1] == "interval"
    assert kwargs["id"] == "provider_hub_update_check"
    assert kwargs["name"] == "Check Provider Hub Updates"
    assert kwargs["replace_existing"] is True


def test_provider_hub_update_task_refreshes_catalog_before_checking_updates(monkeypatch):
    from provider_hub import tasks

    calls = []
    monkeypatch.setattr(tasks, "refresh_catalog", lambda: calls.append("refresh"))
    monkeypatch.setattr(tasks, "check_updates", lambda: calls.append("check") or {"state": "completed"})

    assert tasks.provider_hub_check_updates() == {"state": "completed"}
    assert calls == ["refresh", "check"]


def test_provider_hub_updates_check_endpoint_refreshes_catalog_before_check(monkeypatch):
    from api.provider_hub.provider_hub import ProviderHubUpdatesCheck
    from api.provider_hub import provider_hub

    calls = []
    monkeypatch.setattr(provider_hub.service, "refresh_catalog", lambda: calls.append("refresh"))
    monkeypatch.setattr(
        provider_hub.service,
        "check_updates",
        lambda: calls.append("check") or {"state": "completed"},
    )

    assert ProviderHubUpdatesCheck.post.__wrapped__(ProviderHubUpdatesCheck()) == {"state": "completed"}
    assert calls == ["refresh", "check"]


def test_apply_update_without_available_manifest_does_not_stage(tmp_path, monkeypatch):
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    provider_id = "activehub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                },
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    provider = apply_update(provider_id)

    assert provider["state"] == "active"
    assert provider["pending_restart"] is False
    assert provider.get("staged_version") is None
    assert "No update manifest" in provider["last_error"]
    assert load_state()["installations"][provider_id]["state"] == "active"


def test_apply_update_uses_latest_catalog_manifest(tmp_path, monkeypatch):
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    provider_id = "activehub"
    state_file = tmp_path / "state.json"
    update_manifest = _manifest(provider_id=provider_id, version="1.1.0")
    state_file.write_text(
        json.dumps(
            {
                "catalog_entries": {
                    f"official:{provider_id}:1.1.0": {
                        "source": "official",
                        "source_name": "Official",
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "version": "1.1.0",
                        "trusted": True,
                        "manifest": update_manifest,
                    }
                },
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Active Hub",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "active",
                        "pending_restart": False,
                        "source_bound": True,
                        "source_id": "official",
                        "manifest": _manifest(provider_id=provider_id, version="1.0.0"),
                    }
                },
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated, timeout=None):
            return tmp_path / "env"

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    provider = apply_update(provider_id)

    assert provider["state"] == "staged"
    assert provider["pending_restart"] is True
    assert provider["staged_version"] == "1.1.0"
    installation = load_state()["installations"][provider_id]
    assert installation["staged_manifest"] == update_manifest


def test_provider_hub_restart_activation_promotes_staged_install(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "stagehub"
    staged_path = tmp_path / "provider_hub" / "bundles" / provider_id / "1.1.0" / ("b" * 40)
    staged_python_path = tmp_path / "provider_hub" / "envs" / provider_id / "1.1.0" / "test" / "bin" / "python"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Stage Hub Provider",
                        "active_version": "1.0.0",
                        "staged_version": "1.1.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_path": str(staged_path),
                        "staged_python_path": str(staged_python_path),
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id, version="1.1.0"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    assert activate_staged_installations() == [provider_id]

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.1.0"
    assert installation["active_path"] == str(staged_path)
    assert installation["python_path"] == str(staged_python_path)
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert installation["pending_restart"] is False
    jobs = load_state()["jobs"]
    assert any(
        job.get("action") == "activate"
        and job.get("target_id") == provider_id
        and job.get("state") == "completed"
        for job in jobs
    )


def test_provider_hub_state_write_lock_serializes_load_modify_save_cycles(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state, state_write_lock

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    first_loaded = threading.Event()
    second_entered = threading.Event()
    errors = []

    def write_first():
        try:
            with state_write_lock():
                state = load_state()
                state.setdefault("installations", {})["first"] = {"provider_id": "first"}
                first_loaded.set()
                if second_entered.wait(0.05):
                    errors.append("second writer entered before first save")
                save_state(state)
        except Exception as error:
            errors.append(str(error))

    def write_second():
        try:
            first_loaded.wait(1)
            with state_write_lock():
                second_entered.set()
                state = load_state()
                state.setdefault("installations", {})["second"] = {"provider_id": "second"}
                save_state(state)
        except Exception as error:
            errors.append(str(error))

    first = threading.Thread(target=write_first)
    second = threading.Thread(target=write_second)
    first.start()
    second.start()
    first.join(1)
    second.join(1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    installations = load_state()["installations"]
    assert "first" in installations
    assert "second" in installations


def test_provider_hub_restart_activation_keeps_active_on_staged_failure(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "stagehub"
    old_manifest = _manifest(provider_id=provider_id, version="1.0.0")
    staged_manifest = _manifest(provider_id=provider_id, version="1.1.0")
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Stage Hub Provider",
                        "active_version": "1.0.0",
                        "staged_version": "1.1.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_path": "/missing/bundle",
                        "staged_python_path": "/missing/python",
                        "staged_manifest": staged_manifest,
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": old_manifest,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert activate_staged_installations() == []

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.0.0"
    assert installation["active_path"] == "/old/bundle"
    assert installation["python_path"] == "/old/python"
    assert installation["staged_version"] is None
    assert installation["staged_manifest"] is None
    assert installation["manifest"] == old_manifest
    assert installation["pending_restart"] is False
    assert installation["last_error"]


def test_provider_hub_restart_activation_finalizes_removed_installation(tmp_path, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    provider_id = "removedhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Removed Hub Provider",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "state": "removed",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert activate_staged_installations() == []
    assert provider_id not in load_state()["installations"]


def test_provider_hub_uninstall_discards_staged_install_without_restart(tmp_path, monkeypatch):
    from provider_hub.service import remove_installation
    from provider_hub.state import load_state

    provider_id = "stagedonlyhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Staged Only Hub Provider",
                        "active_version": None,
                        "staged_version": "1.0.0",
                        "staged_path": "/new/bundle",
                        "staged_python_path": "/new/python",
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert remove_installation(provider_id) is True
    assert provider_id not in load_state()["installations"]


def test_provider_hub_uninstall_stages_active_removal_and_discards_staged_update(tmp_path, monkeypatch):
    from provider_hub.service import remove_installation
    from provider_hub.state import load_state

    provider_id = "updatedhub"
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    provider_id: {
                        "provider_id": provider_id,
                        "name": "Updated Hub Provider",
                        "active_version": "1.0.0",
                        "active_path": "/old/bundle",
                        "python_path": "/old/python",
                        "staged_version": "1.1.0",
                        "staged_path": "/new/bundle",
                        "staged_python_path": "/new/python",
                        "staged_manifest": _manifest(provider_id=provider_id, version="1.1.0"),
                        "state": "staged",
                        "pending_restart": True,
                        "manifest": _manifest(provider_id=provider_id),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert remove_installation(provider_id) is True
    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "removed"
    assert installation["pending_restart"] is True
    assert installation["active_version"] == "1.0.0"
    assert installation["staged_version"] is None
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert installation["staged_manifest"] is None


def test_provider_hub_config_is_write_only_with_descriptor(tmp_path, monkeypatch):
    """The Provider Hub installation API never returns a secret's value or
    the old asterisk placeholder: a configured secret comes back empty with a
    sibling descriptor, an absent key keeps the stored value, an explicit
    empty value clears it, and a new value replaces it."""
    from provider_hub.service import (
        SECRET_PLACEHOLDER,
        get_provider,
        runtime_provider_configs,
        update_provider,
    )
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest_with_region(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)

    secret_value = "real-secret-value-1234"  # pragma: allowlist secret
    redacted = update_provider(
        "examplehub",
        enabled=True,
        config={"api_key": secret_value, "region": "eu"},
    )

    assert redacted["enabled"] is True
    # Write-only: the value is empty and the descriptor says how it ends.
    assert redacted["config"]["api_key"] == ""
    descriptor = redacted["config"]["__secret_fields"]["api_key"]
    assert descriptor == {"configured": True, "last_four": "1234"}
    assert secret_value not in json.dumps(redacted)
    assert SECRET_PLACEHOLDER not in json.dumps(redacted)
    assert runtime_provider_configs()["examplehub"]["api_key"] == secret_value

    # An absent key keeps the stored value; a non-secret field updates.
    redacted = update_provider("examplehub", config={"region": "us"})
    assert runtime_provider_configs()["examplehub"] == {
        "api_key": secret_value,
        "region": "us",
    }

    # The descriptor the API echoes back is never stored as config.
    update_provider(
        "examplehub",
        config={"__secret_fields": {"api_key": {"configured": True}}, "region": "de"},
    )
    stored = load_state()["installations"]["examplehub"]["config"]
    assert "__secret_fields" not in stored
    assert stored["region"] == "de"

    # An explicit empty value clears the stored secret.
    cleared = update_provider("examplehub", config={"api_key": ""})
    assert cleared["config"]["__secret_fields"]["api_key"] == {"configured": False}
    assert runtime_provider_configs()["examplehub"]["api_key"] == ""
    assert get_provider("examplehub")["config"]["api_key"] == ""

    # A new value replaces it.
    update_provider("examplehub", config={"api_key": "replacement-secret-value-5678"})  # pragma: allowlist secret
    assert runtime_provider_configs()["examplehub"]["api_key"] == "replacement-secret-value-5678"  # pragma: allowlist secret


def test_an_empty_collection_in_a_stored_secret_is_delivered_to_the_worker_as_empty_text(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """An empty list an earlier write left in a stored installation config
    is not a credential: the worker config build reads it as an empty
    string, so the plugin's setup logic cannot treat the leftover
    collection as a present value."""
    from provider_hub.service import runtime_provider_configs
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {"api_key": [], "region": "eu"},
        }
    }
    save_state(state)

    configs = runtime_provider_configs()
    assert configs["examplehub"]["api_key"] == ""
    assert configs["examplehub"]["region"] == "eu"


def test_manifest_secret_fields_honours_secret_true():
    """A field declared secret only through `secret: true` in the config
    schema is a secret field too, matching what the frontend renders as a
    password input."""
    from provider_hub.service import _manifest_secret_fields

    installation = {
        "manifest": {
            "secret_fields": ["api_key"],
            "config_schema": {
                "type": "object",
                "properties": {
                    "api_key": {"type": "string"},
                    "solver_token": {"type": "string", "secret": True},
                    "plain_url": {"type": "string"},
                },
            },
        },
    }

    assert _manifest_secret_fields(installation) == {"api_key", "solver_token"}


def test_update_provider_with_failing_encrypter_stores_nothing(tmp_path, monkeypatch):
    """A credential that cannot be encrypted is not stored: the save is
    refused with the reason, the Hub state is not written at all, and the
    enabled toggle travelling with the request does not stay behind in
    Bazarr's own settings store either."""
    from provider_hub.service import update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest_with_region(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)

    enabled_calls = []
    monkeypatch.setattr(
        "provider_hub.service._set_bazarr_provider_enabled",
        lambda provider_id, enabled: enabled_calls.append((provider_id, enabled)) or True,
    )

    def broken_encrypt(value, master_key=None):
        raise RuntimeError("key unavailable")

    monkeypatch.setattr("secret_store.encrypt_secret", broken_encrypt)

    with pytest.raises(ValueError) as error:
        update_provider("examplehub", enabled=True,
                        config={"api_key": "unit-secret", "region": "eu"})  # pragma: allowlist secret

    assert "could not be encrypted" in str(error.value).lower()

    # Nothing was stored, in the secret or in the rest of the request: the
    # encryption happens before anything persists, so the toggle never
    # reached the settings store either.
    stored = load_state()["installations"]["examplehub"]["config"]
    assert stored == {}
    assert enabled_calls == []
    assert "unit-secret" not in json.dumps(load_state())


def test_runtime_config_omits_provider_with_undecryptable_secret(tmp_path, monkeypatch):
    """A stored secret that will not decrypt never reaches the worker as the
    credential: the provider is left out of the runtime configs and the
    installation says the credential has to be re-entered."""
    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import runtime_provider_configs
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload", "region": "eu"},
        }
    }
    save_state(state)

    configs = runtime_provider_configs()

    assert "examplehub" not in configs
    assert "tampered-payload" not in json.dumps(configs)
    installation = load_state()["installations"]["examplehub"]
    assert installation["last_error"] == "Stored credential could not be decrypted; re-enter it"


def test_a_reentered_credential_clears_the_decrypt_failed_note(tmp_path, monkeypatch):
    """Re-entering a working credential ends the re-enter-it note: the
    installation stops asking for the key the moment its stored config
    decrypts again, and the provider returns to the runtime configs."""
    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import runtime_provider_configs, update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)

    # A credential that will not decrypt parks the note on the installation.
    update_provider("examplehub",
                    config={"api_key": SECRET_MARKER_PREFIX + "tampered-payload"})
    runtime_provider_configs()
    installation = load_state()["installations"]["examplehub"]
    assert installation["last_error"] == "Stored credential could not be decrypted; re-enter it"

    # Re-entering it clears the note and restores the provider.
    update_provider("examplehub", config={"api_key": "unit-fresh-secret"})  # pragma: allowlist secret
    installation = load_state()["installations"]["examplehub"]
    assert installation.get("last_error") is None
    assert runtime_provider_configs()["examplehub"]["api_key"] == "unit-fresh-secret"


def test_a_schema_change_cannot_expose_a_retained_secret(tmp_path, monkeypatch):
    """A newer version that drops a secret field from its schema does not
    make the retained value visible again: the persisted registration keeps
    it redacted in the installation API and out of the worker config."""
    from secret_store.crypto import encrypt_secret

    from provider_hub.service import list_providers, runtime_provider_configs

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    # The current schema (v2) knows only region; the earlier version's
    # legacy_token value is still stored, and the persisted registration
    # still names it.
    state_file.write_text(json.dumps({
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "2.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": {
                    "provider_id": "examplehub",
                    "version": "2.0.0",
                    "config_schema": {"properties": {"region": {"type": "string"}}},
                },
                "config": {"legacy_token": encrypt_secret("unit-legacy-secret"),
                           "region": "eu"},
            }
        },
        "plugin_secret_paths": ["examplehub.legacy_token"],
        "jobs": [],
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    # The installation API redacts the retained value, with a descriptor.
    providers = list_providers()
    installation = next(p for p in providers if p["provider_id"] == "examplehub")
    assert installation["config"]["legacy_token"] == ""
    assert installation["config"]["__secret_fields"]["legacy_token"]["configured"] is True
    assert "unit-legacy-secret" not in json.dumps(providers)
    # The worker config leaves the undeclared retained field out entirely.
    configs = runtime_provider_configs()
    assert "legacy_token" not in configs["examplehub"]
    assert configs["examplehub"]["region"] == "eu"


def test_a_decrypt_failed_provider_is_blocked_from_searches(tmp_path, monkeypatch):
    """A provider whose stored credential will not decrypt is out of the
    searches completely: no runtime config, no rebuild from the
    installation row, not selected for the pool, and not admitted by a
    stale cached search, so no worker request occurs for it."""
    from types import SimpleNamespace

    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)
    update_provider("examplehub",
                    config={"api_key": SECRET_MARKER_PREFIX + "tampered-payload"})

    from app import get_providers

    # Configuration: neither the runtime configs nor the installation-row
    # rebuild carries it, so the pool has nothing to build a request from.
    assert "examplehub" not in get_providers.get_providers_auth()

    # Selection and admission, against a settings tree that has it enabled.
    with monkeypatch.context() as context:
        context.setattr(
            get_providers, "settings",
            SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
        context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
        context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
        assert "examplehub" not in (get_providers.get_providers() or [])
        assert get_providers.provider_is_usable("examplehub") is False
    # The control: the same fake settings with a working credential keeps
    # the provider selectable, so the block above is the credential's.
    with monkeypatch.context() as context:
        context.setattr(
            get_providers, "settings",
            SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
        context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
        context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
        update_provider("examplehub", config={"api_key": "unit-fresh-secret"})  # pragma: allowlist secret
        assert "examplehub" in (get_providers.get_providers() or [])
        assert get_providers.provider_is_usable("examplehub") is True


def test_removed_plugin_keeps_registered_secret_paths(tmp_path, monkeypatch):
    """Removing a plugin deletes its installation row but keeps its secret
    paths in the persisted set, so the value still decrypts and stays
    write-only. The set starts empty: the installation's own row is what
    persists the path, which is exactly the registration the removal must
    survive."""
    from provider_hub.service import remove_installation, update_provider
    from provider_hub.state import load_state

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "installations": {
                    "examplehub": {
                        "provider_id": "examplehub",
                        "name": "Example Hub Provider",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "manifest": _manifest(provider_id="examplehub"),
                    }
                },
                "plugin_secret_paths": [],
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service._set_bazarr_provider_enabled", lambda provider_id, enabled: True)

    # Configuring the plugin persists its manifest's secret path.
    update_provider("examplehub", config={"api_key": "unit-secret"})  # pragma: allowlist secret
    assert "examplehub.api_key" in (load_state().get("plugin_secret_paths") or [])

    from provider_hub.service import activate_staged_installations

    assert remove_installation("examplehub") is True
    # The row goes at the next restart, and the restart's own state write
    # keeps the persisted paths.
    activate_staged_installations()

    state = load_state()
    assert "examplehub" not in (state.get("installations") or {})
    persisted_paths = state.get("plugin_secret_paths") or []
    assert "examplehub.api_key" in persisted_paths


# The registry must stay hermetic around the retained-field tests: the
# runtime plugin paths live for the whole process, and one test's
# registrations must not decide another test's field classification.
@pytest.fixture
def isolated_plugin_registry():
    from secret_store import registry

    saved_paths = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    yield registry
    registry.reset_plugin_secret_paths(saved_paths)


class _EmptyScanSettings:
    """A settings stand-in for the boot registry loader: no sections at
    all, so the leftover scan finds nothing and the loader registers only
    what state.json names."""

    def as_dict(self):
        return {}


def _write_state(tmp_path, state):
    state_file = tmp_path / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return state_file


def test_update_provider_rejects_a_non_string_secret(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """The Hub API refuses a new secret value that is not text. A number or
    a boolean in a secret slot is exactly the shape an older save coerced
    typed tokens into, and accepting it would store a credential that
    neither encrypts at rest nor masks in the API. Nothing is stored, and a
    text value still stores encrypted."""
    from provider_hub.service import update_provider
    from provider_hub.state import load_state, save_state
    from secret_store.crypto import is_encrypted

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)

    for bad in (12345, 1.5, True):
        with pytest.raises(ValueError):
            update_provider("examplehub", config={"api_key": bad})
    assert load_state()["installations"]["examplehub"]["config"] == {}

    # The refusal is the credential's, not the field's: text still stores.
    update_provider("examplehub", config={"api_key": "unit-text-secret"})  # pragma: allowlist secret
    stored = load_state()["installations"]["examplehub"]["config"]
    assert is_encrypted(stored["api_key"])


def test_updating_a_retained_secret_field_stores_it_encrypted(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A PATCH to a field the current schema dropped but the registry still
    calls a secret is stored encrypted, not written verbatim in the clear
    until the next boot, and the installation API keeps redacting it with
    a descriptor."""
    from provider_hub.service import update_provider
    from secret_store.crypto import encrypt_secret, is_encrypted

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "2.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": {
                    "provider_id": "examplehub",
                    "version": "2.0.0",
                    "config_schema": {"properties": {"region": {"type": "string"}}},
                },
                "config": {"legacy_token": encrypt_secret("unit-old-secret"),  # pragma: allowlist secret
                           "region": "eu"},
            }
        },
        "plugin_secret_paths": ["examplehub.legacy_token"],
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    update_provider("examplehub", config={"legacy_token": "unit-fresh-secret"})  # pragma: allowlist secret

    stored = json.loads(state_file.read_text(encoding="utf-8"))["installations"]["examplehub"]["config"]
    assert is_encrypted(stored["legacy_token"])
    assert stored["region"] == "eu"
    from provider_hub.service import get_provider
    provider = get_provider("examplehub")
    assert provider["config"]["legacy_token"] == ""
    assert provider["config"]["__secret_fields"]["legacy_token"]["configured"] is True
    assert "unit-fresh-secret" not in json.dumps(provider)


def test_a_staged_schema_cannot_put_a_retained_secret_in_the_active_worker(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A field only the staged version declares never reaches the current
    worker: the exclusion filters against the active manifest alone, so an
    update waiting for a restart does not hand the plugin a credential its
    running code does not read. Ciphertext of that field does not block the
    provider either, because a retained field counts as undecryptable only
    when the active manifest declares it."""
    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import (decrypt_blocked_provider_ids,
                                     runtime_provider_configs)

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "2.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": {
                    "provider_id": "examplehub",
                    "version": "2.0.0",
                    "config_schema": {"properties": {"region": {"type": "string"}}},
                },
                "staged_manifest": {
                    "provider_id": "examplehub",
                    "version": "3.0.0",
                    "config_schema": {"properties": {
                        "region": {"type": "string"},
                        "legacy_token": {"type": "string", "secret": True},
                    }},
                },
                "config": {"legacy_token": SECRET_MARKER_PREFIX + "tampered-payload",
                           "region": "eu"},
            }
        },
        "plugin_secret_paths": ["examplehub.legacy_token"],
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    configs = runtime_provider_configs()
    assert "examplehub" in configs
    assert "legacy_token" not in configs["examplehub"]
    assert configs["examplehub"]["region"] == "eu"
    assert decrypt_blocked_provider_ids() == set()


def test_a_pre_registry_installations_retained_secret_is_covered(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A pre-registry upgrade: a stored legacy credential no current
    manifest declares and no persisted registration names is discovered
    from the installation config itself, registered and persisted at boot,
    encrypted in state.json, redacted by the installation API and kept out
    of the worker config."""
    from provider_hub import service
    from provider_hub.state import load_state
    from secret_store.crypto import is_encrypted

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "2.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": {
                    "provider_id": "examplehub",
                    "version": "2.0.0",
                    "config_schema": {"properties": {"region": {"type": "string"}}},
                },
                "config": {"legacy_token": "unit-legacy-secret", "region": "eu"},  # pragma: allowlist secret
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    # The boot: the loader discovers the field, the boot-migration encrypts
    # it once the master key is durable.
    isolated_plugin_registry.load_plugin_secret_paths(_EmptyScanSettings())
    assert isolated_plugin_registry.is_plugin_secret("examplehub.legacy_token")
    isolated_plugin_registry.migrate_installation_config_secrets()
    state = load_state()
    assert "examplehub.legacy_token" in (state.get("plugin_secret_paths") or [])
    stored = state["installations"]["examplehub"]["config"]
    assert is_encrypted(stored["legacy_token"])
    assert stored["region"] == "eu"

    # The installation API redacts it; the worker config leaves it out.
    providers = service.list_providers()
    installation = next(p for p in providers if p["provider_id"] == "examplehub")
    assert installation["config"]["legacy_token"] == ""
    assert installation["config"]["__secret_fields"]["legacy_token"]["configured"] is True
    assert "unit-legacy-secret" not in json.dumps(providers)
    configs = service.runtime_provider_configs()
    assert "legacy_token" not in configs["examplehub"]
    assert configs["examplehub"]["region"] == "eu"


def test_a_settings_section_value_satisfies_a_blocked_provider(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A provider whose stored credential will not decrypt is unblocked the
    moment its own settings section supplies a live value for the field:
    the worker config is laid over that section, so the plugin runs on the
    section's value. With no section value the provider stays out of the
    runtime configs, the selection and the admission."""
    from types import SimpleNamespace

    from app import get_providers
    from app.config import settings
    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import (decrypt_blocked_provider_ids,
                                      runtime_provider_configs)

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload",
                           "region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        # No section value: blocked in every direction.
        assert "examplehub" not in runtime_provider_configs()
        assert decrypt_blocked_provider_ids() == {"examplehub"}
        with monkeypatch.context() as context:
            context.setattr(
                get_providers, "settings",
                SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
            context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
            context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
            assert "examplehub" not in (get_providers.get_providers() or [])
            assert get_providers.provider_is_usable("examplehub") is False

        # The section supplies a working value: the block lifts, the stored
        # side keeps serving the rest of the config, and the provider is
        # selectable and admissible again.
        settings.set("examplehub", {"api_key": "unit-live-secret"})  # pragma: allowlist secret
        configs = runtime_provider_configs()
        assert "examplehub" in configs
        assert "api_key" not in configs["examplehub"]
        assert configs["examplehub"]["region"] == "eu"
        assert decrypt_blocked_provider_ids() == set()
        with monkeypatch.context() as context:
            context.setattr(
                get_providers, "settings",
                SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
            context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
            context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
            assert "examplehub" in (get_providers.get_providers() or [])
            assert get_providers.provider_is_usable("examplehub") is True
    finally:
        from app.config import remove_settings_section
        remove_settings_section("examplehub")


def test_an_empty_section_collection_does_not_unblock_a_decrypt_failed_provider(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """An empty list or dict at the field is an absent credential, not a
    supplied one, so it must not lift the decrypt block: the provider stays
    out of the runtime configs, the selection and the admission until a
    real value is supplied."""
    from types import SimpleNamespace

    from app import get_providers
    from app.config import remove_settings_section, settings
    from provider_hub import service
    from secret_store.crypto import SECRET_MARKER_PREFIX

    from provider_hub.service import (decrypt_blocked_provider_ids,
                                      runtime_provider_configs)

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload",
                           "region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        # No section value at all: blocked in every direction.
        assert "examplehub" not in runtime_provider_configs()
        assert decrypt_blocked_provider_ids() == {"examplehub"}

        # The section holds an empty collection at the field: an absent
        # value, so the block stands in every direction.
        for empty in ([], {}):
            settings.set("examplehub", {"api_key": empty})
            assert "examplehub" not in runtime_provider_configs()
            assert decrypt_blocked_provider_ids() == {"examplehub"}
            with monkeypatch.context() as context:
                context.setattr(
                    get_providers, "settings",
                    SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
                context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
                context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
                assert "examplehub" not in (get_providers.get_providers() or [])
                assert get_providers.provider_is_usable("examplehub") is False

        # A real value in the section is a supplied credential: the block
        # lifts and the provider is searched again.
        settings.set("examplehub", {"api_key": "unit-live-secret"})  # pragma: allowlist secret
        assert "examplehub" in runtime_provider_configs()
        assert decrypt_blocked_provider_ids() == set()
        with monkeypatch.context() as context:
            context.setattr(
                get_providers, "settings",
                SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
            context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
            context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
            assert "examplehub" in (get_providers.get_providers() or [])
            assert get_providers.provider_is_usable("examplehub") is True
    finally:
        remove_settings_section("examplehub")
        service._BLOCK_CACHE["key"] = None


def test_a_failed_settings_section_credential_blocks_selection_and_admission(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A credential in the plugin's own config.yaml section that failed to
    decrypt at boot is withheld from the overlay, so with no working stored
    value behind it the provider is left out of configuration, selection
    and admission, exactly like a broken stored credential. A working
    stored value for the same declared field satisfies it."""
    from types import SimpleNamespace

    from app import get_providers
    from provider_hub import service
    from provider_hub.service import (decrypt_blocked_provider_ids,
                                      update_provider)
    from secret_store.migration import reset_decrypt_failures

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    # The post-boot state of a failed section credential: the live value is
    # withheld as empty and its path is in the failure set.
    from secret_store.migration import _FAILED_DECRYPT_PATHS
    _FAILED_DECRYPT_PATHS.add("examplehub.api_key")

    try:
        assert decrypt_blocked_provider_ids() == {"examplehub"}
        # Configuration: the withheld empty value is never laid over the
        # stored config, and the broken credential travels nowhere.
        auth = get_providers.get_providers_auth()
        assert "api_key" not in auth["examplehub"]
        assert "tampered" not in json.dumps(auth)
        with monkeypatch.context() as context:
            context.setattr(
                get_providers, "settings",
                SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
            context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
            context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
            assert "examplehub" not in (get_providers.get_providers() or [])
            assert get_providers.provider_is_usable("examplehub") is False

        # A working stored value for the same declared field satisfies the
        # failed section credential: the overlay is empty, the stored one
        # stands, and the provider is searched again.
        update_provider("examplehub", config={"api_key": "unit-stored-secret"})  # pragma: allowlist secret
        assert decrypt_blocked_provider_ids() == set()
        with monkeypatch.context() as context:
            context.setattr(
                get_providers, "settings",
                SimpleNamespace(general=SimpleNamespace(enabled_providers=["examplehub"])))
            context.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
            context.setattr(get_providers.provider_registry, "names", lambda: ["examplehub"])
            assert "examplehub" in (get_providers.get_providers() or [])
            assert get_providers.provider_is_usable("examplehub") is True
        assert get_providers.get_providers_auth()["examplehub"]["api_key"] == "unit-stored-secret"
    finally:
        reset_decrypt_failures()
        service._BLOCK_CACHE["key"] = None


def test_the_blocked_set_is_cached_and_invalidated_by_a_state_mutation(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """The admission gate runs before every provider call, so the blocked
    set is computed once per state.json version: repeated calls do not
    re-read and re-walk the file, and a mutation that fixes a credential
    invalidates the cache on the spot."""
    from provider_hub import service
    from provider_hub.service import decrypt_blocked_provider_ids, update_provider
    from secret_store.crypto import SECRET_MARKER_PREFIX

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert decrypt_blocked_provider_ids() == {"examplehub"}

    # The cached answer: no re-walk of the state behind it.
    original = service._effective_installation_config
    calls = []

    def counting(installation, state=None):
        calls.append(installation.get("provider_id"))
        return original(installation, state)

    monkeypatch.setattr(service, "_effective_installation_config", counting)
    try:
        assert decrypt_blocked_provider_ids() == {"examplehub"}
        assert calls == []

        # A mutation invalidates the cache: the next call recomputes.
        update_provider("examplehub", config={"api_key": "unit-fresh-secret"})  # pragma: allowlist secret
        assert decrypt_blocked_provider_ids() == set()
        assert calls == ["examplehub"]
    finally:
        monkeypatch.setattr(service, "_effective_installation_config", original)
        service._BLOCK_CACHE["key"] = None


def test_a_cache_hit_never_loads_state(tmp_path, monkeypatch, isolated_plugin_registry):
    """The cache key is read before anything else, so a hit answers without
    loading and parsing state.json: the admission gate runs before every
    provider call, and the file read it saved was the point of the cache."""
    from provider_hub import service
    from provider_hub.service import decrypt_blocked_provider_ids
    from secret_store.crypto import SECRET_MARKER_PREFIX

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        # The first call computes and caches under the state's identity.
        assert decrypt_blocked_provider_ids() == {"examplehub"}

        original = service.load_state
        loads = []

        def counting():
            loads.append(1)
            return original()

        monkeypatch.setattr(service, "load_state", counting)
        assert decrypt_blocked_provider_ids() == {"examplehub"}
        assert loads == []
    finally:
        service._BLOCK_CACHE["key"] = None


def test_a_save_during_the_cache_computation_is_not_cached(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A credential corrected while the gate is computing its answer must not
    have the pre-save answer cached under the post-save state identity: the
    provider would stay out of searches until some other write, exactly what
    the correction was supposed to end. The answer computed from the
    pre-save state is served for that one call and never stored."""
    from provider_hub import service
    from provider_hub.service import (decrypt_blocked_provider_ids,
                                      update_provider)
    from secret_store.crypto import SECRET_MARKER_PREFIX

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    original_load = service.load_state
    saved = {"done": False}

    def load_then_save():
        state = original_load()
        if not saved["done"]:
            saved["done"] = True
            # The correction lands while the gate holds the pre-save state.
            update_provider("examplehub", config={"api_key": "unit-fresh-secret"})  # pragma: allowlist secret
        return state

    monkeypatch.setattr(service, "load_state", load_then_save)
    try:
        assert decrypt_blocked_provider_ids() == {"examplehub"}
        # The next call sees the corrected state, not the stale answer.
        assert decrypt_blocked_provider_ids() == set()
    finally:
        service._BLOCK_CACHE["key"] = None


def test_the_update_path_refuses_to_encrypt_under_a_memory_only_master_key(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """The Hub update path makes the master key durable before any secret it
    encrypts is committed. Without that, a settings write that keeps failing
    would leave ciphertext in state.json under a key that exists only in
    memory, and a restart would mint a different one: a credential no boot
    can ever open. The refusal is the existing could-not-be-encrypted one,
    and nothing is stored."""
    from provider_hub.state import load_state
    from secret_store import crypto
    from provider_hub.service import update_provider

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    # A master key that exists only in this process's memory: the boot has
    # not written it to disk, and the settings write that would keeps
    # failing.
    monkeypatch.setattr(crypto, "_unpersisted_master_keys", set())
    original_get_master_key = crypto.get_master_key

    def memory_only_key(settings_obj=None):
        key = original_get_master_key(settings_obj)
        crypto._unpersisted_master_keys.add(key)
        return key

    monkeypatch.setattr(crypto, "get_master_key", memory_only_key)
    monkeypatch.setattr("app.config.write_config", lambda: False)

    with pytest.raises(ValueError) as refused:
        update_provider("examplehub", config={"api_key": "unit-fresh-secret"})  # pragma: allowlist secret
    assert "could not be encrypted" in str(refused.value)
    stored = load_state()["installations"]["examplehub"]["config"]
    assert stored == {"region": "eu"}
    assert "enc:v1:" not in json.dumps(stored)


def test_the_update_route_refuses_config_keys_no_manifest_declares(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A PATCH can only write fields a manifest declares or the registry
    names. An undeclared key would sit in plaintext in state, the API
    response and the worker config until the next boot's discovery
    disagreed with what this write did, so the route refuses it, and the
    refusal is atomic: a declared field in the same request is not applied
    either."""
    from provider_hub.state import load_state
    from provider_hub.service import update_provider

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest_with_region(dependencies={"requirements": []}),
                "config": {"region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    with pytest.raises(ValueError) as refused:
        update_provider("examplehub", config={"undeclared_token": "unit-value"})  # pragma: allowlist secret
    assert "not declared" in str(refused.value)

    before = load_state()["installations"]["examplehub"]["config"]
    with pytest.raises(ValueError):
        update_provider("examplehub", config={"region": "us", "undeclared_token": "unit-value"})  # pragma: allowlist secret
    assert load_state()["installations"]["examplehub"]["config"] == before
    assert "undeclared_token" not in json.dumps(before)

    # A declared plain field and a declared secret still store, together.
    provider = update_provider("examplehub", config={"region": "us", "api_key": "unit-text-secret"})  # pragma: allowlist secret
    assert provider["config"]["region"] == "us"
    assert load_state()["installations"]["examplehub"]["config"]["region"] == "us"


def test_a_mixed_case_secret_key_is_stored_under_its_canonical_name(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A PATCH naming a declared secret with the wrong case (API_KEY for
    api_key) is canonicalized before classification, encryption and
    storage, the same lowercase spelling the manifest declares and the
    reader walks. Stored as typed it would sit beside the declared field
    in plaintext under its mixed-case name, and the redaction, which
    answers for the declared name, would hand it back as it stood."""
    from provider_hub.service import runtime_provider_configs, update_provider
    from provider_hub.state import load_state
    from secret_store.crypto import decrypt_secret, is_encrypted

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest_with_region(dependencies={"requirements": []}),
                "config": {"region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    provider = update_provider("examplehub", config={"API_KEY": "unit-secret-value"})  # pragma: allowlist secret

    # Stored under the canonical name only, encrypted at rest.
    stored = load_state()["installations"]["examplehub"]["config"]
    assert "API_KEY" not in stored
    assert is_encrypted(stored["api_key"])
    assert decrypt_secret(stored["api_key"]) == "unit-secret-value"  # pragma: allowlist secret
    # The reader treats the key the way the writer does: the redaction
    # answers for the canonical name, and no value comes back under
    # either spelling.
    assert provider["config"]["api_key"] == ""
    assert "API_KEY" not in provider["config"]
    assert "unit-secret-value" not in json.dumps(provider)  # pragma: allowlist secret
    # The worker receives the decrypted credential under the canonical
    # name.
    assert runtime_provider_configs()["examplehub"]["api_key"] == "unit-secret-value"  # pragma: allowlist secret


def test_a_persisted_mixed_case_credential_is_never_served_or_stored_as_it_stood(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A credential an earlier version persisted under a mixed-case alias
    of a declared secret field (API_KEY for api_key) is the credential the
    canonical name answers for. The installation API never returns it,
    whatever spelling it sits under, and the next update canonicalizes it
    away: an update that replaces the credential drops the alias with the
    old value, and one that touches nothing related moves the alias's
    value under the canonical name, encrypted. The alias never survives
    to ride another response."""
    from provider_hub.service import get_provider, update_provider
    from provider_hub.state import load_state
    from secret_store.crypto import decrypt_secret, is_encrypted

    alias_state = {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest_with_region(dependencies={"requirements": []}),
                "config": {"region": "eu", "API_KEY": "unit-alias-secret"},
            }
        },
        "jobs": [],
    }

    # The redaction answers for every spelling: the stored alias value
    # never comes back.
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_write_state(tmp_path, alias_state)))
    served = get_provider("examplehub")["config"]
    assert served["API_KEY"] == ""
    assert served["api_key"] == ""
    assert "unit-alias-secret" not in json.dumps(served)  # pragma: allowlist secret

    # An update that replaces the credential stores the canonical writer's
    # value and drops the alias with it.
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_write_state(tmp_path, alias_state)))
    update_provider("examplehub", config={"api_key": "unit-new-secret"})  # pragma: allowlist secret
    stored = load_state()["installations"]["examplehub"]["config"]
    assert "API_KEY" not in stored
    assert is_encrypted(stored["api_key"])
    assert decrypt_secret(stored["api_key"]) == "unit-new-secret"  # pragma: allowlist secret
    assert "unit-alias-secret" not in json.dumps(get_provider("examplehub"))  # pragma: allowlist secret

    # An unrelated update canonicalizes the alias away too: the stored
    # value moves under the canonical name, encrypted at rest.
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_write_state(tmp_path, alias_state)))
    update_provider("examplehub", config={"region": "us"})
    stored = load_state()["installations"]["examplehub"]["config"]
    assert stored["region"] == "us"
    assert set(stored) == {"region", "api_key"}
    assert is_encrypted(stored["api_key"])
    assert decrypt_secret(stored["api_key"]) == "unit-alias-secret"  # pragma: allowlist secret


def test_an_unrelated_update_encrypts_a_stored_non_text_alias(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A stored alias holding a non-string scalar (a typed token an older
    save coerced into a number) moves under the canonical name as the typed
    text it stands for, encrypted: the move is a write to a secret slot, so
    the canonical secret name never holds plaintext after it, and the
    worker receives the decrypted token rather than a bare number."""
    from provider_hub.service import runtime_provider_configs, update_provider
    from provider_hub.state import load_state
    from secret_store.crypto import decrypt_secret, is_encrypted

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest_with_region(dependencies={"requirements": []}),
                "config": {"region": "eu", "API_KEY": 12345678901234567},
            }
        },
        "jobs": [],
    })))

    update_provider("examplehub", config={"region": "us"})

    stored = load_state()["installations"]["examplehub"]["config"]
    assert "API_KEY" not in stored
    assert stored["region"] == "us"
    assert is_encrypted(stored["api_key"])
    assert decrypt_secret(stored["api_key"]) == "12345678901234567"
    assert runtime_provider_configs()["examplehub"]["api_key"] == "12345678901234567"


def test_a_mixed_case_alias_colliding_with_a_canonical_value_keeps_the_canonical(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """The stored alias never displaces the canonical value: whichever
    order the config holds them in, the value the canonical writer
    exposes is the one that stays and the alias drops."""
    from provider_hub.service import update_provider
    from provider_hub.state import load_state
    from secret_store.crypto import decrypt_secret, encrypt_secret

    for stored_config in ({"region": "eu", "API_KEY": "unit-alias-left",
                           "api_key": encrypt_secret("unit-canonical")},
                          {"region": "eu", "api_key": encrypt_secret("unit-canonical"),
                           "API_KEY": "unit-alias-left"}):
        monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_write_state(tmp_path, {
            "installations": {
                "examplehub": {
                    "provider_id": "examplehub",
                    "name": "Example Hub Provider",
                    "active_version": "1.0.0",
                    "state": "active",
                    "pending_restart": False,
                    "manifest": _manifest_with_region(dependencies={"requirements": []}),
                    "config": dict(stored_config),
                }
            },
            "jobs": [],
        })))

        update_provider("examplehub", config={"region": "us"})

        stored = load_state()["installations"]["examplehub"]["config"]
        assert set(stored) == {"region", "api_key"}
        assert stored["region"] == "us"
        assert decrypt_secret(stored["api_key"]) == "unit-canonical"  # pragma: allowlist secret


def test_a_lifted_block_clears_the_stale_re_enter_it_note(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """The re-enter-it note is the echo of the block, so it goes when the
    block lifts on its own: a settings-section value starts covering the
    field, the provider is searched again, and the installation stops
    asking for a credential it already has. While the block stands, the
    note stays."""
    from app.config import remove_settings_section, settings
    from provider_hub import service
    from provider_hub.service import (DECRYPT_FAILED_MESSAGE,
                                      runtime_provider_configs)
    from provider_hub.state import load_state
    from secret_store.crypto import SECRET_MARKER_PREFIX

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": False,
                "manifest": _manifest(dependencies={"requirements": []}),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload",
                           "region": "eu"},
                "last_error": DECRYPT_FAILED_MESSAGE,
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        # Blocked: out of the runtime configs, and the note stands.
        assert "examplehub" not in runtime_provider_configs()
        assert load_state()["installations"]["examplehub"]["last_error"] == DECRYPT_FAILED_MESSAGE

        # The settings section starts supplying the credential: the block
        # lifts, the provider is admitted, and the note is cleared with it.
        settings.set("examplehub", {"api_key": "unit-live-secret"})  # pragma: allowlist secret
        configs = runtime_provider_configs()
        assert "examplehub" in configs
        assert "api_key" not in configs["examplehub"]
        assert load_state()["installations"]["examplehub"]["last_error"] is None
    finally:
        remove_settings_section("examplehub")
        service._BLOCK_CACHE["key"] = None


def test_the_settings_overlay_covers_a_pending_restart_installation(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A pending update keeps the active version running, and the runtime
    configs admit it with its settings-section value standing in for a
    credential the stored config can no longer supply. The overlay walks the
    same active-version population with the active manifest, so the
    replacement actually reaches the worker; a field only the staged schema
    declares never does, because the code that is running is the active
    version's."""
    from app import get_providers
    from app.config import remove_settings_section, settings
    from provider_hub import service
    from provider_hub.service import (decrypt_blocked_provider_ids,
                                      runtime_provider_configs)
    from secret_store.crypto import SECRET_MARKER_PREFIX

    state_file = _write_state(tmp_path, {
        "installations": {
            "examplehub": {
                "provider_id": "examplehub",
                "name": "Example Hub Provider",
                "active_version": "1.0.0",
                "state": "active",
                "pending_restart": True,
                "manifest": _manifest(dependencies={"requirements": []}),
                "staged_manifest": _manifest(
                    version="2.0.0",
                    config_schema={
                        "type": "object",
                        "properties": {
                            "api_key": {"type": "string", "secret": True},
                            "staged_only_token": {"type": "string", "secret": True},
                        },
                    },
                    secret_fields=["api_key", "staged_only_token"],
                    dependencies={"requirements": []},
                ),
                "config": {"api_key": SECRET_MARKER_PREFIX + "tampered-payload",
                           "region": "eu"},
            }
        },
        "jobs": [],
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    try:
        # The section supplies the replacement, so the provider is admitted.
        settings.set("examplehub", {"api_key": "unit-live-secret",  # pragma: allowlist secret
                                    "staged_only_token": "unit-staged-value"})
        assert "examplehub" in runtime_provider_configs()
        assert decrypt_blocked_provider_ids() == set()

        auth = get_providers.get_providers_auth()
        assert auth["examplehub"]["api_key"] == "unit-live-secret"
        # A field only the staged version declares is never overlaid: the
        # active code does not read it.
        assert "staged_only_token" not in auth["examplehub"]
        assert "tampered" not in json.dumps(auth)
    finally:
        remove_settings_section("examplehub")
        service._BLOCK_CACHE["key"] = None


def test_stage_install_local_persists_plugin_secret_paths(tmp_path, monkeypatch):
    """Installing a plugin registers its schema's secret paths in the live
    registry and in the persisted set, so the plugin's credentials are
    write-only from the moment it is configured."""
    from provider_hub.service import stage_install_local
    from provider_hub.state import load_state

    provider_content = b"class LocalProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(
        provider_id="locallyinstalled",
        name="Locally Installed",
        provider_content=provider_content,
        dependencies={"requirements": []},
    )
    package = _provider_zip(manifest, file_payloads, prefix="locallyinstalled/")
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    _patch_local_install_env(monkeypatch, tmp_path)

    from secret_store import registry

    saved_paths = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    try:
        stage_install_local(package)

        # The manifest's api_key is a declared secret, so it is registered in
        # the live registry and persisted with the installation.
        assert registry.is_plugin_secret("locallyinstalled.api_key")
        persisted_paths = load_state().get("plugin_secret_paths") or []
        assert "locallyinstalled.api_key" in persisted_paths
    finally:
        registry.reset_plugin_secret_paths(saved_paths)


def test_stage_install_local_repairs_a_malformed_persisted_path_set(
        tmp_path, monkeypatch, isolated_plugin_registry):
    """A persisted plugin_secret_paths that is not a list survives a boot
    that discovers nothing (the boot only rewrites the set when it has a
    path to add), so the next plugin installation is what meets the
    value. That installation reads the persisted set through the shared
    persisted-path validator rather than iterating the raw value: it does
    not break on the malformed shape, and its write repairs the set to
    the valid union of what it held and what it adds."""
    from provider_hub.service import stage_install_local
    from provider_hub.state import load_state
    from secret_store.registry import load_plugin_secret_paths

    provider_content = b"class LocalProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(
        provider_id="locallyinstalled",
        name="Locally Installed",
        provider_content=provider_content,
        dependencies={"requirements": []},
    )
    package = _provider_zip(manifest, file_payloads, prefix="locallyinstalled/")
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "installations": {}, "jobs": [], "plugin_secret_paths": 42,
    }), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    _patch_local_install_env(monkeypatch, tmp_path)

    # The boot discovers no paths, so the malformed value survives it.
    load_plugin_secret_paths(_EmptyScanSettings())
    assert load_state()["plugin_secret_paths"] == 42

    stage_install_local(package)

    # The credentialed installation neither broke on the value nor kept
    # it: the set is the valid union now.
    assert isolated_plugin_registry.is_plugin_secret("locallyinstalled.api_key")
    assert load_state()["plugin_secret_paths"] == ["locallyinstalled.api_key"]


def test_get_providers_auth_includes_active_provider_hub_config(tmp_path, monkeypatch):
    from app.get_providers import get_providers_auth
    from provider_hub.service import update_provider
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest_with_region(dependencies={"requirements": []}),
            "config": {},
        }
    }
    save_state(state)
    update_provider("examplehub", config={"api_key": "runtime-secret", "region": "eu"})  # pragma: allowlist secret

    assert get_providers_auth()["examplehub"]["api_key"] == "runtime-secret"  # pragma: allowlist secret
    assert get_providers_auth()["examplehub"]["region"] == "eu"


def test_provider_hub_connection_test_runs_worker_health(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    requests = []

    class FakeWorkerClient:
        def __init__(self, command, cwd=None, env=None):
            self.command = command
            self.cwd = cwd
            self.env = env

        def request(self, op, payload=None, timeout=30):
            requests.append((op, payload, timeout, self.cwd, self.env))
            return type("Result", (), {"payload": {"initialized": True}, "events": []})()

        def stop(self):
            requests.append(("stop", None, None, self.cwd, self.env))

    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    python_path = tmp_path / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", FakeWorkerClient)

    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "active_path": str(bundle_path),
            "python_path": str(python_path),
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(dependencies={"requirements": []}),
            "config": {"region": "eu"},
            "last_error": "old failure",
        }
    }
    save_state(state)

    from provider_hub.service import test_provider_connection

    result = test_provider_connection("examplehub")

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["message"] == "Worker health check passed"
    assert requests[0][0] == "health"
    assert requests[0][2] == 10
    assert str(requests[0][3]) == str(bundle_path)
    assert json.loads(requests[0][4]["BAZARR_PROVIDER_HUB_MANIFEST"])["provider_id"] == "examplehub"
    assert load_state()["installations"]["examplehub"]["last_error"] is None


def test_provider_hub_connection_test_ignores_runtime_pycache(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    class FakeWorkerClient:
        def __init__(self, command, cwd=None, env=None):
            self.command = command
            self.cwd = cwd
            self.env = env

        def request(self, op, payload=None, timeout=30):
            return type("Result", (), {"payload": {"initialized": True}, "events": []})()

        def stop(self):
            return None

    provider_content = b"class ExampleProvider: pass\n"
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    (bundle_path / "provider.py").write_bytes(provider_content)
    pycache_path = bundle_path / "__pycache__"
    pycache_path.mkdir()
    (pycache_path / "provider.cpython-314.pyc").write_bytes(b"runtime bytecode")

    python_path = tmp_path / "venv" / "bin" / "python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", FakeWorkerClient)

    state = load_state()
    state["installations"] = {
        "examplehub": {
            "provider_id": "examplehub",
            "name": "Example Hub Provider",
            "active_version": "1.0.0",
            "active_path": str(bundle_path),
            "python_path": str(python_path),
            "state": "active",
            "pending_restart": False,
            "manifest": _manifest(
                provider_content=provider_content,
                dependencies={"requirements": []},
            ),
            "config": {},
        }
    }
    save_state(state)

    from provider_hub.service import test_provider_connection

    result = test_provider_connection("examplehub")

    assert result["ok"] is True
    assert not pycache_path.exists()


def test_stage_install_failure_preserves_active_install_and_records_last_error(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from provider_hub.state import load_state

    provider_id = "stablehub"
    state_file = tmp_path / "provider_hub" / "state.json"
    old_installation = {
        "provider_id": provider_id,
        "name": "Stable Hub Provider",
        "active_version": "1.0.0",
        "staged_version": None,
        "active_path": "/old/bundle",
        "python_path": "/old/python",
        "staged_path": None,
        "staged_python_path": None,
        "state": "active",
        "pending_restart": False,
        "manifest": _manifest(provider_id=provider_id, name="Stable Hub Provider"),
        "last_error": None,
    }
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {provider_id: old_installation}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout):
        return _FakeResponse(content=b"tampered\n")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    with pytest.raises(ProviderHubInstallError, match="SHA256 mismatch"):
        stage_install(
            _manifest(
                provider_id=provider_id,
                name="Stable Hub Provider",
                version="1.1.0",
                dependencies={"requirements": []},
            )
        )

    installation = load_state()["installations"][provider_id]
    assert installation["state"] == "active"
    assert installation["active_version"] == "1.0.0"
    assert installation["active_path"] == "/old/bundle"
    assert installation["python_path"] == "/old/python"
    assert installation["staged_version"] is None
    assert installation["staged_path"] is None
    assert installation["staged_python_path"] is None
    assert "SHA256 mismatch" in installation["last_error"]


def test_custom_github_catalog_source_is_stored_as_untrusted(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_catalog

    state_file = tmp_path / "state.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    source = add_catalog_source(
        name="community",
        url="https://github.com/example/providers/blob/main/catalog.json",
        trusted=True,
    )

    assert source["trusted"] is False
    assert source["type"] == "github"
    assert source["url"] == "https://github.com/example/providers/blob/main/catalog.json"
    sources = {item["name"]: item for item in list_catalog()["sources"]}
    assert sources["community"]["trusted"] is False


def test_existing_state_normalizes_custom_source_trust(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "community": {
                        "id": "community",
                        "name": "community",
                        "type": "github",
                        "url": "https://github.com/example/providers/blob/main/catalog.json",
                        "enabled": True,
                        "trusted": True,
                    }
                },
                "catalog_entries": {
                    "community:examplehub:1.0.0": {
                        "source": "community",
                        "provider_id": "examplehub",
                        "version": "1.0.0",
                        "trusted": True,
                        "manifest": _manifest(),
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    catalog = list_catalog()
    sources = {item["id"]: item for item in catalog["sources"]}

    assert sources["community"]["trusted"] is False
    assert catalog["entries"][0]["trusted"] is False


def test_official_catalog_source_is_reseeded_when_missing(tmp_path, monkeypatch):
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID
    from provider_hub.service import list_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    sources = {item["id"]: item for item in list_catalog()["sources"]}

    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["trusted"] is True


def test_official_catalog_source_cannot_be_overwritten_or_deleted(tmp_path, monkeypatch):
    from provider_hub.service import CatalogSourceError, add_catalog_source, remove_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    with pytest.raises(CatalogSourceError, match="reserved"):
        add_catalog_source(
            name="official",
            url="https://github.com/example/providers/blob/main/catalog.json",
            trusted=True,
        )

    with pytest.raises(CatalogSourceError, match="reserved"):
        add_catalog_source(
            name="Official Bazarr Provider Catalog",
            url="https://github.com/example/providers/blob/main/catalog.json",
            trusted=True,
        )

    assert remove_catalog_source("official") is False


def test_existing_state_does_not_trust_sources_spoofing_official_name(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID, load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["catalog_sources"]["spoof"] = {
        "id": "spoof",
        "name": "Official Bazarr Provider Catalog",
        "type": "github",
        "url": "https://github.com/example/providers/blob/main/catalog.json",
        "enabled": True,
        "trusted": True,
    }
    state["catalog_entries"]["spoof:examplehub:1.0.0"] = {
        "source": "spoof",
        "source_name": "Official Bazarr Provider Catalog",
        "provider_id": "examplehub",
        "version": "1.0.0",
        "trusted": True,
        "manifest": _manifest(),
    }
    state["catalog_entries"]["official:officialhub:1.0.0"] = {
        "source": OFFICIAL_CATALOG_SOURCE_ID,
        "source_name": "Official Bazarr Provider Catalog",
        "provider_id": "officialhub",
        "version": "1.0.0",
        "trusted": True,
        "manifest": _manifest(provider_id="officialhub"),
    }
    save_state(state)

    catalog = list_catalog()
    sources = {item["id"]: item for item in catalog["sources"]}
    entries = {item["provider_id"]: item for item in catalog["entries"]}

    assert sources["spoof"]["name"] == "spoof"
    assert sources["spoof"]["trusted"] is False
    assert entries["examplehub"]["trusted"] is False
    assert entries["officialhub"]["trusted"] is True


def test_empty_state_seeds_official_trusted_catalog_source(tmp_path, monkeypatch):
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID, OFFICIAL_CATALOG_URL
    from provider_hub.service import list_catalog

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    sources = {item["id"]: item for item in list_catalog()["sources"]}

    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["url"] == OFFICIAL_CATALOG_URL
    assert sources[OFFICIAL_CATALOG_SOURCE_ID]["trusted"] is True


def test_list_catalog_auto_refreshes_unchecked_sources(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        return _FakeResponse(
            {
                "providers": [
                    {
                        "manifest": _manifest(
                            provider_id="autocataloghub",
                            dependencies={"requirements": []},
                        )
                    }
                ]
            }
        )

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    catalog = list_catalog(auto_refresh=True)

    assert catalog["sources"][0]["last_checked_at"]
    assert catalog["sources"][0]["last_error"] is None
    assert catalog["entries"][0]["provider_id"] == "autocataloghub"
    assert catalog["entries"][0]["trusted"] is True


def test_custom_catalog_source_rejects_non_github_url(tmp_path, monkeypatch):
    from provider_hub.service import CatalogSourceError, add_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    with pytest.raises(CatalogSourceError):
        add_catalog_source(name="bad", url="https://example.com/catalog.json")


def test_catalog_refresh_fetches_github_catalog_entries(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_catalog, refresh_catalog

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append((url, timeout))
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [{"manifest": _manifest(provider_id="cataloghub")} ]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source("community", "https://github.com/example/providers/blob/main/catalog.json")
    result = refresh_catalog()

    assert result["sources"] == 2
    assert result["entries"] == 1
    catalog = list_catalog()
    assert catalog["entries"][0]["provider_id"] == "cataloghub"
    assert catalog["entries"][0]["trusted"] is False
    assert "https://github.com/example/providers/info/refs?service=git-upload-pack" in [
        call[0] for call in calls
    ]
    assert (
        "https://raw.githubusercontent.com/example/providers/" + ("d" * 40) + "/catalog.json",
        30,
    ) in calls
    assert catalog["entries"][0]["manifest"]["source"]["commit"] == "d" * 40


def test_catalog_refresh_prunes_stale_entries_for_successful_source(tmp_path, monkeypatch):
    from provider_hub.service import list_catalog, refresh_catalog

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [{"manifest": _manifest(provider_id="freshhub")} ]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "community": {
                        "id": "community",
                        "name": "community",
                        "type": "github",
                        "url": "https://github.com/example/providers/blob/main/catalog.json",
                        "enabled": True,
                        "trusted": False,
                    }
                },
                "catalog_entries": {
                    "community:stalehub:1.0.0": {
                        "source": "community",
                        "provider_id": "stalehub",
                        "version": "1.0.0",
                        "trusted": False,
                        "manifest": _manifest(provider_id="stalehub"),
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    refresh_catalog()

    provider_ids = {entry["provider_id"] for entry in list_catalog()["entries"]}
    assert "freshhub" in provider_ids
    assert "stalehub" not in provider_ids


def test_stage_install_fetches_bundle_builds_env_and_records_staged_paths(tmp_path, monkeypatch):
    from provider_hub.service import stage_install
    from provider_hub.state import load_state

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    manifest = _manifest(
        files={"provider.py": _sha256(provider_content)},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    state_file = tmp_path / "provider_hub" / "state.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse(content=provider_content)

    env_calls = []

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_calls.append((self.root, validated.provider_id, validated.version))
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    installation = stage_install(manifest)

    bundle_path = tmp_path / "provider_hub" / "bundles" / "examplehub" / "1.0.0" / commit
    python_path = tmp_path / "provider_hub" / "envs" / "examplehub" / "1.0.0" / "test"
    python_path = python_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert calls == [
        (
            "https://raw.githubusercontent.com/owner/repo/" + commit + "/provider.py",
            30,
        )
    ]
    assert env_calls == [(tmp_path / "provider_hub", "examplehub", "1.0.0")]
    assert (bundle_path / "provider.py").read_bytes() == provider_content
    assert json.loads((bundle_path / "provider.json").read_text(encoding="utf-8"))["provider_id"] == "examplehub"
    assert installation["staged_path"] == str(bundle_path)
    assert installation["staged_python_path"] == str(python_path)
    assert installation["active_path"] is None
    assert installation["python_path"] is None
    assert installation["trusted"] is False

    stored = load_state()["installations"]["examplehub"]
    assert stored["staged_path"] == str(bundle_path)
    assert stored["staged_python_path"] == str(python_path)


def test_stage_install_fetches_bundle_from_manifest_source_path(tmp_path, monkeypatch):
    from provider_hub.service import stage_install

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "path": "providers/smoke",
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    stage_install(manifest)

    assert calls == [
        (
            "https://raw.githubusercontent.com/owner/repo/" + commit + "/providers/smoke/provider.py",
            30,
        )
    ]


def test_stage_install_trust_comes_from_catalog_entry_only(tmp_path, monkeypatch):
    from provider_hub.service import stage_install
    from provider_hub.state import official_catalog_source

    provider_content = b"class ExampleProvider: pass\n"
    commit = "e" * 40
    catalog_manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    install_manifest = json.loads(json.dumps(catalog_manifest))
    install_manifest["source"].pop("trusted")
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {"official": official_catalog_source()},
                "catalog_entries": {
                    "official:examplehub:1.0.0": {
                        "source": "official",
                        "provider_id": "examplehub",
                        "version": "1.0.0",
                        "trusted": True,
                        "manifest": catalog_manifest,
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)

    installation = stage_install(install_manifest)

    assert installation["trusted"] is True

    tampered_manifest = json.loads(json.dumps(install_manifest))
    tampered_manifest["entry_class"] = "DifferentProvider"

    installation = stage_install(tampered_manifest)

    assert installation["trusted"] is False


def test_stage_install_rejects_untrusted_built_in_shadow(tmp_path, monkeypatch):
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.service import stage_install

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    monkeypatch.setattr("provider_hub.service._built_in_provider_ids", lambda: {"gestdown"})

    with pytest.raises(ManifestValidationError, match="built-in"):
        stage_install(
            _manifest(
                provider_id="gestdown",
                name="Gestdown",
                dependencies={"requirements": []},
            )
        )


def test_stage_install_accepts_trusted_migrated_built_in_shadow(tmp_path, monkeypatch):
    from provider_hub.service import stage_install
    from provider_hub.state import load_state, official_catalog_source

    provider_content = b"class GestdownProvider: pass\n"
    catalog_manifest = _manifest(
        provider_id="gestdown",
        name="Gestdown",
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "e" * 40,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    install_manifest = json.loads(json.dumps(catalog_manifest))
    install_manifest["source"].pop("trusted")

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {"official": official_catalog_source()},
                "catalog_entries": {
                    "official:gestdown:1.0.0": {
                        "source": "official",
                        "provider_id": "gestdown",
                        "version": "1.0.0",
                        "trusted": True,
                        "manifest": catalog_manifest,
                    }
                },
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service._built_in_provider_ids", lambda: {"gestdown"})
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            return self.root / "envs" / validated.provider_id / validated.version / "test"

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    monkeypatch.setattr("provider_hub.service._set_bazarr_provider_enabled", lambda provider_id, enabled: True)

    installation = stage_install(install_manifest)

    assert installation["provider_id"] == "gestdown"
    assert installation["trusted"] is True
    assert installation["state"] == "staged"
    assert installation["pending_restart"] is True
    assert installation["staged_version"] == "1.0.0"
    stored = load_state()["installations"]["gestdown"]
    assert stored["trusted"] is True
    assert stored["staged_manifest"] == install_manifest


def test_stage_install_smoke_failure_records_failed_install(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from provider_hub.state import load_state

    provider_content = b"class ExampleProvider: pass\n"
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: (_ for _ in ()).throw(RuntimeError("worker broke")),
    )

    with pytest.raises(ProviderHubInstallError, match="worker broke"):
        stage_install(manifest)

    installation = load_state()["installations"]["examplehub"]
    assert installation["state"] == "failed"
    assert installation["pending_restart"] is False
    assert "worker broke" in installation["last_error"]


def test_bundle_tree_verification_rejects_symlink_and_hash_mismatch(tmp_path):
    from provider_hub.bundle import BundleValidationError, verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    manifest = validate_manifest(_manifest(), built_in_provider_ids=set())
    provider_file = tmp_path / "provider.py"
    provider_file.write_bytes(b"class ExampleProvider: pass\n")

    verify_bundle_tree(manifest, tmp_path)

    provider_file.write_bytes(b"tampered\n")
    with pytest.raises(BundleValidationError, match="SHA256"):
        verify_bundle_tree(manifest, tmp_path)

    provider_file.write_bytes(b"class ExampleProvider: pass\n")
    provider_file.unlink()
    provider_file.symlink_to(tmp_path / "target.py")
    with pytest.raises(BundleValidationError, match="symlink"):
        verify_bundle_tree(manifest, tmp_path)


def test_bundle_tree_verification_rejects_bundle_hash_mismatch(tmp_path):
    from provider_hub.bundle import BundleValidationError, verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    provider_file = tmp_path / "provider.py"
    provider_file.write_bytes(b"class ExampleProvider: pass\n")
    manifest = validate_manifest(_manifest(bundle_sha256="a" * 64), built_in_provider_ids=set())

    with pytest.raises(BundleValidationError, match="bundle SHA256"):
        verify_bundle_tree(manifest, tmp_path)


def test_hub_proxy_provider_search_and_download_uses_worker_payload(monkeypatch):
    from provider_hub import registry as registry_module
    from provider_hub.manifest import validate_manifest
    from provider_hub.registry import _make_provider_class
    from provider_hub.protocol import language_to_payload

    # Pin the global default so the timeout assertion below is a concrete value
    # (an explicit timeout=9 is below the 120s floor -> raised to 120).
    monkeypatch.setattr(registry_module, "_global_worker_timeout", lambda: 120.0)

    class FakeWorker:
        def __init__(self):
            self.requests = []

        def request(self, op, payload, timeout):
            self.requests.append((op, payload, timeout))
            if op == "search":
                return type(
                    "Result",
                    (),
                    {
                        "payload": {
                            "candidates": [
                                {
                                    "provider": "fake",
                                    "id": "sub-1",
                                    "language": language_to_payload(Language("eng")),
                                    "release_info": "Example.Movie.2024.1080p-GROUP",
                                    "filename": "example.srt",
                                    "matches": ["title", "year"],
                                    "provider_payload": {
                                        "provider": "fake",
                                        "schema": 1,
                                        "data": {"file_id": "sub-1"},
                                    },
                                }
                            ]
                        },
                        "events": [],
                    },
                )()
            content = b"hello"
            return type(
                "Result",
                (),
                {
                    "payload": {
                        "content_b64": base64.b64encode(content).decode("ascii"),
                        "content_sha256": _sha256(content),
                        "empty": False,
                    },
                    "events": [],
                },
            )()

        def stop(self):
            return None

    worker = FakeWorker()
    manifest = validate_manifest(_manifest(provider_id="proxyhub"), built_in_provider_ids=set())
    provider_cls = _make_provider_class(manifest, worker_client=worker)
    provider_config = {"profile_name": "smoke-profile", "api_token": "secret-token"}
    provider = provider_cls(timeout=9, **provider_config)
    movie = Movie("/media/example.mkv", "Example Movie", year=2024)

    subtitles = provider.list_subtitles(movie, {Language("eng")})
    assert len(subtitles) == 1
    assert subtitles[0].provider_name == "proxyhub"

    provider.download_subtitle(subtitles[0])
    assert subtitles[0].content == b"hello"
    assert worker.requests[0][0] == "search"
    # The worker request carries the host-side deadline from _request_timeout();
    # an explicit timeout (9) below the global default floor is raised to 120.
    assert worker.requests[0][2] == 120.0
    assert worker.requests[0][1]["config"] == provider_config
    assert worker.requests[1][0] == "download"
    assert worker.requests[1][1]["config"] == provider_config


def test_hub_proxy_uses_configured_worker_timeout_for_long_downloads():
    from provider_hub.manifest import validate_manifest
    from provider_hub.registry import _make_provider_class
    from provider_hub.protocol import language_to_payload

    class FakeWorker:
        def __init__(self):
            self.requests = []

        def request(self, op, payload, timeout):
            self.requests.append((op, timeout))
            if op == "search":
                return type(
                    "Result",
                    (),
                    {
                        "payload": {
                            "candidates": [
                                {
                                    "provider": "embeddedsubtitles",
                                    "id": "track-10",
                                    "language": language_to_payload(Language("eng")),
                                    "release_info": "English SRT Completos",
                                    "matches": ["hash"],
                                    "provider_payload": {
                                        "provider": "embeddedsubtitles",
                                        "schema": 1,
                                        "path": "/media/movie.mkv",
                                        "stream_index": 10,
                                        "format": "srt",
                                    },
                                }
                            ]
                        },
                        "events": [],
                    },
                )()
            return type(
                "Result",
                (),
                {
                    "payload": {
                        "content_b64": base64.b64encode(b"hello").decode("ascii"),
                        "content_sha256": _sha256(b"hello"),
                        "empty": False,
                    },
                    "events": [],
                },
            )()

        def stop(self):
            return None

    worker = FakeWorker()
    manifest = validate_manifest(_manifest(provider_id="embeddedsubtitles"), built_in_provider_ids=set())
    provider = _make_provider_class(manifest, worker_client=worker)(
        timeout=30,
        timeout_seconds=600,
    )
    movie = Movie("/media/movie.mkv", "Example Movie", year=2026)
    subtitle = provider.list_subtitles(movie, {Language("eng")})[0]

    provider.download_subtitle(subtitle)

    # 600s configured + 30s host margin so the worker's own timeout fires first.
    assert provider._request_timeout() == 630.0
    assert worker.requests[0] == ("search", 630.0)
    assert worker.requests[1] == ("download", 630.0)


def test_hub_proxy_download_invokes_select_archive_member():
    import io
    import zipfile

    from provider_hub.manifest import validate_manifest
    from provider_hub.registry import _make_provider_class
    from provider_hub.protocol import language_to_payload

    def _zip(names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name in names:
                archive.writestr(name, b"1\n00:00:01,000 --> 00:00:02,000\nx\n")
        return buf.getvalue()

    body = _zip(["show.eng.srt", "show.fre.srt"])

    class FakeWorker:
        def __init__(self):
            self.select_calls = []

        def request(self, op, payload, timeout):
            if op == "search":
                return type("R", (), {"payload": {"candidates": [{
                    "provider": "selecthub", "id": "sub-1",
                    "language": language_to_payload(Language("eng")),
                    "release_info": "Example.Movie.2024-GROUP", "filename": "x.srt",
                    "matches": ["title"],
                    "provider_payload": {"provider": "selecthub", "schema": 1},
                }]}, "events": []})()
            return type("R", (), {"payload": {
                "archive_b64": base64.b64encode(body).decode("ascii"),
                "archive_sha256": _sha256(body),
                "select_member": True,
            }, "events": []})()

        def select_archive_member(self, payload, timeout=None):
            self.select_calls.append(payload)
            return type("R", (), {"payload": {"member": "show.eng.srt", "decision": "pin"}, "events": []})()

        def stop(self):
            return None

    worker = FakeWorker()
    manifest = validate_manifest(_manifest(provider_id="selecthub"), built_in_provider_ids=set())
    provider = _make_provider_class(manifest, worker_client=worker)(
        timeout=9, profile_name="smoke-profile", api_token="secret-token"
    )
    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    subtitle = provider.list_subtitles(movie, {Language("eng")})[0]

    provider.download_subtitle(subtitle)

    assert worker.select_calls, "download must call select_archive_member for select_member payloads"
    assert sorted(worker.select_calls[0]["members"]) == ["show.eng.srt", "show.fre.srt"]
    assert subtitle.content is not None and b"00:00:01" in subtitle.content


def test_worker_runner_executes_simple_bundle(tmp_path):
    import sys

    from provider_hub.worker import ProviderWorkerClient, worker_command

    provider_file = tmp_path / "provider.py"
    provider_file.write_text(
        """
import base64


class ExampleProvider:
    def search(self, video, languages, config):
        if config.get("profile_name") != "smoke-profile" or config.get("api_token") != "secret-token":
            raise ValueError("missing smoke config")
        return [{
            "provider": "example",
            "id": "sub-1",
            "language": languages[0],
            "release_info": video.get("title"),
            "matches": ["title"],
            "provider_payload": {
                "provider": "example",
                "schema": 1,
                "profile_name": config["profile_name"],
                "data": {"id": "sub-1"},
            },
        }]

    def download(self, provider_payload, language, config):
        if config.get("profile_name") != "smoke-profile" or config.get("api_token") != "secret-token":
            raise ValueError("missing smoke config")
        if provider_payload.get("profile_name") != config["profile_name"]:
            raise ValueError("profile mismatch")
        content = b"hello from worker"
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "content_sha256": "94bbc6037685e2186909083aa02abe58fbec222f6e2d73bb3e9e59d5b24a3d25",  # pragma: allowlist secret
            "empty": False,
        }
""",
        encoding="utf-8",
    )
    manifest = _manifest(
        files={"provider.py": _sha256(provider_file.read_bytes())},
        dependencies={"requirements": []},
    )
    runner = Path(__file__).parents[2] / "bazarr" / "provider_hub" / "worker_runner.py"
    client = ProviderWorkerClient(
        worker_command(sys.executable, runner),
        cwd=tmp_path,
        env={
            "BAZARR_PROVIDER_HUB_BUNDLE": str(tmp_path),
            "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(manifest),
        },
    )

    try:
        search = client.request(
            "search",
            {
                "video": {"title": "Example Movie"},
                "languages": [{"alpha3": "eng", "hi": False, "forced": False}],
                "config": {"profile_name": "smoke-profile", "api_token": "secret-token"},
            },
            timeout=3,
        )
        assert search.payload["candidates"][0]["id"] == "sub-1"

        download = client.request(
            "download",
            {
                "provider_payload": search.payload["candidates"][0]["provider_payload"],
                "language": {"alpha3": "eng"},
                "config": {"profile_name": "smoke-profile", "api_token": "secret-token"},
            },
            timeout=3,
        )
        assert download.payload["empty"] is False
    finally:
        client.stop()


def test_worker_client_times_out_when_worker_stops_writing_stdout(tmp_path):
    import sys

    from provider_hub.worker import ProviderWorkerClient, WorkerError

    worker_file = tmp_path / "silent_worker.py"
    worker_file.write_text(
        "import time\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    client = ProviderWorkerClient([sys.executable, "-I", "-B", str(worker_file)])

    try:
        with pytest.raises(WorkerError, match="deadline"):
            client.request("health", timeout=0.1)
        assert client.process is not None
        assert client.process.poll() is not None
    finally:
        client.stop()


def test_worker_command_disables_bytecode_in_isolated_mode(tmp_path):
    from provider_hub.worker import worker_command

    runner = tmp_path / "runner.py"

    assert worker_command("/venv/bin/python", runner) == [
        "/venv/bin/python",
        "-I",
        "-B",
        str(runner),
    ]


def test_add_catalog_source_accepts_optional_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    source = add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/foo",
    )
    assert source["dev_ref"] == "feat/foo"


def test_add_catalog_source_rejects_invalid_dev_ref(tmp_path, monkeypatch):
    import pytest
    from provider_hub.service import CatalogSourceError, add_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    with pytest.raises(CatalogSourceError):
        add_catalog_source(
            "bad",
            "https://github.com/example/providers/blob/main/catalog.json",
            dev_ref="this is not a ref",
        )


def test_update_catalog_source_sets_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, update_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    updated = update_catalog_source("community", dev_ref="feat/test")
    assert updated["dev_ref"] == "feat/test"
    cleared = update_catalog_source("community", dev_ref=None)
    assert cleared["dev_ref"] is None


def test_update_catalog_source_rejects_invalid_dev_ref(tmp_path, monkeypatch):
    import pytest
    from provider_hub.service import (
        CatalogSourceError,
        add_catalog_source,
        update_catalog_source,
    )

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    with pytest.raises(CatalogSourceError):
        update_catalog_source("community", dev_ref="bad ref with space")


def test_update_catalog_source_returns_none_for_unknown(tmp_path, monkeypatch):
    from provider_hub.service import update_catalog_source

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    assert update_catalog_source("missing", dev_ref="feat/x") is None


def test_refresh_catalog_uses_dev_ref_when_set(tmp_path, monkeypatch):
    from provider_hub.service import (
        add_catalog_source,
        refresh_catalog,
        update_catalog_source,
    )

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([
                ("a" * 40, "refs/heads/main"),
                ("f" * 40, "refs/heads/feat/test-branch"),
            ]))
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )
    update_catalog_source("community", dev_ref="feat/test-branch")
    refresh_catalog()

    from provider_hub.state import load_state

    assert load_state()["catalog_sources"]["community"]["resolved_commit"] == "f" * 40, (
        f"calls were: {calls}"
    )


def test_refresh_catalog_reports_network_errors_for_users(tmp_path, monkeypatch):
    import requests
    from provider_hub.service import add_catalog_source, refresh_catalog
    from provider_hub.state import load_state

    def fake_get(url, timeout, **_kwargs):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.github.com', port=443): "
            "Max retries exceeded with url: /repos/example/providers/commits/main"
        )

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )

    refresh_catalog()

    source = load_state()["catalog_sources"]["community"]
    assert source["last_error"] == (
        "Could not reach GitHub while refreshing this catalog source. "
        "Check network or DNS and try again."
    )
    assert "HTTPSConnectionPool" not in source["last_error"]


def test_patch_catalog_source_updates_dev_ref(tmp_path, monkeypatch):
    from provider_hub.service import (
        add_catalog_source,
        get_catalog_source,
        refresh_catalog,
        update_catalog_source,
    )

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([
                ("9" * 40, "refs/heads/main"),
                ("9" * 40, "refs/heads/feat/branch"),
            ]))
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
    )

    # Mirrors the PATCH endpoint logic: validate + update, then refresh.
    update_catalog_source("community", dev_ref="feat/branch")
    refresh_catalog()

    source = get_catalog_source("community")
    assert source is not None
    assert source["dev_ref"] == "feat/branch"
    assert source["last_checked_at"] is not None


def _empty_state_file(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
        encoding="utf-8",
    )
    return state_file


def test_record_job_writes_lifecycle_pending_running_completed(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    observed = []
    with record_job(
        "stage_update",
        target_kind="provider",
        target_id="examplehub",
        target_name="Example",
        from_version="1.0.0",
        to_version="1.1.0",
    ) as job:
        observed.append(list_jobs()[-1]["state"])
        job.update(message="staged")

    jobs = list_jobs()
    assert len(jobs) == 1
    last = jobs[-1]
    assert observed == ["running"]
    assert last["state"] == "completed"
    assert last["action"] == "stage_update"
    assert last["target_id"] == "examplehub"
    assert last["from_version"] == "1.0.0"
    assert last["to_version"] == "1.1.0"
    assert last["message"] == "staged"
    assert last["duration_ms"] is not None
    assert last["started_at"] is not None
    assert last["completed_at"] is not None
    assert last["error"] is None


def test_record_job_marks_failed_with_exception(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    with pytest.raises(ValueError):
        with record_job("install", target_kind="provider", target_id="examplehub") as job:
            job.update(message="kicking off")
            raise ValueError("boom")

    last = list_jobs()[-1]
    assert last["state"] == "failed"
    assert last["error"] == "boom"
    assert last["duration_ms"] is not None
    assert last["message"] == "kicking off"


def test_record_job_trims_history_to_limit(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.service import list_jobs, record_job

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    monkeypatch.setattr(service, "_JOB_LOG_LIMIT", 3)

    for index in range(5):
        with record_job("check_updates", target_kind="system") as job:
            job.update(message=f"check {index}")

    jobs = list_jobs()
    assert len(jobs) == 3
    assert [job["message"] for job in jobs] == ["check 2", "check 3", "check 4"]


def test_check_updates_emits_lifecycle_job(tmp_path, monkeypatch):
    from provider_hub.service import check_updates, list_jobs

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    job = check_updates()

    assert job["state"] == "completed"
    assert job["action"] == "check_updates"
    assert job["duration_ms"] is not None
    assert "No new updates available" in job["message"]
    assert job["id"] == list_jobs()[-1]["id"]


def test_check_updates_reports_available_updates(tmp_path, monkeypatch):
    from provider_hub.service import check_updates
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "source_bound": True,
        "source_id": "official",
        "manifest": _manifest(),
    }
    state["catalog_entries"]["official:examplehub:1.1.0"] = {
        "provider_id": "examplehub",
        "version": "1.1.0",
        "source": "official",
        "manifest": _manifest(version="1.1.0"),
    }
    save_state(state)

    job = check_updates()

    assert job["state"] == "completed"
    assert job["details"]["updates_available"] == 1
    assert "1.0.0 -> 1.1.0" in job["message"]


def _official_entries(*versions):
    return {
        f"official:examplehub:{version}": {
            "source": "official",
            "provider_id": "examplehub",
            "version": version,
            "manifest": _manifest(version=version),
        }
        for version in versions
    }


def test_latest_catalog_manifest_respects_prerelease_precedence():
    from provider_hub.service import _latest_catalog_manifest

    state = {"catalog_entries": _official_entries("1.0.0-alpha", "1.0.0-rc.1")}

    assert _latest_catalog_manifest(state, "examplehub", "1.0.0", "official") is None

    state["catalog_entries"].update(_official_entries("1.0.0"))

    latest = _latest_catalog_manifest(state, "examplehub", "1.0.0-rc.1", "official")

    assert latest["version"] == "1.0.0"


def test_latest_catalog_manifest_ignores_build_metadata():
    from provider_hub.service import _latest_catalog_manifest

    state = {"catalog_entries": _official_entries("1.2.3+build.5")}

    assert _latest_catalog_manifest(state, "examplehub", "1.2.3", "official") is None


def test_add_catalog_source_records_activity_job(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_jobs

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/foo",
    )

    job = list_jobs()[-1]
    assert job["action"] == "add_source"
    assert job["state"] == "completed"
    assert job["target_kind"] == "source"
    assert job["target_id"] == "community"
    assert job["details"]["dev_ref"] == "feat/foo"
    assert "Added catalog source" in job["message"]


def test_update_catalog_source_records_version_diff(tmp_path, monkeypatch):
    from provider_hub.service import add_catalog_source, list_jobs, update_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    add_catalog_source(
        "community",
        "https://github.com/example/providers/blob/main/catalog.json",
        dev_ref="feat/old",
    )
    update_catalog_source("community", dev_ref="feat/new")

    job = list_jobs()[-1]
    assert job["action"] == "update_source"
    assert job["state"] == "completed"
    assert job["from_version"] == "feat/old"
    assert job["to_version"] == "feat/new"


def test_remove_catalog_source_records_failed_when_missing(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, remove_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    assert remove_catalog_source("ghost") is False
    job = list_jobs()[-1]
    assert job["action"] == "remove_source"
    assert job["state"] == "completed"
    assert "not found" in job["message"]


def test_refresh_catalog_records_summary(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, refresh_catalog

    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "catalog_sources": {
                    "official": {
                        "id": "official",
                        "name": "Official Bazarr Provider Catalog",
                        "type": "github",
                        "url": "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
                        "enabled": True,
                        "official": True,
                        "trusted": True,
                        "dev_ref": None,
                        "last_checked_at": None,
                        "last_error": None,
                    },
                },
                "catalog_entries": {},
                "installations": {},
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("f" * 40, "refs/heads/main")]))
        return _FakeResponse({"providers": []})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    refresh_catalog()
    job = list_jobs()[-1]
    assert job["action"] == "refresh_catalog"
    assert job["state"] == "completed"
    assert job["details"]["sources_total"] == 1
    assert job["details"]["sources_ok"] == 1
    assert "Refreshed 1 source(s)" in job["message"]


def test_remove_installation_records_uninstall_job(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, remove_installation
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
    }
    save_state(state)

    assert remove_installation("examplehub") is True
    job = list_jobs()[-1]
    assert job["action"] == "uninstall"
    assert job["state"] == "completed"
    assert job["target_id"] == "examplehub"
    assert job["from_version"] == "1.0.0"
    assert "Staged removal" in job["message"]


def test_stage_install_records_install_job_on_success(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, stage_install

    provider_content = b"class ExampleProvider: pass\n"
    commit = "a" * 40
    manifest = _manifest(
        files={"provider.py": _sha256(provider_content)},
        dependencies={"requirements": []},
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": commit,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: None,
    )

    stage_install(manifest)

    job = list_jobs()[-1]
    assert job["action"] == "install"
    assert job["state"] == "completed"
    assert job["target_id"] == "examplehub"
    assert job["to_version"] == "1.0.0"
    assert job["from_version"] is None
    assert "Staged install" in job["message"]


def test_stage_install_records_failed_job_on_smoke_error(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, list_jobs, stage_install

    provider_content = b"class ExampleProvider: pass\n"
    manifest = _manifest(
        file_payloads={"provider.py": provider_content},
        dependencies={"requirements": []},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))

    def fake_get(url, timeout):
        return _FakeResponse(content=provider_content)

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        "provider_hub.service._smoke_validate_worker",
        lambda manifest, bundle_path, python_path: (_ for _ in ()).throw(RuntimeError("worker broke")),
    )

    with pytest.raises(ProviderHubInstallError):
        stage_install(manifest)

    job = list_jobs()[-1]
    assert job["action"] == "install"
    assert job["state"] == "failed"
    assert "worker broke" in job["error"]


def test_list_providers_overlays_enabled_from_bazarr_enabled_providers(
    tmp_path, monkeypatch
):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": False,  # stale stored value
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    monkeypatch.setattr(
        service, "_bazarr_enabled_providers", lambda: ["examplehub", "other"]
    )

    providers = service.list_providers()
    assert any(
        p["provider_id"] == "examplehub" and p["enabled"] is True for p in providers
    )

    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["other"])
    providers = service.list_providers()
    assert any(
        p["provider_id"] == "examplehub" and p["enabled"] is False for p in providers
    )


def test_update_provider_syncs_enabled_providers(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": True,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    calls = []
    monkeypatch.setattr(
        service,
        "_set_bazarr_provider_enabled",
        lambda pid, enabled: calls.append((pid, enabled)) or True,
    )

    service.update_provider("examplehub", enabled=False)
    service.update_provider("examplehub", enabled=True)
    assert calls == [("examplehub", False), ("examplehub", True)]


def test_remove_installation_clears_enabled_providers(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": True,
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)

    calls = []
    monkeypatch.setattr(
        service,
        "_set_bazarr_provider_enabled",
        lambda pid, enabled: calls.append((pid, enabled)) or True,
    )

    service.remove_installation("examplehub")
    assert calls == [("examplehub", False)]


def test_manifest_rejects_unsafe_version_path_components():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    for bad in ("../escape", "/abs", "a/b", "a\\b", "..", "."):
        with pytest.raises(ManifestValidationError):
            validate_manifest(_manifest(version=bad), built_in_provider_ids=set())


def test_runtime_provider_configs_includes_pending_restart_with_active_version(
    tmp_path, monkeypatch
):
    from provider_hub import service
    from provider_hub.state import load_state, save_state
    from secret_store import registry

    # The manifest declares no schema at all, so nothing in this config is a
    # secret: keep earlier tests' registrations out, or this fixture's
    # api_key would be judged as a retained field the schema does not
    # declare and excluded from the worker config.
    saved_paths = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    try:
        monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
        state = load_state()
        state["installations"]["examplehub"] = {
            "provider_id": "examplehub",
            "name": "Example",
            "active_version": "1.0.0",
            "state": "staged",
            "pending_restart": True,
            "config": {"api_key": "abc"},
            "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
        }
        state["installations"]["nokey"] = {
            "provider_id": "nokey",
            "active_version": None,
            "state": "staged",
            "pending_restart": True,
            "config": {},
            "manifest": {"provider_id": "nokey", "version": "1.0.0"},
        }
        save_state(state)

        configs = service.runtime_provider_configs()
        assert "examplehub" in configs
        assert configs["examplehub"].get("api_key") == "abc"
        assert "nokey" not in configs
    finally:
        registry.reset_plugin_secret_paths(saved_paths)


def test_remove_catalog_source_purges_catalog_entries(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["catalog_sources"]["community"] = {
        "id": "community",
        "name": "community",
        "type": "github",
        "url": "https://github.com/example/providers/blob/main/catalog.json",
        "enabled": True,
        "trusted": False,
    }
    state["catalog_entries"]["community:foo:1.0.0"] = {
        "source": "community",
        "source_name": "community",
        "provider_id": "foo",
        "version": "1.0.0",
        "trusted": False,
        "manifest": {"provider_id": "foo", "version": "1.0.0"},
    }
    state["catalog_entries"]["official:bar:1.0.0"] = {
        "source": "official",
        "source_name": "Official Bazarr Provider Catalog",
        "provider_id": "bar",
        "version": "1.0.0",
        "trusted": True,
        "manifest": {"provider_id": "bar", "version": "1.0.0"},
    }
    save_state(state)

    assert service.remove_catalog_source("community") is True
    remaining = load_state()["catalog_entries"]
    assert "community:foo:1.0.0" not in remaining
    assert "official:bar:1.0.0" in remaining


def test_stage_install_local_records_origin_local_and_untrusted(tmp_path, monkeypatch):
    from provider_hub.service import stage_install_local
    from provider_hub.state import load_state

    provider_content = b"class LocalProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(
        provider_id="locallyinstalled",
        name="Locally Installed",
        provider_content=provider_content,
        dependencies={"requirements": []},
        # Even with a catalog_url, an uploaded package is always local + untrusted.
        source={
            "type": "github",
            "repo": "owner/repo",
            "ref": "main",
            "commit": "b" * 40,
            "catalog_url": "https://github.com/owner/repo/blob/main/catalog.json",
            "trusted": True,
        },
    )
    package = _provider_zip(manifest, file_payloads, prefix="locallyinstalled/")

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    _patch_local_install_env(monkeypatch, tmp_path)

    installation = stage_install_local(package)

    assert installation["provider_id"] == "locallyinstalled"
    assert installation["origin"] == "local"
    assert installation["source_id"] is None
    assert installation["trusted"] is False
    assert installation["state"] == "staged"

    stored = load_state()["installations"]["locallyinstalled"]
    assert stored["origin"] == "local"
    assert stored["trusted"] is False
    # A local install is authoritative: even a catalog_url that matches a source
    # must not relabel it as a catalog provider on read.
    from provider_hub.service import list_providers
    listed = {p["provider_id"]: p for p in list_providers()}
    assert listed["locallyinstalled"]["origin"] == "local"


def test_stage_install_local_rejects_built_in_shadow(tmp_path, monkeypatch):
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.service import stage_install_local

    provider_content = b"class GestdownProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(
        provider_id="gestdown",
        name="Gestdown",
        provider_content=provider_content,
        dependencies={"requirements": []},
    )
    package = _provider_zip(manifest, file_payloads)

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr("provider_hub.service._built_in_provider_ids", lambda: {"gestdown"})
    _patch_local_install_env(monkeypatch, tmp_path)

    # Untrusted local packages can never shadow a built-in, even an allowlisted one.
    with pytest.raises(ManifestValidationError, match="built-in"):
        stage_install_local(package)


def test_stage_install_local_rejects_zip_slip(tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install_local

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.py", b"print('pwned')\n")

    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    with pytest.raises(ProviderHubInstallError, match="unsafe path"):
        stage_install_local(buffer.getvalue())


def test_stage_install_local_creates_hub_dir_when_missing(tmp_path, monkeypatch):
    # A local upload can be the first Provider Hub write, before the hub dir exists;
    # it must create the directory instead of failing with FileNotFoundError.
    from provider_hub.service import stage_install_local

    provider_content = b"class LocalProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(
        provider_id="firstlocal",
        name="First Local",
        provider_content=provider_content,
        dependencies={"requirements": []},
    )
    package = _provider_zip(manifest, file_payloads)

    # Point the state at a provider_hub dir that does not exist yet.
    state_file = tmp_path / "provider_hub" / "state.json"
    assert not state_file.parent.exists()
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    _patch_local_install_env(monkeypatch, tmp_path)

    installation = stage_install_local(package)

    assert installation["provider_id"] == "firstlocal"
    assert installation["origin"] == "local"
    assert state_file.parent.exists()


def _local_install_state_file(tmp_path):
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "catalog_entries": {
                    "official:locallyowned:2.0.0": {
                        "source": "official",
                        "provider_id": "locallyowned",
                        "version": "2.0.0",
                        "trusted": True,
                        "manifest": _manifest(
                            provider_id="locallyowned",
                            version="2.0.0",
                            dependencies={"requirements": []},
                        ),
                    }
                },
                "installations": {
                    "locallyowned": {
                        "provider_id": "locallyowned",
                        "name": "Locally Owned",
                        "active_version": "1.0.0",
                        "state": "active",
                        "pending_restart": False,
                        "origin": "local",
                        "trusted": False,
                        "manifest": _manifest(
                            provider_id="locallyowned",
                            version="1.0.0",
                            dependencies={"requirements": []},
                        ),
                    }
                },
                "jobs": [],
            }
        ),
        encoding="utf-8",
    )
    return state_file


def test_check_updates_skips_local_installs(tmp_path, monkeypatch):
    # A local install with a higher catalog version must NOT be reported as an
    # available update; the catalog does not own local packages.
    from provider_hub.service import check_updates

    monkeypatch.setenv(
        "BAZARR_PROVIDER_HUB_STATE", str(_local_install_state_file(tmp_path))
    )

    result = check_updates()

    assert (result.get("details") or {}).get("updates_available") == 0


def test_apply_update_ignores_local_install(tmp_path, monkeypatch):
    # apply_update must refuse to replace a local package with a catalog version.
    from provider_hub.service import apply_update
    from provider_hub.state import load_state

    monkeypatch.setenv(
        "BAZARR_PROVIDER_HUB_STATE", str(_local_install_state_file(tmp_path))
    )

    provider = apply_update("locallyowned")

    assert provider["origin"] == "local"
    assert provider.get("staged_version") is None
    stored = load_state()["installations"]["locallyowned"]
    assert stored["active_version"] == "1.0.0"
    assert stored.get("staged_version") is None


def test_stage_install_local_rejects_oversized_package(tmp_path, monkeypatch):
    # An upload whose declared uncompressed size exceeds the bound is rejected as a
    # 400-class error before extraction, not allowed to fill the disk.
    import provider_hub.service as svc
    from provider_hub.service import ProviderHubInstallError, stage_install_local

    monkeypatch.setattr(svc, "_LOCAL_PACKAGE_MAX_TOTAL_BYTES", 10)
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    manifest = _manifest(provider_id="toobig", dependencies={"requirements": []})
    package = _provider_zip(manifest, {"provider.py": b"class P: pass\n"})

    with pytest.raises(ProviderHubInstallError, match="too large"):
        stage_install_local(package)


def _catalog_state(tmp_path, *, installations=None, entries=None):
    state = {
        "catalog_sources": {
            "official": {"id": "official", "name": "Official Bazarr Provider Catalog",
                         "type": "github", "url": "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
                         "enabled": True, "official": True, "trusted": True},
        },
        "catalog_entries": entries or {},
        "installations": installations or {},
        "jobs": [],
    }
    f = tmp_path / "state.json"
    f.write_text(json.dumps(state), encoding="utf-8")
    return f


def _official_entry(provider_id, version="1.0.0"):
    return {
        "source": "official",
        "provider_id": provider_id,
        "version": version,
        "trusted": True,
        "manifest": _manifest(provider_id=provider_id, version=version,
                              dependencies={"requirements": []}),
    }


def test_autoinstall_stages_enabled_official_providers_not_yet_installed(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = _catalog_state(
        tmp_path,
        installations={},  # nothing installed yet
        entries={
            "official:subdl:1.0.0": _official_entry("subdl"),
            "official:gestdown:1.0.0": _official_entry("gestdown"),
        },
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl", "opensubtitlescom"])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: staged.append(manifest["provider_id"]))

    result = service.autoinstall_enabled_builtins()

    # only "subdl" is enabled AND in the official catalog AND not installed.
    # "opensubtitlescom" is enabled but not in the catalog -> skipped.
    # "gestdown" is in the catalog but not enabled -> skipped.
    assert result == ["subdl"]
    assert staged == ["subdl"]


def test_autoinstall_is_idempotent_skips_existing_installs(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = _catalog_state(
        tmp_path,
        installations={"subdl": {"provider_id": "subdl", "state": "active",
                                 "active_version": "1.0.0", "manifest": _manifest(provider_id="subdl")}},
        entries={"official:subdl:1.0.0": _official_entry("subdl")},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: staged.append(manifest["provider_id"]))

    assert service.autoinstall_enabled_builtins() == []
    assert staged == []


def test_autoinstall_ignores_non_official_catalog_entries(tmp_path, monkeypatch):
    from provider_hub import service

    third_party = _official_entry("subdl")
    third_party["source"] = "thirdparty"
    third_party["trusted"] = False
    state_file = _catalog_state(tmp_path, entries={"thirdparty:subdl:1.0.0": third_party})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: staged.append(manifest["provider_id"]))

    assert service.autoinstall_enabled_builtins() == []
    assert staged == []


def test_autoinstall_swallows_stage_failures(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = _catalog_state(tmp_path, entries={
        "official:subdl:1.0.0": _official_entry("subdl"),
        "official:gestdown:1.0.0": _official_entry("gestdown"),
    })
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl", "gestdown"])

    calls = []

    def flaky(manifest, install_timeout=None):
        calls.append(manifest["provider_id"])
        if manifest["provider_id"] == "subdl":
            raise RuntimeError("boom")

    monkeypatch.setattr(service, "stage_install", flaky)

    # subdl fails (swallowed), gestdown still staged; never raises.
    assert service.autoinstall_enabled_builtins() == ["gestdown"]
    assert calls == ["subdl", "gestdown"]


def test_autoinstall_kill_switch_returns_empty(monkeypatch):
    from provider_hub import service
    monkeypatch.setenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", "1")
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    assert service.autoinstall_enabled_builtins() == []


def test_autoinstall_returns_empty_when_state_load_fails(monkeypatch):
    from provider_hub import service
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    def boom(*a, **k):
        raise RuntimeError("corrupt state")
    monkeypatch.setattr(service, "load_state", boom)
    # Must not raise; returns [].
    assert service.autoinstall_enabled_builtins() == []


def test_autoinstall_proceeds_when_catalog_refresh_fails(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = _catalog_state(tmp_path, entries={"official:subdl:1.0.0": _official_entry("subdl")})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))

    def boom(source_ids=None):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(service, "refresh_catalog", boom)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: staged.append(manifest["provider_id"]))

    # Refresh failure is swallowed; auto-install still stages from the cached catalog.
    assert service.autoinstall_enabled_builtins() == ["subdl"]
    assert staged == ["subdl"]


def test_autoinstall_retries_failed_install_rows(tmp_path, monkeypatch):
    from provider_hub import service

    # A previous attempt left a "failed" row; it must not be treated as installed.
    state_file = _catalog_state(
        tmp_path,
        installations={"subdl": {"provider_id": "subdl", "state": "failed",
                                 "active_version": None, "manifest": _manifest(provider_id="subdl")}},
        entries={"official:subdl:1.0.0": _official_entry("subdl")},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: staged.append(manifest["provider_id"]))

    assert service.autoinstall_enabled_builtins() == ["subdl"]
    assert staged == ["subdl"]


def test_autoinstall_skips_refresh_when_nothing_to_install(tmp_path, monkeypatch):
    from provider_hub import service

    # Every enabled provider already has an install row -> no migration work, so the
    # network is never touched (no refresh_catalog call).
    state_file = _catalog_state(
        tmp_path,
        installations={"subdl": {"provider_id": "subdl", "state": "active",
                                 "active_version": "1.0.0", "manifest": _manifest(provider_id="subdl")}},
        entries={"official:subdl:1.0.0": _official_entry("subdl")},
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    refresh_calls = []
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: refresh_calls.append(source_ids))
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: (_ for _ in ()).throw(AssertionError("should not install")))

    assert service.autoinstall_enabled_builtins() == []
    assert refresh_calls == []  # no candidates -> no catalog fetch


def test_autoinstall_refreshes_only_the_official_source(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID

    state_file = _catalog_state(tmp_path, entries={"official:subdl:1.0.0": _official_entry("subdl")})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    refresh_calls = []
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: refresh_calls.append(source_ids))
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    monkeypatch.setattr(service, "stage_install", lambda manifest, install_timeout=None: None)

    service.autoinstall_enabled_builtins()
    assert refresh_calls == [{OFFICIAL_CATALOG_SOURCE_ID}]


def test_autoinstall_bounds_each_install_with_remaining_budget(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = _catalog_state(tmp_path, entries={"official:subdl:1.0.0": _official_entry("subdl")})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setattr(service, "refresh_catalog", lambda source_ids=None: None)
    monkeypatch.setattr(service, "_bazarr_enabled_providers", lambda: ["subdl"])
    captured = {}

    def capture(manifest, install_timeout=None):
        captured["install_timeout"] = install_timeout

    monkeypatch.setattr(service, "stage_install", capture)

    service.autoinstall_enabled_builtins()
    # The install is bounded by the remaining startup budget, not left unbounded.
    assert captured["install_timeout"] is not None
    assert 0 < captured["install_timeout"] <= 180.0


def test_plugin_environment_install_times_out(tmp_path, monkeypatch):
    import subprocess

    from provider_hub.manifest import validate_manifest
    from provider_hub.venv import PluginEnvironment, PluginEnvironmentError

    validated = validate_manifest(
        _manifest(dependencies={"requirements": []}), built_in_provider_ids=set()
    )

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    # A stuck venv/pip step is surfaced as a clean PluginEnvironmentError rather than
    # hanging the install (and therefore boot) indefinitely.
    with pytest.raises(PluginEnvironmentError):
        PluginEnvironment(tmp_path).install(validated, timeout=30)


def test_deadline_request_timeout_bounds_and_rejects():
    import time

    from provider_hub.service import ProviderHubInstallError, _deadline_request_timeout

    # No deadline -> the full per-request default.
    assert _deadline_request_timeout(None) == 30.0
    # Plenty of budget left -> still capped at the default.
    assert _deadline_request_timeout(time.monotonic() + 1000) == 30.0
    # Little budget left -> shrunk to the remaining time.
    shrunk = _deadline_request_timeout(time.monotonic() + 5)
    assert 1.0 <= shrunk <= 5.0
    # Budget exhausted -> the bundle fetch is rejected instead of running long.
    with pytest.raises(ProviderHubInstallError):
        _deadline_request_timeout(time.monotonic() - 1)


def test_worker_download_rejects_excessive_archive_member_count(monkeypatch):
    # The byte caps never trip on an archive full of tiny members, so an entry-count
    # bomb (hundreds of thousands of zero-byte members) would still be scanned/extracted.
    # The guard must also cap the member count, like service.py's _LOCAL_PACKAGE_MAX_MEMBERS.
    from provider_hub import protocol
    from provider_hub.protocol import WorkerProtocolError, worker_download_to_content

    candidate = _hub_candidate("sub-manymembers")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        # Without a count cap this archive extracts fine (one usable .srt); the only
        # thing that should reject it is the member-count guard.
        archive.writestr("only.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        for i in range(20):
            archive.writestr(f"junk{i}.txt", b"x")
    archive_bytes = buffer.getvalue()

    monkeypatch.setattr(protocol, "_MAX_ARCHIVE_MEMBERS", 5, raising=False)
    with pytest.raises(WorkerProtocolError):
        worker_download_to_content(
            candidate, {"archive_b64": base64.b64encode(archive_bytes).decode("ascii")}
        )


def test_worker_download_archive_clears_stale_encoding():
    # An encoding can be carried onto the subtitle at search time (e.g. via the display
    # attribute splat in candidate_from_worker). The archive path must clear it so
    # Subtitle.normalize()/chardet detects encoding from the extracted bytes instead of
    # pinning a stale legacy guess (which silently decodes UTF-8 as mojibake).
    from provider_hub.protocol import worker_download_to_content

    candidate = _hub_candidate("sub-staleenc")
    candidate.encoding = "latin-1"
    candidate._guessed_encoding = "latin-1"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("only.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    archive_bytes = buffer.getvalue()

    worker_download_to_content(
        candidate, {"archive_b64": base64.b64encode(archive_bytes).decode("ascii")}
    )

    assert candidate.encoding is None
    assert candidate._guessed_encoding is None


def test_worker_download_auto_selects_single_vtt_member():
    # The explicit-member path already supports VTT; the auto-select path must too, so a
    # worker archive containing a lone WebVTT file is not rejected as "no usable member".
    from provider_hub.protocol import worker_download_to_content

    candidate = _hub_candidate("sub-vtt")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("only.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")
    archive_bytes = buffer.getvalue()

    worker_download_to_content(
        candidate, {"archive_b64": base64.b64encode(archive_bytes).decode("ascii")}
    )

    assert b"WEBVTT" in candidate.content


def test_worker_download_forwards_episode_title_to_archive_selection(monkeypatch):
    # Episode packs are disambiguated by episode_title when filenames carry the title
    # rather than the number (as subdl/subf2m do). The host must forward episode_title
    # into the shared selector, not just the episode number.
    import subliminal_patch.providers.utils as utils

    from provider_hub.protocol import worker_download_to_content

    captured = {}

    def fake_select(archive, **kwargs):
        captured.update(kwargs)
        return b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"

    monkeypatch.setattr(utils, "get_subtitle_from_archive", fake_select)

    candidate = _hub_candidate("sub-eptitle")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.srt", b"x")
        archive.writestr("b.srt", b"y")
    archive_bytes = buffer.getvalue()

    worker_download_to_content(
        candidate,
        {
            "archive_b64": base64.b64encode(archive_bytes).decode("ascii"),
            "episode": 1,
            "episode_title": "The One With The Test",
        },
    )

    assert captured.get("episode_title") == "The One With The Test"


def test_get_matches_unions_release_derived_matches_for_episode():
    # Provider Hub workers run isolated and cannot run guessit, so they return only the
    # structural matches they verified. The host must derive resolution/source/video_codec/
    # release_group from the candidate's release_info, exactly like native subliminal
    # providers do (e.g. SubdlSubtitle.get_matches calls utils.update_matches).
    from provider_hub.protocol import candidate_from_worker

    episode = Episode(
        "/media/Show.S01E01.1080p.WEB-DL.x264-GROUP.mkv",
        "Show",
        1,
        1,
        source="Web",
        release_group="GROUP",
        resolution="1080p",
        video_codec="H.264",
    )
    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "release_info": "Show.S01E01.1080p.WEB-DL.x264-GROUP",
            "matches": ["series", "season", "episode"],
            "provider_payload": {"provider": "examplehub"},
        },
    )

    matches = candidate.get_matches(episode)

    # Worker-provided structural matches are preserved ...
    assert {"series", "season", "episode"}.issubset(matches)
    # ... and the host adds the release-derived matches on top.
    assert {"resolution", "source", "video_codec", "release_group"}.issubset(matches)


def test_get_matches_handles_release_info_as_list():
    # Some workers return release_info as a list of release names (the protocol allows it,
    # and update_matches accepts an iterable). Each entry is guessed and unioned, so a
    # candidate whose best release matches the file scores like a native provider would.
    from provider_hub.protocol import candidate_from_worker

    movie = Movie(
        "/media/Example.Movie.2024.1080p.WEB-DL.x264-GROUP.mkv",
        "Example Movie",
        year=2024,
        source="Web",
        release_group="GROUP",
        resolution="1080p",
        video_codec="H.264",
    )
    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "release_info": [
                "Example.Movie.2024.720p.HDTV.x265-OTHER",
                "Example.Movie.2024.1080p.WEB-DL.x264-GROUP",
            ],
            "matches": ["title", "year"],
            "provider_payload": {"provider": "examplehub"},
        },
    )

    matches = candidate.get_matches(movie)

    assert {"title", "year"}.issubset(matches)
    assert {"resolution", "source", "video_codec", "release_group"}.issubset(matches)


def test_get_matches_without_release_info_returns_only_worker_matches():
    # Backward compatibility: a candidate with no release_info is unaffected by the
    # host-side derivation and yields exactly the matches the worker reported.
    from provider_hub.protocol import candidate_from_worker

    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "matches": ["title"],
            "provider_payload": {"provider": "examplehub"},
        },
    )

    assert candidate.get_matches(movie) == {"title"}


@pytest.mark.parametrize("flag, expected", [
    (True, True), (False, False), ("true", False), (1, False), (None, False),
])
def test_candidate_ai_translation_requires_literal_true(flag, expected):
    from provider_hub.protocol import candidate_from_worker

    payload = {
        "id": "sub-1", "language": {"alpha3": "eng"},
        "provider_payload": {"provider": "examplehub"},
        "display": {"ai_translated": True},
    }
    if flag is not None:
        payload["ai_translated"] = flag
    candidate = candidate_from_worker("examplehub", payload)
    assert candidate.ai_translated is expected


def test_candidate_language_round_trips_script_and_country():
    from provider_hub.protocol import candidate_from_worker, language_to_payload

    language = Language("zho", "TW", script="Hant")
    candidate = candidate_from_worker("examplehub", {
        "id": "sub-1",
        "language": language_to_payload(language),
        "provider_payload": {"provider": "examplehub"},
    })
    assert candidate.language.script == language.script
    assert candidate.language.country == language.country


def test_get_matches_swallows_release_update_errors(monkeypatch):
    # A malformed release string must never break scoring for a candidate: if the
    # release-based match update raises, get_matches falls back to the worker matches.
    # This also guards the module-level logger the except branch needs -- without it the
    # handler would raise NameError and propagate, breaking scoring for the candidate.
    import subliminal_patch.providers.utils as utils

    from provider_hub.protocol import candidate_from_worker

    def boom(matches, video, release_info, *args, **kwargs):
        raise ValueError("bad release")

    monkeypatch.setattr(utils, "update_matches", boom)

    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "release_info": "Example.Movie.2024.1080p.WEB-DL.x264-GROUP",
            "matches": ["title", "year"],
            "provider_payload": {"provider": "examplehub"},
        },
    )

    matches = candidate.get_matches(movie)

    assert matches == {"title", "year"}


def test_candidate_display_metadata_cannot_clobber_engine_attributes():
    # Mirrors the live embeddedsubtitles payload: its display dict carries a plain
    # string "language" label. Worker-controlled display metadata must never replace
    # host-derived engine state (language is a babelfish Language, matches a set):
    # a clobbered language crashes every later .alpha3/.basename access in the pool.
    from provider_hub.protocol import candidate_from_worker

    candidate = candidate_from_worker(
        provider_name="embeddedsubtitles",
        payload={
            "id": "track-2",
            "language": {"alpha3": "eng"},
            "release_info": "Example.Movie.2026.1080p.WEB-DL.x264-GROUP",
            "matches": ["hash"],
            "provider_payload": {"provider": "embeddedsubtitles", "stream": 2},
            "display": {
                "source": "embedded",
                "codec": "subrip",
                "stream": 2,
                "language": "eng",
                "default": False,
                "matches": ["bogus"],
                "provider_payload": {"hijacked": True},
                "score": "9000",
            },
        },
    )

    assert isinstance(candidate.language, Language)
    assert candidate.language.alpha3 == "eng"
    assert candidate.matches == {"hash"}
    assert candidate.provider_payload == {"provider": "embeddedsubtitles", "stream": 2}
    assert candidate.score is None
    # cosmetic keys still reach the subtitle for UI display
    assert candidate.source == "embedded"
    assert candidate.codec == "subrip"
    assert candidate.stream == 2


def test_candidate_display_reserved_and_private_keys_are_ignored():
    # "id" collides with a read-only property: unguarded, it raises AttributeError and
    # discards the provider's whole result set. Private names must not be injectable
    # either (get_matches caches worker matches under _worker_matches).
    from provider_hub.protocol import candidate_from_worker

    candidate = candidate_from_worker(
        provider_name="examplehub",
        payload={
            "id": "sub-1",
            "language": {"alpha3": "eng"},
            "provider_payload": {"provider": "examplehub"},
            "display": {
                "id": "spoofed",
                "worker_id": "spoofed",
                "_worker_matches": ["leak"],
            },
        },
    )

    assert candidate.id == "examplehub:sub-1"
    assert candidate.worker_id == "sub-1"
    assert getattr(candidate, "_worker_matches", None) is None


def test_pool_drops_subtitles_whose_language_is_not_a_language_object(monkeypatch):
    # One malformed subtitle (language degraded to a plain string) must be skipped at
    # the pool boundary instead of poisoning the whole search: downstream scoring
    # dereferences subtitle.language.alpha3/.basename and would abort every provider.
    from subliminal_patch import extensions
    from subliminal_patch.core import SZProviderPool
    from subliminal_patch.subtitle import Subtitle as PatchedSubtitle

    good = PatchedSubtitle(Language("eng"), subtitle_id="good-1")
    bad = PatchedSubtitle(Language("eng"), subtitle_id="bad-1")
    bad.language = "eng"  # simulate a provider handing back a degraded language

    class FakeGuardProvider(object):
        languages = {Language("eng")}

        @classmethod
        def check(cls, video):
            return True

        def list_subtitles(self, video, languages):
            return [bad, good]

    monkeypatch.setitem(extensions.provider_registry.providers, "fakeguard", FakeGuardProvider)

    pool = SZProviderPool(providers=["fakeguard"])
    pool.initialized_providers["fakeguard"] = FakeGuardProvider()

    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    movie.fps = None

    out = pool.list_subtitles_provider("fakeguard", movie, {Language("eng")})

    assert out == [good]


@pytest.mark.parametrize("status", [403, 429])
def test_catalog_rate_limit_preserves_cached_source(tmp_path, monkeypatch, status):
    import requests
    from provider_hub import service
    from provider_hub.state import load_state

    checked = "2026-09-12T12:00:00+00:00"
    state_file = tmp_path / "state.json"
    entry = {"source": "community", "provider_id": "example", "version": "1.0.0", "trusted": False}
    state_file.write_text(json.dumps({
        "catalog_sources": {"community": {
            "id": "community", "name": "community", "url": "https://github.com/example/providers/blob/main/catalog.json",
            "last_checked_at": checked,
        }}, "catalog_entries": {"community:example:1.0.0": entry}, "jobs": [],
    }))
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    response = requests.Response()
    response.status_code = status
    response.headers.update({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1789228800"})

    def limited(*args, **kwargs):
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(service, "_fetch_github_catalog", limited)
    service.refresh_catalog()
    saved = load_state()
    source = saved["catalog_sources"]["community"]
    assert source["last_checked_at"] == checked
    assert source["last_attempted_at"]
    assert "2026-09-12 16:00:00 UTC" in source["last_error"]
    assert "temporarily limited" in source["last_error"]
    assert saved["catalog_entries"]["community:example:1.0.0"] == entry


def test_catalog_forbidden_is_not_presented_as_rate_limiting():
    import requests
    from provider_hub.service import _catalog_source_error_message
    response = requests.Response()
    response.status_code = 403
    response.headers["x-ratelimit-remaining"] = "20"
    message = _catalog_source_error_message(requests.HTTPError(response=response))
    assert "denied access" in message
    assert "temporarily" not in message


@pytest.mark.parametrize("reset", ["invalid", "9999999999999999999999999"])
def test_catalog_malformed_rate_limit_time_is_recoverable(reset):
    import requests
    from provider_hub.service import _catalog_source_error_message
    response = requests.Response()
    response.status_code = 429
    response.headers["x-ratelimit-remaining"] = "0"
    response.headers["x-ratelimit-reset"] = reset
    assert "did not provide a reset time" in _catalog_source_error_message(requests.HTTPError(response=response))


def test_catalog_secondary_limit_without_retry_header_is_temporary():
    import requests
    from provider_hub.service import _catalog_source_error_message
    response = requests.Response()
    response.status_code = 403
    response.headers["x-ratelimit-remaining"] = "20"
    response.headers["x-ratelimit-reset"] = "1789228800"
    response._content = b'{"message":"You have exceeded a secondary rate limit. Please wait before you try again."}'
    message = _catalog_source_error_message(requests.HTTPError(response=response))
    assert "temporarily limited" in message
    assert "permissions" not in message
    assert "did not provide a reset time" in message
    assert "2026-09-12" not in message


def test_catalog_retry_after_has_precedence(monkeypatch):
    import requests
    from provider_hub import service
    monkeypatch.setattr(service.time, "time", lambda: 1789228800)
    response = requests.Response()
    response.status_code = 429
    response.headers.update({"retry-after": "60", "x-ratelimit-reset": "1789228800", "x-ratelimit-remaining": "0"})
    message = service._catalog_source_error_message(requests.HTTPError(response=response))
    assert "2026-09-12 16:01:00 UTC" in message


@pytest.mark.parametrize("failure", ["rate_limit", "timeout"])
def test_failed_initial_catalog_refresh_waits_for_explicit_retry(tmp_path, monkeypatch, failure):
    import requests
    from provider_hub import service

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    attempts = []
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "60"

    def fetch(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            if failure == "rate_limit":
                raise requests.HTTPError(response=response)
            raise requests.Timeout("Catalog request timed out")
        return {"providers": []}, "f" * 40

    monkeypatch.setattr(service, "_fetch_github_catalog", fetch)
    first = service.list_catalog(auto_refresh=True)
    source = first["sources"][0]
    assert len(attempts) == 1
    assert source["last_checked_at"] is None
    assert source["last_attempted_at"]
    assert source["last_error"]
    for _ in range(3):
        assert service.list_catalog(auto_refresh=True) == first
    assert len(attempts) == 1

    service.refresh_catalog()
    recovered = service.list_catalog(auto_refresh=True)["sources"][0]
    assert len(attempts) == 2
    assert recovered["last_checked_at"]
    assert recovered["last_error"] is None


# ---------------------------------------------------------------------------
# enabled_providers across a run of back-to-back installs
#
# The onboarding wizard lets a user tick several providers and install them in
# one go, and the report was "installing multiple providers might end up
# installing only the first". Two very different faults produce that symptom: an
# install that fails and stops the run, or an install that succeeds and then
# vanishes because the next one read a stale enabled_providers and wrote it back
# without the previous entry. _set_bazarr_provider_enabled read-modify-writes
# that list, so this pins the second possibility shut: three sequential installs
# leave all three ids in the list, in memory and on disk.
#
# It runs in a child process with its own config dir because it exercises the
# real settings singleton and a real write_config(); doing that in the test
# process would rewrite the repository's own config.

_ENABLED_PROVIDERS_SEQUENCE_CHILD = '''\
import json
import os
import sys
import threading
import time

ROOT = os.environ["BAZARR_REPO_ROOT"]
sys.path.insert(0, os.path.join(ROOT, "bazarr"))
sys.path.insert(0, os.path.join(ROOT, "custom_libs"))

from app.get_args import args

assert os.path.realpath(args.config_dir) == os.path.realpath(
    os.environ["EXPECTED_CONFIG_DIR"]
), "child resolved config_dir to %r" % (args.config_dir,)

from app.config import config_yaml_file
from provider_hub import service
from provider_hub.service import _bazarr_enabled_providers, _set_bazarr_provider_enabled

ids = json.loads(os.environ["PROVIDER_IDS"])
returned = [_set_bazarr_provider_enabled(provider_id, True) for provider_id in ids]

sequential_in_memory = list(_bazarr_enabled_providers())

# Concurrent half. A parallel install run reaches _set_bazarr_provider_enabled
# from several threads at once; widening the gap between its read and its write
# turns the lost update from a rare interleaving into a certainty, so the lock
# is what the assertion is really measuring.
_real_read = service._bazarr_enabled_providers


def _slow_read():
    value = _real_read()
    time.sleep(0.05)
    return value


service._bazarr_enabled_providers = _slow_read

threaded_ids = json.loads(os.environ["THREADED_PROVIDER_IDS"])
errors = []


def _enable(provider_id):
    try:
        service._set_bazarr_provider_enabled(provider_id, True)
    except Exception as error:  # noqa: BLE001
        errors.append(repr(error))


threads = [threading.Thread(target=_enable, args=(pid,)) for pid in threaded_ids]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

service._bazarr_enabled_providers = _real_read

from dynaconf import Dynaconf

on_disk = Dynaconf(settings_file=config_yaml_file, core_loaders=["YAML"]).as_dict()

print("__RESULT__" + json.dumps({
    "returned": returned,
    "sequential_in_memory": sequential_in_memory,
    "in_memory": list(_bazarr_enabled_providers()),
    "on_disk": list(on_disk.get("GENERAL", {}).get("enabled_providers") or []),
    "thread_errors": errors,
}))
'''


def test_installs_keep_every_enabled_provider_sequentially_and_in_parallel(tmp_path):
    import sys

    repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    provider_ids = ["hubalpha", "hubbravo", "hubcharlie"]
    threaded_ids = ["hubpara1", "hubpara2", "hubpara3", "hubpara4", "hubpara5"]

    script = tmp_path / "enabled_providers_sequence.py"
    script.write_text(_ENABLED_PROVIDERS_SEQUENCE_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "BAZARR_REPO_ROOT": repo_root,
        "EXPECTED_CONFIG_DIR": str(tmp_path),
        "PROVIDER_IDS": json.dumps(provider_ids),
        "THREADED_PROVIDER_IDS": json.dumps(threaded_ids),
        "SZ_USER_AGENT": "test",
        "BAZARR_VERSION": "test",
        "NO_CLI": "false",
    })

    proc = subprocess.run(
        [sys.executable, str(script), "-c", str(tmp_path)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"child crashed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    marker = "__RESULT__"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None)
    assert line is not None, f"child produced no result\nstdout:\n{proc.stdout}"
    result = json.loads(line[len(marker):])

    assert result["returned"] == [True, True, True]
    assert result["sequential_in_memory"] == provider_ids
    assert result["thread_errors"] == []

    # Order is not a contract once threads are in play, membership is.
    assert sorted(result["in_memory"]) == sorted(provider_ids + threaded_ids)
    assert sorted(result["on_disk"]) == sorted(provider_ids + threaded_ids)


# ---------------------------------------------------------------------------
# enabled_providers that cannot be saved
#
# write_config() answers a full or read-only volume with False rather than an
# exception, and that answer was ignored: installs, enable and disable, and
# removals all reported success and changed the live list while the file kept
# the old one, so a restart undid them.
# ---------------------------------------------------------------------------

def test_enabled_providers_that_cannot_be_saved_are_put_back(monkeypatch):
    from app import config
    from provider_hub import service

    monkeypatch.setattr(config.settings.general, "enabled_providers", ["alpha"])
    monkeypatch.setattr(config, "write_config", lambda: False)

    assert service._set_bazarr_provider_enabled("beta", True) is False
    assert list(config.settings.general.enabled_providers) == ["alpha"]
    assert service._set_bazarr_provider_enabled("alpha", False) is False
    assert list(config.settings.general.enabled_providers) == ["alpha"]
    # A list that already holds the requested state needs no write, and is not a failure.
    assert service._set_bazarr_provider_enabled("alpha", True) is True


def _installed_examplehub(tmp_path, monkeypatch):
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))
    state = load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub",
        "name": "Example",
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "enabled": True,
        "config": {},
        "manifest": {"provider_id": "examplehub", "version": "1.0.0"},
    }
    save_state(state)


def test_an_update_whose_enabled_state_cannot_be_saved_changes_nothing(tmp_path, monkeypatch):
    from flask import Flask
    from provider_hub import service
    from provider_hub.state import load_state

    _installed_examplehub(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_set_bazarr_provider_enabled", lambda provider_id, enabled: False)
    monkeypatch.setattr(service, "_forget_credential_throttle", lambda provider_id: None)

    with pytest.raises(service.ProviderHubSettingsError):
        service.update_provider("examplehub", enabled=False, config={"api_key": "new"})

    stored = load_state()["installations"]["examplehub"]
    assert stored["enabled"] is True
    assert stored["config"] == {}

    # The API reports it as a failure, not as the updated provider.
    import api.provider_hub.provider_hub as hub_api
    app = Flask(__name__)
    with app.test_request_context("/api/provider-hub/providers/examplehub", method="PATCH",
                                  json={"enabled": False}):
        body, status = hub_api.ProviderHubProvider.patch.__wrapped__(
            hub_api.ProviderHubProvider(), "examplehub")
    assert status == 500
    assert "could not be saved" in body


def test_a_removal_whose_disable_cannot_be_saved_leaves_the_provider_installed(tmp_path, monkeypatch):
    from app.jobs_queue import JobFailed
    from provider_hub import jobs as hub_jobs, service
    from provider_hub.state import load_state

    _installed_examplehub(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_set_bazarr_provider_enabled", lambda provider_id, enabled: False)

    with pytest.raises(JobFailed, match="could not be saved"):
        hub_jobs.uninstall_provider("examplehub", "Example")

    stored = load_state()["installations"]["examplehub"]
    assert stored["state"] == "active"
    assert stored["pending_restart"] is False


def test_a_removal_whose_staging_cannot_be_saved_keeps_the_provider_enabled(tmp_path, monkeypatch):
    """The disable is saved before the removal is staged. When the Hub state
    then cannot be written, the provider is still installed and active, so the
    disable has to be put back or it stays switched off after a restart."""
    from app import config
    from provider_hub import service
    from provider_hub.state import load_state

    _installed_examplehub(tmp_path, monkeypatch)
    monkeypatch.setattr(config.settings.general, "enabled_providers", ["examplehub"])
    monkeypatch.setattr(config, "write_config", lambda: True)
    original = service.mutate_state

    def full_volume(mutator, *args, **kwargs):
        if mutator.__name__ == "remove_or_stage":
            raise OSError("No space left on device")
        return original(mutator, *args, **kwargs)

    monkeypatch.setattr(service, "mutate_state", full_volume)

    with pytest.raises(OSError):
        service.remove_installation("examplehub")

    assert list(config.settings.general.enabled_providers) == ["examplehub"]
    assert load_state()["installations"]["examplehub"]["state"] == "active"


def test_a_failed_removal_does_not_enable_a_provider_that_was_off(tmp_path, monkeypatch):
    from app import config
    from provider_hub import service

    _installed_examplehub(tmp_path, monkeypatch)
    monkeypatch.setattr(config.settings.general, "enabled_providers", ["other"])
    monkeypatch.setattr(config, "write_config", lambda: True)
    original = service.mutate_state

    def full_volume(mutator, *args, **kwargs):
        if mutator.__name__ == "remove_or_stage":
            raise OSError("No space left on device")
        return original(mutator, *args, **kwargs)

    monkeypatch.setattr(service, "mutate_state", full_volume)

    with pytest.raises(OSError):
        service.remove_installation("examplehub")

    assert list(config.settings.general.enabled_providers) == ["other"]


def test_a_first_install_that_cannot_be_enabled_is_reported_as_failed(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.service import stage_install_local

    provider_content = b"class LocalProvider: pass\n"
    file_payloads = {"provider.py": provider_content}
    manifest = _manifest(provider_id="locallyinstalled", name="Locally Installed",
                         provider_content=provider_content, dependencies={"requirements": []})
    package = _provider_zip(manifest, file_payloads)
    state_file = tmp_path / "provider_hub" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    _patch_local_install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_set_bazarr_provider_enabled", lambda provider_id, enabled: False)

    with pytest.raises(service.ProviderHubSettingsError, match="could not be enabled"):
        stage_install_local(package)


# Trust and install binding follow the catalog source Bazarr+ resolved, checked
# against a fake GitHub that knows the official catalog's branch history.

_OFFICIAL_REPO_API = "https://api.github.com/repos/LavX/bazarr-provider-catalog"
_MAIN, _BETA, _OUTSIDE = "1" * 40, "2" * 40, "f" * 40
_OLD_MAIN = "3" * 40


class _GitHubAnswer(_FakeResponse):
    def __init__(self, payload=None, status=200):
        super().__init__(payload)
        self.status_code = status
        self.headers = {"x-ratelimit-remaining": "0"} if status == 403 else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class _FakeGitHub:
    """The official catalog repository: branch heads, which commits each commit
    contains, the commits GitHub does not know, and the catalog every commit serves."""

    def __init__(self, monkeypatch, providers=()):
        self.heads = {"main": _MAIN, "beta": _BETA}
        self.history = {_MAIN: {_MAIN, _OLD_MAIN}, _BETA: {_BETA, _OLD_MAIN}, _OLD_MAIN: {_OLD_MAIN}}
        self.unknown = set()
        self.compare_limited = False
        self.providers = list(providers)
        self.compares = []
        monkeypatch.setattr("provider_hub.service.requests.get", self.get)

    def get(self, url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement(
                [(sha, f"refs/heads/{name}") for name, sha in self.heads.items()]))
        if url.startswith(_OFFICIAL_REPO_API + "/compare/"):
            base, head = url.rsplit("/", 1)[1].split("...")
            self.compares.append((base, head))
            if self.compare_limited:
                return _GitHubAnswer({"message": "API rate limit exceeded"}, status=403)
            if head in self.unknown:
                return _GitHubAnswer({"message": "Not Found"}, status=404)
            if head == base:
                status = "identical"
            elif head in self.history.get(base, ()):
                status = "behind"
            elif base in self.history.get(head, ()):
                status = "ahead"
            else:
                status = "diverged"
            return _GitHubAnswer({"status": status})
        if url.startswith("https://raw.githubusercontent.com/"):
            return _GitHubAnswer({"providers": [{"manifest": manifest} for manifest in self.providers]})
        raise AssertionError(f"unexpected request {url}")


def _official_manifest(provider_id="examplehub", version="1.0.0", commit=_MAIN):
    return _manifest(
        provider_id=provider_id, name=provider_id, version=version, dependencies={"requirements": []},
        source={"type": "github", "repo": "LavX/bazarr-provider-catalog", "ref": "main", "commit": commit,
                "catalog_url": "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
                "trusted": True},
    )


def _hub_state(tmp_path, monkeypatch, *, dev_ref=None, installations=None, sources=None, entries=None):
    from provider_hub.state import load_state, save_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "provider_hub" / "state.json"))
    state = load_state()
    state["catalog_sources"]["official"]["dev_ref"] = dev_ref
    state["catalog_sources"].update(sources or {})
    state["catalog_entries"].update(entries or {})
    state["installations"].update(installations or {})
    save_state(state)


def _community_source(name, url=None, resolved_commit="c" * 40):
    return {name: {"id": name, "name": name, "type": "github", "enabled": True, "trusted": False,
                   "url": url or f"https://github.com/{name}/catalog/blob/main/catalog.json",
                   "resolved_commit": resolved_commit}}


def _entry(source, manifest):
    key = f"{source}:{manifest['provider_id']}:{manifest['version']}"
    return {key: {"source": source, "source_name": source, "provider_id": manifest["provider_id"],
                  "name": manifest["name"], "version": manifest["version"], "trusted": source == "official",
                  "manifest": manifest}}


def _installed(manifest, *, trusted, **fields):
    row = {"provider_id": manifest["provider_id"], "name": manifest["name"], "state": "active",
           "active_version": manifest["version"], "pending_restart": False, "origin": "catalog",
           "trusted": trusted, "manifest": manifest}
    row.update(fields)
    return {manifest["provider_id"]: row}


@pytest.mark.parametrize("dev_ref, reason", [
    (None, "default branch"),
    ("beta", "beta"),
    (_OLD_MAIN, "ancestor of main"),
])
def test_official_source_is_trusted_on_main_beta_and_their_ancestors(tmp_path, monkeypatch, dev_ref, reason):
    from provider_hub.service import list_catalog, refresh_catalog

    _FakeGitHub(monkeypatch, providers=[_official_manifest()])
    _hub_state(tmp_path, monkeypatch, dev_ref=dev_ref)

    refresh_catalog()

    catalog = list_catalog()
    official = next(source for source in catalog["sources"] if source["id"] == "official")
    assert (official["trusted"], official["trust_reason"]) == (True, reason)
    assert [entry["trusted"] for entry in catalog["entries"]] == [True]


def test_official_source_on_a_commit_outside_main_and_beta_is_community_code(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.state import load_state

    _FakeGitHub(monkeypatch, providers=[_official_manifest("gestdown", commit=_OUTSIDE)])
    _hub_state(tmp_path, monkeypatch, dev_ref=_OUTSIDE)

    service.refresh_catalog()

    state = load_state()
    official = state["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (False, "outside main and beta")
    [entry] = state["catalog_entries"].values()
    assert entry["trusted"] is False and entry["manifest"]["source"]["trusted"] is False
    # It cannot replace a built-in provider, and startup auto-install skips it.
    with pytest.raises(ManifestValidationError, match="built-in"):
        service.install_catalog_entry("official", "gestdown", "1.0.0")
    assert service._latest_official_catalog_manifest(state, "gestdown") is None


def test_an_unanswered_ancestry_check_never_grants_trust_and_never_removes_it(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    github = _FakeGitHub(monkeypatch, providers=[_official_manifest()])
    github.compare_limited = True
    _hub_state(tmp_path, monkeypatch, dev_ref=_OLD_MAIN)

    refresh_catalog()
    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (False, "could not verify: rate limited")

    # A stored yes that no ancestry check produced is not an earlier verdict.
    from provider_hub.state import save_state
    state = load_state()
    state["catalog_sources"]["official"]["trusted"] = True
    state["catalog_sources"]["official"].pop("trust_reason")
    save_state(state)
    refresh_catalog()
    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (False, "could not verify: rate limited")

    # Once trusted on that commit, the same unanswered check keeps it trusted.
    github.compare_limited = False
    refresh_catalog()
    github.compare_limited = True
    github.heads["main"] = "4" * 40
    refresh_catalog()
    assert load_state()["catalog_sources"]["official"]["trusted"] is True


def test_an_install_from_a_community_source_is_bound_to_it_whatever_its_manifest_says(tmp_path, monkeypatch):
    from provider_hub import service

    _patch_local_install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    # A copy of an official manifest, still naming the official catalog.
    copied = _official_manifest(commit="c" * 40)
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               entries=_entry("community", copied))

    reference = service.resolve_catalog_install(manifest=copied)
    assert reference["source"] == "community"
    service.install_catalog_entry("community", "examplehub", "1.0.0")

    [provider] = service.list_providers()
    assert (provider["source_id"], provider["source_name"], provider["trusted"]) == (
        "community", "community", False)


def test_only_the_official_catalog_installs_or_updates_an_official_id(tmp_path, monkeypatch):
    from provider_hub import service

    github = _FakeGitHub(monkeypatch, providers=[_official_manifest("officialhub")])
    installed = _official_manifest("officialhub")
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               installations=_installed(installed, trusted=True, source_bound=True, source_id="official"))
    service.refresh_catalog(source_ids={"official"})
    github.providers = []
    higher = _official_manifest("officialhub", version="9.0.0", commit="c" * 40)
    _hub_state(tmp_path, monkeypatch, entries=_entry("community", higher))

    assert service.check_updates()["details"]["updates_available"] == 0
    with pytest.raises(service.ProviderHubInstallError, match="only the official catalog"):
        service.resolve_catalog_install("community", "officialhub", "9.0.0")
    blocked = [entry for entry in service.list_catalog()["entries"] if entry["source"] == "community"]
    assert blocked[0]["blocked_reason"] == service.RESERVED_ID_REASON


def test_an_update_comes_only_from_the_source_the_install_is_bound_to(tmp_path, monkeypatch):
    from provider_hub.service import check_updates

    def community(name, version):
        return _manifest(version=version, dependencies={"requirements": []},
                         source={"type": "github", "repo": f"{name}/catalog", "ref": "main",
                                 "commit": "c" * 40, "trusted": False,
                                 "catalog_url": f"https://github.com/{name}/catalog/blob/main/catalog.json"})

    _hub_state(tmp_path, monkeypatch,
               sources={**_community_source("first"), **_community_source("second")},
               entries={**_entry("first", community("first", "1.1.0")),
                        **_entry("second", community("second", "5.0.0"))},
               installations=_installed(community("first", "1.0.0"), trusted=False,
                                        source_bound=True, source_id="first"))

    job = check_updates()

    assert job["details"]["providers"] == ["Example Hub Provider: 1.0.0 -> 1.1.0"]


def test_the_first_official_refresh_binds_existing_installs_by_where_their_code_came_from(tmp_path, monkeypatch):
    from provider_hub.service import list_providers, refresh_catalog

    github = _FakeGitHub(monkeypatch)
    github.history[_OUTSIDE] = {_OUTSIDE, _OLD_MAIN}
    foreign = _official_manifest("foreignhub")
    foreign["source"]["repo"] = "someone/elsewhere"
    orphan = _manifest(provider_id="orphanhub", name="orphanhub", source={
        "type": "github", "repo": "gone/catalog", "ref": "main", "commit": "d" * 40,
        "catalog_url": "https://github.com/gone/catalog/blob/main/catalog.json", "trusted": False})
    held = _manifest(provider_id="heldhub", name="heldhub", source={
        "type": "github", "repo": "kept/catalog", "ref": "main", "commit": "c" * 40,
        "catalog_url": "https://github.com/kept/catalog/blob/main/catalog.json", "trusted": False})
    unrelated_orphan = dict(orphan, source=dict(orphan["source"], repo="other/catalog", commit="e" * 40))
    _hub_state(
        tmp_path, monkeypatch,
        sources={**_community_source("kept"), **_community_source("other", resolved_commit="e" * 40)},
        entries={**_entry("kept", held), **_entry("other", unrelated_orphan)},
        installations={
            **_installed(_official_manifest("mainhub", commit=_OLD_MAIN), trusted=True, source_id="official"),
            **_installed(_official_manifest("outsidehub", commit=_OUTSIDE), trusted=True, source_id="official"),
            **_installed(foreign, trusted=True, source_id="official"),
            **_installed(orphan, trusted=False),
            **_installed(held, trusted=False),
        })

    refresh_catalog(source_ids={"official"})

    providers = {row["provider_id"]: row for row in list_providers()}
    assert (providers["mainhub"]["trusted"], providers["mainhub"]["source_id"]) == (True, "official")
    assert not providers["mainhub"].get("trust_note")
    for demoted in ("outsidehub", "foreignhub"):
        assert (providers[demoted]["trusted"], providers[demoted]["source_id"]) == (False, None)
        assert providers[demoted]["trust_note"].startswith("No longer trusted")
    assert "outside main and beta" in providers["outsidehub"]["trust_note"]
    assert providers["orphanhub"]["source_id"] is None
    assert "no updates" in providers["orphanhub"]["trust_note"]
    assert (providers["heldhub"]["source_id"], providers["heldhub"]["source_name"]) == ("kept", "kept")


def test_installed_trust_follows_the_official_branches_on_every_refresh(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    github = _FakeGitHub(monkeypatch)
    _hub_state(tmp_path, monkeypatch, installations=_installed(
        _official_manifest("gestdown", commit=_OLD_MAIN), trusted=True, source_bound=True, source_id="official"))

    def installed():
        return load_state()["installations"]["gestdown"]

    refresh_catalog()
    assert installed()["trusted"] is True

    # A fast-forward keeps the cached yes: one compare per moved branch, none for the commit.
    new_main = "5" * 40
    github.history[new_main] = {new_main, _MAIN, _OLD_MAIN}
    github.heads["main"] = new_main
    github.compares.clear()
    refresh_catalog()
    assert installed()["trusted"] is True
    assert github.compares == [(_MAIN, new_main)]

    # A check GitHub does not answer changes nothing.
    rewritten_main, rewritten_beta = "6" * 40, "7" * 40
    github.history.update({rewritten_main: {rewritten_main}, rewritten_beta: {rewritten_beta}})
    github.heads.update(main=rewritten_main, beta=rewritten_beta)
    github.compare_limited = True
    refresh_catalog()
    assert installed()["trusted"] is True

    # Both branches rewritten without the commit: demoted, and it says why.
    github.compare_limited = False
    refresh_catalog()
    assert installed()["trusted"] is False and installed()["source_id"] is None
    assert "leaves provider searches" in installed()["trust_note"]

    # beta repaired to contain the commit again: trust and the official binding return.
    repaired = "8" * 40
    github.history[repaired] = {repaired, _OLD_MAIN}
    github.heads["beta"] = repaired
    refresh_catalog()
    assert (installed()["trusted"], installed()["source_id"], installed()["trust_note"]) == (
        True, "official", None)


def test_a_commit_github_does_not_know_is_untrusted(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    github = _FakeGitHub(monkeypatch, providers=[_official_manifest(commit=_OUTSIDE)])
    github.unknown.add(_OUTSIDE)
    _hub_state(tmp_path, monkeypatch, dev_ref=_OUTSIDE, installations=_installed(
        _official_manifest("outsidehub", commit=_OUTSIDE), trusted=True, source_bound=True, source_id="official"))

    refresh_catalog()

    state = load_state()
    assert state["catalog_sources"]["official"]["trust_reason"] == "unknown commit"
    assert state["catalog_sources"]["official"]["trusted"] is False
    assert state["installations"]["outsidehub"]["trusted"] is False
    assert "unknown to GitHub" in state["installations"]["outsidehub"]["trust_note"]


def test_a_failed_refresh_keeps_entries_and_installed_trust(tmp_path, monkeypatch):
    import requests
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    def failing_get(url, timeout, **_kwargs):
        raise requests.exceptions.ConnectionError("could not reach GitHub")

    _hub_state(tmp_path, monkeypatch,
               entries=_entry("official", _official_manifest("examplehub")),
               installations=_installed(_official_manifest("examplehub", commit=_OLD_MAIN),
                                        trusted=True, source_bound=True, source_id="official"))
    monkeypatch.setattr("provider_hub.service.requests.get", failing_get)

    refresh_catalog()

    state = load_state()
    assert "Could not reach GitHub" in state["catalog_sources"]["official"]["last_error"]
    assert list(state["catalog_entries"]) == ["official:examplehub:1.0.0"]
    installation = state["installations"]["examplehub"]
    assert (installation["state"], installation["trusted"], installation["source_id"]) == (
        "active", True, "official")
    assert not installation.get("trust_note")


def test_one_source_failing_does_not_block_other_sources(tmp_path, monkeypatch):
    import requests
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    live_commit = "9" * 40

    def community(repo, provider_id, version, commit):
        return _manifest(provider_id=provider_id, name=provider_id, version=version,
                         dependencies={"requirements": []},
                         source={"type": "github", "repo": f"{repo}/catalog", "ref": "main", "commit": commit,
                                 "trusted": False,
                                 "catalog_url": f"https://github.com/{repo}/catalog/blob/main/catalog.json"})

    def get(url, timeout, **_kwargs):
        if url.startswith("https://github.com/broken/catalog/"):
            raise requests.exceptions.ConnectionError("could not reach GitHub")
        if url == "https://github.com/live/catalog/info/refs?service=git-upload-pack":
            return _AdvertisementResponse(_git_advertisement([(live_commit, "refs/heads/main")]))
        if url == f"https://raw.githubusercontent.com/live/catalog/{live_commit}/catalog.json":
            return _FakeResponse({"providers": [
                {"manifest": community("live", "livehub", "2.0.0", live_commit)}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", get)
    _hub_state(tmp_path, monkeypatch,
               sources={**_community_source("broken"), **_community_source("live")},
               entries=_entry("broken", community("broken", "brokenhub", "1.0.0", "c" * 40)))

    refresh_catalog(source_ids={"broken", "live"})

    state = load_state()
    broken = state["catalog_sources"]["broken"]
    assert "Could not reach GitHub" in broken["last_error"]
    assert broken["resolved_commit"] == "c" * 40
    live = state["catalog_sources"]["live"]
    assert (live["last_error"], live["resolved_commit"]) == (None, live_commit)
    assert set(state["catalog_entries"]) == {"broken:brokenhub:1.0.0", "live:livehub:2.0.0"}


# Catalog ref resolution over the git smart-HTTP protocol: the ref
# advertisement every git client reads, parsed with bounds, plus the
# unchanged-commit skip that ends a refresh after the advertisement.

_OFFICIAL_AD = "https://github.com/LavX/bazarr-provider-catalog/info/refs?service=git-upload-pack"
_COMMUNITY_AD = "https://github.com/community/catalog/info/refs?service=git-upload-pack"

_FROZEN_NOW = "2026-10-08T12:00:00Z"
_OLD_CHECK = "2020-01-01T00:00:00Z"


def _seed_a_successful_check(tmp_path, monkeypatch, source):
    """Seed an old successful check on ``source`` and freeze the clock, so a
    refresh that records a fresh check at the current time is provable."""
    from provider_hub.state import load_state, save_state

    state = load_state()
    state["catalog_sources"][source]["last_checked_at"] = _OLD_CHECK
    state["catalog_sources"][source]["last_error"] = None
    save_state(state)
    monkeypatch.setattr("provider_hub.service.utcnow_iso", lambda: _FROZEN_NOW)


def _pkt_line(payload: bytes) -> bytes:
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def _git_advertisement(refs) -> bytes:
    """A protocol v0 ref advertisement body: the service banner, a flush, one
    ``<sha> <ref>`` line per entry (the first carrying the symref capability a
    real server sends), then the closing flush."""
    body = [_pkt_line(b"# service=git-upload-pack\n"), b"0000"]
    for index, (sha, name) in enumerate(refs):
        capabilities = b"\0symref=HEAD:refs/heads/main agent=git/2.43.0" if index == 0 else b""
        body.append(_pkt_line(f"{sha} {name}".encode("utf-8") + capabilities + b"\n"))
    body.append(b"0000")
    return b"".join(body)


class _AdvertisementResponse(_FakeResponse):
    def __init__(self, content=b"", content_type="application/x-git-upload-pack-advertisement",
                 status_code=200, content_length=None):
        super().__init__(content=content)
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if content_length is None:
            content_length = len(content)
        if content_length:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        yield self.content


def test_advertisement_parser_reads_heads_and_peeled_tags_only():
    from provider_hub.service import _parse_git_ref_advertisement

    body = _git_advertisement([
        ("a" * 40, "refs/heads/main"),
        ("b" * 40, "refs/heads/feature/slash-bearing"),
        ("c" * 40, "refs/tags/v1.0.0"),
        ("d" * 40, "refs/tags/v1.0.0^{}"),
        ("e" * 40, "refs/pull/1/head"),
        ("f" * 40, "refs/remotes/origin/old"),
        ("0" * 40, "HEAD"),
    ])

    refs = _parse_git_ref_advertisement(body)

    assert refs == {
        "refs/heads/main": "a" * 40,
        "refs/heads/feature/slash-bearing": "b" * 40,
        "refs/tags/v1.0.0": "d" * 40,
    }


def test_advertisement_parser_accepts_a_real_shaped_body_with_pull_refs():
    from provider_hub.service import _parse_git_ref_advertisement

    # Not a capture: the capture lives in the fixture parsed by the test
    # below. This synthetic body stresses the parser with a large block of
    # namespaces it must skip, the shape a busy repository advertises.
    refs = [(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]
    for number in range(1, 155):
        refs.append(("%040x" % number, f"refs/pull/{number}/head"))
        refs.append(("%040x" % (number + 1000), f"refs/pull/{number}/merge"))
    refs.append(("c" * 40, "refs/tags/v2.7.1"))
    refs.append(("d" * 40, "refs/tags/v2.7.1^{}"))

    parsed = _parse_git_ref_advertisement(_git_advertisement(refs))

    assert parsed == {
        "refs/heads/main": _MAIN,
        "refs/heads/beta": _BETA,
        "refs/tags/v2.7.1": "d" * 40,
    }


_ADVERTISEMENT_BANNER_FLUSH = _pkt_line(b"# service=git-upload-pack\n") + b"0000"


@pytest.mark.parametrize("body", [
    b"",
    b"0001",
    b"0003",
    b"zzzzjunk",
    # A complete receive-pack advertisement: the exact service line is the
    # rule that rejects it, not a missing closing flush.
    _pkt_line(b"# service=git-receive-pack\n") + b"0000"
    + _pkt_line((_MAIN + " refs/heads/main\n").encode()) + b"0000",
    _pkt_line(b"# service=git-upload-pack\n") + b"0000" + _pkt_line(b"not a ref line\n"),
    _pkt_line(b"# service=git-upload-pack\n") + b"0000" + _pkt_line(b"deadbeef refs/heads/main\n"),
    _git_advertisement([(_MAIN, "refs/heads/main")])[:-6],
    _git_advertisement([(_MAIN, "refs/heads/main")])[:-3],
    # No closing flush: the ref list was cut off after a complete line.
    _git_advertisement([(_MAIN, "refs/heads/main")])[:-4],
    # Data appended after the closing flush.
    _git_advertisement([(_MAIN, "refs/heads/main")])
    + _pkt_line((_BETA + " refs/heads/beta\n").encode()),
    # A ref line before the service banner: the protocol's first pkt-line
    # is the service banner, and both flushes cannot legitimize it after.
    _pkt_line((_MAIN + " refs/heads/main\n").encode())
    + _pkt_line(b"# service=git-upload-pack\n") + b"0000" + b"0000",
    # Length prefixes int(..., 16) tolerates when the regex is gone. Each
    # reads as 61 against the 57-byte ref line that follows it, so without
    # the regex the body parses clean instead of failing closed.
    *(_ADVERTISEMENT_BANNER_FLUSH + bad_prefix
       + (_MAIN + " refs/heads/main\n").encode() + b"0000"
       for bad_prefix in (b" 03d", b"0x3d", b"+03d", b"0_3d")),
    # A well-formed ref line whose 65,517-byte payload is over the bound.
    _ADVERTISEMENT_BANNER_FLUSH
    + _pkt_line((_MAIN + " refs/heads/main" + "-" * 65460 + "\n").encode()) + b"0000",
    # The empty-repository marker with a real ref line after it: the marker
    # is only the whole advertisement git sends for a repository with no refs.
    _ADVERTISEMENT_BANNER_FLUSH
    + _pkt_line(b"0" * 40 + b" capabilities^{}\0agent=git/2.43.0\n")
    + _pkt_line((_MAIN + " refs/heads/main\n").encode()) + b"0000",
    # A flush before the banner.
    b"0000" + _pkt_line(b"# service=git-upload-pack\n"),
    # A ref line after a second flush closed the list early.
    _pkt_line(b"# service=git-upload-pack\n") + b"0000" + b"0000"
    + _pkt_line((_MAIN + " refs/heads/main\n").encode()) + b"0000",
])
def test_advertisement_parser_fails_closed_on_malformed_bodies(body):
    from provider_hub.service import CatalogSourceError, _parse_git_ref_advertisement

    with pytest.raises(CatalogSourceError):
        _parse_git_ref_advertisement(body)


def test_advertisement_parser_rejects_a_ref_advertised_at_two_commits():
    from provider_hub.service import CatalogSourceError, _parse_git_ref_advertisement

    body = _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/main")])

    with pytest.raises(CatalogSourceError):
        _parse_git_ref_advertisement(body)


def test_advertisement_parser_peels_only_a_tag_advertised_on_the_line_before():
    from provider_hub.service import CatalogSourceError, _parse_git_ref_advertisement

    # A peeled line that does not follow its own tag line.
    not_after_its_tag = _git_advertisement([
        (_MAIN, "refs/heads/main"),
        ("a" * 40, "refs/tags/v1.0.0"),
        ("b" * 40, "refs/tags/v2.0.0^{}"),
    ])
    # A peel that does not directly follow its own tag: v1, then v2, then
    # the v1 peel, which is out of order even though v1 was advertised.
    out_of_order_peel = _git_advertisement([
        ("a" * 40, "refs/tags/v1"),
        ("b" * 40, "refs/tags/v2"),
        ("c" * 40, "refs/tags/v1^{}"),
    ])
    # A peeled line for a branch, which must never override its sha.
    peel_of_a_branch = _git_advertisement([
        (_MAIN, "refs/heads/main"),
        (_BETA, "refs/heads/main^{}"),
    ])
    # A peeled line whose tag was never advertised.
    peel_without_a_tag = _git_advertisement([("b" * 40, "refs/tags/v2.0.0^{}")])

    for body in (not_after_its_tag, out_of_order_peel, peel_of_a_branch, peel_without_a_tag):
        with pytest.raises(CatalogSourceError):
            _parse_git_ref_advertisement(body)


def test_advertisement_parser_rejects_a_tag_peeled_at_two_commits():
    from provider_hub.service import CatalogSourceError, _parse_git_ref_advertisement

    body = _git_advertisement([
        ("a" * 40, "refs/tags/v1.0.0"),
        ("b" * 40, "refs/tags/v1.0.0^{}"),
        ("a" * 40, "refs/tags/v1.0.0"),
        ("c" * 40, "refs/tags/v1.0.0^{}"),
    ])

    with pytest.raises(CatalogSourceError):
        _parse_git_ref_advertisement(body)


class _StreamingAdvertisement:
    """A streamed advertisement answer that counts the chunks it gave out."""

    def __init__(self, chunks, content_length=None):
        self.headers = {"Content-Type": "application/x-git-upload-pack-advertisement"}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status_code = 200
        self._chunks = chunks
        self.consumed = 0
        self.closed = False

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def test_an_oversized_advertisement_stops_reading_early(monkeypatch):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError

    chunks = [b"\0" * (2 * 1024 * 1024) for _ in range(20)]
    response = _StreamingAdvertisement(chunks)
    streams = []

    def fake_get(url, timeout, stream=None, **_kwargs):
        streams.append(stream)
        return response

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    with pytest.raises(CatalogSourceError):
        service._fetch_git_ref_advertisement("owner", "repo")

    # The 10 MB cap stopped the download at the sixth 2 MB chunk of twenty,
    # the advertisement was streamed, and the response was closed.
    assert streams == [True]
    assert response.consumed == 6
    assert response.closed


def test_an_advertisement_content_length_over_the_cap_is_never_read(monkeypatch):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError

    chunks = [b"\0" * (2 * 1024 * 1024) for _ in range(20)]
    response = _StreamingAdvertisement(chunks, content_length=20 * 1024 * 1024)
    streams = []

    def fake_get(url, timeout, stream=None, **_kwargs):
        streams.append(stream)
        return response

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    with pytest.raises(CatalogSourceError):
        service._fetch_git_ref_advertisement("owner", "repo")

    assert streams == [True]
    assert response.consumed == 0
    assert response.closed


def _streamed_github_error(body, content_encoding=None):
    """A requests response over a live urllib3 stream, the way the real
    advertisement fetch receives an error: preloaded nowhere, decoded only on
    an explicit read."""
    import io
    import urllib3
    import requests

    headers = {"Content-Encoding": content_encoding} if content_encoding else {}
    response = requests.Response()
    response.status_code = 403
    response.headers = dict(headers)
    response.raw = urllib3.HTTPResponse(
        body=io.BytesIO(body), headers=headers, status=403,
        preload_content=False, decode_content=False,
    )
    return response


def _spy_on_close(response):
    """Replace a response's close with one that records its calls.

    The raw side of a consumed response closes itself at end-of-file, so
    ``response.raw.closed`` is true whether or not anything called close.
    Recording the calls is the only way to tell the two apart."""
    import requests

    calls = []
    original_close = requests.Response.close.__get__(response)

    def spy_close():
        calls.append(1)
        original_close()

    response.close = spy_close
    return calls


def test_a_streamed_secondary_rate_limit_body_survives_the_close(monkeypatch):
    import requests
    from provider_hub import service
    from provider_hub.service import _fetch_git_ref_advertisement

    # A streamed 403 whose only rate-limit evidence is the body: the JSON
    # secondary-rate-limit message, with no rate-limit headers. The body is
    # retained and the response closed before the error mapper runs, so the
    # mapper must see the retained slice, not a misclassified plain access
    # denial.
    body = (b'{"message": "You have exceeded a secondary rate limit. Please '
            b'wait a few minutes before you try again."}')
    response = _streamed_github_error(body)
    close_calls = _spy_on_close(response)

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, stream=None: response,
    )

    with pytest.raises(requests.exceptions.HTTPError) as caught:
        _fetch_git_ref_advertisement("owner", "repo")

    assert "temporarily limited" in service._catalog_source_error_message(caught.value)
    assert response.raw.closed
    assert close_calls


def test_a_gzip_encoded_secondary_rate_limit_body_is_decoded_before_retention(monkeypatch):
    import gzip
    import requests
    from provider_hub import service
    from provider_hub.service import _fetch_git_ref_advertisement

    # requests asks GitHub for gzip on every request, and the advertisement
    # stream is opened with decode_content=False, so the raw stream carries
    # compressed bytes. A secondary rate limit sent gzip-encoded must still
    # be read as its JSON message, not classified as a plain access denial,
    # and the response must be closed either way.
    body = (b'{"message": "You have exceeded a secondary rate limit. Please '
            b'wait a few minutes before you try again."}')
    response = _streamed_github_error(gzip.compress(body), content_encoding="gzip")
    close_calls = _spy_on_close(response)

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, stream=None: response,
    )

    with pytest.raises(requests.exceptions.HTTPError) as caught:
        _fetch_git_ref_advertisement("owner", "repo")

    assert "temporarily limited" in service._catalog_source_error_message(caught.value)
    assert response.raw.closed
    assert close_calls


class _ChunkedRaw:
    """The raw side of a Transfer-Encoding: chunked answer: stream() hands
    out one HTTP chunk at a time, running the content decoding incrementally
    across the chunks the way urllib3 does. It never closes itself, so its
    closed flag moves only when something calls close."""

    def __init__(self, chunks, gzip_encoded=False):
        self._chunks = chunks
        self._gzip_encoded = gzip_encoded
        self.closed = False

    def stream(self, amt, decode_content=None):
        import zlib

        decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS)
                   if self._gzip_encoded and decode_content else None)
        for chunk in self._chunks:
            yield decoder.decompress(chunk) if decoder is not None else chunk

    def close(self):
        self.closed = True


def _chunked_github_error(pieces, gzip_encoded=False):
    """A requests response whose raw stream yields its 403 body one HTTP
    chunk at a time, the shape a chunked denial arrives in on a live
    connection: the first read does not carry the whole body."""
    import requests

    response = requests.Response()
    response.status_code = 403
    response.headers = {"Content-Encoding": "gzip"} if gzip_encoded else {}
    response.raw = _ChunkedRaw(pieces, gzip_encoded=gzip_encoded)
    return response


_SECONDARY_RATE_LIMIT_BODY = (b'{"message": "You have exceeded a secondary '
                              b'rate limit. Please wait a few minutes before '
                              b'you try again."}')


@pytest.mark.parametrize("gzip_encoded", [False, True])
def test_a_chunked_secondary_rate_limit_body_is_retained_across_chunks(monkeypatch, gzip_encoded):
    import gzip
    import requests
    from provider_hub import service
    from provider_hub.service import _fetch_git_ref_advertisement

    # A chunked 403 does not arrive as one read: the raw stream yields one
    # HTTP chunk at a time, and the first chunk stops inside the message.
    # The retention must accumulate the decoded chunks until it has the
    # body, or the mapper sees a cut-off body and reports a plain access
    # denial instead of the secondary rate limit.
    body = _SECONDARY_RATE_LIMIT_BODY
    split = 40
    encoded = gzip.compress(body) if gzip_encoded else body
    response = _chunked_github_error([encoded[:split], encoded[split:]], gzip_encoded=gzip_encoded)
    close_calls = _spy_on_close(response)

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, stream=None: response,
    )

    with pytest.raises(requests.exceptions.HTTPError) as caught:
        _fetch_git_ref_advertisement("owner", "repo")

    assert "temporarily limited" in service._catalog_source_error_message(caught.value)
    assert close_calls


def test_advertisement_parser_reads_the_captured_catalog_advertisement():
    from provider_hub.service import _parse_git_ref_advertisement

    # Captured 2026-10-07 from the public catalog repository's own
    # advertisement, the response every git ls-remote reads:
    # https://github.com/LavX/bazarr-provider-catalog/info/refs?service=git-upload-pack
    body = (Path(__file__).parent / "fixtures" / "provider_hub"
            / "catalog_info_refs_v0.bin").read_bytes()

    refs = _parse_git_ref_advertisement(body)

    assert refs["refs/heads/main"] == "be946567da192b9b9f05a18ef43acf9bb7668620"
    assert refs["refs/heads/beta"] == "7bcd0a3a82fa068496111993f08ef93f7f33182e"
    assert b"refs/pull/" in body
    assert all(name.startswith(("refs/heads/", "refs/tags/")) for name in refs)


def test_advertisement_parser_fails_closed_on_an_oversized_body():
    from provider_hub.service import (
        _GIT_ADVERTISEMENT_MAX_BYTES,
        _parse_git_ref_advertisement,
        CatalogSourceError,
    )

    with pytest.raises(CatalogSourceError):
        _parse_git_ref_advertisement(b"\0" * (_GIT_ADVERTISEMENT_MAX_BYTES + 1))


def test_advertisement_parser_accepts_a_line_at_the_exact_payload_bound():
    from provider_hub.service import _PKT_LINE_MAX_PAYLOAD, _parse_git_ref_advertisement

    # A well-formed ref line whose payload is exactly 65,516 bytes: the
    # bound rejects only what is over it, not the boundary itself.
    name = "refs/heads/main" + "-" * (_PKT_LINE_MAX_PAYLOAD - 57)
    body = _ADVERTISEMENT_BANNER_FLUSH + _pkt_line((_MAIN + " " + name + "\n").encode()) + b"0000"

    assert _parse_git_ref_advertisement(body) == {name: _MAIN}


def test_advertisement_parser_accepts_the_empty_repository_advertisement(monkeypatch):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError, _parse_git_ref_advertisement

    # git's documented advertisement for a repository with no refs: one
    # line, the all-zero sha and the capabilities peel.
    body = (_ADVERTISEMENT_BANNER_FLUSH
            + _pkt_line(b"0" * 40 + b" capabilities^{}"
                        + b"\0symref=HEAD:refs/heads/main agent=git/2.43.0\n")
            + b"0000")

    assert _parse_git_ref_advertisement(body) == {}

    # No branch is advertised, so the resolver reports the missing branch
    # instead of blaming GitHub's framing.
    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(body),
    )
    with pytest.raises(CatalogSourceError, match="did not advertise 'main'"):
        service._resolve_github_ref("owner", "repo", "main")


def test_resolve_github_ref_short_circuits_a_full_sha_without_a_request(monkeypatch):
    from provider_hub import service

    def unexpected(url, timeout, **_kwargs):
        raise AssertionError(f"the 40-hex short-circuit must not call GitHub: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", unexpected)

    assert service._resolve_github_ref("owner", "repo", "A" * 40) == "a" * 40


def test_resolve_github_ref_reads_heads_first_and_peeled_tags_second(monkeypatch):
    from provider_hub import service

    body = _git_advertisement([
        ("a" * 40, "refs/heads/main"),
        ("b" * 40, "refs/tags/main"),
        ("c" * 40, "refs/tags/v1.0.0"),
        ("d" * 40, "refs/tags/v1.0.0^{}"),
    ])
    calls = []
    answers = []

    def fake_get(url, timeout, **_kwargs):
        calls.append((url, timeout))
        answer = _AdvertisementResponse(body)
        answers.append(answer)
        return answer

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)

    assert service._resolve_github_ref("owner", "repo", "main") == "a" * 40
    assert service._resolve_github_ref("owner", "repo", "v1.0.0") == "d" * 40
    # A successful resolve closes its advertisement response too, not only
    # the error path.
    assert [answer.closed for answer in answers] == [True, True]
    assert calls == [
        ("https://github.com/owner/repo/info/refs?service=git-upload-pack", 20),
        ("https://github.com/owner/repo/info/refs?service=git-upload-pack", 20),
    ]


def test_resolve_github_ref_fails_closed_on_a_ref_the_advertisement_lacks(monkeypatch):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(
            _git_advertisement([(_MAIN, "refs/heads/main")])),
    )

    with pytest.raises(CatalogSourceError, match="branch name or a tag name"):
        service._resolve_github_ref("owner", "repo", "v2.0.0")


@pytest.mark.parametrize("content_type", [
    "text/html",
    "text/plain; marker=x-git-upload-pack-advertisement",
    "application/x-git-upload-pack-advertisement-malicious",
    "application/octet-stream",
])
def test_resolve_github_ref_fails_closed_on_a_non_git_content_type(monkeypatch, content_type):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(
            _git_advertisement([(_MAIN, "refs/heads/main")]), content_type=content_type),
    )

    with pytest.raises(CatalogSourceError):
        service._resolve_github_ref("owner", "repo", "main")


def test_the_advertisement_content_type_allows_parameters(monkeypatch):
    from provider_hub import service

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(
            _git_advertisement([(_MAIN, "refs/heads/main")]),
            content_type="application/x-git-upload-pack-advertisement; charset=utf-8"),
    )

    assert service._resolve_github_ref("owner", "repo", "main") == _MAIN


@pytest.mark.parametrize("failure, expected", [
    # github.com answers an anonymous request for a missing or private
    # repository with 401, so it means the same as a 404 here.
    (401, "GitHub does not have this catalog source"),
    (429, "temporarily limited"),
    (503, "GitHub returned HTTP 503 while refreshing this catalog source."),
    ("timeout", "Could not reach GitHub while refreshing this catalog source."),
    ("connection", "Could not reach GitHub while refreshing this catalog source."),
])
def test_advertisement_failures_keep_the_refresh_messages(
    tmp_path, monkeypatch, failure, expected
):
    import requests
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    def fake_get(url, timeout, **_kwargs):
        if url == _OFFICIAL_AD:
            if failure == "timeout":
                raise requests.exceptions.Timeout("advertisement timed out")
            if failure == "connection":
                raise requests.exceptions.ConnectionError("connection reset")
            return _AdvertisementResponse(status_code=failure)
        if url.startswith("https://api.github.com/"):
            # The REST commits call is gone: a refresh must never ask it.
            raise AssertionError(f"a refresh must not use the REST API: {url}")
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    refresh_catalog(source_ids={"official"})

    assert expected in load_state()["catalog_sources"]["official"]["last_error"]


def test_advertisement_404_is_a_hard_error(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(status_code=404),
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(_empty_state_file(tmp_path)))

    refresh_catalog(source_ids={"official"})

    last_error = load_state()["catalog_sources"]["official"]["last_error"]
    assert "does not have" in last_error
    assert "temporarily" not in last_error


def test_refresh_skips_the_catalog_fetch_when_the_commit_is_unchanged(tmp_path, monkeypatch):
    from provider_hub.service import list_jobs, refresh_catalog
    from provider_hub.state import load_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(_git_advertisement([("c" * 40, "refs/heads/main")]))
        raise AssertionError(f"an unchanged commit must not be fetched: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               entries=_entry("community", _manifest()))
    _seed_a_successful_check(tmp_path, monkeypatch, "community")

    result = refresh_catalog(source_ids={"community"})

    assert calls == [_COMMUNITY_AD]
    source = load_state()["catalog_sources"]["community"]
    assert source["resolved_commit"] == "c" * 40
    assert source["last_error"] is None
    # The skip records a fresh check at the current time, not the old one.
    assert source["last_checked_at"] == _FROZEN_NOW
    assert list(load_state()["catalog_entries"]) == ["community:examplehub:1.0.0"]
    # A skip keeps the stored entries, so the summary reports them, not zero.
    assert result["entries"] == 1
    job = list_jobs()[-1]
    assert job["message"] == "Refreshed 1 source(s), 1 entries"
    assert job["details"]["entries"] == 1


def test_a_skipped_source_never_prunes_entries(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(
                _git_advertisement([("c" * 40, "refs/heads/main")]))
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               entries=_entry("community", _manifest(provider_id="stalehub")))

    refresh_catalog(source_ids={"community"})

    # Only the advertisement was asked, the refresh recorded no error, and
    # the stored entry was kept: a skip is a success that prunes nothing.
    assert calls == [_COMMUNITY_AD]
    source = load_state()["catalog_sources"]["community"]
    assert source["last_error"] is None
    assert "community:stalehub:1.0.0" in load_state()["catalog_entries"]


def test_refresh_fetches_when_the_unchanged_commit_has_no_entries(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    raw_url = "https://raw.githubusercontent.com/community/catalog/" + "c" * 40 + "/catalog.json"
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(_git_advertisement([("c" * 40, "refs/heads/main")]))
        if url == raw_url:
            return _FakeResponse({"providers": []})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"))
    _seed_a_successful_check(tmp_path, monkeypatch, "community")

    refresh_catalog(source_ids={"community"})

    assert calls == [_COMMUNITY_AD, raw_url]
    source = load_state()["catalog_sources"]["community"]
    assert source["last_error"] is None
    # A fetch that found no entries still records a fresh check.
    assert source["last_checked_at"] == _FROZEN_NOW


def test_refresh_fetches_when_the_advertised_commit_moved(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    raw_url = "https://raw.githubusercontent.com/community/catalog/" + "d" * 40 + "/catalog.json"
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        if url == raw_url:
            return _FakeResponse({"providers": []})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               entries=_entry("community", _manifest()))

    refresh_catalog(source_ids={"community"})

    assert calls == [_COMMUNITY_AD, raw_url]
    assert load_state()["catalog_sources"]["community"]["resolved_commit"] == "d" * 40


def test_a_user_source_on_a_dev_ref_skips_like_any_user_source(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    community = _community_source("community")
    community["community"]["dev_ref"] = "feat/x"
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(
                _git_advertisement([("c" * 40, "refs/heads/feat/x")]))
        raise AssertionError(f"an unchanged dev ref must not be fetched: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, sources=community, entries=_entry("community", _manifest()))

    refresh_catalog(source_ids={"community"})

    assert calls == [_COMMUNITY_AD]
    assert load_state()["catalog_sources"]["community"]["last_error"] is None


def test_a_source_pinned_to_a_full_sha_still_fetches_raw(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    raw_url = "https://raw.githubusercontent.com/community/catalog/" + "c" * 40 + "/catalog.json"
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == raw_url:
            return _FakeResponse({"providers": [{"manifest": _manifest()}]})
        raise AssertionError(f"a pinned commit needs no advertisement: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    pinned = _community_source("community")
    pinned["community"]["dev_ref"] = "c" * 40
    _hub_state(tmp_path, monkeypatch, sources=pinned, entries=_entry("community", _manifest()))

    refresh_catalog(source_ids={"community"})

    # The skip needs an advertised sha equal to the stored commit. A
    # pinned source has no advertisement, so it still fetches raw, and a
    # deleted, renamed or private repository keeps showing at refresh.
    assert calls == [raw_url]
    source = load_state()["catalog_sources"]["community"]
    assert source["last_error"] is None
    assert source["resolved_commit"] == "c" * 40
    assert list(load_state()["catalog_entries"]) == ["community:examplehub:1.0.0"]


def test_a_short_sha_failure_keeps_the_cached_catalog(tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.service import CatalogSourceError, refresh_catalog
    from provider_hub.state import load_state

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(
            _git_advertisement([("c" * 40, "refs/heads/main")])),
    )
    short = _community_source("community")
    short["community"]["dev_ref"] = "abc1234"
    _hub_state(tmp_path, monkeypatch, sources=short, entries=_entry("community", _manifest()))

    refresh_catalog(source_ids={"community"})

    source = load_state()["catalog_sources"]["community"]
    # The resolver's advice reaches the user verbatim through the refresh's
    # error mapping, and the stored entries and commit survive it, so the
    # cached catalog stays visible and usable.
    with pytest.raises(CatalogSourceError) as caught:
        service._resolve_github_ref("community", "catalog", "abc1234")
    expected = service._catalog_source_error_message(caught.value)
    assert "full commit SHA" in expected
    assert source["last_error"] == expected
    assert source["resolved_commit"] == "c" * 40
    assert list(load_state()["catalog_entries"]) == ["community:examplehub:1.0.0"]


def test_a_malformed_advertisement_failure_keeps_the_cached_catalog(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(b"zzzzjunk"),
    )
    _hub_state(tmp_path, monkeypatch, sources=_community_source("community"),
               entries=_entry("community", _manifest()))

    refresh_catalog(source_ids={"community"})

    source = load_state()["catalog_sources"]["community"]
    assert source["last_error"] == "GitHub sent a malformed ref advertisement"
    assert source["resolved_commit"] == "c" * 40
    assert list(load_state()["catalog_entries"]) == ["community:examplehub:1.0.0"]


def test_a_trust_failure_on_the_skip_path_fails_the_refresh_not_the_source(
    tmp_path, monkeypatch
):
    from provider_hub import service
    from provider_hub.state import load_state, save_state

    monkeypatch.setattr(
        "provider_hub.service.requests.get",
        lambda url, timeout, **_kwargs: _AdvertisementResponse(
            _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")])),
    )

    def broken_trust(source, commit, previous_commit, ancestry):
        raise RuntimeError("trust machinery broke")

    monkeypatch.setattr(service, "_set_official_trust", broken_trust)
    _hub_state(tmp_path, monkeypatch, entries=_entry("official", _official_manifest()))
    state = load_state()
    state["catalog_sources"]["official"]["resolved_commit"] = _MAIN
    save_state(state)
    before = load_state()

    with pytest.raises(RuntimeError):
        service.refresh_catalog(source_ids={"official"})

    # The trust update runs outside the fetch on the skip path too, so its
    # failure propagates like on the fetch path instead of marking a source
    # failed after its success fields were already written. The one-write
    # refresh leaves the catalog exactly as it was (the failed job record is
    # the only thing written).
    after = load_state()
    assert after["catalog_sources"] == before["catalog_sources"]
    assert after["catalog_entries"] == before["catalog_entries"]
    assert after["catalog_sources"]["official"]["last_error"] is None


def test_an_official_source_on_main_skips_an_unchanged_commit(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(
                _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        raise AssertionError(f"an unchanged official commit on main must not be fetched: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, entries=_entry("official", _official_manifest()))
    from provider_hub.state import load_state as _load, save_state
    state = _load()
    state["catalog_sources"]["official"]["resolved_commit"] = _MAIN
    state["catalog_sources"]["official"]["last_checked_at"] = _OLD_CHECK
    save_state(state)
    monkeypatch.setattr("provider_hub.service.utcnow_iso", lambda: _FROZEN_NOW)

    # The official skip's kept entry still counts toward the refresh summary.
    assert refresh_catalog()["entries"] == 1

    assert calls == [_OFFICIAL_AD]
    official = load_state()["catalog_sources"]["official"]
    assert official["last_error"] is None
    # The official skip records a fresh check at the current time.
    assert official["last_checked_at"] == _FROZEN_NOW
    assert (official["trusted"], official["trust_reason"]) == (True, "default branch")
    assert list(load_state()["catalog_entries"]) == ["official:examplehub:1.0.0"]


def test_an_unchanged_refresh_with_a_trusted_install_costs_one_advertisement(
    tmp_path, monkeypatch
):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state, save_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(
                _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        raise AssertionError(f"an unchanged commit must not be fetched: {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, entries=_entry("official", _official_manifest()),
               installations=_installed(_official_manifest(), trusted=True))
    state = load_state()
    official = state["catalog_sources"]["official"]
    official["resolved_commit"] = _MAIN
    official["provider_ids"] = ["examplehub"]
    state["trust_cache"] = {
        "heads": {"main": _MAIN, "beta": _BETA},
        "answers": {_MAIN: {"main": True, "beta": False}},
    }
    save_state(state)

    refresh_catalog()

    # The skip means no raw fetch, and the installed-trust recheck reuses the
    # advertisement the resolve step already read: one request in total.
    assert calls == [_OFFICIAL_AD]
    official = load_state()["catalog_sources"]["official"]
    assert official["last_error"] is None
    assert official["trusted"] is True


def test_a_community_advertisement_never_feeds_official_ancestry(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state, save_state

    # Only the official repository's advertisement may answer the ancestry
    # check. A community source that sorts after the official one and
    # advertises its own main and beta must not become the heads the
    # installed-trust recheck compares against.
    community_ad = "https://github.com/zcommunity/catalog/info/refs?service=git-upload-pack"
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(_git_advertisement([
                (_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        if url == community_ad:
            return _AdvertisementResponse(_git_advertisement([
                ("a" * 40, "refs/heads/main"), ("b" * 40, "refs/heads/beta")]))
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(
        tmp_path, monkeypatch,
        sources=_community_source("zcommunity", resolved_commit="a" * 40),
        entries={**_entry("official", _official_manifest()),
                 **_entry("zcommunity", _manifest(provider_id="communityhub", name="Community Hub"))},
        installations=_installed(_official_manifest(), trusted=True),
    )
    state = load_state()
    state["catalog_sources"]["official"]["resolved_commit"] = _MAIN
    state["trust_cache"] = {
        "heads": {"main": _MAIN, "beta": _BETA},
        "answers": {_MAIN: {"main": True, "beta": False}},
    }
    save_state(state)

    # Each source's skip counts exactly its own kept entry, so the community
    # advertisement adds one to the official source's one.
    assert refresh_catalog()["entries"] == 2

    # Both sources skip on their stored commits, so the refresh reads each
    # advertisement once: no raw catalog fetch and no compare call, whose
    # answers the trust cache already holds for the current official heads.
    # The install stays trusted and the official heads stay official.
    assert calls == [_OFFICIAL_AD, community_ad]
    after = load_state()
    assert after["installations"]["examplehub"]["trusted"] is True
    assert after["trust_cache"]["heads"] == {"main": _MAIN, "beta": _BETA}


def test_an_untrusted_official_commit_never_skips_the_published_ids_rebuild(
    tmp_path, monkeypatch
):
    from provider_hub import service
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state, save_state

    # The last trusted refresh published examplehub. Then the source sat on
    # a feature dev_ref at a commit outside main and beta, untrusted, and the
    # repository fast-forwarded that commit onto main before the dev_ref was
    # cleared.
    fast_forwarded = _OLD_MAIN
    raw_url = (f"https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"
               f"{fast_forwarded}/catalog.json")
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(_git_advertisement([
                (fast_forwarded, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        if url == raw_url:
            return _GitHubAnswer({"providers": [
                {"manifest": _official_manifest(commit=fast_forwarded)},
                {"manifest": _official_manifest("newhub", commit=fast_forwarded)},
            ]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, entries=_entry("official", _official_manifest()))
    state = load_state()
    official = state["catalog_sources"]["official"]
    official["resolved_commit"] = fast_forwarded
    official["trusted"] = False
    official["trust_reason"] = "outside main and beta"
    official["provider_ids"] = ["examplehub"]
    save_state(state)

    refresh_catalog()

    # The dev_ref is gone and main's head equals the stored commit, but the
    # stored verdict is untrusted, so the skip is refused: the catalog is
    # fetched again and the id the fast-forwarded commit published is
    # reserved again.
    official = load_state()["catalog_sources"]["official"]
    assert calls == [_OFFICIAL_AD, raw_url]
    assert official["trusted"] is True
    assert official["provider_ids"] == ["examplehub", "newhub"]
    assert "newhub" in service._reserved_provider_ids(load_state())


@pytest.mark.parametrize("dev_ref, advertised", [
    # A branch name resolving through the advertisement: the pinned-SHA guard
    # cannot be what blocks this skip.
    ("feat/x", [(_OLD_MAIN, "refs/heads/feat/x"),
                (_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]),
    (_OLD_MAIN, [(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]),
])
def test_an_official_source_with_a_dev_ref_never_skips_the_fetch(
    tmp_path, monkeypatch, dev_ref, advertised
):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state, save_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement(advertised))
        if url.startswith(_OFFICIAL_REPO_API + "/compare/"):
            return _GitHubAnswer({"status": "behind"})
        if url.startswith("https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, dev_ref=dev_ref,
               entries=_entry("official", _official_manifest(commit=_OLD_MAIN)))
    state = load_state()
    state["catalog_sources"]["official"]["resolved_commit"] = _OLD_MAIN
    save_state(state)

    refresh_catalog()

    # A dev_ref never skips, branch name or pinned commit alike: the catalog
    # is fetched again at the stored commit, the trust verdict is re-derived
    # from the graph, and the recheck reuses the advertisement the resolve
    # step read, so it too costs a single advertisement request.
    assert any(url.startswith("https://raw.githubusercontent.com/") for url in calls)
    assert any("/compare/" in url for url in calls)
    assert calls.count(_OFFICIAL_AD) == 1
    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (True, "ancestor of main")


def test_ancestry_heads_come_from_the_ref_advertisement(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    def fake_get(url, timeout, **_kwargs):
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(
                _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        if url.startswith(_OFFICIAL_REPO_API + "/compare/"):
            return _GitHubAnswer({"status": "behind"})
        if url.startswith("https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, dev_ref=_OLD_MAIN)

    refresh_catalog()

    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (True, "ancestor of main")


def test_a_one_branch_advertisement_cannot_verify_trust(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([(_MAIN, "refs/heads/main")]))
        if url.startswith("https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, dev_ref=_OLD_MAIN)

    refresh_catalog()

    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (
        False, "could not verify: GitHub did not list main and beta")


@pytest.mark.parametrize("failure, expected", [
    # github.com answers an anonymous request for a missing or private
    # repository with 401, so it lists no branches, like a 404.
    (401, "could not verify: GitHub did not list main and beta"),
    (404, "could not verify: GitHub did not list main and beta"),
    (403, "could not verify: rate limited"),
    (429, "could not verify: rate limited"),
    (503, "could not verify: GitHub returned HTTP 503"),
    ("timeout", "could not verify: GitHub could not be reached"),
    ("reset", "could not verify: GitHub could not be reached"),
    ("malformed", "could not verify: GitHub could not be reached"),
])
def test_ancestry_failures_keep_the_base_reason_strings(tmp_path, monkeypatch, failure, expected):
    import requests
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    def fake_get(url, timeout, **_kwargs):
        if url == _OFFICIAL_AD:
            if failure == "timeout":
                raise requests.exceptions.Timeout("advertisement timed out")
            if failure == "reset":
                raise requests.exceptions.ConnectionError("connection reset")
            if failure == "malformed":
                return _AdvertisementResponse(b"not an advertisement", content_type="text/html")
            return _AdvertisementResponse(status_code=failure)
        if url.startswith("https://raw.githubusercontent.com/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, dev_ref=_OLD_MAIN)

    refresh_catalog()

    official = load_state()["catalog_sources"]["official"]
    assert (official["trusted"], official["trust_reason"]) == (False, expected)


def test_updating_a_dev_ref_keeps_the_resolved_commit_and_the_skip(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog, update_catalog_source
    from provider_hub.state import load_state

    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _COMMUNITY_AD:
            return _AdvertisementResponse(_git_advertisement([
                ("c" * 40, "refs/heads/feat/x"),
                ("c" * 40, "refs/heads/feat/y"),
            ]))
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    community = _community_source("community")
    community["community"]["dev_ref"] = "feat/x"
    _hub_state(tmp_path, monkeypatch, sources=community, entries=_entry("community", _manifest()))

    # A dev_ref change keeps the stored commit: update_catalog_source cannot
    # change the source URL, and raw content at a fixed commit never changes,
    # so a new ref that resolves to the same commit still skips, and the
    # official trust input (previous_commit) keeps its value.
    update_catalog_source("community", dev_ref="feat/y")
    assert load_state()["catalog_sources"]["community"]["resolved_commit"] == "c" * 40

    refresh_catalog(source_ids={"community"})

    assert calls == [_COMMUNITY_AD]
    assert load_state()["catalog_sources"]["community"]["last_error"] is None


def test_an_official_source_on_an_untrusted_url_ref_never_skips(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state, save_state

    staging = "9" * 40
    calls = []

    def fake_get(url, timeout, **_kwargs):
        calls.append(url)
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(_git_advertisement([(staging, "refs/heads/staging")]))
        if url.startswith("https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    # load_state rebuilds the official record from this constant, and drops
    # any stored url, so the only way its ref is not a trusted branch is the
    # official catalog itself moving off main or beta.
    monkeypatch.setattr(
        "provider_hub.state.OFFICIAL_CATALOG_URL",
        "https://github.com/LavX/bazarr-provider-catalog/blob/staging/catalog.json")
    _hub_state(tmp_path, monkeypatch, entries=_entry("official", _official_manifest()))
    state = load_state()
    state["catalog_sources"]["official"]["resolved_commit"] = staging
    save_state(state)

    refresh_catalog()

    # The URL's ref is not a trusted branch, so the skip never applies: the
    # catalog is fetched again at the same commit.
    assert f"https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/{staging}/catalog.json" in calls
    assert load_state()["catalog_sources"]["official"]["last_error"] is None


def test_compare_calls_still_run_for_a_commit_not_yet_checked(tmp_path, monkeypatch):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    compares = []

    def fake_get(url, timeout, **_kwargs):
        if url == _OFFICIAL_AD:
            return _AdvertisementResponse(
                _git_advertisement([(_MAIN, "refs/heads/main"), (_BETA, "refs/heads/beta")]))
        if url.startswith(_OFFICIAL_REPO_API + "/compare/"):
            compares.append(url)
            return _GitHubAnswer({"status": "behind"})
        if url.startswith("https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"):
            return _GitHubAnswer({"providers": [{"manifest": _official_manifest()}]})
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)
    _hub_state(tmp_path, monkeypatch, dev_ref=_OLD_MAIN)

    # The heads did not move between the two refreshes; the second refresh
    # still re-asks nothing, but a commit never checked before is compared.
    refresh_catalog()
    assert compares
    saved = load_state()
    saved["trust_cache"]["answers"].pop(_OLD_MAIN)
    from provider_hub.state import save_state
    save_state(saved)
    compares.clear()
    refresh_catalog()

    assert compares
