# coding=utf-8
"""Guards and filters a review round found too wide or too narrow.

Each test pins one defect that was live on the branch: a guard that refused
work it was meant to allow, a gate that let work through after the user closed
it, a loop that let one instance end a whole job, and two lists that described
less than the page beside them showed.
"""

import json
from types import SimpleNamespace

import pytest
from test_sportarr_webhook import webhook, _post  # noqa: F401


# --------------------------------------------------------------------------
# The sports source guard and server-generated extractions.
# --------------------------------------------------------------------------

@pytest.fixture
def extraction(monkeypatch, tmp_path):
    """An event, its video, and the extraction directory the server writes to."""
    from app import get_args
    from subtitles.tools.translate import batch

    config_dir = tmp_path / 'config'
    media_dir = tmp_path / 'sports'
    media_dir.mkdir()
    (config_dir / 'extracted_subs').mkdir(parents=True)
    monkeypatch.setattr(get_args.args, 'config_dir', str(config_dir))
    video = media_dir / 'race.mkv'
    video.write_bytes(b'video')
    context = SimpleNamespace(mapped_path=str(video))
    extracted = (config_dir / 'extracted_subs' /
                 f'{batch.extracted_subtitle_key(str(video))}.en.srt')
    extracted.write_text('1\n')
    return context, video, extracted, config_dir / 'extracted_subs'


def test_the_guard_accepts_this_events_own_extracted_track(extraction):
    """Embedded sports translation was refused outright.

    extract_embedded_subtitle() writes the track to
    <config_dir>/extracted_subs/<key>..., and both the subtitle toolbox and the
    mass-translate runner hand that file to manual_translation_operation. The
    guard only described sidecars next to the video, so every embedded sports
    translation died on "Sports subtitle source does not belong to its event".
    """
    from sportarr import profile_hooks

    context, _, extracted, _ = extraction
    profile_hooks._source_path(context, str(extracted))


def test_the_guard_still_refuses_what_the_server_did_not_write(extraction):
    """Verified, not merely located: the directory alone is not the licence.

    A file that happens to sit in extracted_subs, or another event's
    extraction, does not carry the cache name an extraction of this event's
    video would be given, so it stays outside the operation's source set.
    """
    from sportarr import profile_hooks
    from subtitles.tools.translate import batch

    context, _, _, extract_dir = extraction
    stranger = extract_dir / 'anything.en.srt'
    stranger.write_text('1\n')
    other = extract_dir / f'{batch.extracted_subtitle_key("/sports/other.mkv")}.en.srt'
    other.write_text('1\n')
    for path in (stranger, other):
        with pytest.raises(ValueError):
            profile_hooks._source_path(context, str(path))


def test_the_guard_refuses_a_symlink_in_the_extraction_directory(extraction):
    """The extraction arm widens where a source may live, not what it may be.

    A symlink named like this event's extraction would otherwise read an
    arbitrary file through a directory the server owns.
    """
    from sportarr import profile_hooks
    from subtitles.tools.translate import batch

    context, _, _, extract_dir = extraction
    secret = extract_dir.parent / 'config.ini'
    secret.write_text('apikey\n')
    link = extract_dir / f'{batch.extracted_subtitle_key(str(context.mapped_path))}.de.srt'
    link.symlink_to(secret)
    with pytest.raises(ValueError):
        profile_hooks._source_path(context, str(link))


def test_the_sidecar_rule_is_unchanged(extraction):
    """The event's own sidecars still pass and strangers still do not."""
    from sportarr import profile_hooks

    context, video, _, _ = extraction
    sidecar = video.parent / 'race.en.srt'
    sidecar.write_text('1\n')
    profile_hooks._source_path(context, str(sidecar))

    unrelated = video.parent / 'other.en.srt'
    unrelated.write_text('1\n')
    with pytest.raises(ValueError):
        profile_hooks._source_path(context, str(unrelated))
    with pytest.raises(ValueError):
        profile_hooks._source_path(context, str(video.parent / 'race.gone.srt'))


