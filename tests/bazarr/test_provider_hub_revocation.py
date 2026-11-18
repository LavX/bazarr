# coding=utf-8
"""The revocation list of the signed catalog: what it blocks, what it holds back and what it never trusts.

Every key is generated for the test, as in the signed feed tests.
"""
import json
from datetime import timedelta

import pytest

from plugin_platform_helpers import service_manifest, verified_official_source, write_state
from signed_feed_helpers import HEAD, SignedWorld

PLUGIN = "revokeme"
OTHER = "bystander"
REASON = "It sends credentials to the wrong host."


def _plugin(plugin_id=PLUGIN, version="1.0.0", **fields):
    return service_manifest(plugin_id, version, **fields)


def _entry(manifest, source="official"):
    return {"source": source, "source_name": source, "provider_id": manifest["id"], "name": manifest["name"],
            "version": manifest["version"], "trusted": source == "official", "manifest": manifest,
            "catalog_list": "plugins"}


def _row(manifest, enabled=True, **fields):
    row = {"provider_id": manifest["id"], "kind": manifest["kind"], "name": manifest["name"],
           "active_version": manifest["version"], "state": "active", "pending_restart": False,
           "manifest": manifest, "enabled": enabled, "source_id": "official", "source_bound": True,
           "trusted": True}
    row.update(fields)
    return {manifest["id"]: row}


def _list(*items):
    return {"channel": "main", "signed_version": 100, "entries": list(items)}


def _stored_world(tmp_path, monkeypatch, manifests=(), installations=None, revocations=None):
    """A state document with a verified official source that carries ``revocations``."""
    entries = {f"official:plugins:{m['id']}:{m['version']}": _entry(m) for m in manifests}
    source = verified_official_source(catalog_signature="verified", catalog_channel="main",
                                      catalog_signed_version=100, catalog_key_file_version=1)
    if revocations is not None:
        source["catalog_revocations"] = revocations
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json", catalog_sources={"official": source}, catalog_entries=entries,
        installations=installations or {})))


def _state():
    from provider_hub.state import load_state

    return load_state()


def _rows():
    return _state()["installations"]


# --- the list itself --------------------------------------------------------------------------------------


def test_a_missing_list_revokes_nothing_and_a_misshapen_one_is_an_error():
    from provider_hub import revocation

    assert revocation.parse(None) == ()
    assert revocation.parse([]) == ()
    for bad in ({}, "x", [1], [{}], [{"id": ""}], [{"id": " x"}], [{"id": "x", "version": 3}],
                [{"id": "x", "revoked_at": "yesterday"}], [{"id": "x"}] * (revocation.MAX_ENTRIES + 1)):
        with pytest.raises(revocation.RevocationFormatError):
            revocation.parse(bad)


def test_an_entry_without_a_version_revokes_every_release_and_one_with_a_version_only_that_release():
    from provider_hub import revocation

    entries = revocation.parse([
        {"id": "all"}, {"id": "one", "version": "1.2.0", "reason": REASON, "revoked_at": "2026-12-01T00:00:00Z"}])

    assert revocation.lookup(entries, "all", "9.9.9") and revocation.lookup(entries, "all")
    assert revocation.lookup(entries, "one", "1.2.0").reason == REASON
    assert revocation.lookup(entries, "one", "1.2.1") is None
    # With no release in hand, only a revocation of the whole plugin counts.
    assert revocation.lookup(entries, "one") is None
    assert revocation.lookup(entries, "other", "1.2.0") is None


def test_a_release_is_matched_by_version_order_so_a_respelling_does_not_slip_past():
    from provider_hub import revocation

    entries = revocation.parse([{"id": "one", "version": "1.2"}])

    assert revocation.lookup(entries, "one", "1.2.0")
    assert revocation.lookup(entries, "one", "v1.2.0+build5")
    assert revocation.lookup(entries, "one", "1.2.1") is None
    assert revocation.lookup(entries, "one", "1.2.0-rc.1") is None


def test_the_reason_is_flattened_cut_and_the_texts_name_the_release():
    from provider_hub import revocation

    entry = revocation.parse([{"id": "one", "version": "1.2.0", "reason": "line one\n\n  line   two " + "x" * 400}])[0]

    assert "\n" not in entry.reason and entry.reason.startswith("line one line two xxx") and len(entry.reason) == 300
    assert revocation.refusal(entry, "One", "1.2.0", "installed").startswith("Version 1.2.0 of One was revoked by")
    assert revocation.refusal(entry, "One", "1.2.0", "switched on").endswith("so it cannot be switched on.")
    assert "Update it to a newer version" in revocation.held_back(entry, "One", "1.2.0")
    whole = revocation.parse([{"id": "one"}])[0]
    assert revocation.held_back(whole, "One", "1.2.0").startswith("Every version of One was revoked")


