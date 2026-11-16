# coding=utf-8
"""A signed official catalog that can be served to a refresh, signed with throwaway keys.

Every world generates its own Ed25519 root and CI key at run time and bakes only the root's public
half into the host for the test. No key material is stored in the repository.
"""
import json
from datetime import datetime, timedelta, timezone

from test_provider_hub import _AdvertisementResponse, _git_advertisement
from test_provider_hub_signing import Keys, envelope_for, stamp

OFFICIAL_RAW = "https://raw.githubusercontent.com/LavX/bazarr-provider-catalog/"
HEAD = "d" * 40
OLDER = "a" * 40


class FeedResponse:
    """Enough of requests.Response for the catalog transport."""

    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class SignedWorld:
    """The official repository as the host sees it: branch heads, files by commit, and the keys."""

    def __init__(self, monkeypatch, tmp_path=None):
        from provider_hub import catalog_feed, signing_roots

        self.keys = Keys()
        self.heads = {"main": HEAD, "beta": HEAD}
        self.files = {}
        self.requests = []
        self.community = None
        # Commits GitHub reports as behind the branch heads, which is what an ancestor is.
        self.ancestors = set()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(self.keys.roots))
        monkeypatch.setattr(catalog_feed, "current_time", lambda: self.now)
        monkeypatch.setattr("provider_hub.service.requests.get", self._get)
        self.monkeypatch = monkeypatch

    # --- building files ---

    def catalog_body(self, channel="main", *, version=100, plugins=(), providers=(), source_commit=HEAD,
                     not_before=None, expires=None, **extra):
        document = {
            "schema_version": 2,
            "name": "Official",
            "channel": channel,
            "signed_version": version,
            "not_before": stamp(not_before or self.now - timedelta(days=1)),
            "expires": stamp(expires or self.now + timedelta(days=89)),
            "source_commit": source_commit,
            "providers": [{"manifest": item} for item in providers],
            "plugins": [{"manifest": item} for item in plugins],
        }
        document.update(extra)
        return json.dumps(document, sort_keys=True).encode("utf-8")

    def sign(self, body, signer=None):
        return json.dumps(envelope_for(body, signer or self.keys.ci)).encode("utf-8")

    def key_file(self, **kwargs):
        return json.dumps(self.keys.key_file(**kwargs)).encode("utf-8")

    def publish(self, commit=HEAD, channel="main", *, body=None, envelope=None, key_file=None, **kwargs):
        """Put a signed catalog at ``commit``, validly signed unless a part is given."""
        body = body if body is not None else self.catalog_body(channel, **kwargs)
        self.files[(commit, f"signed/{channel}/catalog.json")] = body
        self.files[(commit, f"signed/{channel}/catalog.json.sig")] = envelope if envelope is not None else self.sign(body)
        self.files[(commit, "keys/catalog-signing-keys.json")] = key_file if key_file is not None else self.key_file()
        return body

    def legacy(self, commit=HEAD, providers=(), plugins=()):
        document = {"schema_version": 1, "name": "Official", "providers": [{"manifest": item} for item in providers]}
        if plugins:
            document["plugins"] = [{"manifest": item} for item in plugins]
        self.files[(commit, "catalog.json")] = json.dumps(document).encode("utf-8")

    # --- the transport ---

    def _get(self, url, timeout=None, **_kwargs):
        self.requests.append(url)
        if url.endswith("/info/refs?service=git-upload-pack"):
            if "LavX/bazarr-provider-catalog" in url:
                return _AdvertisementResponse(_git_advertisement(
                    [(self.heads["main"], "refs/heads/main"), (self.heads["beta"], "refs/heads/beta")]))
        compare = "https://api.github.com/repos/LavX/bazarr-provider-catalog/compare/"
        if url.startswith(compare):
            base, _, commit = url[len(compare):].partition("...")
            if commit == base:
                status = "identical"
            elif commit in self.ancestors:
                status = "behind"
            elif commit in self.heads.values() or commit in {c for c, _p in self.files}:
                status = "diverged"
            else:
                return FeedResponse(b"", 404)
            return FeedResponse(json.dumps({"status": status}).encode("utf-8"))
        if url.startswith(OFFICIAL_RAW):
            commit, _, path = url[len(OFFICIAL_RAW):].partition("/")
            content = self.files.get((commit, path))
            return FeedResponse(b"", 404) if content is None else FeedResponse(content)
        if self.community is not None:
            return self.community(url, timeout)
        return FeedResponse(b"", 404)

    def fetched(self, suffix):
        return [url for url in self.requests if url.endswith(suffix)]
