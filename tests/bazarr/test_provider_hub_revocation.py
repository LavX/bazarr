# coding=utf-8
"""The revocation list of the signed catalog: what it blocks, what it holds back and what it never trusts.

Every key is generated for the test, as in the signed feed tests.
"""
import io
import json
import zipfile
from datetime import timedelta

import pytest

from plugin_platform_helpers import provider_manifest_v1, service_manifest, verified_official_source, write_state
from signed_feed_helpers import HEAD, SignedWorld

PLUGIN = "revokeme"
NAME = PLUGIN.title()
OTHER = "bystander"
REASON = "It sends credentials to the wrong host."
LONG_REASON = ("It sends the user's credentials to a host the plugin's author does not control, and the "
               "author has not answered. Remove it from every Bazarr+ install you run until a fixed release.")
LIMIT = 200


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


def _list(*items, version=100, channel="main", held=None):
    """The record a verified refresh keeps on the official source: each channel's list and its signed_version."""
    channels = dict((held or {}).get("channels") or {})
    channels[channel] = {"signed_version": version, "entries": list(items)}
    return {"channels": channels}


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


@pytest.mark.parametrize("bad", [
    {}, "x", [1], [{}], [{"id": ""}], [{"id": " revokeme"}], [{"id": "Revokeme"}], [{"id": "a"}],
    [{"id": "re-voke"}], [{"id": "x" * 65}],
    [{"id": PLUGIN, "version": 3}], [{"id": PLUGIN, "version": ""}], [{"id": PLUGIN, "version": "   "}],
    [{"id": PLUGIN, "version": None}], [{"id": PLUGIN, "version": "1" * 65}],
    [{"id": PLUGIN, "revoked_at": "yesterday"}], [{"id": PLUGIN, "reason": 5}],
    # A field this host does not know could one day narrow an entry: it is never read as "every release".
    [{"id": PLUGIN, "below": "2.0.0"}], [{"id": PLUGIN, "versions": ["1.0.0"]}],
    [{"id": PLUGIN}] * 2001,
])
def test_a_misshapen_list_is_an_error_and_never_read_as_revoking_everything(bad):
    from provider_hub import revocation

    with pytest.raises(revocation.RevocationFormatError):
        revocation.parse(bad)


def test_a_missing_or_empty_list_revokes_nothing_and_a_blank_reason_is_none():
    from provider_hub import revocation

    assert revocation.parse(None) == () and revocation.parse([]) == ()
    assert revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": "  "}])[0].reason is None
    assert len(revocation.parse([{"id": PLUGIN}] * 2000)) == 2000


def test_an_entry_without_a_version_revokes_every_release_and_one_with_a_version_only_that_release():
    from provider_hub import revocation

    entries = revocation.parse([
        {"id": "everything"},
        {"id": "single", "version": "1.2.0", "reason": REASON, "revoked_at": "2026-12-01T00:00:00Z"}])

    assert revocation.lookup(entries, "everything", "9.9.9") and revocation.lookup(entries, "everything")
    assert revocation.lookup(entries, "single", "1.2.0").reason == REASON
    assert revocation.lookup(entries, "single", "1.2.1") is None
    # With no release in hand, only a revocation of the whole plugin counts.
    assert revocation.lookup(entries, "single") is None
    assert revocation.lookup(entries, "other", "1.2.0") is None


@pytest.mark.parametrize("revoked, installed, same", [
    ("1.2", "1.2.0", True), ("1.2.0", "v1.2.0+build5", True),
    ("1.0.0rc1", "1.0.0-rc.1", True), ("1.0.0-rc.1", "1.0.0rc1", True), ("1.0.0-RC1", "1.0.0rc1", True),
    ("1.0.0-alpha.2", "1.0.0a2", True), ("1.0.0b1", "1.0.0-beta.1", True), ("1.0.0c1", "1.0.0-rc.1", True),
    ("1.0.0-rc", "1.0.0rc0", True), ("1.0.0.post1", "1.0.0.POST1", True),
    ("1.2.0", "1.2.1", False), ("1.2.0", "1.2.0-rc.1", False), ("1.0.0rc1", "1.0.0rc2", False),
    ("1.0.0rc1", "1.0.0b1", False), ("1.0.0-rc.1.2", "1.0.0-rc.1", False), ("1.0.0.post1", "1.0.0", False),
    ("nightly", "NIGHTLY", True), ("nightly", "1.0.0", False),
])
def test_a_release_is_matched_by_the_documented_rule_so_a_respelling_does_not_slip_past(revoked, installed, same):
    from provider_hub import revocation

    assert revocation.same_version(revoked, installed) is same
    entries = revocation.parse([{"id": PLUGIN, "version": revoked}])
    assert bool(revocation.lookup(entries, PLUGIN, installed)) is same


def _texts(entry, name=NAME, version="1.0.0"):
    from provider_hub import revocation

    return {
        "install": revocation.refusal(entry, name, version, "installed"),
        "update": revocation.refusal(entry, name, version, "offered as an update"),
        "switch on": revocation.refusal(entry, name, version, "switched on"),
        "held": revocation.held_back(entry, name, version, revocation.HELD),
        "running": revocation.held_back(entry, name, version, revocation.RUNNING),
        "staged": revocation.held_back(entry, name, version, revocation.STAGED),
    }


@pytest.mark.parametrize("reason", [None, "", REASON, "x" * 120, "y" * 300, LONG_REASON])
@pytest.mark.parametrize("version", ["1.0.0", None])
def test_every_text_fits_the_message_limit_and_always_ends_with_what_to_do_next(reason, version):
    from provider_hub import revocation

    fields = {"id": PLUGIN, **({"version": version} if version else {}), **({} if reason is None else {"reason": reason})}
    entry = revocation.parse([fields])[0]
    tails = {"install": "so it cannot be installed.", "update": "so it cannot be offered as an update.",
             "switch on": "so it cannot be switched on.",
             "held": "It will not start. Update it or remove it." if version else "It will not start. Remove it.",
             "running": ("Restart Bazarr+ to stop it, then update or remove it." if version
                         else "Restart Bazarr+ to stop it, then remove it."),
             "staged": "Restart Bazarr+ to apply the staged update."}
    for name, text in _texts(entry, version="1.0.0").items():
        assert len(text) <= LIMIT, (name, len(text))
        assert text.endswith(tails[name]), (name, text)
        assert "was revoked by the official catalog" in text
        assert NAME in text