def test_a_stored_list_is_read_back_and_an_unreadable_stored_entry_is_skipped():
    from provider_hub import revocation

    stored = revocation.to_record("main", 7, revocation.parse([{"id": "a", "version": "1.0.0"}, {"id": "b"}]))

    assert [item.plugin_id for item in revocation.from_source({"catalog_revocations": stored})] == ["a", "b"]
    stored["entries"].append({"nonsense": True})
    assert len(revocation.from_source({"catalog_revocations": stored})) == 2
    assert revocation.from_source({}) == () and revocation.from_source(None) == ()


# --- installing, updating and listing ----------------------------------------------------------------------


def test_a_revoked_release_is_marked_in_the_catalog_and_cannot_be_installed(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    _stored_world(tmp_path, monkeypatch, [_plugin(version="1.0.0"), _plugin(version="1.1.0"), _plugin(OTHER)],
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))

    marked = {(entry["provider_id"], entry["version"]): entry.get("blocked_reason")
              for entry in service.list_catalog()["entries"]}
    assert marked[(PLUGIN, "1.0.0")] == (
        f"Version 1.0.0 of {PLUGIN} was revoked by the official catalog: It sends credentials to the wrong host, "
        "so it cannot be installed.")
    assert marked[(PLUGIN, "1.1.0")] is None and marked[(OTHER, "1.0.0")] is None
    with pytest.raises(service.ProviderHubInstallError, match="was revoked by the official catalog"):
        service.resolve_catalog_install("official", PLUGIN, "1.0.0")
    with pytest.raises(service.ProviderHubInstallError, match="was revoked by the official catalog"):
        service.stage_install(_plugin(version="1.0.0"), source_id="official")
    assert service.resolve_catalog_install("official", PLUGIN, "1.1.0")["version"] == "1.1.0"


def test_a_plugin_revoked_whole_cannot_be_installed_at_any_version_or_from_any_source(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    _stored_world(tmp_path, monkeypatch, [_plugin(version="1.0.0"), _plugin(version="2.0.0")],
                  revocations=_list({"id": PLUGIN}))

    for version in ("1.0.0", "2.0.0"):
        with pytest.raises(service.ProviderHubInstallError, match="Every version of"):
            service.resolve_catalog_install("official", PLUGIN, version)
    # A package nobody lists is refused too: the install core checks, whichever way it was asked for.
    with pytest.raises(service.ProviderHubInstallError, match="Every version of"):
        service.stage_install(_plugin(version="3.0.0"))
    assert service.installable_catalog_plugins(service_kind.name) == []


def test_a_revoked_release_is_never_offered_as_an_update_and_the_next_one_still_is(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(
        tmp_path, monkeypatch, [installed, _plugin(version="1.1.0"), _plugin(version="1.2.0")],
        installations=_row(installed),
        revocations=_list({"id": PLUGIN, "version": "1.2.0", "reason": REASON}))

    assert service.offered_update_version(PLUGIN) == "1.1.0"
    message = service.check_updates()["message"]
    assert "1.0.0 -> 1.1.0" in message and "1.2.0" not in message.split("(newer version")[0]
    assert "Version 1.2.0 of" in message and "cannot be updated to" in message


def test_an_update_that_is_revoked_says_so_when_it_is_asked_for(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed, _plugin(version="1.1.0")], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.1.0", "reason": REASON}))

    assert service.offered_update_version(PLUGIN) is None
    result = service.apply_update(PLUGIN)

    assert "Version 1.1.0 of" in result["last_error"] and "cannot be updated to" in result["last_error"]
    assert _rows()[PLUGIN]["active_version"] == "1.0.0" and not _rows()[PLUGIN].get("staged_version")


# --- switching on ------------------------------------------------------------------------------------------


def test_a_revoked_plugin_cannot_be_switched_on_from_the_hub_or_from_the_settings(
        tmp_path, monkeypatch, service_kind):
    from app import config
    from provider_hub import service
    from provider_hub.state import blocked_installations

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))

    with pytest.raises(ValueError, match="cannot be switched on"):
        service.update_provider(PLUGIN, enabled=True)
    assert service.revoked_switch_on_refusal(PLUGIN).startswith("Version 1.0.0 of")
    # The settings save names the same refusal. A plugin that is switched off may stay off.
    monkeypatch.setattr(config.settings.general, "enabled_providers", [])
    with pytest.raises(config.ValidationError, match="was revoked by the official catalog"):
        config._refuse_provider_switch_with_dependencies([("settings-general-enabled_providers", [PLUGIN])])
    assert service.update_provider(PLUGIN, enabled=False)["provider_id"] == PLUGIN
    assert blocked_installations(kind=None) == []


