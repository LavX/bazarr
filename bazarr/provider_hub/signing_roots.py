# coding=utf-8
"""The root public keys this release trusts to vouch for the catalog's signing keys.

A root key never signs a catalog. It signs the key file that lists the keys that do,
and it revokes them. A root only ever appears here as a public key: the private halves
are made and kept offline by the project owner, and nothing in this repository can
produce a signature a host would accept.

Maps a key id (the first 16 hex digits of SHA-256 over the raw public key, see
``signing.key_id``) to the raw 32-byte Ed25519 public key. The map is empty until the
owner publishes a root's public record. A build with no root reads the legacy catalog only; a
build with one verifies the signed catalog when the repository serves it, and reads the legacy
catalog until it does. Replacing a root needs a host release that trusts the new
one before anything is signed with a key it certifies, and the old root stays listed
while a supported release still verifies with it.
"""
from __future__ import annotations

import base64

SIGNING_ROOTS: dict[str, bytes] = {
    # Made 2026-11-17.
    "d2edde7047b7b2fe": base64.b64decode("39GrDDEActD1h3UWLIQljZzTXHoBZIYggT7ZRBPYpCU="),
}
