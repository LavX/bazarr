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
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
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
        monkeypatch.setattr(runtime, "_absent_until", 0.0)  # past the short wait after a no
        assert runtime.runtime_v2_enabled() is False  # a table is still missing
        database.Base.metadata.create_all(engine, tables=[database.TableTranslatorSecret.__table__])
        monkeypatch.setattr(runtime, "_absent_until", 0.0)
        assert runtime.runtime_v2_enabled() is False  # the job tables are still missing
        database.Base.metadata.create_all(engine, tables=[
            database.TableTranslatorJob.__table__, database.TableTranslatorCheckpoint.__table__])
        monkeypatch.setattr(runtime, "_absent_until", 0.0)
        assert runtime.runtime_v2_enabled() is False  # the history and usage tables are still missing
        database.Base.metadata.create_all(engine, tables=[
            getattr(database, name).__table__ for name in (
                "TableTranslationHistory", "TableTranslationHistoryConnection", "TableTranslationUsage",
                "TableTranslationBudget")])
        monkeypatch.setattr(runtime, "_absent_until", 0.0)
        assert runtime.runtime_v2_enabled() is True
    finally:
        engine.dispose()


def test_a_boot_builds_no_translator_tables(monkeypatch, tmp_path, fresh_table_check):
    """Only the migration creates the runtime tables, so a boot with the switch off adds none."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from app import database

    runtime = fresh_table_check
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
    session = Session(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "database", session)
    try:
        database.init_db()
        names = set(sa.inspect(engine).get_table_names())
        assert "table_shows" in names  # the rest of the schema is still built
        assert not names & database.MIGRATION_ONLY_TABLES

        # With the switch on and the tables still missing, the runtime stays off.
        monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
        assert runtime.runtime_v2_enabled() is False
    finally:
        session.close()
        engine.dispose()


def test_a_missing_table_answer_is_remembered_for_a_short_while(monkeypatch, fresh_table_check):
    runtime = fresh_table_check
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    asked = []

    def inspect_names(_engine):
        asked.append(1)
        return type("Inspector", (), {"get_table_names": staticmethod(lambda: [])})()

    monkeypatch.setattr("sqlalchemy.inspect", inspect_names)
    for _ in range(5):  # a consumer loop asking every pass
        assert runtime.runtime_v2_enabled() is False
    assert len(asked) == 1
    now[0] += runtime.ABSENT_RECHECK_SECONDS
    assert runtime.runtime_v2_enabled() is False
    assert len(asked) == 2


def test_a_failing_table_check_is_not_repeated_on_every_pass(monkeypatch, fresh_table_check, caplog):
    runtime = fresh_table_check
    monkeypatch.setattr(runtime, "runtime_v2_flag", lambda: True)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 1000.0)

    def broken(_engine):
        raise RuntimeError("database is down")

    monkeypatch.setattr("sqlalchemy.inspect", broken)
    for _ in range(5):
        assert runtime.runtime_v2_enabled() is False
    assert caplog.text.count("Could not check for the translation runtime tables") == 1


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

    monkeypatch.setattr("app.config.settings", SimpleNamespace(translator=SimpleNamespace(runtime_v2=False)))
    # A switched-off boot must not load the translation package at all.
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)
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

    monkeypatch.setattr("app.config.settings", SimpleNamespace(translator=SimpleNamespace(runtime_v2=True)))
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
    # The pointers let go at once; the instance goes only once the removal has been saved.
    assert pointers.get() == (None, None)
    assert len(store.list_instances("google_translate")) == 1
    kind.after_removed("google_translate")
    assert store.list_instances("google_translate") == []


def test_kind_hooks_report_failure_instead_of_raising(monkeypatch, forget_translator_kind):
    from provider_hub.kinds.translator import TranslatorKind
    from subtitles.tools.translate.runtime import store as store_module

    def broken(*_args, **_kwargs):
        raise RuntimeError("no tables")

    monkeypatch.setattr(store_module.TranslatorStore, "ensure_instance", broken)
    monkeypatch.setattr(store_module.TranslatorStore, "release_plugin", broken)
    monkeypatch.setattr(store_module.TranslatorStore, "delete_plugin_data", broken)
    monkeypatch.setattr(store_module.TranslatorStore, "plugin_disabled", broken)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(database=object()))
    kind = TranslatorKind()
    assert kind.on_installed("x") is False
    assert kind.on_removed("x") is False
    kind.after_removed("x")  # logs, never raises: the removal is already saved
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


def test_the_score_of_a_translated_subtitle_is_a_share_of_100_and_no_more(saveable):
    from dynaconf.validator import ValidationError

    from app.config import settings, validators

    by_name = {validator.names[0]: validator for validator in validators if validator.names}
    bounds = by_name["translator.default_score"].operations
    assert (bounds["gte"], bounds["lte"]) == (0, 100)
    score = settings.translator.default_score
    try:
        saveable.save_settings([("settings-translator-default_score", ["100"])])
        assert settings.translator.default_score == 100
        # Above the scale: refused, and what was saved stays.
        with pytest.raises(ValidationError):
            saveable.save_settings([("settings-translator-default_score", ["101"])])
        assert settings.translator.default_score == 100
        with pytest.raises(ValidationError):
            saveable.save_settings([("settings-translator-default_score", ["-1"])])
        assert settings.translator.default_score == 100
    finally:
        settings.translator.default_score = score


def _stored_score(tmp_path, value):
    """Settings read from a config file that holds this score, with the real validator for it."""
    import yaml
    from dynaconf import Dynaconf

    from app.config import validators

    path = tmp_path / f"config-{len(list(tmp_path.iterdir()))}.yaml"
    path.write_text(yaml.safe_dump({"translator": {"default_score": value}}), encoding="utf-8")
    stored = Dynaconf(settings_file=str(path), core_loaders=["YAML"], apply_default_on_none=True)
    stored.validators.register(*[validator for validator in validators
                                 if validator.names and validator.names[0] == "translator.default_score"])
    return stored


def test_a_stored_score_above_100_is_brought_down_to_100_and_said_once(tmp_path, caplog):
    import logging

    from dynaconf.validator import ValidationError

    from app.config import clamp_translated_subtitle_score

    # Left alone, the validator refuses it, and the startup loop answers a refusal with the default of 50.
    with pytest.raises(ValidationError):
        _stored_score(tmp_path, 150).validators.validate_all()

    stored = _stored_score(tmp_path, 150)
    with caplog.at_level(logging.INFO):
        assert clamp_translated_subtitle_score(stored) is True
        stored.validators.validate_all()  # nothing is left for the loop to reset
        assert stored.translator.default_score == 100
        said = [record.getMessage() for record in caplog.records if "score of a translated subtitle" in record.getMessage()]
        assert len(said) == 1 and "150" in said[0] and "100" in said[0]
        # The next start finds 100: nothing to do, and nothing said again.
        caplog.clear()
        assert clamp_translated_subtitle_score(stored) is False
        assert caplog.records == []


@pytest.mark.parametrize("value", [0, 50, 65, 100])
def test_a_stored_score_in_range_is_left_as_it_is(tmp_path, value):
    from app.config import clamp_translated_subtitle_score

    stored = _stored_score(tmp_path, value)
    assert clamp_translated_subtitle_score(stored) is False
    stored.validators.validate_all()
    assert stored.translator.default_score == value


def test_only_a_score_above_the_limit_is_brought_down_and_the_rest_is_left_to_the_validator(tmp_path):
    from dynaconf.validator import ValidationError

    from app.config import clamp_translated_subtitle_score

    # A score that is a number with a fraction, above the limit: the highest score, as a whole number.
    high = _stored_score(tmp_path, 150.5)
    assert clamp_translated_subtitle_score(high) is True and high.translator.default_score == 100
    assert isinstance(high.translator.default_score, int)
    high.validators.validate_all()
    # Nothing that says "the highest": the validator judges these as it did before.
    for odd in (-5, "150", True):
        stored = _stored_score(tmp_path, odd)
        assert clamp_translated_subtitle_score(stored) is False
        assert stored.translator.default_score == odd
    with pytest.raises(ValidationError):
        _stored_score(tmp_path, -5).validators.validate_all()


def test_a_start_with_a_stored_150_comes_up_with_100_not_the_default_and_saves_it(tmp_path):
    """The whole start, in a process of its own: the stored value is brought down before the validation
    loop can reset it, and the boot write puts the new value in the file."""
    import os
    import subprocess
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    config_dir = tmp_path / "data"
    (config_dir / "config").mkdir(parents=True)
    config_file = config_dir / "config" / "config.yaml"
    config_file.write_text(yaml.safe_dump({"translator": {"default_score": 150}}), encoding="utf-8")
    start = ("import sys; sys.path[:0] = [sys.argv[1], sys.argv[2]]; "
             "from app.config import settings; print('score', settings.translator.default_score)")
    environment = {**os.environ, "BAZARR_CONFIG_DIR": str(config_dir), "NO_CLI": "true",
                   "PYTHONDONTWRITEBYTECODE": "1"}

    def boot():
        done = subprocess.run([sys.executable, "-c", start, str(root / "bazarr"), str(root / "custom_libs")],
                              cwd=root / "bazarr", env=environment, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-2000:]
        return done.stdout.strip().splitlines()[-1]

    assert boot() == "score 100"
    assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["translator"]["default_score"] == 100
    # The next start reads the saved 100 and keeps it.
    assert boot() == "score 100"


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
