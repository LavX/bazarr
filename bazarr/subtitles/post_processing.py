# coding=utf-8
# fmt: off

import os
import logging
import subprocess

from locale import getpreferredencoding
from utilities.helper import get_target_folder
from subtitles.tools.subsync_engines import subtitle_write_locks, subtitle_mutation


def postprocessing(command, path, subtitle_path=None, *, lock_paths=None):
    # Configured commands can mutate subtitles in place. This is the one boundary
    # that must hold this media's mutation locks while the external command runs.
    if lock_paths is None:
        destination = os.path.join(get_target_folder(path, create=False) or os.path.dirname(path), '.destination')
        lock_paths = (path, destination, subtitle_path or path)
    # A caller already holding these locks must reuse its resolved directories.
    # Live destination settings may have changed since that outer acquisition.
    with subtitle_write_locks(path, *lock_paths) as states:
        watched_paths = {watched for state in states.values() for watched in state.revisions}
        if subtitle_path:
            watched_paths.add(subtitle_path)
        with subtitle_mutation(path, *watched_paths):
            return _postprocessing_locked(command, path)


def _postprocessing_locked(command, path):
    try:
        encoding = getpreferredencoding()
        if os.name == 'nt':
            from ctypes import windll
            code_page = windll.kernel32.GetConsoleOutputCP()
            encoding = f"cp{code_page}"
            
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, encoding=encoding)
        # wait for the process to terminate
        out, err = process.communicate()

        out = out.replace('\n', ' ').replace('\r', ' ')

    except Exception as e:
        logging.error(f'BAZARR Post-processing failed for file {path}: {repr(e)}')  # noqa: G004
    else:
        if err:
            parsed_err = err.replace('\n', ' ').replace('\r', ' ')
            logging.error(f'BAZARR Post-processing result for file {path}: {parsed_err}')  # noqa: G004
        elif out == "":
            logging.info(
                f'BAZARR Post-processing result for file {path}: Nothing returned from command execution')  # noqa: G004
        else:
            logging.info(f'BAZARR Post-processing result for file {path}: {out}')  # noqa: G004
