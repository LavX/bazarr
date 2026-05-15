# coding=utf-8

import importlib
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys

from literals import ENV_RESTARTFILE, EXIT_NORMAL, EXIT_REQUIREMENTS_ERROR


RUNTIME_IMPORTS = (
    "setuptools",
    "signalrcore",
    "subliminal",
    "flask_compress",
    "py7zr",
    "deathbycaptcha",
    "click_option_group",
    "tomlkit",
    "aiohttp",
    "cachetools",
    "lxml",
    "numpy",
    "webrtcvad",
    "PIL",
    "cryptography",
    "jwt",
    "yaml",
    "rarfile",
)

WINDOWS_RUNTIME_IMPORTS = (
    "win32api",
    "win32con",
)


def is_virtualenv():
    base_prefix = getattr(sys, "base_prefix", None)
    real_prefix = getattr(sys, "real_prefix", None) or sys.prefix
    return base_prefix != real_prefix


def missing_runtime_requirements():
    missing = []
    probes = list(RUNTIME_IMPORTS)
    if os.name == "nt":
        probes.extend(WINDOWS_RUNTIME_IMPORTS)

    for module in probes:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)

    return missing


def install_requirements(missing_modules=None):
    if importlib.util.find_spec("pip") is None:
        logging.info("BAZARR unable to install requirements because pip is not installed.")
        return False

    if os.path.expanduser("~") == "/":
        logging.info("BAZARR unable to install requirements because the user has no home directory.")
        return False

    logging.info("BAZARR installing requirements. Missing imports: %s", ", ".join(missing_modules or []))

    pip_command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-qq",
        "--disable-pip-version-check",
        "-r",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "requirements.txt"),
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
            logging.exception("BAZARR cannot create restart file after installing requirements.")
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