def test_a_reason_that_does_not_fit_is_cut_with_an_ellipsis_and_one_that_fits_is_shown_whole():
    from provider_hub import revocation

    short = revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])[0]
    assert all("It sends credentials to the wrong host" in text and "..." not in text
               for text in _texts(short).values())
    realistic = revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": "z" * 120}])[0]
    texts = _texts(realistic)
    for name in ("held", "running", "staged"):
        assert "z..." in texts[name] and "z" * 120 not in texts[name], name
    for name in ("install", "update", "switch on"):
        assert "z" * 100 in texts[name], name
    cut = revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": LONG_REASON}])[0]
    for name, text in _texts(cut).items():
        assert "It sends the user's credentials" in text and "..." in text, name


def test_a_very_long_name_or_version_cannot_push_the_next_step_out_of_the_text():
    from provider_hub import revocation

    entry = revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])[0]
    for text in _texts(entry, name="N" * 300, version="1." * 30).values():
        assert len(text) <= LIMIT


def test_texts_read_plainly():
    from provider_hub import revocation

    entry = revocation.parse([{"id": PLUGIN, "version": "1.2.0", "reason": REASON}])[0]
    texts = _texts(entry, version="1.2.0")

    assert texts["install"] == (
        f"{NAME} 1.2.0 was revoked by the official catalog: It sends credentials to the wrong host, "
        "so it cannot be installed.")
    assert texts["update"].endswith("so it cannot be offered as an update.")
    assert texts["held"] == (
        f"{NAME} 1.2.0 was revoked by the official catalog: It sends credentials to the wrong host. "
        "It will not start. Update it or remove it.")
    whole = revocation.parse([{"id": PLUGIN}])[0]
    assert revocation.held_back(whole, NAME, "1.2.0").startswith(f"Every version of {NAME} was revoked")


def test_a_stored_list_is_read_back_by_channel_and_an_unreadable_stored_entry_is_skipped():
    from provider_hub import revocation

    held = revocation.to_record(None, "main", 7, revocation.parse([{"id": "aa", "version": "1.0.0"}, {"id": "bb"}]))
    held = revocation.to_record(held, "beta", 9, revocation.parse([{"id": "cc"}]))

    lists = revocation.from_source({"catalog_revocations": held})
    assert lists["main"].signed_version == 7 and [i.plugin_id for i in lists["main"].entries] == ["aa", "bb"]
    assert lists["beta"].signed_version == 9
    held["channels"]["main"]["entries"].append({"nonsense": True})
    assert len(revocation.from_source({"catalog_revocations": held})["main"].entries) == 2
    assert revocation.from_source({}) == {} and revocation.from_source(None) == {}
    # A channel of a catalog this host does not read is ignored.
    held["channels"]["other"] = {"signed_version": 1, "entries": [{"id": "dd"}]}
    assert set(revocation.from_source({"catalog_revocations": held})) == {"main", "beta"}


def test_of_two_copies_of_a_channel_the_higher_signed_version_counts_and_the_channels_are_unioned():
    from provider_hub import revocation

    def lists(**by_channel):
        return {channel: revocation.ChannelList(version, tuple(revocation.parse([{"id": i} for i in ids])))
                for channel, (version, ids) in by_channel.items()}

    database = lists(main=(100, []), beta=(50, ["old_beta"]))
    mirror = lists(main=(200, ["new_main"]), beta=(10, []))

    merged = revocation.effective(database, mirror)

    assert merged["main"].signed_version == 200 and merged["beta"].signed_version == 50
    assert {i.plugin_id for i in revocation.entries_of(merged)} == {"new_main", "old_beta"}


def test_the_mirror_keeps_the_higher_signed_version_per_channel_and_survives_a_bad_file(tmp_path, monkeypatch):
    from provider_hub import revocation

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    entries = revocation.parse([{"id": PLUGIN}])
    revocation.write_mirror({"main": revocation.ChannelList(200, entries)})
    revocation.write_mirror({"main": revocation.ChannelList(100, ()), "beta": revocation.ChannelList(5, ())})

    mirrored = revocation.read_mirror()
    assert mirrored["main"].signed_version == 200 and mirrored["main"].entries == entries
    assert mirrored["beta"].signed_version == 5
    assert (tmp_path / "plugins" / "feed-revocations.json").exists()
    (tmp_path / "plugins" / "feed-revocations.json").write_text("not json", encoding="utf-8")
    assert revocation.read_mirror() == {}


# --- installing, updating and listing ----------------------------------------------------------------------


def test_a_revoked_release_is_marked_in_the_catalog_and_cannot_be_installed(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    _stored_world(tmp_path, monkeypatch, [_plugin(version="1.0.0"), _plugin(version="1.1.0"), _plugin(OTHER)],
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))

    marked = {(entry["provider_id"], entry["version"]): entry.get("blocked_reason")
              for entry in service.list_catalog()["entries"]}
    assert marked[(PLUGIN, "1.0.0")] == (
        f"{NAME} 1.0.0 was revoked by the official catalog: It sends credentials to the wrong host, "
        "so it cannot be installed.")
    assert marked[(PLUGIN, "1.1.0")] is None and marked[(OTHER, "1.0.0")] is None
    with pytest.raises(service.ProviderHubInstallError, match="was revoked by the official catalog") as refused:
        service.resolve_catalog_install("official", PLUGIN, "1.0.0")
    assert str(refused.value).startswith(NAME)
    with pytest.raises(service.ProviderHubInstallError, match="was revoked by the official catalog"):
        service.stage_install(_plugin(version="1.0.0"), source_id="official")
    assert service.resolve_catalog_install("official", PLUGIN, "1.1.0")["version"] == "1.1.0"


