import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_hub.signing import (
    CLOCK_SKEW,
    CounterVerdict,
    Status,
    WireFormatError,
    compare_counter,
    key_file_statement,
    key_id,
    parse_wire_format,
    verify_catalog,
)

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
SIGNED_AT = NOW - timedelta(days=1)
EXPIRES = NOW + timedelta(days=89)


def stamp(moment):
    # strftime("%Y") does not zero-pad years below 1000 on glibc.
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}Z"


def raw_public(private):
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


class Keys:
    """A throwaway root and CI key pair, generated fresh for every test."""

    def __init__(self):
        self.root = Ed25519PrivateKey.generate()
        self.root_public = raw_public(self.root)
        self.root_id = key_id(self.root_public)
        self.ci = Ed25519PrivateKey.generate()
        self.ci_public = raw_public(self.ci)
        self.ci_id = key_id(self.ci_public)
        self.roots = {self.root_id: self.root_public}

    def entry(self, private=None):
        public = raw_public(private or self.ci)
        return {
            "key_id": key_id(public),
            "algorithm": "ed25519",
            "public_key": b64(public),
            "added": "2026-10-01",
        }

    def revocation(self, revoked_at, kid=None):
        return {"key_id": kid or self.ci_id, "revoked_at": stamp(revoked_at)}

    def sign_key_file(self, document, signer=None):
        """Sign the document's current keys and revocations as the root."""
        signer = signer or self.root
        root_id = key_id(raw_public(signer))
        document["root_key_id"] = root_id
        statement = key_file_statement(
            root_id, document["key_file_version"], document["keys"], document.get("revocations", [])
        )
        document["signature"] = b64(signer.sign(statement))
        return document

    def key_file(self, revocations=(), extra=(), version=1, signer=None):
        document = {
            "key_file_version": version,
            "keys": [self.entry(), *extra],
            "revocations": list(revocations),
        }
        return self.sign_key_file(document, signer)


def catalog_bytes(channel="main", version=10, not_before=SIGNED_AT, expires=EXPIRES, **extra):
    document = {
        "channel": channel,
        "signed_version": version,
        "not_before": stamp(not_before),
        "expires": stamp(expires),
        "plugins": [{"id": "demo"}],
    }
    document.update(extra)
    return json.dumps(document, sort_keys=True).encode("utf-8")


def envelope_for(data, *signers):
    return {
        "schema_version": 1,
        "signatures": [
            {
                "algorithm": "ed25519",
                "key_id": key_id(raw_public(private)),
                "signature": b64(private.sign(data)),
            }
            for private in signers
        ],
    }


@pytest.fixture
def keys():
    return Keys()


def verify(keys, data=None, envelope=None, key_file=None, **kwargs):
    data = catalog_bytes() if data is None else data
    options = {"roots": keys.roots, "channel": "main", "now": NOW}
    options.update(kwargs)
    return verify_catalog(
        data,
        envelope if envelope is not None else envelope_for(data, keys.ci),
        key_file if key_file is not None else keys.key_file(),
        **options,
    )


def test_valid_catalog_is_ok(keys):
    result = verify(keys)

    assert result.status is Status.OK
    assert result.ok and result.usable and result.may_advance_state
    assert result.signer_key_id == keys.ci_id
    assert result.root_key_id == keys.root_id
    assert result.channel == "main"
    assert result.signed_version == 10
    assert result.not_before == SIGNED_AT
    assert result.expires == EXPIRES
    assert result.host_time == NOW
    assert result.counter is CounterVerdict.FIRST
    assert result.baked is False


def test_accepts_json_text_and_bytes_for_documents(keys):
    data = catalog_bytes()
    envelope = json.dumps(envelope_for(data, keys.ci))
    key_file = json.dumps(keys.key_file()).encode("utf-8")

    assert verify(keys, data, envelope, key_file).ok


def test_a_valid_catalog_decoded_to_text_is_malformed_not_an_error(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)

    result = verify(keys, data.decode("utf-8"), envelope)

    assert result.status is Status.MALFORMED
    assert not result.usable


@pytest.mark.parametrize("bad", [5, [b"{}"], {"channel": "main"}])
def test_a_catalog_that_is_not_bytes_is_malformed(keys, bad):
    envelope = envelope_for(catalog_bytes(), keys.ci)

    assert verify(keys, bad, envelope).status is Status.MALFORMED


@pytest.mark.parametrize("wrap", [bytearray, memoryview])
def test_a_bytes_like_catalog_is_verified_as_its_bytes(keys, wrap):
    data = catalog_bytes()

    assert verify(keys, wrap(data), envelope_for(data, keys.ci)).ok


def test_key_id_is_sha256_prefix_of_the_public_key(keys):
    assert key_id(keys.ci_public) == hashlib.sha256(keys.ci_public).hexdigest()[:16]
    assert len(key_id(keys.ci_public)) == 16


