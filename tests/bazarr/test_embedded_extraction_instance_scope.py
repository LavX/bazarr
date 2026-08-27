# coding=utf-8
"""Embedded extraction has to stay on the instance that owns the media (#156).

The mass-translate collector maps the video path forward with the owning
instance's mapping. If extraction reverses it with the global one, the row
lookup misses on any secondary instance that has its own path_mappings, the
extraction returns nothing, and the failure is reported as though the track
were an unsupported bitmap codec.
"""
import os
from unittest.mock import MagicMock

import pytest



@pytest.fixture(autouse=True)
def language_table(monkeypatch):
    """alpha3_from_alpha2 reads a table the app fills at startup."""
    import subtitles.tools.translate.batch as batch

    monkeypatch.setattr(batch, 'alpha3_from_alpha2', lambda code: 'eng')


@pytest.fixture
def mapping_calls(monkeypatch):
    """Record which reverse mapping extraction reaches for."""
    import subtitles.tools.translate.batch as batch

    calls = {'global': [], 'instance': []}

    def _reverse(path):
        calls['global'].append(path)
        return path

    def _reverse_instance(path, arr_instance_id, kind):
        calls['instance'].append((path, arr_instance_id, kind))
        return path

    monkeypatch.setattr(batch.path_mappings, 'path_replace_reverse', _reverse)
    monkeypatch.setattr(batch.path_mappings, 'path_replace_reverse_movie', _reverse)
    monkeypatch.setattr(batch.path_mappings, 'path_replace_reverse_instance', _reverse_instance)
    return calls


def test_the_owning_instance_reverses_the_episode_path(mapping_calls, monkeypatch):
    import subtitles.tools.translate.batch as batch

    monkeypatch.setattr(batch, 'database', MagicMock())
    monkeypatch.setattr(batch, 'parse_video_metadata', lambda *a, **kw: None)

    batch.extract_embedded_subtitle('/media/Show/ep.mkv', 'en', 'episode',
                                    arr_instance_id=4)

    assert mapping_calls['instance'] == [('/media/Show/ep.mkv', 4, 'series')]
    assert mapping_calls['global'] == []


def test_the_owning_instance_reverses_the_movie_path(mapping_calls, monkeypatch):
    import subtitles.tools.translate.batch as batch

    monkeypatch.setattr(batch, 'database', MagicMock())
    monkeypatch.setattr(batch, 'parse_video_metadata', lambda *a, **kw: None)

    batch.extract_embedded_subtitle('/media/Film.mkv', 'en', 'movies',
                                    arr_instance_id=4)

    assert mapping_calls['instance'] == [('/media/Film.mkv', 4, 'movie')]
    assert mapping_calls['global'] == []


def test_without_an_instance_the_helper_still_maps_the_path(mapping_calls, monkeypatch):
    """path_replace_instance falls back to the global mapping for a None
    instance, so the single-item caller keeps working unchanged."""
    import subtitles.tools.translate.batch as batch

    monkeypatch.setattr(batch, 'database', MagicMock())
    monkeypatch.setattr(batch, 'parse_video_metadata', lambda *a, **kw: None)

    batch.extract_embedded_subtitle('/media/Show/ep.mkv', 'en', 'episode')

    assert mapping_calls['instance'] == [('/media/Show/ep.mkv', None, 'series')]


def test_the_row_lookup_is_scoped_to_the_owning_instance(monkeypatch):
    """Two instances can index the same path; an unscoped .first() would take
    an arbitrary row and feed the wrong file_size to the metadata cache."""
    import subtitles.tools.translate.batch as batch

    scoped_calls = []

    def _scoped(statement, column, arr_instance_id):
        scoped_calls.append(arr_instance_id)
        return statement

    monkeypatch.setattr(batch, 'scoped', _scoped)
    monkeypatch.setattr(batch, 'database', MagicMock())
    monkeypatch.setattr(batch, 'parse_video_metadata', lambda *a, **kw: None)
    monkeypatch.setattr(batch.path_mappings, 'path_replace_reverse_instance',
                        lambda p, i, k: p)

    batch.extract_embedded_subtitle('/media/Show/ep.mkv', 'en', 'episode',
                                    arr_instance_id=9)

    assert scoped_calls == [9]


