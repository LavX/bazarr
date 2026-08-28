# coding=utf-8
"""One place for the bind-parameter ceiling every IN clause has to respect.

SQLite compiled with the legacy SQLITE_MAX_VARIABLE_NUMBER rejects a statement
binding more than 999 variables with "too many SQL variables". That is still
what ships in plenty of distributions, and a library large enough to trip it is
ordinary rather than exotic, so any query that binds one variable per row has to
be split. The margin below 999 leaves room for the other bound values in the
statement.
"""

MAX_IN_CLAUSE = 900


def in_chunks(values, size=MAX_IN_CLAUSE):
    """Yield ``values`` in slices small enough for a single IN clause."""
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]
