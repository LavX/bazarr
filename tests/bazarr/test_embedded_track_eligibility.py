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

    subtitles = [('en', None, None), ('nl:forced', '/subs/m.nl.forced.srt', 10)]
    parsed = [(lang, path) for lang, path, _size in subtitles]

    assert not mass_operations._translate_target_satisfied(
        parsed, target_lang='nl', source_lang='en',
        usable=lambda lang, path: True)


def test_a_plain_target_file_still_suppresses_the_translation():
    from subtitles import mass_operations

    parsed = [('en', None), ('nl', '/subs/m.nl.srt')]

    assert mass_operations._translate_target_satisfied(
        parsed, target_lang='nl', source_lang='en',
        usable=lambda lang, path: True)


def test_a_missing_target_file_does_not_suppress_the_translation():
    """A row can name a file that is no longer on disk."""
    from subtitles import mass_operations

    parsed = [('en', None), ('nl', '/subs/gone.nl.srt')]

    assert not mass_operations._translate_target_satisfied(
        parsed, target_lang='nl', source_lang='en',
        usable=lambda lang, path: False)


def test_the_hi_variant_is_matched_not_only_the_base_language():
    """en:hi translates to nl:hi, which a plain nl file does not satisfy."""
    from subtitles import mass_operations

    parsed = [('en:hi', None), ('nl', '/subs/m.nl.srt')]

    assert not mass_operations._translate_target_satisfied(
        parsed, target_lang='nl', source_lang='en',
        usable=lambda lang, path: True)
