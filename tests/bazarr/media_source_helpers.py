"""Shared fixtures and builders for the media source tests: a real database on SQLite and on PostgreSQL."""
import json
import os
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

SOURCE_TABLES = ("media_sources", "media_source_libraries", "media_source_library_roots", "table_source_groups",
                 "table_source_items", "table_source_history", "table_source_blacklist")

BIG = str(2 ** 64 - 1)


def _pg_url():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is not set: the PostgreSQL half of this test did not run")
    return url


def make_engine(backend, tmp_path):
    """Return ``(engine, cleanup)``: the engine as app/database.py builds the real one (AUTOCOMMIT, foreign
    keys on), on a private SQLite file or a private PostgreSQL schema."""
    if backend == "postgresql":
        url = _pg_url()
        schema = "ms_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True,
                                  connect_args={"options": f"-csearch_path={schema}"})

        def cleanup():
            engine.dispose()
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
        return engine, cleanup

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}", poolclass=NullPool,
                              isolation_level="AUTOCOMMIT")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine, engine.dispose


@pytest.fixture(params=["sqlite", "postgresql"])
def backend(request):
    return request.param


@pytest.fixture
def source_db(backend, tmp_path, monkeypatch):
    """A scoped session on a fresh database built from the live ORM metadata, installed as the
    application's ``database`` for the modules that look it up at call time."""
    from app import database as db

    engine, cleanup = make_engine(backend, tmp_path)
    db.Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(db, "database", session)
    # The profile list is cached for a minute across the process.
    db.update_profile_id_list.invalidate()
    try:
        yield session
    finally:
        db.update_profile_id_list.invalidate()
        session.remove()
        cleanup()


def add_profile(session, items, profile_id=1, cutoff=None):
    """A language profile as the settings page stores it. ``items`` are ``(language, forced, hi)`` triples."""
    from app.database import TableLanguagesProfiles, update_profile_id_list
    rows = [{"id": number, "language": language, "audio_exclude": "False", "audio_only_include": "False",
             "forced": forced, "hi": hi} for number, (language, forced, hi) in enumerate(items, start=1)]
    session.execute(sa.insert(TableLanguagesProfiles).values(
        profileId=profile_id, name=f"profile {profile_id}", cutoff=cutoff, items=json.dumps(rows),
        mustContain="[]", mustNotContain="[]", originalFormat=0))
    update_profile_id_list.invalidate()
    return profile_id


def add_source(session, tmp_path, name="Local", folder="Movies", content_kind="movie", profile_id=None):
    """A folder source with one library over ``tmp_path / folder``. Returns ``(source_id, library_id, root)``."""
    from media_sources import repository

    root = tmp_path / folder
    root.mkdir(exist_ok=True)
    source_id = repository.create_source(kind="folder", name=name, target=session)
    library_id = repository.create_library(source_id=source_id, content_kind=content_kind, locator=str(root),
                                           profile_id=profile_id, target=session)
    return source_id, library_id, root


def add_item(session, source_id, library_id, path, kind="movie", group_id=None, **extra):
    from media_sources import repository

    values = dict(source_id=source_id, library_id=library_id, kind=kind, group_id=group_id, path=str(path),
                  title=extra.pop("title", "Item"), identity_source="filename", identity_state="parsed")
    values.update(extra)
    return repository.insert_item(session, **values)


def add_group(session, source_id, library_id, kind="show", title="Show", key=None, **extra):
    from media_sources import repository

    return repository.create_group(source_id=source_id, library_id=library_id, kind=kind,
                                   group_key=key or f"/library/{title}", title=title, target=session, **extra)


def video(path, size=1000):
    """A file that looks like a video to the walk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


@pytest.fixture
def pristine_media_source_modules():
    """A test that replaces ``app.database`` or re-imports a media source module leaves them as it found them.

    ``monkeypatch`` puts the ``sys.modules`` entries back, but an import also sets the module as an attribute of
    its package, and nothing restores that: the next ``from media_sources import repository`` would get the copy
    bound to the stub. So the entries and the package attributes are snapshotted and restored together, and the
    modules first imported inside the window are dropped."""
    import sys

    def mine(name):
        return name == "app.database" or name == "media_sources" or name.startswith("media_sources.")

    before = {name: module for name, module in sys.modules.items() if mine(name)}
    try:
        yield
    finally:
        for name in [name for name in sys.modules if mine(name) and name not in before]:
            del sys.modules[name]
        sys.modules.update(before)
        for name, module in before.items():
            if "." in name:
                parent, child = name.rsplit(".", 1)
                if parent in sys.modules:
                    setattr(sys.modules[parent], child, module)


_stamp = [0]


def stamped_video(path, size=None):
    """A video whose size and modification time no other stamped video shares, so that "the same file" and "a
    different file" are decided by identity and not by two files that happen to look alike."""
    _stamp[0] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * (size or 1000 + _stamp[0]))
    stamp = 1_700_000_000_000_000_000 + _stamp[0] * 1_000_000_007
    os.utime(path, ns=(stamp, stamp))
    return path


class Lib:
    """A series library over a temporary folder, with the few operations the scan tests need."""

    def __init__(self, session, tmp_path, name="TV", folder="TV", content_kind="series"):
        self.session = session
        self.source_id, self.library_id, self.root = add_source(session, tmp_path, name=name, folder=folder,
                                                                content_kind=content_kind)

    def show(self, name, count=3, prefix=None, base=None):
        prefix = prefix or name
        return [stamped_video(self.root / name / f"{prefix} - S01E{number:02d}.mkv")
                for number in range(1, count + 1)]

    def scan(self):
        from media_sources.scanner import run_scan
        (result,) = run_scan(self.source_id, self.library_id)
        return result

    def groups(self):
        from media_sources import repository
        return {row.group_key: row for row in repository.library_groups(self.library_id)}

    def group(self, name):
        return self.groups()[str(self.root / name)]

    def items(self):
        from media_sources import repository
        return {row.path: row for row in repository.list_items(library_id=self.library_id, limit=1000)}

    def path(self, *parts):
        return str(self.root.joinpath(*parts))

    def set_group(self, name, **values):
        from media_sources import repository
        repository.update_group(self.group(name).id, self.session, **values)


def table_counts(session, names):
    return {name: session.execute(sa.text(f'SELECT count(*) FROM "{name}"')).scalar_one() for name in names}


ARR_AND_SPORTS_TABLES = (
    "table_shows", "table_episodes", "table_movies", "table_history", "table_history_movie", "table_blacklist",
    "table_blacklist_movie", "table_sports_leagues", "table_sports_events", "table_history_sports",
    "table_blacklist_sports", "table_sports_file_index", "arr_instances", "release_type_mismatches",
    "table_embedded_track_hidden")


def now():
    return datetime.now()
