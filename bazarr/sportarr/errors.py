"""Error types shared by the sports layer.

The sports API answered 400 for a row that does not exist, because every
resolution helper raised a bare ``ValueError`` and every handler mapped
``ValueError`` onto "bad request". The series and movies equivalents answer
404, and the sports combine Resource even declared a 404 response it could
never emit. A client cannot tell "you asked for something that is not here"
from "your request was malformed".

``SportsNotFound`` subclasses ``ValueError`` on purpose: a handler that has not
been taught about it keeps the old 400 behaviour instead of turning a missing
row into an unhandled 500.
"""


class SportsNotFound(ValueError):
    """The requested sports row does not exist, or is not this owner's."""


class SportsOwnersBusy(ValueError):
    """The owned publication boundary could not take its locks right now.

    The boundary is deliberately NOWAIT, so a concurrent writer on the owner
    tables fails it outright rather than queueing behind a scan. That is a
    transient contention answer, not a fault, and callers that can afford to
    wait a moment should retry it rather than abandoning a publication midway.

    Subclasses ``ValueError`` for the reason ``SportsNotFound`` does: every
    handler that already maps ``ValueError`` onto a 409 keeps doing so.
    """
