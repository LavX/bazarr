# coding=utf-8
"""Scrub secrets out of what the backend stores, shows and logs.

Two layers, because a leak has two shapes.

PATTERNS covers text whose credential never passed through this install's
settings: a third-party library that echoed back the request it built, a
provider that named the key it rejected. The shapes are ``Bearer <token>``,
``x-goog-api-key`` assignments, Google ``AIza...`` keys, OpenRouter
``sk-or-v1-...`` keys, and ciphertext in either the at-rest ``enc:v1:`` form
or the translator's submitted ``enc:`` form.

VALUES covers the exact credentials this install holds: every at-rest secret
value in the settings tree plus every Provider Hub installation credential,
decrypted. An exact value is masked in text (logs, payloads, messages), label
or no label, which is what covers the shapes no pattern knows - a self-hosted
provider's key, a webhook URL that carries its token, a password a plugin
quoted back. In a Provider Hub state document it is masked only in the fields
that carry free text: the rest of that document is structure, and a username
that equals a provider id must not be rewritten to the mask.

Both stay cheap on ordinary text: the value set compiles to one alternation,
rebuilt only when the live credentials change, and text with nothing to mask
comes back unchanged after a bounded scan.
"""

import logging
import re
from threading import RLock, local
from typing import Any, Iterable, Mapping

from .crypto import decrypt_secret
from .registry import (SYSTEM_SECRETS, USER_VISIBLE_SECRET_LISTS, at_rest_secret_paths,
                       installation_secret_fields)

logger = logging.getLogger(__name__)

# What a masked credential reads as. The settings serializer masks the same
# way, so a value masked here and a value withheld there are recognisably
# both "a secret was configured here".
MASK = '***'

# A credential shorter than this is not registered as an exact value. Exact
# masking replaces the value wherever it appears, so a password of "test" would
# redact every "test" in logs and job text. The trade-off is deliberate and
# stays: a configured credential of five characters or fewer is not masked by
# value, and a provider error that echoes one reaches logs and job text as is.
# Proxy, PostgreSQL and some provider password validators accept values that
# short, so use a longer one. The patterns below (bearer tokens, Google and
# OpenRouter keys, ciphertext) match by shape and are not affected.
_MIN_VALUE_LENGTH = 6

_BEARER_RE = re.compile(r'(?i)\b(Bearer\s+)([A-Za-z0-9\-._~+/=]{8,})')
_GOOG_API_KEY_RE = re.compile(r'(?i)(x-goog-api-key["\']?\s*[:=]\s*["\']?)([A-Za-z0-9\-_]{8,})')
_GOOGLE_KEY_RE = re.compile(r'\bAIza[0-9A-Za-z\-_]{35}\b')
_OPENROUTER_KEY_RE = re.compile(r'\bsk-or-v1-[0-9a-fA-F]{64}\b')
# Ciphertext is skipped by the value layer because it is not a plaintext
# credential. A service that echoes the request, or an error body that quotes
# the stored value, still carries it, so the shape is masked wherever it is
# logged, stored on a job or returned by an endpoint. The token length floor
# leaves the bare marker in prose alone.
_ENC_CIPHERTEXT_RE = re.compile(r'\benc:(?:v1:)?[A-Za-z0-9_\-+/=.]{16,}')


def _mask_bearer(match):
    """Mask a Bearer token, keeping the scheme.

    Prose about a credential ("Bearer authentication is required") stays, and
    so does a short word. A token of sixteen characters or more is masked
    whether or not it carries a digit: a real token can be all letters, and a
    prose word that long after "Bearer" does not exist in practice.
    """
    token = match.group(2)
    if len(token) < 16 and not any(character.isdigit() or character in '-._~+/=' for character in token):
        return match.group(0)
    return match.group(1) + MASK


# Every pattern with what replaces its match: a replacement string keeps the
# label a value sits behind, so a scrubbed line still says what leaked.
_PATTERNS = (
    (_BEARER_RE, _mask_bearer),
    (_GOOG_API_KEY_RE, r'\1' + MASK),
    (_GOOGLE_KEY_RE, MASK),
    (_OPENROUTER_KEY_RE, MASK),
    (_ENC_CIPHERTEXT_RE, MASK),
)

