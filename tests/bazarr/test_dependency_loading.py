import subprocess
import sys
from pathlib import Path

import subliminal
import yaml


def test_pytest_conftest_imports_without_pkg_resources_deprecation_warning():
    repo_root = Path(__file__).resolve().parents[2]
    conftest_path = repo_root / "tests" / "conftest.py"

    script = (
        "import importlib.util; "
        f"spec = importlib.util.spec_from_file_location('bazarr_test_conftest', {str(conftest_path)!r}); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module)"
    )

    result = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_pkg_resources_shim_only_exposes_distribution_versions():
    from importlib import metadata

    import pkg_resources

    distribution = pkg_resources.get_distribution("PyYAML")

    assert distribution.version == metadata.version("PyYAML")
    assert not hasattr(pkg_resources, "resource_filename")


def test_flask_compress_is_loaded_from_python_environment_not_custom_libs():
    import flask_compress

    repo_root = Path(__file__).resolve().parents[2]
    custom_flask_compress_dir = repo_root / "custom_libs" / "flask_compress"
    flask_compress_path = Path(flask_compress.__file__).resolve()

    requirements = (repo_root / "requirements.txt").read_text()
    custom_versions = (repo_root / "custom_libs" / "custom_version.txt").read_text()

    assert not custom_flask_compress_dir.exists()
    assert not flask_compress_path.is_relative_to(custom_flask_compress_dir)
    assert "Flask-Compress==1.24" in requirements
    assert "Flask-Compress" not in custom_versions


def test_deathbycaptcha_is_loaded_from_official_package_not_custom_libs():
    import deathbycaptcha

    repo_root = Path(__file__).resolve().parents[2]
    custom_deathbycaptcha_file = repo_root / "custom_libs" / "deathbycaptcha.py"
    deathbycaptcha_path = Path(deathbycaptcha.__file__).resolve()

    requirements = (repo_root / "requirements.txt").read_text()
    custom_versions = (repo_root / "custom_libs" / "custom_version.txt").read_text()

    assert not custom_deathbycaptcha_file.exists()
    assert deathbycaptcha_path != custom_deathbycaptcha_file
    assert hasattr(deathbycaptcha, "SocketClient")
    assert deathbycaptcha.DEFAULT_TOKEN_TIMEOUT == 120
    assert "deathbycaptcha-official==4.7.1" in requirements
    assert "deathbycaptcha" not in custom_versions


def test_filebot_refiner_does_not_ship_libfilebot_or_pyads_packages():
    repo_root = Path(__file__).resolve().parents[2]
    custom_libs_dir = repo_root / "custom_libs"

    custom_versions = (custom_libs_dir / "custom_version.txt").read_text()
    filebot_refiner = (
        custom_libs_dir / "subliminal_patch" / "refiners" / "filebot.py"
    ).read_text()
    subzero_constants = (custom_libs_dir / "subzero" / "constants.py").read_text()

    assert not (custom_libs_dir / "libfilebot").exists()
    assert not (custom_libs_dir / "pyads.py").exists()
    assert "libfilebot" not in custom_versions
    assert "pyADS" not in custom_versions
    assert "pyads" not in custom_versions
    assert "from libfilebot" not in filebot_refiner
    assert "from pyads" not in filebot_refiner
    assert "libfilebot" not in subzero_constants


def test_py7zr_is_loaded_from_python_environment_not_custom_libs():
    import py7zr

    repo_root = Path(__file__).resolve().parents[2]
    custom_py7zr_dir = repo_root / "custom_libs" / "py7zr"
    py7zr_path = Path(py7zr.__file__).resolve()

    requirements = (repo_root / "requirements.txt").read_text()
    custom_versions = (repo_root / "custom_libs" / "custom_version.txt").read_text()

    assert not custom_py7zr_dir.exists()
    assert not py7zr_path.is_relative_to(custom_py7zr_dir)
    assert "py7zr==1.1.0" in requirements
    assert "py7zr" not in custom_versions


