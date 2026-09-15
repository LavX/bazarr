# coding=utf-8

"""The single source of truth for "is this browser session signed in?".

Every form-auth gate asks this function. It exists because the two gates used
to ask the question differently: one tested whether the session carried a
`logged_in` key, the other tested the key's value, and a failed login wrote the
key with the value False. A visitor who submitted one wrong password therefore
held a cookie that satisfied the weaker gate, which fronted the log download,
the backup download (config.yaml plus the database) and both cover-image
proxies. Two gates, one question, one answer.

The value is compared against True identity, not truthiness, so a future
refactor that stores something else under the key cannot quietly re-open the
hole.
"""

from flask import session

SESSION_KEY = 'logged_in'


def is_session_authenticated():
    """True only for a session established by a successful login."""
    return session.get(SESSION_KEY) is True


def establish_session():
    """Mark the current session as signed in.

    Marking it permanent is what puts PERMANENT_SESSION_LIFETIME to work: it
    gives the cookie an expiry and makes the signature itself expire, so a
    session cannot outlive the configured window. Without it Flask issues a
    browser-session cookie that never expires server-side.
    """
    session.permanent = True
    session[SESSION_KEY] = True


def clear_session():
    """Drop every trace of a signed-in session.

    Used on logout and after a rejected login. A rejected login must remove the
    key rather than set it to False: a stored False is a value some other gate
    may one day read as "present, therefore fine".
    """
    session.pop(SESSION_KEY, None)
