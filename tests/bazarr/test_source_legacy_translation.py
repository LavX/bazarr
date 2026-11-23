"""Manual source translations retain their captured input through legacy dispatch."""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from media_source_helpers import ARR_AND_SPORTS_TABLES, table_counts
import test_media_sources_tools as tools
from translator_dispatch_helpers import Env

world = tools.world
isolated_jobs = tools.isolated_jobs
toolbox = tools.toolbox


@pytest.fixture
def legacy_runtime(toolbox, monkeypatch):
    from app import database
    from app.config import settings
    from subtitles.tools.translate import runtime
    from subtitles.tools.translate.runtime import gate, legacy_engines, service
    from subtitles.tools.translate.services import lingarr_translator

    monkeypatch.setattr(settings.translator, 'runtime_v2', True)
    monkeypatch.setattr(settings.translator, 'lingarr_url', 'http://lingarr.example.test:9876')
    monkeypatch.setattr(database, 'engine', toolbox.db.get_bind())
    monkeypatch.setattr(runtime, '_tables_present', False)
    monkeypatch.setattr(runtime, '_absent_until', 0.0)
    monkeypatch.setattr(lingarr_translator, 'jobs_queue', toolbox.jobs)
    monkeypatch.setattr(lingarr_translator.LingarrTranslatorService, '_translate_content',
                        lambda self, lines, job_id: [{'position': i, 'line': f'Translated {line}'}
                                                     for i, line in enumerate(lines)])
    assert runtime.runtime_v2_enabled()
    env = Env(toolbox.db, toolbox.world.tmp_path)
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, name='Lingarr',
                                         settings={legacy_engines.SAVED_FLAG: True})
    env.pointers.engine = instance.id
    monkeypatch.setattr(gate, 'translation_gate', lambda: gate.evaluate(env.store, env.holds, env.workers))
    monkeypatch.setattr(service, 'Dispatcher', env.dispatcher)
    try:
        yield env
    finally:
        env.close()


def test_manual_source_api_legacy_translation_publishes_without_an_automatic_profile_rule(toolbox, legacy_runtime):
    from media_sources import repository

    context = tools.pipeline._episode(toolbox.world)
    path, _ = tools._sidecars(toolbox, context)
    target = Path(context.path).with_suffix('.hu.srt')
    target.write_text(tools.SRT.replace('Original cue', 'Previous translation'))
    before = table_counts(toolbox.db, ARR_AND_SPORTS_TABLES)

    job = tools._run(toolbox, tools._request(toolbox, context, path, 'translate', language='hu'))

    assert job.kwargs['expected_destination_versions'] == ()
    assert job.kwargs['source_tool'].sidecar.path == str(path)
    assert 'Translated Original cue' in target.read_text()
    assert table_counts(toolbox.db, ARR_AND_SPORTS_TABLES) == before
    (row,) = toolbox.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.action, row.subtitles_path) == (context.item_id, 6, str(target))
    history = legacy_runtime.history(legacy_runtime.last_job())
    assert (history['media_type'], history['media_id'], history['plugin_id'], history['status']) == (
        'source', context.item_id, 'lingarr', 'completed')


@pytest.mark.parametrize(('changed', 'when'), [
    ('subtitle', 'before-legacy'), ('video', 'before-legacy'),
    ('destination', 'before-legacy'),
    ('subtitle', 'during-translation'), ('video', 'during-translation'),
])
def test_manual_source_api_legacy_translation_preserves_output_when_files_change(
        toolbox, legacy_runtime, monkeypatch, changed, when):
    from media_sources import repository
    from subtitles.tools.translate.runtime import service
    from subtitles.tools.translate.services import lingarr_translator

    context = tools.pipeline._episode(toolbox.world)
    path, _ = tools._sidecars(toolbox, context)
    target = Path(context.path).with_suffix('.hu.srt')
    previous = tools.SRT.replace('Original cue', 'Previous translation')
    edited = tools.SRT.replace('Original cue', 'Edited translation')
    target.write_text(previous)

    def change_input():
        if changed == 'subtitle':
            path.write_text(tools.SRT.replace('Original cue', 'Edited cue'))
        elif changed == 'destination':
            target.write_text(edited)
        else:
            Path(context.path).write_bytes(b'Updated source video')

    if when == 'before-legacy':
        original = service.RuntimeTranslatorService._legacy

        def legacy(self, engine, instance):
            change_input()
            return original(self, engine, instance)

        monkeypatch.setattr(service.RuntimeTranslatorService, '_legacy', legacy)
    else:
        def translate(self, lines, job_id):
            change_input()
            return [{'position': i, 'line': f'Translated {line}'} for i, line in enumerate(lines)]

        monkeypatch.setattr(lingarr_translator.LingarrTranslatorService, '_translate_content', translate)

    tools._run(toolbox, tools._request(toolbox, context, path, 'translate', language='hu'))

    assert target.read_text() == (edited if changed == 'destination' else previous)
    assert toolbox.db.execute(sa.select(repository.History)).all() == []
    assert toolbox.world.published == []
    assert legacy_runtime.history(legacy_runtime.last_job())['status'] != 'completed'