def test_a_plugin_revoked_whole_cannot_be_installed_at_any_version_or_from_any_source(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    _stored_world(tmp_path, monkeypatch, [_plugin(version="1.0.0"), _plugin(version="2.0.0")],
                  revocations=_list({"id": PLUGIN}))

    for version in ("1.0.0", "2.0.0"):
        with pytest.raises(service.ProviderHubInstallError, match=f"Every version of {NAME}"):
            service.resolve_catalog_install("official", PLUGIN, version)
    # A package nobody lists is refused too: the install core checks, whichever way it was asked for.
    with pytest.raises(service.ProviderHubInstallError, match="Every version of"):
        service.stage_install(_plugin(version="3.0.0"))
    assert service.installable_catalog_plugins(service_kind.name) == []


def test_a_local_package_of_a_revoked_release_is_refused(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    manifest = provider_manifest_v1("localsub", "1.0.0")
    _stored_world(tmp_path, monkeypatch, [], revocations=_list({"id": "localsub", "version": "1.0.0"}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("provider.json", json.dumps(manifest))
        package.writestr("provider.py", "class ExampleProvider: pass\n")

    with pytest.raises(service.ProviderHubInstallError, match="Localsub 1.0.0 was revoked"):
        service.stage_install_local(buffer.getvalue())
    assert "localsub" not in _rows()


def test_the_startup_auto_install_skips_a_revoked_newest_release(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    older, newest = provider_manifest_v1("autosub", "1.0.0"), provider_manifest_v1("autosub", "1.1.0")
    entries = [{**_entry({**m, "id": m["provider_id"]}), "manifest": m, "catalog_list": "providers"}
               for m in (older, newest)]
    entries = {f"official:providers:autosub:{e['version']}": e for e in entries}
    source = verified_official_source(catalog_signature="verified", catalog_channel="main")
    source["catalog_revocations"] = _list({"id": "autosub", "version": "1.1.0", "reason": REASON})
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json", catalog_sources={"official": source}, catalog_entries=entries)))

    assert service._latest_official_catalog_manifest(_state(), "autosub")["version"] == "1.0.0"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "other.json", catalog_sources={"official": {
            **source, "catalog_revocations": _list({"id": "autosub"})}}, catalog_entries=entries)))
    assert service._latest_official_catalog_manifest(_state(), "autosub") is None


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
    assert "1.0.0 -> 1.1.0" in message and "1.2.0 -> " not in message
    refused = message.split("(newer version not offered for ")[1]
    assert f"{NAME} 1.2.0 was revoked by the official catalog" in refused
    assert "so it cannot be offered as an update." in refused and "updated to." not in refused


def test_an_update_that_is_revoked_says_so_when_it_is_asked_for(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed, _plugin(version="1.1.0")], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.1.0", "reason": LONG_REASON}))

    assert service.offered_update_version(PLUGIN) is None
    result = service.apply_update(PLUGIN)

    assert result["last_error"].startswith(f"{NAME} 1.1.0 was revoked by the official catalog")
    assert result["last_error"].endswith("so it cannot be offered as an update.") and len(result["last_error"]) <= LIMIT
    assert _rows()[PLUGIN]["active_version"] == "1.0.0" and not _rows()[PLUGIN].get("staged_version")


def test_the_install_and_listing_texts_fit_the_limit_with_a_long_reason(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    _stored_world(tmp_path, monkeypatch, [_plugin(version="1.0.0")],
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": LONG_REASON}))

    listed = service.list_catalog()["entries"][0]["blocked_reason"]
    with pytest.raises(service.ProviderHubInstallError) as refused:
        service.resolve_catalog_install("official", PLUGIN, "1.0.0")
    for text in (listed, str(refused.value)):
        assert len(text) <= LIMIT and text.endswith("so it cannot be installed.") and "..." in text


# --- switching on ------------------------------------------------------------------------------------------


def test_a_revoked_plugin_cannot_be_switched_on_from_the_hub_or_from_the_settings(
        tmp_path, monkeypatch, service_kind):
    from app import config
    from provider_hub import service
    from provider_hub.state import blocked_installations

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": LONG_REASON}))

    with pytest.raises(ValueError, match="cannot be switched on") as refused:
        service.update_provider(PLUGIN, enabled=True)
    assert len(str(refused.value)) <= LIMIT and str(refused.value).startswith(f"{NAME} 1.0.0 was revoked")
    # The settings save names the same refusal. A plugin that is switched off may stay off.
    monkeypatch.setattr(config.settings.general, "enabled_providers", [])
    with pytest.raises(config.ValidationError, match="was revoked by the official catalog"):
        config._refuse_provider_switch_with_dependencies([("settings-general-enabled_providers", [PLUGIN])])
    assert service.update_provider(PLUGIN, enabled=False)["provider_id"] == PLUGIN
    assert blocked_installations(kind=None) == []


@pytest.mark.parametrize("value", [1, "true", "false", 0, "", [], {}])
def test_an_enabled_value_that_is_not_a_boolean_is_refused_before_anything_is_checked_or_saved(
        tmp_path, monkeypatch, service_kind, value):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))

    with pytest.raises(ValueError, match="enabled must be true or false"):
        service.update_provider(PLUGIN, enabled=value)
    assert _rows()[PLUGIN]["enabled"] is False


def test_the_api_refuses_an_enabled_value_that_is_not_a_boolean_with_a_400(tmp_path, monkeypatch, service_kind):
    from flask import Flask

    from api import api_bp
    from app.config import settings

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))
    monkeypatch.setattr(settings.auth, "apikey", "revocation-test-key")  # pragma: allowlist secret
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "revocation-test-secret"  # pragma: allowlist secret
    app.register_blueprint(api_bp)
    client = app.test_client()
    headers = {"X-API-KEY": "revocation-test-key"}  # pragma: allowlist secret

    for value in (1, "true", None, [], {}):
        response = client.patch(f"/api/plugins/installations/{PLUGIN}", json={"enabled": value}, headers=headers)
        assert response.status_code == 400 and "enabled must be true or false" in response.get_data(as_text=True)
    # A real boolean reaches the revocation check, which refuses it for its own reason.
    response = client.patch(f"/api/plugins/installations/{PLUGIN}", json={"enabled": True}, headers=headers)
    assert response.status_code == 400 and "was revoked" in response.get_data(as_text=True)
    assert _rows()[PLUGIN]["enabled"] is False


