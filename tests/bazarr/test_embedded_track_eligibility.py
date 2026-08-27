# coding=utf-8
"""Extraction has to agree with indexing about which track is which.

The indexer decides what the user is offered: it drops commentary tracks by
title, and it gives an undefined-language track the code configured in
``default_und_embedded_subtitles_lang``. Extraction then re-walks the same
stream list on its own and applies neither rule, so the two disagree in both
directions:

* a container whose first English stream is a commentary track has that
  commentary extracted and written out as the requested translation, silently,
  because the indexer never offered it and nothing downstream re-checks
* a track the indexer listed under the configured undefined-language code can
  never be extracted at all, because extraction skips any stream with no
  language field, so the item fails every time it is queued

Both were reported against pull request 357 and reached development unfixed.
The third case is the target-variant check in the collector: a forced file in
the target language is not the subtitle a plain translation would produce, so
it must not suppress the run.
"""
import os
from unittest.mock import MagicMock, patch

from babelfish import Language


def _track(alpha3=None, name=None, codec="subrip", forced=False, hi=False):
    track = {"format": codec, "forced": forced, "hearing_impaired": hi}
    if alpha3 is not None:
        track["language"] = Language(alpha3)
    if name is not None:
        track["name"] = name
    return track


def _extract(tmp_path, tracks, monkeypatch, **kwargs):
    """Drive extract_embedded_subtitle far enough to see which stream it picks."""
    from subtitles.tools.translate import batch

    chosen = {}

    def fake_ffmpeg(cmd, **kw):
        chosen['map'] = cmd[cmd.index('-map') + 1]
        out_path = cmd[-1]
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w') as handle:
            handle.write('1\n00:00:01,000 --> 00:00:02,000\nline\n')
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch('subtitles.tools.translate.batch.database') as mock_db,
        patch('subtitles.tools.translate.batch.parse_video_metadata',
              return_value={'ffprobe': {'subtitle': tracks}}),
        patch('subtitles.tools.translate.batch.get_binary', return_value='/usr/bin/ffmpeg'),
        patch('subtitles.tools.translate.batch.alpha3_from_alpha2',
              side_effect=lambda code: {'en': 'eng'}.get(code)),
        patch('app.get_args.args') as mock_args,
        patch('subprocess.run', side_effect=fake_ffmpeg),
    ):
        mock_db.execute.return_value.first.return_value = MagicMock(movie_file_id=1,
                                                                    file_size=1024)
        mock_args.config_dir = str(tmp_path)
        result = batch.extract_embedded_subtitle('/fake/movie.mkv', 'en', 'movie', **kwargs)

    return result, chosen.get('map')


def test_a_commentary_track_is_never_extracted(tmp_path, monkeypatch):
    """The indexer drops commentary by title, so it is not what the user chose."""
    tracks = [
        _track('eng', name='Commentary by the director'),
        _track('eng', name='English'),
    ]

    result, mapped = _extract(tmp_path, tracks, monkeypatch)

    assert result is not None, 'the real English track should still be extracted'
    assert mapped == '0:s:1', (
        f'extracted stream {mapped}, which is the commentary track the indexer '
        'never offered')


def test_a_commentary_only_container_extracts_nothing(tmp_path, monkeypatch):
    """Falling back to commentary is worse than reporting nothing: the caller
    reports failure, rather than translating the wrong text silently."""
    result, mapped = _extract(tmp_path, [_track('eng', name='Commentary')], monkeypatch)

    assert result is None, f'commentary was extracted anyway as stream {mapped}'


def test_an_undefined_track_uses_the_configured_language(tmp_path, monkeypatch):
    """The indexer records an und track under default_und_embedded_subtitles_lang,
    so the item is queued. Extraction skipped it, so the item always failed."""
    from app.config import settings

    monkeypatch.setattr(settings.general, 'default_und_embedded_subtitles_lang',
                        'en', raising=False)
    tracks = [_track(None, name='Untitled')]

    result, mapped = _extract(tmp_path, tracks, monkeypatch)

    assert result is not None, (
        'the track the indexer offered under the configured language could not '
        'be extracted')
    assert mapped == '0:s:0'


def test_an_undefined_track_stays_skipped_without_the_setting(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, 'default_und_embedded_subtitles_lang',
                        '', raising=False)

    result, _mapped = _extract(tmp_path, [_track(None, name='Untitled')], monkeypatch)

    assert result is None


