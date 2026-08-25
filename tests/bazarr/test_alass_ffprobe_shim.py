# coding=utf-8
"""alass cannot read ffprobe output for a stream it cannot identify.

alass-cli probes the reference video itself and deserializes the JSON into a
struct whose codec_long_name is a required String. ffprobe omits that field
entirely for a stream whose codec it cannot identify, which is what an MKV
attachment muxed as application/octet-stream produces, and those are routine in
fansub releases. One such stream fails the whole parse, so alass dies before
extracting any audio and the user sees "alass is broken".

The shim sits between alass and the real ffprobe and fills the field in.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest


def _shim():
    from subtitles.tools import alass_ffprobe_shim

    return alass_ffprobe_shim


def test_a_stream_without_codec_long_name_gets_one():
    probe = {
        "streams": [
            {"index": 0, "codec_long_name": "AV1", "codec_type": "video", "duration": "1421.0"},
            {"index": 1, "codec_long_name": "Opus", "codec_type": "audio", "channels": 2},
            {"index": 2, "codec_type": "attachment"},
        ],
        "format": {"duration": "1421.0"},
    }

    patched = json.loads(_shim().patch_ffprobe_json(json.dumps(probe)))

    assert [s.get("codec_long_name") for s in patched["streams"]] == ["AV1", "Opus", "unknown"]
    assert patched["format"] == {"duration": "1421.0"}


def test_streams_that_already_name_their_codec_are_untouched():
    probe = {"streams": [{"index": 0, "codec_long_name": "TrueType font", "codec_type": "attachment"}]}

    assert json.loads(_shim().patch_ffprobe_json(json.dumps(probe))) == probe


def test_output_that_is_not_json_is_passed_through_verbatim():
    text = "index=0\ncodec_type=video\n"

    assert _shim().patch_ffprobe_json(text) == text


def test_json_without_a_streams_array_is_passed_through():
    text = json.dumps({"format": {"duration": "10.0"}})

    assert json.loads(_shim().patch_ffprobe_json(text)) == {"format": {"duration": "10.0"}}


def _run_shim(args, real_ffprobe):
    env = dict(os.environ)
    env["BAZARR_ALASS_REAL_FFPROBE"] = real_ffprobe
    return subprocess.run(
        [sys.executable, _shim().__file__, *args],
        capture_output=True, text=True, env=env,
    )


def test_the_shim_reports_the_real_ffprobe_exit_code_and_stderr(tmp_path):
    fake = tmp_path / "ffprobe"
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    fake.chmod(0o755)

    done = _run_shim(["-of", "json", "-show_streams", "whatever.mkv"], str(fake))

    assert done.returncode == 3
    assert "boom" in done.stderr


def test_the_shim_fills_the_field_when_ffprobe_is_run_for_json(tmp_path):
    probe = {"streams": [{"index": 0, "codec_type": "attachment"}]}
    fake = tmp_path / "ffprobe"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(probe)}\nEOF\n")
    fake.chmod(0o755)

    done = _run_shim(["-v", "error", "-of", "json", "file.mkv"], str(fake))

    assert done.returncode == 0
    assert json.loads(done.stdout)["streams"][0]["codec_long_name"] == "unknown"


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="needs ffmpeg and ffprobe to build the reproduction file")
def test_a_real_octet_stream_attachment_survives_the_shim(tmp_path):
    """The end to end shape of the bug, built rather than committed as a fixture."""
    attachment = tmp_path / "blob.bin"
    attachment.write_bytes(b"\x00\x01\x02\x03")
    mkv = tmp_path / "episode.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=5",
         "-f", "lavfi", "-i", "sine=duration=1",
         "-attach", str(attachment), "-metadata:s:t:0", "mimetype=application/octet-stream",
         "-c:v", "libx264", "-c:a", "aac", str(mkv)],
        check=True, capture_output=True,
    )

    args = ["-v", "error", "-show_entries",
            "format=duration:stream=index,codec_long_name,channels,duration,codec_type",
            "-of", "json", str(mkv)]
    raw = subprocess.run(["ffprobe", *args], capture_output=True, text=True, check=True).stdout
    assert any("codec_long_name" not in s for s in json.loads(raw)["streams"]), (
        "this ffprobe build names every stream, so the reproduction is not reproducing"
    )

    done = _run_shim(args, shutil.which("ffprobe"))

    assert done.returncode == 0
    assert all(s.get("codec_long_name") for s in json.loads(done.stdout)["streams"])


# --- wiring: only alass gets the shim ---------------------------------------


@pytest.fixture
def recorded_alass_run(monkeypatch, tmp_path):
    """Run SubSyncer's external-engine path against a stub subprocess.run."""
    from subtitles.tools import subsyncer as module

    calls = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(module, "get_binary", lambda name: f"/usr/bin/{name}")

    syncer = module.SubSyncer()
    syncer.srtin = str(tmp_path / "sub.srt")
    syncer.reference = None

    return syncer, calls


