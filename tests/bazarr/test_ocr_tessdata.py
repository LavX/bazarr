# coding=utf-8
"""Tesseract language packs: naming, download, checksum and offline behaviour."""

import hashlib
import os

import pytest
import requests

from subtitles.tools.ocr import tessdata
from subtitles.tools.ocr.errors import LanguagePackError

PACK = b"fake traineddata payload " * 64


class _Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


@pytest.fixture
def fake_table(monkeypatch):
    table = {"hun": (len(PACK), hashlib.sha256(PACK).hexdigest()),
             "eng": (len(PACK), hashlib.sha256(PACK).hexdigest()),
             "chi_sim": (1, "x"), "chi_tra": (1, "x"), "por": (1, "x"), "deu": (1, "x")}
    monkeypatch.setattr(tessdata, "TESSDATA_CHECKSUMS", table)
    return table


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("no download expected")

    monkeypatch.setattr(tessdata.requests, "get", refuse)
    return calls


@pytest.mark.parametrize("code,pack", [
    ("hun", "hun"), ("HUN ", "hun"), ("ger", "deu"), ("deu", "deu"), ("zho", "chi_sim"), ("chi", "chi_sim"),
    ("zht", "chi_tra"), ("pob", "por"),
])
def test_language_codes_map_to_tessdata_pack_names(fake_table, code, pack):
    assert tessdata.pack_name(code) == pack


@pytest.mark.parametrize("code", ["nob", "nno", "nor"])
def test_bokmal_and_nynorsk_tracks_read_with_the_norwegian_pack(code):
    # Bazarr offers both written standards; tessdata_fast has one Norwegian pack.
    assert tessdata.pack_name(code) == "nor"


@pytest.mark.parametrize("code", ["xxx", "", None, "und", "tlh"])
def test_unknown_language_fails_before_any_download(fake_table, no_network, tmp_path, code):
    with pytest.raises(LanguagePackError, match="No OCR language pack"):
        tessdata.ensure_pack(code, str(tmp_path))
    assert no_network == []
    assert list(tmp_path.iterdir()) == []


def test_custom_language_codes_match_bazarr_custom_languages():
    from languages.custom_lang import CustomLanguage

    for code, official in tessdata._OFFICIAL_ALPHA3.items():
        assert CustomLanguage.from_value(code, "alpha3").official_alpha3 == official


def test_download_is_verified_and_lands_atomically(fake_table, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(tessdata.requests, "get", lambda url, **kw: seen.append((url, kw)) or _Response(PACK))
    progress = []

    assert tessdata.ensure_pack("hun", str(tmp_path), progress=lambda *a: progress.append(a)) == "hun"

    assert (tmp_path / "hun.traineddata").read_bytes() == PACK
    assert [p.name for p in tmp_path.iterdir()] == ["hun.traineddata"]
    assert seen[0][0] == ("https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/"
                          f"{tessdata.TESSDATA_TAG}/hun.traineddata")
    assert seen[0][1]["timeout"] == tessdata.DOWNLOAD_TIMEOUT
    assert progress[-1] == ("pack", len(PACK), len(PACK))


@pytest.mark.parametrize("body", [PACK[:-1] + b"!", PACK + b"extra", PACK[:10]])
def test_checksum_mismatch_is_refused_and_the_partial_removed(fake_table, monkeypatch, tmp_path, body):
    monkeypatch.setattr(tessdata.requests, "get", lambda url, **kw: _Response(body))

    with pytest.raises(LanguagePackError, match="does not match its checksum") as caught:
        tessdata.ensure_pack("hun", str(tmp_path))

    assert str(tmp_path) in str(caught.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [requests.ConnectionError("unreachable"), requests.Timeout("slow")])
def test_offline_failure_names_the_url_and_the_folder(fake_table, monkeypatch, tmp_path, failure):
    def offline(url, **kw):
        raise failure

    monkeypatch.setattr(tessdata.requests, "get", offline)
    folder = tmp_path / "packs"

    with pytest.raises(LanguagePackError) as caught:
        tessdata.ensure_pack("hun", str(folder))

    message = str(caught.value)
    assert tessdata.pack_url("hun") in message
    assert str(folder) in message
    assert not any(folder.iterdir())


def test_http_error_is_reported_like_offline(fake_table, monkeypatch, tmp_path):
    monkeypatch.setattr(tessdata.requests, "get", lambda url, **kw: _Response(b"", status=404))

    with pytest.raises(LanguagePackError, match="Download that file and place it in"):
        tessdata.ensure_pack("hun", str(tmp_path))


def test_hand_placed_pack_with_the_right_checksum_is_used_without_a_download(fake_table, no_network, tmp_path):
    (tmp_path / "hun.traineddata").write_bytes(PACK)

    assert tessdata.ensure_pack("hun", str(tmp_path)) == "hun"
    assert no_network == []


def test_hand_placed_pack_with_the_wrong_checksum_is_replaced(fake_table, monkeypatch, tmp_path):
    (tmp_path / "hun.traineddata").write_bytes(b"tessdata_best, not fast")
    monkeypatch.setattr(tessdata.requests, "get", lambda url, **kw: _Response(PACK))

    tessdata.ensure_pack("hun", str(tmp_path))

    assert (tmp_path / "hun.traineddata").read_bytes() == PACK


def test_tessdata_dir_defaults_to_the_config_folder(monkeypatch, tmp_path):
    from app.config import settings
    from app.get_args import args

    monkeypatch.setattr(settings.ocr, "tessdata_dir", "")
    monkeypatch.setattr(args, "config_dir", str(tmp_path))
    assert tessdata.tessdata_dir() == os.path.join(str(tmp_path), "tessdata")

    monkeypatch.setattr(settings.ocr, "tessdata_dir", "/srv/packs")
    assert tessdata.tessdata_dir() == "/srv/packs"


def test_committed_table_covers_the_pinned_tag():
    from subtitles.tools.ocr import tessdata_checksums as table

    assert table.TESSDATA_REPOSITORY == "tesseract-ocr/tessdata_fast"
    assert table.TESSDATA_TAG == "4.1.0"
    assert len(table.TESSDATA_CHECKSUMS) == 125
    assert table.TESSDATA_CHECKSUMS["hun"] == (
        5296273, "35067e7cfe102dcdc953f9a758fdfaa6296b17a1ee6d874ee780fa306430b9fb")
    assert table.TESSDATA_CHECKSUMS["eng"] == (
        4113088, "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2")
    for size, sha256 in table.TESSDATA_CHECKSUMS.values():
        assert size > 0 and len(sha256) == 64


def test_generator_renders_the_table_module(tmp_path):
    import importlib.util

    root = os.path.join(os.path.dirname(__file__), "..", "..")
    spec = importlib.util.spec_from_file_location(
        "generate_tessdata_checksums", os.path.join(root, "scripts", "ocr", "generate_tessdata_checksums.py"))
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    source = generator.render("tesseract-ocr/tessdata_fast", "4.1.0", [("hun", 3, "a" * 64), ("eng", 2, "b" * 64)])

    namespace = {}
    exec(compile(source, "tessdata_checksums.py", "exec"), namespace)
    assert namespace["TESSDATA_CHECKSUMS"] == {"eng": (2, "b" * 64), "hun": (3, "a" * 64)}
    assert list(namespace["TESSDATA_CHECKSUMS"]) == ["eng", "hun"]
