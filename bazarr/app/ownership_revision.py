"""Transactional ownership revisions and bounded row change metadata."""

import secrets

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

OWNER_TABLES = ("arr_instances", "table_episodes", "table_movies", "table_sports_events")
REVISION_TABLE = "subtitle_ownership_revision"
CHANGES_TABLE = "subtitle_ownership_changes"


ROW_FUNCTION = """
BEGIN
    UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'Subtitle ownership revision missing'; END IF;
    IF TG_TABLE_NAME = 'arr_instances' THEN
        INSERT INTO subtitle_ownership_changes VALUES ('arr_instances', 0, (SELECT revision FROM subtitle_ownership_revision WHERE id = 1))
        ON CONFLICT (table_name, row_id) DO UPDATE SET revision = EXCLUDED.revision;
    ELSIF TG_OP = 'DELETE' THEN
        DELETE FROM subtitle_ownership_changes WHERE table_name = TG_TABLE_NAME AND row_id = OLD.id;
        INSERT INTO subtitle_ownership_changes VALUES ('*', 0, (SELECT revision FROM subtitle_ownership_revision WHERE id = 1))
        ON CONFLICT (table_name, row_id) DO UPDATE SET revision = EXCLUDED.revision;
    ELSE
        INSERT INTO subtitle_ownership_changes VALUES (TG_TABLE_NAME, NEW.id, (SELECT revision FROM subtitle_ownership_revision WHERE id = 1))
        ON CONFLICT (table_name, row_id) DO UPDATE SET revision = EXCLUDED.revision;
        IF TG_OP = 'UPDATE' AND OLD.id <> NEW.id THEN
            DELETE FROM subtitle_ownership_changes WHERE table_name = TG_TABLE_NAME AND row_id = OLD.id;
            INSERT INTO subtitle_ownership_changes VALUES ('*', 0, (SELECT revision FROM subtitle_ownership_revision WHERE id = 1))
            ON CONFLICT (table_name, row_id) DO UPDATE SET revision = EXCLUDED.revision;
        END IF;
    END IF;
    RETURN NULL;
END
"""
TRUNCATE_FUNCTION = """
BEGIN
    UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'Subtitle ownership revision missing'; END IF;
    DELETE FROM subtitle_ownership_changes WHERE table_name = TG_TABLE_NAME;
    INSERT INTO subtitle_ownership_changes VALUES ('*', 0, (SELECT revision FROM subtitle_ownership_revision WHERE id = 1))
    ON CONFLICT (table_name, row_id) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END
"""


def _sqlite_trigger(table, action):
    name = f"ownership_revision_{table}_{action.lower()}"
    revision = "(SELECT revision FROM subtitle_ownership_revision WHERE id = 1)"
    if table == 'arr_instances':
        changed = f"INSERT INTO subtitle_ownership_changes VALUES ('arr_instances', 0, {revision}) ON CONFLICT (table_name, row_id) DO UPDATE SET revision = excluded.revision;"
    elif action == 'DELETE':
        changed = f"""DELETE FROM subtitle_ownership_changes WHERE table_name = '{table}' AND row_id = OLD.id;
        INSERT INTO subtitle_ownership_changes VALUES ('*', 0, {revision}) ON CONFLICT (table_name, row_id) DO UPDATE SET revision = excluded.revision;"""
    else:
        changed = f"INSERT INTO subtitle_ownership_changes VALUES ('{table}', NEW.id, {revision}) ON CONFLICT (table_name, row_id) DO UPDATE SET revision = excluded.revision;"
        if action == 'UPDATE':
            changed += f"""
            DELETE FROM subtitle_ownership_changes WHERE table_name = '{table}' AND row_id = OLD.id AND OLD.id <> NEW.id;
            INSERT INTO subtitle_ownership_changes SELECT '*', 0, {revision} WHERE OLD.id <> NEW.id
            ON CONFLICT (table_name, row_id) DO UPDATE SET revision = excluded.revision;"""
    return name, f"""CREATE TRIGGER "{name}" AFTER {action} ON "{table}"
    BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM subtitle_ownership_revision WHERE id = 1)
            THEN RAISE(ABORT, 'Subtitle ownership revision missing') END;
        UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1;
        {changed}
    END"""


def _normalized(sql):
    return ' '.join(sql.split()).rstrip(';')


