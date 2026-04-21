from unittest.mock import patch
from babelfish import Language
from bazarr.compat import service, cache as C


def test_all_providers_return_empty_returns_empty_data(monkeypatch):
    """When every provider errors/returns empty, search still succeeds with empty data.
    The HTTP layer (routes.py) is responsible for translating empty providers to 503
    when needed; service.search itself returns the empty-data shape."""
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_secret", "f" * 32)
    C.invalidate_all()
    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel") as lf:
        lf.return_value = {}  # no results at all
        gp.return_value.providers = ["p1", "p2"]
        gp.return_value.discarded_providers = set()
        result = service.search("tt1", None, None, [Language("eng")], "movie")

    assert result["data"] == []
    assert result["total_pages"] == 1
    C.invalidate_all()