# The exact credential values currently masked, with the regex that masks
# them, as one immutable pair. Published by a single assignment in
# register_secret_values: a reader that caught the new set with the old regex
# would return the credential unchanged, which is exactly the leak this layer
# exists to prevent.
_VALUE_STATE: 'tuple[frozenset[str], re.Pattern[str] | None]' = (frozenset(), None)

# The credential containers a Provider Hub state document holds. Values under
# these keys are a plugin's own stored credentials, encrypted at rest by the
# boot migration; masking a non-secret field because it happens to repeat a
# credential's text would hand the plugin the mask as its value. The
# migration is what covers those containers.
_CREDENTIAL_CONTAINERS = frozenset({'config'})

# The state fields that carry free text: what a worker, a provider or a raised
# error said. An exact credential value is masked only in these, because the
# rest of the document is structure. A username that happens to equal a
# provider id ("opensubtitles") would otherwise rename the installation and
# its manifest to the mask on the next boot. The pattern shapes are masked
# everywhere: no identifier is shaped like a Bearer token or an API key.
_STATE_TEXT_FIELDS = frozenset({
    'message', 'error', 'last_error', 'detail', 'details', 'reason',
    'stderr', 'stdout', 'output', 'traceback', 'activity', 'notes',
    'trust_note', 'log',
})

# Refreshes are serialized so the registration order matches the collection
# order: a slow refresh that started before a settings write must not publish
# its older set after the write's own refresh. A refresh re-entering itself
# (a settings write triggered from a decryption inside one, on the same
# thread) answers with the set it already has instead of recursing; other
# threads wait for the lock and then collect for real.
_refresh_lock = RLock()
_refresh_local = local()


def registered_secret_values() -> frozenset[str]:
    """The exact credential values currently masked in text."""
    return _VALUE_STATE[0]


def register_secret_values(values: Iterable[Any]) -> int:
    """Replace the exact credential values masked in text.

    Returns how many values are registered. A value shorter than
    ``_MIN_VALUE_LENGTH``, one that is already just the mask, and one that is
    ciphertext are skipped: none of them is a plaintext credential a leak
    could carry, and masking a short one would redact ordinary text.

    The set and its regex are published together, in one assignment, so no
    reader can see the new values behind the old regex.
    """
    global _VALUE_STATE
    cleaned = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        text = value.strip()
        if (len(text) >= _MIN_VALUE_LENGTH
                and text != MASK
                and not text.startswith('enc:v1:')
                ):
            cleaned.add(text)
    registered = frozenset(cleaned)
    # Longest first: a credential that contains another is masked whole
    # rather than in pieces.
    ordered = sorted(registered, key=len, reverse=True)
    regex = re.compile('|'.join(re.escape(value) for value in ordered)) if ordered else None
    _VALUE_STATE = (registered, regex)
    return len(registered)


def clear_secret_values() -> None:
    """Forget every registered value, so tests and a torn-down boot leave no
    stale credential masked."""
    register_secret_values(())


def scrub_text(text: str) -> str:
    """The text with every known credential shape and every registered
    credential value masked.

    A non-string, an empty string, and text with nothing to mask come back
    unchanged.
    """
    if not isinstance(text, str) or not text:
        return text

    text = _mask_patterns(text)

    values, regex = _VALUE_STATE
    if values and regex is not None and any(value in text for value in values):
        text = regex.sub(MASK, text)
    return text


def _mask_patterns(text: str) -> str:
    """The text with the credential shapes masked, registered values untouched.

    The state scrub applies this to every string in a Provider Hub document
    and the exact-value layer only to its free-text fields: a shape is never a
    legitimate identifier, an exact value can be one.
    """
    for pattern, replacement in _PATTERNS:
        if pattern.search(text):
            text = pattern.sub(replacement, text)
    return text


def scrub_value(value: Any) -> Any:
    """The value with every string inside it scrubbed, for a payload that is
    stored or sent as a whole.

    A dict, list or tuple is rebuilt with scrubbed members, so the caller
    rebinds the result; every other type comes back as it is. Keys are
    structure, not text the backend prints, and stay as they are.
    """
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {key: scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(item) for item in value)
    return value