@pytest.mark.parametrize("envelope", [None, "", b"", "   "])
def test_missing_envelope_is_unsigned(keys, envelope):
    result = verify_catalog(
        catalog_bytes(), envelope, keys.key_file(), roots=keys.roots, channel="main", now=NOW
    )

    assert result.status is Status.UNSIGNED
    assert not result.usable


def test_flipped_catalog_byte_is_a_bad_signature(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    tampered = bytearray(data)
    tampered[len(tampered) // 2] ^= 0x01

    result = verify(keys, bytes(tampered), envelope)

    # A flipped byte may also break the JSON; either way it must not verify.
    assert result.status in (Status.BAD_SIGNATURE, Status.MALFORMED)
    assert not result.usable


def test_flipped_byte_inside_a_json_value_is_a_bad_signature(keys):
    data = catalog_bytes(note="alpha")
    envelope = envelope_for(data, keys.ci)
    tampered = data.replace(b"alpha", b"alphb")

    result = verify(keys, tampered, envelope)

    assert result.status is Status.BAD_SIGNATURE
    assert result.signer_key_id is None
    assert result.channel is None


def test_flipped_signature_byte_is_a_bad_signature(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    raw = bytearray(base64.b64decode(envelope["signatures"][0]["signature"]))
    raw[0] ^= 0x80
    envelope["signatures"][0]["signature"] = b64(bytes(raw))

    assert verify(keys, data, envelope).status is Status.BAD_SIGNATURE


def test_signature_from_a_key_missing_from_the_key_file(keys):
    data = catalog_bytes()
    stranger = Ed25519PrivateKey.generate()

    result = verify(keys, data, envelope_for(data, stranger))

    assert result.status is Status.BAD_SIGNATURE
    assert "not in the key file" in result.detail


def test_key_file_signed_by_an_untrusted_root_is_rejected(keys):
    data = catalog_bytes()
    other_root = Ed25519PrivateKey.generate()

    result = verify(keys, data, key_file=keys.key_file(signer=other_root))

    assert result.status is Status.BAD_SIGNATURE
    assert "key file is not signed by a trusted root" in result.detail


def test_key_file_signed_by_the_ci_key_is_rejected(keys):
    assert verify(keys, key_file=keys.key_file(signer=keys.ci)).status is Status.BAD_SIGNATURE


def test_key_file_must_be_signed_at_all(keys):
    key_file = keys.key_file()
    del key_file["signature"]

    assert verify(keys, key_file=key_file).status is Status.MALFORMED


def test_key_added_to_a_signed_key_file_is_rejected(keys):
    data = catalog_bytes()
    attacker = Ed25519PrivateKey.generate()
    key_file = keys.key_file()
    key_file["keys"].append(keys.entry(private=attacker))

    result = verify(keys, data, envelope_for(data, attacker), key_file)

    assert result.status is Status.BAD_SIGNATURE


def test_key_file_bound_to_a_different_root_id_is_rejected(keys):
    other = Keys()
    key_file = keys.key_file()
    key_file["root_key_id"] = other.root_id

    assert verify(keys, key_file=key_file, roots={**keys.roots, **other.roots}).status is Status.BAD_SIGNATURE


def test_key_file_entry_order_and_whitespace_do_not_matter(keys):
    spare = Ed25519PrivateKey.generate()
    key_file = keys.key_file(extra=[keys.entry(private=spare)])
    key_file["keys"].reverse()
    text = json.dumps(key_file, indent=4)

    assert verify(keys, key_file=text).ok


def test_root_key_signs_only_when_listed(keys):
    data = catalog_bytes()
    listed = keys.key_file(extra=[keys.entry(private=keys.root)])

    bare = verify(keys, data, envelope_for(data, keys.root), keys.key_file())
    assert bare.status is Status.BAD_SIGNATURE
    # Listing a root as a signing key is refused, so a root never signs a catalog.
    refused = verify(keys, data, envelope_for(data, keys.root), listed)
    assert refused.status is Status.MALFORMED
    assert "root key" in refused.detail
    assert verify(keys, data, envelope_for(data, keys.ci), listed).status is Status.MALFORMED


def test_either_trusted_root_may_sign_the_key_file(keys):
    data = catalog_bytes()
    second = Keys()
    roots = {**keys.roots, **second.roots}

    result = verify(keys, data, key_file=keys.key_file(signer=second.root), roots=roots)

    assert result.ok
    assert result.root_key_id == second.root_id


# Revocation


def test_revoked_key_is_rejected(keys):
    revocation = keys.revocation(SIGNED_AT - timedelta(hours=1))

    result = verify(keys, key_file=keys.key_file([revocation]))

    assert result.status is Status.REVOKED_KEY
    assert result.signer_key_id == keys.ci_id
    assert result.key_file_version == 1
    assert not result.usable
    assert result.signed_version is None


@pytest.mark.parametrize("offset", [timedelta(seconds=1), timedelta(days=30), timedelta(days=-400), timedelta(0)])
def test_revoked_key_is_rejected_whatever_date_the_catalog_claims(keys, offset):
    # The key holder writes not_before, so a backdated catalog must not slip
    # under the revocation date.
    revocation = keys.revocation(SIGNED_AT + offset)

    result = verify(keys, stored_counter=3, key_file=keys.key_file([revocation]))

    assert result.status is Status.REVOKED_KEY
    assert not result.usable and not result.may_advance_state
    assert result.counter is None


def test_backdated_catalog_from_a_revoked_key_cannot_poison_the_counter(keys):
    revoked_at = NOW - timedelta(hours=1)
    data = catalog_bytes(version=10**18, not_before=revoked_at - timedelta(seconds=1))
    key_file = keys.key_file([keys.revocation(revoked_at)])

    result = verify(keys, data, key_file=key_file)

    assert result.status is Status.REVOKED_KEY
    assert not result.may_advance_state


def test_revocation_added_without_a_root_signature_is_rejected(keys):
    key_file = keys.key_file()
    key_file["revocations"].append(keys.revocation(SIGNED_AT))

    assert verify(keys, key_file=key_file).status is Status.BAD_SIGNATURE


def test_revocation_signed_by_the_ci_key_is_rejected(keys):
    forged = keys.key_file([keys.revocation(SIGNED_AT)], signer=keys.ci)

    assert verify(keys, key_file=forged).status is Status.BAD_SIGNATURE


def test_revocation_with_a_tampered_date_is_rejected(keys):
    key_file = keys.key_file([keys.revocation(SIGNED_AT + timedelta(days=5))])
    key_file["revocations"][0]["revoked_at"] = stamp(SIGNED_AT - timedelta(days=5))

    assert verify(keys, key_file=key_file).status is Status.BAD_SIGNATURE


def test_stripped_revocations_are_rejected(keys):
    revoked = keys.key_file([keys.revocation(SIGNED_AT)])
    stripped = copy.deepcopy(revoked)
    stripped["revocations"] = []

    assert verify(keys, key_file=revoked).status is Status.REVOKED_KEY
    assert verify(keys, key_file=stripped).status is Status.BAD_SIGNATURE
    del stripped["revocations"]
    assert verify(keys, key_file=stripped).status is Status.BAD_SIGNATURE


def test_older_key_file_without_the_revocation_is_a_rollback(keys):
    old = keys.key_file(version=1)
    current = keys.key_file([keys.revocation(SIGNED_AT)], version=2)

    # Without remembered state the old file looks fine, which is why the
    # caller has to pass the highest version it accepted.
    assert verify(keys, key_file=old).ok
    assert verify(keys, key_file=current, stored_key_file_version=1).status is Status.REVOKED_KEY

    result = verify(keys, key_file=old, stored_key_file_version=2)
    assert result.status is Status.KEY_FILE_ROLLBACK
    assert not result.usable and not result.may_advance_state
    assert result.key_file_version == 1
    assert result.root_key_id == keys.root_id
    assert "version 1" in result.detail and "2" in result.detail


def test_emptied_key_file_is_refused_by_the_stored_version(keys):
    emptied = keys.sign_key_file({"key_file_version": 1, "keys": [], "revocations": []})

    result = verify(keys, key_file=emptied, stored_key_file_version=4)

    assert result.status is Status.KEY_FILE_ROLLBACK


@pytest.mark.parametrize("stored, status", [(None, Status.OK), (1, Status.OK), (3, Status.OK), (4, Status.KEY_FILE_ROLLBACK)])
def test_key_file_version_boundaries(keys, stored, status):
    assert verify(keys, key_file=keys.key_file(version=3), stored_key_file_version=stored).status is status


def test_a_later_key_file_version_is_accepted(keys):
    result = verify(keys, key_file=keys.key_file(version=9), stored_key_file_version=2)

    assert result.ok
    assert result.key_file_version == 9


def test_key_file_rollback_applies_in_baked_mode(keys):
    old = keys.key_file(version=1)

    assert verify(keys, key_file=old, stored_key_file_version=2, baked=True).status is Status.KEY_FILE_ROLLBACK
    assert verify(keys, key_file=old, baked=True).ok


def test_revocation_names_only_its_own_key(keys):
    data = catalog_bytes()
    other = Ed25519PrivateKey.generate()
    other_id = key_id(raw_public(other))
    revocation = keys.revocation(SIGNED_AT - timedelta(days=1), kid=other_id)
    key_file = keys.key_file([revocation], extra=[keys.entry(private=other)])

    assert verify(keys, data, envelope_for(data, keys.ci), key_file).ok
    result = verify(keys, data, envelope_for(data, other), key_file)
    assert result.status is Status.REVOKED_KEY
    assert result.signer_key_id == other_id


def test_revoked_key_does_not_hide_a_good_second_signature(keys):
    data = catalog_bytes()
    spare = Ed25519PrivateKey.generate()
    revocation = keys.revocation(SIGNED_AT - timedelta(days=1))
    key_file = keys.key_file([revocation], extra=[keys.entry(private=spare)])

    result = verify(keys, data, envelope_for(data, keys.ci, spare), key_file)

    assert result.ok
    assert result.signer_key_id == key_id(raw_public(spare))


def test_revoked_key_with_a_bad_signature_is_a_bad_signature(keys):
    data = catalog_bytes()
    revocation = keys.revocation(SIGNED_AT - timedelta(days=1))
    envelope = envelope_for(b"something else", keys.ci)

    result = verify(keys, data, envelope, keys.key_file([revocation]))

    assert result.status is Status.BAD_SIGNATURE


# Expiry and skew


def test_expiry_boundaries(keys):
    data = catalog_bytes(expires=NOW)

    assert verify(keys, data, now=NOW).ok
    assert verify(keys, data, now=NOW + timedelta(seconds=1)).ok
    assert verify(keys, data, now=NOW + CLOCK_SKEW).ok

    late = NOW + CLOCK_SKEW + timedelta(seconds=1)
    result = verify(keys, data, now=late)
    assert result.status is Status.EXPIRED
    assert not result.usable
    assert result.host_time == late
    assert result.expires == NOW
    assert stamp(late) in result.detail and stamp(NOW) in result.detail


def test_not_before_boundaries(keys):
    not_before = NOW + timedelta(days=10)
    data = catalog_bytes(not_before=not_before, expires=not_before + timedelta(days=30))

    assert verify(keys, data, now=not_before).ok
    assert verify(keys, data, now=not_before - CLOCK_SKEW).ok

    early = not_before - CLOCK_SKEW - timedelta(seconds=1)
    result = verify(keys, data, now=early)
    assert result.status is Status.NOT_YET_VALID
    assert not result.usable and not result.may_advance_state
    assert result.host_time == early
    assert result.not_before == not_before
    assert stamp(early) in result.detail and stamp(not_before) in result.detail


def test_future_not_before_is_not_a_clock_wrong_result(keys):
    data = catalog_bytes(not_before=NOW + timedelta(days=300), expires=NOW + timedelta(days=360))

    assert verify(keys, data).status is Status.NOT_YET_VALID
    # Only a host clock earlier than the running build is "clock wrong".
    wrong = verify(keys, data, host_build_time=NOW + timedelta(days=1))
    assert wrong.status is Status.CLOCK_WRONG
    assert wrong.usable


def test_clock_before_build_time_skips_expiry(keys):
    data = catalog_bytes(not_before=NOW - timedelta(days=200), expires=NOW - timedelta(days=110))
    build_time = NOW + timedelta(days=1)

    stale = verify(keys, data, now=NOW)
    assert stale.status is Status.EXPIRED

    result = verify(keys, data, now=NOW, host_build_time=build_time)
    assert result.status is Status.CLOCK_WRONG
    assert result.usable and result.may_advance_state
    assert not result.ok
    assert "host time" in result.detail


def test_clock_before_build_time_still_applies_the_counter(keys):
    result = verify(keys, now=NOW, host_build_time=NOW + timedelta(days=1), stored_counter=11)

    assert result.status is Status.REPLAYED


def test_clock_at_or_after_build_time_is_trusted(keys):
    data = catalog_bytes(not_before=NOW - timedelta(days=200), expires=NOW - timedelta(days=110))

    result = verify(keys, data, now=NOW, host_build_time=NOW)

    assert result.status is Status.EXPIRED


def test_validity_window_longer_than_366_days_is_rejected(keys):
    ok = catalog_bytes(not_before=SIGNED_AT, expires=SIGNED_AT + timedelta(days=366))
    too_long = catalog_bytes(not_before=SIGNED_AT, expires=SIGNED_AT + timedelta(days=366, seconds=1))

    assert verify(keys, ok).ok
    assert verify(keys, too_long).status is Status.MALFORMED


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(days=-1)])
def test_validity_window_must_be_positive(keys, delta):
    data = catalog_bytes(not_before=SIGNED_AT, expires=SIGNED_AT + delta)

    assert verify(keys, data).status is Status.MALFORMED


# Replay counter


@pytest.mark.parametrize(
    "stored, verdict",
    [
        (None, CounterVerdict.FIRST),
        (9, CounterVerdict.ADVANCED),
        (10, CounterVerdict.UNCHANGED),
        (11, CounterVerdict.REPLAYED),
    ],
)
def test_compare_counter(stored, verdict):
    assert compare_counter(10, stored) is verdict


@pytest.mark.parametrize(
    "stored, status",
    [
        (None, Status.OK),
        (0, Status.OK),
        (9, Status.OK),
        (10, Status.OK),
        (11, Status.REPLAYED),
    ],
)
def test_verify_applies_the_counter(keys, stored, status):
    result = verify(keys, stored_counter=stored)

    assert result.status is status
    assert result.signed_version == 10
    assert not (status is Status.REPLAYED and result.usable)


def test_counter_is_compared_per_channel(keys):
    beta = catalog_bytes(channel="beta", version=40)
    main = catalog_bytes(channel="main", version=7)
    stored = {"beta": 40, "main": 5}

    beta_result = verify(keys, beta, channel="beta", stored_counter=stored["beta"])
    main_result = verify(keys, main, channel="main", stored_counter=stored["main"])
    assert beta_result.ok and main_result.ok

    # The main counter is far below beta's. Comparing against the wrong
    # channel's value would lock the host out, so a channel switch must use
    # that channel's own stored value.
    assert verify(keys, main, channel="main", stored_counter=stored["beta"]).status is Status.REPLAYED
    assert verify(keys, main, channel="main", stored_counter=stored["main"]).ok


def test_catalog_for_another_channel_is_rejected(keys):
    beta = catalog_bytes(channel="beta")

    result = verify(keys, beta, channel="main")

    assert result.status is Status.CHANNEL_MISMATCH
    assert result.channel == "beta"
    assert not result.usable


# Baked mode


def test_baked_catalog_ignores_expiry(keys):
    data = catalog_bytes(not_before=NOW - timedelta(days=500), expires=NOW - timedelta(days=410))

    assert verify(keys, data).status is Status.EXPIRED
    result = verify(keys, data, baked=True)

    assert result.status is Status.OK
    assert result.baked is True
    assert result.usable
    assert not result.may_advance_state
    assert result.counter is None


def test_baked_catalog_ignores_the_counter_and_the_clock(keys):
    result = verify(
        keys, baked=True, stored_counter=999, host_build_time=NOW + timedelta(days=30)
    )

    assert result.status is Status.OK
    assert result.counter is None
    assert not result.may_advance_state


def test_baked_catalog_still_needs_a_valid_signature(keys):
    data = catalog_bytes(note="alpha")
    envelope = envelope_for(data, keys.ci)

    assert verify(keys, data.replace(b"alpha", b"alphb"), envelope, baked=True).status is Status.BAD_SIGNATURE
    stranger = Ed25519PrivateKey.generate()
    assert verify(keys, data, envelope_for(data, stranger), baked=True).status is Status.BAD_SIGNATURE
    unsigned = verify_catalog(
        data, None, keys.key_file(), roots=keys.roots, channel="main", now=NOW, baked=True
    )
    assert unsigned.status is Status.UNSIGNED
    assert unsigned.baked is True


def test_baked_catalog_still_checks_revocation_and_channel(keys):
    revocation = keys.revocation(SIGNED_AT - timedelta(days=1))

    revoked = verify(keys, key_file=keys.key_file([revocation]), baked=True)
    assert revoked.status is Status.REVOKED_KEY

    beta = catalog_bytes(channel="beta")
    assert verify(keys, beta, channel="main", baked=True).status is Status.CHANNEL_MISMATCH


# Malformed input


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(schema_version=2),
        lambda e: e.update(schema_version=True),
        lambda e: e.update(signatures=[]),
        lambda e: e.update(signatures="nope"),
        lambda e: e.pop("signatures"),
        lambda e: e["signatures"][0].update(algorithm="rsa"),
        lambda e: e["signatures"][0].update(key_id="XYZ"),
        lambda e: e["signatures"][0].update(signature="***"),
        lambda e: e["signatures"][0].update(signature=b64(b"short")),
        lambda e: e["signatures"][0].pop("key_id"),
    ],
)
def test_malformed_envelope(keys, mutate):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    mutate(envelope)

    result = verify(keys, data, envelope)

    assert result.status is Status.MALFORMED
    assert not result.usable