def test_msgpack_is_not_bundled_and_can_follow_signalrcore_dependency():
    import msgpack

    repo_root = Path(__file__).resolve().parents[2]
    libs_dir = repo_root / "libs"
    msgpack_path = Path(msgpack.__file__).resolve()

    assert not libs_dir.exists()
    assert not (libs_dir / "msgpack").exists()
    assert not msgpack_path.is_relative_to(libs_dir)


def test_third_party_libs_directory_is_not_part_of_runtime_or_tests():
    repo_root = Path(__file__).resolve().parents[2]

    assert not (repo_root / "libs").exists()
    assert "COPY libs" not in (repo_root / "Dockerfile").read_text()
    ci_lines = (repo_root / ".github" / "workflows" / "ci.yml").read_text().splitlines()
    assert "      - libs/**" not in ci_lines
    assert "\nlibs\n" not in (repo_root / ".github" / "files_to_copy").read_text()
    assert 'APP_DIR / "libs"' not in (repo_root / "docker" / "supervisor.py").read_text()
    assert "../libs" not in (repo_root / "tests" / "conftest.py").read_text()
    assert '"libs"' not in (repo_root / "tests" / "compat" / "conftest.py").read_text()
    assert "../libs/" not in (repo_root / "bazarr" / "app" / "libs.py").read_text()


def test_py_pretty_dependency_is_replaced_by_local_utility():
    repo_root = Path(__file__).resolve().parents[2]
    custom_libs_dir = repo_root / "custom_libs"

    custom_versions = (custom_libs_dir / "custom_version.txt").read_text()

    assert not (custom_libs_dir / "pretty").exists()
    assert "py-pretty" not in custom_versions

    for path in [
        repo_root / "bazarr" / "app" / "scheduler.py",
        repo_root / "bazarr" / "app" / "get_providers.py",
        repo_root / "bazarr" / "app" / "announcements.py",
        repo_root / "bazarr" / "api" / "movies" / "history.py",
        repo_root / "bazarr" / "api" / "movies" / "blacklist.py",
        repo_root / "bazarr" / "api" / "episodes" / "history.py",
        repo_root / "bazarr" / "api" / "episodes" / "blacklist.py",
    ]:
        lines = path.read_text().splitlines()
        assert "import pretty" not in lines


def test_pyyaml_is_loaded_from_python_environment_not_bundled_libs():
    repo_root = Path(__file__).resolve().parents[2]
    libs_dir = repo_root / "libs"
    yaml_path = Path(yaml.__file__).resolve()

    assert not yaml_path.is_relative_to(libs_dir)


def test_subliminal_is_loaded_from_python_environment_not_custom_libs():
    repo_root = Path(__file__).resolve().parents[2]
    custom_subliminal_dir = repo_root / "custom_libs" / "subliminal"
    subliminal_path = Path(subliminal.__file__).resolve()

    assert subliminal.__version__ == "2.6.0"
    assert not subliminal_path.is_relative_to(custom_subliminal_dir)


def test_startup_requirements_probe_covers_unvendored_runtime_imports():
    from app.requirements import RUNTIME_IMPORTS

    assert {
        "setuptools",
        "signalrcore",
        "subliminal",
        "flask_compress",
        "py7zr",
        "deathbycaptcha",
        "click_option_group",
        "tomlkit",
        "msgpack",
        "yaml",
    } <= set(RUNTIME_IMPORTS)


def test_startup_requirements_probe_rejects_wrong_pinned_versions(monkeypatch):
    from app import requirements

    monkeypatch.setattr(requirements, "RUNTIME_IMPORTS", ("subliminal",))
    monkeypatch.setattr(requirements, "RUNTIME_REQUIREMENTS", {"subliminal": ("subliminal", "==2.6.0")})
    monkeypatch.setattr(requirements.importlib, "import_module", lambda module: object())
    monkeypatch.setattr(requirements, "_module_origin", lambda module: None)
    monkeypatch.setattr(requirements.metadata, "version", lambda distribution: "2.5.0")

    assert requirements.missing_runtime_requirements() == ["subliminal"]


