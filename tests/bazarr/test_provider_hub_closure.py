# coding=utf-8
"""What a plugin needs, resolved at selection, and what cannot be removed or switched off under it."""
import pytest

from plugin_platform_helpers import service_manifest, verified_official_source, write_state

PARENT_CAPABILITY = "demo_parent.backend.v1"


def _manifest(plugin_id, version="1.0.0", **fields):
    return service_manifest(plugin_id, version, **fields)


def _entry(manifest, source="official"):
    return {"source": source, "source_name": source, "provider_id": manifest["id"], "name": manifest["name"],
            "version": manifest["version"], "trusted": source == "official", "manifest": manifest,
            "catalog_list": "plugins"}


def _world(tmp_path, monkeypatch, manifests, installations=None):
    entries = {f"official:plugins:{m['id']}:{m['version']}": _entry(m) for m in manifests}
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json", catalog_sources={"official": verified_official_source()},
        catalog_entries=entries, installations=installations or {})))


def _installed(manifest, enabled=True, **fields):
    row = {"provider_id": manifest["id"], "kind": manifest["kind"], "name": manifest["name"],
           "active_version": manifest["version"], "state": "active", "pending_restart": False,
           "manifest": manifest, "enabled": enabled}
    row.update(fields)
    return {manifest["id"]: row}


def _closure(plugin_id, version="1.0.0"):
    from provider_hub.service import _closure_of
    from provider_hub.state import load_state

    state = load_state()
    entry = next(e for e in state["catalog_entries"].values()
                 if e["provider_id"] == plugin_id and e["version"] == version)
    return _closure_of(state, entry)


def _ids(closure):
    return [(member.plugin_id, member.reason) for member in closure.members]


def test_a_plugin_with_no_needs_is_its_own_closure(tmp_path, monkeypatch, service_kind):
    _world(tmp_path, monkeypatch, [_manifest("alone")])

    closure = _closure("alone")

    assert _ids(closure) == [("alone", "root")] and closure.resolved and closure.external == []


def test_an_add_on_pulls_in_the_plugin_it_extends_first(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [parent, addon])

    assert _ids(_closure("addon")) == [("parent", "extends"), ("addon", "root")]


def test_an_installed_parent_satisfies_the_add_on_and_is_not_pulled_in_again(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [parent, addon], _installed(parent))

    closure = _closure("addon")

    assert _ids(closure) == [("addon", "root")]
    assert closure.satisfied_by == [{"id": "parent", "version": "1.0.0", "capability": PARENT_CAPABILITY}]


def test_an_installed_parent_that_does_not_provide_the_capability_does_not_resolve(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent")
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [addon], _installed(parent))

    assert "does not provide" in _closure("addon").refusal


def test_a_parent_the_catalog_does_not_list_does_not_resolve(tmp_path, monkeypatch, service_kind):
    _world(tmp_path, monkeypatch, [_manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})])

    closure = _closure("addon")

    assert not closure.resolved and "extends parent" in closure.refusal


def test_a_needed_capability_is_taken_from_the_plugin_that_provides_it(tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [helper, user])

    assert _ids(_closure("user")) == [("helper", "requires"), ("user", "root")]


def test_a_capability_no_plugin_provides_is_external_and_never_blocks(tmp_path, monkeypatch, service_kind):
    _world(tmp_path, monkeypatch, [_manifest("user", requires=["host.ffmpeg.v1", "llm.openai.v1"])])

    closure = _closure("user")

    assert closure.resolved and closure.external == ["host.ffmpeg.v1", "llm.openai.v1"]


