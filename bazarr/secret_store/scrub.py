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
import sys
from threading import Lock, RLock, local
from typing import Any, Iterable, Mapping

from .crypto import decrypt_secret
from .registry import (SYSTEM_SECRETS, USER_VISIBLE_SECRET_LISTS, at_rest_secret_paths,
                       installation_secret_fields)

logger = logging.getLogger(__name__)

# What a masked credential reads as. The settings serializer masks the same
# way, so a value masked here and a value withheld there are recognisably
# both "a secret was configured here".
MASK = '***'

# A credential of this many characters or more is masked wherever its text
# appears. Exact masking replaces the value as a substring, so a password of
# "test" would redact every "test" in logs and job text. A shorter credential
# is still registered, and masked only where it stands as a whole token in a
# context that names a value: after "=" or ":" (key=value pairs, JSON and YAML
# values, query parameters, Authorization headers), as a quoted string, and as
# the user or password of a URL. "abc" is masked in "password=abc" and in
# '"pass": "abc"', and left alone in "abcdef" or in a sentence. The patterns
# below (bearer tokens, Google and OpenRouter keys, ciphertext) match by shape
# and are not affected.
MIN_CREDENTIAL_LENGTH = 6


def mask_credential_refusal(path: str, submitted: Any, stored: Any = None) -> str | None:
    """Why a submitted credential is refused because it is the mask text, or None.

    ``path`` is the ``section.field`` the value is stored under and is named in
    the message. The settings read shows some credentials as the mask, so a
    client can send the mask back meaning "keep what is stored". Saved as a
    value, it would replace the stored credential with three asterisks, so it
    is refused. A mask equal to the stored value is not a change and passes, and
    so does an empty value, which is a clear. Length is not checked: a short
    credential is allowed, and the scrub layer masks it as a whole token.
    """
    if submitted is None or str(submitted).strip() != MASK:
        return None
    if stored is not None and str(stored).strip() == MASK:
        return None
    return f'{path} cannot be {MASK}: that text stands for a hidden value. Enter the value itself'

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

# The exact credential values currently masked, with the regex that masks the
# long ones, the set of short ones scanned for in context and the long ones
# again as a tuple for a cheap look before the regex runs, as one immutable
# tuple. Published by a single assignment in register_secret_values: a reader
# that caught the new set with the old regex would return the credential
# unchanged, which is exactly the leak this layer exists to prevent.
_VALUE_STATE: 'tuple[frozenset[str], re.Pattern[str] | None, frozenset[str], tuple[str, ...]]' = (
    frozenset(), None, frozenset(), ())

# What the last settings and Provider Hub collection found, what the last
# database collection found (the instance keys that live only in the database),
# the keys writers added themselves since boot, and the declared Provider Hub
# secrets the state store reports. A refresh replaces only its own set and
# registers the union of all four, so none of them drops another.
# A key added while a refresh was collecting is never lost: a database refresh
# drops only the added keys that were there before it started collecting.
_COLLECTED: frozenset[str] = frozenset()
_DATABASE_VALUES: frozenset[str] = frozenset()
# The added keys map to the order they were added in, so a database refresh can
# tell the ones added before it started collecting (which it may drop if the
# database no longer holds them) from the ones added during it (which it must
# keep). The mapping is replaced, never changed in place.
_ADDED_VALUES: 'dict[str, int]' = {}
_ADDED_SEQUENCE = 0
# The declared Provider Hub secrets the state store reports as it writes a state
# document. No refresh replaces or prunes them: they stay until clear_secret_values.
_DECLARED_VALUES: frozenset[str] = frozenset()


def _registered_union() -> frozenset[str]:
    """Every credential the sets above hold. Called with the publish lock held."""
    return _COLLECTED | _DATABASE_VALUES | _ADDED_VALUES.keys() | _DECLARED_VALUES

# Publishing the union of the sets above. A leaf lock: whoever holds it only
# builds a regex from values already in memory and takes no other lock, so it can
# be taken while holding any other lock without a cycle. All the sets are read
# inside it.
_publish_lock = Lock()


