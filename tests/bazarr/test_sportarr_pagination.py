# coding=utf-8
"""What a page of sports rows means.

The shared Wanted, History and Blacklist views fetch a whole list by asking for
length -1, which every episodes and movies endpoint already honours. Each sports
reader carried its own 1..1000 guard and rejected it, so a sports page could
not be built on those views at all: filtering or sorting across the library
needs the whole list, and the sports endpoints would only ever answer one page.
The shared page control also sends the row total as the length when "All" is
chosen, so the ceiling made "All" fail on a library above 1000 rows. The other
two media types cap nothing, so sports now matches them: -1 stays the fetch-all
mode and any positive length is served as asked.
"""

import pytest

from sportarr.pagination import validate_page


def test_a_normal_page_limits_to_its_length():
    assert validate_page(0, 100) == 100
    assert validate_page(200, 50) == 50


def test_fetch_all_asks_for_no_limit():
    # None is what SQLAlchemy's .limit() wants for an unbounded query, and what
    # the slice-based wanted reader treats as "to the end".
    assert validate_page(0, -1) is None


def test_an_all_page_size_above_a_thousand_is_served():
    # The shared page control asks for "All" by sending the row total as the
    # length. A history with more than a thousand rows used to be refused, so
    # the table came back empty; the other two media types cap nothing.
    assert validate_page(0, 1001) == 1001
    assert validate_page(0, 5000) == 5000
    assert validate_page(0, 12000) == 12000


@pytest.mark.parametrize('length', [0, -2])
def test_a_length_that_is_neither_a_page_nor_fetch_all_is_refused(length):
    """-1 is the only negative that means anything. 0 would silently return an
    empty page forever."""
    with pytest.raises(ValueError, match='Invalid pagination'):
        validate_page(0, length)


def test_a_negative_start_is_refused_even_when_fetching_everything():
    with pytest.raises(ValueError, match='Invalid pagination'):
        validate_page(-1, -1)


def test_every_sports_reader_uses_the_shared_definition():
    """Three copies of the same guard is how they drifted apart in the first
    place, and how -1 came to be rejected in all of them."""
    import inspect

    from sportarr import history, library, workflows

    for module in (history, library, workflows):
        source = inspect.getsource(module)
        assert 'validate_page(start, length)' in source
        assert '1 <= length <= 1000' not in source
