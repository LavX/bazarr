from unittest.mock import MagicMock, patch
from babelfish import Language
from bazarr.compat import service, cache as C


def test_partial_success_returns_available_results(monkeypatch):
    """When some providers return subs and others fail, return the successful ones."""
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_secret", "f" * 32)
    C.invalidate_all()
    good_sub = MagicMock(
        provider_name="good", id="99",
        language=Language("eng"), release_info="x",
        download_count=100, hearing_impaired=False, matches=set(),
    )
    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel") as lf:
        # Simulate 3 providers succeeded (good_sub * 3 flattened), 2 errored silently (dropped)
        lf.return_value = {MagicMock(): [good_sub, good_sub, good_sub]}
        gp.return_value.providers = ["good", "good2", "good3", "bad1", "bad2"]
        gp.return_value.discarded_providers = set()
        result = service.search("tt1", None, None, [Language("eng")], "movie")

    assert len(result["data"]) == 3
    C.invalidate_all()
