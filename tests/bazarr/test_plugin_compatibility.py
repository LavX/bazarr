# coding=utf-8
"""Version order, the host's own version, and whether an entry runs here."""
import pytest

from plugin_platform_helpers import archive_runtime, oci_runtime, provider_manifest_v2, service_manifest


@pytest.mark.parametrize("lower,higher", [
    ("2.8.0-alpha", "2.8.0-alpha.1"),
    ("2.8.0-alpha.1", "2.8.0-alpha.beta"),
    ("2.8.0-beta.2", "2.8.0-beta.11"),
    ("2.8.0-rc.1", "2.8.0"),
    ("2.8.0", "2.8.1"),
    ("2.9", "2.10.0"),
])
def test_versions_sort_by_semantic_version_precedence(lower, higher):
    from provider_hub.versions import version_key

    assert version_key(lower) < version_key(higher)
    assert version_key("1.2.3+build.5") == version_key("1.2.3")


def test_a_git_describe_build_counts_as_its_tag():
    from provider_hub.versions import host_version_key, version_key

    assert host_version_key("2.7.0-12-gabc1234") == version_key("2.7.0")
    assert host_version_key("v2.8.0-beta.1") < version_key("2.8.0")
    assert host_version_key("unknown") is None


def _host(**overrides):
    from provider_hub.compatibility import AVAILABLE_RUNTIMES, HostFacts

    values = {"version": "2.8.0", "platform": "linux-x86_64", "runtimes": AVAILABLE_RUNTIMES,
              "python_tag": "cp314", "in_container": True}
    values.update(overrides)
    return HostFacts(**values)


@pytest.mark.parametrize("overrides,host,reason", [
    ({"kind_api": "bazarr.subtitle_provider.v2"}, {}, "this Bazarr+ does not support bazarr.subtitle_provider.v2"),
    ({"min_host_version": "2.8.1"}, {}, "needs Bazarr+ 2.8.1 or later"),
    ({"min_host_version": "2.8.0"}, {"version": "2.8.0-rc.2"}, "needs Bazarr+ 2.8.0 or later"),
    ({"platforms": ["linux-aarch64"]}, {}, "not available for linux-x86_64"),
    (archive_runtime("worker"), {},
     "needs the archive worker runtime, which this Bazarr+ does not provide"),
    (archive_runtime("daemon"), {},
     "needs the archive daemon runtime, which this Bazarr+ does not provide"),
    (oci_runtime(), {},
     "needs the oci daemon runtime, which this Bazarr+ does not provide"),
    ({"python_tags": ["cp312", "cp313"]}, {}, "not available for Python cp314"),
])
def test_an_entry_that_cannot_run_here_says_why(overrides, host, reason):
    from provider_hub.compatibility import evaluate
    from provider_hub.plugin_manifest import parse_plugin_manifest

    assert evaluate(parse_plugin_manifest(provider_manifest_v2(**overrides)), _host(**host)) == (False, reason)


def test_the_python_daemon_pair_is_refused_when_the_manifest_is_read():
    from provider_hub.manifest import ManifestValidationError
    from provider_hub.plugin_manifest import parse_plugin_manifest

    with pytest.raises(ManifestValidationError, match="runtime python run as daemon is not a supported combination"):
        parse_plugin_manifest(provider_manifest_v2(runtime={"artifact": "python", "process": "daemon"}))


def test_the_runtimes_a_host_offers_decide_what_it_can_run():
    from provider_hub.compatibility import evaluate
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(provider_manifest_v2(**archive_runtime()))
    reason = "needs the archive worker runtime, which this Bazarr+ does not provide"

    assert evaluate(plugin, _host()) == (False, reason)
    assert evaluate(plugin, _host(runtimes=frozenset({("archive", "worker")}))) == (True, None)


def test_a_catalog_entry_with_an_invalid_runtime_is_blocked_with_the_specific_reason():
    from provider_hub.compatibility import entry_compatibility

    runtimes = frozenset({("oci", "daemon")})
    entry = {"manifest": service_manifest(runtime={"artifact": "oci", "process": "daemon"})}

    assert entry_compatibility(entry, _host(runtimes=runtimes)) == (
        False, "runtime oci run as daemon starts a managed container, so permissions must include docker")


