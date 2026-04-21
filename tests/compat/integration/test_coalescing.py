import threading
import time
from unittest.mock import MagicMock, patch
from babelfish import Language
from bazarr.compat import service, cache as C


def test_concurrent_identical_queries_coalesce(monkeypatch):
    """Two concurrent identical search queries must share ONE fanout via dogpile."""
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_secret", "f" * 32)
    C.invalidate_all()
    call_count = {"n": 0}
    lock = threading.Lock()

    def slow_fanout(*args, **kwargs):
        with lock:
            call_count["n"] += 1
        time.sleep(0.3)
        return {MagicMock(): []}

    with patch("bazarr.compat.service._get_compat_pool") as gp, \
         patch("bazarr.compat.service.list_all_subtitles_parallel", side_effect=slow_fanout):
        gp.return_value.providers = ["p"]
        gp.return_value.discarded_providers = set()
        results = []
        errors = []

        def go():
            try:
                results.append(service.search("tt1", 1, 1, [Language("eng")], "episode"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"unexpected errors: {errors}"
    assert call_count["n"] == 1, f"expected 1 fanout, got {call_count['n']}"
    C.invalidate_all()