def test_a_plugin_whose_active_release_is_not_revoked_switches_on_as_before(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.1.0")
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))

    assert service.revoked_switch_on_refusal(PLUGIN) is None
    assert service.update_provider(PLUGIN, enabled=True)["provider_id"] == PLUGIN


def test_a_revoked_plugin_with_a_staged_update_that_is_not_revoked_may_be_switched_on(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0"), "blocked": True, "blocked_cause": "revoked",
              "blocked_reason": "held"}
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False, **staged),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))

    assert service.revoked_switch_on_refusal(PLUGIN) is None
    assert service.update_provider(PLUGIN, enabled=True)["enabled"] is True


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
    assert row["blocked_reason"] == row["last_error"] == row["revoked_reason"]
    assert row["last_error"] == (
        f"{NAME} 1.0.0 was revoked by the official catalog: It sends credentials to the wrong host. "
        "It will not start. Update it or remove it.")
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


def test_switching_a_revoked_release_off_does_not_clear_the_reason_it_shows(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))
    service.enforce_activation_needs()
    reason = _rows()[PLUGIN]["last_error"]

    assert service.update_provider(PLUGIN, enabled=False)["last_error"] == reason
    service.note_switched_providers(set(), {PLUGIN})
    assert _rows()[PLUGIN]["last_error"] == reason and _rows()[PLUGIN]["revoked_reason"] == reason


def test_a_release_the_list_stops_naming_is_released_at_boot(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import mutate_state

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0"}))
    service.enforce_activation_needs()
    assert _rows()[PLUGIN]["blocked"] is True

    def lift(state):
        state["catalog_sources"]["official"]["catalog_revocations"] = _list(version=101)

    mutate_state(lift)
    assert service.enforce_activation_needs() is True

    row = _rows()[PLUGIN]
    assert "blocked" not in row and "revoked" not in row and "revoked_reason" not in row
    assert row["last_error"] is None


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
    assert row["last_error"].startswith(f"{NAME} 1.1.0 was revoked") and "cannot be installed" in row["last_error"]
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


def test_a_revoked_subtitle_provider_is_held_back_and_left_out_of_the_registry_at_boot(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import registry, service
    from provider_hub.registry import register_active_provider_classes, registration_incomplete

    registry._REGISTRATION_INCOMPLETE.clear()
    sub = provider_manifest_v1("revokedsub", "1.0.0")
    row = {"provider_id": "revokedsub", "name": "Revokedsub", "active_version": "1.0.0", "state": "active",
           "pending_restart": False, "manifest": sub, "enabled": True}
    _stored_world(tmp_path, monkeypatch, [], installations={"revokedsub": row},
                  revocations=_list({"id": "revokedsub", "version": "1.0.0", "reason": REASON}))
    try:
        service.activate_staged_installations()
        assert register_active_provider_classes() == []

        assert _rows()["revokedsub"]["blocked"] is True
        # It is installed and only not started, so it keeps its provider list entries and its settings.
        assert registration_incomplete() == {"revokedsub"}
    finally:
        registry._REGISTRATION_INCOMPLETE.clear()


def test_a_revoked_dependency_does_not_satisfy_what_needs_it_unless_a_safe_update_is_staged(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import closure, service

    capability = "demo_parent.backend.v1"
    parent = _plugin("parent", provides=[capability])
    add_on = _plugin("addon", extends={"id": "parent", "capability": capability})
    user = _plugin("user", requires=[capability])
    flagged = {"revoked": True, "revoked_reason": "x"}

    def resolve(owner, **parent_fields):
        _stored_world(tmp_path, monkeypatch, [owner], installations=_row(parent, **parent_fields))
        entry = next(e for e in _state()["catalog_entries"].values() if e["provider_id"] == owner["id"])
        return service._closure_of(_state(), entry)

    for owner in (user, add_on):
        assert resolve(owner).resolved
        refused = resolve(owner, **flagged)
        assert not refused.resolved and "revoked by the official catalog" in refused.refusal
        assert "no release of parent can be installed" in refused.refusal
        staged = {"staged_version": "1.1.0", "state": "staged", "pending_restart": True,
                  "staged_manifest": _plugin("parent", "1.1.0", provides=[capability])}
        assert resolve(owner, **flagged, **staged).resolved
    assert closure._revoked_without_update({"revoked": True}) and not closure._revoked_without_update({})


# --- a refresh marks, the boot holds -----------------------------------------------------------------------


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


def _publish(world, *, version=100, revocations=None, commit=HEAD, plugins=(), channel="main", **kwargs):
    extra = {} if revocations is None else {"revocations": revocations}
    plugins = plugins or [_plugin(version="1.0.0"), _plugin(version="1.1.0")]
    return world.publish(commit=commit, channel=channel, version=version, plugins=plugins, **extra, **kwargs)


def _install_row(manifest, **fields):
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].update(_row(manifest, **fields)))


def _new_head(world, commit):
    world.ancestors.add(HEAD)
    world.heads["main"] = commit
    world.heads["beta"] = commit


def _forget_the_database(world_state_change=None):
    """A restore of an older database: the source record and the counters go back, the row flags with them."""
    from provider_hub.state import mutate_state

    def restore(state):
        official = state["catalog_sources"]["official"]
        official["catalog_revocations"] = _list(version=100)
        official["catalog_signed_version"] = 100
        state["catalog_feeds"]["main"]["signed_version"] = 100
        for row in state["installations"].values():
            for field in ("revoked", "revoked_reason", "revoked_restart", "blocked", "blocked_reason"):
                row.pop(field, None)
            row.update(last_error=None, pending_restart=False)

    mutate_state(restore)


def test_a_verified_catalog_stores_its_list_by_channel_and_in_a_file_beside_the_counters(world, tmp_path):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])

    _refresh()

    stored = _official()["catalog_revocations"]["channels"]
    assert stored == {"main": {"signed_version": 100,
                               "entries": [{"id": PLUGIN, "version": "1.0.0", "reason": REASON}]}}
    mirrored = json.loads((tmp_path / "plugins" / "feed-revocations.json").read_text(encoding="utf-8"))
    assert mirrored["channels"] == stored


