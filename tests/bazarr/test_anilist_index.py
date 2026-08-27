# coding=utf-8
"""The AniList refiner must cache an index, not the whole anime list.

`get_series_mappings` cached the parsed Fribb anime-list, roughly 43,000 dicts,
in the file-backed dogpile region. That backend unpickles its value on every
read, so each jimaku-refined search inflated a 2.6 MB pickle into about 30 MB of
short-lived Python objects, linear-scanned it with a `str()` conversion per
entry, took the first match and threw the rest away.

Only two fields of each entry are ever consulted, so the cache should hold the
lookup rather than the corpus. A dict keyed by the two id types is a fraction of
the size, survives the same round trip, and turns the scan into a hash lookup.
"""
import pytest


ANIME_LIST = [
    {"anidb_id": 1, "imdb_id": "tt0001", "anilist_id": 101, "title": "First",
     "mal_id": 9001, "themoviedb_id": 5001},
    {"anidb_id": 2, "anilist_id": 102, "title": "No imdb", "mal_id": 9002},
    {"imdb_id": "tt0003", "anilist_id": 103, "title": "No anidb"},
    {"anidb_id": 4, "imdb_id": "tt0004", "title": "No anilist id at all"},
    {"anidb_id": 1, "anilist_id": 999, "title": "Duplicate anidb, must not win"},
]


def test_the_index_resolves_both_id_types():
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["1"] == 101
    assert index["anidb_id"]["2"] == 102
    assert index["imdb_id"]["tt0001"] == 101
    assert index["imdb_id"]["tt0003"] == 103


def test_an_entry_without_an_anilist_id_still_claims_its_key():
    """It claims the key with a None value. The old scan matched that entry and
    returned nothing, so dropping it would let a later duplicate answer in its
    place, which is a different result for the same input."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["4"] is None
    assert index["imdb_id"]["tt0004"] is None


def test_a_later_duplicate_cannot_displace_an_id_less_first_entry():
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index([
        {"anidb_id": 42, "title": "First, no anilist id"},
        {"anidb_id": 42, "anilist_id": 999, "title": "Later duplicate"},
    ])

    assert index["anidb_id"]["42"] is None, (
        'the later duplicate answered for a key the first entry already claimed'
    )


def test_a_non_mapping_element_does_not_break_the_build():
    """The build runs inside the cached call, so raising here would mean nothing
    is ever cached and the 3 MB list is refetched on every search."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(["junk", None, 7, {"anidb_id": 1, "anilist_id": 101}])

    assert index["anidb_id"]["1"] == 101


def test_the_first_entry_wins_as_it_did_before():
    """The old code took obj[0] of the matches, so a later duplicate never won."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["1"] == 101, 'a later duplicate overwrote the first match'


def test_the_index_keeps_only_what_is_looked_up():
    """The point of the change. Anything else retained puts the corpus back."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert set(index) == {"anidb_id", "imdb_id"}
    flattened = repr(index)
    for noise in ("title", "mal_id", "themoviedb_id", "9001", "5001"):
        assert noise not in flattened, (
            f'{noise!r} survived into the cached index; only the two id maps '
            'should be cached')


@pytest.mark.parametrize("candidate,value,expected", [
    ("series_anidb_id", 1, 101),
    ("series_anidb_id", "2", 102),
    ("imdb_id", "tt0003", 103),
    ("series_anidb_id", 4, None),      # present, but carries no AniList id
    ("imdb_id", "tt9999", None),       # not in the list at all
])
def test_lookup_matches_the_old_behaviour(monkeypatch, candidate, value, expected):
    from subtitles.refiners import anilist

    client = anilist.AniListClient.__new__(anilist.AniListClient)
    monkeypatch.setattr(anilist.AniListClient, 'get_series_index',
                        lambda self: anilist.build_series_index(ANIME_LIST))

    assert client.get_series_id(candidate, value) == expected