def test_startup_requirements_probe_supports_upper_bound_specs():
    from app.requirements import _satisfies_spec

    assert _satisfies_spec("1.2.58", "<=1.2.58")
    assert _satisfies_spec("1.2.57", "<=1.2.58")
    assert not _satisfies_spec("1.2.59", "<=1.2.58")


def test_startup_requirements_probe_rejects_removed_vendor_origins(monkeypatch):
    from app import requirements

    repo_root = Path(__file__).resolve().parents[2]

    monkeypatch.setattr(requirements, "RUNTIME_IMPORTS", ("subliminal",))
    monkeypatch.setattr(requirements, "RUNTIME_REQUIREMENTS", {"subliminal": ("subliminal", "==2.6.0")})
    monkeypatch.setattr(requirements.importlib, "import_module", lambda module: object())
    monkeypatch.setattr(
        requirements,
        "_module_origin",
        lambda module: repo_root / "custom_libs" / "subliminal" / "__init__.py",
    )
    monkeypatch.setattr(requirements.metadata, "version", lambda distribution: "2.6.0")

    assert requirements.missing_runtime_requirements() == ["subliminal"]


def test_legacy_sonarr_signalr_support_is_removed():
    repo_root = Path(__file__).resolve().parents[2]

    assert not (repo_root / "custom_libs" / "signalr").exists()
    assert not (repo_root / "custom_libs" / "sseclient.py").exists()

    custom_versions = (repo_root / "custom_libs" / "custom_version.txt").read_text()
    assert "signalr-client-threads" not in custom_versions
    assert "sseclient" not in custom_versions

    signalr_client = (repo_root / "bazarr" / "app" / "signalr_client.py").read_text()
    assert "SonarrSignalrClientLegacy" not in signalr_client
    assert "from signalr import Connection" not in signalr_client


def test_sonarr_sub_v4_api_compat_paths_are_removed():
    repo_root = Path(__file__).resolve().parents[2]

    sonarr_info = (repo_root / "bazarr" / "sonarr" / "info.py").read_text()
    assert "def is_legacy" not in sonarr_info
    assert '"/v3" if not get_sonarr_info.is_legacy() else ""' not in sonarr_info

    sonarr_sync_utils = (repo_root / "bazarr" / "sonarr" / "sync" / "utils.py").read_text()
    assert "languageprofile" not in sonarr_sync_utils
    assert "qualityProfileId" not in sonarr_sync_utils

    sonarr_parser = (repo_root / "bazarr" / "sonarr" / "sync" / "parser.py").read_text()
    assert "qualityProfileId" not in sonarr_parser
    assert "languageProfileId" not in sonarr_parser

    sonarr_episodes = (repo_root / "bazarr" / "sonarr" / "sync" / "episodes.py").read_text()
    assert "Sonarr v3" not in sonarr_episodes
    assert "get_sonarr_info.is_legacy()" not in sonarr_episodes


def test_sonarr_signalr_core_support_requires_known_v4(monkeypatch):
    from sonarr.info import GetSonarrInfo
    import semver

    info = GetSonarrInfo()

    monkeypatch.setattr(info, "version", lambda: "unknown")
    assert info.semver() is None
    assert info.supports_signalr_core() is False

    monkeypatch.setattr(info, "version", lambda: "3.0.10")
    assert info.supports_signalr_core() is False

    monkeypatch.setattr(info, "version", lambda: "4.0.9.2421")
    assert info.supports_signalr_core() is True
    assert info.semver() == semver.Version(4, 0, 9, "2421")

    monkeypatch.setattr(info, "version", lambda: "4.0.9.2244")
    assert info.semver() < semver.Version(4, 0, 9, "2421")