def test_an_envelope_with_too_many_signatures_is_malformed(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    template = envelope["signatures"][0]
    envelope["signatures"] = [
        {**template, "key_id": "%016x" % number} for number in range(17)
    ]

    result = verify(keys, data, envelope)

    assert result.status is Status.MALFORMED
    assert "at most 16" in result.detail


def test_sixteen_signatures_are_still_accepted(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    template = envelope["signatures"][0]
    envelope["signatures"] = [
        {**template, "key_id": "%016x" % number} for number in range(15)
    ] + envelope["signatures"]

    assert verify(keys, data, envelope).ok


def test_two_signatures_for_one_key_id_are_malformed(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    envelope["signatures"].append(dict(envelope["signatures"][0]))

    assert verify(keys, data, envelope).status is Status.MALFORMED


def test_the_failure_detail_stays_short(keys):
    data = catalog_bytes()
    stranger = Ed25519PrivateKey.generate()
    envelope = envelope_for(data, *[Ed25519PrivateKey.generate() for _ in range(16)], stranger)
    envelope["signatures"] = envelope["signatures"][:16]

    result = verify(keys, data, envelope)

    assert result.status is Status.BAD_SIGNATURE
    assert 0 < len(result.detail) <= 300


def test_envelope_that_is_not_json_or_not_an_object(keys):
    assert verify(keys, envelope="{not json").status is Status.MALFORMED
    assert verify(keys, envelope="[1, 2]").status is Status.MALFORMED
    assert verify(keys, envelope=b"\xff\xfe").status is Status.MALFORMED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.pop("keys"),
        lambda f: f.update(keys={}),
        lambda f: f.update(revocations="x"),
        lambda f: f["keys"][0].pop("algorithm"),
        lambda f: f["keys"][0].update(algorithm=5),
        lambda f: f["keys"].append({"algorithm": "ml-dsa-65", "key_id": "k1"}),
        lambda f: f["keys"].append({"algorithm": "ml-dsa-65", "key_id": "k1", "public_key": "x" * 300, "added": "d"}),
        lambda f: f["keys"].extend([
            {"algorithm": "ml-dsa-65", "key_id": "k1", "public_key": "x", "added": "d"},
            {"algorithm": "ml-dsa-65", "key_id": "k1", "public_key": "y", "added": "d"},
        ]),
        lambda f: f["keys"][0].update(key_id="0" * 16),
        lambda f: f["keys"][0].update(public_key="***"),
        lambda f: f["keys"][0].update(public_key=b64(b"short")),
        lambda f: f.pop("key_file_version"),
        lambda f: f.update(key_file_version=0),
        lambda f: f.update(key_file_version=True),
        lambda f: f.update(key_file_version="1"),
        lambda f: f.pop("signature"),
        lambda f: f.update(signature=b64(b"short")),
        lambda f: f.update(root_key_id="nothex"),
        lambda f: f["keys"][0].update(added=""),
        lambda f: f["keys"].append(copy.deepcopy(f["keys"][0])),
        lambda f: f.update(revocations=[{"key_id": "a" * 16, "revoked_at": "2026-01-01"}]),
        lambda f: f.update(revocations=[{"key_id": "nothex", "revoked_at": "2026-01-01T00:00:00Z"}]),
    ],
)
def test_malformed_key_file(keys, mutate):
    key_file = keys.key_file()
    mutate(key_file)

    assert verify(keys, key_file=key_file).status is Status.MALFORMED


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        b"\xff\xfe",
        json.dumps({"signed_version": 1, "not_before": "2026-10-31T12:00:00Z", "expires": "2027-01-01T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": True, "not_before": "2026-10-31T12:00:00Z", "expires": "2027-01-01T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": 0, "not_before": "2026-10-31T12:00:00Z", "expires": "2027-01-01T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": "7", "not_before": "2026-10-31T12:00:00Z", "expires": "2027-01-01T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": 1, "not_before": "2026-10-31", "expires": "2027-01-01T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": 1, "not_before": "2026-10-31T12:00:00Z", "expires": "2027-02-30T00:00:00Z"}).encode(),
        json.dumps({"channel": "main", "signed_version": 1, "not_before": "2026-10-31T12:00:00Z"}).encode(),
    ],
)
def test_malformed_signed_metadata(keys, data):
    envelope = envelope_for(data, keys.ci)

    result = verify(keys, data, envelope)

    assert result.status is Status.MALFORMED
    assert not result.usable


