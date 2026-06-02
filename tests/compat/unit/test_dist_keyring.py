def test_tables_importable():
    from app.database import TableCompatApiKeys, TableCompatUsage
    assert TableCompatApiKeys.__tablename__ == "compat_api_keys"
    assert TableCompatUsage.__tablename__ == "compat_usage"


def test_generate_and_resolve(compat_db):
    from compat import keyring
    kid, token = keyring.create("site-a", tier="pro",
                                excluded_providers=["opensubtitles"],
                                timeout_seconds=12)
    rec = keyring.resolve(token)
    assert rec and rec["id"] == kid and rec["tier"] == "pro"
    assert rec["excluded_providers"] == ["opensubtitles"]
    assert rec["timeout_seconds"] == 12
    assert keyring.resolve("bzr_wrong") is None
    assert keyring.resolve("") is None


def test_resolve_returns_prefix_not_token(compat_db):
    from compat import keyring
    kid, token = keyring.create("site-prefix")
    rec = keyring.resolve(token)
    assert rec["key_prefix"] == token[:8]
    assert rec["key_hash"] != token  # never stored plaintext


def test_disabled_key_resolves_none(compat_db):
    from compat import keyring
    kid, token = keyring.create("site-b")
    keyring.update(kid, enabled=0)
    keyring.invalidate_cache()
    assert keyring.resolve(token) is None


def test_rotate_invalidates_old(compat_db):
    from compat import keyring
    kid, token = keyring.create("site-c")
    new = keyring.rotate(kid)
    keyring.invalidate_cache()
    assert keyring.resolve(token) is None
    assert keyring.resolve(new)["id"] == kid


def test_update_custom_limits_roundtrip(compat_db):
    from compat import keyring
    kid, _ = keyring.create("site-d")
    keyring.update(kid, custom_limits={"search": {"hour": 5}})
    rec = keyring.get(kid)
    assert rec["custom_limits"] == {"search": {"hour": 5}}
    keyring.update(kid, custom_limits=None)
    assert keyring.get(kid)["custom_limits"] is None


def test_delete_removes_key(compat_db):
    from compat import keyring
    kid, token = keyring.create("site-e")
    keyring.delete(kid)
    keyring.invalidate_cache()
    assert keyring.resolve(token) is None
    assert keyring.get(kid) is None


def test_seed_legacy_idempotent(compat_db):
    from app.config import settings
    from compat import keyring
    settings["compat_endpoint"]["token"] = "x" * 40
    keyring.seed_legacy_key()
    keyring.seed_legacy_key()
    legacy = [k for k in keyring.list_keys() if k["is_legacy"]]
    assert len(legacy) == 1 and legacy[0]["tier"] == "unlimited"
    assert keyring.resolve("x" * 40)["is_legacy"] == 1


def test_seed_legacy_repoints_on_rotation(compat_db):
    from app.config import settings
    from compat import keyring
    settings["compat_endpoint"]["token"] = "a" * 40
    keyring.seed_legacy_key()
    settings["compat_endpoint"]["token"] = "b" * 40
    keyring.seed_legacy_key()
    keyring.invalidate_cache()
    assert keyring.resolve("a" * 40) is None
    assert keyring.resolve("b" * 40)["is_legacy"] == 1
    assert len([k for k in keyring.list_keys() if k["is_legacy"]]) == 1