def _publish() -> int:
    with _publish_lock:
        return register_secret_values(_registered_union())


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

    Returns how many values are registered. A value that is already just the
    mask and one that is ciphertext are skipped: neither is a plaintext
    credential a leak could carry. A value shorter than
    ``MIN_CREDENTIAL_LENGTH`` is registered too, but it is masked only as a whole
    token in the contexts described above, so ordinary text that contains the
    same letters stays readable.

    The set, its regex and the short values are published together, in one
    assignment, so no reader can see the new values behind the old regex.
    """
    global _VALUE_STATE
    cleaned = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text != MASK and not text.startswith('enc:v1:'):
            cleaned.add(text)
    registered = frozenset(cleaned)
    # Longest first: a credential that contains another is masked whole
    # rather than in pieces.
    # A log line can quote a credential through ``%r``, which escapes quotes,
    # backslashes and non-printable characters, so that spelling is masked too.
    # It is derived here, for every registration path, and the value set itself
    # stays the credentials. A spelling is long or short by its own length, so
    # the escaped form of a short credential is masked as a whole token like the
    # credential is.
    spellings = set(registered)
    for value in registered:
        quoted = repr(value)[1:-1]
        if quoted:
            spellings.add(quoted)
    long_values = sorted((value for value in spellings if len(value) >= MIN_CREDENTIAL_LENGTH),
                         key=len, reverse=True)
    short_values = frozenset(value for value in spellings if len(value) < MIN_CREDENTIAL_LENGTH)
    regex = re.compile('|'.join(re.escape(value) for value in long_values)) if long_values else None
    _VALUE_STATE = (registered, regex, short_values, tuple(long_values))
    return len(registered)


# Where a short credential may stand. After "=" or ":" (key=value pairs, JSON
# and YAML values, query parameters, headers), optionally followed by spaces and
# an opening quote, plain or escaped as in JSON that was itself put in a string
# (once, or twice when that was put in a string again, any run of backslashes);
# or after an authorization scheme name, whatever its case; or as an item of a
# YAML block list, one or more "-" at the start of a line after any
# indentation, or of a YAML flow list, right after its "[" or a ",". As a quoted string.
# As the user of a URL, right after "://". A standalone quote is matched from the
# start of its run of backslashes, so a long run is scanned once and not again from
# every position in it. The quote is its own group so that a credential that
# begins with a quote can be found starting at it.
_SHORT_CONTEXT_RE = re.compile(
    r'(?P<after>(?P<pre>(?:[=:]|\b(?i:basic|bearer|digest|token)\s+)\s*|(?m:^)[ \t]*(?:-[ \t]+)+)(?:\\*["\'])?)'
    r'|(?P<quote>(?<!\\)\\*["\'])'
    r'|(?P<user>(?<=://))'
)

# What may follow a short credential for it to be a whole token: whitespace and
# control characters, quotes and brackets, and the separators of a query string,
# a URL, a list or a pipe. A "." or a ":" counts only when no word character
# follows it, so "abc." ends a sentence and "abc.example" and "10:30:45" do not.
_SHORT_END_CHARS = frozenset('"\'&;,@()[]{}<>%+=!?#|/\\$^')
_SHORT_MAX_LENGTH = MIN_CREDENTIAL_LENGTH - 1

# After the user of a URL: a colon starts a port or a password, and only a
# password is followed by an at sign before the path or the next space. The run
# it scans holds no "/", so it cannot contain another "://" and the scans of one
# text never overlap: the total work stays linear without a fixed limit.
_USERINFO_TAIL_RE = re.compile(r'[^@/\s]*@')


def _ends_token(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    character = text[index]
    if character.isspace() or ord(character) < 32 or ord(character) == 127:
        return True
    if character in '.:':
        return index + 1 >= len(text) or not (text[index + 1].isalnum() or text[index + 1] == '_')
    return character in _SHORT_END_CHARS


def _ends_url_user(text: str, index: int) -> bool:
    if index >= len(text):
        return False
    if text[index] == '@':
        return True
    return text[index] == ':' and _USERINFO_TAIL_RE.match(text, index + 1) is not None


def _short_ends(text: str, start: int, values: frozenset[str]):
    """The ends of the short credentials that begin at ``start``, longest first.

    At most ``_SHORT_MAX_LENGTH`` slices are looked up in the set, so the cost
    does not depend on how many credentials are registered."""
    for length in range(min(_SHORT_MAX_LENGTH, len(text) - start), 0, -1):
        if text[start:start + length] in values:
            yield start + length


# The delimiters that open, split and close a YAML flow list ("key: [a, b]"). A
# "[" counts as the start of one only after "=" or ":", so a bracket in prose or
# in an array index does not make a comma an item separator.
_FLOW_TOKEN_RE = re.compile(r'(?P<open>[=:]\s*\[)|(?P<bracket>\[)|(?P<close>\])|(?P<comma>,)')
_BLANKS = ' \t\r\n'


# A quoted flow item to its closing quote, on the same line. A backslash escapes
# the character after it, as in JSON, and a quote doubled inside single quotes
# is YAML's own escape.
_QUOTED_ITEM_RES = {
    '"': re.compile(r'"(?:[^"\\\n]|\\.)*"'),
    "'": re.compile(r"'(?:[^'\\\n]|''|\\.)*'"),
}


# What may follow the closing quote of a flow item: blanks, then the comma or the
# bracket that ends it. A quote that is followed by anything else did not close
# an item, so nothing after it is skipped.
_AFTER_ITEM_RE = re.compile(r'[ \t\r\n]*[,\]]')


def _flow_item_starts(text: str) -> list[int]:
    """The position where each item of every YAML flow list in the text starts,
    after the opening bracket or a comma and the blanks beside it. One pass over
    the brackets and commas, with a stack so that a comma inside a nested
    bracket does not count for the list around it. A bracket or a comma inside a
    quoted item is part of that item, not structure, so a quoted item is skipped
    whole."""
    starts = []
    stack: list[bool] = []
    skip_until = 0
    for match in _FLOW_TOKEN_RE.finditer(text):
        if match.start() < skip_until:
            continue
        if match.group('close') is not None:
            if stack:
                stack.pop()
            continue
        if match.group('open') is not None:
            stack.append(True)
        elif match.group('bracket') is not None:
            # A list inside a flow list is a flow list; any other bracket is not.
            stack.append(bool(stack and stack[-1]))
        if not (stack and stack[-1]):
            continue
        position = match.end()
        while position < len(text) and text[position] in _BLANKS:
            position += 1
        starts.append(position)
        if position < len(text) and text[position] in _QUOTED_ITEM_RES:
            quoted = _QUOTED_ITEM_RES[text[position]].match(text, position)
            if quoted is not None and _AFTER_ITEM_RE.match(text, quoted.end()):
                skip_until = quoted.end()
    return starts


def _short_spans(text: str, values: frozenset[str]) -> list[tuple[int, int]]:
    """The spans of the text where a short credential stands as a whole token in
    one of the contexts above, in order and without overlap.

    One pass over the contexts, each checked with a few set lookups, so the work
    is linear in the text whatever the number of registered values. A span is the
    credential alone: the context that named it (the key, the quotes, the scheme)
    is not in it. Where an opening quote follows the context, a credential is
    looked for both from the quote and after it, because a credential may itself
    begin with a quote."""
    if not values:
        return []
    spans = []
    search_from = 0
    while True:
        match = _SHORT_CONTEXT_RE.search(text, search_from)
        if match is None:
            break
        if match.group('after') is not None:
            starts = [match.end('pre'), match.end()] if match.end('pre') != match.end() else [match.end()]
            closes = _ends_token
        elif match.group('user') is not None:
            starts = [match.end()]
            closes = _ends_url_user
        else:
            quote = match.group('quote')
            starts = [match.end()]
            closes = lambda t, i, quote=quote: t.startswith(quote, i)  # noqa: E731
        found = None
        for start in starts:
            for end in _short_ends(text, start, values):
                if closes(text, end):
                    found = (start, end)
                    break
            if found:
                break
        search_from = match.start() + 1
        if found is not None:
            spans.append(found)
    if '[' in text:
        for start in _flow_item_starts(text):
            quoted = start < len(text) and text[start] in '"\''
            for begin in ([start, start + 1] if quoted else [start]):
                end = next((end for end in _short_ends(text, begin, values) if _ends_token(text, end)), None)
                if end is not None:
                    spans.append((begin, end))
                    break
    # Searching resumes just after each context, so one credential can be found
    # from two contexts and the span of one can lie where the context of the next
    # begins ("Authorization: Basic abc" with both "Basic" and "abc" registered).
    # Sorted, with a span that overlaps the one before it joined to it.
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The spans in order, with any that overlap joined into one that runs to
    the furthest end, so no part of a match is left readable."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def clear_secret_values() -> None:
    """Forget every registered value, so tests and a torn-down boot leave no
    stale credential masked."""
    global _COLLECTED, _DATABASE_VALUES, _ADDED_VALUES, _DECLARED_VALUES
    _COLLECTED = frozenset()
    _DATABASE_VALUES = frozenset()
    _ADDED_VALUES = {}
    _DECLARED_VALUES = frozenset()
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

    _values, regex, short_values, long_values = _VALUE_STATE
    # A substring search per long value is far cheaper than running the
    # alternation at every position of text that holds none of them.
    long_spans = []
    if regex is not None and any(value in text for value in long_values):
        long_spans = [match.span() for match in regex.finditer(text)]
    # Both sets are found on the text as it stands, so masking a long value
    # cannot take away the context a short one needs ("Bearer" registered as a
    # long value must not turn "Authorization: Bearer abc" into "*** abc"). Where
    # a long and a short match overlap they are joined, then everything is
    # replaced in one pass.
    short_spans = _short_spans(text, short_values) if short_values else []
    if not long_spans and not short_spans:
        return text
    pieces = []
    position = 0
    for start, end in _merge_spans(long_spans + short_spans):
        pieces.append(text[position:start])
        pieces.append(MASK)
        position = end
    pieces.append(text[position:])
    return ''.join(pieces)


def scrub_leaf(text: str) -> str:
    """scrub_text for a string that stands alone as a value in a structure.

    A leaf of a dict or list is scrubbed without the key beside it, so a short
    credential has no context to be found in: {"api_key": "abc"} holds the bare
    string "abc". A leaf whose stripped text is exactly a registered credential
    is that credential, so it is masked whole, short or long. Anything else
    takes scrub_text.
    """
    if not isinstance(text, str) or not text:
        return text
    if text.strip() in _VALUE_STATE[0]:
        return MASK
    return scrub_text(text)


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
        return scrub_leaf(value)
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


def collect_translator_secret_values(settings_obj) -> set[str]:
    """Every decrypted translator secret, while the translation runtime is on.

    Empty with the switch off, and before the database module has finished
    loading (the first refresh runs while the settings module is still being
    imported). With the switch off this reads one setting and imports nothing
    from the translation modules. A database error raises, so the caller keeps
    the set it already registered instead of publishing one without these.
    """
    if _read_setting(settings_obj, 'translator', 'runtime_v2') is not True:
        return set()
    database_module = sys.modules.get('app.database')
    if database_module is None or not hasattr(database_module, 'init_db'):
        return set()
    from subtitles.tools.translate.runtime import runtime_v2_enabled
    if not runtime_v2_enabled():
        return set()
    from subtitles.tools.translate.runtime.store import collect_secret_values
    return collect_secret_values()


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
    user just entered is masked in the same process that stores it. The
    instance keys the database holds are registered by
    refresh_database_secret_values, and this keeps what it found. Never
    raises: a read that fails leaves the previous set in place, because
    refusing a boot or a save over the scrub set would be worse than a
    credential that is not yet registered.

    Refreshes are serialized, so a collection that started before a settings
    write cannot publish its older set after the write's own refresh. A
    refresh that re-enters itself on the same thread (a settings write
    triggered from a decryption inside one) answers with the current set.
    """
    global _COLLECTED
    with _refresh_lock:
        if getattr(_refresh_local, 'depth', 0):
            return len(_VALUE_STATE[0])
        _refresh_local.depth = 1
        try:
            if settings_obj is None:
                from app.config import settings as settings_obj
            values = collect_settings_secret_values(settings_obj)
            values |= collect_hub_secret_values(state)
            values |= collect_translator_secret_values(settings_obj)
        except Exception as error:
            logger.warning("The credential values to scrub could not be collected: %s",
                           type(error).__name__)
            return len(_VALUE_STATE[0])
        finally:
            _refresh_local.depth = 0
        _COLLECTED = frozenset(values)
        return _publish()


