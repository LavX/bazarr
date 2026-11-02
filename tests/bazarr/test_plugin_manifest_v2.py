# coding=utf-8
"""Manifest v2: the shared envelope, schema-2 providers and non-Python files."""
import pytest

from plugin_platform_helpers import provider_manifest_v1, provider_manifest_v2, service_manifest


def _parse(manifest):
    from provider_hub.plugin_manifest import parse_plugin_manifest

    return parse_plugin_manifest(manifest)


def test_a_schema_1_provider_lifts_into_the_envelope_with_its_old_meaning():
    plugin = _parse(provider_manifest_v1("legacyhub"))

    assert (plugin.plugin_id, plugin.kind, plugin.kind_api) == (
        "legacyhub", "subtitle_provider", "bazarr.subtitle_provider.v1")
    assert (plugin.platforms, plugin.artifact, plugin.process) == (("any",), "python", "worker")
    assert (plugin.requires, plugin.provides, plugin.extends, plugin.permissions) == ((), (), None, ())


def test_the_schema_2_envelope_reads_every_field():
    plugin = _parse(service_manifest(
        "ai_translator",
        min_host_version="2.8.0-rc.1",
        platforms=["linux-x86_64", "linux-aarch64"],
        runtime={"artifact": "python", "process": "worker"},
        settings={"fields": [{"key": "model", "type": "options"}, {"key": "api_key", "type": "secret"}]},
        settings_version=3,
        requires=["llm.openai.v1", "host.ffmpeg.v1"],
        provides=["ai_translator.backend.v1"],
        permissions=["network", "data_dir"],
    ))

    assert plugin.kind_api_major == 1
    assert plugin.min_host_version == "2.8.0-rc.1"
    assert plugin.platforms == ("linux-x86_64", "linux-aarch64")
    assert plugin.settings_version == 3
    assert [field["key"] for field in plugin.settings_fields] == ["model", "api_key"]
    assert plugin.requires == ("llm.openai.v1", "host.ffmpeg.v1")
    assert plugin.permissions == ("data_dir", "network")


def test_extends_names_the_parent_and_its_extension_point():
    plugin = _parse(service_manifest(
        "cli_backend", extends={"id": "ai_translator", "capability": "ai_translator.backend.v1"}))

    assert (plugin.extends.plugin_id, plugin.extends.capability) == ("ai_translator", "ai_translator.backend.v1")


@pytest.mark.parametrize("overrides,reason", [
    ({"schema_version": 3}, "schema_version must be 1 or 2"),
    ({"id": "Bad Id"}, "id must use lowercase plugin id syntax"),
    ({"provider_id": "other"}, "provider_id, when present, must equal id"),
    ({"kind": "Bad Kind"}, "kind must use lowercase kind syntax"),
    ({"kind_api": "service.v1"}, "kind_api must look like"),
    ({"min_host_version": "v2"}, "must be a semantic version"),
    ({"platforms": ["beos"]}, "unknown platform"),
    ({"platforms": ["any", "linux-x86_64"]}, "lists 'any' together"),
    ({"runtime": {"artifact": "oci", "process": "worker"}}, "not a supported combination"),
    ({"runtime": {"artifact": "oci", "process": "daemon"}}, "not a supported combination"),
    ({"runtime": {"artifact": "archive", "process": "worker"}}, "not a supported combination"),
    ({"runtime": {"artifact": "archive", "process": "daemon"}}, "not a supported combination"),
    ({"permissions": ["root"]}, "unknown permission"),
    ({"requires": ["flaresolverr"]}, "must look like name.v1"),
    ({"provides": ["host.ffmpeg.v1"]}, "cannot provide a host capability"),
    ({"extends": {"id": "exampleservice", "capability": "x.backend.v1"}}, "cannot extend itself"),
    ({"settings_version": 1}, "settings_version is set but there are no settings"),
    ({"settings": {"fields": []}}, "settings_version must be a positive integer"),
    ({"settings": {"fields": [{"key": "a", "type": "text"}, {"key": "a", "type": "text"}]},
      "settings_version": 1}, "declared twice"),
    ({"endpoints": "https://openrouter.ai:443"}, "must be a list of strings"),
])
def test_the_envelope_refuses_what_it_cannot_honour(overrides, reason):
    from provider_hub.manifest import ManifestValidationError

    with pytest.raises(ManifestValidationError, match=reason):
        _parse(service_manifest(**overrides))


def test_unknown_top_level_keys_are_kept_and_ignored():
    plugin = _parse(service_manifest(login={"argv": ["vendor", "login"]}))

    assert plugin.raw["login"] == {"argv": ["vendor", "login"]}


def test_a_schema_2_provider_validates_and_reads_as_a_provider():
    from provider_hub.manifest import validate_manifest

    validated = validate_manifest(provider_manifest_v2("modernhub"), built_in_provider_ids=set())

    assert validated.provider_id == "modernhub"
    assert validated.raw["provider_id"] == "modernhub"
    assert (validated.kind, validated.schema_version) == ("subtitle_provider", 2)


@pytest.mark.parametrize("overrides,reason", [
    ({"kind": "test_service", "kind_api": "bazarr.test_service.v1"},
     "is a test_service plugin, not a subtitle provider"),
    ({"kind_api": "bazarr.subtitle_provider.v9"}, "unsupported subtitle provider contract"),
    ({"settings": {"fields": []}, "settings_version": 1}, "declares config_schema, not settings"),
])
def test_a_schema_2_provider_refuses_what_a_provider_cannot_be(overrides, reason):
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    with pytest.raises(ManifestValidationError, match=reason):
        validate_manifest(provider_manifest_v2("modernhub", **overrides), built_in_provider_ids=set())