def test_parse_wire_format_raises_wire_format_error(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)

    parsed = parse_wire_format(keys.key_file(), envelope, data)
    assert parsed.metadata.channel == "main"
    assert parsed.metadata.signed_version == 10
    assert [entry.key_id for entry in parsed.signatures] == [keys.ci_id]
    assert [entry.key_id for entry in parsed.key_file.keys] == [keys.ci_id]

    with pytest.raises(WireFormatError):
        parse_wire_format(keys.key_file(), {"schema_version": 1, "signatures": []}, data)


def test_naive_datetimes_are_a_programming_error(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    naive = datetime(2026, 11, 1, 12, 0, 0)

    with pytest.raises(ValueError):
        verify_catalog(data, envelope, keys.key_file(), roots=keys.roots, channel="main", now=naive)
    with pytest.raises(ValueError):
        verify_catalog(
            data, envelope, keys.key_file(), roots=keys.roots, channel="main", now=NOW, host_build_time=naive
        )


def test_unknown_root_in_the_caller_map_trusts_nothing(keys):
    result = verify(keys, roots={})

    assert result.status is Status.BAD_SIGNATURE


def test_extra_fields_in_the_key_file_and_envelope_are_ignored(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    envelope["comment"] = "extra"
    envelope["signatures"][0]["note"] = "extra"
    key_file = keys.key_file()
    key_file["generated"] = "2026-10-01"
    key_file["keys"][0]["label"] = "ci"

    assert verify(keys, data, envelope, key_file).ok


# Strict parsing and hostile input


def test_a_key_entry_with_an_unknown_algorithm_is_skipped_but_stays_signed(keys):
    data = catalog_bytes()
    future = {"key_id": "ab" * 8, "algorithm": "ml-dsa-65", "public_key": "not-base64-at-all!", "added": "2026-11-01"}
    key_file = keys.key_file(extra=[future])

    assert verify(keys, data, key_file=key_file).ok
    # It is covered by the root's signature: dropping it from the file breaks the file.
    stripped = copy.deepcopy(key_file)
    stripped["keys"] = [entry for entry in stripped["keys"] if entry["algorithm"] == "ed25519"]
    assert verify(keys, data, key_file=stripped).status is Status.BAD_SIGNATURE
    # And it can never verify a catalog: a signature naming it is not in the usable keys.
    envelope = envelope_for(data, keys.ci)
    envelope["signatures"] = [{"algorithm": "ed25519", "key_id": future["key_id"], "signature": b64(b"x" * 64)}]
    assert verify(keys, data, envelope, key_file).status is Status.BAD_SIGNATURE


def test_unknown_signature_algorithms_are_skipped(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    envelope["signatures"].insert(0, {"algorithm": "ml-dsa-65", "key_id": "x", "signature": 7})
    envelope["signatures"].append({"algorithm": "rsa"})

    assert verify(keys, data, envelope).ok


def test_unknown_algorithm_does_not_rescue_a_bad_ed25519_signature(keys):
    data = catalog_bytes()
    stranger = Ed25519PrivateKey.generate()
    envelope = envelope_for(data, stranger)
    envelope["signatures"].append({"algorithm": "ml-dsa-65", "key_id": keys.ci_id, "signature": "AAAA"})

    assert verify(keys, data, envelope).status is Status.BAD_SIGNATURE


def test_envelope_with_only_unknown_algorithms_is_malformed_not_unsigned(keys):
    data = catalog_bytes()
    envelope = {"schema_version": 1, "signatures": [{"algorithm": "ml-dsa-65", "key_id": "x", "signature": "y"}]}

    result = verify(keys, data, envelope)

    assert result.status is Status.MALFORMED
    assert "no ed25519 signature" in result.detail


def test_signature_entry_without_an_algorithm_is_malformed(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    envelope["signatures"].append({"key_id": keys.ci_id})

    assert verify(keys, data, envelope).status is Status.MALFORMED


def test_duplicate_keys_in_the_catalog_are_rejected(keys):
    data = (
        b'{"channel": "beta", "channel": "main", "signed_version": 10, '
        b'"not_before": "%s", "expires": "%s"}' % (stamp(SIGNED_AT).encode(), stamp(EXPIRES).encode())
    )

    assert verify(keys, data).status is Status.MALFORMED


def test_duplicate_keys_in_the_envelope_and_key_file_are_rejected(keys):
    data = catalog_bytes()
    envelope = json.dumps(envelope_for(data, keys.ci))
    duplicated_envelope = envelope.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    key_file = json.dumps(keys.key_file())
    duplicated_key_file = key_file.replace('"revocations"', '"revocations": [], "revocations"')

    assert duplicated_envelope != envelope and duplicated_key_file != key_file
    assert verify(keys, data, duplicated_envelope).status is Status.MALFORMED
    assert verify(keys, data, key_file=duplicated_key_file).status is Status.MALFORMED


def test_catalog_bytes_must_be_utf8(keys):
    document = catalog_bytes().decode("utf-8")

    for encoded in (document.encode("utf-16"), b"\xef\xbb\xbf" + document.encode("utf-8")):
        result = verify(keys, encoded, envelope_for(encoded, keys.ci))
        assert result.status is Status.MALFORMED


def test_non_finite_numbers_are_rejected(keys):
    data = b'{"channel": "main", "signed_version": NaN, "not_before": "%s", "expires": "%s"}' % (
        stamp(SIGNED_AT).encode(),
        stamp(EXPIRES).encode(),
    )

    assert verify(keys, data).status is Status.MALFORMED


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"])
def test_non_finite_numbers_in_an_ignored_field_are_rejected(keys, constant):
    """Metadata is valid, so only strict JSON parsing can refuse this catalog."""
    data = b'{"channel": "main", "signed_version": 10, "not_before": "%s", "expires": "%s", "weight": %s}' % (
        stamp(SIGNED_AT).encode(),
        stamp(EXPIRES).encode(),
        constant,
    )

    assert verify(keys, data).status is Status.MALFORMED


FULLWIDTH = str.maketrans("0123456789", "\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19")


def test_fullwidth_digits_in_timestamps_are_malformed_not_a_crash(keys):
    document = json.loads(catalog_bytes())
    document["not_before"] = stamp(SIGNED_AT).translate(FULLWIDTH)
    tampered = json.dumps(document).encode("utf-8")
    assert verify(keys, tampered, envelope_for(tampered, keys.ci)).status is Status.MALFORMED

    revocation = keys.revocation(NOW)
    revocation["revoked_at"] = revocation["revoked_at"].translate(FULLWIDTH)
    assert verify(keys, key_file=keys.key_file([revocation])).status is Status.MALFORMED


def test_trailing_newline_is_rejected_in_key_ids_and_timestamps(keys):
    data = catalog_bytes()
    document = json.loads(data)
    document["expires"] += "\n"
    tampered = json.dumps(document).encode("utf-8")
    assert verify(keys, tampered, envelope_for(tampered, keys.ci)).status is Status.MALFORMED

    envelope = envelope_for(data, keys.ci)
    envelope["signatures"][0]["key_id"] += "\n"
    assert verify(keys, data, envelope).status is Status.MALFORMED


def test_deeply_nested_documents_are_malformed_not_a_crash(keys):
    data = catalog_bytes()
    deep = "[" * 200000 + "]" * 200000
    deep_object = '{"a":' * 100000 + "1" + "}" * 100000

    assert verify(keys, data, deep).status is Status.MALFORMED
    assert verify(keys, data, deep_object).status is Status.MALFORMED
    assert verify(keys, data, key_file=deep).status is Status.MALFORMED
    assert verify(keys, data, key_file=deep_object).status is Status.MALFORMED
    nested = deep.encode("utf-8")
    assert verify(keys, nested, envelope_for(nested, keys.ci)).status is Status.MALFORMED


def test_extreme_dates_do_not_overflow(keys):
    first = datetime(1, 1, 1, tzinfo=timezone.utc)
    last = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    old = catalog_bytes(not_before=first, expires=first + timedelta(days=30))
    assert verify(keys, old, now=last).status is Status.EXPIRED
    assert verify(keys, old, now=first).ok

    far = catalog_bytes(not_before=last - timedelta(days=30), expires=last)
    assert verify(keys, far, now=first).status is Status.NOT_YET_VALID
    assert verify(keys, far, now=last).ok
    assert verify(keys, far, now=datetime.max.replace(tzinfo=timezone.utc)).ok

    both = catalog_bytes(not_before=first, expires=last)
    assert verify(keys, both, now=NOW).status is Status.MALFORMED
    assert verify(keys, old, now=NOW, host_build_time=last).status is Status.CLOCK_WRONG


GARBAGE_STATUSES = {Status.UNSIGNED, Status.MALFORMED, Status.BAD_SIGNATURE}


def test_a_baked_catalog_still_needs_a_sane_validity_window(keys):
    inverted = catalog_bytes(not_before=EXPIRES, expires=SIGNED_AT)
    too_long = catalog_bytes(not_before=SIGNED_AT, expires=SIGNED_AT + timedelta(days=367))
    longest = catalog_bytes(not_before=SIGNED_AT, expires=SIGNED_AT + timedelta(days=366))

    assert verify(keys, inverted, baked=True).status is Status.MALFORMED
    assert verify(keys, too_long, baked=True).status is Status.MALFORMED
    assert verify(keys, longest, baked=True).status is Status.OK


def test_garbage_never_raises(keys):
    data = catalog_bytes()
    envelope = envelope_for(data, keys.ci)
    samples = [None, 0, 1.5, [], {}, "", "null", "[]", "{}", b"\x00", b"\xff", "\ud800", {"keys": None}, {"schema_version": None}]
    for sample in samples:
        for call in (
            lambda v: verify_catalog(data, v, keys.key_file(), roots=keys.roots, channel="main", now=NOW),
            lambda v: verify_catalog(data, envelope, v, roots=keys.roots, channel="main", now=NOW),
            lambda v: verify_catalog(v if isinstance(v, (bytes, str)) else b"", envelope, keys.key_file(), roots=keys.roots, channel="main", now=NOW),
        ):
            result = call(sample)
            assert not result.usable
            assert result.status in GARBAGE_STATUSES


@pytest.mark.parametrize(
    "value",
    [10**5000, 2**63, 0, -1, True, 1.5, "1"],
    ids=["huge", "2**63", "zero", "negative", "bool", "float", "string"],
)
def test_out_of_range_counters_are_malformed_not_a_crash(keys, value):
    # A parsed key file can hold an int far past the JSON digit limit.
    key_file = keys.key_file()
    key_file["key_file_version"] = value
    assert verify(keys, key_file=key_file).status is Status.MALFORMED

    literal = b"9" * 6000 if value == 10**5000 else json.dumps(value).encode("ascii")
    data = catalog_bytes().replace(b'"signed_version": 10', b'"signed_version": ' + literal)
    assert verify(keys, data, envelope_for(data, keys.ci)).status is Status.MALFORMED


def test_oversized_digit_strings_in_json_text_are_malformed(keys):
    text = '{"key_file_version": %s}' % ("9" * 6000)

    assert verify(keys, key_file=text).status is Status.MALFORMED


def test_largest_counters_are_accepted(keys):
    data = catalog_bytes(version=2**63 - 1)

    assert verify(keys, data, key_file=keys.key_file(version=2**63 - 1)).ok