def collect_database_secret_values(session) -> set[str]:
    """Every instance API key the database holds, decrypted: the media server
    destinations (Emby, Jellyfin, Plex, Silo) and the arr instances (Sonarr,
    Radarr, Sportarr), the default ones and the rest. Only the default Sonarr
    and Radarr instances mirror their key into the settings, so every other key
    is in the database alone. A key that will not decrypt is skipped."""
    from sqlalchemy import select

    from app.database import TableArrInstances, TableMediaServerInstances

    values: set[str] = set()
    for model in (TableMediaServerInstances, TableArrInstances):
        for stored in session.execute(select(model.api_key)).scalars().all():
            if not isinstance(stored, str) or not stored:
                continue
            try:
                plaintext = decrypt_secret(stored)
            except Exception:
                continue
            if isinstance(plaintext, str) and plaintext:
                values.add(plaintext)
    return values


def add_database_secret_values(values: Iterable[Any]) -> int:
    """Register instance keys the caller already holds, next to the ones the
    database collection found, without reading the database.

    For a writer that has just committed a key and may be holding other locks
    (the account, config and destination locks): it takes only the leaf publish
    lock, so it cannot wait on a refresh that is waiting on those. A refresh
    that was collecting while this ran cannot drop the keys, and one that starts
    later drops them if the database no longer holds them. Never raises: masking is best effort, and a write
    that has committed must not fail over it."""
    global _ADDED_VALUES, _ADDED_SEQUENCE
    try:
        added = frozenset(value.strip() for value in values or () if isinstance(value, str) and value.strip())
        if not added:
            return len(_VALUE_STATE[0])
        with _publish_lock:
            _ADDED_SEQUENCE += 1
            _ADDED_VALUES = {**_ADDED_VALUES, **dict.fromkeys(added, _ADDED_SEQUENCE)}
            return register_secret_values(_registered_union())
    except Exception as error:
        logger.warning("The instance keys to scrub could not be registered: %s", type(error).__name__)
        return len(_VALUE_STATE[0])


