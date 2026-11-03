# coding=utf-8
"""Shared helpers for the plugin platform tests.

Imported by the test_plugin_*.py files. The fixture these helpers go with,
service_kind, lives in conftest.py.
"""
import hashlib
import json

SERVICE_KIND = "test_service"
SERVICE_KIND_API = "bazarr.test_service.v1"


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def bundle_digest(file_payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative_path, content in sorted(file_payloads.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def provider_manifest_v1(provider_id="examplehub", version="1.0.0", **overrides):
    payloads = overrides.pop("file_payloads", {"provider.py": b"class ExampleProvider: pass\n"})
    manifest = {
        "schema_version": 1,
        "provider_id": provider_id,
        "name": provider_id.title(),
        "version": version,
        "api_version": "bazarr.provider-hub.v1",
        "entry_module": "provider",
        "entry_class": "ExampleProvider",
        "config_schema": {"type": "object", "properties": {"api_key": {"type": "string"}}},
        "secret_fields": ["api_key"],
        "supported_media": ["movie", "episode"],
        "languages": ["eng"],
        "files": {path: sha256_hex(content) for path, content in payloads.items()},
        "bundle_sha256": bundle_digest(payloads),
        "source": {"type": "github", "repo": "owner/repo", "ref": "main", "commit": "b" * 40},
        "dependencies": {"requirements": []},
    }
    manifest.update(overrides)
    return manifest


def provider_manifest_v2(plugin_id="examplehub", version="1.0.0", **overrides):
    payloads = overrides.pop("file_payloads", None)
    manifest = provider_manifest_v1(plugin_id, version, **({"file_payloads": payloads} if payloads else {}))
    manifest.pop("provider_id")
    manifest.pop("api_version")
    manifest.update({
        "schema_version": 2,
        "id": plugin_id,
        "kind": "subtitle_provider",
        "kind_api": "bazarr.subtitle_provider.v1",
    })
    manifest.update(overrides)
    return manifest


def service_manifest(plugin_id="exampleservice", version="1.0.0", **overrides):
    payloads = overrides.pop("file_payloads", {"service.py": b"class Service: pass\n"})
    manifest = {
        "schema_version": 2,
        "id": plugin_id,
        "kind": SERVICE_KIND,
        "kind_api": SERVICE_KIND_API,
        "name": plugin_id.title(),
        "version": version,
        "files": {path: sha256_hex(content) for path, content in payloads.items()},
        "bundle_sha256": bundle_digest(payloads),
        "source": {"type": "github", "repo": "LavX/bazarr-provider-catalog", "ref": "main", "commit": "b" * 40},
        "dependencies": {"requirements": []},
    }
    manifest.update(overrides)
    return manifest


def write_state(path, **collections):
    state = {"catalog_sources": {}, "catalog_entries": {}, "installations": {}, "jobs": []}
    state.update(collections)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


FAR_FUTURE = "2999-01-01T00:00:00Z"
OFFICIAL_PAGES = "https://lavx.github.io/bazarr-provider-catalog"
OFFICIAL_RAW = "https://raw.githubusercontent.com/LavX/bazarr-provider-catalog"


def verified_official_source(**overrides):
    """The official source record as a verified, unexpired refresh leaves it."""
    source = {
        "id": "official", "name": "Official Bazarr Provider Catalog", "type": "github",
        "url": "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
        "enabled": True, "official": True, "trusted": True,
        "signature_verified": True, "catalog_expires_at": FAR_FUTURE, "revocations_expires_at": FAR_FUTURE,
    }
    source.update(overrides)
    return source


class FakeHTTPResponse:
    """Enough of requests.Response for the catalog transport."""

    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class SigningKeys:
    """A throwaway root key and the CI signing key it certifies, as the host sees them.

    The host trusts a catalog signature only through a key that the root
    certified, so the tests build the same chain: ``document`` is the keys file
    the host ships (the certificate of the CI key), and ``root_public`` is what
    the host embeds. The certificate covers the 30 days before now and the next
    330, so documents stamped with the helper's times are inside it.
    """

    def __init__(self, *, not_before=None, not_after=None):
        import base64
        from datetime import datetime, timedelta, timezone

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from provider_hub.catalog_signature import canonical_bytes, key_id_for

        def raw_public(key):
            return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

        now = datetime.now(timezone.utc)
        self.root = Ed25519PrivateKey.generate()
        self.ci = Ed25519PrivateKey.generate()
        self.root_id = key_id_for(raw_public(self.root))
        self.ci_id = key_id_for(raw_public(self.ci))
        self.root_public = base64.b64encode(raw_public(self.root)).decode("ascii")
        self.ci_public = base64.b64encode(raw_public(self.ci)).decode("ascii")
        self.statement = {
            "schema_version": 1, "type": "catalog-signing-certificate",
            "subject_key_id": self.ci_id, "subject_public_key": self.ci_public,
            "issuer_key_id": self.root_id,
            "not_before": iso(not_before or now - timedelta(days=30)),
            "not_after": iso(not_after or now + timedelta(days=330)),
            "serial": 1,
        }
        signature = self.root.sign(canonical_bytes(self.statement))
        self.document = {"schema_version": 1, "certificates": [{
            "statement": self.statement, "signature": base64.b64encode(signature).decode("ascii")}]}

    def sign(self, data, key_id=None, key=None):
        """The detached signature envelope for ``data``, by the CI key unless told otherwise."""
        import base64

        signature = (key or self.ci).sign(data)
        return json.dumps({"schema_version": 1, "signatures": [{
            "algorithm": "ed25519", "key_id": key_id or self.ci_id,
            "signature": base64.b64encode(signature).decode("ascii")}]}).encode("utf-8")


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(channel="main", commit="c" * 40, sequence=1, signed_at=None, expires=None):
    """The stamp the publish tooling puts on every signed document."""
    from datetime import datetime, timedelta, timezone

    signed = signed_at or datetime.now(timezone.utc) - timedelta(hours=1)
    return {"channel": channel, "commit": commit, "sequence": sequence, "signed_at": iso(signed),
            "expires": expires or iso(signed + timedelta(days=90))}


def signed_file(keys, payload):
    """The bytes of a signed JSON file and of its detached signature."""
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    return data, keys.sign(data)


def official_pages(keys, catalog, *, channel="main", commit="c" * 40, revoked=(), sequence=1,
                   signed_at=None, expires=None):
    """URL to response map for one official channel on GitHub Pages.

    The channel's unsigned ``signed-current`` pointer names the commit, and the
    signed files sit under that commit. Both documents carry the same stamp, the
    way the publish tooling writes them.
    """
    document_stamp = stamp(channel, commit, sequence, signed_at, expires)
    catalog_bytes, catalog_sig = signed_file(keys, dict(catalog, **document_stamp))
    revocations_bytes, revocations_sig = signed_file(
        keys, dict(document_stamp, schema_version=1, revoked=list(revoked)))
    base = f"{OFFICIAL_PAGES}/v1/{channel}"
    signed = f"{base}/{commit}/signed"
    return {
        f"{base}/signed-current": FakeHTTPResponse(
            json.dumps(dict(document_stamp)).encode(), headers={"ETag": f'"pointer-{sequence}"'}),
        f"{signed}/catalog.json": FakeHTTPResponse(catalog_bytes, headers={"ETag": f'"catalog-{sequence}"'}),
        f"{signed}/catalog.json.sig": FakeHTTPResponse(catalog_sig),
        f"{signed}/revocations.json": FakeHTTPResponse(
            revocations_bytes, headers={"ETag": f'"revocations-{sequence}"'}),
        f"{signed}/revocations.json.sig": FakeHTTPResponse(revocations_sig),
    }


def official_raw(pages, *, commit="d" * 40):
    """The same files as GitHub serves them from the publication branch at one commit
    (the fallback origin; the commit is the branch's, not the catalog's)."""
    return {url.replace(OFFICIAL_PAGES, f"{OFFICIAL_RAW}/{commit}", 1): response
            for url, response in pages.items()}
