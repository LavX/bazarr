# coding=utf-8
"""Every reader of a stored subtitles list accepts the four element embedded entry.

The indexers store an embedded track as ``[language, None, None, codec]``.
Rows indexed before that hold ``[language, None, None]`` until they are
indexed again, so both shapes live side by side in one library, and each
reader below has to give the same answer for either one.
"""
import pytest

LEGACY = "[['en', None, None], ['fr', '/m/Movie.fr.srt', 12]]"
CURRENT = "[['en', None, None, 'pgs'], ['fr', '/m/Movie.fr.srt', 12]]"

LANGUAGES = [
    {"code2": "en", "code3": "eng", "code3b": "eng", "name": "English"},
    {"code2": "fr", "code3": "fra", "code3b": "fre", "name": "French"},
]


@pytest.fixture
def languages(monkeypatch):
    from languages import get_languages

    monkeypatch.setattr(get_languages, "languages_dict", LANGUAGES, raising=False)


def test_sports_missing_accepts_both_shapes(languages, monkeypatch):
    import ast

    from app.config import settings
    from subtitles.indexer.sports import _missing

    monkeypatch.setattr(settings.general, "use_embedded_subs", True)
    profile = {"cutoff": None, "items": [
        {"id": 1, "language": "en", "forced": "False", "hi": "False"},
        {"id": 2, "language": "de", "forced": "False", "hi": "False"},
    ]}

    for raw in (LEGACY, CURRENT):
        assert _missing(1, ast.literal_eval(raw), "[]", "[]", profile=profile) == ["de"]


def test_postprocess_emits_codec_for_both_shapes(languages, monkeypatch):
    from api import utils

    monkeypatch.setattr(utils.settings.general, "embedded_subs_show_desired", False)
    monkeypatch.setattr(utils.path_mappings, "path_replace_movie", lambda path: path)

    legacy = utils.postprocess({"radarrId": 1, "subtitles": LEGACY})["subtitles"]
    current = utils.postprocess({"radarrId": 1, "subtitles": CURRENT})["subtitles"]

    assert [(s["code2"], s["path"], s["codec"]) for s in legacy] == [
        ("en", None, None), ("fr", "/m/Movie.fr.srt", None)]
    assert [(s["code2"], s["path"], s["codec"]) for s in current] == [
        ("en", None, "pgs"), ("fr", "/m/Movie.fr.srt", None)]
    assert [s["file_size"] for s in current] == [None, 12]


def test_marshalled_subtitle_carries_codec(languages, monkeypatch):
    from flask_restx import fields, marshal

    from api import utils
    from api.swaggerui import subtitles_model

    monkeypatch.setattr(utils.settings.general, "embedded_subs_show_desired", False)
    monkeypatch.setattr(utils.path_mappings, "path_replace_movie", lambda path: path)
    item = utils.postprocess({"radarrId": 1, "subtitles": CURRENT})

    out = marshal(item, {"subtitles": fields.Nested(subtitles_model)})

    assert [s["codec"] for s in out["subtitles"]] == ["pgs", None]


@pytest.mark.parametrize("raw", [LEGACY, CURRENT])
def test_path_only_readers_ignore_the_trailing_codec(raw):
    from api.subtitles.download import iter_external_subtitles
    from compat.local_subs import _parse_subtitles_blob
    from sportarr.library import parse_stored_list
    from subtitles.mass_operations import _parse_subtitles_column
    from subtitles.wanted.utils import _find_existing_subtitle_path, _provider_file_on_disk

    assert _parse_subtitles_column(raw) == [("fr", "/m/Movie.fr.srt")]
    assert _parse_subtitles_column(raw, include_embedded=True) == [("en", None), ("fr", "/m/Movie.fr.srt")]
    assert iter_external_subtitles(raw) == [("fr", "/m/Movie.fr.srt")]
    assert _find_existing_subtitle_path(raw, "en") is None
    assert _provider_file_on_disk(raw, "en", None) is False
    assert [item[:2] for item in _parse_subtitles_blob(raw)] == [["en", None], ["fr", "/m/Movie.fr.srt"]]
    assert [item[:2] for item in parse_stored_list(raw)] == [["en", None], ["fr", "/m/Movie.fr.srt"]]
