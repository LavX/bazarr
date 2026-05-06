import os
import tempfile


def test_oshash_for_1mb_zeros_is_stable():
    """OS hash for a 1MB file of zeros: file_size + sum(uint64 zeros) = 0x100000."""
    from compat.local_subs import _opensubtitles_hash
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"\x00" * (1 << 20))
        path = f.name
    try:
        assert _opensubtitles_hash(path) == "0000000000100000"
    finally:
        os.unlink(path)


def test_oshash_small_file_returns_hex16():
    from compat.local_subs import _opensubtitles_hash
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"\x01" * 100)
        path = f.name
    try:
        h = _opensubtitles_hash(path)
        assert isinstance(h, str)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
    finally:
        os.unlink(path)