def install_ownership_revision(connection):
    if not set(OWNER_TABLES) <= set(inspect(connection).get_table_names()):
        return
    connection.execute(text("CREATE TABLE IF NOT EXISTS subtitle_ownership_revision (id INTEGER PRIMARY KEY, revision BIGINT NOT NULL)"))
    connection.execute(text("INSERT INTO subtitle_ownership_revision (id, revision) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))
    connection.execute(text("CREATE TABLE IF NOT EXISTS subtitle_ownership_changes (table_name VARCHAR(64) NOT NULL, row_id BIGINT NOT NULL, revision BIGINT NOT NULL, PRIMARY KEY (table_name, row_id))"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_subtitle_ownership_changes_revision ON subtitle_ownership_changes (revision)"))
    connection.execute(text("INSERT INTO subtitle_ownership_changes VALUES ('generation', 0, :generation) ON CONFLICT (table_name, row_id) DO NOTHING"), {'generation': secrets.randbits(63) or 1})
    # Startup and rebuilt tables invalidate any snapshot from the previous schema.
    connection.execute(text("UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1"))
    connection.execute(text("DELETE FROM subtitle_ownership_changes WHERE table_name <> 'generation'"))
    connection.execute(text("INSERT INTO subtitle_ownership_changes SELECT '*', 0, revision FROM subtitle_ownership_revision WHERE id = 1"))
    if connection.dialect.name == 'postgresql':
        for function, body in [('advance_subtitle_ownership_revision', ROW_FUNCTION),
                               ('truncate_subtitle_ownership_revision', TRUNCATE_FUNCTION)]:
            connection.execute(text(f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ {body} $$"))
        for table in OWNER_TABLES:
            connection.execute(text(f'DROP TRIGGER IF EXISTS ownership_revision ON "{table}"'))
            connection.execute(text(f'CREATE TRIGGER ownership_revision AFTER INSERT OR UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION advance_subtitle_ownership_revision()'))
            connection.execute(text(f'DROP TRIGGER IF EXISTS ownership_revision_truncate ON "{table}"'))
            connection.execute(text(f'CREATE TRIGGER ownership_revision_truncate AFTER TRUNCATE ON "{table}" FOR EACH STATEMENT EXECUTE FUNCTION truncate_subtitle_ownership_revision()'))
    elif connection.dialect.name == 'sqlite':
        for table in OWNER_TABLES:
            for action in ('INSERT', 'UPDATE', 'DELETE'):
                name, sql = _sqlite_trigger(table, action)
                connection.execute(text(f'DROP TRIGGER IF EXISTS "{name}"'))
                connection.execute(text(sql))
    else:
        raise ValueError('Unsupported subtitle ownership database')


def metadata_created(metadata, connection, **kwargs):
    install_ownership_revision(connection)


def verify_ownership_protection(session):
    """Check definitions, not just names, before using incremental metadata."""
    if session.get_bind().dialect.name == 'sqlite':
        actual = {name: sql for name, sql in session.execute(text("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))}
        valid = all(_normalized(actual.get(name, '')) == _normalized(sql)
                    for table in OWNER_TABLES for action in ('INSERT', 'UPDATE', 'DELETE')
                    for name, sql in [_sqlite_trigger(table, action)])
    else:
        rows = session.execute(text("""SELECT c.relname, t.tgname, t.tgtype, p.proname, p.prosrc
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE t.tgname IN ('ownership_revision', 'ownership_revision_truncate')
            AND t.tgenabled IN ('O','A') AND n.nspname=current_schema()"""))
        actual = {(table, trigger): (kind, function, _normalized(body))
                  for table, trigger, kind, function, body in rows}
        valid = all(actual.get((table, 'ownership_revision')) == (29, 'advance_subtitle_ownership_revision', _normalized(ROW_FUNCTION))
                    and actual.get((table, 'ownership_revision_truncate')) == (32, 'truncate_subtitle_ownership_revision', _normalized(TRUNCATE_FUNCTION))
                    for table in OWNER_TABLES)
    if not valid:
        raise ValueError('Subtitle ownership protection is unavailable')


def ownership_revision(session):
    try:
        value = session.execute(text("SELECT revision FROM subtitle_ownership_revision WHERE id = 1")).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise ValueError('Subtitle ownership revision is unavailable') from exc
    if value is None:
        raise ValueError('Subtitle ownership revision is unavailable')
    return value


def ownership_token(session):
    try:
        value = session.execute(text("""SELECT r.revision, c.revision
            FROM subtitle_ownership_revision r JOIN subtitle_ownership_changes c
            ON c.table_name='generation' AND c.row_id=0 WHERE r.id=1""")).one_or_none()
    except SQLAlchemyError as exc:
        raise ValueError('Subtitle ownership revision is unavailable') from exc
    if value is None:
        raise ValueError('Subtitle ownership generation is unavailable')
    return tuple(value)


def remove_ownership_revision(connection):
    existing = set(inspect(connection).get_table_names())
    for table in OWNER_TABLES:
        if table not in existing:
            continue
        if connection.dialect.name == 'postgresql':
            for name in ('ownership_revision', 'ownership_revision_truncate'):
                connection.execute(text(f'DROP TRIGGER IF EXISTS {name} ON "{table}"'))
        else:
            for action in ('insert', 'update', 'delete'):
                connection.execute(text(f'DROP TRIGGER IF EXISTS "ownership_revision_{table}_{action}"'))
    if connection.dialect.name == 'postgresql':
        for name in ('advance_subtitle_ownership_revision', 'truncate_subtitle_ownership_revision'):
            connection.execute(text(f'DROP FUNCTION IF EXISTS {name}()'))
    connection.execute(text('DROP TABLE IF EXISTS subtitle_ownership_changes'))
    connection.execute(text('DROP TABLE IF EXISTS subtitle_ownership_revision'))
