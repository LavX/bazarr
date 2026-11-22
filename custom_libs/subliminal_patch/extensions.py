# coding=utf-8
from __future__ import absolute_import
from collections import OrderedDict
import threading

import subliminal
import babelfish


class ProviderRegistry(object):
    def __init__(self):
        self._providers = OrderedDict()
        # Registering, unregistering and listing names share this lock: a plugin that is detached while
        # Bazarr+ runs must not change the map under a walk of it, and a caller that swaps a class for
        # another (a detached plugin gives its place back to the built-in it shadowed) does it in one step.
        self.lock = threading.RLock()
        # How many times each id has changed hands between a Provider Hub plugin and anything else (a built-in, or
        # nothing). Counted here, under the lock, at the moment the class changes, and by nothing else: a settings
        # save never moves it. A configuration read is stamped with it (see ``subliminal_patch.core.ConfigSnapshot``).
        self._ownership = {}

    def __contains__(self, item):
        return item in self._providers

    def __setitem__(self, key, item):
        self.register(key, item)

    def __iter__(self):
        with self.lock:
            return iter(list(self._providers))

    def __getitem__(self, key):
        # One lookup, so a class removed between a membership test and the read is an answer of None and not a
        # KeyError. Callers hold the class they got and never look the name up twice.
        return self._providers.get(key)

    def get(self, key, default=None):
        return self._providers.get(key, default)

    def __repr__(self):
        return repr(self._providers)

    def __str__(self):
        return str(self._providers)

    def __len__(self):
        return len(self._providers)

    def __delitem__(self, key):
        if self.unregister(key) is None:
            raise KeyError(key)

    @staticmethod
    def _is_plugin(cls):
        return bool(getattr(cls, "hub_proxy", False))

    def _note_handover(self, name, old, new):
        if self._is_plugin(old) != self._is_plugin(new):
            self._ownership[name] = self._ownership.get(name, 0) + 1

    def owner(self, name):
        """Read the registered class and its handover stamp together."""
        with self.lock:
            return self._providers.get(name), self._ownership.get(name, 0)

    def ownership_generation(self, name):
        with self.lock:
            return self._ownership.get(name, 0)

    def ownership_generations(self):
        with self.lock:
            return dict(self._ownership)

    def register(self, name, cls):
        with self.lock:
            self._note_handover(name, self._providers.get(name), cls)
            self._providers[name] = cls

    def unregister(self, name, expected=None):
        """Take ``name`` out of the registry and return the class that was there, or None.

        With ``expected``, only that class is removed: a name that has since been registered to another class
        is left alone."""
        with self.lock:
            current = self._providers.get(name)
            if current is None or (expected is not None and current is not expected):
                return None
            self._note_handover(name, current, None)
            del self._providers[name]
            return current

    def names(self):
        with self.lock:
            return list(self._providers.keys())


provider_registry = ProviderRegistry()

# add language converters
try:
    babelfish.language_converters.unregister('addic7ed = subliminal.converters.addic7ed:Addic7edConverter')
except ValueError:
    pass

for entry_point in (
        'opensubtitles = babelfish.converters.opensubtitles:OpenSubtitlesConverter',
        'opensubtitles = subliminal.converters.opensubtitles:OpenSubtitlesConverter',
):
    try:
        babelfish.language_converters.unregister(entry_point)
    except ValueError:
        pass

try:
    del babelfish.language_converters.converters['opensubtitles']
except KeyError:
    pass

babelfish.language_converters.register('addic7ed = subliminal_patch.language:PatchedAddic7edConverter')
babelfish.language_converters.register('opensubtitles = subliminal_patch.language:PatchedOpenSubtitlesConverter')
babelfish.language_converters.register('szopensubtitles = subliminal_patch.language:PatchedOpenSubtitlesConverter')
subliminal.refiner_manager.register('sz_metadata = subliminal_patch.refiners.metadata:refine')
subliminal.refiner_manager.register('sz_omdb = subliminal_patch.refiners.omdb:refine')
subliminal.refiner_manager.register('sz_tvdb = subliminal_patch.refiners.tvdb:refine')
subliminal.refiner_manager.register('drone = subliminal_patch.refiners.drone:refine')
subliminal.refiner_manager.register('filebot = subliminal_patch.refiners.filebot:refine')
subliminal.refiner_manager.register('file_info_file = subliminal_patch.refiners.file_info_file:refine')
subliminal.refiner_manager.register('symlinks = subliminal_patch.refiners.symlinks:refine')
