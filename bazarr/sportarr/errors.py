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
