# coding=utf-8

"""The password-confirmation gate in front of API actions that run code.

While form or basic login is configured, the API key alone must not add a
catalog source, change a source ref, install or upload a plugin, apply a
plugin update, change the post-processing command or the login settings, or
restore a backup: each of those runs code on the host or rewrites the whole
configuration. A caller who is signed in and has just confirmed the password
may; the API key by itself gets a refusal that names the missing step.

The gate lives here, at the API layer, and never inside the modules that do
the work. Startup auto-install, activation at boot and the restore that runs
at the next start call those modules directly, with no request and no user,
and must keep working. Listing, creating and deleting the things the gate
protects stays open.
"""

from functools import wraps

from flask import request

from app.auth import is_session_authenticated, password_is_confirmed
from app.config import settings
from utilities.helper import check_credentials

# The reason the refusal carries, so a caller holding only the API key can
# tell what is missing instead of guessing at a bare 403.
CONFIRM_PASSWORD_REASON = 'confirm_password'


def password_confirm_refusal():
    """The 403 that names the missing confirmation."""
    return {'code': CONFIRM_PASSWORD_REASON,
            'message': 'This action needs a signed-in user who has just confirmed the '
                       'password. Confirm your password, then try again.'}, 403


def password_confirmation_live():
    """True when this request's caller may run a gated action right now.

    With no login mode configured there is no password to confirm, so every
    caller is live: while login is off, the request-host check is the defence.
    Under form login the caller is the browser session a successful login
    established; under basic login it is the request's own basic credentials.
    Either way, the session must also carry a confirmation inside its window,
    which is the one thing the API key alone never holds.
    """
    auth_type = settings.auth.type
    if auth_type not in ('form', 'basic'):
        return True
    if auth_type == 'form':
        if not is_session_authenticated():
            return False
    else:
        credentials = request.authorization
        if not (credentials and check_credentials(credentials.username, credentials.password,
                                                  request, log_success=False)):
            return False
    return password_is_confirmed()


def require_password_confirmation(actual_method):
    """Route decorator: pass only for a signed-in caller with a live confirmation."""
    @wraps(actual_method)
    def wrapper(*args, **kwargs):
        if not password_confirmation_live():
            return password_confirm_refusal()
        return actual_method(*args, **kwargs)
    return wrapper