def test_a_refresh_marks_a_running_revoked_release_and_asks_for_a_restart_but_takes_nothing_from_the_registry(
        world):
    from provider_hub import service
    from provider_hub.kinds.subtitle_provider import apply_settings_overlay
    from provider_hub.state import blocked_installations, running_installations

    sub = provider_manifest_v1("runningsub", "1.0.0")
    row = {"provider_id": "runningsub", "name": "Runningsub", "active_version": "1.0.0", "state": "active",
           "pending_restart": False, "manifest": sub, "enabled": True, "trusted": True,
           "source_id": "official", "source_bound": True}
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["installations"].update({"runningsub": row}))
    plugin = _plugin(version="1.0.0")
    _install_row(plugin)
    _publish(world, revocations=[{"id": "runningsub"}, {"id": PLUGIN, "version": "1.0.0", "reason": REASON}],
             providers=[sub])

    _refresh()

    for plugin_id in ("runningsub", PLUGIN):
        row = _rows()[plugin_id]
        assert row["revoked"] is True and "blocked" not in row and "blocked_reason" not in row
        assert row["revoked_reason"] == row["last_error"]
        assert "Restart Bazarr+ to stop it, then " in row["revoked_reason"]
        assert "It will not start" not in row["revoked_reason"] and len(row["revoked_reason"]) <= LIMIT
        # The restart cue the Hub already reads.
        assert row["pending_restart"] is True and row["revoked_restart"] is True
    # needs_restart is derived from that cue by the kind's attach policy: a restart-attached kind reports it.
    assert service.get_provider("runningsub")["needs_restart"] is True
    # The restart cue has its own effect, and it is the safe one: a connection test says to restart first.
    assert service.test_provider_connection("runningsub")["status"] == "pending_restart"
    # The registry was not touched: not the running set, and not the settings overlay.
    assert {item.provider_id for item in running_installations(kind=None)} == {"runningsub", PLUGIN}
    assert blocked_installations(kind=None) == []
    configs = {}
    apply_settings_overlay(configs, {}, set(), set())
    assert "runningsub" in configs
    # The next boot holds them back, and the restart it is drops the cue.
    assert service.enforce_activation_needs() is True
    for plugin_id in ("runningsub", PLUGIN):
        row = _rows()[plugin_id]
        assert row["blocked"] is True and row["pending_restart"] is False and "revoked_restart" not in row
        assert "It will not start. " in row["blocked_reason"] and row["blocked_cause"] == "revoked"
        assert row["last_error"] == row["blocked_reason"] == row["revoked_reason"]
    assert service.get_provider("runningsub")["needs_restart"] is False
    assert running_installations(kind=None) == []


def test_a_verified_catalog_that_revokes_an_offered_update_still_offers_the_next_one(world):
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])

    _refresh()

    marked = {entry["version"]: entry.get("blocked_reason") for entry in service.list_catalog()["entries"]}
    assert marked["1.0.0"] and marked["1.1.0"] is None
    assert service.offered_update_version(PLUGIN) == "1.1.0"


def test_a_later_catalog_that_drops_an_entry_lifts_it_and_one_that_adds_an_entry_marks_it(world):
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    assert _rows()[PLUGIN]["revoked"] is True and _rows()[PLUGIN]["pending_restart"] is True

    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=[], source_commit="e" * 40)
    _refresh()

    assert _official()["catalog_revocations"]["channels"]["main"]["entries"] == []
    row = _rows()[PLUGIN]
    assert "revoked" not in row and "revoked_reason" not in row and row["last_error"] is None
    assert row["pending_restart"] is False and "revoked_restart" not in row

    _new_head(world, "f" * 40)
    world.ancestors.add("e" * 40)
    _publish(world, commit="f" * 40, version=102, revocations=[{"id": PLUGIN}], source_commit="f" * 40)
    _refresh()

    assert _rows()[PLUGIN]["revoked"] is True
    # A release that was held back at boot and is then lifted starts again after a restart, and says so.
    service.enforce_activation_needs()
    assert _rows()[PLUGIN]["blocked"] is True
    _new_head(world, "9" * 40)
    world.ancestors.add("f" * 40)
    _publish(world, commit="9" * 40, version=103, revocations=[], source_commit="9" * 40)
    _refresh()
    row = _rows()[PLUGIN]
    assert "blocked" not in row and "revoked" not in row and row["last_error"] is None
    assert row["pending_restart"] is True


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
    assert _rows()[PLUGIN]["revoked"] is True


def test_a_replayed_older_catalog_cannot_bring_a_revoked_release_back(world):
    _install_row(_plugin(version="1.0.0"))
    old = _publish(world, version=100, revocations=[])
    _new_head(world, "e" * 40)
    world.files[("e" * 40, "signed/main/catalog.json")] = old
    _publish(world, commit="e" * 40, version=200, revocations=[{"id": PLUGIN, "version": "1.0.0"}],
             source_commit="e" * 40)
    _refresh()
    assert _rows()[PLUGIN]["revoked"] is True

    # The repository now serves the older signed pair, which has no revocation: its counter is behind.
    _new_head(world, "d" * 40)
    world.ancestors.add("e" * 40)
    world.publish(commit="d" * 40, body=old, envelope=world.sign(old))
    _refresh()

    assert "older" in _official()["last_error"] or "replayed" in _official()["last_error"]
    assert _rows()[PLUGIN]["revoked"] is True


def test_a_list_in_an_unsigned_legacy_catalog_is_never_read(world):
    _install_row(_plugin(version="1.0.0"))
    document = {"schema_version": 1, "name": "Official", "providers": [],
                "revocations": [{"id": PLUGIN}], "plugins": []}
    world.files[(HEAD, "catalog.json")] = json.dumps(document).encode("utf-8")

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "unsigned (legacy)" and "catalog_revocations" not in source
    assert "revoked" not in _rows()[PLUGIN]


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
    assert _rows()[PLUGIN]["revoked"] is True


@pytest.mark.parametrize("bad", [
    "everything", [{"version": "1.0.0"}], [{"id": PLUGIN, "revoked_at": "soon"}],
    [{"id": PLUGIN, "version": " "}], [{"id": PLUGIN, "below": "2"}], [{"id": "Not-An-Id"}]])
