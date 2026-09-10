"""One definition of what a page of sports rows means.

The shared frontend views fetch a whole list by asking for length -1, which
every episodes and movies endpoint already honours. The sports readers each
carried their own 1..1000 guard and rejected it, so a sports page could not use
those views at all: filtering or sorting across the library needs the whole
list, and the sports endpoints would only ever answer with one page.
"""

# Ceiling for an explicit page. -1 is unbounded, which is the fetch-all mode.
MAX_PAGE_LENGTH = 1000


def validate_page(start, length):
    """Raise on a page that cannot be served; return the SQL limit to apply.

    None means no limit, which is what SQLAlchemy's .limit() wants for an
    unbounded query.
    """
    if start < 0:
        raise ValueError('Invalid pagination')
    if length == -1:
        return None
    if not 1 <= length <= MAX_PAGE_LENGTH:
        raise ValueError('Invalid pagination')
    return length
