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

    service.install_catalog_entry("official", "addon", "1.0.0")

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
