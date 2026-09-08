# coding=utf-8
"""Explicit segment-aware mappings across POSIX and Windows media roots."""

from pathlib import PurePosixPath, PureWindowsPath

from .http import MediaServerError


def _media_path(value, code="path_invalid"):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise MediaServerError(code)
    windows = bool(PureWindowsPath(value).drive) or "\\" in value
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise MediaServerError(code)
    return path


def validate_path_mappings(mappings):
    if not isinstance(mappings, list):
        raise MediaServerError("mapping_invalid")
    for row in mappings:
        if (not isinstance(row, dict) or not {"local_path", "remote_path"}.issubset(row)
                or set(row) - {"local_path", "remote_path", "library_id"}):
            raise MediaServerError("mapping_invalid")
        _media_path(row["local_path"], "mapping_invalid")
        _media_path(row["remote_path"], "mapping_invalid")
        library_id = row.get("library_id")
        if library_id is not None and (not isinstance(library_id, str) or any(ord(char) < 32 for char in library_id)):
            raise MediaServerError("mapping_invalid")
    return mappings


def media_paths_equal(left, right):
    try:
        return _media_path(left) == _media_path(right)
    except MediaServerError:
        return False


def map_media_path(video_path: str, mappings: list[dict], *, require_library: bool = False) -> dict:
    path = _media_path(video_path)
    validate_path_mappings(mappings)
    matches = []
    for row in mappings:
        local = _media_path(row["local_path"])
        if type(path) is not type(local) or not path.is_relative_to(local):
            continue
        matches.append((len(local.parts), row, path.relative_to(local)))
    if not matches:
        raise MediaServerError("mapping_missing")
    specificity = max(match[0] for match in matches)
    best = [match for match in matches if match[0] == specificity]
    if len(best) != 1:
        raise MediaServerError("mapping_ambiguous")
    _specificity, row, relative = best[0]
    library_id = row.get("library_id")
    if require_library and (not library_id or not library_id.strip()):
        raise MediaServerError("library_missing")
    remote = _media_path(row["remote_path"])
    for component in relative.parts:
        destination_component = type(remote)(component)
        # A source filename must not reset the drive/root or become different
        # components when the destination uses another path flavor.
        if destination_component.anchor or destination_component.parts != (component,):
            raise MediaServerError("path_invalid")
    return {"path": str(remote.joinpath(*relative.parts)), "library_id": library_id}