def test_a_list_that_cannot_be_read_refuses_the_whole_catalog_and_keeps_the_last_good_one(world, bad):
    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": "somethingelse"}])
    _refresh()
    held = _official()["catalog_revocations"]
    before = set(_state()["catalog_entries"])

    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=bad, source_commit="e" * 40,
             plugins=[_plugin(OTHER)])
    _refresh()

    assert "revocation list could not be read" in _official()["last_error"]
    assert _official()["catalog_revocations"] == held
    assert set(_state()["catalog_entries"]) == before


def test_a_staged_release_the_refresh_finds_revoked_is_dropped_and_the_active_one_keeps_running(world):
    active = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0"), "staged_path": "/nowhere", "staged_python_path": "/nowhere"}
    _install_row(active, **staged)
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.1.0", "reason": REASON}])

    _refresh()

    row = _rows()[PLUGIN]
    assert row["state"] == "active" and row["active_version"] == "1.0.0" and row["pending_restart"] is False
    assert row["staged_version"] is None and f"{NAME} 1.1.0 was revoked" in row["last_error"]
    assert "blocked" not in row and "revoked" not in row


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
    assert "revoked" not in _rows()["parent"]


def test_the_list_is_applied_whichever_source_an_install_came_from(world):
    # An id the official catalog revokes is revoked here, so a community copy of it cannot be installed either.
    from provider_hub import service

    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()

    revoked = service._install_refusal(_state(), "community", PLUGIN, service.SUBTITLE_PROVIDER, "1.0.0", NAME)
    assert revoked.startswith(f"{NAME} 1.0.0 was revoked") and "revoked by the official catalog" in revoked
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
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    world.now = world.now + timedelta(days=200)

    _refresh()

    assert _rows()[PLUGIN]["revoked"] is True
    assert service.revoked_switch_on_refusal(PLUGIN)


# --- a restored database --------------------------------------------------------------------------------------


def test_a_restored_older_database_does_not_lift_a_revocation_the_mirror_remembers(world, tmp_path):
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[])
    _refresh()
    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=200, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}],
             source_commit="e" * 40)
    _refresh()
    assert _rows()[PLUGIN]["revoked"] is True

    # The database is restored from the backup that held catalog 100. The files beside it were not.
    _forget_the_database()
    assert "revoked" not in _rows()[PLUGIN] and _official()["catalog_revocations"]["channels"]["main"][
        "signed_version"] == 100
    # The host restarts with no network: the boot check and every install path still know.
    assert service.enforce_activation_needs() is True

    row = _rows()[PLUGIN]
    assert row["revoked"] is True and row["blocked"] is True and "was revoked" in row["last_error"]
    with pytest.raises(service.ProviderHubInstallError, match="was revoked"):
        service.resolve_catalog_install("official", PLUGIN, "1.0.0")
    with pytest.raises(ValueError, match="cannot be switched on"):
        service.update_provider(PLUGIN, enabled=True)
    assert service._revocations_stale(_state()) == []
    assert "signed_feed_retry" not in _official()


def test_a_restored_database_with_no_mirror_keeps_the_list_it_has_marks_the_source_and_reads_again(world, tmp_path):
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": OTHER}])
    _refresh()
    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=200, revocations=[{"id": PLUGIN, "version": "1.0.0"}, {"id": OTHER}],
             source_commit="e" * 40)
    _refresh()
    (tmp_path / "plugins" / "feed-revocations.json").unlink()
    _forget_the_database()
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["catalog_sources"]["official"].update(
        catalog_revocations=_list({"id": OTHER}, version=100)))

    # The counter file says 200 was accepted, and the list held is from 100: it is kept, and the host says so.
    assert service._revocations_stale(_state()) == ["main"]
    assert [item.plugin_id for item in service._revocations(_state())] == [OTHER]
    service.enforce_activation_needs()
    assert _official()["signed_feed_retry"] is True
    asked = len(world.fetched("signed/main/catalog.json"))

    # The head has not moved, and the catalog is read again anyway; the list is current once it has been.
    _refresh()

    assert len(world.fetched("signed/main/catalog.json")) > asked
    assert service._revocations_stale(_state()) == [] and "signed_feed_retry" not in _official()
    assert _rows()[PLUGIN]["revoked"] is True


def test_a_host_that_verified_a_catalog_before_the_list_was_kept_reads_it_again_on_an_unchanged_head(world):
    from provider_hub import revocation

    _install_row(_plugin(version="1.0.0"))
    _publish(world, revocations=[{"id": PLUGIN}])
    _refresh()

    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["catalog_sources"]["official"].pop("catalog_revocations"))
    revocation.mirror_path().unlink()
    asked = len(world.fetched("signed/main/catalog.json"))

    _refresh()

    assert len(world.fetched("signed/main/catalog.json")) > asked
    assert _rows()[PLUGIN]["revoked"] is True


# --- channels ---------------------------------------------------------------------------------------------


def test_each_channel_keeps_its_own_list_and_a_switch_does_not_lift_what_the_other_revoked(world):
    from provider_hub.service import update_catalog_source

    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    assert _rows()[PLUGIN]["revoked"] is True

    # The host moves to beta, whose catalog does not list the entry.
    _publish(world, channel="beta", version=50, revocations=[])
    update_catalog_source("official", dev_ref="beta")
    _refresh()

    channels = _official()["catalog_revocations"]["channels"]
    assert channels["beta"]["entries"] == [] and len(channels["main"]["entries"]) == 1
    assert _rows()[PLUGIN]["revoked"] is True
    from provider_hub import service
    assert service.revoked_switch_on_refusal(PLUGIN)

    # Back on main, a newer main catalog is what lifts it, and only main's entry.
    update_catalog_source("official", dev_ref="main")
    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=[], source_commit="e" * 40)
    _refresh()

    assert "revoked" not in _rows()[PLUGIN]


def test_an_entry_a_channel_adds_holds_on_the_other_channel_until_that_channel_lifts_it(world):
    from provider_hub import service
    from provider_hub.service import update_catalog_source

    _publish(world, version=100, revocations=[])
    _publish(world, channel="beta", version=50, revocations=[{"id": PLUGIN, "version": "1.0.0"}])
    _refresh()
    update_catalog_source("official", dev_ref="beta")
    _refresh()
    update_catalog_source("official", dev_ref="main")
    _refresh()

    assert service._install_refusal(_state(), "official", PLUGIN, "test_service", "1.0.0")
    assert service._install_refusal(_state(), "official", PLUGIN, "test_service", "1.1.0") is None


