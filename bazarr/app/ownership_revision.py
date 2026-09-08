"""Transactional ownership revisions for native and sports media writes."""

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

OWNER_TABLES = (
    "arr_instances",
    "table_episodes",
    "table_movies",
    "table_sports_events",
)
REVISION_TABLE = "subtitle_ownership_revision"


def install_ownership_revision(connection):
    """Install after table creation or rebuilding, without changing media rows."""
    if not set(OWNER_TABLES) <= set(inspect(connection).get_table_names()):
        return
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS subtitle_ownership_revision "
            "(id INTEGER PRIMARY KEY, revision BIGINT NOT NULL)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO subtitle_ownership_revision (id, revision) VALUES (1, 0) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("""
            CREATE OR REPLACE FUNCTION advance_subtitle_ownership_revision()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1;
                IF NOT FOUND THEN RAISE EXCEPTION 'Subtitle ownership revision missing'; END IF;
                RETURN NULL;
            END $$
        """)
        )
        for table in OWNER_TABLES:
            connection.execute(
                text(f'DROP TRIGGER IF EXISTS ownership_revision ON "{table}"')
            )
            connection.execute(
                text(
                    f'CREATE TRIGGER ownership_revision AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON "{table}" '
                    "FOR EACH STATEMENT EXECUTE FUNCTION advance_subtitle_ownership_revision()"
                )
            )
    elif connection.dialect.name == "sqlite":
        for table in OWNER_TABLES:
            for action in ("INSERT", "UPDATE", "DELETE"):
                name = f"ownership_revision_{table}_{action.lower()}"
                connection.execute(
                    text(f"""
                    CREATE TRIGGER IF NOT EXISTS "{name}" AFTER {action} ON "{table}"
                    BEGIN
                        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM subtitle_ownership_revision WHERE id = 1)
                            THEN RAISE(ABORT, 'Subtitle ownership revision missing') END;
                        UPDATE subtitle_ownership_revision SET revision = revision + 1 WHERE id = 1;
                    END
                """)
                )
    else:
        raise ValueError("Unsupported subtitle ownership database")


def metadata_created(metadata, connection, **kwargs):
    install_ownership_revision(connection)


def verify_ownership_protection(session):
    """Catalog checks happen during preparation, before any writer locks."""
    if session.get_bind().dialect.name == "sqlite":
        expected = {
            f"ownership_revision_{table}_{action}"
            for table in OWNER_TABLES
            for action in ("insert", "update", "delete")
        }
        actual = set(
            session.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            ).scalars()
        )
    else:
        expected = set(OWNER_TABLES)
        actual = set(
            session.execute(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE t.tgname='ownership_revision' AND t.tgenabled IN ('O','A') "
                    "AND n.nspname=current_schema()"
                )
            ).scalars()
        )
    if not expected <= actual:
        raise ValueError("Subtitle ownership protection is unavailable")


def ownership_revision(session):
    try:
        value = session.execute(
            text("SELECT revision FROM subtitle_ownership_revision WHERE id = 1")
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise ValueError("Subtitle ownership revision is unavailable") from exc
    if value is None:
        raise ValueError("Subtitle ownership revision is unavailable")
    return value


def remove_ownership_revision(connection):
    existing = set(inspect(connection).get_table_names())
    for table in OWNER_TABLES:
        if table not in existing:
            continue
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(f'DROP TRIGGER IF EXISTS ownership_revision ON "{table}"')
            )
        else:
            for action in ("insert", "update", "delete"):
                connection.execute(
                    text(
                        f'DROP TRIGGER IF EXISTS "ownership_revision_{table}_{action}"'
                    )
                )
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("DROP FUNCTION IF EXISTS advance_subtitle_ownership_revision()")
        )
    connection.execute(text("DROP TABLE IF EXISTS subtitle_ownership_revision"))
