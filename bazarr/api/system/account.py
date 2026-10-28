# coding=utf-8

import gc
import time
import logging
import threading
from collections import OrderedDict

from flask import session, request
from flask_restx import Resource, Namespace, reqparse

from api.utils import authenticate
from app.auth import clear_authentication, establish_session, is_session_authenticated, mark_password_confirmed
from app.config import settings
from app.auth.generation import between_saves, revoke_signed_in_sessions, stamp_session
from utilities.helper import check_credentials, needs_password_upgrade, read_stored_login, upgrade_password_hash

api_ns_system_account = Namespace('System Account', description='Login or logout from Bazarr UI')

# In-memory login rate limiter: {ip: (fail_count, last_fail_time)}
_login_attempts = OrderedDict()
_login_lock = threading.Lock()
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes
_MAX_TRACKED_IPS = 10000
# How often one sign-in is checked again because the login settings or the
# session generation moved while it was being verified.
_SIGN_IN_ATTEMPTS = 3


def _get_client_ip():
    # Only use remote_addr by default to prevent IP spoofing via headers.
    # Reverse proxies should be configured to set X-Real-IP reliably.
    return request.remote_addr


def _is_rate_limited(ip):
    with _login_lock:
        if ip not in _login_attempts:
            return False
        count, last_time = _login_attempts[ip]
        if time.time() - last_time > _LOCKOUT_SECONDS:
            del _login_attempts[ip]
            return False
        return count >= _MAX_ATTEMPTS


def _record_failed_attempt(ip):
    with _login_lock:
        # Evict oldest entries if at capacity
        while len(_login_attempts) >= _MAX_TRACKED_IPS:
            _login_attempts.popitem(last=False)

        now = time.time()
        if ip in _login_attempts:
            count, last_time = _login_attempts[ip]
            if now - last_time > _LOCKOUT_SECONDS:
                _login_attempts[ip] = (1, now)
            else:
                _login_attempts[ip] = (count + 1, now)
        else:
            _login_attempts[ip] = (1, now)

        count = _login_attempts[ip][0]
        if count >= _MAX_ATTEMPTS:
            logging.warning(f'Login rate limit triggered for {ip} after {count} failed attempts')  # noqa: G004