def test_a_capability_only_an_incompatible_plugin_provides_does_not_resolve(tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", provides=["translit.v1"], platforms=["windows-x86_64"])
    _world(tmp_path, monkeypatch, [helper, _manifest("user", requires=["translit.v1"])])

    assert "no plugin this Bazarr+ can install provides" in _closure("user").refusal


def test_a_plugin_cycle_does_not_resolve(tmp_path, monkeypatch, service_kind):
    first = _manifest("first", provides=["first.v1"], requires=["second.v1"])
    second = _manifest("second", provides=["second.v1"], requires=["first.v1"])
    _world(tmp_path, monkeypatch, [first, second])

    assert "cycle" in _closure("first").refusal


def test_an_entry_whose_closure_does_not_resolve_is_blocked_and_cannot_be_installed(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, list_catalog, resolve_catalog_install

    _world(tmp_path, monkeypatch, [_manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})])

    [listed] = list_catalog()["entries"]
    assert "extends parent" in listed["blocked_reason"]
    with pytest.raises(ProviderHubInstallError, match="extends parent"):
        resolve_catalog_install("official", "addon", "1.0.0", catalog_list="plugins")


def test_an_update_whose_closure_does_not_resolve_falls_back_to_the_next_lower_release(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import _latest_catalog_manifest, _refused_update_reason
    from provider_hub.state import load_state

    parent_ref = {"id": "parent", "capability": PARENT_CAPABILITY}
    installed = _manifest("addon", "1.0.0", extends=parent_ref)
    _world(tmp_path, monkeypatch,
           [_manifest("parent", provides=[PARENT_CAPABILITY]), installed,
            _manifest("addon", "1.5.0", extends=parent_ref),
            _manifest("addon", "2.0.0", extends={"id": "gone", "capability": "gone.v1"})],
           {**_installed(installed, source_id="official", source_bound=True),
            **_installed(_manifest("parent", provides=[PARENT_CAPABILITY]))})

    state = load_state()
    assert _latest_catalog_manifest(state, "addon", "1.0.0", "official", "plugins",
                                    installed_kind="test_service")["version"] == "1.5.0"
    assert "extends gone" in _refused_update_reason(state, "addon", "1.5.0", "official", "plugins",
                                                    installed_kind="test_service")


def test_installing_an_add_on_installs_what_it_extends_first(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [parent, addon])
    staged = []
    monkeypatch.setattr(service, "stage_install", lambda manifest, **kwargs: staged.append((manifest["id"], kwargs["source_id"])))

    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    digest = service.install_consent("official", "addon", "1.0.0", catalog_list="plugins")["digest"]

    service.install_catalog_entry("official", "addon", "1.0.0", consent_digest=digest)

    assert staged == [("parent", "official"), ("addon", "official")]


# --- removing and switching off ----------------------------------------------------------------


def _pair(tmp_path, monkeypatch, addon_enabled=True):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [], {**_installed(parent), **_installed(addon, enabled=addon_enabled)})


