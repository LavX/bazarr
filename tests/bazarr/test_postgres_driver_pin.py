# coding=utf-8
"""The PostgreSQL engine runs on psycopg2, the one driver the image ships.

postgres-requirements.txt installs psycopg2 and nothing else. For a bare
``postgresql`` URL SQLAlchemy picks the driver itself, and from 2.1 on its pick
is psycopg 3, so an engine that leaves the choice to it imports a module the
image does not have on the day SQLAlchemy is bumped. These cases fail on such a
bump unless the driver is named.

create_engine loads the driver but does not connect, so no server is needed.
The file runs where CI installs postgres-requirements.txt.
"""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

FIELDS = dict(username='bazarr', password='not-a-secret', host='db.invalid', port='5432',
              database='bazarr')


def _url(postgres_url=None, **fields):
    from app.postgres_url import postgres_engine_url

    postgresql = SimpleNamespace(url=postgres_url or '', **dict(FIELDS, **fields))
    return postgres_engine_url(SimpleNamespace(postgresql=postgresql), environ={})


def test_the_five_settings_select_psycopg2():
    url = _url()
    # SQLAlchemy 2.0 resolves a bare 'postgresql' to psycopg2 too, so only the
    # URL shows whether the driver is named before the bump that changes that.
    assert url.drivername == 'postgresql+psycopg2'
    engine = sa.create_engine(url)
    try:
        assert engine.dialect.driver == 'psycopg2'
    finally:
        engine.dispose()


@pytest.mark.parametrize('scheme', ['postgresql', 'postgresql+psycopg2'])
def test_a_postgres_url_selects_psycopg2(scheme):
    engine = sa.create_engine(_url(f'{scheme}://someone@elsewhere.invalid:6543/other'))
    try:
        assert engine.dialect.driver == 'psycopg2'
    finally:
        engine.dispose()


def test_a_driver_the_url_names_is_kept():
    """Choosing another driver is the user's call, not something to rewrite."""
    url = _url('postgresql+pg8000://someone@elsewhere.invalid/other')
    assert url.drivername == 'postgresql+pg8000'


def test_the_url_keeps_what_the_settings_do_not_override():
    url = _url('postgresql://someone@elsewhere.invalid:6543/other?sslmode=require',
               username='', password='', host='', port='', database='')
    assert url.drivername == 'postgresql+psycopg2'
    assert (url.username, url.host, url.port, url.database) == (
        'someone', 'elsewhere.invalid', 6543, 'other')
    assert url.query == {'sslmode': 'require'}


def test_a_url_that_is_not_postgres_is_refused():
    with pytest.raises(ValueError, match="scheme must be 'postgresql'"):
        _url('mysql://someone@elsewhere.invalid/other')