def _clear_failed_attempts(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


@api_ns_system_account.hide
@api_ns_system_account.route('system/account')
class SystemAccount(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('action', type=str, required=True,
                                     help='Action from ["login", "logout"]')
    post_request_parser.add_argument('username', type=str, required=False, help='Bazarr username')
    post_request_parser.add_argument('password', type=str, required=False, help='Bazarr password')

    @api_ns_system_account.doc(parser=post_request_parser)
    @api_ns_system_account.response(204, 'Success')
    @api_ns_system_account.response(400, 'Unknown action')
    @api_ns_system_account.response(403, 'Authentication failed')
    @api_ns_system_account.response(406, 'Browser must be closed to invalidate basic authentication')
    @api_ns_system_account.response(429, 'Too many failed login attempts')
    @api_ns_system_account.response(500, 'Unknown authentication type define in config')
    def post(self):
        """Login or logout from Bazarr UI when using form login"""
        args = self.post_request_parser.parse_args()
        if settings.auth.type not in ['form', 'basic']:
            return 'Unknown authentication type define in config', 500

        action = args.get('action')
        if action == 'login':
            ip = _get_client_ip()
            if _is_rate_limited(ip):
                return 'Too many failed login attempts. Try again in 5 minutes.', 429

            username = args.get('username')
            password = args.get('password')
            # The password check is slow, and a password change, an auth-type
            # change or a logout can land while it runs. The generation and the
            # stored login are therefore read together before it starts, the
            # session is stamped with that generation, and if the generation
            # moved meanwhile the check is made again against the settings as
            # they are now. A sign-in is never stamped with a number it was not
            # checked under.
            for _ in range(_SIGN_IN_ATTEMPTS):
                with between_saves() as generation:
                    stored = read_stored_login()
                if not check_credentials(username, password, request, stored=stored):
                    _record_failed_attempt(ip)
                    # Remove the key rather than storing False: a stored False is a
                    # value another gate may read as "present, therefore signed in".
                    clear_authentication()
                    return 'Authentication failed', 403
                if establish_session(generation):
                    _clear_failed_attempts(ip)
                    if needs_password_upgrade():
                        # Re-hash here, where the plaintext already is. The old path
                        # parked it in the session and re-read it on a follow-up
                        # call, but the session is a signed, unencrypted cookie:
                        # "server-side only" was never true of it. A write failure
                        # must not reject credentials that were correct.
                        try:
                            upgrade_password_hash(password, expected_hash=stored[1])
                        except Exception:
                            logging.exception('Password hash upgrade failed; the login itself stands')
                    return '', 204
                if generation is None:
                    break  # no readable number: another look cannot change that
            # The login settings kept moving under every attempt, or no
            # generation could be read: nothing was verified against a state
            # that held still, so nobody is signed in. Not the caller's wrong
            # password, so it is not counted against them.
            clear_authentication()
            return 'Authentication failed', 403
        elif action == 'logout':
            if settings.auth.type == 'basic':
                return 'Browser must be closed to invalidate basic authentication', 406
            else:
                # Asked before the session is cleared, because clearing is what
                # it reads.
                revoke_signed_in_sessions()
                session.clear()
                gc.collect()
                return '', 204

        return 'Unknown action', 400


@api_ns_system_account.hide
@api_ns_system_account.route('system/account/confirm')
class SystemAccountConfirm(Resource):
    post_request_parser = reqparse.RequestParser()
    post_request_parser.add_argument('username', type=str, required=False, help='Bazarr username')
    post_request_parser.add_argument('password', type=str, required=False, help='Bazarr password')

    @authenticate
    @api_ns_system_account.doc(parser=post_request_parser)
    @api_ns_system_account.response(204, 'Success')
    @api_ns_system_account.response(401, 'Sign in first')
    @api_ns_system_account.response(403, 'Authentication failed')
    @api_ns_system_account.response(429, 'Too many failed attempts')
    @api_ns_system_account.response(500, 'Unknown authentication type define in config')
    def post(self):
        """Confirm the signed-in user's password for actions that run code"""
        if settings.auth.type not in ['form', 'basic']:
            return 'Unknown authentication type define in config', 500

        ip = _get_client_ip()
        if _is_rate_limited(ip):
            return 'Too many failed login attempts. Try again in 5 minutes.', 429

        args = self.post_request_parser.parse_args()

        # Take the login and generation together, as sign-in does. In basic
        # mode there may be no cookie yet, so the response must not invent a
        # confirmation under a generation newer than the password it checked.
        with between_saves() as generation:
            auth_type = settings.auth.type
            stored = read_stored_login()
            if auth_type == 'form' and not is_session_authenticated():
                return 'Sign in first', 401

        # Under basic login the request has to carry valid basic credentials
        # as well: a browser sends them on its own, and the confirmation on top
        # is what the API key alone never holds.
        if auth_type == 'basic':
            auth = request.authorization
            if not (auth and check_credentials(auth.username, auth.password, request,
                                               log_success=False, stored=stored)):
                _record_failed_attempt(ip)
                return 'Authentication failed', 403

        # The confirmation is for the one signed-in account, so a request that
        # names no username (the prompt could not read the settings) checks
        # the password against the configured one.
        username = args.get('username') or stored[0]
        if not check_credentials(username, args.get('password'), request, stored=stored):
            _record_failed_attempt(ip)
            return 'Authentication failed', 403

        with between_saves() as current:
            if (generation is None or current != generation or settings.auth.type != auth_type
                    or read_stored_login() != stored):
                return 'Authentication changed. Confirm your password again.', 403
            if not stamp_session(generation):
                return 'Authentication changed. Confirm your password again.', 403
            mark_password_confirmed()
        _clear_failed_attempts(ip)
        return '', 204