def _read_setting(settings_obj, section: str, key: str):
    """One setting by section and key, tolerating the mixed-case section
    names a settings dict may carry and both the mapping and the attribute
    access a Dynaconf settings object answers."""
    section_obj = None
    for name in (section, section.lower(), section.upper()):
        try:
            if isinstance(settings_obj, Mapping):
                if name in settings_obj:
                    section_obj = settings_obj[name]
                    break
            else:
                section_obj = getattr(settings_obj, name, None)
                if section_obj is not None:
                    break
        except Exception:
            continue
    if section_obj is None:
        return None
    for name in (key, key.lower(), key.upper()):
        try:
            if isinstance(section_obj, Mapping):
                if name in section_obj:
                    return section_obj[name]
            else:
                value = getattr(section_obj, name, None)
                if value is not None:
                    return value
        except Exception:
            continue
    return None


def collect_settings_secret_values(settings_obj) -> set[str]:
    """Every plaintext credential the settings tree holds at a registered
    secret path: one value per at-rest path, one per entry of a secret list.

    Three tiers are walked. The at-rest paths cover the credentials stored in
    config.yaml and the plugin secrets registered for it. The user-visible
    lists are credentials too, one per entry, and sit outside the at-rest
    walk because their pipeline handles a list where that one handles a
    scalar. The system secrets (the signing keys and the master key) are
    credentials as well, so they are registered even though no at-rest pass
    walks them."""
    values: set[str] = set()
    for path in at_rest_secret_paths() | USER_VISIBLE_SECRET_LISTS | SYSTEM_SECRETS:
        text = str(path)
        section, _, key = text.partition('.')
        if not section or not key:
            continue
        value = _read_setting(settings_obj, section, key)
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(item for item in value if isinstance(item, str))
    return values


def collect_hub_secret_values(state=None) -> set[str]:
    """Every plaintext credential a Provider Hub state document holds,
    decrypted: one value per registered secret field of every installation.

    A stored value that will not decrypt is skipped with a warning rather
    than raised: it stays covered at rest by the migration's refusal, and
    collecting the scrub set must not be what keeps the boot from
    finishing. A document that cannot be read at all is different: it raises
    so the caller keeps the set it already registered instead of publishing
    a collection that is missing every Hub value. ``state`` is the document
    a caller already read, so a writer can collect without a second read;
    None loads it.
    """
    values: set[str] = set()
    if state is None:
        from provider_hub.state import load_state_strict
        state = load_state_strict()
    installations = state.get("installations") if isinstance(state, dict) else None
    if not isinstance(installations, dict):
        # A document whose inventory is not an object is unreadable, not an
        # empty inventory: raising keeps the caller's current set instead of
        # publishing one that is missing every Hub credential.
        raise ValueError("the Provider Hub state installations are not an object")
    for provider_id, installation in installations.items():
        if not isinstance(installation, dict):
            continue
        config = installation.get('config')
        if not isinstance(config, dict):
            continue
        wanted = {str(field).lower()
                  for field in installation_secret_fields(provider_id, installation,
                                                          state.get('plugin_secret_paths'))}
        for stored_field, stored_value in config.items():
            if not isinstance(stored_field, str) or stored_field.lower() not in wanted:
                continue
            if not isinstance(stored_value, str) or not stored_value:
                continue
            try:
                plaintext = decrypt_secret(stored_value)
            except Exception as error:
                logger.warning("A stored Provider Hub credential could not be decrypted "
                               "to register it for scrubbing: %s", type(error).__name__)
                continue
            if isinstance(plaintext, str) and plaintext:
                values.add(plaintext)
    return values


def refresh_secret_values(settings_obj=None, state=None) -> int:
    """Rebuild the registered value set from the live settings and the
    Provider Hub state.

    Called at boot and after a settings or Hub write, so a credential the
    user just entered is masked in the same process that stores it. Never
    raises: a read that fails leaves the previous set in place, because
    refusing a boot or a save over the scrub set would be worse than a
    credential that is not yet registered.

    Refreshes are serialized, so a collection that started before a settings
    write cannot publish its older set after the write's own refresh. A
    refresh that re-enters itself on the same thread (a settings write
    triggered from a decryption inside one) answers with the current set.
    """
    with _refresh_lock:
        if getattr(_refresh_local, 'depth', 0):
            return len(_VALUE_STATE[0])
        _refresh_local.depth = 1
        try:
            if settings_obj is None:
                from app.config import settings as settings_obj
            values = collect_settings_secret_values(settings_obj)
            values |= collect_hub_secret_values(state)
        except Exception as error:
            logger.warning("The credential values to scrub could not be collected: %s",
                           type(error).__name__)
            return len(_VALUE_STATE[0])
        finally:
            _refresh_local.depth = 0
        return register_secret_values(values)


