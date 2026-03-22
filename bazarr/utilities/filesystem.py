# coding=utf-8

import os
import logging
import string

# System directories that should not be browsable (Linux/macOS)
# Note: /run is NOT blocked because /run/media is used for automounted
# removable media on many Linux distributions (GNOME, KDE, etc.)
_BLOCKED_PATHS = {
    '/proc', '/sys', '/dev', '/snap',
    '/boot', '/lost+found', '/swapfile',
    '/etc', '/root', '/tmp',
}


def _is_path_blocked(path):
    """Check if a path falls within a blocked system directory."""
    real = os.path.realpath(path)
    for blocked in _BLOCKED_PATHS:
        if real == blocked or real.startswith(blocked + os.sep):
            return True
    return False


def browse_bazarr_filesystem(path='#'):
    if path == '#' or path == '/' or path == '':
        if os.name == 'nt':
            dir_list = []
            for drive in string.ascii_uppercase:
                drive_letter = f'{drive}:\\'
                if os.path.exists(drive_letter):
                    dir_list.append(drive_letter)
        else:
            path = "/"
            dir_list = [f for f in os.listdir(path) if os.path.isdir(os.path.join(path, f))]
    else:
        if _is_path_blocked(path):
            logging.warning(f'Filesystem browse blocked for restricted path: {path}')
            return {'directories': [], 'parent': os.path.dirname(path)}
        dir_list = [f for f in os.listdir(path) if os.path.isdir(os.path.join(path, f))]

    data = []
    for item in dir_list:
        full_path = os.path.join(path, item, '')
        if _is_path_blocked(full_path):
            continue
        item = {
            "name": item,
            "path": full_path
        }
        data.append(item)

    parent = os.path.dirname(path)

    result = {'directories': sorted(data, key=lambda i: i['name'])}
    if path == '#':
        result.update({'parent': '#'})
    else:
        result.update({'parent': parent})

    return result
