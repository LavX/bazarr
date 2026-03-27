# coding=utf-8

import os
import hashlib
import requests
import logging
import json
import pretty

from datetime import datetime
from operator import itemgetter

from app.database import TableAnnouncements, database, insert, select

from app.get_args import args
from app.jobs_queue import jobs_queue


# Announcements as receive by browser must be in the form of a list of dicts converted to JSON
# [
#     {
#         'text': 'some text',
#         'link': 'http://to.somewhere.net',
#         'hash': '',
#         'dismissible': True,
#         'timestamp': 1676236978,
#         'enabled': True,
#     },
# ]


def parse_announcement_dict(announcement_dict):
    announcement_dict['timestamp'] = pretty.date(announcement_dict['timestamp'])
    announcement_dict['link'] = announcement_dict.get('link', '')
    announcement_dict['dismissible'] = announcement_dict.get('dismissible', True)
    announcement_dict['enabled'] = announcement_dict.get('enabled', True)
    announcement_dict['hash'] = hashlib.sha256(announcement_dict['text'].encode('UTF8')).hexdigest()

    return announcement_dict


def get_announcements_to_file(job_id=None, startup=False):
    if not startup and not job_id:
        jobs_queue.add_job_from_function("Updating Announcements File", is_progress=False)
        return

    try:
        r = requests.get(
            url="https://cdn.jsdelivr.net/gh/LavX/bazarr-binaries@latest/announcements.json",
            timeout=30
        )
    except Exception:
        try:
            logging.exception("Error trying to get announcements from jsdelivr.net, falling back to Github.")
            r = requests.get(
                url="https://raw.githubusercontent.com/LavX/bazarr-binaries/refs/heads/master/announcements.json",
                timeout=30
            )
        except Exception:
            logging.exception("Error trying to get announcements from Github.")
            return
    else:
        with open(os.path.join(args.config_dir, 'config', 'announcements.json'), 'wb') as f:
            f.write(r.content)
    finally:
        if not startup:
            jobs_queue.update_job_name(job_id=job_id, new_job_name="Updated Announcements File")


def get_online_announcements():
    try:
        with open(os.path.join(args.config_dir, 'config', 'announcements.json'), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    else:
        for announcement in data['data']:
            if 'enabled' not in announcement:
                data['data'][announcement]['enabled'] = True
            if 'dismissible' not in announcement:
                data['data'][announcement]['dismissible'] = True

        return data['data']


def get_local_announcements():
    return []


def get_all_announcements():
    # get announcements that haven't been dismissed yet
    announcements = [parse_announcement_dict(x) for x in get_online_announcements() + get_local_announcements() if
                     x['enabled'] and (not x['dismissible'] or not
                     database.execute(
                         select(TableAnnouncements)
                         .where(TableAnnouncements.hash ==
                                hashlib.sha256(x['text'].encode('UTF8')).hexdigest()))
                                       .first())]

    return sorted(announcements, key=itemgetter('timestamp'), reverse=True)


def mark_announcement_as_dismissed(hashed_announcement):
    text = [x['text'] for x in get_all_announcements() if x['hash'] == hashed_announcement]
    if text:
        database.execute(
            insert(TableAnnouncements)
            .values(hash=hashed_announcement,
                    timestamp=datetime.now(),
                    text=text[0])
            .on_conflict_do_nothing())
