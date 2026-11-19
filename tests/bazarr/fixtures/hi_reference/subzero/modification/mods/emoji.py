# Vendored from origin/development @ 3dc435b8f4f6aadbe3cae9236b12b0e4acf9ce01 (custom_libs/subzero/modification/mods/emoji.py).
# Frozen reference for the Remove HI differential gate. Do not edit.
# coding=utf-8
from __future__ import absolute_import
from __future__ import unicode_literals

import emoji

from subzero.modification.mods import SubtitleModification
from subzero.modification import registry


class Emoji(SubtitleModification):
    identifier = "emoji"
    description = "Remove Emoji"
    exclusive = True
    advanced = True
    modifies_whole_file = True
    apply_last = False

    long_description = "Removes emoji characters from subtitles"

    def modify(self, content, debug=False, parent=None, **kwargs):
        for entry in parent.f:
            entry.text = emoji.replace_emoji(entry.text)


registry.register(Emoji)
