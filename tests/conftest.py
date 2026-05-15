# -*- coding: utf-8 -*-
import os
import pkgutil
import sys
from importlib.metadata import distributions

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../libs/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../bazarr/"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../custom_libs/"))

def pytest_report_header(config):
    conflicting_packages = _get_conflicting("libs")
    if conflicting_packages:
        return f"Conflicting packages detected:\n{conflicting_packages}"


def _get_conflicting(path):
    libs_packages = []
    for _, package_name, _ in pkgutil.iter_modules([path]):
        libs_packages.append(package_name)

    package_names = [package.metadata["Name"].lower() for package in distributions()]
    unique_package_names = set(package_names)

    conflicting = []
    for installed in unique_package_names:
        if installed in libs_packages:
            conflicting.append(installed)  # noqa: PERF401

    return conflicting
