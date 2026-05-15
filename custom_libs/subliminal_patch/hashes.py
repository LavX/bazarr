# coding=utf-8

import hashlib
import os

from subliminal.refiners.hash import hash_opensubtitles as hash_opensubtitles


def hash_thesubdb(video_path):
    readsize = 64 * 1024
    if os.path.getsize(video_path) < readsize:
        return None
    with open(video_path, 'rb') as handle:
        data = handle.read(readsize)
        handle.seek(-readsize, os.SEEK_END)
        data += handle.read(readsize)

    return hashlib.md5(data).hexdigest()


def hash_napiprojekt(video_path):
    readsize = 1024 * 1024 * 10
    with open(video_path, 'rb') as handle:
        data = handle.read(readsize)
    return hashlib.md5(data).hexdigest()


def hash_shooter(video_path):
    filesize = os.path.getsize(video_path)
    readsize = 4096
    if filesize < readsize * 2:
        return None

    offsets = (readsize, filesize // 3 * 2, filesize // 3, filesize - readsize * 2)
    filehash = []
    with open(video_path, 'rb') as handle:
        for offset in offsets:
            handle.seek(offset)
            filehash.append(hashlib.md5(handle.read(readsize)).hexdigest())

    return ';'.join(filehash)
