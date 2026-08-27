# coding=utf-8
"""An indexing call that names an owner has to map its paths with that owner.

``store_subtitles`` and ``store_subtitles_movie`` take a remote path and the
local path it resolves to. Everything inside them, the existence check, the
embedded probe, the external subtitle scan and the row write, is resolved
against ``arr_instance_id``.

Threading the owner through the call was done. Deriving the paths with that same
owner was not: eighteen call sites passed ``arr_instance_id=`` while building the
local path with the global mapper. On any secondary instance configured with its
own path_mappings the two disagree, so the indexer is handed a path inside
another instance's library. It then finds no file and writes an empty subtitle
listing over a row whose subtitles are all present, or reads the wrong file's
tracks entirely.

This is asserted over the source rather than per call site: the failure is a
mismatched pair of arguments, which is visible in the call itself, and there are
too many paths through the app to reach each one behaviourally.
"""
import ast
import pathlib

INDEXERS = {"store_subtitles", "store_subtitles_movie"}
GLOBAL_MAPPERS = {
    "path_replace",
    "path_replace_movie",
    "path_replace_reverse",
    "path_replace_reverse_movie",
}
BAZARR = pathlib.Path(__file__).resolve().parents[2] / "bazarr"


def _called_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _global_mappers_inside(node):
    return {
        _called_name(inner)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and _called_name(inner) in GLOBAL_MAPPERS
    }


def test_no_indexing_call_maps_its_path_globally_while_naming_an_owner():
    offenders = []
    for source in sorted(BAZARR.rglob("*.py")):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) not in INDEXERS:
                continue
            if not any(kw.arg == "arr_instance_id" for kw in node.keywords):
                continue
            mappers = set()
            for argument in node.args:
                mappers |= _global_mappers_inside(argument)
            if mappers:
                offenders.append(
                    f"{source.relative_to(BAZARR.parent)}:{node.lineno} "
                    f"uses {sorted(mappers)}"
                )

    assert not offenders, (
        "these calls tell the indexer which instance owns the media and then "
        "hand it a path resolved with the global mapping, which is a different "
        "file on any instance that has its own path_mappings:\n  "
        + "\n  ".join(offenders)
    )
