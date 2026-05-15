# coding=utf-8

from __future__ import absolute_import
import os
import logging
import re
import shutil
import subprocess
import sys
import traceback

from .common import update_video

logger = logging.getLogger(__name__)

FILEBOT_XATTR_PATTERN = re.compile(
    r'(?um)(net\.filebot\.filename(?=="|: )[=:" ]+|Attribute.+:\s)([^"\n\r\0]+)'
)
FILEBOT_STREAM_NAME = "net.filebot.filename"
FILEBOT_XATTR_TIMEOUT = 10


def _parse_filebot_output(output):
    match = FILEBOT_XATTR_PATTERN.search(output)
    if match:
        return match.group(2).strip()


def _win32_xattr(filename):
    with open(f"{filename}:{FILEBOT_STREAM_NAME}", "rb") as stream:
        return stream.read().decode("utf-8", errors="replace")


def _default_xattr_command(filename):
    attr_binary = shutil.which("getfattr") or shutil.which("attr") or shutil.which("filebot") or "filebot"

    if "getfattr" in attr_binary:
        return [attr_binary, "-n", f"user.{FILEBOT_STREAM_NAME}", filename]

    if "attr" in attr_binary:
        return [attr_binary, "-g", FILEBOT_STREAM_NAME, filename]

    return [attr_binary, "-script", "fn:xattr", filename]


def _darwin_xattr_command(filename):
    return ["filebot", "-script", "fn:xattr", filename]


XATTR_MAP = {
    "darwin": (_darwin_xattr_command, _parse_filebot_output),
    "win32": (lambda filename: filename, _win32_xattr),
}


def get_filebot_attrs(filename):
    args_func, match_func = XATTR_MAP.get(sys.platform, (_default_xattr_command, _parse_filebot_output))
    args = args_func(filename)

    if isinstance(args, list):
        env_path = os.pathsep.join(
            [
                "/usr/local/bin",
                "/usr/bin",
                "/usr/local/sbin",
                "/usr/sbin",
                os.environ.get("PATH", ""),
            ]
        )
        env = dict(os.environ, PATH=env_path)
        env.pop("LD_LIBRARY_PATH", None)

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=FILEBOT_XATTR_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.info("%s: Timed out while getting filebot original filename, args: %r", filename, args)
            return
        except Exception:
            logger.error("%s: Unexpected error while getting filebot original filename: %s",
                         filename, traceback.format_exc())
            return

        if proc.returncode != 0:
            logger.info("%s: Couldn't get filebot original filename, args: %r, output: %r, error: %r",
                        filename, args, proc.stdout, proc.stderr)
            return

        output = proc.stdout
    else:
        output = args

    try:
        orig_fn = match_func(output)
        if orig_fn:
            return orig_fn.strip()
    except Exception:
        logger.info("%s: Couldn't get filebot original filename", filename)
        logger.debug("%s: Result: %r", filename, output)


def refine(video, **kwargs):
    """

    :param video:
    :param kwargs:
    :return:
    """
    try:
        orig_fn = get_filebot_attrs(video.name)

        if orig_fn:
            update_video(video, orig_fn)
        else:
            logger.info(u"%s: Filebot didn't return an original filename", video.name)
    except Exception:
        logger.exception(u"%s: Something went wrong when retrieving filebot attributes:", video.name)