class TestExtractionIsAtomic:
    """A half-written file must never be handed to a translator.

    ffmpeg wrote straight to the final path, and the cache check is only
    "exists and is not empty", so a second caller arriving mid-write took the
    partial file as a hit. One click could not race itself; a library-wide
    batch, whose translations are queued and read the file later, can.
    """

    def _prepare(self, monkeypatch, tmp_path, ffmpeg_behaviour):
        import subtitles.tools.translate.batch as batch

        monkeypatch.setattr(batch, 'alpha3_from_alpha2', lambda code: 'eng')
        monkeypatch.setattr(batch.path_mappings, 'path_replace_reverse_instance',
                            lambda p, i, k: p)
        monkeypatch.setattr(batch, 'scoped', lambda statement, column, instance: statement)
        media = MagicMock()
        media.file_size, media.episode_file_id = 1, 1
        database = MagicMock()
        database.execute.return_value.first.return_value = media
        monkeypatch.setattr(batch, 'database', database)
        monkeypatch.setattr(batch, 'parse_video_metadata', lambda *a, **kw: {
            'ffprobe': {'subtitle': [{'language': MagicMock(alpha3='eng'), 'forced': False,
                                      'hearing_impaired': False, 'format': 'subrip'}]}})
        monkeypatch.setattr(batch, 'get_binary', lambda name: '/usr/bin/ffmpeg')
        from app.get_args import args as bazarr_args
        monkeypatch.setattr(bazarr_args, 'config_dir', str(tmp_path))
        monkeypatch.setattr(batch.subprocess, 'run', ffmpeg_behaviour)

    def test_the_final_file_only_appears_once_it_is_complete(self, monkeypatch, tmp_path):
        """The path ffmpeg writes to must not be the path the cache check
        reads, so a concurrent reader sees either nothing or a finished file."""
        import subtitles.tools.translate.batch as batch

        written = {}

        def fake_run(cmd, **kwargs):
            target = cmd[-1]
            written['target'] = target
            with open(target, 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:01,000 --> 00:00:02,000\nhi\n')
            return MagicMock(returncode=0, stderr='')

        self._prepare(monkeypatch, tmp_path, fake_run)

        result = batch.extract_embedded_subtitle('/media/ep.mkv', 'en', 'episode')

        assert result is not None
        assert written['target'] != result, \
            'ffmpeg wrote directly to the path the cache check reads'
        assert os.path.exists(result)
        assert not os.path.exists(written['target']), 'the temporary file was left behind'

    def test_a_failed_extraction_leaves_no_file_at_the_cached_path(self, monkeypatch, tmp_path):
        import subtitles.tools.translate.batch as batch

        def fake_run(cmd, **kwargs):
            with open(cmd[-1], 'w', encoding='utf-8') as handle:
                handle.write('partial')
            return MagicMock(returncode=1, stderr='boom')

        self._prepare(monkeypatch, tmp_path, fake_run)

        assert batch.extract_embedded_subtitle('/media/ep.mkv', 'en', 'episode') is None
        leftovers = [n for n in os.listdir(tmp_path / 'extracted_subs')] \
            if os.path.isdir(tmp_path / 'extracted_subs') else []
        assert leftovers == [], leftovers

    def test_ffmpeg_is_told_the_format_and_given_a_recognisable_name(
            self, monkeypatch, tmp_path):
        """ffmpeg chooses its muxer from the output extension. Writing to a
        temporary name broke that: it exited with "Unable to choose an output
        format" and extracted nothing. Caught on a real deployment, because
        this suite stubs the subprocess away."""
        import subtitles.tools.translate.batch as batch

        seen = {}

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            with open(cmd[-1], 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:01,000 --> 00:00:02,000\nhi\n')
            return MagicMock(returncode=0, stderr='')

        self._prepare(monkeypatch, tmp_path, fake_run)
        batch.extract_embedded_subtitle('/media/ep.mkv', 'en', 'episode')

        cmd = seen['cmd']
        assert '-f' in cmd and cmd[cmd.index('-f') + 1] == 'srt', cmd
        assert cmd[-1].endswith('.srt'), cmd[-1]

    def test_two_concurrent_extractions_do_not_share_a_temporary_path(
            self, monkeypatch, tmp_path):
        """Jobs run as threads inside one process, so the pid is the same for
        both. Two extractions of the same variant would then write one file
        and the first os.replace would pull it out from under the second."""
        import subtitles.tools.translate.batch as batch

        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd[-1])
            with open(cmd[-1], 'w', encoding='utf-8') as handle:
                handle.write('1\n00:00:01,000 --> 00:00:02,000\nhi\n')
            return MagicMock(returncode=0, stderr='')

        self._prepare(monkeypatch, tmp_path, fake_run)

        first = batch.extract_embedded_subtitle('/media/ep.mkv', 'en', 'episode')
        os.remove(first)  # force a second cache miss for the same variant
        batch.extract_embedded_subtitle('/media/ep.mkv', 'en', 'episode')

        assert len(seen) == 2
        assert seen[0] != seen[1], seen
