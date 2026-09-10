import pytest


def map_path(path, rows, **kwargs):
    from media_servers.paths import map_media_path
    return map_media_path(path, rows, **kwargs)


def test_mapping_uses_segment_boundary():
    from media_servers.http import MediaServerError
    rows = [{"local_path": "/movies", "remote_path": "/media"}]
    with pytest.raises(MediaServerError, match="mapping_missing"):
        map_path("/movies-old/A.mkv", rows)
    assert map_path("/movies/A.mkv", rows) == {"path": "/media/A.mkv", "library_id": None}


def test_longest_prefix_and_trailing_separators():
    rows = [
        {"local_path": "/movies/", "remote_path": "/media/"},
        {"local_path": "/movies/4k/", "remote_path": "/uhd/"},
    ]
    assert map_path("/movies/4k/A.mkv", rows)["path"] == "/uhd/A.mkv"


def test_numeric_ids_are_not_coerced():
    rows = [{"local_path": "/tv", "remote_path": "/media/tv", "library_id": "0007"}]
    assert map_path("/tv/A.mkv", rows, require_library=True)["library_id"] == "0007"


def test_windows_drive_paths_keep_remote_separator_style():
    rows = [{"local_path": "C:\\Movies\\", "remote_path": "D:\\Library\\"}]
    assert map_path("c:\\movies\\A\\A.mkv", rows)["path"] == "D:\\Library\\A\\A.mkv"
    rows[0]["remote_path"] = "/media"
    assert map_path("C:\\Movies\\A\\A.mkv", rows)["path"] == "/media/A/A.mkv"


@pytest.mark.parametrize("path", [
    "/movies/D:/A.mkv",
    "/movies/C:/A.mkv",
    "/movies/d:/A.mkv",
    "/movies/D:A.mkv",
    "/movies/C:A.mkv",
    "/movies/Edition/D:/A.mkv",
    "/movies/Edition/C:/A.mkv",
    "/movies/\\root/A.mkv",
    "/movies/folder\\A.mkv",
], ids=["same-drive", "other-drive", "same-drive-lowercase", "same-drive-filename", "other-drive-filename",
        "nested-same-drive", "nested-other-drive", "rooted-component", "split-component"])
def test_posix_components_cannot_acquire_windows_path_semantics(path):
    from media_servers.http import MediaServerError
    rows = [{"local_path": "/movies", "remote_path": "D:\\Library"}]
    with pytest.raises(MediaServerError, match="path_invalid"):
        map_path(path, rows)


def test_posix_to_windows_mapping_preserves_ordinary_relative_components():
    rows = [{"local_path": "/movies", "remote_path": "D:\\Library"}]
    assert map_path("/movies/Edition/A.mkv", rows)["path"] == "D:\\Library\\Edition\\A.mkv"


def test_posix_destination_keeps_literal_drive_like_filename_components():
    rows = [{"local_path": "/movies", "remote_path": "/media"}]
    assert map_path("/movies/D:/A.mkv", rows)["path"] == "/media/D:/A.mkv"


@pytest.mark.parametrize("path", ["/movies/../private/A.mkv", "C:\\Movies\\..\\A.mkv", "relative/A.mkv", "", None])
def test_invalid_video_paths_fail_closed(path):
    from media_servers.http import MediaServerError
    with pytest.raises(MediaServerError, match="path_invalid"):
        map_path(path, [{"local_path": "/movies", "remote_path": "/media"}])


@pytest.mark.parametrize("rows", [None, {}, [None], [["/movies", "/media"]],
                                  [{"local_path": "/movies"}],
                                  [{"local_path": "/movies", "remote_path": "relative"}],
                                  [{"local_path": "/movies", "remote_path": "/media", "library_id": 7}],
                                  [{"local_path": "/movies/../", "remote_path": "/media"}]])
def test_malformed_mappings_fail_closed(rows):
    from media_servers.http import MediaServerError
    with pytest.raises(MediaServerError, match="mapping_invalid"):
        map_path("/movies/A.mkv", rows)


def test_equal_specificity_is_ambiguous_even_for_duplicate_rows():
    from media_servers.http import MediaServerError
    row = {"local_path": "/movies", "remote_path": "/media"}
    with pytest.raises(MediaServerError, match="mapping_ambiguous"):
        map_path("/movies/A.mkv", [row, row])


def test_library_id_is_required_for_silo():
    from media_servers.http import MediaServerError
    with pytest.raises(MediaServerError, match="library_missing"):
        map_path("/movies/A.mkv", [{"local_path": "/movies", "remote_path": "/media"}], require_library=True)