def test_a_schema_2_bundle_may_carry_declared_data_files(tmp_path):
    from provider_hub.bundle import verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    payloads = {"provider.py": b"class ExampleProvider: pass\n", "data/languages.json": b"{}"}
    validated = validate_manifest(provider_manifest_v2("modernhub", file_payloads=payloads),
                                  built_in_provider_ids=set())
    for relative, content in payloads.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(content)

    verify_bundle_tree(validated, tmp_path)


def test_undeclared_files_stay_refused_in_either_schema(tmp_path):
    from provider_hub.bundle import BundleValidationError, verify_bundle_tree
    from provider_hub.manifest import validate_manifest

    payloads = {"provider.py": b"class ExampleProvider: pass\n"}
    for manifest in (provider_manifest_v1("legacyhub"), provider_manifest_v2("modernhub", file_payloads=payloads)):
        root = tmp_path / manifest.get("id", manifest.get("provider_id"))
        root.mkdir()
        (root / "provider.py").write_bytes(payloads["provider.py"])
        (root / "extra.bin").write_bytes(b"\0")
        with pytest.raises(BundleValidationError, match="undeclared bundle file|unexpected bundle file"):
            verify_bundle_tree(validate_manifest(manifest, built_in_provider_ids=set()), root)


def test_schema_1_still_refuses_data_files():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    payloads = {"provider.py": b"x = 1\n", "data.json": b"{}"}
    with pytest.raises(ManifestValidationError, match="only .py files are allowed"):
        validate_manifest(provider_manifest_v1("legacyhub", file_payloads=payloads), built_in_provider_ids=set())


def test_no_bundle_may_declare_the_hosts_manifest_file():
    from provider_hub.manifest import ManifestValidationError, validate_manifest

    payloads = {"provider.py": b"x = 1\n", "provider.json": b"{}"}
    with pytest.raises(ManifestValidationError, match="reserved for the host"):
        validate_manifest(provider_manifest_v2("modernhub", file_payloads=payloads), built_in_provider_ids=set())


@pytest.mark.parametrize("origin", [
    "api.example.com", "ftp://api.example.com:443", "https://api.example.com", "https://api.example.com:443/path",
    "https://localhost:443", "https://API.example.com:443", "https://api.example.com:0",
    "https://api.example.com:99999",
])
def test_endpoints_are_full_origins(origin):
    from provider_hub.manifest import ManifestValidationError

    with pytest.raises(ManifestValidationError, match="must be an origin written as scheme://host:port"):
        _parse(service_manifest(endpoints=[origin]))
    assert _parse(service_manifest(endpoints=["https://openrouter.ai:443"])).endpoints == (
        "https://openrouter.ai:443",)


@pytest.mark.parametrize("tags", [["cp3.14"], ["CP314"], ["pp310"]])
def test_python_tags_are_cpython_tags_or_py3(tags):
    from provider_hub.manifest import ManifestValidationError

    with pytest.raises(ManifestValidationError, match="must be a tag such as cp314 or py3"):
        _parse(service_manifest(python_tags=tags))
    assert _parse(service_manifest(python_tags=["cp313", "cp314"])).python_tags == ("cp313", "cp314")


def _multi_backend(**types):
    return service_manifest("multi", settings_version=1, settings={"fields": [], "backend_types": types})


def test_endpoints_are_listed_per_back_end_type():
    from provider_hub.manifest import ManifestValidationError

    plugin = _parse(_multi_backend(
        openrouter={"endpoints": ["https://openrouter.ai:443"], "fields": [{"key": "api_key", "type": "secret"}]},
        gemini={"endpoints": ["https://generativelanguage.googleapis.com:443"],
                "fields": [{"key": "api_keys", "type": "key_list"}]},
        openai_compatible={"fields": [{"key": "base_url", "type": "text", "destination": True}]},
    ))

    assert plugin.backend_endpoints == (
        ("gemini", ("https://generativelanguage.googleapis.com:443",)),
        ("openai_compatible", ()),
        ("openrouter", ("https://openrouter.ai:443",)),
    )
    assert plugin.all_endpoints == (
        "https://generativelanguage.googleapis.com:443", "https://openrouter.ai:443")
    assert dict(plugin.backend_fields)["openai_compatible"][0]["destination"] is True
    with pytest.raises(ManifestValidationError, match="must be an origin written as scheme://host:port"):
        _parse(_multi_backend(openrouter={"endpoints": ["openrouter.ai"]}))
    with pytest.raises(ManifestValidationError, match="settings.backend_types"):
        _parse(service_manifest("multi", settings_version=1, settings={"fields": [], "backend_types": ["gemini"]}))


def test_a_schema_2_secret_field_is_known_to_the_secret_registry():
    from secret_store.registry import manifest_secret_fields

    manifest = _multi_backend(gemini={"fields": [{"key": "api_keys", "type": "key_list", "secret": True}]})
    manifest["settings"]["fields"] = [{"key": "token", "type": "secret"}, {"key": "mode", "type": "choice"}]

    assert manifest_secret_fields(manifest) == {"token", "api_keys"}


def test_a_plugin_of_another_kind_validates_through_the_shared_bundle_checks():
    from provider_hub.manifest import ManifestValidationError, validate_plugin_bundle

    validated = validate_plugin_bundle(service_manifest("exampleservice"))

    assert (validated.provider_id, validated.kind, validated.schema_version) == ("exampleservice", "test_service", 2)
    with pytest.raises(ManifestValidationError, match="files must not be empty"):
        validate_plugin_bundle(dict(service_manifest("exampleservice"), files={}))
    with pytest.raises(ManifestValidationError, match="source.commit must be an immutable commit SHA"):
        validate_plugin_bundle(service_manifest("exampleservice", source={
            "type": "github", "repo": "LavX/bazarr-provider-catalog", "ref": "main", "commit": "main"}))
