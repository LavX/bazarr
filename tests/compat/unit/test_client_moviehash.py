from types import SimpleNamespace


def test_client_opensubtitles_moviehash_populates_os_hash_consumers():
    from compat import service

    video = SimpleNamespace(hashes={})

    service._apply_client_moviehash(video, "8e245d9679d31e12")

    assert video.hashes["bsplayer"] == "8e245d9679d31e12"
    assert video.hashes["opensubtitles"] == "8e245d9679d31e12"
    assert video.hashes["opensubtitlescom"] == "8e245d9679d31e12"
    assert video.hashes["napisy24"] == "8e245d9679d31e12"
    assert "shooter" not in video.hashes


def test_client_shooter_moviehash_populates_only_shooter_hash():
    from compat import service

    video = SimpleNamespace(hashes={})
    shooter_hash = (
        "fc884e136ada6a0d21cb22df64095850;"
        "bf11c971a439215af399213fa49fee02;"
        "3bbc3ecb4646f0e86bc9197eae2aed06;"
        "d96fe613346c17d345ecc723318e1b1d"
    )

    service._apply_client_moviehash(video, shooter_hash)

    assert video.hashes == {"shooter": shooter_hash}