def test_a_plugin_whose_active_release_is_not_revoked_switches_on_as_before(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.1.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))

    assert service.revoked_switch_on_refusal(PLUGIN) is None
    assert service.update_provider(PLUGIN, enabled=True)["provider_id"] == PLUGIN


# --- boot --------------------------------------------------------------------------------------------------


def test_an_installed_revoked_release_is_held_back_at_boot_with_its_reason(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import running_installations

    revoked, fine = _plugin(version="1.0.0"), _plugin(OTHER)
    _stored_world(tmp_path, monkeypatch, [], installations={**_row(revoked), **_row(fine)},
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))

    service.activate_staged_installations()

    row = _rows()[PLUGIN]
    assert row["blocked"] is True and row["revoked"] is True
    assert row["blocked_reason"] == row["last_error"]
    assert row["last_error"].startswith(
        f"Version 1.0.0 of {PLUGIN.title()} was revoked by the official catalog: It sends credentials to the wrong host. ")
    assert row["last_error"].endswith("Update it to a newer version when one is listed, or remove it.")
    assert "blocked" not in _rows()[OTHER] and "revoked" not in _rows()[OTHER]
    assert [item.provider_id for item in running_installations(kind=None)] == [OTHER]


def test_a_revoked_release_that_is_switched_off_still_shows_why_and_a_second_check_changes_nothing(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN}))

    assert service.enforce_activation_needs() is True
    assert _rows()[PLUGIN]["last_error"] and _rows()[PLUGIN]["last_error"] == _rows()[PLUGIN]["blocked_reason"]
    assert service.enforce_activation_needs() is False


def test_a_release_the_list_stops_naming_is_released_at_boot(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))
    service.enforce_activation_needs()
    assert _rows()[PLUGIN]["blocked"] is True

    def lift(state):
        state["catalog_sources"]["official"]["catalog_revocations"] = _list()

    from provider_hub.state import mutate_state
    mutate_state(lift)
    assert service.enforce_activation_needs() is True

    row = _rows()[PLUGIN]
    assert "blocked" not in row and "revoked" not in row and row["last_error"] is None


def test_a_plugin_that_relies_on_a_revoked_one_is_held_back_with_it(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    capability = "demo_parent.backend.v1"
    parent = _plugin("parent", provides=[capability])
    user = _plugin("user", requires=[capability])
    _stored_world(tmp_path, monkeypatch, [], installations={
        **_row(parent), **_row(user, supplied_by={capability: "parent"})},
        revocations=_list({"id": "parent"}))

    service.activate_staged_installations()

    assert _rows()["parent"]["revoked"] is True
    assert _rows()["user"]["blocked"] is True and "parent (demo_parent.backend.v1)" in _rows()["user"]["last_error"]
    assert not _rows()["user"].get("revoked")


def test_a_staged_release_that_is_revoked_does_not_activate_and_the_active_one_stays(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    active = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0"), "staged_path": "/nowhere", "staged_python_path": "/nowhere"}
    _stored_world(tmp_path, monkeypatch, [], installations=_row(active, **staged),
                  revocations=_list({"id": PLUGIN, "version": "1.1.0", "reason": REASON}))

    assert service.activate_staged_installations() == []

    row = _rows()[PLUGIN]
    assert row["active_version"] == "1.0.0" and row["state"] == "active" and row["pending_restart"] is False
    assert row["staged_version"] is None and row["staged_manifest"] is None
    assert row["last_error"].startswith("Version 1.1.0 of") and "cannot be installed" in row["last_error"]
    assert "blocked" not in row


def test_a_first_install_that_was_revoked_while_staged_fails_instead_of_activating(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    first = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "active_version": None, "staged_version": "1.0.0",
              "staged_manifest": first, "staged_path": "/nowhere", "staged_python_path": "/nowhere"}
    _stored_world(tmp_path, monkeypatch, [], installations=_row(first, **staged),
                  revocations=_list({"id": PLUGIN}))

    assert service.activate_staged_installations() == []

    row = _rows()[PLUGIN]
    assert row["state"] == "failed" and row["staged_version"] is None and not row["active_version"]
    assert "Every version of" in row["last_error"]


# --- the signed feed carries the list ----------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch, service_kind):
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json")))
    world = SignedWorld(monkeypatch)
    # The commit the test manifests name is on the official branches, so installs made from it stay trusted.
    world.ancestors.add("b" * 40)
    return world


def _refresh():
    from provider_hub.service import refresh_catalog

    refresh_catalog(source_ids={"official"})


def _official():
    return _state()["catalog_sources"]["official"]


def _publish(world, *, version=100, revocations=None, commit=HEAD, plugins=(), **kwargs):
    extra = {} if revocations is None else {"revocations": revocations}
    plugins = plugins or [_plugin(version="1.0.0"), _plugin(version="1.1.0")]
    return world.publish(commit=commit, version=version, plugins=plugins, **extra, **kwargs)


def _install_row(manifest, **fields):
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].update(_row(manifest, **fields)))


