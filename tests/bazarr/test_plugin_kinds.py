# coding=utf-8
"""Kinds on the shared Provider Hub core.

Every installation row carries a kind that never changes, every iteration
filters by it, and the side effects that belong to subtitle providers run
only through the subtitle provider kind.
"""
import pytest


def test_kind_registry_refuses_conflicts():
    from provider_hub.kinds import (
        SUBTITLE_PROVIDER, Kind, UnknownKindError, get_kind, register_kind, unregister_kind,
    )

    class Squatter(Kind):
        name = "squatter"
        kind_apis = frozenset({"bazarr.subtitle_provider.v1"})

    with pytest.raises(ValueError, match="already belongs"):
        register_kind(Squatter())
    with pytest.raises(UnknownKindError):
        get_kind("no_such_kind")
    with pytest.raises(ValueError):
        unregister_kind(SUBTITLE_PROVIDER)


def test_the_subtitle_provider_kind_attaches_on_restart():
    from provider_hub.kinds import ATTACH_ON_RESTART, SUBTITLE_PROVIDER, get_kind, kind_for_api, kind_of

    kind = get_kind(SUBTITLE_PROVIDER)
    assert kind.attach_policy == ATTACH_ON_RESTART
    assert kind_for_api("bazarr.subtitle_provider.v1") is kind
    assert kind_of({"provider_id": "legacyhub"}) == SUBTITLE_PROVIDER