# --- a revoked release and its update -----------------------------------------------------------------------


@pytest.fixture
def staging(tmp_path, monkeypatch, service_kind):
    """The install path with the network, the environment build and the worker replaced."""

    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated, timeout=None, **kwargs):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    monkeypatch.setattr("provider_hub.service._fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr("provider_hub.service.python_executable", lambda env_path: tmp_path / "python")
    monkeypatch.setattr(service_kind, "smoke_test", lambda *args, **kwargs: None, raising=False)


def test_an_update_staged_over_a_revoked_release_keeps_it_revoked_and_is_not_told_to_update_again(
        tmp_path, monkeypatch, staging):
    from provider_hub import closure, service
    from provider_hub.state import mutate_state

    installed = _plugin(version="1.0.0")
    _stored_world(tmp_path, monkeypatch, [installed, _plugin(version="1.1.0")], installations=_row(installed),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))
    service.enforce_activation_needs()
    assert _rows()[PLUGIN]["blocked"] is True

    service.apply_update(PLUGIN)

    row = _rows()[PLUGIN]
    assert row["staged_version"] == "1.1.0" and row["state"] == "staged" and row["pending_restart"] is True
    # Still revoked and still held, so it supplies nothing and does not start, until the update takes over.
    assert row["revoked"] is True and row["blocked"] is True and row["revoked_reason"]
    assert closure.unmet_needs(_rows()) == {}
    # The refresh after staging does not tell the person to update: the update is staged.
    mutate_state(lambda state: service._hold_revoked_installations(state))
    row = _rows()[PLUGIN]
    assert "Restart Bazarr+ to apply the staged update." in row["revoked_reason"]
    assert "Update it or remove it" not in row["revoked_reason"]
    assert row["last_error"] in (None, row["revoked_reason"]) and "Update it or remove" not in (row["last_error"] or "")
    assert service.revoked_switch_on_refusal(PLUGIN) is None


# --- a second review of the hold ----------------------------------------------------------------------------


def _hold(state):
    from provider_hub import service

    service._hold_revoked_installations(state)


def _refresh_part():
    """What every refresh does to the installations, unchanged heads and failed fetches included."""
    from provider_hub.state import mutate_state

    mutate_state(_hold)


