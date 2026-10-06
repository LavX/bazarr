# coding=utf-8

import os
import hashlib
import logging
import json
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from datetime import datetime

from app.database import TableAnnouncements, database, insert, select

from app.get_args import args
from app.jobs_queue import jobs_queue
from utilities.pretty_date import pretty_date


_session: requests.Session | None = None
_session_lock = threading.Lock()


def _announcements_session() -> requests.Session:
    """Lazily-initialized module-level requests.Session for the
    announcements fetcher. The pool benefit is small here (the job runs
    every 6 hours and falls back to a second host on jsdelivr failure),
    but mounting an HTTPAdapter keeps the behaviour consistent with the
    Sonarr / Radarr pooled clients and avoids urllib3's stock 1-connection
    default."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=4,
                    pool_maxsize=4,
                    max_retries=Retry(
                        total=3,
                        backoff_factor=0.3,
                        status_forcelist=(502, 503, 504),
                    ),
                )
                s.mount('http://', adapter)
                s.mount('https://', adapter)
                _session = s
    return _session


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
    announcement_dict['timestamp'] = pretty_date(announcement_dict['timestamp'])
    announcement_dict['link'] = announcement_dict.get('link', '')
    announcement_dict['dismissible'] = announcement_dict.get('dismissible', True)
    announcement_dict['enabled'] = announcement_dict.get('enabled', True)
    announcement_dict['hash'] = hashlib.sha256(announcement_dict['text'].encode('UTF8')).hexdigest()

    return announcement_dict


def get_announcements_to_file(job_id=None, startup=False, wait_for_completion=False):
    if not startup and not job_id:
        jobs_queue.add_job_from_function("Updating Announcements File", is_progress=False,
                                         wait_for_completion=wait_for_completion)
        return

    try:
        r = _announcements_session().get(
            url="https://cdn.jsdelivr.net/gh/LavX/bazarr-binaries@master/announcements.json",
            timeout=30
        )
        r.raise_for_status()
    except Exception:
        try:
            logging.exception("Error trying to get announcements from jsdelivr.net, falling back to Github.")
            r = _announcements_session().get(
                url="https://raw.githubusercontent.com/LavX/bazarr-binaries/refs/heads/master/announcements.json",
                timeout=30
            )
            r.raise_for_status()
        except Exception:
            logging.exception("Error trying to get announcements from Github.")
            return
    with open(os.path.join(args.config_dir, 'config', 'announcements.json'), 'wb') as f:
        f.write(r.content)
    if not startup:
        jobs_queue.update_job_name(job_id=job_id, new_job_name="Updated Announcements File")


def get_online_announcements():
    try:
        with open(os.path.join(args.config_dir, 'config', 'announcements.json'), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    entries = data.get('data') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    # The file is stored as it was downloaded, so an entry with no text that can
    # be hashed, or a timestamp pretty_date cannot handle, is skipped rather than
    # failing the whole list. pretty_date dates an integer epoch (a millisecond
    # epoch is past year 9999) and shows an empty timestamp as now.
    online = []
    for announcement in entries:
        if not isinstance(announcement, dict) or not isinstance(announcement.get('text'), str) \
                or 'timestamp' not in announcement:
            continue
        timestamp = announcement['timestamp']
        if timestamp and type(timestamp) is not int:
            continue
        try:
            announcement['text'].encode('UTF8')
            if timestamp:
                datetime.fromtimestamp(timestamp)
        except (OverflowError, OSError, ValueError):
            continue
        announcement.setdefault('enabled', True)
        announcement.setdefault('dismissible', True)
        online.append(announcement)

    return online


def get_local_announcements():
    return []


def get_all_announcements():
    # get announcements that haven't been dismissed yet
    announcements = [x for x in get_online_announcements() + get_local_announcements() if
                     x['enabled'] and (not x['dismissible'] or not
                     database.execute(
                         select(TableAnnouncements)
                         .where(TableAnnouncements.hash ==
                                hashlib.sha256(x['text'].encode('UTF8')).hexdigest()))
                                       .first())]

    # sort on the integer timestamp before parse_announcement_dict replaces it
    # with the relative-age text, which does not sort newest-first; an empty
    # timestamp sorts as current, matching the now age pretty_date gives it
    now = int(datetime.now().timestamp())
    announcements = sorted(announcements, key=lambda x: x['timestamp'] or now, reverse=True)

    return [parse_announcement_dict(x) for x in announcements]


def mark_announcement_as_dismissed(hashed_announcement):
    text = [x['text'] for x in get_all_announcements() if x['hash'] == hashed_announcement]
    if text:
        database.execute(
            insert(TableAnnouncements)
            .values(hash=hashed_announcement,
                    timestamp=datetime.now(),
                    text=text[0])
            .on_conflict_do_nothing())
