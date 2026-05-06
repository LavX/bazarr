def test_mint_local_file_id_roundtrips_payload():
    from compat import auth
    fid = auth.mint_local_file_id(
        path="/abs/realpath/sub.srt",
        lang="en", modifier=None, fmt="srt",
        media_type="movie", media_id=99,
        media_dir="/abs/realpath",
    )
    assert isinstance(fid, int) and fid > 0
    ok, payload = auth.parse_file_id(fid)
    assert ok
    assert payload["kind"] == "local"
    assert payload["path"] == "/abs/realpath/sub.srt"
    assert payload["lang"] == "en"
    assert payload["modifier"] is None
    assert payload["fmt"] == "srt"
    assert payload["media_type"] == "movie"
    assert payload["media_id"] == 99
    assert payload["media_dir"] == "/abs/realpath"


def test_mint_local_file_id_distinct_from_provider():
    from compat import auth
    fid_local = auth.mint_local_file_id(
        path="/x/sub.srt", lang="en", modifier=None, fmt="srt",
        media_type="movie", media_id=1, media_dir="/x",
    )
    fid_provider = auth.mint_file_id(
        provider="opensubtitlescom", native_id="123",
        language="eng", release_info="rel", subtitle=None,
    )
    ok_l, p_l = auth.parse_file_id(fid_local)
    ok_p, p_p = auth.parse_file_id(fid_provider)
    assert p_l.get("kind") == "local"
    assert p_p.get("kind") in (None, "provider")
