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
    is_trusted_plex_target,
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


# --- is_trusted_plex_target (Finding 1: don't leak the token) ----------------

@pytest.mark.parametrize("uri", [
    "http://127.0.0.1:32400",
    "https://192.168.1.50:32400",
    "http://10.5.5.5",
    "http://172.16.0.9:32400",
    "https://[::1]:32400",
])
def test_trusted_plex_local_targets(uri):
    # The common case: home Plex on loopback / LAN must stay allowed.
    assert is_trusted_plex_target(uri) is True


@pytest.mark.parametrize("uri", [
    "http://8.8.8.8",
    "https://1.2.3.4:32400",
])
def test_untrusted_public_ip_rejected(uri):
    # A public, non-Plex destination would exfiltrate the token -> reject.
    assert is_trusted_plex_target(uri) is False


@pytest.mark.parametrize("uri", [
    "https://plex.tv",
    "https://app.plex.tv",
    "https://abc123.plex.direct:32400",
])
def test_plex_owned_domains_trusted(uri):
    assert is_trusted_plex_target(uri) is True


def test_configured_host_trusted_even_if_public():
    # The user's own configured Plex server (possibly a public hostname) is fine.
    assert is_trusted_plex_target(
        "https://myplex.example.com:32400",
        trusted_hosts=("myplex.example.com",),
    ) is True


def test_unknown_public_host_not_in_trustlist_rejected():
    assert is_trusted_plex_target(
        "https://evil.example.com",
        trusted_hosts=("myplex.example.com",),
    ) is False


@pytest.mark.parametrize("uri", ["", "ftp://127.0.0.1", "file:///etc/passwd", "127.0.0.1:32400"])
def test_trusted_plex_bad_scheme_or_empty(uri):
    # bare host (no scheme) defaults to https and is allowed if local; the
    # explicitly bad schemes and empty string must be rejected.
    if uri == "127.0.0.1:32400":
        assert is_trusted_plex_target(uri) is True
    else:
        assert is_trusted_plex_target(uri) is False


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
