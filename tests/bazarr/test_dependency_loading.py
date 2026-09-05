import subprocess
import sys
import re
import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
import subliminal
import yaml
import pytest


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


def test_python_anticaptcha_uses_pypi_distribution_not_git_source():
    from app.requirements import RUNTIME_REQUIREMENTS

    repo_root = Path(__file__).resolve().parents[2]
    requirements = (repo_root / "requirements.txt").read_text()

    assert RUNTIME_REQUIREMENTS["python_anticaptcha"] == ("python-anticaptcha", "==2.0.0")
    assert "python-anticaptcha==2.0.0" in requirements
    assert "github.com/morpheus65535/python-anticaptcha" not in requirements


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
    assert "py7zr==1.1.3" in requirements
    assert "py7zr" not in custom_versions


def test_msgpack_is_not_bundled_and_can_follow_signalrcore_dependency():
    import msgpack

    repo_root = Path(__file__).resolve().parents[2]
    libs_dir = repo_root / "libs"
    msgpack_path = Path(msgpack.__file__).resolve()

    assert not libs_dir.exists()
    assert not (libs_dir / "msgpack").exists()
    assert not msgpack_path.is_relative_to(libs_dir)


def test_binaries_json_does_not_self_download_rar_tools():
    # RAR archives are extracted via p7zip's 7z (Dockerfile installs p7zip-full,
    # no unrar/unar package). Listing unrar/unar here makes init_binaries try to
    # download them into the read-only /app/bazarr/bin dir at startup
    # (PermissionError) before falling back to 7z, so they must stay out.
    import json

    repo_root = Path(__file__).resolve().parents[2]
    binaries = json.loads(
        (repo_root / "bazarr" / "utilities" / "binaries.json").read_text()
    )
    names = {entry["name"].lower() for entry in binaries}

    assert "unrar" not in names
    assert "unrar.exe" not in names
    assert "unar" not in names


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


def test_docker_build_forces_setuptools_into_install_prefix():
    repo_root = Path(__file__).resolve().parents[2]
    dockerfile = (repo_root / "Dockerfile").read_text()

    assert 'pip install --prefix=/install --ignore-installed "setuptools>=82.0.1"' in dockerfile


def test_startup_requirements_probe_uses_security_patched_dependency_versions():
    from app.requirements import RUNTIME_REQUIREMENTS

    repo_root = Path(__file__).resolve().parents[2]
    requirements = (repo_root / "requirements.txt").read_text()

    expected_versions = {
        "cryptography": ("cryptography", ">=50.0.1"),
        "dynaconf": ("dynaconf", "==3.3.5"),
        "urllib3": ("urllib3", "==2.7.0"),
    }

    for module, (distribution, spec) in expected_versions.items():
        assert RUNTIME_REQUIREMENTS[module] == (distribution, spec)
        assert f"{distribution}{spec}" in requirements


def test_startup_declarations_match_installation_requirements():
    from app.requirements import RUNTIME_REQUIREMENTS, WINDOWS_RUNTIME_REQUIREMENTS

    repo_root = Path(__file__).resolve().parents[2]
    installed = {}
    for raw_line in (repo_root / "requirements.txt").read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        # pip's per-requirement options are not part of the version or marker.
        requirement = Requirement(re.split(r"\s+--", line, maxsplit=1)[0])
        installed[canonicalize_name(requirement.name)] = requirement

    declarations = {**RUNTIME_REQUIREMENTS, **WINDOWS_RUNTIME_REQUIREMENTS}
    separate_installs = {distribution for distribution, _ in declarations.values()
                         if canonicalize_name(distribution) not in installed}
    # signalrcore is installed separately with --no-deps to retain safe msgpack.
    assert separate_installs == {"signalrcore"}
    mismatches = {}
    for module, (distribution, spec) in declarations.items():
        if distribution == "signalrcore":
            continue
        requirement = installed[canonicalize_name(distribution)]
        if SpecifierSet(spec) != requirement.specifier:
            mismatches[module] = (spec, str(requirement.specifier))
        if module in WINDOWS_RUNTIME_REQUIREMENTS:
            assert requirement.marker is not None
            assert requirement.marker.evaluate({"platform_system": "Windows"})
            assert not requirement.marker.evaluate({"platform_system": "Linux"})
        else:
            assert requirement.marker is None
    assert mismatches == {}


