def test_local_to_os_entry_basic():
    from compat.response_mapper import local_to_os_entry
    e = local_to_os_entry(
        file_id=42, lang="en", modifier=None, filename="movie.en.srt",
        upload_mtime=1700000000.0, media_type="movie", media_id=99,
        requested_language="en",
        imdb_id="tt1375666", title="Inception", year=2010,
    )
    assert e["id"] == "subtitle-42"
    assert e["type"] == "subtitle"
    a = e["attributes"]
    assert a["language"] == "en"
    assert a["release"] == "movie.en.srt"
    assert a["hearing_impaired"] is False
    assert a["foreign_parts_only"] is False
    assert a["from_trusted"] is True
    assert a["ratings"] == 10.0
    assert a["download_count"] == 999_999
    assert a["upload_date"].endswith("Z")
    files = a["files"]
    assert len(files) == 1
    assert files[0]["file_id"] == 42
    assert files[0]["file_name"] == "movie.en.srt"
    # Schema-parity fields the Jellyfin plugin requires
    assert a["comments"] == "movie.en.srt"
    assert a["votes"] == 0
    assert a["hd"] is False
    assert a["moviehash_match"] is False
    assert a["ai_translated"] is False
    assert a["machine_translated"] is False
    assert a["fps"] == 0.0
    assert a["uploader"] == {"name": "bazarr:local"}
    assert a["url"] == ""
    fd = a["feature_details"]
    assert fd["feature_type"] == "Movie"
    assert fd["imdb_id"] == 1375666
    assert fd["season_number"] == 0
    assert fd["episode_number"] == 0
    assert fd["title"] == "Inception"
    assert fd["movie_name"] == "2010 - Inception"
    assert fd["year"] == 2010


def test_local_to_os_entry_hi_flag():
    from compat.response_mapper import local_to_os_entry
    e = local_to_os_entry(
        file_id=43, lang="en", modifier="hi", filename="movie.en.hi.srt",
        upload_mtime=0, media_type="movie", media_id=99,
        requested_language="en",
    )
    assert e["attributes"]["hearing_impaired"] is True
    assert e["attributes"]["foreign_parts_only"] is False


def test_local_to_os_entry_forced_flag():
    from compat.response_mapper import local_to_os_entry
    e = local_to_os_entry(
        file_id=44, lang="en", modifier="forced",
        filename="movie.en.forced.srt", upload_mtime=0,
        media_type="movie", media_id=99, requested_language="en",
    )
    assert e["attributes"]["foreign_parts_only"] is True


def test_local_to_os_entry_subtitle_id_unique_per_file():
    """Two distinct local files (different file_ids) for the same lang
    + media must produce different subtitle_id values, otherwise clients
    that de-dupe on subtitle_id collapse them. Codex P2."""
    from compat.response_mapper import local_to_os_entry
    e1 = local_to_os_entry(
        file_id=42, lang="en", modifier=None, filename="alt1.srt",
        upload_mtime=0, media_type="movie", media_id=99,
        requested_language="en",
    )
    e2 = local_to_os_entry(
        file_id=43, lang="en", modifier=None, filename="alt2.srt",
        upload_mtime=0, media_type="movie", media_id=99,
        requested_language="en",
    )
    sid1 = e1["attributes"]["subtitle_id"]
    sid2 = e2["attributes"]["subtitle_id"]
    assert sid1 != sid2
    assert sid1.endswith("-42")
    assert sid2.endswith("-43")


def test_local_to_os_entry_preserves_request_region():
    from compat.response_mapper import local_to_os_entry
    e = local_to_os_entry(
        file_id=45, lang="pt-BR", modifier=None, filename="m.pt-BR.srt",
        upload_mtime=0, media_type="movie", media_id=99,
        requested_language="pt-BR",
    )
    assert e["attributes"]["language"] == "pt-BR"
