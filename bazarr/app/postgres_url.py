# coding=utf-8
"""The PostgreSQL connection URL, resolved once for the application and its backups.

Nothing here reads the environment or the configuration at import time, so
backups can use it without importing app.database, which opens the engine.
"""

import os

from sqlalchemy.engine import URL, make_url

# The image installs psycopg2 from postgres-requirements.txt and no other
# PostgreSQL driver. A bare 'postgresql' URL leaves the choice to SQLAlchemy,
# which picks psycopg 3 from 2.1 on, so the driver is named instead.
POSTGRES_DRIVERNAME = "postgresql+psycopg2"

# Each URL component, the environment variable that sets it, and the libpq
# parameters that can carry it in the URL's query string instead.
_COMPONENTS = (
    ('username', 'POSTGRES_USERNAME', ('user',)),
    ('password', 'POSTGRES_PASSWORD', ('password',)),
    ('host', 'POSTGRES_HOST', ('host', 'hostaddr')),
    ('port', 'POSTGRES_PORT', ('port',)),
    ('database', 'POSTGRES_DATABASE', ('dbname',)),
)


def _query_hosts_without_inline_ports(url):
    """The URL, with the port every query host entry carries inline removed.

    The application's dialect reads the port inside a query host entry,
    host=db:6543, over the port the URL itself carries, so a POSTGRES_PORT that
    replaced the URL's port still connected the application to the inline one.
    The entries are rewritten instead, db:6543 to db, the way the port query
    key is replaced, so the port the environment names is the one the
    connection uses.

    An entry is split on its last colon, and only when what follows is digits
    or nothing, so an IPv6 address is not taken for a host and a port.
    """
    entries = url.query.get('host')
    if entries is None:
        return url
    if isinstance(entries, str):
        entries = (entries,)
    hosts = []
    for entry in entries:
        host, colon, port = entry.rpartition(':')
        hosts.append(host if colon and (port.isdigit() or not port) else entry)
    return url.update_query_dict({'host': hosts[0] if len(hosts) == 1 else hosts})


def postgres_engine_url(settings, environ=None):
    """The URL the PostgreSQL engine connects with, and backups dump and restore.

    Each of the five components comes from its POSTGRES_* environment variable,
    then POSTGRES_URL, then config.yaml. config.yaml only fills in what the URL
    leaves out: its host and port always hold a value, localhost and 5432 by
    default, and letting those win sent every URL-only install to localhost.
    A URL naming a service gets nothing from config.yaml, since anything set
    beside a service wins over the service file. An environment variable also
    replaces the query parameters for its component, which SQLAlchemy would
    otherwise lay over it.

    Without a URL the five settings are used alone, each environment variable
    replacing its config.yaml key even when it is empty. A URL that names a
    driver keeps it; a bare 'postgresql' one gets psycopg2, as the five
    settings do.
    """
    environ = os.environ if environ is None else environ
    configured = settings.postgresql
    postgres_url = environ.get('POSTGRES_URL', configured.url)

    if not postgres_url:
        return URL.create(
            drivername=POSTGRES_DRIVERNAME,
            **{component: environ.get(variable, getattr(configured, component))
               for component, variable, _ in _COMPONENTS})

    url = make_url(postgres_url)
    backend_name = url.get_backend_name()
    if backend_name != 'postgresql':
        raise ValueError(f"Invalid Postgres URL, scheme must be 'postgresql', got {backend_name}")

    from_service = 'service' in url.query
    overrides = {}
    replaced_query_keys = []
    for component, variable, query_keys in _COMPONENTS:
        if variable in environ:
            # Set but empty, it still keeps config.yaml out, which is what the
            # POSTGRES_HOST= and POSTGRES_PORT= stopgap relied on.
            value = environ[variable]
            if value:
                replaced_query_keys += query_keys
        elif (getattr(url, component) is None and not from_service
              and not any(key in url.query for key in query_keys)):
            value = getattr(configured, component)
        else:
            continue
        if value:
            overrides[component] = value
    url = url.set(**overrides).difference_update_query(replaced_query_keys)
    # The environment's port also replaces the port a query host entry carries
    # inline, which the dialect would otherwise read over it.
    if 'port' in replaced_query_keys:
        url = _query_hosts_without_inline_ports(url)
    if url.drivername == 'postgresql':
        url = url.set(drivername=POSTGRES_DRIVERNAME)
    return url
