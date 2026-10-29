# coding=utf-8

"""The session generation: one number that revokes every signed cookie at once.

A Flask session cookie is signed, not stored. Nothing on the server knows
which cookies are out there, so deleting the cookie in the browser on logout
revokes nothing: anyone holding a copy (or a request that was already in
flight when the logout ran, which Flask answers by signing the same session
again) keeps a valid session for the whole lifetime window.

The fix is a number the server owns. Each signed-in cookie carries the
generation it was issued under, and the session gate refuses a cookie whose
generation is not the current one. Logout, a password change and an auth-type
change advance the number, which makes every cookie issued before that moment
worthless in one step, with nothing to track per cookie.

Where it lives: `auth.session_generation` in the settings store, which is
already persisted in config.yaml and already in memory. A read is an in-process
attribute lookup, so the gate never touches the database or the disk, and
there is nothing to invalidate: advancing the number is the invalidation. No
migration is involved.

A stale cookie is only ever refused, never deleted by a response. A cookie
deletion cannot name the cookie it means, and the browser may hold a newer one
by the time an older response arrives: a request that left before the browser
signed in again, or before a password change re-stamped its own cookie, answers
afterwards. A deletion from that answer would sign the browser out. The gate
is what revokes; a stale cookie in the browser authenticates nobody, and the
next sign-in replaces it. Logout also sends no deletion: its response can
arrive after a fresh sign-in, whose cookie it cannot distinguish from the one
the request carried. Advancing the generation is enough to end the session.

Cookies issued before this existed carry no generation. They count as
generation 0 and are honoured only while the stored generation is still 0, so
an upgrade signs nobody out; the first revocation ends them with the rest.
"""

import logging
import threading
from contextlib import contextmanager
from typing import NamedTuple

from flask import has_request_context, session

# The key the generation is stored under inside the signed cookie.
GENERATION_KEY = 'gen'

# Serialises advances so two concurrent revocations land on two numbers. It also
# guards every read of the number: a reload of the settings from disk holds it
# from its first step to its last (never_going_backwards), so a reader never
# sees the file's older value in the middle of one. Reentrant because the
# holders read the number themselves.
_advance_lock = threading.RLock()

# The largest generation this process has handed out by an advance. It only
# grows, and it is what keeps a number from being issued twice: a refused save
# gives its advance back (withdraw_advance), and a cookie signed with that
# number during the save, by a sign-in under the new in-memory password, must
# stay refused rather than come back to life when the number is reached again.
# The gate compares a cookie with the current generation only. The next advance
# goes past this mark and the current number alike, so the current number can
# drop back after a withdraw but a number already issued is never issued again.
#
# It is per process and persisted nowhere: the file keeps the one generation
# key. After a restart the mark starts again, so a number withdrawn before the
# restart can be reached again by a later advance, and a cookie signed with it
# in the window of a refused save would be honoured then. That needs a sign-in
# inside a refused save, a restart, and an advance up to the same number, and
# it is accepted rather than adding a second persisted value.
_issued_high_water = 0