# --------------------------------------------------------------------------
# The master toggle is the shutdown boundary for the import hook too.
# --------------------------------------------------------------------------

def test_the_webhook_stops_at_the_master_toggle(webhook, monkeypatch):  # noqa: F811
    """Turning Sportarr off did not stop an import from restarting searches.

    An instance row stays enabled when the master toggle goes off, so a keyed
    hook still resolved an owner, indexed the event and queued
    automatic_search_sports() behind the user's back. The scheduler and the SSE
    client both treat the toggle as the boundary; this one did not.
    """
    from api.webhooks import sportarr

    _, namespace, calls = webhook
    monkeypatch.setattr(sportarr.settings.general, 'use_sportarr', False)
    _, status = _post(monkeypatch, namespace, calls,
                      {'eventType': 'Download', 'episodeFiles': [{'id': 71}]},
                      stable_key='sportarr-1')
    assert status == 200
    assert calls == []


# --------------------------------------------------------------------------
# One unreachable Sportarr must not end the health job.
# --------------------------------------------------------------------------

def test_one_offline_sportarr_does_not_abort_health(schema_session, monkeypatch, tmp_path):
    """A single raise took the whole job with it.

    sync_rootfolders() raises on an offline instance, a non-200 answer or a
    malformed payload, and the loop was unguarded: the owners after it went
    unchecked, the badge event and backup_rotation() never ran, and the job
    never reached its terminal rename.
    """
    from app import database as app_database
    from app.database import TableArrInstances
    from sportarr import rootfolder
    from utilities import backup, health

    for owner in (1, 2):
        schema_session.add(TableArrInstances(
            id=owner, kind='sportarr', name=str(owner), stable_key=str(owner), port=1867,
            enabled=1, path_mappings=json.dumps([['/sports', str(tmp_path)]])))
    schema_session.commit()
    for module in (health, rootfolder, app_database):
        monkeypatch.setattr(module, 'database', schema_session)
    # The language table is built at app start, and nothing starts the app
    # here, so the endpoint's alpha2/alpha3 lookups would quietly drop every
    # code and hide what this test is about.
    from languages import get_languages

    monkeypatch.setattr(get_languages, 'languages_dict',
                        [{'code2': 'en', 'code3': 'eng', 'name': 'English'},
                         {'code2': 'hu', 'code3': 'hun', 'name': 'Hungarian'}],
                        raising=False)
    monkeypatch.setattr(health.settings.general, 'use_sportarr', True)
    monkeypatch.setattr(health.settings.general, 'use_sonarr', False)
    monkeypatch.setattr(health.settings.general, 'use_radarr', False)
    monkeypatch.setattr(rootfolder, 'notify', lambda *args: None)

    refreshed, finished = [], []

    def _sync(arr_instance_id, **kwargs):
        refreshed.append(arr_instance_id)
        if arr_instance_id == 1:
            raise ConnectionError('Sportarr is offline')

    monkeypatch.setattr(rootfolder, 'sync_rootfolders', _sync)
    monkeypatch.setattr(health, 'event_stream', lambda **kwargs: finished.append('badges'))
    monkeypatch.setattr(backup, 'backup_rotation', lambda: finished.append('backup'))
    monkeypatch.setattr(health.jobs_queue, 'update_job_name',
                        lambda **kwargs: finished.append(kwargs['new_job_name']))

    health.check_health(job_id=7)

    assert refreshed == [1, 2]
    assert finished == ['badges', 'backup', 'Checked Health']


# --------------------------------------------------------------------------
# The single-league profile refresh is queued, like the batch one.
# --------------------------------------------------------------------------

