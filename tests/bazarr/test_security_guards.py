# coding=utf-8
"""Unit tests for the pure security-guard helpers used to harden the API/UI
surface (SSRF + path traversal). These functions encode the security decisions;
the route handlers just call them. Kept dependency-free so they run without
booting Flask or the DB and never touch the network (numeric IPs only).
"""
import pytest

from utilities.security_guards import (
    api_key_matches,
    is_subtitle_path_extension,
    normalized_origin,
    subtitle_path_within_area,
)


# --- api_key_matches (Finding 2: gate the /test proxy) -----------------------

def test_api_key_matches_exact():
    assert api_key_matches("abc123", "abc123") is True


def test_api_key_matches_wrong():
    assert api_key_matches("abc123", "different") is False


@pytest.mark.parametrize("provided", ["", None])
def test_api_key_matches_empty_provided(provided):
    assert api_key_matches(provided, "abc123") is False


def test_api_key_matches_empty_expected():
    # An unset server key must never authorize a request.
    assert api_key_matches("anything", "") is False
    assert api_key_matches("", "") is False


def test_api_key_matches_prefix_is_not_enough():
    assert api_key_matches("abc", "abc123") is False


# --- normalized_origin (the origin a Plex credential may be sent to) ---------

def test_normalized_origin_reads_scheme_host_port():
    assert normalized_origin("https://plex.example:32400/path?query") == \
        ("https", "plex.example", 32400)


def test_normalized_origin_fills_the_default_port():
    assert normalized_origin("http://plex.example") == ("http", "plex.example", 80)
    assert normalized_origin("plex.example") == ("https", "plex.example", 443)


def test_normalized_origin_strips_the_ipv6_brackets_both_parsers_keep():
    """A bracketed IPv6 literal is the one host two URL parsers spell
    differently, so the brackets come off before the hosts are compared: an
    address that only matches with them on is still the same server."""
    assert normalized_origin("http://[2001:db8::1]:32400") == \
        ("http", "2001:db8::1", 32400)
    assert normalized_origin("http://[fe80::1]:32400") == ("http", "fe80::1", 32400)


@pytest.mark.parametrize("uri", [
    "https://attacker.example\\@plex.example",
    "https://user:pass@plex.example",
    "https://plex.example:0x150",
    "ftp://plex.example",
    "",
])
def test_normalized_origin_refuses_what_two_parsers_read_differently(uri):
    assert normalized_origin(uri) is None


@pytest.mark.parametrize("uri", [
    "http://[::1",
    "https://[fe80::1",
    "http://]::1[",
])
def test_normalized_origin_answers_none_for_a_url_the_parser_itself_rejects(uri):
    """A malformed bracketed host raises out of the URL parser before any
    comparison runs, so the guard answers None and the route that asked
    refuses the address rather than failing on an unhandled error."""
    assert normalized_origin(uri) is None


# --- subtitle_path_within_area (Finding 3: contain PATCH /api/subtitles) ------

VIDEO = "/media/movies/Film (2020)/Film.mkv"


def test_subtitle_alongside_video_allowed():
    assert subtitle_path_within_area(
        "/media/movies/Film (2020)/Film.en.srt", VIDEO) is True


def test_subtitle_in_subfolder_under_video_allowed():
    assert subtitle_path_within_area(
        "/media/movies/Film (2020)/subs/Film.en.srt", VIDEO) is True


def test_subtitle_outside_video_dir_rejected():
    assert subtitle_path_within_area("/config/config/config.yaml", VIDEO) is False


def test_subtitle_traversal_escape_rejected():
    assert subtitle_path_within_area(
        "/media/movies/Film (2020)/../../../etc/passwd", VIDEO) is False


def test_subtitle_absolute_custom_subfolder_allowed():
    assert subtitle_path_within_area(
        "/srt-store/Film.en.srt", VIDEO,
        subfolder_mode="absolute", custom_subfolder="/srt-store") is True


def test_subtitle_custom_subfolder_ignored_when_mode_not_absolute():
    assert subtitle_path_within_area(
        "/srt-store/Film.en.srt", VIDEO,
        subfolder_mode="relative", custom_subfolder="/srt-store") is False


@pytest.mark.parametrize("sub,video", [("", VIDEO), (VIDEO, ""), ("", "")])
def test_subtitle_within_area_empty_inputs(sub, video):
    assert subtitle_path_within_area(sub, video) is False


# --- is_subtitle_path_extension (Finding 4: reject non-subtitle reads) --------

@pytest.mark.parametrize("path", [
    "/x/a.srt", "/x/a.ass", "/x/a.ssa", "/x/a.vtt", "/x/a.sub", "/x/a.SRT",
])
def test_subtitle_extensions_allowed(path):
    assert is_subtitle_path_extension(path) is True


@pytest.mark.parametrize("path", [
    "/config/config.yaml", "/etc/passwd", "/x/id_rsa", "/x/bazarr.db",
    "/x/a.srt.bak", "/x/noext", "",
])
def test_non_subtitle_extensions_rejected(path):
    assert is_subtitle_path_extension(path) is False