def test_a_revoked_plugin_with_a_staged_update_is_switched_on_only_once_the_revoked_release_cannot_run(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0")}
    _stored_world(tmp_path, monkeypatch, [installed], installations=_row(installed, enabled=False, **staged),
                  revocations=_list({"id": PLUGIN, "version": "1.0.0", "reason": REASON}))

    # The refresh found the revocation and left the release registered and unblocked: it would run.
    _refresh_part()
    assert "blocked" not in _rows()[PLUGIN]
    refusal = service.revoked_switch_on_refusal(PLUGIN)
    assert refusal and "Restart Bazarr+ to apply the staged update." in refusal
    with pytest.raises(ValueError, match="Restart Bazarr"):
        service.update_provider(PLUGIN, enabled=True)
    assert _rows()[PLUGIN]["enabled"] is False

    # Once the boot hold has stopped it, the staged update takes over at the next restart: switching on is safe.
    service.enforce_activation_needs()
    assert _rows()[PLUGIN]["blocked"] is True and _rows()[PLUGIN]["staged_version"] == "1.1.0"
    assert service.revoked_switch_on_refusal(PLUGIN) is None
    assert service.update_provider(PLUGIN, enabled=True)["enabled"] is True


def test_a_revoked_supplier_is_no_alternative_when_a_removal_or_an_update_is_judged(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import closure

    capability = "demo_parent.backend.v1"
    first = _plugin("first", provides=[capability])
    second = _plugin("second", provides=[capability])
    user = _plugin("user", requires=[capability])
    rows = {**_row(first), **_row(second, revoked=True, revoked_reason="x"),
            **_row(user, supplied_by={capability: "first"})}
    _stored_world(tmp_path, monkeypatch, [], installations=rows)
    state = _state()

    # The second supplier is revoked, so the first is the only one that runs after the restart.
    assert closure.dependents(state, "first") == ["user"]
    assert closure.dependents(state, "first", enabled_only=True) == ["user"]
    assert closure.broken_by(state, "first", _plugin("first", "2.0.0")) == [("user", capability)]
    # With nothing revoked the second one carries the need, as before.
    rows["second"].pop("revoked")
    _stored_world(tmp_path, monkeypatch, [], installations=rows)
    assert closure.dependents(_state(), "first") == []
    assert closure.broken_by(_state(), "first", _plugin("first", "2.0.0")) == []


def test_a_refresh_on_a_row_the_boot_already_held_changes_nothing_and_a_lift_cues_the_restart(world):
    from provider_hub import service

    _install_row(_plugin(version="1.0.0"))
    _publish(world, version=100, revocations=[{"id": PLUGIN, "version": "1.0.0", "reason": REASON}])
    _refresh()
    assert _rows()[PLUGIN]["pending_restart"] is True
    service.enforce_activation_needs()
    held = dict(_rows()[PLUGIN])
    assert held["blocked"] is True and held["pending_restart"] is False and "revoked_restart" not in held
    assert "It will not start." in held["revoked_reason"]

    # Refreshes that change nothing, and ones that fail, leave the held row exactly as it is.
    _refresh()
    _refresh_part()
    _refresh()
    assert _rows()[PLUGIN] == held

    # A refresh that lifts it: the revocation fields go, the hold is released and the restart is asked for.
    _new_head(world, "e" * 40)
    _publish(world, commit="e" * 40, version=101, revocations=[], source_commit="e" * 40)
    _refresh()
    row = _rows()[PLUGIN]
    assert not {"revoked", "revoked_reason", "blocked", "blocked_reason", "blocked_cause"} & set(row)
    assert row["last_error"] is None and row["pending_restart"] is True and row["revoked_restart"] is True

    # The boot after that is the restart: the cue goes and the plugin runs.
    assert service.enforce_activation_needs() is True
    row = _rows()[PLUGIN]
    assert row["pending_restart"] is False and "revoked_restart" not in row and "blocked" not in row


def test_a_whole_plugin_revocation_drops_a_staged_update_before_the_text_and_the_cue_are_decided(world):
    active = _plugin(version="1.0.0")
    staged = {"state": "staged", "pending_restart": True, "staged_version": "1.1.0",
              "staged_manifest": _plugin(version="1.1.0"), "staged_path": "/nowhere", "staged_python_path": "/nowhere"}
    _install_row(active, **staged)
    _publish(world, revocations=[{"id": PLUGIN, "reason": REASON}])

    _refresh()

    row = _rows()[PLUGIN]
    assert row["staged_version"] is None and row["state"] == "active"
    assert "apply the staged update" not in row["revoked_reason"]
    assert "Restart Bazarr+ to stop it, then remove it." in row["revoked_reason"]
    assert row["pending_restart"] is True and row["revoked_restart"] is True


def test_a_refresh_leaves_the_pending_removal_of_a_revoked_plugin_alone(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    installed = _plugin(version="1.0.0")
    removed = {"state": "removed", "pending_restart": True, "revoked": True, "revoked_restart": True,
               "revoked_reason": "x"}
    for revocations in (_list({"id": PLUGIN}), _list(version=101)):
        _stored_world(tmp_path, monkeypatch, [], installations=_row(installed, **removed), revocations=revocations)
        before = dict(_rows()[PLUGIN])

        _refresh_part()

        assert _rows()[PLUGIN] == before
        service.activate_staged_installations()
        assert PLUGIN not in _rows()


def test_a_whole_plugin_entry_says_remove_and_never_update(tmp_path, monkeypatch, service_kind):
    from provider_hub import revocation, service

    whole = revocation.parse([{"id": PLUGIN}])[0]
    one = revocation.parse([{"id": PLUGIN, "version": "1.0.0"}])[0]
    assert revocation.held_back(whole, NAME, "1.0.0").endswith("It will not start. Remove it.")
    assert revocation.held_back(whole, NAME, "1.0.0", revocation.RUNNING).endswith(
        "Restart Bazarr+ to stop it, then remove it.")
    assert revocation.held_back(one, NAME, "1.0.0").endswith("Update it or remove it.")

    capability = "demo_parent.backend.v1"
    parent = _plugin("parent", provides=[capability])
    flagged = {"revoked": True, "revoked_reason": "x"}
    for owner in (_plugin("user", requires=[capability]),
                  _plugin("addon", extends={"id": "parent", "capability": capability})):
        # Nothing newer can be installed: the text does not send the person to an update.
        _stored_world(tmp_path, monkeypatch, [owner], installations=_row(parent, **flagged))
        entry = next(e for e in _state()["catalog_entries"].values() if e["provider_id"] == owner["id"])
        refusal = service._closure_of(_state(), entry).refusal
        assert "revoked by the official catalog" in refusal and "Update" not in refusal
        assert "no release of parent can be installed" in refusal
        # A newer release the catalog lists does.
        newer = _plugin("parent", "1.1.0", provides=[capability])
        _stored_world(tmp_path, monkeypatch, [owner, newer], installations=_row(parent, **flagged))
        entry = next(e for e in _state()["catalog_entries"].values() if e["provider_id"] == owner["id"])
        assert "Update" in service._closure_of(_state(), entry).refusal


@pytest.mark.parametrize("ending, tail_start", [("!", ""), ("?", ""), (".", ". "), ("", ". ")])
def test_a_reason_that_ends_with_its_own_stop_does_not_get_a_second_one(ending, tail_start):
    from provider_hub import revocation

    reason = "Do not trust it" + ending
    entry = revocation.parse([{"id": PLUGIN, "version": "1.0.0", "reason": reason}])[0]

    held = revocation.held_back(entry, NAME, "1.0.0")
    assert ".." not in held and "!." not in held and "?." not in held
    expected = f"Do not trust it{ending} It will not start." if ending in ("!", "?") else "Do not trust it. It will"
    assert expected in held
    refused = revocation.refusal(entry, NAME, "1.0.0", "installed")
    assert "!," not in refused and "?," not in refused and ".." not in refused
    assert refused.endswith("so it cannot be installed.") or refused.endswith("So it cannot be installed.")


def test_the_staleness_of_the_list_is_judged_on_the_channel_being_read(world, tmp_path):
    from provider_hub import service
    from provider_hub.catalog_feed import ChannelRecord, write_counter_file

    _publish(world, version=100, revocations=[])
    _refresh()
    # The host once accepted a beta catalog and has no list for it: beta is stale, main is not.
    write_counter_file({"beta": ChannelRecord(500, 1)})

    assert service._revocations_stale(_state(), ["main"]) == []
    assert service._revocations_stale(_state(), ["beta"]) == ["beta"]
    assert service._revocations_stale(_state()) == ["beta"]
    # A host reading main is not told to read again for beta's sake.
    service.enforce_activation_needs()
    assert "signed_feed_retry" not in _official()
    asked = len(world.fetched("signed/main/catalog.json"))
    _refresh()
    assert len(world.fetched("signed/main/catalog.json")) == asked


def test_the_mirror_is_synced_before_it_replaces_the_file_and_the_directory_after(tmp_path, monkeypatch):
    import os

    from provider_hub import revocation

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda *a: (order.append("replace"), real_replace(*a))[1])

    revocation.write_mirror({"main": revocation.ChannelList(5, ())})

    assert order[:2] == ["fsync", "replace"] and order.count("fsync") == 2 and order[-1] == "fsync"


def test_a_corrupt_mirror_is_read_and_reported_once_until_it_changes(tmp_path, monkeypatch, caplog):
    from provider_hub import revocation

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    path = tmp_path / "plugins" / "feed-revocations.json"
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        for _ in range(5):
            assert revocation.read_mirror() == {}
    assert sum("could not be read" in record.getMessage() for record in caplog.records) == 1
    # A write replaces it, and the cache follows.
    revocation.write_mirror({"main": revocation.ChannelList(5, ())})
    assert revocation.read_mirror()["main"].signed_version == 5
