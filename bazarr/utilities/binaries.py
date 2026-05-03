# coding=utf-8

import os
import platform
import logging
import requests
import json
import hashlib
import stat

from whichcraft import which
from dogpile.cache import make_region

from utilities.locked_lru import LockedLRU

# Bounded thread-safe LRU + 1h TTL. Only used to memoise md5() and
# get_binaries_from_json() results across a few binary paths. In practice
# there are at most ~4 entries (ffmpeg, ffprobe, mediainfo, the JSON blob);
# maxsize=8 communicates that intent, with headroom for platform variants.
# The 1h TTL rotates the md5 cache often enough to pick up auto-updated
# ffmpeg/ffprobe binaries within a reasonable window. LockedLRU is used
# instead of bare LRUCache because get_binary() can be invoked from
# concurrent request threads via the video analyzer.
region = make_region().configure(
    'dogpile.cache.memory',
    arguments={'cache_dict': LockedLRU(maxsize=8)},
    expiration_time=3600,
)


class BinaryNotFound(Exception):
    pass


@region.cache_on_arguments()
def md5(fname):
    hash_md5 = hashlib.md5()
    with open(fname, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


@region.cache_on_arguments()
def get_binaries_from_json():
    try:
        binaries_json_file = os.path.realpath(os.path.join(os.path.dirname(__file__), 'binaries.json'))
        with open(binaries_json_file) as json_file:
            binaries_json = json.load(json_file)
    except OSError:
        logging.exception('BAZARR cannot access binaries.json')
        return []
    else:
        return binaries_json


def get_binary(name):
    installed_exe = which(name)

    if installed_exe and os.path.isfile(installed_exe):
        logging.debug(f'BAZARR returning this binary: {installed_exe}')  # noqa: G004
        return installed_exe
    else:
        logging.debug('BAZARR binary not found in path, searching for it...')
        binaries_dir = os.path.realpath(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'bin'))
        system = platform.system()
        machine = platform.machine()
        dir_name = name

        # deals with exceptions
        if platform.system() == "Windows":  # Windows
            machine = "i386"
            name = "%s.exe" % name
        elif platform.system() == "Darwin":  # MacOSX
            system = 'MacOSX'
        if name in ['ffprobe', 'ffprobe.exe']:
            dir_name = 'ffmpeg'

        exe_dir = os.path.abspath(os.path.join(binaries_dir, system, machine, dir_name))
        exe = os.path.abspath(os.path.join(exe_dir, name))

        binaries_json = get_binaries_from_json()
        binary = next((item for item in binaries_json if item['system'] == system and item['machine'] == machine and
                       item['directory'] == dir_name and item['name'] == name), None)
        if not binary:
            logging.debug('BAZARR binary not found in binaries.json')
            raise BinaryNotFound
        else:
            logging.debug(f'BAZARR found this in binaries.json: {binary}')  # noqa: G004

        if os.path.isfile(exe) and md5(exe) == binary['checksum']:
            logging.debug(f'BAZARR returning this existing and up-to-date binary: {exe}')  # noqa: G004
            return exe
        else:
            try:
                logging.debug(f'BAZARR creating directory tree for {exe_dir}')  # noqa: G004
                os.makedirs(exe_dir, exist_ok=True)
                logging.debug(f'BAZARR downloading {name} from {binary["url"]}')  # noqa: G004
                r = requests.get(binary['url'])
                logging.debug(f'BAZARR saving {name} to {exe_dir}')  # noqa: G004
                with open(exe, 'wb') as f:
                    f.write(r.content)
                if system != 'Windows':
                    logging.debug(f'BAZARR adding execute permission on {exe}')  # noqa: G004
                    st = os.stat(exe)
                    os.chmod(exe, st.st_mode | stat.S_IEXEC)
            except Exception:
                logging.exception(f'BAZARR unable to download {name} to {exe_dir}')  # noqa: G004
                raise BinaryNotFound
            else:
                logging.debug(f'BAZARR returning this new binary: {exe}')  # noqa: G004
                return exe
