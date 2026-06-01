# coding=utf-8

import importlib
import importlib.util
from importlib import metadata
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

from literals import ENV_RESTARTFILE, EXIT_NORMAL, EXIT_REQUIREMENTS_ERROR


RUNTIME_IMPORTS = (
    "setuptools",
    "aiohttp",
    "alembic",
    "apprise",
    "apscheduler",
    "babelfish",
    "bs4",
    "cachetools",
    "certifi",
    "chardet",
    "charset_normalizer",
    "click_option_group",
    "cloudscraper",
    "cryptography",
    "dateutil",
    "deathbycaptcha",
    "deep_translator",
    "dns",
    "dogpile",
    "dynaconf",
    "emoji",
    "engineio",
    "enzyme",
    "fcache",
    "fese",
    "ffmpeg",
    "ffsubsync",
    "autosubsync",
    "filetype",
    "flask",
    "flask_compress",
    "flask_cors",
    "flask_migrate",
    "flask_restx",
    "flask_socketio",
    "flask_sqlalchemy",
    "ftfy",
    "guess_language",
    "guessit",
    "itsdangerous",
    "jwt",
    "json_tricks",
    "knowit",
    "lxml",
    "msgpack",
    "numpy",
    "PIL",
    "plexapi",
    "py7zr",
    "pycountry",
    "pysrt",
    "pysubs2",
    "python_anticaptcha",
    "rarfile",
    "requests",
    "retry",
    "semver",
    "signalrcore",
    "six",
    "sqlalchemy",
    "srt",
    "subliminal",
    "textdistance",
    "tld",
    "tomlkit",
    "tzlocal",
    "unidecode",
    "urllib3",
    "waitress",
    "webrtcvad",
    "werkzeug",
    "whichcraft",
    "yaml",
)

WINDOWS_RUNTIME_IMPORTS = (
    "win32api",
    "win32con",
)

RUNTIME_REQUIREMENTS = {
    "setuptools": ("setuptools", ">=82.0.1"),
    "aiohttp": ("aiohttp", ">=3.13.5"),
    "alembic": ("alembic", "==1.18.4"),
    "apprise": ("apprise", "==1.9.8"),
    "apscheduler": ("apscheduler", "==3.11.2"),
    "babelfish": ("babelfish", "==0.6.1"),
    "bs4": ("beautifulsoup4", "==4.14.3"),
    "cachetools": ("cachetools", ">=7.1.1"),
    "certifi": ("certifi", "==2026.2.25"),
    "chardet": ("chardet", "==7.4.3"),
    "charset_normalizer": ("charset-normalizer", "==3.4.6"),
    "click_option_group": ("click-option-group", ">=0.5.6"),
    "cloudscraper": ("cloudscraper", "<=1.2.58"),
    "cryptography": ("cryptography", ">=48.0.0"),
    "dateutil": ("python-dateutil", "==2.9.0"),
    "deathbycaptcha": ("deathbycaptcha-official", "==4.7.1"),
    "deep_translator": ("deep-translator", "==1.11.4"),
    "dns": ("dnspython", "==2.8.0"),
    "dogpile": ("dogpile.cache", "==1.5.0"),
    "dynaconf": ("dynaconf", "==3.2.13"),
    "emoji": ("emoji", "==2.15.0"),
    "enzyme": ("enzyme", "==0.5.2"),
    "fcache": ("fcache", "==0.6.0"),
    "fese": ("fese", "==0.3.0"),
    "ffmpeg": ("ffmpeg-python", "==0.2.0"),
    "ffsubsync": ("ffsubsync", "==0.4.31"),
    "autosubsync": ("autosubsync", "==1.0.1"),
    "filetype": ("filetype", "==1.2.0"),
    "flask": ("Flask", "==3.1.3"),
    "flask_compress": ("Flask-Compress", "==1.24"),
    "flask_cors": ("flask-cors", "==6.0.2"),
    "flask_migrate": ("Flask-Migrate", "==4.1.0"),
    "flask_restx": ("flask-restx", "==1.3.2"),
    "flask_socketio": ("Flask-SocketIO", "==5.6.1"),
    "flask_sqlalchemy": ("flask_sqlalchemy", "==3.1.1"),
    "ftfy": ("ftfy", "==6.3.1"),
    "guess_language": ("guess_language-spirit", "==0.5.3"),
    "guessit": ("guessit", "==3.8.0"),
    "itsdangerous": ("itsdangerous", "==2.2.0"),
    "jwt": ("PyJWT", ">=2.12.1"),
    "json_tricks": ("json_tricks", "==3.17.3"),
    "knowit": ("knowit", "==0.5.11"),
    "lxml": ("lxml", ">=6.1.0"),
    "msgpack": ("msgpack", "==1.1.2"),
    "numpy": ("numpy", ">=2.0.0,<2.4.0"),
    "PIL": ("Pillow", ">=12.2.0"),
    "plexapi": ("plexapi", ">=4.16.1"),
    "py7zr": ("py7zr", "==1.1.0"),
    "pycountry": ("pycountry", "==26.2.16"),
    "pysrt": ("pysrt", "==1.1.2"),
    "pysubs2": ("pysubs2", "==1.8.0"),
    "python_anticaptcha": ("python-anticaptcha", "==2.0.0"),
    "rarfile": ("rarfile", "==4.2"),
    "requests": ("requests", "==2.34.2"),
    "retry": ("retry", "==0.9.2"),
    "semver": ("semver", "==3.0.4"),
    "signalrcore": ("signalrcore", "==1.0.2"),
    "six": ("six", "==1.17.0"),
    "sqlalchemy": ("sqlalchemy", "==2.0.49"),
    "srt": ("srt", "==3.5.3"),
    "subliminal": ("subliminal", "==2.6.0"),
    "textdistance": ("textdistance", "==4.6.3"),
    "tld": ("tld", "==0.13.2"),
    "tomlkit": ("tomlkit", ">=0.15.0"),
    "tzlocal": ("tzlocal", "==5.3.1"),
    "unidecode": ("unidecode", "==1.4.0"),
    "urllib3": ("urllib3", "==2.7.0"),
    "waitress": ("waitress", "==3.0.2"),
    "webrtcvad": ("webrtcvad-wheels", ">=2.0.14"),
    "werkzeug": ("werkzeug", "==3.1.8"),
    "whichcraft": ("whichcraft", "==0.6.1"),
    "yaml": ("PyYAML", ">=6.0.3"),
}

