# coding=utf-8
"""The manual subtitle toolbox for sports.

/api/subtitles declared its type as episode-or-movie and its else branch
treated anything non-episode as a movie, so a sports request came back
404 "Movie not found". That put the whole user-initiated toolbox, subsync with
its reference and engine choice, AI translate, and every subzero mod, out of
reach for sports while the automatic versions worked fine.
"""


import ast
import inspect

import pytest
from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library, sports  # noqa: F401


def test_the_endpoint_declares_the_third_media_type():
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles)
    assert '["episode", "movie", "sports"]' in source


def test_a_sports_event_resolves_its_own_row_not_a_movie():
    """The else branch used to swallow sports and answer 'Movie not found'."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert 'if media_type == "sports":' in source
    assert '"Sports event not found", 404' in source
    # Sports takes the leading branch, which is what pushed episode to elif.
    # Without that, sports fell through to the movie else and 404'd.
    assert 'elif media_type == "episode":' in source


def test_the_video_path_uses_the_sports_instance_mapping():
    """Everything downstream resolves against the owning instance, and a
    global mapping points at another instance's library when the two differ."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "path_replace_instance(\n                metadata.path, arr_instance_id, 'sports')" in source


def test_sync_runs_under_the_owned_publication_boundary():
    """Without it an operation that began before a resync replaced the
    recording could publish over the new file."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "sports_manual_operation(id, arr_instance_id)" in source
    assert "context=sports_context" in source
    assert "validate=sports_validate" in source
    assert "publication_guard=sports_guard" in source


def test_the_other_two_media_types_keep_their_behaviour():
    """Episodes and movies must pass None and go down exactly the path they
    always did."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.Subtitles.patch)
    assert "nullcontext((None, None, None, None))" in source


def test_the_helper_pins_the_file_signature():
    import inspect

    from sportarr import subtitles as sports_subtitles

    source = inspect.getsource(sports_subtitles.sports_manual_operation)
    assert "candidate_signature(context)" in source
    assert '"Sports file changed. Please try again."' in source