# --------------------------------------------------- the target variant check

def test_a_forced_target_file_does_not_suppress_a_plain_translation():
    """en -> nl with an existing nl:forced file must still run: the forced file
    is not what this translation would produce."""
    from subtitles import mass_operations

    parsed = [('en', None), ('nl:forced', '/subs/m.nl.forced.srt')]

    assert not mass_operations._translate_output_present(
        parsed, target_lang='nl', hi=False, usable=lambda lang, path: True)


def test_a_plain_target_file_still_suppresses_the_translation():
    from subtitles import mass_operations

    parsed = [('en', None), ('nl', '/subs/m.nl.srt')]

    assert mass_operations._translate_output_present(
        parsed, target_lang='nl', hi=False, usable=lambda lang, path: True)


def test_a_missing_target_file_does_not_suppress_the_translation():
    """A row can name a file that is no longer on disk."""
    from subtitles import mass_operations

    parsed = [('en', None), ('nl', '/subs/gone.nl.srt')]

    assert not mass_operations._translate_output_present(
        parsed, target_lang='nl', hi=False, usable=lambda lang, path: False)


def test_the_hi_variant_is_matched_not_only_the_base_language():
    """An en:hi source produces nl:hi, which a plain nl file does not satisfy."""
    from subtitles import mass_operations

    parsed = [('en:hi', None), ('nl', '/subs/m.nl.srt')]

    assert not mass_operations._translate_output_present(
        parsed, target_lang='nl', hi=True, usable=lambda lang, path: True)


# ------------------------------------- the skip has to be per source, not per item

class TestPerVariantTargetSkip:
    """Whole-item skipping over-produces when only some variants are missing.

    An item with an ``en`` and an ``en:hi`` source and an existing ``nl`` file is
    not fully satisfied, because ``nl:hi`` is missing. Deciding that at the item
    level then queues BOTH English sources, so the plain one re-translates and
    overwrites the ``nl`` file that was already there. On a paid translator that
    is billed work for a file the user already had.
    """

    @staticmethod
    def _episode(subtitles):
        ep = MagicMock()
        ep.sonarrEpisodeId = 1
        ep.sonarrSeriesId = 10
        ep.path = '/video/ep1.mkv'
        ep.subtitles = subtitles
        return ep

    @staticmethod
    def _wire(mock_settings, mock_path_map):
        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_embedded_subs = True
        mock_path_map.path_replace_instance.side_effect = lambda p, *a, **kw: p
        mock_path_map.path_replace_reverse_instance.side_effect = lambda p, *a, **kw: p

    @patch('subtitles.mass_operations.is_sync_engine_output', return_value=False)
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_only_the_missing_variant_is_queued(self, mock_settings, mock_db, _sm, _se,
                                                mock_path_map, _isfile, _sync):
        from subtitles.mass_operations import _collect_subtitle_items

        self._wire(mock_settings, mock_path_map)
        episode = self._episode(
            "[['en', '/subs/e.en.srt', 10], ['en:hi', '/subs/e.en.hi.srt', 11], "
            "['nl', '/subs/e.nl.srt', 12]]")
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_subtitle_items(
            [{'type': 'episode', 'sonarrEpisodeId': 1}],
            action='translate', options={'from_lang': 'en', 'to_lang': 'nl'})[:2]

        queued = sorted((i['srt_lang'], i.get('hi', False)) for i in items)
        assert queued == [('en', True)], (
            'only the source whose output is missing should be queued; the plain '
            f'en source would overwrite the existing nl file. got {queued!r}')

    @patch('subtitles.mass_operations.is_sync_engine_output', return_value=False)
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_nothing_is_queued_when_every_variant_exists(self, mock_settings, mock_db,
                                                         _sm, _se, mock_path_map,
                                                         _isfile, _sync):
        from subtitles.mass_operations import _collect_subtitle_items

        self._wire(mock_settings, mock_path_map)
        episode = self._episode(
            "[['en', '/subs/e.en.srt', 10], ['nl', '/subs/e.nl.srt', 12]]")
        mock_db.execute.return_value.all.return_value = [episode]

        items = _collect_subtitle_items(
            [{'type': 'episode', 'sonarrEpisodeId': 1}],
            action='translate', options={'from_lang': 'en', 'to_lang': 'nl'})[0]

        assert items == [], f'nothing should be queued, got {items!r}'
