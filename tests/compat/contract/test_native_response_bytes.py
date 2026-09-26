"""Frozen episode/movie response bytes, captured before sports Hub integration."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from babelfish import Language


def native_envelope(media_type):
    from compat import response_mapper as mapper

    sub = SimpleNamespace(
        id="12345",
        provider_name="opensubtitlescom",
        language=Language("eng"),
        release_info="Native.2020.1080p.BluRay-GROUP",
        download_count=123,
        hearing_impaired=False,
        uploader="Uploader",
        ratings=7.5,
        matches={"hash"},
        ai_translated=False,
        machine_translated=False,
        foreign_parts_only=False,
        fps=24.0,
        upload_date="2020-09-08T10:00:00Z",
        page_link="https://example.org/subtitle/12345",
    )
    video = SimpleNamespace(
        title="Native", series="Series", year=2020, season=1, episode=2
    )
    kwargs = dict(season=1, episode=2) if media_type == "episode" else {}
    provider = mapper.subtitle_to_os_entry(
        sub, 42, media_type, "tt0000909", video=video, **kwargs
    )
    local = mapper.local_to_os_entry(
        file_id=43,
        lang="en",
        modifier="hi",
        filename="Native.en.hi.srt",
        upload_mtime=1600000000,
        media_type=media_type,
        media_id=909,
        requested_language="en",
        imdb_id="tt0000909",
        title="Native",
        year=2020,
        episode_title="Episode title",
        **kwargs,
    )
    return json.dumps(
        mapper.search_envelope([local, provider]), sort_keys=True, separators=(",", ":")
    ).encode()


@pytest.mark.parametrize("media_type", ["movie", "episode"])
def test_native_response_bytes(media_type):
    fixture = (
        Path(__file__).parents[1] / "fixtures" / f"native_{media_type}_response.json"
    )
    assert native_envelope(media_type) == fixture.read_bytes()