def test_postprocessing_reindexes_the_event_and_pushes_a_refresh():
    """The toolbox result is invisible until the event is re-indexed."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.postprocess_subtitles)
    assert "store_subtitles_sports(id, arr_instance_id)" in source
    assert 'event_stream(type="sports", payload=id)' in source


def test_postprocessing_makes_no_media_server_call_for_sports():
    """Both refresh helpers key on imdbId, which a sports event has not got."""
    import inspect

    from api.subtitles import subtitles

    source = inspect.getsource(subtitles.postprocess_subtitles)
    sports_branch = source[source.index('if media_type == "sports":'):source.index('if media_type == "episode":')]
    assert "plex_refresh_item" not in sports_branch
    assert "jellyfin_refresh_item" not in sports_branch


@pytest.fixture
def sports_toolbox(indexed_library, monkeypatch):  # noqa: F811
    from api.subtitles import subtitles as endpoint
    from app.config import settings
    from media_servers import events
    from sportarr import notify, workflows
    from sportarr import subtitles as service
    from subtitles.tools import mods

    session, folder = indexed_library
    module = sports(monkeypatch, session)
    module.store_subtitles_sports(61, 1)
    monkeypatch.setattr(endpoint, 'database', session)
    monkeypatch.setattr(service, 'database', session)
    monkeypatch.setattr(workflows, 'database', session)
    monkeypatch.setattr(settings.general, 'chmod_enabled', False)
    monkeypatch.setattr(mods, 'with_keep_lyrics', lambda chosen, owner: chosen)
    monkeypatch.setattr(endpoint, 'event_stream', lambda **kwargs: None)
    monkeypatch.setattr(events, 'notify_subtitle_mutation', lambda event: None)
    monkeypatch.setattr(notify, '_rescan_request', lambda owner, **kwargs: None)
    return endpoint, session, folder


def run_toolbox(endpoint, monkeypatch, **kwargs):
    args = dict(action='remove_tags', language='en', path='/sports/event.en.hi.srt',
                type='sports', id=61, arr_instance_id=1)
    args.update(kwargs)
    monkeypatch.setattr(endpoint.Subtitles.patch_request_parser, 'parse_args', lambda: args)
    return inspect.unwrap(endpoint.Subtitles.patch)(endpoint.Subtitles())


def test_toolbox_maps_indexed_remote_sidecar_before_editing(sports_toolbox, monkeypatch):
    from app.database import TableSportsEvents

    endpoint, session, folder = sports_toolbox
    subtitle = folder / '1' / 'event.en.hi.srt'
    subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n<i>A sporting event subtitle.</i>\n')
    original_other = (folder / '2' / 'event.de.forced.srt').read_bytes()
    assert run_toolbox(endpoint, monkeypatch) == ('', 204)
    assert '<i>' not in subtitle.read_text()
    session.expire_all()
    assert ['en:hi', '/sports/event.en.hi.srt'] in [entry[:2] for entry in ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)]
    assert (folder / '2' / 'event.de.forced.srt').read_bytes() == original_other


@pytest.mark.parametrize('kind', ['owner', 'local_sibling', 'traversal', 'symlink'])
def test_toolbox_refuses_paths_outside_the_selected_owner(sports_toolbox, monkeypatch, kind):
    endpoint, session, folder = sports_toolbox
    sibling = folder / '2' / 'event.de.forced.srt'
    original = sibling.read_bytes()
    args = {}
    if kind == 'owner':
        args['arr_instance_id'] = 2
    elif kind == 'local_sibling':
        args['path'] = str(sibling)
    elif kind == 'traversal':
        args['path'] = '/sports/../2/event.de.forced.srt'
    else:
        (folder / '1' / 'escape.srt').symlink_to(sibling)
        args['path'] = '/sports/escape.srt'
    _, status = run_toolbox(endpoint, monkeypatch, **args)
    assert status in (403, 404)
    assert sibling.read_bytes() == original


def test_toolbox_refreshes_destinations_once_after_a_renaming_mod(sports_toolbox, monkeypatch):
    from app.database import TableSportsEvents
    from app.config import settings
    from media_servers import events
    from sportarr import notify
    from subtitles import processing

    endpoint, session, folder = sports_toolbox
    refreshes = []
    publications = []
    monkeypatch.setattr(settings.general, 'use_plex', True)
    monkeypatch.setattr(settings.plex, 'sports_library', ['Sports'])
    monkeypatch.setattr(settings.general, 'use_jellyfin', True)
    monkeypatch.setattr(settings.jellyfin, 'sports_library_ids', ['sports-id'])
    monkeypatch.setattr(settings.general, 'use_emby', True)
    monkeypatch.setattr(events, 'notify_subtitle_mutation', publications.append)
    monkeypatch.setattr(processing, 'notify_subtitle_mutation', publications.append)
    monkeypatch.setattr(processing, 'plex_update_sports_library', lambda: refreshes.append('plex'))
    monkeypatch.setattr(processing, 'jellyfin_update_sports_library', lambda: refreshes.append('jellyfin'))
    monkeypatch.setattr(notify, 'notify_rescan', lambda owner: refreshes.append(('sportarr', owner)))
    assert run_toolbox(endpoint, monkeypatch, action='remove_HI',
                       path=str(folder / '1' / 'event.en.hi.srt')) == ('', 204)
    assert not (folder / '1' / 'event.en.hi.srt').exists()
    output = folder / '1' / 'event.en.srt'
    assert output.exists()
    session.expire_all()
    entries = ast.literal_eval(session.get(TableSportsEvents, 61).subtitles)
    assert ['en', '/sports/event.en.srt'] in [entry[:2] for entry in entries]
    assert sorted(refreshes, key=str) == sorted(['plex', 'jellyfin', ('sportarr', 1)], key=str)
    assert [(event.operation, event.subtitle_path) for event in publications] == [('edit', str(output))]


@pytest.mark.parametrize('entrypoint', ['toolbox', 'mass'])
def test_remove_hi_preserves_another_owners_absolute_destination(sports_toolbox, monkeypatch, entrypoint):
    from app.config import settings
    from app.database import TableSportsEvents
    from subtitles import mass_operations

    endpoint, session, folder = sports_toolbox
    shared = folder / 'shared'
    shared.mkdir()
    sibling = shared / 'event.en.srt'
    original = b'1\n00:00:00,000 --> 00:00:01,000\nSibling owner content\n'
    sibling.write_bytes(original)
    session.get(TableSportsEvents, 62).subtitles = repr([['en', str(sibling), len(original)]])
    session.commit()
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(shared))
    monkeypatch.setattr(mass_operations, 'database', session)
    source = folder / '1' / 'event.en.hi.srt'
    original_source = source.read_bytes()
    outcome = None
    if entrypoint == 'toolbox':
        outcome = run_toolbox(endpoint, monkeypatch, action='remove_HI')
    else:
        items, _ = mass_operations._collect_sports(event_ids=[61], sports_instance={61: {1}})
        try:
            outcome = mass_operations._process_subtitle_item(items[0], 'remove_HI', {}, 99)
        except ValueError:
            outcome = 'refused'
    assert sibling.read_bytes() == original
    assert source.read_bytes() == original_source
    assert outcome[1] == 409 if entrypoint == 'toolbox' else outcome == 'refused'


@pytest.mark.parametrize('entrypoint', ['toolbox', 'mass'])
@pytest.mark.parametrize('change', ['video', 'source', 'owner'])
def test_sports_mod_refuses_stale_inputs_before_mutation(sports_toolbox, monkeypatch, entrypoint, change):
    from app.database import TableSportsEvents
    from subtitles import mass_operations
    from subtitles.tools import mods

    endpoint, session, folder = sports_toolbox
    monkeypatch.setattr(mass_operations, 'database', session)
    source = folder / '1' / 'event.en.hi.srt'
    original = source.read_bytes()
    transform = mods.Subtitle.get_modified_content

    def changed_input(subtitle, **kwargs):
        content = transform(subtitle, **kwargs)
        if change == 'video':
            (folder / '1' / 'event.mkv').write_bytes(b'replaced video')
        elif change == 'source':
            source.write_bytes(original + b'\nNew source version\n')
        else:
            session.get(TableSportsEvents, 61).file_id = 999
            session.commit()
        return content

    monkeypatch.setattr(mods.Subtitle, 'get_modified_content', changed_input)
    if entrypoint == 'toolbox':
        outcome = run_toolbox(endpoint, monkeypatch, action='remove_HI')
        assert outcome[1] == 409
    else:
        items, _ = mass_operations._collect_sports(event_ids=[61], sports_instance={61: {1}})
        with pytest.raises(ValueError):
            mass_operations._process_subtitle_item(items[0], 'remove_HI', {}, 99)
    assert not (folder / '1' / 'event.en.srt').exists()
    assert source.read_bytes() == (original + b'\nNew source version\n' if change == 'source' else original)


def test_in_place_mod_uses_its_current_folder_when_new_outputs_are_ambiguous(sports_toolbox, monkeypatch):
    from app.config import settings
    from app.database import TableSportsEvents

    endpoint, session, folder = sports_toolbox
    shared = folder / 'shared'
    shared.mkdir()
    sibling = shared / 'event.en.srt'
    original = b'1\n00:00:00,000 --> 00:00:01,000\nSibling subtitle\n'
    sibling.write_bytes(original)
    session.get(TableSportsEvents, 62).subtitles = repr([['en', str(sibling), len(original)]])
    session.commit()
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(shared))
    source = folder / '1' / 'event.en.hi.srt'
    source.write_text('1\n00:00:00,000 --> 00:00:01,000\n<i>Own subtitle</i>\n')
    assert run_toolbox(endpoint, monkeypatch, action='remove_tags') == ('', 204)
    assert '<i>' not in source.read_text()
    assert sibling.read_bytes() == original
