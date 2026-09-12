"""One definition of what a page of sports rows means.

The shared frontend views fetch a whole list by asking for length -1, which
every episodes and movies endpoint already honours, and they ask for "All" by
sending the row total as an explicit length. The episodes and movies endpoints
serve any positive length with no ceiling. The sports readers once carried a
1..1000 guard: it rejected -1, so a sports page could not use the shared views
at all (filtering or sorting across the library needs the whole list), and once
-1 was accepted it still rejected a total above 1000, so "All" failed on a
history larger than that. Sports now speaks the same contract: -1 is unbounded
and any positive length is served as asked.
"""


def validate_page(start, length):
    """Raise on a page that cannot be served; return the SQL limit to apply.

    None means no limit, which is what SQLAlchemy's .limit() wants for an
    unbounded query.
    """
    if start < 0:
        raise ValueError('Invalid pagination')
    if length == -1:
        return None
    if length < 1:
        raise ValueError('Invalid pagination')
    return length