def add_declared_secret_values(values: Iterable[Any]) -> int:
    """Register the declared Provider Hub secrets of a state document the state
    store is writing, next to everything else registered.

    Takes only the leaf publish lock, like add_database_secret_values, so it can
    be called while a refresh is collecting without waiting on it and without
    being undone by it: no refresh replaces or prunes these. Never raises."""
    global _DECLARED_VALUES
    try:
        declared = frozenset(value.strip() for value in values or () if isinstance(value, str) and value.strip())
        if not declared:
            return len(_VALUE_STATE[0])
        with _publish_lock:
            _DECLARED_VALUES = _DECLARED_VALUES | declared
            return register_secret_values(_registered_union())
    except Exception as error:
        logger.warning("The declared secrets to scrub could not be registered: %s", type(error).__name__)
        return len(_VALUE_STATE[0])


def refresh_database_secret_values(session=None) -> int:
    """Register the instance keys held in the database, next to the values the
    settings and the Provider Hub state gave.

    Called at boot once the database is ready, and after an instance is created
    or changed, so a key entered there is masked in the same process that stores
    it. ``session`` is the one the caller is writing through; None uses the
    shared one. Never raises: a read that fails leaves the previous set in place.
    """
    global _DATABASE_VALUES, _ADDED_VALUES
    with _refresh_lock:
        with _publish_lock:
            started = _ADDED_SEQUENCE
        try:
            if session is None:
                from app.database import database as session
            values = collect_database_secret_values(session)
        except Exception as error:
            logger.warning("The instance keys to scrub could not be collected: %s", type(error).__name__)
            return len(_VALUE_STATE[0])
        with _publish_lock:
            _DATABASE_VALUES = frozenset(values)
            # What the database holds is now in the set above, so the keys added
            # before this collection started are done with. A key added while it
            # was reading may not have been visible to it, and stays.
            _ADDED_VALUES = {value: order for value, order in _ADDED_VALUES.items() if order > started}
            return register_secret_values(_registered_union())


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
                    masked = scrub_leaf(value) if text_field else _mask_patterns(value)
                    if masked != value:
                        node[key] = masked
                        changed = True
                elif isinstance(value, (dict, list)):
                    walk(value, text_field)
        else:
            for index, value in enumerate(node):
                if isinstance(value, str):
                    masked = scrub_leaf(value) if in_text else _mask_patterns(value)
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
            masked = scrub_leaf(node) if in_text else _mask_patterns(node)
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


def _sync_state_file_copy() -> None:
    """Make the state.json copy match the scrubbed database, best effort."""
    try:
        from provider_hub.state import sync_snapshot
        sync_snapshot()
    except Exception as error:
        logger.warning("The Provider Hub state file could not be refreshed after the "
                       "scrub: %s", type(error).__name__)


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
        _sync_state_file_copy()
        return False
    try:
        from provider_hub.state import mutate_state
        mutate_state(lambda current: scrub_state_secrets(current))
        _sync_state_file_copy()
    except Exception as error:
        logger.warning("The scrubbed Provider Hub state could not be written: %s",
                       type(error).__name__)
        return False
    return True
