# coding=utf-8

"""A reason a user can act on for an AI Subtitle Translator refusal.

The service answers a refusal with FastAPI's ``{"detail": {"error": code,
"message": text}}`` envelope. Reporting only the status told a user whose
encryption key was missing or stale that the translator "refused the request
(HTTP 401)", which reads like a wrong OpenRouter API key and sends them to the
wrong setting. The codes below are the ones the service speaks; anything else
keeps the service's own message, sanitized, and the status code.

The unknown message is the service's own text, and a refusal that echoes the
request can carry a credential: the OpenRouter key Bazarr sent, or the ``enc:``
ciphertext it wrapped that key in. The reason is stored on the job, retained by
the editor and returned by the settings Test, so it is sanitized once here,
before any of those readers can hold it.

A blank encryption key is not a reason to refuse before sending: the translator's
documented encryption-off mode expects Bazarr to send the API key in the body and
no ``X-Auth-Token``. Against a default, encryption-on translator that request is
refused with ``unauthorized``, and the reason below says which key to look at.
"""

import re

from secret_store.scrub import MASK, scrub_text

# The translator keeps the key in this file inside its container, and its CLI
# writes a new one when the file is gone. Both are named so the message points
# at a command the user can run, not at the status code they just saw.
ENCRYPTION_KEY_REMEDY = (
    'Paste the key from data/encryption.key in the translator container into the '
    'encryption key setting, or regenerate it there with regenerate-key.'
)

REFUSAL_REASONS = {
    'unauthorized': (
        "The AI Subtitle Translator rejected Bazarr's authentication token, so the "
        f'encryption key is missing or wrong. {ENCRYPTION_KEY_REMEDY}'
    ),
    'decryption_failed': (
        'The AI Subtitle Translator could not decrypt the OpenRouter API key, so the '
        f'encryption key does not match. {ENCRYPTION_KEY_REMEDY}'
    ),
    'encryption_disabled': (
        'The AI Subtitle Translator has encryption disabled but Bazarr sent an encrypted '
        'OpenRouter API key. Clear the encryption key in Settings, or enable encryption in '
        'the translator.'
    ),
    'encryption_required': (
        'The AI Subtitle Translator requires encrypted OpenRouter API keys but Bazarr has '
        f'no encryption key. {ENCRYPTION_KEY_REMEDY}'
    ),
    'configuration_error': (
        'The AI Subtitle Translator has no OpenRouter API key. Set one in the translator '
        'service or in the Bazarr AI translator settings.'
    ),
    'queue_full': (
        'The AI Subtitle Translator is busy: its translation queue is full. Try again later.'
    ),
}


# The scrub module knows a real OpenRouter key by its exact shape. A service
# body is untrusted text, so any ``sk-or-v1-`` token is masked here even when
# its suffix is not the 64 hex characters a real key carries.
_OPENROUTER_TOKEN_RE = re.compile(r'\bsk-or-v1-[A-Za-z0-9_\-]{8,}')


def redact_service_text(message):
    """The service's own message with anything shaped like a credential masked.

    Ciphertext is covered by the scrub module's shape layer, both the at-rest
    ``enc:v1:`` form and the ``enc:`` form Bazarr submits, and a registered
    plaintext credential is masked by its value layer. The broad token shape
    below is the one a service can echo that neither layer knows.
    """
    text = scrub_text(str(message or '').strip())
    return _OPENROUTER_TOKEN_RE.sub(MASK, text)


def sidecar_error_detail(response):
    """The service's error code and message from its response body, when readable."""
    try:
        body = response.json()
    except (ValueError, AttributeError, TypeError):
        return None, None
    if not isinstance(body, dict):
        return None, None
    detail = body.get('detail')
    if isinstance(detail, dict):
        return detail.get('error'), detail.get('message')
    if isinstance(detail, str):
        return None, detail
    return body.get('error'), body.get('message')


def describe_sidecar_refusal(response):
    """The refusal in words a user can act on, for the status and body it came with."""
    status = getattr(response, 'status_code', None)
    code, message = sidecar_error_detail(response)
    if code in REFUSAL_REASONS:
        return REFUSAL_REASONS[code]
    if status in (404, 405):
        return (f'The AI Subtitle Translator does not offer the translation API at this URL '
                f'(HTTP {status}). Check the service URL, and update the translator if it is old.')
    text = redact_service_text(message)
    if text:
        return f'The AI Subtitle Translator refused the request (HTTP {status}): {text}'
    return f'The AI Subtitle Translator refused the request (HTTP {status}).'
