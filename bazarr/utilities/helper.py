# coding=utf-8

import os
import hmac
import logging
import hashlib

from charset_normalizer import detect
from bs4 import UnicodeDammit

from app.config import settings


def hash_password(pw):
    salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac('sha256', f"{pw}".encode('utf-8'), salt, 150000)
    return 'pbkdf2:' + salt.hex() + ':' + hashed.hex()


def _is_legacy_md5(stored_hash):
    return not stored_hash.startswith('pbkdf2:')


def _verify_password(pw, stored_hash):
    if stored_hash.startswith('pbkdf2:'):
        _, salt_hex, hash_hex = stored_hash.split(':', 2)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac('sha256', f"{pw}".encode('utf-8'), salt, 150000)
        return hmac.compare_digest(actual, expected)
    else:
        return hmac.compare_digest(
            hashlib.md5(f"{pw}".encode('utf-8')).hexdigest(),
            stored_hash
        )


def upgrade_password_hash(pw):
    new_hash = hash_password(pw)
    settings.auth.password = new_hash
    from app.config import write_config
    write_config()
    logging.info('Upgraded password hash from MD5 to PBKDF2-SHA256')


def check_credentials(user, pw, request, log_success=True):
    forwarded_for_ip_addr = request.environ.get('HTTP_X_FORWARDED_FOR')
    real_ip_addr = request.environ.get('HTTP_X_REAL_IP')
    ip_addr = forwarded_for_ip_addr or real_ip_addr or request.remote_addr
    username = settings.auth.username
    password = settings.auth.password
    if user == username and _verify_password(pw, password):
        if log_success:
            logging.info(f'Successful authentication from {ip_addr} for user {user}')
        return True
    else:
        logging.info(f'Failed authentication from {ip_addr} for user {user}')
        return False


def needs_password_upgrade():
    password = settings.auth.password
    return bool(password) and _is_legacy_md5(password)


def get_subtitle_destination_folder():
    fld_custom = str(settings.general.subfolder_custom).strip() if (settings.general.subfolder_custom and
                                                                    settings.general.subfolder != 'current') else None
    return fld_custom


def get_target_folder(file_path):
    subfolder = settings.general.subfolder
    fld_custom = str(settings.general.subfolder_custom).strip() \
        if settings.general.subfolder_custom else None

    if subfolder != "current" and fld_custom:
        # specific subFolder requested, create it if it doesn't exist
        fld_base = os.path.split(file_path)[0]

        if subfolder == "absolute":
            # absolute folder
            fld = fld_custom
        elif subfolder == "relative":
            fld = os.path.join(fld_base, fld_custom)
        else:
            fld = None

        fld = force_unicode(fld)

        if not os.path.isdir(fld):
            try:
                os.makedirs(fld)
            except Exception:
                logging.error(f'BAZARR is unable to create directory to save subtitles: {fld}')
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