def test_the_single_league_refresh_is_queued_best_effort():
    """The PATCH walked every event of the league while the request was open.

    assign_profile() has already committed by then, so a timeout or a refresh
    error reported a failed request for a change that had taken effect. The
    batch endpoint queues the same refresh; this one now does too, with its own
    refresh_id so the queue's kwargs deduplication cannot swallow a second
    assignment while an older refresh is still running.
    """
    import ast
    import inspect
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / 'bazarr' / 'api' / 'sports' / 'leagues.py').read_text()
    tree = ast.parse(source)
    patch = next(node for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
                 and cls.name == 'SportsLeague'
                 for node in cls.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'patch')
    body = ast.get_source_segment(source, patch)
    assert 'library.refresh_league_profiles(' not in body
    assert "_queue('refresh_league_profiles'" in body
    assert 'refresh_id' in body
    assert 'except Exception' in body

    from sportarr import library

    assert 'refresh_id' in inspect.signature(library.refresh_league_profiles).parameters


# --------------------------------------------------------------------------
# The statistics language filter has to know the languages it plots.
# --------------------------------------------------------------------------

def test_sports_only_languages_are_offered_to_the_statistics_filter(schema_session, monkeypatch):
    """A sports-only language could be filtered for but never selected.

    The statistics chart plots sports downloads, and its language selector is
    built from GET /system/languages?history=true. That branch read only
    TableHistory and TableHistoryMovie, so a language that appears only in
    sports history had no option in the list.
    """
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType
    from app.database import (TableArrInstances, TableHistoryMovie, TableHistorySports,
                              TableSportsEvents, TableSportsLeagues)

    schema_session.add(TableArrInstances(id=1, kind='sportarr', name='S', stable_key='s', port=1867))
    schema_session.add(TableHistoryMovie(action=1, radarrId=1, language='en', video_path='/m.mkv',
                                        description='downloaded'))
    schema_session.add(TableSportsLeagues(id=51, arr_instance_id=1, sportarrLeagueId=7, title='League'))
    schema_session.flush()
    schema_session.add(TableSportsEvents(id=61, arr_instance_id=1, league_id=51, sportarrEventId=9,
                                         file_id=71, path='/sports/race.mkv', title='Race'))
    schema_session.flush()
    schema_session.add(TableHistorySports(action=1, arr_instance_id=1, league_id=51, event_id=61,
                                          language='hu', description='downloaded'))
    schema_session.commit()

    root = Path(__file__).resolve().parents[2] / 'bazarr' / 'api'
    for name in ('_languages_api', '_languages_api.system'):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [('_languages_api.utils', root / 'utils.py'),
                       ('_languages_api.system.languages', root / 'system' / 'languages.py')]:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'database', schema_session)
    # The language table is built at app start, and nothing starts the app
    # here, so the endpoint's alpha2/alpha3 lookups would quietly drop every
    # code and hide what this test is about.
    from languages import get_languages

    monkeypatch.setattr(get_languages, 'languages_dict',
                        [{'code2': 'en', 'code3': 'eng', 'name': 'English'},
                         {'code2': 'hu', 'code3': 'hun', 'name': 'Hungarian'}],
                        raising=False)

    from flask import Flask
    from flask_restx import Api
    from app.config import settings

    app = Flask(__name__)
    Api(app).add_namespace(module.api_ns_system_languages, path='/')
    client = app.test_client()
    answer = client.get('/system/languages?history=true',
                        headers={'X-API-KEY': settings.auth.apikey})
    assert answer.status_code == 200
    codes = {item['code2'] for item in answer.json}
    assert {'en', 'hu'} <= codes


def test_the_extraction_directory_has_one_definition():
    """The guard and the extractor have to agree on the artifact's name.

    Recomputing the cache key in two places is how the guard would drift back
    into refusing the extractor's own output, so both read it from here.
    """
    import inspect

    from sportarr import profile_hooks
    from subtitles.tools.translate import batch

    source = inspect.getsource(batch.extract_embedded_subtitle)
    assert 'extracted_subtitles_dir()' in source
    assert 'extracted_subtitle_key(' in source
    assert 'hashlib' not in source
    guard = inspect.getsource(profile_hooks._is_extraction_artifact)
    assert 'extracted_subtitle_key' in guard
