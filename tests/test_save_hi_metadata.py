"""Exercise the actual saver in isolation, with in-memory destinations only."""
import ast
import logging
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


def saver():
    path = Path(__file__).resolve().parents[1] / 'custom_libs/subliminal_patch/core.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if
             isinstance(node, ast.FunctionDef) and node.name in ('save_subtitles', 'parse_for_hi_regex')
             or isinstance(node, ast.Assign) and any(
                 isinstance(target, ast.Name) and target.id.startswith('HI_REGEX_')
                 for target in node.targets)]
    namespace = dict(re=re, os=os, logger=logging.getLogger(__name__),
                     SUBTITLE_FORMAT_EXTENSIONS={},
                     UnicodeDammit=lambda value: SimpleNamespace(unicode_markup=value),
                     get_subtitle_path=lambda path, language, **kw:
                     'episode.es' + ('.hi' if kw['hi_tag'] else '') + '.srt')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['save_subtitles']


class SaveHiMetadataTests(unittest.TestCase):
    def save(self, reported=False, verifiable=True, hi=False, provider='example',
             missing=False, mods=None):
        text = '1\n00:00:01,000 --> 00:00:02,000\n(ordinary dialogue)\n'
        content = text.encode()
        class Language:
            alpha3 = 'spa'
            forced = False
        language = Language()
        language.hi = hi
        subtitle = SimpleNamespace(
            provider_name=provider, hearing_impaired_verifiable=verifiable,
            language=language,
            hearing_impaired=hi, content=content, text=text, format='srt',
            mods=mods or [], get_modified_content=lambda **kw: content)
        if not missing:
            subtitle._reported_hearing_impaired = reported
        writes = []
        saver()('episode.mkv', [subtitle],
                write_subtitle=lambda path, data: writes.append((path, data)))
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1], content)
        return writes[0][0], subtitle

    def test_explicit_verified_normal_preserves_path_and_content_for_any_provider(self):
        for provider in ('moonfinbridge', 'another_hub_provider'):
            with self.subTest(provider=provider):
                path, subtitle = self.save(provider=provider)
                self.assertEqual(path, 'episode.es.srt')
                self.assertFalse(subtitle.language.hi)

    def test_unknown_unverified_and_malformed_metadata_keep_heuristic(self):
        for options in ({'missing': True}, {'reported': None}, {'reported': 0},
                        {'reported': 'false'}, {'verifiable': False},
                        {'verifiable': 1}, {'reported': True}):
            with self.subTest(options=options):
                path, subtitle = self.save(**options)
                self.assertEqual(path, 'episode.es.hi.srt')
                self.assertTrue(subtitle.language.hi)

    def test_existing_hi_is_never_cleared_by_negative_metadata(self):
        path, subtitle = self.save(hi=True)
        self.assertEqual(path, 'episode.es.hi.srt')
        self.assertTrue(subtitle.language.hi)

    def test_explicit_remove_hi_mod_retains_existing_behavior(self):
        path, subtitle = self.save(hi=True, reported=True, mods=['remove_HI'])
        self.assertEqual(path, 'episode.es.srt')
        self.assertFalse(subtitle.language.hi)
        self.assertFalse(subtitle.hearing_impaired)


if __name__ == '__main__':
    unittest.main()
