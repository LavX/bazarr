"""Public source searches recheck confirmed server absence before fetching and saving."""
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import sqlalchemy as sa
from flask import Flask

import test_media_sources_pipeline as pipeline

world = pipeline.world
isolated_jobs = pipeline.isolated_jobs


@pytest.mark.parametrize("missing_at", ["before-queue", "while-queued", "during-fetch", "available"])
def test_public_item_search_excludes_confirmed_missing_items(world, monkeypatch, isolated_jobs, missing_at):
    monkeypatch.setattr(requests.Session, "send", Mock(side_effect=AssertionError("Unexpected network request")))
    from api import api_bp
    from app.config import settings
    from app.database import TableMediaServerInstances
    from media_sources import analysis, repository
    from subtitles import download

    monkeypatch.setattr(analysis.video_analyzer, "parse_video_metadata", lambda *args, **kwargs: {
        "ffprobe": {"audio": [{"language": "eng"}]}, "mediainfo": {}})
    context = pipeline._movie(world)
    world.db.execute(sa.insert(TableMediaServerInstances).values(
        id="synthetic-jellyfin", kind="jellyfin", name="Server", enabled=1, url="http://server.invalid",
        verify_ssl=1, path_mappings="[]", options="{}", revision=1))
    world.db.execute(sa.update(repository.Sources).where(repository.Sources.c.id == world.source_id).values(
        kind="jellyfin", media_server_instance_id="synthetic-jellyfin"))
    repository.update_item(context.item_id, world.db, external_id="server-item-1")
    destination = Path(context.path).with_suffix(".en.srt")

    def mark_missing():
        repository.update_item(context.item_id, world.db, server_missing=1)

    def fetch(videos, **kwargs):
        if missing_at == "during-fetch":
            mark_missing()
        return {next(iter(videos)): [pipeline.FakeSubtitle()]}

    provider = Mock(side_effect=fetch)
    monkeypatch.setattr(download, "download_best_subtitles", provider)
    monkeypatch.setitem(settings.auth, "apikey", "synthetic-source-search-key")
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    if missing_at == "before-queue":
        mark_missing()

    response = app.test_client().post(f"/api/sources/items/{context.item_id}/search", headers={
        "X-API-KEY": "synthetic-source-search-key"})

    assert response.status_code == 202
    (job,) = isolated_jobs.jobs_pending_queue
    assert (job.module, job.func) == ("media_sources.service", "search_source_item")
    if missing_at == "while-queued":
        mark_missing()
    assert isolated_jobs._run_job(isolated_jobs._reserve_next_job())
    assert job.status == "completed"
    assert provider.call_count == (1 if missing_at in {"during-fetch", "available"} else 0)
    history = world.db.execute(sa.select(repository.History)).all()
    if missing_at == "available":
        assert destination.read_bytes() == pipeline.CONTENT
        assert len(history) == len(world.published) == 1
    else:
        assert repository.get_item(context.item_id).server_missing == 1
        assert not destination.exists()
        assert history == []
        assert world.published == []
