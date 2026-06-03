from datetime import timedelta


# ---- tiers ----

def test_presets_have_four_windows():
    from compat import tiers
    p = tiers.preset_tiers()
    for t in ("free", "basic", "pro", "unlimited"):
        for kind in ("search", "download"):
            assert set(p[t][kind]) == {"hour", "day", "week", "month"}
    assert all(v == 0 for v in p["unlimited"]["search"].values())


def test_get_tier_falls_back_to_default():
    from compat import tiers
    assert tiers.get_tier("nonexistent")["label"]
    assert tiers.get_tier(None)["label"]


def test_config_override_merges_over_preset(monkeypatch):
    from app.config import settings
    from compat import tiers
    settings["compat_endpoint"]["tiers"] = {"free": {"search": {"hour": 7}}}
    try:
        free = tiers.all_tiers()["free"]
        assert free["search"]["hour"] == 7        # overridden
        assert free["search"]["day"] == 1000      # preset preserved
    finally:
        settings["compat_endpoint"]["tiers"] = {}


# ---- meter ----

def test_record_and_hour_sum(compat_db):
    from compat import meter
    meter.record(1, "search")
    meter.record(1, "search")
    assert meter.window_sum(1, "search", "hour") == 2
    assert meter.window_sum(1, "download", "hour") == 0


def test_blocked_does_not_count_against_usage(compat_db):
    from compat import meter
    meter.record(3, "search", blocked=True)
    assert meter.window_sum(3, "search", "hour") == 0


def test_window_rollup(compat_db):
    from compat import meter
    from datetime import datetime
    from app.database import database, insert, TableCompatUsage
    base = meter._truncate_hour(datetime.now())
    for d in range(0, 40):
        database.execute(insert(TableCompatUsage).values(
            key_id=2, kind="download", hour_start=base - timedelta(days=d),
            count=1, blocked=0))
    meter._invalidate(2)
    assert meter.window_sum(2, "download", "day") >= 1
    assert meter.window_sum(2, "download", "week") >= 7
    assert 29 <= meter.window_sum(2, "download", "month") <= 31


# ---- limits ----

def test_custom_overrides_tier():
    from compat import limits
    rec = {"id": 1, "tier": "free",
           "custom_limits": {"search": {"hour": 5}}}
    eff = limits.effective_limits(rec, "search")
    assert eff["hour"] == 5            # custom
    assert eff["day"] == 1000          # tier free search


def test_unlimited_tier_always_allows(compat_db):
    from compat import limits
    rec = {"id": 9, "tier": "unlimited", "custom_limits": None}
    d = limits.check(rec, "download")
    assert d.allowed is True and d.limit == 0


def test_block_when_hour_exceeded(compat_db):
    from compat import meter, limits
    rec = {"id": 7, "tier": "free", "custom_limits": {"download": {"hour": 2}}}
    for _ in range(2):
        meter.record(7, "download")
    d = limits.check(rec, "download")
    assert d.allowed is False and d.window == "hour" and d.remaining == 0


def test_tightest_window_binds(compat_db):
    from compat import meter, limits
    rec = {"id": 8, "tier": "free",
           "custom_limits": {"search": {"hour": 100, "day": 10}}}
    for _ in range(5):
        meter.record(8, "search")
    d = limits.check(rec, "search")
    assert d.allowed is True and d.window == "day" and d.remaining == 5
