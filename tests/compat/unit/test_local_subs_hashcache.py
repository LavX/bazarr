import os
import tempfile
from unittest.mock import patch


def test_hashcache_caches_on_second_call():
    from compat.local_subs import _HashCache
    cache = _HashCache(max_entries=10)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"\x00" * (1 << 20))
        path = f.name
    try:
        with patch("compat.local_subs._opensubtitles_hash") as mock_hash:
            mock_hash.return_value = "deadbeefcafebabe"
            h1 = cache.get(path)
            h2 = cache.get(path)
        assert h1 == "deadbeefcafebabe"
        assert h2 == "deadbeefcafebabe"
        assert mock_hash.call_count == 1
    finally:
        os.unlink(path)


def test_hashcache_invalidates_on_mtime_change():
    from compat.local_subs import _HashCache
    cache = _HashCache(max_entries=10)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"\x00" * (1 << 20))
        path = f.name
    try:
        with patch("compat.local_subs._opensubtitles_hash") as mock_hash:
            mock_hash.side_effect = ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"]
            h1 = cache.get(path)
            os.utime(path, (1000, 1000))
            h2 = cache.get(path)
        assert h1 == "aaaaaaaaaaaaaaaa"
        assert h2 == "bbbbbbbbbbbbbbbb"
        assert mock_hash.call_count == 2
    finally:
        os.unlink(path)


def test_hashcache_returns_none_for_missing_file():
    from compat.local_subs import _HashCache
    cache = _HashCache(max_entries=10)
    assert cache.get("/nonexistent/path/xyz") is None


def test_hashcache_evicts_lru_at_cap():
    from compat.local_subs import _HashCache
    cache = _HashCache(max_entries=2)
    paths = []
    try:
        for i in range(3):
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(bytes([i]) * 1024)
                paths.append(f.name)
        with patch("compat.local_subs._opensubtitles_hash") as mock_hash:
            mock_hash.side_effect = ["a" * 16, "b" * 16, "c" * 16, "d" * 16]
            cache.get(paths[0])
            cache.get(paths[1])
            cache.get(paths[2])  # evicts paths[0]
            cache.get(paths[0])  # recomputes
        assert mock_hash.call_count == 4
    finally:
        for p in paths:
            os.unlink(p)