def current_generation():
    """The generation a cookie must carry to be honoured, or None if unreadable.

    None means the stored value is not an integer: nothing is honoured then,
    rather than guessing a number. The settings validator keeps the stored
    value an integer, so this is a backstop for a hand-edited file.

    Read under _advance_lock, so it waits out a reload of the settings from
    disk instead of reading what the reload has put there for the moment, which
    can be a number behind the one in force and a settings object that is half
    rebuilt. The lock is in memory and uncontended outside a reload: this is
    still no database and no disk.
    """
    from app.config import settings

    with _advance_lock:
        value = settings.auth.get('session_generation', 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _carries(cookie_generation, current):
    """True when a cookie's generation is the integer `current`.

    Equality alone is not enough: True == 1 and 1.0 == 1 in Python, so a cookie
    signed with the server's own key but carrying `true` would pass as
    generation 1. Anything that is not exactly an int fails closed.
    """
    return type(cookie_generation) is int and cookie_generation == current


def session_generation_is_current():
    """True when the open session's generation matches the stored one.

    A session with no generation is a cookie from before this existed and is
    generation 0.
    """
    current = current_generation()
    if current is None:
        return False
    return _carries(session.get(GENERATION_KEY, 0), current)


@contextmanager
def between_saves():
    """Hold still while a sign-in reads the login settings; yields the generation.

    Used around the read of the stored login that a sign-in is about to check
    credentials against, so that the login and the generation come from one
    state: the sign-in is stamped with the generation of the very login it was
    checked against, and not with whichever number is current once the (slow)
    password check is over.

    It is the config lock, which a settings save holds from its first change to
    its last step, including the reload that undoes a refused one. So a sign-in
    never reads a password a save has applied in memory and is about to give
    back: the proposed password is live in memory for the whole save, and a
    sign-in checked against it would be stamped with a number the refused save
    then restores. The cost is that a sign-in waits for a save in progress, as a
    logout does.
    """
    from discover.metadata import CONFIG_LOCK

    with CONFIG_LOCK:
        yield current_generation()


def stamp_session(generation):
    """Record `generation` in the open session, at sign-in. True if it is still the current one.

    The number stamped is the one read before the credentials were checked
    (between_saves), never the current one. If a password change, an auth-type
    change or a logout moved the generation while the check ran, the session is
    stamped with a number that is already dead, and False tells the caller to
    check again against the settings as they are now. Nothing is stamped when
    there is no readable number, and the answer is False.
    """
    if isinstance(generation, bool) or not isinstance(generation, int):
        return False
    session[GENERATION_KEY] = generation
    return current_generation() == generation


class Advance(NamedTuple):
    """One advance: the number it moved to, and the value it moved away from.

    `previous` is what the setting held at that instant, exactly as stored, and
    is what a withdrawal puts back. It is never worked out from `number`:
    advances skip numbers (see _issued_high_water), so "one less" is not where
    the advance began.
    """
    number: int
    previous: object


def _advance_locked():
    """The advance itself. The caller holds _advance_lock. Returns the Advance.

    The new number is past the current one and past everything issued before,
    so it is a number no cookie has ever carried.
    """
    global _issued_high_water
    from app.config import settings

    previous = settings.auth.get('session_generation', 0)
    new = max(current_generation() or 0, _issued_high_water) + 1
    settings.auth.session_generation = new
    _issued_high_water = new
    return Advance(new, previous)


def advance_session_generation():
    """Move to the next generation in memory and return the new number.

    The caller owns persisting it: the settings save writes config.yaml once
    for the whole save, and a rollback of that save reloads the file, which
    puts the number back with everything else.
    """
    with _advance_lock:
        return _advance_locked().number


def generation_for_restore(restored_generation):
    """Reserve a fresh number for a staged backup, without changing the live login.

    A backup carries both an older generation and the signing key, so putting
    it back unchanged would revive its old cookies. The restored file gets a
    number past its own value, the live value and every number issued in this
    process. Reserving also raises the high-water mark, even if the restore
    later fails, so a later advance cannot hand out the reserved number again.
    """
    global _issued_high_water

    if type(restored_generation) is not int or restored_generation < 0:
        raise ValueError('The backup session generation must be a non-negative integer')
    with _advance_lock:
        new = max(current_generation() or 0, _issued_high_water, restored_generation) + 1
        _issued_high_water = new
        return new


def advance_for_login_change():
    """Advance for a login change. Returns (session_was_in_force, Advance).

    The first value is True when the open request's session was signed in and
    belonged to the generation that was current immediately before this
    advance, which is the only session a password change may carry over to the
    new generation.
    Both are read under the advance lock, so a revocation that lands between
    them cannot slip through. A session that was already revoked while this
    request ran is stale at that point and must stay revoked.
    """
    from .session import SESSION_KEY

    with _advance_lock:
        was_in_force = (has_request_context() and session.get(SESSION_KEY) is True
                        and session_generation_is_current())
        return was_in_force, _advance_locked()


def withdraw_advance(advance):
    """Take back an advance that never reached the disk, if nothing moved since.

    A login-change save advances in memory before it writes. When the write is
    refused the save is rolled back, and so is its advance: nobody's password
    changed, so nobody should be signed out. The number goes back only while it
    is still the one this save made; if a logout advanced it further in the
    meantime, that revocation is real and stays. What goes back is the value the
    advance captured when it started, not a number worked out from the new one:
    two refused saves in a row skip a number between them, and one less than
    the second is the number a cookie signed during the first still carries. The
    number it takes back stays issued: the high-water mark keeps it, so the next
    advance skips it.
    """
    with _advance_lock:
        if current_generation() == advance.number:
            from app.config import settings

            settings.auth.session_generation = advance.previous


@contextmanager
def never_going_backwards():
    """Around a reload of the settings from disk: the generation only moves forward.

    The reload that undoes a refused save puts the file's value back, and the
    file can be behind this process: a logout whose write failed is in force in
    memory only. Handing that number back would give every cookie it revoked
    its life again. So the larger of the two survives.

    Two locks, in this order. The config lock is the one a revocation advances
    and writes under, so a reload lands entirely before or entirely after one.
    The advance lock, which every read of the number takes, is held for the whole
    block, so a request carrying a revoked cookie cannot read the file's older
    number while the reload is under way and be let in: restoring the larger
    number afterwards would not take that answer back. Readers wait for the
    reload instead.
    """
    from app.config import settings
    from discover.metadata import CONFIG_LOCK

    with CONFIG_LOCK, _advance_lock:
        before = current_generation()
        try:
            yield
        finally:
            after = current_generation()
            if before is not None and (after is None or after < before):
                settings.auth.session_generation = before


def revoke_all_sessions():
    """Advance the generation and persist it, so every outstanding cookie dies.

    The in-memory advance is what revokes, and it happens first and holds even
    when the write fails: a failed write is logged and the revocation stays in
    force for this process, surviving any reload of the settings, but the file
    still holds the old number, so a restart before a later settings save
    writes it brings the revoked cookies back. Advance and write happen under
    the config lock, so a refused save reloading the settings cannot land
    between the two.

    The same lock means a logout waits for any settings save in progress, which
    holds it for the whole save: a logout during a slow save blocks until that
    save ends. The revocation is not lost, only late.
    """
    from app.config import write_config
    from discover.metadata import CONFIG_LOCK

    with CONFIG_LOCK:
        new = advance_session_generation()
        try:
            if write_config() is not True:
                logging.error('Session generation %s is in force but could not be saved; '
                              'it is lost at the next restart unless a later settings save '
                              'writes it', new)
        except Exception:
            logging.exception('Session generation %s is in force but could not be saved; '
                              'it is lost at the next restart unless a later settings save '
                              'writes it', new)
    return new


def revoke_signed_in_sessions():
    """Logout: revoke everything, but only on the word of a signed-in session.

    A stranger, or a browser whose cookie is already revoked, posting logout
    must not sign everyone else out, nor cost a config write each time.
    """
    from .session import is_session_authenticated
    from discover.metadata import CONFIG_LOCK

    # The check and advance belong to one settings state. A logout waiting
    # for a password save must recheck its old cookie after the save finishes,
    # rather than revoke the replacement session that save just issued.
    with CONFIG_LOCK:
        if is_session_authenticated():
            revoke_all_sessions()


def keep_current_session_signed_in(was_in_force, advance):
    """Re-stamp the open session under the generation the change made, if it was in force.

    A password change revokes every other browser, but the one that made the
    change was signed in a moment ago and is the person who asked, so it keeps
    its session instead of being thrown at the login page. `was_in_force` and
    `advance` are what advance_for_login_change reported: a session that was
    already revoked when the change began is not renewed by it, and the stamp is
    the number this change made, so a revocation that lands afterwards leaves the
    session revoked rather than being stamped over.
    """
    if was_in_force and has_request_context():
        session[GENERATION_KEY] = advance.number


# Session keys that older releases stored. _pw_for_upgrade held the admin's
# plaintext password, and browsers that declined the upgrade still carry it.
LEGACY_UPGRADE_KEYS = ('_pw_for_upgrade', '_upgrade_token')


def session_cookie_must_expire(session_object):
    """True when the cookie opened for this request carried a legacy upgrade key.

    The response then deletes the session cookie instead of renewing it, unless
    the request itself wrote a new session, which replaces the cookie as usual.
    """
    return bool(getattr(session_object, 'expire_cookie_on_save', False))


def open_session_cleanup(session_object):
    """Called when a cookie is opened: normalise it or refuse it.

    Returns the session to use. A non-empty session from another generation
    is dropped whole, so nothing in it reaches a handler, but it is not marked
    modified: the response then sends no cookie at all. Marking it modified made
    the response delete the cookie, and a deletion cannot tell which cookie the
    browser holds by the time the response arrives. A request that left before
    a sign-in, or before a password change re-stamped the browser's own cookie,
    answers afterwards and would delete the newer cookie, signing out the
    browser that had just signed in. The stale cookie is worthless to anyone,
    because the gate refuses it, so leaving it in the browser costs nothing; the
    next sign-in replaces it.

    A cookie that carries a key from an older release (see LEGACY_UPGRADE_KEYS)
    is the exception. Its contents are emptied here, so nothing in it, the
    password included, reaches a handler or is written back, and it is marked so
    that the response expires the cookie: a browser that declined the upgrade
    must not keep that copy of the password. A legacy-key cookie signed with the
    current secret is expired whatever its age, because the open step in app.py
    reads an aged cookie for its keys too. A cookie signed with another secret
    cannot be verified and is left alone.

    A session with no generation is pre-upgrade; it is given an explicit 0
    here so a later stamp in the same request cannot mistake it for a new one.
    """
    if not session_object:
        return session_object
    if any(key in session_object for key in LEGACY_UPGRADE_KEYS):
        session_object.clear()
        session_object.expire_cookie_on_save = True
        return session_object
    current = current_generation()
    if current is None or not _carries(session_object.get(GENERATION_KEY, 0), current):
        session_object.clear()
        # clear() marks the session modified, which would turn the response into
        # a cookie deletion. The handler gets an empty session; the browser's
        # cookie is left as it is.
        session_object.modified = False
        return session_object
    if GENERATION_KEY not in session_object:
        session_object[GENERATION_KEY] = 0
    return session_object


def before_save_cleanup(session_object):
    """Called before a session is written to the cookie. Returns False to write nothing.

    A session created during this request (no generation yet) is stamped with
    the current one. A session whose generation went stale while the request
    ran, which is what a request in flight during a logout or a password change
    looks like, is not written at all: no renewal, and no deletion either.

    No deletion, because the browser may already hold a newer cookie: the
    request that changed the password re-stamped its own browser, a sign-in
    replaced the cookie, and this older response arrives after. A deletion header
    cannot say which cookie it means and would delete that one. The stale cookie
    cannot authenticate anyone, since the gate compares generations, so leaving
    it in the browser costs nothing.

    Empty sessions also write nothing, including logout. A fresh sign-in may
    have reached the browser while the logout response was still in flight,
    and a deletion would erase that newer cookie. The generation advance
    revokes the old cookie without a browser-side deletion.
    """
    if not session_object:
        return False
    current = current_generation()
    if current is None:
        return False
    if GENERATION_KEY not in session_object:
        session_object[GENERATION_KEY] = current
        return True
    return _carries(session_object[GENERATION_KEY], current)