def test_source_legacy_translation_keeps_runtime_input_version_without_a_manual_tool(
        toolbox, legacy_runtime, monkeypatch):
    from media_sources import repository
    from subtitles.tools.translate import main
    from subtitles.tools.translate.runtime import service

    context = tools.pipeline._episode(toolbox.world)
    path, _ = tools._sidecars(toolbox, context)
    target = Path(context.path).with_suffix('.hu.srt')
    original = service.RuntimeTranslatorService._legacy

    def legacy(self, engine, instance):
        path.write_text(tools.SRT.replace('Original cue', 'Edited cue'))
        return original(self, engine, instance)

    monkeypatch.setattr(service.RuntimeTranslatorService, '_legacy', legacy)
    job_id = main.translate_subtitles_file(context.path, str(path), 'en', 'hu', False, False, 'episode',
                                            None, None, None, None, source=context)
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == job_id and job.kwargs['source_tool'] is None
    assert toolbox.jobs._run_job(job)

    assert not target.exists()
    assert toolbox.db.execute(sa.select(repository.History)).all() == []


@pytest.mark.parametrize('change', ['setting', 'symlink'])
def test_source_legacy_translation_skips_when_the_configured_input_folder_moves(
        toolbox, legacy_runtime, monkeypatch, change):
    from app.config import settings
    from media_sources import repository
    from media_sources.indexer import index_item
    from subtitles.tools.translate import main
    from subtitles.tools.translate.runtime import service
    from subtitles.tools.translate.services import lingarr_translator

    context = tools.pipeline._episode(toolbox.world)
    video = Path(context.path)
    folder = video.parent / 'Subs'
    first = video.parent / 'Original subtitles'
    second = video.parent / 'Replacement subtitles'
    for directory in (first, second):
        directory.mkdir()
    name = video.with_suffix('.en.srt').name
    (first / name).write_text(tools.SRT)
    (second / name).write_text(tools.SRT.replace('Original cue', 'Replacement cue'))
    folder.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', folder.name)
    assert index_item(context.item_id) is not None
    path = folder / name
    target = video.with_suffix('.hu.srt')
    target.write_text(tools.SRT)
    original = service.RuntimeTranslatorService._legacy
    translated = []
    loaded = []
    original_load = lingarr_translator.pysubs2.load

    def legacy(self, engine, instance):
        if change == 'setting':
            (video.parent / 'Other').mkdir()
            monkeypatch.setattr(settings.general, 'subfolder_custom', 'Other')
            assert path.is_file() and path.read_text() == tools.SRT
        else:
            folder.unlink()
            folder.symlink_to(second, target_is_directory=True)
        return original(self, engine, instance)

    def load(path, **kwargs):
        loaded.append(str(path))
        return original_load(path, **kwargs)

    def translate(self, lines, job_id):
        translated.extend(lines)
        return [{'position': i, 'line': f'Translated {line}'} for i, line in enumerate(lines)]

    monkeypatch.setattr(service.RuntimeTranslatorService, '_legacy', legacy)
    monkeypatch.setattr(lingarr_translator.LingarrTranslatorService, '_translate_content', translate)
    monkeypatch.setattr(lingarr_translator.pysubs2, 'load', load)
    job_id = main.translate_subtitles_file(context.path, str(path), 'en', 'hu', False, False, 'episode',
                                            None, None, None, None, source=context)
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == job_id and job.kwargs['source_tool'] is None
    assert job.kwargs['expected_destination_versions'] == ()
    assert toolbox.jobs._run_job(job)

    assert loaded == [str(path)]
    assert translated == []
    assert target.read_text() == tools.SRT
    assert toolbox.db.execute(sa.select(repository.History)).all() == []


def test_automatic_source_legacy_translation_keeps_profile_eligibility(toolbox, legacy_runtime, monkeypatch):
    from app.config import settings
    from app.database import TableLanguagesProfiles, update_profile_id_list
    from media_sources import repository, translation

    profile = [{'id': 1, 'language': 'en', 'forced': 'False', 'hi': 'False'},
               {'id': 2, 'language': 'hu', 'forced': 'False', 'hi': 'False', 'translate_from': 'en'}]
    toolbox.db.execute(sa.update(TableLanguagesProfiles).values(items=json.dumps(profile)))
    update_profile_id_list.invalidate()
    monkeypatch.setattr(settings.translator, 'min_source_wait_minutes', 0)
    context = tools.pipeline._episode(toolbox.world)
    tools._sidecars(toolbox, context)

    assert translation.queue_missing_translations(context) == {'hu'}
    job = toolbox.jobs._reserve_next_job()
    assert job.kwargs['source_tool'] is None and len(job.kwargs['expected_destination_versions']) == 2
    assert toolbox.jobs._run_job(job)

    target = Path(context.path).with_suffix('.hu.srt')
    assert 'Translated Original cue' in target.read_text()
    (row,) = toolbox.db.execute(sa.select(repository.History)).all()
    assert (row.item_id, row.action, row.subtitles_path) == (context.item_id, 6, str(target))
