# -*- coding: utf-8 -*-
"""check_releases gates the GitHub releases API on tag movement.

The tags come from the git smart-HTTP ref advertisement, which is answered
outside the 60-per-hour REST quota. The releases API is only called for a repo
whose advertised tags changed since its last successful fetch.
"""
import json
import os

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


def test_a_recreated_annotated_tag_on_the_same_commit_is_a_movement(github):
    github.tags[_TRANSLATOR_AD] = [("b" * 40, "refs/tags/v0.1.0"), (_V1, "refs/tags/v0.1.0^{}")]
    _check()
    github.tags[_TRANSLATOR_AD] = [("c" * 40, "refs/tags/v0.1.0"), (_V1, "refs/tags/v0.1.0^{}")]
    github.calls.clear()

    _check()

    assert github.api_calls() == [_TRANSLATOR_API]


def test_an_unchanged_annotated_tag_is_not_a_movement(github):
    github.tags[_TRANSLATOR_AD] = [("b" * 40, "refs/tags/v0.1.0"), (_V1, "refs/tags/v0.1.0^{}")]
    _check()
    github.calls.clear()

    _check()

    assert github.api_calls() == []

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


# A failed releases fetch must never record the tags it was gated on, or the
# next check would see no movement and keep the failed result until the next
# tag. The stored state is also dropped, so a tag that moves back to the
# recorded value still fetches.

def test_a_failed_fetch_does_not_record_the_moved_tags(github):
    _check()
    github.tags[_BAZARR_AD].append((_V2, "refs/tags/v1.1.0"))
    github.releases[_BAZARR_API].insert(0, _release("v1.1.0", "2026-10-10T00:00:00Z"))
    github.api_status = 403
    _check()
    github.api_status = 200
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.1.0", "v1.0.0", "v0.1.0"]


def test_a_failed_fetch_forgets_the_old_tags_so_a_tag_moving_back_still_fetches(github):
    _check()
    github.tags[_BAZARR_AD].append((_V2, "refs/tags/v1.1.0"))
    github.api_status = 403
    _check()
    github.api_status = 200
    github.tags[_BAZARR_AD] = [(_V1, "refs/tags/v1.0.0")]
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]


def test_an_unparseable_releases_answer_is_a_failed_fetch(github, monkeypatch):
    _check()
    github.tags[_BAZARR_AD].append((_V2, "refs/tags/v1.1.0"))
    original = github.get

    def not_json(url, **kwargs):
        response = original(url, **kwargs)
        if url == _BAZARR_API:
            response._payload = None
        return response

    monkeypatch.setattr(requests, "get", not_json)
    _check()
    monkeypatch.setattr(requests, "get", original)
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]


def test_a_fetch_made_while_the_advertisement_was_down_records_no_tags(github):
    github.advertisement_down = True
    _check()
    github.advertisement_down = False
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]



@pytest.mark.parametrize("payload", [{}, {"message": "API rate limit exceeded"}, "releases", None])
def test_a_releases_answer_that_is_not_a_list_is_a_failed_fetch(github, monkeypatch, payload):
    _check()
    github.tags[_BAZARR_AD].append((_V2, "refs/tags/v1.1.0"))
    github.releases[_BAZARR_API].insert(0, _release("v1.1.0", "2026-10-10T00:00:00Z"))
    original = github.get

    def wrong_shape(url, **kwargs):
        response = original(url, **kwargs)
        if url == _BAZARR_API:
            response.json = lambda: payload
        return response

    monkeypatch.setattr(requests, "get", wrong_shape)
    _check()
    monkeypatch.setattr(requests, "get", original)
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.1.0", "v1.0.0", "v0.1.0"]

# The gate reuses releases.txt. When that file no longer holds what the
# recorded fetch wrote, unchanged tags are not enough to skip the API.

def test_a_missing_releases_file_fetches_even_with_unchanged_tags(github):
    from app.check_update import args

    _check()
    os.remove(f"{args.config_dir}/config/releases.txt")
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]


def test_a_releases_file_that_lost_a_repos_entries_fetches_that_repo(github):
    from app.check_update import args

    _check()
    kept = [r for r in _releases_txt() if r["repo"] != "AI Subtitle Translator"]
    with open(f"{args.config_dir}/config/releases.txt", "w", encoding="UTF-8") as f:
        json.dump(kept, f)
    github.calls.clear()

    _check()

    assert github.api_calls() == [_TRANSLATOR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]


def test_a_repo_with_no_releases_is_still_gated(github):
    github.releases[_TRANSLATOR_API] = []
    _check()
    github.calls.clear()

    _check()

    assert github.api_calls() == []


def test_a_corrupt_tag_state_file_is_a_first_run(github):
    from app.check_update import args

    _check()
    with open(f"{args.config_dir}/config/release_tags.json", "w", encoding="UTF-8") as f:
        f.write("{not json")
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API, _TRANSLATOR_API]


def test_switching_the_releases_repo_away_and_back_fetches_again(github, monkeypatch):
    from app import check_update

    _check()
    other_ad = "https://github.com/someone/bazarr/info/refs?service=git-upload-pack"
    other_api = "https://api.github.com/repos/someone/bazarr/releases?per_page=100"
    github.tags[other_ad] = [(_V3, "refs/tags/v9.0.0")]
    github.releases[other_api] = [_release("v9.0.0", "2026-10-05T00:00:00Z")]
    monkeypatch.setattr(check_update, "RELEASES_REPO", "someone/bazarr")
    _check()
    monkeypatch.setattr(check_update, "RELEASES_REPO", "LavX/bazarr")
    github.calls.clear()

    _check()

    assert github.api_calls() == [_BAZARR_API]
    assert [r["name"] for r in _releases_txt()] == ["v1.0.0", "v0.1.0"]