def test_a_plugin_something_extends_cannot_be_removed(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation
    from provider_hub.state import load_state

    _pair(tmp_path, monkeypatch)

    with pytest.raises(ProviderHubInstallError, match="parent cannot be removed while addon depends on it"):
        remove_installation("parent")
    assert load_state()["installations"]["parent"]["state"] == "active"
    assert remove_installation("addon") is True


def test_a_plugin_something_needs_cannot_be_switched_off_but_a_switched_off_dependent_does_not_count(
        tmp_path, monkeypatch, service_kind):
    from provider_hub.service import update_provider

    _pair(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="parent cannot be switched off while addon depends on it"):
        update_provider("parent", enabled=False)

    _pair(tmp_path, monkeypatch, addon_enabled=False)
    assert update_provider("parent", enabled=False)["enabled"] is False


def test_a_capability_another_installed_plugin_also_provides_leaves_each_removable(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import remove_installation

    first = _manifest("first", provides=["translit.v1"])
    second = _manifest("second", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [], {**_installed(first), **_installed(second), **_installed(user)})

    assert remove_installation("first") is True


# --- staged updates and switched-off suppliers count ------------------------------------------------


def test_a_staged_update_that_extends_a_plugin_protects_it_from_removal(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    plain = _manifest("addon")
    staged = _manifest("addon", "2.0.0", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [], {**_installed(parent), **_installed(
        plain, staged_version="2.0.0", staged_manifest=staged, state="staged", pending_restart=True)})

    with pytest.raises(ProviderHubInstallError, match="parent cannot be removed while addon depends on it"):
        remove_installation("parent")


def test_a_switched_off_plugin_is_no_substitute_for_the_supplier_being_removed(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation

    first = _manifest("first", provides=["translit.v1"])
    second = _manifest("second", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [],
           {**_installed(first), **_installed(second, enabled=False), **_installed(user)})

    with pytest.raises(ProviderHubInstallError, match="first cannot be removed while user depends on it"):
        remove_installation("first")


def test_the_removal_checks_dependents_in_the_write_it_commits(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [], {**_installed(parent), **_installed(addon)})
    # The early check saw nothing, as when the dependent is installed a moment later.
    real = service.closure.dependents
    calls = []
    monkeypatch.setattr(service.closure, "dependents",
                        lambda *args, **kwargs: [] if not calls and not calls.append(1) else real(*args, **kwargs))

    with pytest.raises(service.ProviderHubInstallError, match="addon depends on it"):
        service.remove_installation("parent")

    assert load_state()["installations"]["parent"]["state"] == "active"
    assert service_kind.calls[-1] == ("enabled", "parent", True)


# --- capabilities a staged update supplies, and suppliers that are switched off -----------------------------


def test_a_staged_supplier_update_protects_the_plugin_that_will_need_it(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation

    supplier = _manifest("supplier")
    supplier_next = _manifest("supplier", "2.0.0", provides=["translit.v1"])
    consumer = _manifest("consumer")
    consumer_next = _manifest("consumer", "2.0.0", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [], {
        **_installed(supplier, staged_version="2.0.0", staged_manifest=supplier_next, state="staged",
                     pending_restart=True),
        **_installed(consumer, staged_version="2.0.0", staged_manifest=consumer_next, state="staged",
                     pending_restart=True)})

    with pytest.raises(ProviderHubInstallError, match="supplier cannot be removed while consumer depends on it"):
        remove_installation("supplier")


def test_an_enabled_installed_supplier_satisfies_a_need_and_a_switched_off_one_does_not(
        tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])

    _world(tmp_path, monkeypatch, [user], _installed(helper))
    assert _closure("user").satisfied_by == [{"id": "helper", "version": "1.0.0", "capability": "translit.v1"}]

    _world(tmp_path, monkeypatch, [user], _installed(helper, enabled=False))
    closure = _closure("user")
    assert not closure.resolved and "only helper provides, and it is switched off" in closure.refusal
    assert closure.satisfied_by == []


def test_a_switched_off_supplier_gives_way_to_another_plugin_that_provides_the_capability(
        tmp_path, monkeypatch, service_kind):
    off = _manifest("off", provides=["translit.v1"])
    other = _manifest("other", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [user, other], _installed(off, enabled=False))

    assert _ids(_closure("user")) == [("other", "requires"), ("user", "root")]


# --- one consistent set of releases per phase ----------------------------------------------------------------


def test_a_staged_update_that_drops_a_capability_does_not_keep_another_supplier_removable(
        tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation

    first = _manifest("first", provides=["translit.v1"])
    second = _manifest("second", provides=["translit.v1"])
    second_next = _manifest("second", "2.0.0")
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [], {
        **_installed(first),
        **_installed(second, staged_version="2.0.0", staged_manifest=second_next, state="staged",
                     pending_restart=True),
        **_installed(user)})

    with pytest.raises(ProviderHubInstallError, match="first cannot be removed while user depends on it"):
        remove_installation("first")


def test_a_staged_update_that_adds_a_capability_does_not_justify_removing_the_only_current_supplier(
        tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, remove_installation

    first = _manifest("first", provides=["translit.v1"])
    second = _manifest("second")
    second_next = _manifest("second", "2.0.0", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [], {
        **_installed(first),
        **_installed(second, staged_version="2.0.0", staged_manifest=second_next, state="staged",
                     pending_restart=True),
        **_installed(user)})

    with pytest.raises(ProviderHubInstallError, match="first cannot be removed while user depends on it"):
        remove_installation("first")


def test_a_need_only_a_newer_release_of_an_installed_plugin_meets_asks_for_that_update_first(
        tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", "1.0.0")
    helper_next = _manifest("helper", "2.0.0", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [user, helper_next], _installed(helper, source_id="official", source_bound=True))

    closure = _closure("user")

    assert not closure.resolved and closure.external == []
    assert "provides from version 2.0.0. Update helper first." in closure.refusal


def test_an_add_on_does_not_install_against_a_parent_that_is_switched_off(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [addon], _installed(parent, enabled=False))

    closure = _closure("addon")

    assert not closure.resolved and "parent, which is switched off. Switch it on first." in closure.refusal


def test_install_resolution_relies_on_a_supplier_only_for_what_it_keeps_providing(
        tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", provides=["translit.v1"])
    helper_next = _manifest("helper", "2.0.0")
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [user], _installed(
        helper, staged_version="2.0.0", staged_manifest=helper_next, state="staged", pending_restart=True))

    closure = _closure("user")

    assert not closure.resolved and closure.satisfied_by == []
    assert "provides now, but the update staged for it no longer does" in closure.refusal


def test_a_staged_release_that_adds_a_capability_supplies_a_plugin_installed_now(
        tmp_path, monkeypatch, service_kind):
    # A plugin installed now starts at the restart that activates the staged release, not before it.
    helper = _manifest("helper")
    helper_next = _manifest("helper", "2.0.0", provides=["translit.v1"])
    user = _manifest("user", requires=["translit.v1"])
    _world(tmp_path, monkeypatch, [user], _installed(
        helper, staged_version="2.0.0", staged_manifest=helper_next, state="staged", pending_restart=True))

    closure = _closure("user")

    assert closure.resolved
    assert closure.satisfied_by == [{"id": "helper", "version": "2.0.0", "capability": "translit.v1"}]


def test_a_parent_whose_staged_release_adds_the_capability_satisfies_an_add_on(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent")
    parent_next = _manifest("parent", "2.0.0", provides=[PARENT_CAPABILITY])
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [addon], _installed(
        parent, staged_version="2.0.0", staged_manifest=parent_next, state="staged", pending_restart=True))

    closure = _closure("addon")

    assert closure.resolved and closure.satisfied_by[0]["version"] == "2.0.0"


def test_two_needs_that_only_different_releases_of_one_plugin_meet_do_not_resolve(
        tmp_path, monkeypatch, service_kind):
    first = _manifest("helper", "1.0.0", provides=["alpha.v1"])
    second = _manifest("helper", "2.0.0", provides=["beta.v1"])
    user = _manifest("user", requires=["alpha.v1", "beta.v1"])
    _world(tmp_path, monkeypatch, [user, first, second])

    closure = _closure("user")

    assert not closure.resolved
    assert "provides only in a release other than the one already chosen" in closure.refusal


def test_two_needs_one_release_meets_resolve_once(tmp_path, monkeypatch, service_kind):
    helper = _manifest("helper", provides=["alpha.v1", "beta.v1"])
    user = _manifest("user", requires=["alpha.v1", "beta.v1"])
    _world(tmp_path, monkeypatch, [user, helper])

    assert _ids(_closure("user")) == [("helper", "requires"), ("user", "root")]


def test_a_parent_whose_staged_release_drops_the_capability_does_not_satisfy_an_add_on(
        tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    parent_next = _manifest("parent", "2.0.0")
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})
    _world(tmp_path, monkeypatch, [addon], _installed(
        parent, staged_version="2.0.0", staged_manifest=parent_next, state="staged", pending_restart=True))

    assert "does not provide" in _closure("addon").refusal


def test_the_update_hint_needs_a_real_newer_release_from_the_bound_source(tmp_path, monkeypatch, service_kind):
    user = _manifest("user", requires=["translit.v1"])
    installed = _manifest("helper", "2.0.0")
    # An older release that has the capability is not an update.
    older = _manifest("helper", "1.0.0", provides=["translit.v1"])
    _world(tmp_path, monkeypatch, [user, older], _installed(installed, source_id="official", source_bound=True))
    closure = _closure("user")
    assert closure.resolved and closure.external == ["translit.v1"]

    # Neither is a newer release the install is not bound to.
    newer = _manifest("helper", "3.0.0", provides=["translit.v1"])
    _world(tmp_path, monkeypatch, [user, newer], _installed(installed, source_id="elsewhere", source_bound=True))
    assert _closure("user").resolved

    _world(tmp_path, monkeypatch, [user, newer], _installed(installed, source_id="official", source_bound=True))
    assert "Update helper first" in _closure("user").refusal


def test_an_add_on_that_extends_a_plugin_already_chosen_in_a_release_without_the_capability_does_not_resolve(
        tmp_path, monkeypatch, service_kind):
    # The root needs alpha first, which only helper 1.0.0 provides, and then an add-on that extends helper
    # with beta, which only helper 2.0.0 provides: the release already chosen cannot be changed.
    helper_one = _manifest("helper", "1.0.0", provides=["alpha.v1"])
    helper_two = _manifest("helper", "2.0.0", provides=["beta.v1"])
    addon = _manifest("addon", provides=["addon.v1"], extends={"id": "helper", "capability": "beta.v1"})
    root = _manifest("user", requires=["alpha.v1", "addon.v1"])
    _world(tmp_path, monkeypatch, [root, helper_one, helper_two, addon])

    closure = _closure("user")

    assert not closure.resolved
    assert "already chosen for this install (1.0.0) does not provide beta.v1" in closure.refusal


# --- a release that others rely on cannot be taken away or dropped -------------------------------------------


def _rely_world(tmp_path, monkeypatch, extra=(), consumer_staged=False):
    """parent 1.0.0 runs without the capability, its staged 2.0.0 provides it, and an add-on relies on it."""
    parent = _manifest("parent")
    parent_next = _manifest("parent", "2.0.0", provides=[PARENT_CAPABILITY])
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    row_extra = {"staged_version": "2.0.0", "staged_manifest": parent_next, "state": "staged",
                 "pending_restart": True, "source_id": "official", "source_bound": True}
    user_extra = ({"active_version": None, "staged_version": "1.0.0", "staged_manifest": user, "state": "staged",
                   "pending_restart": True} if consumer_staged else {})
    _world(tmp_path, monkeypatch, list(extra), {**_installed(parent, **row_extra),
                                                  **_installed(user, **user_extra)})
    return parent, parent_next, user


def test_a_failed_restage_keeps_the_staged_release_another_plugin_relies_on(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    third = _manifest("parent", "3.0.0", provides=[PARENT_CAPABILITY])
    _rely_world(tmp_path, monkeypatch, extra=[third])
    monkeypatch.setattr(service, "_fetch_bundle",
                        lambda manifest, deadline=None: (_ for _ in ()).throw(
                            service.ProviderHubInstallError("the download failed")))

    with pytest.raises(service.ProviderHubInstallError, match="download failed"):
        service.stage_install(third, source_id="official", catalog_list="plugins")

    row = load_state()["installations"]["parent"]
    assert row["staged_version"] == "2.0.0" and row["staged_manifest"]["version"] == "2.0.0"
    assert row["state"] == "staged" and row["last_error"] == "the download failed"


def test_a_failed_restage_still_clears_a_staged_release_nothing_relies_on(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    parent = _manifest("parent")
    parent_next = _manifest("parent", "2.0.0", provides=[PARENT_CAPABILITY])
    third = _manifest("parent", "3.0.0", provides=[PARENT_CAPABILITY])
    _world(tmp_path, monkeypatch, [third], _installed(
        parent, staged_version="2.0.0", staged_manifest=parent_next, state="staged", pending_restart=True,
        source_id="official", source_bound=True))
    monkeypatch.setattr(service, "_fetch_bundle",
                        lambda manifest, deadline=None: (_ for _ in ()).throw(
                            service.ProviderHubInstallError("the download failed")))

    with pytest.raises(service.ProviderHubInstallError):
        service.stage_install(third, source_id="official", catalog_list="plugins")

    assert load_state()["installations"]["parent"]["staged_version"] is None


def test_an_update_that_drops_a_capability_a_dependent_needs_is_neither_offered_nor_staged(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    dropped = _manifest("parent", "2.0.0")
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    _world(tmp_path, monkeypatch, [dropped],
           {**_installed(parent, source_id="official", source_bound=True), **_installed(user)})

    assert service._latest_catalog_manifest(
        load_state(), "parent", "1.0.0", "official", "plugins", installed_kind="test_service") is None
    assert service.check_updates()["details"]["updates_available"] == 0
    refused = service.apply_update("parent")
    assert refused["last_error"] == "parent 2.0.0 no longer provides demo_parent.backend.v1, which user relies on."
    with pytest.raises(service.ProviderHubInstallError, match="which user relies on"):
        service.stage_install(dropped, source_id="official", catalog_list="plugins")


def test_an_update_that_drops_a_capability_is_refused_in_the_write_when_a_dependent_lands_meanwhile(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    class FakeEnvironment:
        def __init__(self, root):
            pass

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    dropped = _manifest("parent", "2.0.0")
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    _world(tmp_path, monkeypatch, [dropped], _installed(parent, source_id="official", source_bound=True))

    def fetch(manifest, deadline=None):
        # The early check saw nothing relying on the parent; a dependent installs while the bundle downloads.
        mutate_state(lambda state: state["installations"].update(_installed(user)))
        return tmp_path / "bundle"

    monkeypatch.setattr(service, "_fetch_bundle", fetch)
    monkeypatch.setattr(service, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(service, "python_executable", lambda env_path: tmp_path / "python")

    with pytest.raises(service.ProviderHubInstallError, match="which user relies on"):
        service.stage_install(dropped, source_id="official", catalog_list="plugins")

    assert load_state()["installations"]["parent"].get("staged_version") is None


# --- the boot re-check ----------------------------------------------------------------------------------------


def _boot(tmp_path, monkeypatch, installations):
    from provider_hub import service
    from provider_hub.state import load_state

    _world(tmp_path, monkeypatch, [], installations)
    service.activate_staged_installations()
    return load_state()["installations"]


def test_a_plugin_whose_supplier_is_not_running_at_boot_does_not_start_and_says_why(
        tmp_path, monkeypatch, service_kind):
    from provider_hub.state import active_installations, running_installations

    parent = _manifest("parent")
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    # The supplier's staged release provided the capability when the add-on installed; it never activated.
    rows = _boot(tmp_path, monkeypatch, {
        **_installed(parent), **_installed(user, supplied_by={PARENT_CAPABILITY: "parent"})})

    assert rows["user"]["blocked"] is True
    assert rows["user"]["last_error"] == (
        "It needs parent (demo_parent.backend.v1), which is not running, so it was not started.")
    assert "blocked" not in rows["parent"]
    assert [row.provider_id for row in running_installations(kind=None)] == ["parent"]
    assert [row.provider_id for row in active_installations(kind=None)] == ["parent"]


def test_an_add_on_whose_parent_does_not_provide_the_capability_does_not_start(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent")
    addon = _manifest("addon", extends={"id": "parent", "capability": PARENT_CAPABILITY})

    rows = _boot(tmp_path, monkeypatch, {**_installed(parent), **_installed(addon)})

    assert rows["addon"]["blocked"] is True and "parent (demo_parent.backend.v1)" in rows["addon"]["last_error"]


def test_what_relies_on_a_plugin_that_did_not_start_does_not_start_either(tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent")
    middle = _manifest("middle", provides=["mid.v1"], requires=[PARENT_CAPABILITY])
    top = _manifest("top", requires=["mid.v1"])

    rows = _boot(tmp_path, monkeypatch, {
        **_installed(parent), **_installed(middle, supplied_by={PARENT_CAPABILITY: "parent"}),
        **_installed(top, supplied_by={"mid.v1": "middle"})})

    assert rows["middle"]["blocked"] is True and rows["top"]["blocked"] is True


def test_a_plugin_that_is_met_runs_and_a_blocked_one_is_released_once_its_supplier_runs(
        tmp_path, monkeypatch, service_kind):
    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    blocked = {"blocked": True, "blocked_reason": "old reason", "last_error": "old reason",
               "supplied_by": {PARENT_CAPABILITY: "parent"}}

    rows = _boot(tmp_path, monkeypatch, {**_installed(parent), **_installed(user, **blocked)})

    assert "blocked" not in rows["user"] and rows["user"]["last_error"] is None


def test_a_need_with_no_recorded_supplier_is_left_to_the_host_and_never_blocks(tmp_path, monkeypatch, service_kind):
    user = _manifest("user", requires=["host.ffmpeg.v1", "llm.openai.v1"])

    rows = _boot(tmp_path, monkeypatch, _installed(user))

    assert "blocked" not in rows["user"]


def test_a_supplier_whose_staged_release_fails_to_activate_blocks_the_plugin_installed_against_it(
        tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    parent = _manifest("parent")
    parent_next = _manifest("parent", "2.0.0", provides=[PARENT_CAPABILITY])
    user = _manifest("user", requires=[PARENT_CAPABILITY])
    bundle = tmp_path / "bundles" / "parent" / "2.0.0" / ("b" * 40)
    python = tmp_path / "envs" / "parent" / "2.0.0" / "test" / "bin" / "python"
    _world(tmp_path, monkeypatch, [], {
        **_installed(parent, staged_version="2.0.0", staged_manifest=parent_next, state="staged",
                     pending_restart=True, staged_path=str(bundle), staged_python_path=str(python)),
        **_installed(user, supplied_by={PARENT_CAPABILITY: "parent"})})
    monkeypatch.setattr(service, "verify_bundle_tree", lambda manifest, root: None)

    def refuse(validated, bundle_path, python_path, timeout=None):
        raise RuntimeError("the worker did not start")

    monkeypatch.setattr(service_kind, "smoke_test", refuse)

    service.activate_staged_installations()

    rows = load_state()["installations"]
    assert rows["parent"]["active_version"] == "1.0.0" and rows["parent"]["staged_version"] is None
    assert rows["user"]["blocked"] is True and "parent (demo_parent.backend.v1)" in rows["user"]["last_error"]


def test_a_release_records_which_installed_plugin_supplies_each_need(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.state import load_state

    class FakeEnvironment:
        def __init__(self, root):
            pass

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    parent = _manifest("parent", provides=[PARENT_CAPABILITY])
    user = _manifest("user", requires=[PARENT_CAPABILITY, "host.ffmpeg.v1", "llm.openai.v1"])
    _world(tmp_path, monkeypatch, [user], _installed(parent))
    monkeypatch.setattr(service, "_fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    monkeypatch.setattr(service, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(service, "python_executable", lambda env_path: tmp_path / "python")

    service.stage_install(user, source_id="official", catalog_list="plugins")

    assert load_state()["installations"]["user"]["staged_supplied_by"] == {PARENT_CAPABILITY: "parent"}
