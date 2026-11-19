# Vendored from origin/development @ 3dc435b8f4f6aadbe3cae9236b12b0e4acf9ce01 (custom_libs/subzero/modification/registry.py).
# Frozen reference for the Remove HI differential gate. Do not edit.
# coding=utf-8
from __future__ import absolute_import
from collections import OrderedDict


class SubtitleModRegistry(object):
    mods = None
    mods_available = None

    def __init__(self):
        self.mods = OrderedDict()
        self.mods_available = []

    def register(self, mod):
        self.mods[mod.identifier] = mod
        self.mods_available.append(mod.identifier)

registry = SubtitleModRegistry()
