# -*- coding: utf-8 -*-
import os
import pkgutil
import sys
from importlib.metadata import distributions

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../libs/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../bazarr/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../custom_libs/"))

PACKAGE_IMPORT_ALIASES = {
    "deathbycaptcha-official": "deathbycaptcha",
    "flask-compress": "flask_compress",
    "pyyaml": "yaml",
}


def pytest_report_header(config):
    conflicting_packages = _get_conflicting("libs")
    if conflicting_packages:
        return f"Conflicting packages detected:\n{conflicting_packages}"


def _get_conflicting(path):
    libs_packages = []
    for _, package_name, _ in pkgutil.iter_modules([path]):
        libs_packages.append(package_name)

    package_names = set()
    for package in distributions():
        package_name = package.metadata["Name"].lower()
        package_names.add(package_name)
        package_names.add(package_name.replace("-", "_"))
        if package_name in PACKAGE_IMPORT_ALIASES:
            package_names.add(PACKAGE_IMPORT_ALIASES[package_name])

    conflicting = []
    for installed in package_names:
        if installed in libs_packages:
            conflicting.append(installed)  # noqa: PERF401

    return conflicting
