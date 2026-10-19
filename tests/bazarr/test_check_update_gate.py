# -*- coding: utf-8 -*-
"""check_releases gates the GitHub releases API on tag movement.

The tags come from the git smart-HTTP ref advertisement, which is answered
outside the 60-per-hour REST quota. The releases API is only called for a repo
whose advertised tags changed since its last successful fetch.
"""
import json

import pytest
import requests

_BAZARR_AD = "https://github.com/LavX/bazarr/info/refs?service=git-upload-pack"
_TRANSLATOR_AD = "https://github.com/LavX/ai-subtitle-translator/info/refs?service=git-upload-pack"
_BAZARR_API = "https://api.github.com/repos/LavX/bazarr/releases?per_page=100"
_TRANSLATOR_API = "https://api.github.com/repos/LavX/ai-subtitle-translator/releases?per_page=100"

_V1 = "1" * 40
_V2 = "2" * 40
_V3 = "3" * 40
_MAIN = "a" * 40


def _pkt_line(payload):
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def _advertisement(refs):
    body = [_pkt_line(b"# service=git-upload-pack\n"), b"0000"]
    for index, (sha, name) in enumerate(refs):
        capabilities = b"\0symref=HEAD:refs/heads/main agent=git/2.43.0" if index == 0 else b""
        body.append(_pkt_line(f"{sha} {name}".encode("utf-8") + capabilities + b"\n"))
    body.append(b"0000")
    return b"".join(body)


def _release(name, date, prerelease=False):
    return {"name": name, "body": f"notes for {name}", "published_at": date, "prerelease": prerelease,
            "assets": [{"browser_download_url": f"https://example.invalid/{name}.zip"}]}


class _Response:
    def __init__(self, status_code=200, content=b"", headers=None, payload=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size=None):
        yield self.content

    def close(self):
        pass

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _GitHub:
    """Answers the advertisement and releases URLs and records every call."""

    def __init__(self):
        self.tags = {
            _BAZARR_AD: [(_V1, "refs/tags/v1.0.0")],
            _TRANSLATOR_AD: [(_V1, "refs/tags/v0.1.0")],
        }
        self.releases = {
            _BAZARR_API: [_release("v1.0.0", "2026-10-01T00:00:00Z")],
            _TRANSLATOR_API: [_release("v0.1.0", "2026-09-01T00:00:00Z")],
        }
        self.advertisement_down = False
        self.api_status = 200
        self.calls = []

    def api_calls(self):
        return [url for url in self.calls if url.startswith("https://api.github.com/")]

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url in self.tags:
            if self.advertisement_down:
                raise requests.exceptions.ConnectionError("github.com unreachable")
            refs = [(_MAIN, "refs/heads/main"), *self.tags[url]]
            return _Response(content=_advertisement(refs),
                             headers={"Content-Type": "application/x-git-upload-pack-advertisement"})
        if url in self.releases:
            if self.api_status != 200:
                return _Response(status_code=self.api_status)
            return _Response(payload=self.releases[url])
        raise AssertionError(f"unexpected request to {url}")


@pytest.fixture
def github(monkeypatch, tmp_path):
    from app import check_update

    (tmp_path / "config").mkdir()
    monkeypatch.setattr(check_update.args, "config_dir", str(tmp_path))
    monkeypatch.setattr(check_update, "RELEASES_REPO", "LavX/bazarr")
    monkeypatch.setattr(check_update, "MICROSERVICE_REPOS", [("LavX/ai-subtitle-translator", "AI Subtitle Translator")])
    fake = _GitHub()
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


def _check():
    from app.check_update import check_releases

    check_releases(startup=True)


def _releases_txt():
    from app.check_update import args

    with open(f"{args.config_dir}/config/releases.txt", encoding="UTF-8") as f:
        return json.load(f)


def _rendered():
    """What the releases page serves, read through its own endpoint code."""
    from flask import Flask
    from api.system.releases import SystemReleases

    app = Flask(__name__)
    with app.test_request_context():
        return SystemReleases.get.__wrapped__(SystemReleases())


def test_first_run_with_no_stored_state_fetches_every_repo(github):
    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]


def test_unchanged_tags_do_not_call_the_releases_api(github):
    _check()
    first = _releases_txt()
    github.calls.clear()

    _check()

    assert github.api_calls() == []
    assert _releases_txt() == first


def test_a_new_tag_triggers_exactly_one_fetch_and_the_page_populates_identically(github):
    _check()
    github.tags[_BAZARR_AD].append((_V2, "refs/tags/v1.1.0"))
    github.releases[_BAZARR_API].insert(0, _release("v1.1.0", "2026-10-10T00:00:00Z", prerelease=True))
    github.calls.clear()

    _check()
    _check()

    assert github.api_calls() == [_BAZARR_API]
    expected = sorted(
        [{"name": r["name"], "body": r["body"], "date": r["published_at"], "prerelease": r["prerelease"],
          "download_link": r["assets"][0]["browser_download_url"], "repo": label}
         for url, label in ((_BAZARR_API, "Bazarr+"), (_TRANSLATOR_API, "AI Subtitle Translator"))
         for r in github.releases[url]],
        key=lambda r: r["date"], reverse=True)
    assert _releases_txt() == expected


def test_a_tag_that_moves_backwards_is_a_movement(github):
    github.tags[_BAZARR_AD] = [(_V1, "refs/tags/v1.0.0"), (_V2, "refs/tags/v1.1.0")]
    _check()
    github.tags[_BAZARR_AD] = [(_V1, "refs/tags/v1.0.0")]
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]


def test_a_retargeted_tag_is_a_movement(github):
    _check()
    github.tags[_TRANSLATOR_AD] = [(_V3, "refs/tags/v0.1.0")]
    github.calls.clear()

    _check()

    assert github.api_calls() == [_TRANSLATOR_API]


def test_a_branch_push_alone_is_not_a_movement(github, monkeypatch):
    _check()
    github.calls.clear()
    original = github.get

    def moved_main(url, **kwargs):
        response = original(url, **kwargs)
        if url == _BAZARR_AD:
            response.content = _advertisement([(_V3, "refs/heads/main"), *github.tags[url]])
        return response

    monkeypatch.setattr(requests, "get", moved_main)
    _check()

    assert github.api_calls() == []


def test_an_advertisement_failure_falls_back_to_fetching_the_releases(github):
    _check()
    github.advertisement_down = True
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]


def test_a_malformed_advertisement_falls_back_to_fetching_the_releases(github, monkeypatch):
    _check()
    github.calls.clear()
    original = github.get

    def html_instead(url, **kwargs):
        response = original(url, **kwargs)
        if url in github.tags:
            response.headers = {"Content-Type": "text/html"}
        return response

    monkeypatch.setattr(requests, "get", html_instead)
    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]


def test_quota_exhausted_with_unchanged_tags_renders_the_last_fetch_without_an_api_call(github, monkeypatch):
    monkeypatch.setenv("BAZARR_VERSION", "1.0.0")
    _check()
    before = _rendered()
    github.api_status = 403
    github.calls.clear()

    _check()

    assert github.api_calls() == []
    assert _rendered() == before
    assert [r["name"] for r in before["data"]][:1] == ["v1.0.0"]

