from unittest.mock import MagicMock, patch
import pytest
from babelfish import Language


@pytest.fixture(autouse=True)
def _secrets_and_cache():
    from bazarr.app.config import settings
    from bazarr.compat import cache as C
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    C.invalidate_all()
    yield
    C.invalidate_all()


def _mk(provider_name, sub_id, matches):
    m = MagicMock(
        provider_name=provider_name, id=sub_id,
        language=Language("eng"), release_info="X.1080p",
        download_count=0, hearing_impaired=False, uploader="x",
        matches=matches, upload_date=None,
        ai_translated=False, machine_translated=False,
        foreign_parts_only=False, fps=0.0, filename=None,
    )
    return m


def test_moviehash_only_drops_non_hash_rows():
    from bazarr.compat import service
    hash_sub = _mk("os", "1", {"hash", "series"})
    plain_sub = _mk("os", "2", {"series"})
    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel") as lf:
        gp.return_value.providers = ["os"]
        lf.return_value = {MagicMock(): [hash_sub, plain_sub]}
        res = service.search("tt1", None, None, [Language("eng")],
                             "movie", moviehash="abc", moviehash_match="only")
    ids = [e["attributes"]["subtitle_id"] for e in res["data"]]
    assert "1" in ids
    assert "2" not in ids


def test_moviehash_include_keeps_all_rows():
    from bazarr.compat import service
    hash_sub = _mk("os", "1", {"hash"})
    plain_sub = _mk("os", "2", {"series"})
    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel") as lf:
        gp.return_value.providers = ["os"]
        lf.return_value = {MagicMock(): [hash_sub, plain_sub]}
        res = service.search("tt1", None, None, [Language("eng")],
                             "movie", moviehash="abc", moviehash_match="include")
    assert len(res["data"]) == 2


def test_moviehash_match_only_marks_matched_rows_true():
    from bazarr.compat import service
    hash_sub = _mk("os", "1", {"hash"})
    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel") as lf:
        gp.return_value.providers = ["os"]
        lf.return_value = {MagicMock(): [hash_sub]}
        res = service.search("tt1", None, None, [Language("eng")],
                             "movie", moviehash="abc", moviehash_match="only")
    assert res["data"][0]["attributes"]["moviehash_match"] is True