def _new_head(world, commit):
    world.ancestors.add(HEAD)
    world.heads["main"] = commit
    world.heads["beta"] = commit


def test_a_verified_catalog_stores_its_list_and_holds_an_installed_release_back_at_the_refresh(world):
    from provider_hub import service
    from provider_hub.state import running_installations

    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])

    _refresh()

    stored = _official()["catalog_revocations"]
    assert stored["channel"] == "main" and stored["signed_version"] == 100
    assert stored["entries"] == [{"id": PLUGIN, "version": "1.0.0", "reason": REASON}]
    row = _rows()[PLUGIN]
    assert row["blocked"] is True and row["revoked"] is True and "was revoked by the official catalog" in row["last_error"]
    assert running_installations(kind=None) == []
    # The revoked entry is listed with its reason and the next one is still installable.
    marked = {entry["version"]: entry.get("blocked_reason") for entry in service.list_catalog()["entries"]}
    assert marked["1.0.0"] and marked["1.1.0"] is None
    assert service.offered_update_version(PLUGIN) == "1.1.0"


def test_a_later_catalog_that_drops_an_entry_lifts_it_and_one_that_adds_an_entry_holds_it(world):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    assert _rows()[PLUGIN]["blocked"] is True

    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=[], source_commit="e" * 40)
    _refresh()

    assert _official()["catalog_revocations"]["entries"] == []
    row = _rows()[PLUGIN]
    assert "blocked" not in row and "revoked" not in row and row["last_error"] is None

    _new_head(world, "f" * 40)
    world.ancestors.add("e" * 40)
    _publish(world, commit="f" * 40, version=102, revocations=[{"id": PLUGIN}], source_commit="f" * 40)
    _refresh()

    assert _rows()[PLUGIN]["blocked"] is True


def test_a_catalog_that_fails_its_check_is_refused_whole_and_the_last_verified_list_stays_in_force(world):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])
    _refresh()
    held = _official()["catalog_revocations"]

    # A newer head whose body drops the revocation but whose signature does not match it.
    _new_head(world, "e" * 40)
    body = world.catalog_body("main", version=101, plugins=[_plugin(version="1.0.0")], revocations=[],
                              source_commit="e" * 40)
    forged = body.replace(b'"revocations": []', b'"revocations":[]')
    world.publish(commit="e" * 40, body=forged, envelope=world.sign(body + b" "))

    _refresh()

    assert "signature" in _official()["last_error"].lower()
    assert _official()["catalog_revocations"] == held
    assert _rows()[PLUGIN]["blocked"] is True


def test_a_replayed_older_catalog_cannot_bring_a_revoked_release_back(world):
    _install_row(_plugin(version="1.0.0"))
    old = _publish(world, version=100, revocations=[])
    _new_head(world, "e" * 40)
    world.files[("e" * 40, "signed/main/catalog.json")] = old
    _publish(world, commit="e" * 40, version=200, revocations=[{"id": PLUGIN, "version": "1.0.0"}],
             source_commit="e" * 40)
    _refresh()
    assert _rows()[PLUGIN]["blocked"] is True

    # The repository now serves the older signed pair, which has no revocation: its counter is behind.
    _new_head(world, "d" * 40)
    world.ancestors.add("e" * 40)
    world.publish(commit="d" * 40, body=old, envelope=world.sign(old))
    _refresh()

    assert "older" in _official()["last_error"] or "replayed" in _official()["last_error"]
    assert _rows()[PLUGIN]["blocked"] is True


def test_a_list_in_an_unsigned_legacy_catalog_is_never_read(world):
    _install_row(_plugin(version="1.0.0"))
    document = {"schema_version": 1, "name": "Official", "providers": [],
                "revocations": [{"id": PLUGIN}], "plugins": []}
    world.files[(HEAD, "catalog.json")] = json.dumps(document).encode("utf-8")

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "unsigned (legacy)" and "catalog_revocations" not in source
    assert "blocked" not in _rows()[PLUGIN]