def scrub_state_secrets(state) -> bool:
    """Mask every registered credential and known shape in a Provider Hub
    state document, in place. Returns True when it masked something.

    The credential shapes are masked in every string, because no identifier
    looks like one. An exact registered value is masked only in the free-text
    fields (``_STATE_TEXT_FIELDS``), because the rest of the document is
    structure: an install whose username equals its provider id would
    otherwise have its installation and manifest ids rewritten to the mask.
    Credential containers are always left alone; the migration covers them.
    """
    if not isinstance(state, dict):
        return False
    changed = False

    def walk(node, in_text):
        nonlocal changed
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key in _CREDENTIAL_CONTAINERS:
                    continue
                text_field = in_text or (isinstance(key, str) and key in _STATE_TEXT_FIELDS)
                if isinstance(value, str):
                    masked = scrub_text(value) if text_field else _mask_patterns(value)
                    if masked != value:
                        node[key] = masked
                        changed = True
                elif isinstance(value, (dict, list)):
                    walk(value, text_field)
        else:
            for index, value in enumerate(node):
                if isinstance(value, str):
                    masked = scrub_text(value) if in_text else _mask_patterns(value)
                    if masked != value:
                        node[index] = masked
                        changed = True
                elif isinstance(value, (dict, list)):
                    walk(value, in_text)

    walk(state, False)
    return changed


# The job-row fields that are structure, not text: the action, the target, the
# ids, the versions, the state and the timings. A credential that happens to
# read "install" or equal a provider id must not rewrite them, so they take the
# credential shapes only. The list is of what to spare, not of what to scrub:
# every other field of the row (the message, the error, the details with any
# config echo, the display names, and any field a later writer adds) takes the
# exact registered values, so a new free-text field fails closed. Keys are never
# rewritten, so a masked value still sits behind its own name.
_JOB_STRUCTURAL_FIELDS = frozenset({
    'id', 'action', 'state', 'target_kind', 'target_id', 'source_id',
    'from_version', 'to_version', 'duration_ms',
    'created_at', 'started_at', 'completed_at', 'updated_at',
})


def scrub_job_secrets(job) -> bool:
    """Mask every registered credential and known shape in one Provider Hub
    job row, in place. Returns True when it masked something.

    The structural fields (``_JOB_STRUCTURAL_FIELDS``) take the credential
    shapes only: a password that reads "install" must not rewrite the action.
    Every other top-level field takes the exact registered values, everywhere
    inside it, including a config echo and the members of a tuple (rebuilt as a
    list, as the stored document would read back). Non-string values are left
    as they are.
    """
    if not isinstance(job, dict):
        return False
    changed = False

    def walk(node, in_text):
        nonlocal changed
        if isinstance(node, str):
            masked = scrub_text(node) if in_text else _mask_patterns(node)
            if masked != node:
                changed = True
            return masked
        if isinstance(node, dict):
            for key, value in list(node.items()):
                node[key] = walk(value, in_text)
            return node
        if isinstance(node, (list, tuple)):
            return [walk(value, in_text) for value in node]
        return node

    for key, value in list(job.items()):
        structural = isinstance(key, str) and key in _JOB_STRUCTURAL_FIELDS
        job[key] = walk(value, not structural)
    return changed


def scrub_persisted_secrets(state=None) -> bool:
    """Rewrite the secrets an earlier version left in Provider Hub's
    state.json, atomically, or leave the file untouched. Returns True when
    it rewrote.

    Runs from the boot AFTER the credential migration, so the containers
    are ciphertext by then and a value this boot registered can no longer be
    read out of a stored diagnostic. ``state`` is the document the boot
    already read; None loads it.
    """
    if state is None:
        try:
            from provider_hub.state import load_state
            state = load_state()
        except Exception as error:
            logger.warning("The Provider Hub state could not be read to scrub the "
                           "stored diagnostics: %s", type(error).__name__)
            return False
    if not scrub_state_secrets(state):
        return False
    try:
        from provider_hub.state import mutate_state
        mutate_state(lambda current: scrub_state_secrets(current))
    except Exception as error:
        logger.warning("The scrubbed Provider Hub state could not be written: %s",
                       type(error).__name__)
        return False
    return True
