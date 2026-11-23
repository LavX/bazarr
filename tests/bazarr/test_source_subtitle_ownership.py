"""Source tools use the same subtitle directories and ownership as source indexing."""
import ast
from pathlib import Path

import pytest

from media_source_helpers import add_item, video
import test_media_sources_tools as tools

world = tools.world
isolated_jobs = tools.isolated_jobs
toolbox = tools.toolbox


@pytest.mark.parametrize('layout', ['current', 'relative-child', 'relative-parent', 'absolute'])
def test_indexed_source_sidecars_remain_editable_in_supported_folders(toolbox, monkeypatch, layout):
    from app.config import settings
    from media_sources import repository
    from media_sources.indexer import index_item

    context = tools.pipeline._movie(toolbox.world)
    media_folder = Path(context.path).parent
    if layout == 'current':
        mode, custom, directory = 'current', '', media_folder
    elif layout == 'relative-child':
        mode, custom, directory = 'relative', 'Subs', media_folder / 'Subs'
    elif layout == 'relative-parent':
        mode, custom, directory = 'relative', '../Subs', media_folder / '..' / 'Subs'
    else:
        directory = toolbox.world.tmp_path / 'Shared subtitles'
        mode, custom = 'absolute', str(directory)
    monkeypatch.setattr(settings.general, 'subfolder', mode)
    monkeypatch.setattr(settings.general, 'subfolder_custom', custom)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / Path(context.path).with_suffix('.en.srt').name
    path.write_text(tools.SRT)
    assert index_item(context.item_id) is not None
    assert str(path) in repository.get_item(context.item_id).subtitles

    listing = toolbox.client.get('/api/editor/subtitles', headers=tools.HEADERS,
                                 query_string={'mediaType': 'source', 'mediaId': context.item_id})
    assert listing.status_code == 200
    assert listing.json['subtitles'] == [{'language': 'en', 'format': 'srt'}]
    url = f'/api/sources/items/{context.item_id}/subtitles/en/content'
    opened = toolbox.client.get(url, headers=tools.HEADERS)
    assert opened.status_code == 200 and opened.json['content'] == tools.SRT
    edited = tools.SRT.replace('Original cue', 'Edited cue')
    saved = toolbox.client.put(url, headers={**tools.HEADERS, 'If-Match': opened.headers['ETag']},
                                json={'content': edited})
    assert saved.status_code == 204
    assert path.read_text() == edited

    tools._run(toolbox, tools._request(toolbox, context, path, 'remove_tags'))
    assert 'Edited cue' in path.read_text() and '<i>' not in path.read_text()
    assert {entry[1] for entry in ast.literal_eval(repository.get_item(context.item_id).subtitles)} == {str(path)}


@pytest.mark.parametrize('location', ['unconfigured-child', 'configured-descendant', 'different-name'])
def test_source_tools_reject_listed_paths_without_exact_directory_and_video_ownership(toolbox, monkeypatch, location):
    from app.config import settings
    from media_sources import repository

    context = tools.pipeline._movie(toolbox.world)
    media_folder = Path(context.path).parent
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    if location == 'unconfigured-child':
        directory = media_folder / 'Archive'
    elif location == 'configured-descendant':
        directory = media_folder / 'Subs' / 'Archive'
    else:
        directory = media_folder / 'Subs'
    directory.mkdir(parents=True, exist_ok=True)
    name = 'Different.en.srt' if location == 'different-name' else Path(context.path).with_suffix('.en.srt').name
    path = directory / name
    path.write_text(tools.SRT)
    repository.update_item(context.item_id, toolbox.db, subtitles=repr([['en', str(path), path.stat().st_size]]))

    refused = tools._request(toolbox, context, path, 'remove_tags')
    assert refused.status_code == 409
    assert toolbox.client.get(f'/api/sources/items/{context.item_id}/subtitles/en/content',
                              headers=tools.HEADERS).status_code == 409
    assert not toolbox.jobs.jobs_pending_queue
    assert path.read_text() == tools.SRT


def test_shared_relative_folder_refuses_sidecar_with_two_video_owners(toolbox, monkeypatch):
    from app.config import settings
    from media_sources import repository

    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', '../Subs')
    context = tools.pipeline._movie(toolbox.world, 'A/Film.mkv')
    second = video(toolbox.world.root / 'B' / 'Film.mkv')
    add_item(toolbox.db, context.source_id, context.library_id, second)
    directory = toolbox.world.root / 'Subs'
    directory.mkdir()
    path = directory / 'Film.en.srt'
    path.write_text(tools.SRT)
    repository.update_item(context.item_id, toolbox.db, subtitles=repr([['en', str(path), path.stat().st_size]]))

    assert tools._request(toolbox, context, path, 'remove_tags').status_code == 409
    assert not toolbox.jobs.jobs_pending_queue
    assert path.read_text() == tools.SRT


