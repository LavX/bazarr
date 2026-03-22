# coding=utf-8

import os
import re
import sys
import shlex
import subprocess
import logging

from app.config import settings


def _escape(in_str):
    s = str(in_str) if in_str is not None else ''
    if os.name == 'nt':
        # cmd.exe: use subprocess.list2cmdline for proper Windows quoting
        return subprocess.list2cmdline([s])
    return shlex.quote(s)


def _pp_sub(pattern, value, command):
    """Substitute a placeholder, escaping backslashes for re.sub replacement."""
    escaped = _escape(value)
    return re.sub(pattern, lambda _: escaped, command)


def pp_replace(pp_command, episode, subtitles, language, language_code2, language_code3, episode_language,
               episode_language_code2, episode_language_code3, score, subtitle_id, provider, uploader,
               release_info, series_id, episode_id):
    pp_command = _pp_sub(r'[\'"]?{{directory}}[\'"]?', os.path.dirname(episode), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode}}[\'"]?', episode, pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode_name}}[\'"]?', os.path.splitext(os.path.basename(episode))[0], pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles}}[\'"]?', str(subtitles), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles_language}}[\'"]?', str(language), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles_language_code2}}[\'"]?', str(language_code2), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles_language_code3}}[\'"]?', str(language_code3), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles_language_code2_dot}}[\'"]?',
                         str(language_code2).replace(':', '.'), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitles_language_code3_dot}}[\'"]?',
                         str(language_code3).replace(':', '.'), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode_language}}[\'"]?', str(episode_language), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode_language_code2}}[\'"]?', str(episode_language_code2), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode_language_code3}}[\'"]?', str(episode_language_code3), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{score}}[\'"]?', str(score), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{subtitle_id}}[\'"]?', str(subtitle_id), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{provider}}[\'"]?', str(provider), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{uploader}}[\'"]?', str(uploader), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{release_info}}[\'"]?', str(release_info), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{series_id}}[\'"]?', str(series_id), pp_command)
    pp_command = _pp_sub(r'[\'"]?{{episode_id}}[\'"]?', str(episode_id), pp_command)
    return pp_command


def set_chmod(subtitles_path):
    # apply chmod if required
    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
        'win') and settings.general.chmod_enabled else None
    if chmod:
        logging.debug(f"BAZARR setting permission to {chmod} on {subtitles_path} after custom post-processing.")
        os.chmod(subtitles_path, chmod)
