# coding=utf-8
"""An ffprobe stand-in for alass, which cannot read output it did not expect.

alass-cli probes the reference video itself, with a fixed
``ffprobe -v error -show_entries ... -of json`` command, and deserializes the
result into a struct whose ``codec_long_name`` is a required String. ffprobe
omits that field entirely for a stream whose codec it cannot identify: the
codec descriptor is NULL, so the field is written with print_str_opt, which the
JSON writer drops. An MKV attachment muxed as application/octet-stream is
exactly that, and fansub releases carry fonts and other attachments as a matter
of course, so one attachment fails the whole parse and alass dies before it
extracts any audio.

Upstream alass is effectively unmaintained, so this fills the field in on the
way past instead. Only the missing field is invented, and only when the output
is JSON carrying a streams array; everything else, including the exit code and
stderr, is the real ffprobe's.

Run as a program: it execs the ffprobe named by BAZARR_ALASS_REAL_FFPROBE, or
the first one on PATH, with the arguments it was given.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

# What a stream whose codec ffprobe could not identify gets called. alass only
# needs the field to exist: it reads codec_type to pick the audio stream.
UNKNOWN_CODEC = "unknown"

REAL_FFPROBE_ENV = "BAZARR_ALASS_REAL_FFPROBE"

# Proves the launcher can be executed without depending on any other program:
# /bin/true is not where macOS keeps it, and a self-test that needs a program
# that is not there would disable the shim across a whole platform.
SELFTEST_ARG = "--bazarr-shim-selftest"

logger = logging.getLogger(__name__)


def patch_ffprobe_json(text):
    """Return ffprobe output with a codec_long_name on every stream.

    Anything that is not JSON with a streams array comes back untouched, so a
    caller that asked for another output format is unaffected.
    """
    try:
        probe = json.loads(text)
    except (ValueError, TypeError):
        return text

    if not isinstance(probe, dict):
        return text

    streams = probe.get("streams")
    if not isinstance(streams, list):
        return text

    patched = False
    for stream in streams:
        if isinstance(stream, dict) and not stream.get("codec_long_name"):
            stream["codec_long_name"] = UNKNOWN_CODEC
            patched = True

    if not patched:
        return text

    return json.dumps(probe)


_LAUNCHER_PATH = None


def launcher_script(python, shim):
    """The one line shell wrapper that runs the shim under ``python``.

    Both paths are quoted: /bin/sh splits on whitespace, and an install path
    with a space in it is ordinary rather than exotic.
    """
    return "#!/bin/sh\nexec {python} {shim} \"$@\"\n".format(
        python=shlex.quote(python), shim=shlex.quote(shim),
    )


def _writable_directories():
    """Where the launcher may live, best first.

    Bazarr's own config directory comes first: the system temp directory is
    routinely mounted noexec, and a launcher there can be written and marked
    executable and still refuse to run.
    """
    directories = []
    try:
        from app.get_args import args

        if args.config_dir:
            directories.append(os.path.join(args.config_dir, "cache"))
    except Exception:
        # Imported by the shim's own process too, which has no Bazarr around it.
        logger.debug("No Bazarr config directory for the alass shim", exc_info=True)

    directories.append(tempfile.gettempdir())
    return directories


LAUNCHER_PREFIX = "bazarr-alass-shim-"


def _ensure(parent):
    os.makedirs(parent, exist_ok=True)
    _sweep(parent)
    return parent


def _sweep(parent):
    """Remove launcher directories we left behind on earlier runs.

    The current one is only remembered in memory and Bazarr exits through
    os._exit, so nothing cleans up on the way out and every restart that syncs
    with alass would leave another directory here. Only directories this user
    owns are touched, and a running process that loses its launcher this way
    simply installs another one.
    """
    try:
        entries = os.listdir(parent)
    except OSError:
        return

    for entry in entries:
        if not entry.startswith(LAUNCHER_PREFIX):
            continue

        path = os.path.join(parent, entry)
        try:
            if os.stat(path).st_uid != os.getuid():
                continue
        except OSError:
            continue

        shutil.rmtree(path, ignore_errors=True)


def _install_launcher(directory, content):
    """Write the launcher in ``directory`` and prove it can actually run.

    The directory comes from mkdtemp, which gives us 0700 ownership of a name
    nobody could have guessed.
    """
    launcher = os.path.join(directory, "ffprobe")
    with open(launcher, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(launcher, 0o700)

    # Run it once. A noexec mount fails here rather than in the middle of a
    # subtitle sync.
    probe = subprocess.run([launcher, SELFTEST_ARG], capture_output=True, timeout=30)
    if probe.returncode != 0:
        raise OSError(f"the launcher in {directory} exited {probe.returncode}")

    return launcher


def ensure_launcher():
    """Path to an executable that runs this shim, or None if there cannot be one.

    alass execs ALASS_FFPROBE_PATH directly, so it has to be a program, not an
    interpreter plus a script. A one line shell wrapper pins the interpreter to
    the one Bazarr is running under rather than whatever a shebang would find.

    The directory is created with mkdtemp rather than at a predictable path:
    on a shared host another user could pre-create a well known path as
    world-writable, and replace the launcher between our write and alass
    execing it, which would run their code with Bazarr's credentials.

    Windows has no equivalent alass would run, so there the shim is simply not
    installed and alass behaves as it always has.
    """
    global _LAUNCHER_PATH

    if os.name != "posix":
        return None

    if _LAUNCHER_PATH and os.path.isfile(_LAUNCHER_PATH):
        return _LAUNCHER_PATH

    content = launcher_script(sys.executable, os.path.abspath(__file__))
    for parent in _writable_directories():
        directory = None
        try:
            directory = tempfile.mkdtemp(prefix=LAUNCHER_PREFIX, dir=_ensure(parent))
            _LAUNCHER_PATH = _install_launcher(directory, content)
        except Exception:
            logger.debug("Could not install the alass ffprobe shim in %s", parent, exc_info=True)
            # Nothing remembers the failure, so every sync retries this. Each
            # retry leaving its private directory behind would leak inodes.
            if directory:
                shutil.rmtree(directory, ignore_errors=True)
            continue

        return _LAUNCHER_PATH

    return None


def real_ffprobe():
    return os.environ.get(REAL_FFPROBE_ENV) or shutil.which("ffprobe")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv == [SELFTEST_ARG]:
        # Only ever run by ensure_launcher, to prove this file can be executed.
        return 0

    executable = real_ffprobe()
    if not executable:
        sys.stderr.write("bazarr alass ffprobe shim: no ffprobe found\n")
        return 127

    done = subprocess.run([executable, *argv], capture_output=True, text=True)

    sys.stdout.write(patch_ffprobe_json(done.stdout))
    sys.stderr.write(done.stderr)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
