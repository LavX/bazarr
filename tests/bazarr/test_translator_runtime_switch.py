"""The translation runtime switch: off by default, read in one place, and dark while off."""
import sys
from types import SimpleNamespace

import pytest

TRANSLATOR_MANIFEST_API = "bazarr.translator.v1"


@pytest.fixture
def forget_translator_kind():
    """Leave the process-wide kind registry as the test found it."""
    from provider_hub import kinds

    before = kinds._KINDS.get("translator")
    kinds._KINDS.pop("translator", None)
    try:
        yield
    finally:
        kinds._KINDS.pop("translator", None)
        if before is not None:
            kinds._KINDS["translator"] = before


@pytest.fixture
def fresh_table_check(monkeypatch):
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(runtime, "_tables_present", False)
    return runtime


def test_the_switch_is_off_by_default_and_the_pointers_are_empty():
    from app.config import settings

    assert settings.translator.runtime_v2 is False
    assert settings.translator.engine == ""
    assert settings.translator.fallback_engine == ""


def test_with_the_switch_off_nothing_looks_at_the_database(monkeypatch, fresh_table_check):
    runtime = fresh_table_check
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: False)
    monkeypatch.setattr(runtime, "_runtime_tables_exist", lambda: pytest.fail("the database was consulted"))
    assert runtime.runtime_v2_enabled() is False


def test_on_but_without_the_tables_the_runtime_stays_off(monkeypatch, tmp_path, fresh_table_check):
    import sqlalchemy as sa

    from app import database

    runtime = fresh_table_check
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(database, "engine", engine)
    try:
        assert runtime.runtime_v2_enabled() is False
        database.Base.metadata.create_all(engine, tables=[
            database.TableTranslatorEngineInstance.__table__, database.TableTranslatorConnection.__table__])
        assert runtime.runtime_v2_enabled() is False  # a table is still missing
        database.Base.metadata.create_all(engine, tables=[database.TableTranslatorSecret.__table__])
        assert runtime.runtime_v2_enabled() is True
    finally:
        engine.dispose()


def test_only_a_real_true_turns_the_switch_on(monkeypatch):
    from subtitles.tools.translate import runtime

    for value in (False, None, "true", 1, "yes"):
        monkeypatch.setattr("app.config.settings", SimpleNamespace(translator=SimpleNamespace(runtime_v2=value)))
        assert runtime.runtime_v2_flag() is False
    monkeypatch.setattr("app.config.settings", SimpleNamespace(translator=SimpleNamespace(runtime_v2=True)))
    assert runtime.runtime_v2_flag() is True


def test_a_broken_settings_object_reads_as_off(monkeypatch):
    from subtitles.tools.translate import runtime

    monkeypatch.setattr("app.config.settings", SimpleNamespace())
    assert runtime.runtime_v2_flag() is False


# ---------------------------------------------------------------- the kind

def test_with_the_switch_off_the_kind_is_not_registered(monkeypatch, forget_translator_kind):
    from provider_hub.kinds import SUBTITLE_PROVIDER, kind_for_api, known_kinds
    from provider_hub.kinds.translator import register_when_enabled
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: False)
    assert register_when_enabled() is False
    assert SUBTITLE_PROVIDER in known_kinds() and "translator" not in known_kinds()
    assert kind_for_api(TRANSLATOR_MANIFEST_API) is None


def test_a_translator_manifest_is_unsupported_while_the_switch_is_off(forget_translator_kind):
    from provider_hub.compatibility import HostFacts, evaluate
    from provider_hub.plugin_manifest import parse_plugin_manifest

    plugin = parse_plugin_manifest(_translator_manifest())
    host = HostFacts(version="2.8.0", platform="linux-x86_64", python_tag="cp314",
                     runtimes=frozenset({("python", "worker")}), in_container=False)
    assert evaluate(plugin, host) == (False, f"this Bazarr+ does not support {TRANSLATOR_MANIFEST_API}")


def test_with_the_switch_on_the_kind_registers_once(monkeypatch, forget_translator_kind):
    from provider_hub.compatibility import HostFacts, evaluate
    from provider_hub.kinds import ATTACH_ON_RESTART, get_kind, kind_for_api
    from provider_hub.kinds.translator import TranslatorKind, register_when_enabled
    from provider_hub.plugin_manifest import parse_plugin_manifest
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    assert register_when_enabled() is True
    first = get_kind("translator")
    assert isinstance(first, TranslatorKind) and first.attach_policy == ATTACH_ON_RESTART
    assert register_when_enabled() is True and get_kind("translator") is first
    assert kind_for_api(TRANSLATOR_MANIFEST_API) is first
    host = HostFacts(version="2.8.0", platform="linux-x86_64", python_tag="cp314",
                     runtimes=frozenset({("python", "worker")}), in_container=False)
    assert evaluate(parse_plugin_manifest(_translator_manifest()), host) == (True, None)


def test_the_kind_refuses_a_manifest_of_another_kind(monkeypatch, forget_translator_kind):
    from provider_hub.kinds.translator import register_translator_kind
    from provider_hub.manifest import ManifestValidationError

    kind = register_translator_kind()
    manifest = _translator_manifest()
    assert kind.validate_manifest(manifest, trusted=True).kind == "translator"
    for change in ({"kind": "subtitle_provider"}, {"kind_api": "bazarr.subtitle_provider.v1"}):
        with pytest.raises(ManifestValidationError):
            kind.validate_manifest({**manifest, **change}, trusted=True)