WINDOWS_RUNTIME_REQUIREMENTS = {
    "win32api": ("pywin32", ">=311"),
    "win32con": ("pywin32", ">=311"),
}

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_RUNTIME_ORIGINS = (REPO_ROOT / "custom_libs",)

UNVENDORED_RUNTIME_IMPORTS = {
    "signalrcore",
    "subliminal",
    "flask_compress",
    "py7zr",
    "deathbycaptcha",
    "python_anticaptcha",
    "click_option_group",
    "tomlkit",
    "msgpack",
    "yaml",
}


def is_virtualenv():
    base_prefix = getattr(sys, "base_prefix", None)
    real_prefix = getattr(sys, "real_prefix", None) or sys.prefix
    return base_prefix != real_prefix


def _version_tuple(version):
    parts = re.findall(r"\d+", version.split("+", 1)[0].split("-", 1)[0])
    return tuple(int(part) for part in parts)


def _satisfies_spec(installed_version, spec):
    installed = _version_tuple(installed_version)
    for item in spec.split(","):
        item = item.strip()
        if item.startswith("=="):
            if installed != _version_tuple(item[2:]):
                return False
        elif item.startswith(">="):
            if installed < _version_tuple(item[2:]):
                return False
        elif item.startswith("<="):
            if installed > _version_tuple(item[2:]):
                return False
        elif item.startswith("<"):
            if installed >= _version_tuple(item[1:]):
                return False
        else:
            raise ValueError(f"Unsupported requirement specifier: {item}")
    return True


def _module_origin(module):
    spec = importlib.util.find_spec(module)
    if not spec or not spec.origin or spec.origin in ("built-in", "frozen"):
        return None
    return Path(spec.origin).resolve()


def _has_forbidden_origin(module):
    origin = _module_origin(module)
    if not origin:
        return False
    return any(origin.is_relative_to(path) for path in FORBIDDEN_RUNTIME_ORIGINS)


def _requirement_is_satisfied(distribution, spec):
    try:
        installed_version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return False
    return _satisfies_spec(installed_version, spec)


def missing_runtime_requirements():
    missing = set()
    probes = list(RUNTIME_IMPORTS)
    requirements = dict(RUNTIME_REQUIREMENTS)
    if os.name == "nt":
        probes.extend(WINDOWS_RUNTIME_IMPORTS)
        requirements.update(WINDOWS_RUNTIME_REQUIREMENTS)

    for module in probes:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.add(module)
            continue

        if module in UNVENDORED_RUNTIME_IMPORTS and _has_forbidden_origin(module):
            missing.add(module)
            continue

        requirement = requirements.get(module)
        if requirement and not _requirement_is_satisfied(*requirement):
            missing.add(module)

    return sorted(missing)


def install_requirements(missing_modules=None):
    if importlib.util.find_spec("pip") is None:
        logging.info(
            "BAZARR unable to install requirements because pip is not installed."
        )
        return False

    if os.path.expanduser("~") == "/":
        logging.info(
            "BAZARR unable to install requirements because the user has no home directory."
        )
        return False

    logging.info(
        "BAZARR installing requirements. Missing imports: %s",
        ", ".join(missing_modules or []),
    )

    pip_command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-qq",
        "--disable-pip-version-check",
        "-r",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "requirements.txt",
        ),
    ]
    if not is_virtualenv():
        pip_command.insert(4, "--user")

    try:
        subprocess.check_output(pip_command, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logging.exception("BAZARR requirements.txt installation result: %s", e.stdout)
        os._exit(EXIT_REQUIREMENTS_ERROR)

    logging.info("BAZARR requirements installed.")
    return True


def restart_after_requirements_install():
    restart_file = os.environ.get(ENV_RESTARTFILE)
    if restart_file:
        try:
            Path(restart_file).touch()
        except Exception:
            logging.exception(
                "BAZARR cannot create restart file after installing requirements."
            )
        else:
            os._exit(EXIT_NORMAL)

    os.execv(sys.executable, [sys.executable] + sys.argv)


def ensure_requirements(no_update=False):
    if no_update:
        return False

    missing = missing_runtime_requirements()
    if not missing:
        return False

    if install_requirements(missing):
        restart_after_requirements_install()

    return True
