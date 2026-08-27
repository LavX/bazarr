# coding=utf-8
"""A translation has to be re-indexed against the instance that asked for it.

``translate_subtitles_file`` finishes by calling ``postprocess_subtitles``, which
re-indexes the new file. It never passed the owning instance, so that call took
the ``None`` default and ``store_subtitles`` fell back to resolving an owner from
the path.

That was survivable while the indexer wrote every row sharing a path. It is not
now: the write is scoped, so the translated subtitle can be indexed into a
sibling instance's row while the row the user actually asked about stays stale,
showing no subtitle for a file that exists.
"""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def translate_harness(monkeypatch, tmp_path):
    """Stub everything around the translation so the call reaches postprocess."""
    from subtitles.tools.translate import main as translate_main
    from subzero.language import Language

    monkeypatch.setattr(translate_main, 'validate_translation_params', lambda *a, **kw: None)
    monkeypatch.setattr(translate_main, 'convert_language_codes',
                        lambda to_lang, forced, hi: (Language('nld'), to_lang))
    monkeypatch.setattr(translate_main, 'get_subtitle_path',
                        lambda *a, **kw: str(tmp_path / 'video.nl.srt'))
    monkeypatch.setattr(translate_main, 'get_external_subtitles_path',
                        lambda file, subtitle: str(tmp_path / subtitle))
    monkeypatch.setattr(translate_main, 'alpha3_from_alpha2', lambda code: 'nld')

    translator = MagicMock()
    translator.translate.return_value = True
    monkeypatch.setattr(translate_main.TranslatorFactory, 'create_translator',
                        staticmethod(lambda *a, **kw: translator))
    return translate_main


def test_the_owner_reaches_the_reindex_after_a_translation(translate_harness):
    translate_main = translate_harness
    seen = {}

    def _postprocess(subtitles_path, video_path, media_type, metadata, id,
                     arr_instance_id=None):
        seen['arr_instance_id'] = arr_instance_id

    with (
        patch('api.subtitles.subtitles.postprocess_subtitles', _postprocess),
        patch('subtitles.tools.combine.main.try_combine_for_video', lambda **kw: None),
    ):
        translate_main.translate_subtitles_file(
            video_path='/local/tv/s/e.mkv', source_srt_file='/local/tv/s/e.en.srt',
            from_lang='en', to_lang='nl', forced=False, hi=False,
            media_type='episode', sonarr_series_id=1, sonarr_episode_id=42,
            radarr_id=None, metadata={}, job_id='job-1', arr_instance_id=3)

    assert seen['arr_instance_id'] == 3, (
        'the re-index after a translation ran unscoped, so it can write the '
        "translated subtitle into a sibling instance's row")


def test_no_owner_still_works_for_a_single_instance_install(translate_harness):
    translate_main = translate_harness
    seen = {}

    def _postprocess(subtitles_path, video_path, media_type, metadata, id,
                     arr_instance_id=None):
        seen['arr_instance_id'] = arr_instance_id

    with (
        patch('api.subtitles.subtitles.postprocess_subtitles', _postprocess),
        patch('subtitles.tools.combine.main.try_combine_for_video', lambda **kw: None),
    ):
        translate_main.translate_subtitles_file(
            video_path='/local/movies/m.mkv', source_srt_file='/local/movies/m.en.srt',
            from_lang='en', to_lang='nl', forced=False, hi=False,
            media_type='movies', sonarr_series_id=None, sonarr_episode_id=None,
            radarr_id=9, metadata={}, job_id='job-1')

    assert seen['arr_instance_id'] is None