def test_complete_compliant_runtime_does_not_request_install_or_restart(monkeypatch, tmp_path):
    from app import requirements

    monkeypatch.setenv("PLEXAPI_CONFIG_PATH", str(tmp_path / "plexapi.ini"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repairs, restarts = [], []
    monkeypatch.setattr(requirements, "install_requirements", lambda missing: repairs.append(missing) or True)
    monkeypatch.setattr(requirements, "restart_after_requirements_install", lambda: restarts.append(True))

    assert requirements.missing_runtime_requirements() == []
    assert requirements.ensure_requirements() is False
    assert requirements.ensure_requirements() is False
    assert repairs == restarts == []


@pytest.mark.parametrize("module,distribution,unsupported", [
    ("alembic", "alembic", "1.18.4"),
    ("cloudscraper", "cloudscraper", "1.2.72"),
    ("numpy", "numpy", "2.6.0"),
    ("PIL", "Pillow", "12.2.0"),
    ("dateutil", "python-dateutil", "2.9.0rc0"),
    ("dateutil", "python-dateutil", "2.9.0.dev0"),
    ("dateutil", "python-dateutil", "2.9.0"),
    ("dateutil", "python-dateutil", "2.9.0.post1"),
    ("dateutil", "python-dateutil", "invalid"),
])
def test_complete_runtime_repairs_an_unsupported_version_once(monkeypatch, tmp_path,
                                                             module, distribution, unsupported):
    from app import requirements

    monkeypatch.setenv("PLEXAPI_CONFIG_PATH", str(tmp_path / "plexapi.ini"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    actual_version = requirements.metadata.version
    installed_version = [unsupported]

    def version(name):
        if canonicalize_name(name) == canonicalize_name(distribution):
            return installed_version[0]
        return actual_version(name)

    monkeypatch.setattr(requirements.metadata, "version", version)
    repairs, restarts = [], []

    def install(missing):
        repairs.append(missing)
        installed_version[0] = actual_version(distribution)
        return True

    monkeypatch.setattr(requirements, "install_requirements", install)
    monkeypatch.setattr(requirements, "restart_after_requirements_install", lambda: restarts.append(True))

    assert requirements.missing_runtime_requirements() == [module]
    assert requirements.ensure_requirements(no_update=True) is False
    assert repairs == restarts == []
    assert requirements.ensure_requirements() is True
    assert repairs == [[module]]
    assert restarts == [True]
    assert requirements.missing_runtime_requirements() == []
    assert requirements.ensure_requirements() is False
    assert len(repairs) == len(restarts) == 1


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


VERSION_BOUNDARIES = [
    ("2.9.0rc0", "==2.9.0.post0", False),
    ("2.9.0.dev0", "==2.9.0.post0", False),
    ("2.9.0", "==2.9.0.post0", False),
    ("2.9.0.post0", "==2.9.0.post0", True),
    ("2.9.0.post1", "==2.9.0.post0", False),
    ("2.9.0.post0+vendor.1", "==2.9.0.post0", True),
    ("2.9.0.post0", "==2.9.0", False),
    ("50.0.1rc1", ">=50.0.1", False),
    ("50.0.1.dev1", ">=50.0.1", False),
    ("50.0.1", ">=50.0.1", True),
    ("50.0.1.post0", ">=50.0.1", True),
    ("50.0.1+vendor.1", ">=50.0.1", True),
    ("50.0.2rc1", ">=50.0.1", True),
    ("50.0.2.dev1", ">=50.0.1", True),
    ("1.0", "==1.0.0", True),
    ("v1.0.0", "==1.0", True),
    ("1.0+vendor.1", "==1.0", True),
    ("1.0+vendor.2", "==1.0+vendor.1", False),
    ("1.0+vendor.1", "==1.0+vendor.1", True),
    ("1!1.0", ">=2.0", True),
    ("2.0", ">=1!1.0", False),
    ("2.5.1", ">=2.5.2,<2.6.0", False),
    ("2.5.2", ">=2.5.2,<2.6.0", True),
    ("2.5.9rc1", ">=2.5.2,<2.6.0", True),
    ("2.6.0rc1", ">=2.5.2,<2.6.0", False),
    ("2.6.0.dev1", ">=2.5.2,<2.6.0", False),
    ("2.6.0", ">=2.5.2,<2.6.0", False),
    ("1.2.71", "<=1.2.71", True),
    ("1.2.71.post0", "<=1.2.71", False),
    ("1.2.71rc1", "<=1.2.71", True),
    ("1.0rc1", ">=1.0rc1,<1.0", False),
    ("1.0rc1", "==1.0rc1", True),
    ("1.0rc2", "==1.0rc1", False),
    ("unknown", ">=1.0", False),
    ("unknown", "<2.0", False),
    ("1.0-broken", "==1.0", False),
]


@pytest.mark.parametrize("installed,spec,expected", VERSION_BOUNDARIES)
def test_installed_versions_follow_pep440_boundaries(installed, spec, expected):
    from app.requirements import _satisfies_spec

    assert _satisfies_spec(installed, spec) is expected


@pytest.mark.parametrize("backend", ["packaging", "pip_vendored", "unavailable"])
def test_version_parser_bootstraps_with_available_production_dependencies(backend):
    repo_root = Path(__file__).resolve().parents[2]
    script = r'''
import importlib.abc
import json
import sys

backend, cases = sys.argv[1], json.loads(sys.argv[2])
class ParserBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if backend != "packaging" and (fullname == "packaging" or fullname.startswith("packaging.")):
            raise ModuleNotFoundError(name=fullname)
        if backend == "unavailable" and (fullname == "pip" or fullname.startswith("pip.")):
            raise ModuleNotFoundError(name=fullname)

sys.meta_path.insert(0, ParserBlocker())
from app import requirements
assert requirements.ensure_requirements(no_update=True) is False
assert "packaging.specifiers" not in sys.modules
assert "pip._vendor.packaging.specifiers" not in sys.modules
if backend == "unavailable":
    assert requirements._satisfies_spec("1.0", "==1.0") is False
    requirements.RUNTIME_IMPORTS = ("types",)
    requirements.RUNTIME_REQUIREMENTS = {"types": ("fixture", "==1.0")}
    requirements.metadata.version = lambda distribution: "1.0"
    requirements.importlib.util.find_spec = lambda name: None
    def forbidden_restart():
        raise AssertionError("restart without pip")
    requirements.restart_after_requirements_install = forbidden_restart
    assert requirements.ensure_requirements() is True
else:
    for installed, spec, expected in cases:
        assert requirements._satisfies_spec(installed, spec) is expected, (installed, spec)
    parser = "packaging.specifiers" if backend == "packaging" else "pip._vendor.packaging.specifiers"
    assert parser in sys.modules
    if backend == "pip_vendored":
        assert "packaging" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import sys; sys.path.insert(0, "
         + repr(str(repo_root / "bazarr")) + ");\n" + script, backend, json.dumps(VERSION_BOUNDARIES)],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("backend", ["packaging", "pip_vendored"])
def test_parser_import_does_not_hide_unrelated_missing_dependencies(monkeypatch, backend):
    import builtins
    from app.requirements import _satisfies_spec

    original_import = builtins.__import__

    def broken_parser(name, *args, **kwargs):
        if name == "packaging.specifiers":
            if backend == "pip_vendored":
                raise ModuleNotFoundError(name="packaging")
            raise ModuleNotFoundError(name="unrelated_parser_dependency")
        if name == "pip._vendor.packaging.specifiers":
            raise ModuleNotFoundError(name="unrelated_parser_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_parser)
    with pytest.raises(ModuleNotFoundError) as raised:
        _satisfies_spec("1.0", "==1.0")
    assert raised.value.name == "unrelated_parser_dependency"


def test_invalid_requirement_declarations_remain_errors():
    from app.requirements import _satisfies_spec

    with pytest.raises(ValueError):
        _satisfies_spec("1.0", "not-a-specifier")


@pytest.mark.parametrize('installed', ['48.0.0', '49.0.0', '50.0.0', '50.0.1', '50.0.2'])
def test_cryptography_security_floor_repairs_old_installs_once(monkeypatch, installed):
    from app import requirements

    monkeypatch.setattr(requirements, 'RUNTIME_IMPORTS', ('cryptography',))
    installed_version = [installed]
    monkeypatch.setattr(requirements.metadata, 'version', lambda distribution: installed_version[0])
    repairs, restarts = [], []

    def install(missing):
        repairs.append(missing)
        installed_version[0] = '50.0.1'
        return True

    monkeypatch.setattr(requirements, 'install_requirements', install)
    monkeypatch.setattr(requirements, 'restart_after_requirements_install', lambda: restarts.append(True))
    needs_repair = installed in ['48.0.0', '49.0.0', '50.0.0']
    assert requirements.missing_runtime_requirements() == (['cryptography'] if needs_repair else [])
    assert requirements.ensure_requirements() is needs_repair
    assert repairs == ([['cryptography']] if needs_repair else [])
    assert restarts == ([True] if needs_repair else [])
    assert requirements.ensure_requirements() is False
    assert len(repairs) == len(restarts) == int(needs_repair)


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


def test_sonarr_semver_preserves_build_number_on_nightly_or_ls_suffix(monkeypatch):
    """Nightly/develop builds report e.g. "4.0.9.2421-develop" and linuxserver
    images use "4.0.9.2421-ls123". The leading digits of the 4th segment are
    the actual Sonarr build number and must survive semver parsing: they drive
    the >= 4.0.9.2421 inline-episodeFile threshold in sync_episodes() and the
    v4 channel detection in supports_signalr_core() depends on major/minor/patch
    surviving as well. Dropping the build number to Version(4,0,9) falsely
    satisfies the threshold (release > prerelease) and skips legacy enrichment;
    returning None breaks v4 SignalR detection.
    """
    import semver

    from sonarr.info import GetSonarrInfo

    info = GetSonarrInfo()

    monkeypatch.setattr(info, "version", lambda: "4.0.9.2421-develop")
    assert info.semver() == semver.Version(4, 0, 9, "2421")
    assert info.supports_signalr_core() is True
    assert info.semver() >= semver.Version(4, 0, 9, "2421")

    monkeypatch.setattr(info, "version", lambda: "4.0.9.2421-ls123")
    assert info.semver() == semver.Version(4, 0, 9, "2421")
    assert info.supports_signalr_core() is True

    monkeypatch.setattr(info, "version", lambda: "4.0.9.2400-ls10")
    assert info.semver() < semver.Version(4, 0, 9, "2421")
    assert info.supports_signalr_core() is True

    monkeypatch.setattr(info, "version", lambda: "5.0.0.689-prerelease.0")
    assert info.semver() == semver.Version(5, 0, 0, "689")
    assert info.supports_signalr_core() is True
