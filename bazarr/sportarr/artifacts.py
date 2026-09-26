"""Persisted identity of final sports subtitle bytes and their owning video."""

import hashlib
import json
import os
from dataclasses import dataclass

from sportarr.connection import check_cancelled
from sportarr.output import validate_output_path


class ReplacementDestinationChanged(ValueError):
    pass


@dataclass(frozen=True)
class ReplacementState:
    signature: tuple
    directory: str
    stem: str
    entries: tuple


def _replacement_entries(directory, stem):
    with os.scandir(directory) as entries:
        return tuple(
            sorted(
                (entry.name, tuple(_stat(entry.stat(follow_symlinks=False))))
                for entry in entries
                if entry.name.lower().startswith(stem + ".")
            )
        )


def capture_replacement_state(context, signature):
    from sportarr.output import output_policy

    directory = output_policy(context)[2]
    stem = os.path.splitext(os.path.basename(context.mapped_path))[0].lower()
    return ReplacementState(
        signature, directory, stem, _replacement_entries(directory, stem)
    )


def validate_replacement_state(state, destination=None):
    # Compare the directory under file coordination, outside the writer. Publication
    # then checks only the exact destination's captured and current absence.
    if destination is None:
        changed = _replacement_entries(state.directory, state.stem) != state.entries
    else:
        changed = (
            os.path.normcase(os.path.realpath(os.path.dirname(destination)))
            != state.directory
            or any(
                os.path.normcase(name)
                == os.path.normcase(os.path.basename(destination))
                for name, _ in state.entries
            )
            or os.path.lexists(destination)
        )
    if changed:
        raise ReplacementDestinationChanged(
            "Replacement skipped because the subtitle destination changed or is already occupied"
        )


def artifact_video_matches(proof, path, signature):
    try:
        artifact = json.loads(proof)
        return (
            artifact.get("version") == 1
            and artifact["path"] == os.path.realpath(path)
            and artifact["video"]
            == hashlib.sha256(repr(signature).encode()).hexdigest()
        )
    except (TypeError, ValueError, KeyError):
        return False


def _stat(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def capture_artifact(context, path, signature, cancel=None):
    validate_output_path(context, path)
    check_cancelled(cancel)
    before = os.stat(path)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        if _stat(os.fstat(stream.fileno())) != _stat(before):
            raise ValueError("Sports subtitle changed while inspecting its artifact")
        while block := stream.read(1024 * 1024):
            check_cancelled(cancel)
            digest.update(block)
        if _stat(os.fstat(stream.fileno())) != _stat(before):
            raise ValueError("Sports subtitle changed while inspecting its artifact")
    proof = json.dumps(
        {
            "version": 1,
            "video": hashlib.sha256(repr(signature).encode()).hexdigest(),
            "path": os.path.realpath(path),
            "stat": _stat(before),
            "sha256": digest.hexdigest(),
        },
        sort_keys=True,
    )
    validate_artifact_stat(proof, path)
    check_cancelled(cancel)
    return proof


def validate_artifact_stat(proof, path):
    artifact = json.loads(proof)
    if (
        artifact.get("version") != 1
        or artifact["path"] != os.path.realpath(path)
        or artifact["stat"] != _stat(os.stat(path))
    ):
        raise ValueError("Sports subtitle artifact changed")
