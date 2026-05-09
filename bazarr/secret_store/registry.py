# coding=utf-8
"""Registry of every sensitive setting in config.yaml.

Two tiers:

USER_VISIBLE_SECRETS - credentials the user types in, copies, or rotates
    via the Settings page. They are encrypted at rest, but the
    `/api/system/settings` serializer decrypts before returning so the
    UI can show / let the user copy them. Examples: provider API keys,
    auth.apikey, compat_endpoint.token (which the user copies into
    Jellyfin OS plugin / VLSub).

USER_VISIBLE_SECRET_LISTS - same tier as above, but the value is a list
    of strings (e.g. translator.gemini_keys), each item handled
    individually.

SYSTEM_SECRETS - cryptographic primitives the user MUST NOT see and the
    backend MUST NOT leak. These are scoped to backend-internal use:
    flask_secret_key for session signing, jwt_secret / file_id_secret
    for compat token signing, the encryption keys themselves. The API
    serializer masks these with `***`.

The boundary matters because the user clarification was explicit:
"we should able to see the compat on the frontend" applies to
compat_endpoint.token (the API token they copy). The compat_endpoint's
internal jwt_secret / file_id_secret are NOT visible - they're signing
keys, never shown to the user.

Membership tests use exact-match dotted paths (e.g. "sonarr.apikey").
The dynaconf API serializer flattens nested settings to that form
before checking, so this stays simple.
"""

USER_VISIBLE_SECRETS = frozenset({
    # Bazarr's own admin login (username + password) and the API key that
    # the SPA uses on every authenticated request. Username is NOT
    # universally treated as secret, but encrypting it alongside the
    # password preserves account privacy in support bundles / leaked
    # config.yaml uploads.
    "auth.apikey",
    "auth.username",
    "auth.password",
    # Compat endpoint token (user copies this into VLSub / Jellyfin plugin)
    "compat_endpoint.token",
    # External webhook
    "general.external_webhook_password",
    # *arr API keys
    "sonarr.apikey",
    "radarr.apikey",
    # Plex (apikey + token; encryption_key stays in SYSTEM_SECRETS).
    # plex.username / plex.email come from the OAuth flow and are user
    # PII, encrypted at rest alongside the token.
    "plex.apikey",
    "plex.token",
    "plex.username",
    "plex.email",
    # Jellyfin
    "jellyfin.apikey",
    # Network proxy (full login pair).
    "proxy.username",
    "proxy.password",
    # External Postgres (full login pair).
    "postgresql.username",
    "postgresql.password",
    # AI Subtitle Translator. The "encryption_key" here is a USER-managed
    # secret - the Translator settings page exposes it as a Password field
    # ("Encryption Key (optional)") that the user enters and rotates. It
    # is NOT a master key that signs other secrets, so it's user-visible
    # at rest and decrypted before the API returns it.
    "translator.openrouter_api_key",
    "translator.openrouter_encryption_key",
    "translator.lingarr_token",
    # Subtitle providers - full login pairs (username + password) so a
    # leaked config.yaml doesn't expose which provider accounts the
    # operator owns even when the password is the only crackable bit.
    "opensubtitles.username",
    "opensubtitles.password",
    "opensubtitlescom.username",
    "opensubtitlescom.password",
    "addic7ed.username",
    "addic7ed.password",
    "legendasdivx.username",
    "legendasdivx.password",
    "legendasnet.username",
    "legendasnet.password",
    "pipocas.username",
    "pipocas.password",
    "xsubs.username",
    "xsubs.password",
    "deathbycaptcha.username",
    "deathbycaptcha.password",
    "napisy24.username",
    "napisy24.password",
    "titlovi.username",
    "titlovi.password",
    "titulky.username",
    "titulky.password",
    "karagarga.username",
    "karagarga.password",
    # karagarga forum login is a separate credential pair (no f_username
    # validator exists - the same `username` is reused).
    "karagarga.f_password",
    # ktuvit: separate field names but same pattern (login pair).
    "ktuvit.email",
    "ktuvit.hashed_password",
    # Private-tracker passkey (single credential, no separate password).
    "hdbits.username",
    "hdbits.passkey",
    # Session cookies - functionally equivalent to a long-lived auth
    # token once the provider login has happened.
    "addic7ed.cookies",
    "avistaz.cookies",
    "cinemaz.cookies",
    "turkcealtyaziorg.cookies",
    # Subtitle providers - tokens / keys
    "assrt.token",
    "betaseries.token",
    "jimaku.api_key",
    "subdl.api_key",
    "subsource.apikey",
    "subx.api_key",
    "subsro.api_key",
    "omdb.apikey",
})


USER_VISIBLE_SECRET_LISTS = frozenset({
    # AI Subtitle Translator's pool of Gemini API keys
    "translator.gemini_keys",
})


SYSTEM_SECRETS = frozenset({
    # Flask session signing
    "general.flask_secret_key",
    # Master encryption key - never round-trips through the API
    "general.secrets_encryption_key",
    # Compat endpoint signing keys (NOT the user-facing token)
    "compat_endpoint.jwt_secret",
    "compat_endpoint.file_id_secret",
    # Plex legacy per-namespace key (commit 4 unifies under
    # general.secrets_encryption_key; left here so the legacy read
    # path still has it masked in API responses during the migration
    # window)
    "plex.encryption_key",
})


def is_user_visible_secret(key: str) -> bool:
    """True iff `key` is a scalar credential that should be encrypted at
    rest and decrypted before the API returns it."""
    return key in USER_VISIBLE_SECRETS


def is_user_visible_secret_list(key: str) -> bool:
    """True iff `key` is a list of credentials, each item handled like a
    USER_VISIBLE_SECRET (encrypt at rest per-element, decrypt for API)."""
    return key in USER_VISIBLE_SECRET_LISTS


def is_system_secret(key: str) -> bool:
    """True iff `key` is a backend-only cryptographic primitive that
    must be masked by the API serializer."""
    return key in SYSTEM_SECRETS
