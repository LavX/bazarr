# coding=utf-8

import os
import hmac
import logging
import hashlib

from charset_normalizer import detect
from bs4 import UnicodeDammit

from app.config import settings


PBKDF2_ITERATIONS = 600_000


def hash_password(pw):
    salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac('sha256', f"{pw}".encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return 'pbkdf2:' + salt.hex() + ':' + hashed.hex()


def _is_legacy_md5(stored_hash):
    return not stored_hash.startswith('pbkdf2:')


def _verify_password(pw, stored_hash):
    if not stored_hash:
        # An empty stored hash authenticates nothing: the boot secret pass
        # withholds a credential that will not decrypt as an empty string,
        # and an empty auth.password must never turn into a login that
        # accepts the empty password.
        return False
    if stored_hash.startswith('pbkdf2:'):
        try:
            _, salt_hex, hash_hex = stored_hash.split(':', 2)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
        except (ValueError, TypeError):
            logging.error('Corrupted PBKDF2 password hash in config. Re-set your password in settings.')
            return False
        actual = hashlib.pbkdf2_hmac('sha256', f"{pw}".encode('utf-8'), salt, PBKDF2_ITERATIONS)
        return hmac.compare_digest(actual, expected)
    else:
        return hmac.compare_digest(
            hashlib.md5(f"{pw}".encode('utf-8')).hexdigest(),
            stored_hash
        )


def upgrade_password_hash(pw, expected_hash=None):
    """Store a PBKDF2 hash of `pw` in place of the legacy hash it was checked against.

    `expected_hash` is the stored hash the password was verified against. If the
    password was changed since, the stored hash is no longer that one and the
    upgrade stands down: it would otherwise write a hash of the OLD password over
    the new one. Returns whether it upgraded.
    """
    from discover.metadata import CONFIG_LOCK

    new_hash = hash_password(pw)
    with CONFIG_LOCK:
        old_hash = settings.auth.password
        if expected_hash is not None and old_hash != expected_hash:
            logging.info('Password hash upgrade skipped: the password changed since it was checked')
            return False
        settings.auth.password = new_hash
        try:
            from app.config import write_config
            write_config()
            logging.info('Upgraded password hash from MD5 to PBKDF2-SHA256')
        except Exception:
            settings.auth.password = old_hash
            logging.exception('Failed to persist password hash upgrade, reverted to previous hash')
            raise
    return True


def read_stored_login():
    """The stored (username, password hash) pair a sign-in is checked against."""
    return settings.auth.username, settings.auth.password


def check_credentials(user, pw, request, log_success=True, stored=None):
    """True when `user` and `pw` match the stored login.

    `stored` is a (username, password hash) pair read earlier. A sign-in passes
    the pair it took together with the session generation, so what is checked is
    exactly the login that generation belongs to. Without it the live settings
    are read.
    """
    forwarded_for_ip_addr = request.environ.get('HTTP_X_FORWARDED_FOR')
    real_ip_addr = request.environ.get('HTTP_X_REAL_IP')
    ip_addr = forwarded_for_ip_addr or real_ip_addr or request.remote_addr
    username, password = stored if stored is not None else read_stored_login()
    if user == username and _verify_password(pw, password):
        if log_success:
            logging.info(f'Successful authentication from {ip_addr} for user {user}')  # noqa: G004
        return True
    else:
        logging.info(f'Failed authentication from {ip_addr} for user {user}')  # noqa: G004
        return False


def needs_password_upgrade():
    password = settings.auth.password
    return bool(password) and _is_legacy_md5(password)


def get_subtitle_destination_folder():
    fld_custom = str(settings.general.subfolder_custom).strip() if (settings.general.subfolder_custom and
                                                                    settings.general.subfolder != 'current') else None
    return fld_custom


def get_target_folder(file_path, create=True):
    subfolder = settings.general.subfolder
    fld_custom = str(settings.general.subfolder_custom).strip() \
        if settings.general.subfolder_custom else None

    if subfolder != "current" and fld_custom:
        # specific subFolder requested, create it if it doesn't exist
        # (create=False for read-only callers that only need the location)
        fld_base = os.path.split(file_path)[0]

        if subfolder == "absolute":
            # absolute folder
            fld = fld_custom
        elif subfolder == "relative":
            fld = os.path.join(fld_base, fld_custom)
        else:
            fld = None

        fld = force_unicode(fld)

        if create and not os.path.isdir(fld):
            try:
                os.makedirs(fld)
            except Exception:
                logging.error(f'BAZARR is unable to create directory to save subtitles: {fld}')  # noqa: G004
                fld = None
    else:
        fld = None

    return fld


def force_unicode(s):
    """
    Ensure a string is unicode, not encoded; used for enforcing file paths to be unicode upon saving a subtitle,
    to prevent encoding issues when saving a subtitle to a non-ascii path.
    :param s: string
    :return: unicode string
    """
    if not isinstance(s, str):
        try:
            s = s.decode("utf-8")
        except UnicodeDecodeError:
            t = detect(s)['encoding']
            try:
                s = s.decode(t)
            except UnicodeDecodeError:
                s = UnicodeDammit(s).unicode_markup
    return s
