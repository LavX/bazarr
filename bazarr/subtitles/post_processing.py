# coding=utf-8
# fmt: off

import os
import logging
import subprocess
import shutil

from locale import getpreferredencoding
from utilities.helper import get_target_folder
from subtitles.tools.subsync_engines import subtitle_write_locks, subtitle_mutation


def postprocessing(command, path, subtitle_path=None, *, lock_paths=None,
                   publication_guard=None, command_builder=None, source_version=None,
                   after_write=None, on_publish=None):
    if publication_guard is not None:
        from subtitles.tools.subsync_engines import (
            staged_subtitle_write, source_is_unchanged, SubtitleSourceChanged,
            _report_subtitle_publication,
        )
        if not subtitle_path or command_builder is None:
            raise ValueError('Guarded postprocessing requires a subtitle and command builder')
        def validate_source():
            if source_version is not None and not source_is_unchanged(subtitle_path, source_version):
                raise SubtitleSourceChanged('Uploaded subtitle changed before post-processing')

        with staged_subtitle_write(path, subtitle_path, source_paths=(subtitle_path,),
                                    publication_guard=publication_guard,
                                    before_publish=validate_source, after_write=after_write) as temporary:
            with subtitle_write_locks(path, subtitle_path):
                validate_source()
                shutil.copyfile(subtitle_path, temporary)
            _postprocessing_locked(command_builder(temporary), path)
        _report_subtitle_publication(on_publish, subtitle_path)
        return
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