@pytest.mark.skipif(os.name != "posix", reason="the launcher is a POSIX shell script")
def test_alass_is_run_against_the_shim(recorded_alass_run, tmp_path):
    syncer, calls = recorded_alass_run

    syncer._run_external_engine(engine="alass", output_path=tmp_path / "out.srt",
                                video_path=str(tmp_path / "video.mkv"))

    env = calls[0]["env"]
    assert env is not None
    assert env["BAZARR_ALASS_REAL_FFPROBE"] == "/usr/bin/ffprobe"
    launcher = env["ALASS_FFPROBE_PATH"]
    assert os.access(launcher, os.X_OK), f"{launcher} is not executable, alass cannot run it"
    # The rest of the environment is alass's own, not a two-variable replacement for it.
    assert env["PATH"] == os.environ["PATH"]
    # And the invocation itself is untouched.
    assert calls[0]["command"][0] == "/usr/bin/alass"


@pytest.mark.skipif(os.name != "posix", reason="the launcher is a POSIX shell script")
def test_the_launcher_really_runs_the_shim(tmp_path):
    """Built as a script rather than asserted on its text: what matters is that
    alass, which execs this path directly, gets patched output out of it."""
    probe = {"streams": [{"index": 0, "codec_type": "attachment"}]}
    fake = tmp_path / "ffprobe"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(probe)}\nEOF\n")
    fake.chmod(0o755)

    launcher = _shim().ensure_launcher()
    env = dict(os.environ, BAZARR_ALASS_REAL_FFPROBE=str(fake))
    done = subprocess.run([launcher, "-of", "json", "x.mkv"], capture_output=True, text=True, env=env)

    assert json.loads(done.stdout)["streams"][0]["codec_long_name"] == "unknown"


# --- the launcher must be ours, and must survive awkward install paths ------


@pytest.mark.skipif(os.name != "posix", reason="the launcher is a POSIX shell script")
def test_the_launcher_never_lands_in_a_directory_someone_else_prepared(monkeypatch, tmp_path):
    """A predictable path under /tmp can be pre-created world-writable by
    another user on a shared host. makedirs(exist_ok=True) would accept it, and
    that user could then swap the file between our write and alass execing it,
    so alass would run their code with our credentials."""
    import stat as stat_module

    shim = _shim()
    monkeypatch.setattr(shim.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(shim, "_LAUNCHER_PATH", None, raising=False)
    hostile = tmp_path / "bazarr-alass-shim"
    hostile.mkdir(mode=0o777)

    launcher = shim.ensure_launcher()

    directory = os.path.dirname(launcher)
    mode = os.stat(directory).st_mode
    assert not mode & (stat_module.S_IWGRP | stat_module.S_IWOTH), (
        f"{directory} is writable by someone other than us"
    )
    assert os.stat(directory).st_uid == os.getuid()
    assert not os.path.exists(hostile / "ffprobe"), "the launcher landed in the directory someone else prepared"


@pytest.mark.skipif(os.name != "posix", reason="the launcher is a POSIX shell script")
def test_an_install_path_with_spaces_still_produces_a_working_launcher(tmp_path):
    """/bin/sh splits on whitespace, so an unquoted path in the generated
    script means every alass sync fails on an install path with a space in it,
    which is ordinary on macOS and Windows-turned-POSIX mounts."""
    awkward = tmp_path / "my programs" / "shim dir"
    awkward.mkdir(parents=True)
    shim_copy = awkward / "alass_ffprobe_shim.py"
    shim_copy.write_text(open(_shim().__file__, encoding="utf-8").read(), encoding="utf-8")

    launcher = awkward / "ffprobe"
    launcher.write_text(_shim().launcher_script(sys.executable, str(shim_copy)), encoding="utf-8")
    launcher.chmod(0o700)

    probe = {"streams": [{"index": 0, "codec_type": "attachment"}]}
    fake = tmp_path / "ffprobe-real"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(probe)}\nEOF\n")
    fake.chmod(0o755)

    done = subprocess.run([str(launcher), "-of", "json", "x.mkv"], capture_output=True, text=True,
                          env=dict(os.environ, BAZARR_ALASS_REAL_FFPROBE=str(fake)))

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["streams"][0]["codec_long_name"] == "unknown"


def test_alass_runs_unshimmed_when_ffprobe_cannot_be_found(recorded_alass_run, monkeypatch, tmp_path):
    """get_binary raises rather than returning a falsy value when it cannot
    find a binary. An install running alass off an inherited PATH worked before
    the shim existed and has to keep working."""
    from subtitles.tools import subsyncer as module
    from utilities.binaries import BinaryNotFound

    syncer, calls = recorded_alass_run

    def raising(name):
        raise BinaryNotFound()

    monkeypatch.setattr(module, "get_binary", raising)

    syncer._run_external_engine(engine="alass", output_path=tmp_path / "out.srt",
                                video_path=str(tmp_path / "video.mkv"))

    env = calls[0]["env"]
    assert "ALASS_FFPROBE_PATH" not in env
    assert env["PATH"] == os.environ["PATH"]