def test_an_oci_runtime_needs_a_host_that_runs_in_a_container():
    from provider_hub.compatibility import evaluate
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(provider_manifest_v2(**oci_runtime()))
    runtimes = frozenset({("oci", "daemon")})

    assert evaluate(plugin, _host(runtimes=runtimes, in_container=False)) == (
        False, "a managed container needs Bazarr+ itself to run in a container")
    assert evaluate(plugin, _host(runtimes=runtimes, in_container=True)) == (True, None)


@pytest.mark.parametrize("overrides", [
    {"min_host_version": "999.0.0+" + "a" * 220},
    {"kind_api": "bazarr.subtitle_provider_" + "a" * 220 + ".v1"},
])
def test_the_blocked_reason_of_a_valid_but_incompatible_manifest_is_capped(overrides):
    from provider_hub.compatibility import entry_compatibility

    entry = {"manifest": provider_manifest_v2(**overrides)}

    compatible, reason = entry_compatibility(entry, _host())

    assert compatible is False
    assert len(reason) == 200


def test_the_blocked_reason_of_an_unreadable_stored_manifest_is_capped(monkeypatch):
    from provider_hub.compatibility import entry_compatibility
    from provider_hub.manifest import ManifestValidationError
    from secret_store.scrub import scrub_text

    message = "settings field token has an unsupported type, which this Bazarr+ does not read. " * 4

    def refusing_parse(manifest):
        raise ManifestValidationError(message)

    monkeypatch.setattr("provider_hub.compatibility.parse_plugin_manifest", refusing_parse)

    compatible, reason = entry_compatibility({"manifest": {}}, _host())

    assert compatible is False
    assert len(reason) == 200
    assert reason == scrub_text(message)[:200]


@pytest.mark.parametrize("overrides,host", [
    ({}, {}),
    ({"min_host_version": "2.8.0-rc.1"}, {"version": "2.8.0-rc.1"}),
    ({"min_host_version": "2.7.0"}, {"version": "2.7.0-12-gabc1234"}),
    ({"min_host_version": "9.0.0"}, {"version": "unknown"}),     # a source checkout
    ({"platforms": ["linux-x86_64", "linux-aarch64"], "python_tags": ["cp314"]}, {}),
    ({"python_tags": ["py3"]}, {"python_tag": "cp399"}),
])
def test_an_entry_that_runs_here_is_compatible(overrides, host):
    from provider_hub.compatibility import evaluate
    from provider_hub.plugin_manifest import parse_plugin_manifest

    assert evaluate(parse_plugin_manifest(provider_manifest_v2(**overrides)), _host(**host)) == (True, None)


@pytest.mark.parametrize("system,machine,libc,musl,expected", [
    ("linux", "x86_64", ("glibc", "2.36"), False, "linux-x86_64"),
    ("linux", "aarch64", ("glibc", "2.36"), False, "linux-aarch64"),
    ("linux", "x86_64", ("", ""), True, "linux-musl-x86_64"),
    ("win32", "AMD64", ("", ""), False, "windows-x86_64"),
    ("darwin", "arm64", ("", ""), False, "macos-arm64"),
])
def test_the_host_platform(monkeypatch, system, machine, libc, musl, expected):
    from provider_hub import compatibility

    monkeypatch.setattr(compatibility.sys, "platform", system)
    monkeypatch.setattr(compatibility.platform, "machine", lambda: machine)
    monkeypatch.setattr(compatibility.platform, "libc_ver", lambda: libc)
    monkeypatch.setattr(compatibility.glob, "glob", lambda pattern: ["/lib/ld-musl-x86_64.so.1"] if musl else [])

    assert compatibility.host_platform() == expected


def test_running_in_a_container_is_detected(monkeypatch, tmp_path):
    from provider_hub import compatibility

    monkeypatch.setattr(compatibility.os.path, "exists", lambda path: path == "/.dockerenv")
    assert compatibility.running_in_container() is True
    monkeypatch.setattr(compatibility.os.path, "exists", lambda path: False)
    monkeypatch.setattr(compatibility, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
                        raising=False)
    assert compatibility.running_in_container() is False