def test_an_unsigned_legacy_read_leaves_the_list_of_the_last_verified_catalog_alone(world):
    from provider_hub.service import update_catalog_source

    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN}])
    _refresh()
    held = _official()["catalog_revocations"]
    # A ref that reads the legacy catalog (and is not trusted once the host has latched) changes nothing it holds.
    world.legacy(providers=[])
    update_catalog_source("official", dev_ref="feature-x")
    world.monkeypatch.setattr(
        "provider_hub.service._resolve_github_ref", lambda owner, repo, ref, advertisement_sink=None: HEAD)

    _refresh()

    assert _official()["catalog_revocations"] == held
    assert _rows()[PLUGIN]["blocked"] is True


@pytest.mark.parametrize("bad", ["everything", [{"version": "1.0.0"}], [{"id": PLUGIN, "revoked_at": "soon"}]])
def test_a_list_that_cannot_be_read_refuses_the_whole_catalog_and_keeps_the_last_good_one(world, bad):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": "somethingelse"}])
    _refresh()
    held = _official()["catalog_revocations"]
    before = {key for key in _state()["catalog_entries"]}

    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=bad, source_commit="e" * 40,
             plugins=[_plugin(OTHER)])
    _refresh()

    assert "revocation list could not be read" in _official()["last_error"]
    assert _official()["catalog_revocations"] == held
    assert set(_state()["catalog_entries"]) == before


def test_a_host_that_verified_a_catalog_before_the_list_was_kept_reads_it_again_on_an_unchanged_head(world):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN}])
    _refresh()

    def forget(state):
        state["catalog_sources"]["official"].pop("catalog_revocations")

    from provider_hub.state import mutate_state
    mutate_state(forget)
    asked = len(world.fetched("signed/main/catalog.json"))

    _refresh()

    assert len(world.fetched("signed/main/catalog.json")) > asked
    assert _rows()[PLUGIN]["blocked"] is True


def test_a_staged_release_the_refresh_finds_revoked_is_dropped_and_the_active_one_keeps_running(world):
    active = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0"), "staged_path": "/nowhere", "staged_python_path": "/nowhere"}
    _install_row(active, **staged)
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.1.0", "reason": REASON}])

    _refresh()

    row = _rows()[PLUGIN]
    assert row["state"] == "active" and row["active_version"] == "1.0.0" and row["pending_restart"] is False
    assert row["staged_version"] is None and "Version 1.1.0 of" in row["last_error"]
    assert "blocked" not in row


def test_a_refresh_leaves_every_plugin_the_list_does_not_concern_exactly_as_it_was(world):
    capability = "demo_parent.backend.v1"
    parent = _plugin("parent", provides=[capability])
    user = _plugin("user", requires=[capability])
    # The supplier is flagged for repair, which the boot check would hold its dependent back for. A refresh does
    # not judge that.
    _install_row(parent, needs_repair=True)
    _install_row(user, supplied_by={capability: "parent"})
    _publish(world, revocations=[{"id": PLUGIN}], plugins=[_plugin(version="1.0.0")])

    _refresh()

    assert "blocked" not in _rows()["user"] and "blocked" not in _rows()["parent"]


def test_the_list_is_applied_whichever_source_an_install_came_from(world):
    # An id the official catalog revokes is revoked here, so a community copy of it cannot be installed either.
    from provider_hub import service

    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()

    revoked = service._install_refusal(_state(), "community", PLUGIN, service.SUBTITLE_PROVIDER, "1.0.0")
    assert revoked.startswith("Version 1.0.0 of") and "revoked by the official catalog" in revoked
    other = service._install_refusal(_state(), "community", PLUGIN, service.SUBTITLE_PROVIDER, "9.9.9")
    assert other is None or "revoked" not in other


def test_a_host_with_no_stored_list_refuses_and_holds_nothing(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed))

    assert service.enforce_activation_needs() is False
    assert service.resolve_catalog_install("official", PLUGIN, "1.0.0")["version"] == "1.0.0"
    assert "blocked" not in _rows()[PLUGIN]


def test_the_clock_does_not_decide_a_revocation(world):
    # A list stays in force after the catalog that carried it has expired: only a newer signed catalog lifts it.
    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    world.now = world.now + timedelta(days=200)

    _refresh()

    assert _rows()[PLUGIN]["blocked"] is True
    from provider_hub import service
    assert service.revoked_switch_on_refusal(PLUGIN)