def test_kind_hooks_create_and_remove_the_engine_instance_and_reset_pointers(
        monkeypatch, schema_session, forget_translator_kind):
    from provider_hub.kinds.translator import TranslatorKind
    from subtitles.tools.translate.runtime import store as store_module

    class Pointers:
        engine = fallback = None

        def get(self):
            return self.engine, self.fallback

        def set(self, engine, fallback):
            self.engine, self.fallback = engine, fallback
            return True

    pointers = Pointers()
    monkeypatch.setattr(store_module, "ConfigPointers", lambda: pointers)
    monkeypatch.setattr(store_module, "default_plugin_active", lambda plugin_id: True)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(database=schema_session))
    kind = TranslatorKind()

    assert kind.on_installed("google_translate") is True
    assert kind.on_installed("google_translate") is True  # a second install event adds no second instance
    store = store_module.TranslatorStore(schema_session, pointers=pointers)
    instances = store.list_instances("google_translate")
    assert len(instances) == 1
    store.set_engine(instances[0].id)

    installation = {"provider_id": "google_translate", "enabled": True}
    assert kind.set_enabled(installation, False) is True
    assert installation["enabled"] is False and kind.is_enabled(installation) is False
    assert pointers.get() == (None, None)
    assert kind.set_enabled(installation, True) is True and kind.is_enabled(installation) is True

    store.set_engine(instances[0].id)
    assert kind.on_removed("google_translate") is True
    assert store.list_instances("google_translate") == []
    assert pointers.get() == (None, None)


def test_kind_hooks_report_failure_instead_of_raising(monkeypatch, forget_translator_kind):
    from provider_hub.kinds.translator import TranslatorKind
    from subtitles.tools.translate.runtime import store as store_module

    def broken(*_args, **_kwargs):
        raise RuntimeError("no tables")

    monkeypatch.setattr(store_module.TranslatorStore, "ensure_instance", broken)
    monkeypatch.setattr(store_module.TranslatorStore, "remove_plugin", broken)
    monkeypatch.setattr(store_module.TranslatorStore, "plugin_disabled", broken)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(database=object()))
    kind = TranslatorKind()
    assert kind.on_installed("x") is False
    assert kind.on_removed("x") is False
    assert kind.set_enabled({"provider_id": "x"}, False) is False


# ---------------------------------------------------------------- the settings API

def _translator_manifest(**changes):
    manifest = {
        "schema_version": 2, "id": "ai_translator", "kind": "translator", "kind_api": TRANSLATOR_MANIFEST_API,
        "name": "AI Subtitle Translator", "version": "2.2.0",
        "files": {"worker.py": "a" * 64}, "bundle_sha256": "b" * 64,
        "source": {"type": "github", "repo": "LavX/ai-subtitle-translator", "ref": "main", "commit": "c" * 40,
                   "path": "plugin"},
    }
    manifest.update(changes)
    return manifest


@pytest.fixture
def saveable(monkeypatch):
    """save_settings with the validators' log check and the database stubbed out."""
    from app import config

    class _Update:
        def values(self, **kwargs):
            return self

        def where(self, *args):
            return self

    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None, scalar=lambda *a, **k: "0", close=lambda: None),
        update=lambda _model: _Update(),
        System=SimpleNamespace(updated="updated", configured="configured")))
    return config


def test_the_settings_read_hides_the_switch_and_the_pointers_but_keeps_the_rest():
    from app.config import get_settings

    translator = get_settings()["translator"]
    for hidden in ("runtime_v2", "engine", "fallback_engine"):
        assert hidden not in translator
    # The old Translator page still reads everything it read before.
    for shown in ("translator_type", "gemini_keys", "openrouter_model", "default_score", "min_source_score"):
        assert shown in translator


def test_the_settings_save_cannot_flip_the_switch_or_repoint_the_engine(saveable):
    from app.config import settings

    before = (settings.translator.runtime_v2, settings.translator.engine, settings.translator.fallback_engine)
    score = settings.translator.default_score
    try:
        saveable.save_settings([
            ("settings-translator-runtime_v2", ["true"]),
            ("settings-translator-engine", ["5"]),
            ("settings-translator-fallback_engine", ["6"]),
            ("settings-translator-default_score", [str(score + 1)]),
        ])
        assert (settings.translator.runtime_v2, settings.translator.engine,
                settings.translator.fallback_engine) == before
        saveable.save_settings([("settings-Translator-Runtime_V2", ["true"])])
        assert settings.translator.runtime_v2 is False
        # An ordinary translator setting in the same request is still saved.
        assert settings.translator.default_score == score + 1
        assert [key for key in settings.translator if key.lower() == "runtime_v2"] == ["runtime_v2"]
    finally:
        settings.translator.default_score = score
        settings.translator.runtime_v2, settings.translator.engine, settings.translator.fallback_engine = before


def test_the_switch_and_pointers_are_written_to_the_config_file():
    import yaml

    from app import config

    before = (config.settings.translator.runtime_v2, config.settings.translator.engine)
    try:
        config.settings.translator.runtime_v2 = True
        config.settings.translator.engine = "3"
        assert config.write_config() is True
        with open(config.config_yaml_file, encoding="utf-8") as handle:
            stored = yaml.safe_load(handle)["translator"]
        assert stored["runtime_v2"] is True and stored["engine"] == "3"
    finally:
        config.settings.translator.runtime_v2, config.settings.translator.engine = before
        config.write_config()


def test_the_validators_default_the_switch_off():
    from app.config import validators

    by_name = {validator.names[0]: validator for validator in validators if validator.names}
    assert by_name["translator.runtime_v2"].default is False
    assert by_name["translator.engine"].default == ""
    assert by_name["translator.fallback_engine"].default == ""
