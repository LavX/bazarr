# coding=utf-8
"""The 404 guard shared by every route of the translation runtime's API.

The ``api.translation`` package is imported, and its namespaces registered, only when the
``translator.runtime_v2`` switch is on. This guard is the second line: while the runtime is off,
or its tables are missing, a route answers 404 as if it did not exist. It goes outside
``authenticate``, so an unauthenticated caller sees the same 404 and learns nothing.

Every route of every namespace in this package uses it:

    @runtime_v2_only
    @authenticate
    def get(self): ...
"""
from functools import wraps

from werkzeug.exceptions import NotFound


def runtime_v2_only(method):
    """Answer 404 unless the translation runtime is on."""
    @wraps(method)
    def wrapper(*args, **kwargs):
        # Looked up on every request, and imported here, so the package is not loaded by a switched-off boot.
        from subtitles.tools.translate import runtime

        if not runtime.runtime_v2_enabled():
            raise NotFound()
        return method(*args, **kwargs)

    return wrapper