def test_source_tool_validation_rechecks_shared_sidecar_ownership(toolbox, monkeypatch):
    from app.config import settings
    from media_sources.indexer import index_item
    from media_sources.subtitle_tools import SourceSubtitleTool, indexed_sidecar, tool_item
    from subtitles.tools.subsync_engines import SubtitleSourceChanged

    directory = toolbox.world.tmp_path / 'Shared subtitles'
    directory.mkdir()
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(directory))
    context = tools.pipeline._movie(toolbox.world, 'A/Film.mkv')
    path = directory / 'Film.en.srt'
    path.write_text(tools.SRT)
    assert index_item(context.item_id) is not None
    source, item = tool_item(context.item_id)
    captured = SourceSubtitleTool.capture(source, indexed_sidecar(source, item, path=str(path)))
    captured.validate()

    second = video(toolbox.world.root / 'B' / 'Film.mkv')
    add_item(toolbox.db, context.source_id, context.library_id, second)
    with pytest.raises(SubtitleSourceChanged):
        captured.validate()
    assert path.read_text() == tools.SRT


def test_source_tool_validation_rechecks_reference_ownership(toolbox):
    from media_sources.subtitle_tools import SourceSubtitleTool, indexed_sidecar, tool_item
    from subtitles.tools.subsync_engines import SubtitleSourceChanged

    context = tools.pipeline._movie(toolbox.world, 'Film.mkv')
    path, reference = tools._sidecars(toolbox, context)
    source, item = tool_item(context.item_id)
    selected = indexed_sidecar(source, item, path=str(path))
    captured = SourceSubtitleTool.capture(source, selected, indexed_sidecar(source, item, path=str(reference)))
    captured.validate()

    other_video = video(toolbox.world.root / 'Film.de.mkv')
    add_item(toolbox.db, context.source_id, context.library_id, other_video)
    assert indexed_sidecar(source, item, path=str(path)).read() == tools.SRT.encode()
    with pytest.raises(SubtitleSourceChanged):
        captured.validate()
    assert reference.read_text() == tools.SRT


def test_queued_source_mod_rechecks_the_configured_subtitle_folder(toolbox, monkeypatch):
    from app.config import settings
    from media_sources.indexer import index_item

    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Subs')
    context = tools.pipeline._movie(toolbox.world)
    directory = Path(context.path).parent / 'Subs'
    directory.mkdir()
    path = directory / Path(context.path).with_suffix('.en.srt').name
    path.write_text(tools.SRT)
    assert index_item(context.item_id) is not None
    queued = tools._request(toolbox, context, path, 'remove_tags')
    assert queued.status_code == 202

    monkeypatch.setattr(settings.general, 'subfolder_custom', 'Other subtitles')
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == queued.json['job_id']
    assert toolbox.jobs._run_job(job) is False
    assert toolbox.jobs.get_job_status(job.job_id) == 'failed'
    assert path.read_text() == tools.SRT


@pytest.mark.parametrize('custom', ['Subs', '../Subs'])
def test_source_video_links_use_the_stored_video_directory_for_relative_sidecars(toolbox, monkeypatch, custom):
    from app.config import settings
    from media_sources import repository
    from media_sources.context import resolve_item
    from media_sources.indexer import index_item

    context = tools.pipeline._movie(toolbox.world, 'Storage/Video/Film.mkv')
    linked_video = toolbox.world.root / 'Linked' / 'Film.mkv'
    linked_video.parent.mkdir()
    linked_video.symlink_to(context.path)
    repository.update_item(context.item_id, toolbox.db, path=str(linked_video))
    context = resolve_item(context.item_id)
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', custom)
    directory = linked_video.parent / custom
    directory.mkdir(exist_ok=True)
    path = directory / 'Film.en.srt'
    path.write_text(tools.SRT)
    assert index_item(context.item_id) is not None
    assert str(path) in repository.get_item(context.item_id).subtitles

    opened = toolbox.client.get(f'/api/sources/items/{context.item_id}/subtitles/en/content', headers=tools.HEADERS)
    assert opened.status_code == 200 and opened.json['content'] == tools.SRT
    tools._run(toolbox, tools._request(toolbox, context, path, 'remove_tags'))
    assert '<i>' not in path.read_text()
