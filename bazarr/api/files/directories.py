# coding=utf-8

import logging


def directory_rows(result, server):
    """File browser rows for the directories in a filesystem listing, or None.

    The listing is JSON another server answered, so its shape is not trusted:
    anything but an object carrying a list of directories gives None, and an
    entry without a string name and path is skipped rather than failing the
    whole listing. A None result is a request that already failed and was
    logged where it was made.
    """
    directories = result.get('directories') if isinstance(result, dict) else None
    if not isinstance(directories, list):
        if result is not None:
            logging.debug('BAZARR %s answered a filesystem listing with no list of directories', server)
        return None
    return [{'name': item['name'], 'children': True, 'path': item['path']}
            for item in directories
            if isinstance(item, dict) and isinstance(item.get('name'), str)
            and isinstance(item.get('path'), str)]
