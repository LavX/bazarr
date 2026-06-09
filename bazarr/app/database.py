# -*- coding: utf-8 -*-
import ast
import atexit
import json
import logging
import os
import sqlite3
import flask_migrate

from dogpile.cache import make_region
from datetime import datetime

from sqlalchemy import create_engine, inspect, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, Text, func, text, BigInteger
# importing here to be indirectly imported in other modules later
from sqlalchemy import update, delete, select, func  # noqa: F401, F811
from sqlalchemy.orm import scoped_session, sessionmaker, mapped_column, close_all_sessions, declarative_base
from sqlalchemy.pool import NullPool
from alembic.migration import MigrationContext

from flask_sqlalchemy import SQLAlchemy

from .config import settings
from .get_args import args

logger = logging.getLogger(__name__)

POSTGRES_ENABLED_ENV = os.getenv("POSTGRES_ENABLED")
if POSTGRES_ENABLED_ENV:
    postgresql = POSTGRES_ENABLED_ENV.lower() == 'true'
else:
    postgresql = settings.postgresql.enabled

# Single-entry cache for update_profile_id_list(). No LRU bound is needed
# (one key only). The 60s TTL is a safety net so a missed manual
# invalidation does not pin stale profile data forever.
region = make_region().configure(
    'dogpile.cache.memory',
    expiration_time=60,
)

migrations_directory = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'migrations')


def configure_sqlite_connection(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=60000")
    finally:
        cursor.close()


def optimize_sqlite_database(engine_to_optimize):
    if engine_to_optimize.dialect.name != 'sqlite':
        return False
    if sqlite3.sqlite_version_info < (3, 46, 0):
        logger.debug("Skipping PRAGMA optimize on SQLite %s", sqlite3.sqlite_version)
        return False

    try:
        with engine_to_optimize.connect() as connection:
            connection.execute(text("PRAGMA optimize"))
    except Exception:
        logger.exception("Unable to run SQLite PRAGMA optimize.")
        return False
    return True


def log_sqlite_runtime_version(engine_to_log):
    if engine_to_log.dialect.name != 'sqlite':
        return False
    logging.info("SQLite runtime version: %s", sqlite3.sqlite_version)
    return True


if postgresql:
    # insert is different between database types
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.engine import URL, make_url

    postgres_database = os.getenv("POSTGRES_DATABASE", settings.postgresql.database)
    postgres_username = os.getenv("POSTGRES_USERNAME", settings.postgresql.username)
    postgres_password = os.getenv("POSTGRES_PASSWORD", settings.postgresql.password)
    postgres_host = os.getenv("POSTGRES_HOST", settings.postgresql.host)
    postgres_port = os.getenv("POSTGRES_PORT", settings.postgresql.port)
    postgres_url = os.getenv("POSTGRES_URL", settings.postgresql.url)

    if postgres_url:
        url = make_url(postgres_url)
        backend_name = url.get_backend_name()
        if backend_name != 'postgresql':
            raise ValueError(f"Invalid Postgres URL, scheme must be 'postgresql', got {backend_name}")
        
        # Allow overriding individual components of the URL
        url_overrides = {
            'username': postgres_username if postgres_username else None,
            'password': postgres_password if postgres_password else None,
            'host': postgres_host if postgres_host else None,
            'port': postgres_port if postgres_port else None,
            'database': postgres_database if postgres_database else None,
        }
        url = url.set(**{k: v for k, v in url_overrides.items()})
    else:
        url = URL.create(
            drivername="postgresql",
            username=postgres_username,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port,
            database=postgres_database
        )
    # Build the log message from individual non-secret components instead of
    # going through `url`. SQLAlchemy's render_as_string(hide_password=True)
    # masks the password at render time, but the URL object still carries the
    # password value, which trips CodeQL's py/clear-text-logging-sensitive-data
    # because that masking call is not recognised as a sanitizer.
    log_user = postgres_username or "<default>"
    log_host = postgres_host or "<default>"
    log_port = postgres_port or "<default>"
    log_db = postgres_database or "<default>"
    if postgres_url:
        log_db = f"{log_db} (via POSTGRES_URL)"
    logger.debug(
        "Connecting to PostgreSQL database: postgresql://%s@%s:%s/%s",
        log_user, log_host, log_port, log_db,
    )

    # Postgres: use SQLAlchemy's default QueuePool. NullPool would force a
    # fresh TCP+TLS handshake for every database.execute(...) call (~266
    # callsites), which is catastrophic on Postgres. pool_pre_ping issues a
    # cheap SELECT 1 before handing out a checked-out connection so stale
    # connections (server-side timeout, network blip, db restart) are
    # transparently recycled instead of surfacing as OperationalError.
    # pool_recycle=1800 proactively retires connections after 30 minutes,
    # which is below typical idle-timeout ceilings on managed Postgres.
    engine = create_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        isolation_level="AUTOCOMMIT",
    )
else:
    # insert is different between database types
    from sqlalchemy.dialects.sqlite import insert
    url = f'sqlite:///{os.path.join(args.config_dir, "db", "bazarr.db")}'
    logger.debug(f"Connecting to SQLite database: {url}")  # noqa: G004
    # SQLite: keep NullPool. SQLite's single-writer file-lock model produces
    # "database is locked" errors when connections are pooled and shared
    # across threads, so the safest pattern is one fresh connection per
    # statement and let the WAL / busy_timeout PRAGMAs below absorb
    # contention. Do NOT switch this to QueuePool.
    engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")

    from sqlalchemy.engine import Engine
    from sqlalchemy import event

    event.listen(Engine, "connect", configure_sqlite_connection)

# Dev-only slow-query log. Gated by BAZARR_SQL_PROFILE env var; this
# is a cheap function call and a hard early-return when disabled, so
# we wire it once for both engines without branching.
from utilities.sql_profiler import install_slow_query_log  # noqa: E402
install_slow_query_log(engine)

# sessionmaker defaults are wrong for this codebase's access pattern.
# autoflush=False: bazarr writes through Core insert() / update() / delete()
# constructs, never via session.add(); there is nothing for the session to
# auto-flush, and leaving autoflush on would silently flush any future ORM
# mutation right before unrelated SELECTs run, which is a surprise vector.
# expire_on_commit=False: the engine runs in AUTOCOMMIT, so every statement
# commits on its own. With the SQLAlchemy default (expire_on_commit=True),
# every ORM instance returned by the session would be marked expired the
# instant its statement commits, forcing an implicit re-SELECT the next
# time any attribute is accessed. Disabling preserves the loaded values
# for the natural lifetime of the consuming code.
session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
database = scoped_session(session_factory)


def close_database():
    close_all_sessions()
    engine.dispose()


@atexit.register
def _stop_worker_threads():
    database.remove()

Base = declarative_base()
metadata = Base.metadata


class System(Base):
    __tablename__ = 'system'

    id = mapped_column(Integer, primary_key=True)
    configured = mapped_column(Text)
    updated = mapped_column(Text)


class TableAnnouncements(Base):
    __tablename__ = 'table_announcements'

    id = mapped_column(Integer, primary_key=True)
    timestamp = mapped_column(DateTime, nullable=False, default=datetime.now)
    hash = mapped_column(Text)
    text = mapped_column(Text)


class TableArrInstances(Base):
    # Multiple Sonarr/Radarr instances (#156). One Bazarr+ install can connect
    # to several named Sonarr/Radarr instances - e.g. split libraries: TV,
    # anime, 4K. Bazarr local IDs are canonical; upstream Sonarr/Radarr IDs
    # become per-instance metadata scoped by arr_instance_id on owned rows.
    #
    # api_key holds an ENCRYPTED payload (secret_store.crypto.encrypt_secret,
    # 'enc:v1:' marker), encrypted/decrypted at the ArrInstanceRepository
    # boundary: arr_instances lives outside config.yaml and does not inherit
    # its Fernet-at-rest encryption.
    __tablename__ = 'arr_instances'
    __table_args__ = (
        CheckConstraint("kind IN ('sonarr', 'radarr')", name='ck_arr_instances_kind'),
        CheckConstraint("enabled IN (0, 1)", name='ck_arr_instances_enabled'),
        CheckConstraint("is_default IN (0, 1)", name='ck_arr_instances_is_default'),
        CheckConstraint("is_default = 0 OR enabled = 1", name='ck_arr_instances_default_enabled'),
        Index('ux_arr_instances_kind_stable_key', 'kind', 'stable_key', unique=True),
        # At most one enabled default per kind. Partial unique index so disabled
        # or non-default rows never collide. SQLite and PostgreSQL both honour
        # the WHERE predicate; backends without partial-index support fall back
        # to repository/API-level enforcement.
        Index('ux_arr_instances_default_kind', 'kind', unique=True,
              sqlite_where=text('is_default = 1 AND enabled = 1'),
              postgresql_where=text('is_default = 1 AND enabled = 1')),
    )

    id = mapped_column(Integer, primary_key=True)
    kind = mapped_column(Text, nullable=False)
    stable_key = mapped_column(Text, nullable=False)
    name = mapped_column(Text, nullable=False)
    enabled = mapped_column(Integer, nullable=False, default=1)
    is_default = mapped_column(Integer, nullable=False, default=0)
    # connection - mirrors the scalar settings.sonarr / settings.radarr fields
    ip = mapped_column(Text, nullable=False, default='127.0.0.1')
    port = mapped_column(Integer, nullable=False)
    base_url = mapped_column(Text, nullable=False, default='/')
    ssl = mapped_column(Integer, nullable=False, default=0)
    verify_ssl = mapped_column(Integer, nullable=False, default=0)
    http_timeout = mapped_column(Integer, nullable=False, default=60)
    api_key = mapped_column(Text, nullable=False, default='')  # encrypted at rest
    # per-instance JSON config blobs
    options = mapped_column(Text)        # sync / exclusion options
    path_mappings = mapped_column(Text)  # per-instance remote->local path map
    schedule = mapped_column(Text)       # full_update / sync cadence
    # runtime status
    status = mapped_column(Text)
    last_error = mapped_column(Text)
    last_sync_at = mapped_column(DateTime)
    # timestamps
    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at = mapped_column(DateTime, nullable=False, default=datetime.now)

    def to_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TableCompatApiKeys(Base):
    # Distribution Hub: named API keys for the OpenSubtitles-compat endpoint.
    # The full token is never stored - only its sha256 (key_hash) and an
    # 8-char prefix for UI identification. The legacy shared
    # compat_endpoint.token is migrated in as an is_legacy Unlimited row.
    __tablename__ = 'compat_api_keys'

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(Text, nullable=False)
    key_prefix = mapped_column(Text)
    key_hash = mapped_column(Text, nullable=False, unique=True, index=True)
    tier = mapped_column(Text, nullable=False, default='free')
    custom_limits = mapped_column(Text)            # JSON dict or NULL = inherit tier
    excluded_providers = mapped_column(Text)       # JSON list or NULL = drop these
    allowed_providers = mapped_column(Text)        # JSON list or NULL = allow all (only these)
    timeout_seconds = mapped_column(Integer)       # NULL = inherit global
    enabled = mapped_column(Integer, nullable=False, default=1)
    is_legacy = mapped_column(Integer, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)
    last_used_at = mapped_column(DateTime)
    note = mapped_column(Text)


class TableCompatUsage(Base):
    # Distribution Hub: hourly-bucketed per-key usage counters. Source of
    # truth for both rate-limit window sums and usage statistics. One row
    # per (key_id, kind, hour_start); upserted on each metered request.
    __tablename__ = 'compat_usage'
    __table_args__ = (
        Index('ix_compat_usage_key_kind_hour', 'key_id', 'kind', 'hour_start',
              unique=True),
    )

    id = mapped_column(Integer, primary_key=True)
    key_id = mapped_column(Integer, nullable=False, index=True)
    kind = mapped_column(Text, nullable=False)        # 'search' | 'download'
    hour_start = mapped_column(DateTime, nullable=False, index=True)
    count = mapped_column(Integer, nullable=False, default=0)
    blocked = mapped_column(Integer, nullable=False, default=0)


class TableBlacklist(Base):
    __tablename__ = 'table_blacklist'

    id = mapped_column(Integer, primary_key=True)
    language = mapped_column(Text)
    provider = mapped_column(Text)
    sonarr_episode_id = mapped_column(Integer, ForeignKey('table_episodes.sonarrEpisodeId', ondelete='CASCADE'))
    sonarr_series_id = mapped_column(Integer, ForeignKey('table_shows.sonarrSeriesId', ondelete='CASCADE'))
    subs_id = mapped_column(Text, index=True)
    timestamp = mapped_column(DateTime, default=datetime.now)


class TableBlacklistMovie(Base):
    __tablename__ = 'table_blacklist_movie'

    id = mapped_column(Integer, primary_key=True)
    language = mapped_column(Text)
    provider = mapped_column(Text)
    radarr_id = mapped_column(Integer, ForeignKey('table_movies.radarrId', ondelete='CASCADE'))
    subs_id = mapped_column(Text, index=True)
    timestamp = mapped_column(DateTime, default=datetime.now)


class TableEpisodes(Base):
    __tablename__ = 'table_episodes'

    absoluteEpisode = mapped_column(Integer)
    audio_codec = mapped_column(Text)
    audio_language = mapped_column(Text)
    created_at_timestamp = mapped_column(DateTime)
    episode = mapped_column(Integer, nullable=False)
    episode_file_id = mapped_column(Integer, index=True)
    failedAttempts = mapped_column(Text)
    ffprobe_cache = mapped_column(LargeBinary)
    file_size = mapped_column(BigInteger)
    format = mapped_column(Text)
    missing_subtitles = mapped_column(Text)
    monitored = mapped_column(Text)
    path = mapped_column(Text, nullable=False)
    resolution = mapped_column(Text)
    sceneName = mapped_column(Text)
    season = mapped_column(Integer, nullable=False)
    sonarrEpisodeId = mapped_column(Integer, primary_key=True)
    sonarrSeriesId = mapped_column(Integer, ForeignKey('table_shows.sonarrSeriesId', ondelete='CASCADE'), index=True)
    subtitles = mapped_column(Text)
    title = mapped_column(Text, nullable=False)
    tvdbId = mapped_column(Integer)
    updated_at_timestamp = mapped_column(DateTime)
    video_codec = mapped_column(Text)

    def to_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TableHistory(Base):
    __tablename__ = 'table_history'
    __table_args__ = (
        Index('ix_table_history_video_path_language_timestamp',
              'video_path', 'language', 'timestamp'),
    )

    id = mapped_column(Integer, primary_key=True)
    action = mapped_column(Integer, nullable=False, index=True)
    description = mapped_column(Text, nullable=False)
    language = mapped_column(Text)
    provider = mapped_column(Text)
    score = mapped_column(Integer)
    score_out_of = mapped_column(Integer, nullable=True)
    sonarrEpisodeId = mapped_column(Integer, ForeignKey('table_episodes.sonarrEpisodeId', ondelete='CASCADE'), index=True)
    sonarrSeriesId = mapped_column(Integer, ForeignKey('table_shows.sonarrSeriesId', ondelete='CASCADE'), index=True)
    subs_id = mapped_column(Text)
    subtitles_path = mapped_column(Text)
    timestamp = mapped_column(DateTime, nullable=False, default=datetime.now)
    video_path = mapped_column(Text)
    matched = mapped_column(Text)
    not_matched = mapped_column(Text)
    upgradedFromId = mapped_column(Integer, ForeignKey('table_history.id'))


class TableHistoryMovie(Base):
    __tablename__ = 'table_history_movie'
    __table_args__ = (
        Index('ix_table_history_movie_video_path_language_timestamp',
              'video_path', 'language', 'timestamp'),
    )

    id = mapped_column(Integer, primary_key=True)
    action = mapped_column(Integer, nullable=False, index=True)
    description = mapped_column(Text, nullable=False)
    language = mapped_column(Text)
    provider = mapped_column(Text)
    radarrId = mapped_column(Integer, ForeignKey('table_movies.radarrId', ondelete='CASCADE'), index=True)
    score = mapped_column(Integer)
    score_out_of = mapped_column(Integer, nullable=True)
    subs_id = mapped_column(Text)
    subtitles_path = mapped_column(Text)
    timestamp = mapped_column(DateTime, nullable=False, default=datetime.now)
    video_path = mapped_column(Text)
    matched = mapped_column(Text)
    not_matched = mapped_column(Text)
    upgradedFromId = mapped_column(Integer, ForeignKey('table_history_movie.id'))


class TableLanguagesProfiles(Base):
    __tablename__ = 'table_languages_profiles'

    profileId = mapped_column(Integer, primary_key=True)
    cutoff = mapped_column(Integer)
    originalFormat = mapped_column(Integer)
    items = mapped_column(Text, nullable=False)
    name = mapped_column(Text, nullable=False)
    mustContain = mapped_column(Text)
    mustNotContain = mapped_column(Text)
    tag = mapped_column(Text)
    combine = mapped_column(Text)


class TableMovies(Base):
    __tablename__ = 'table_movies'

    alternativeTitles = mapped_column(Text)
    audio_codec = mapped_column(Text)
    audio_language = mapped_column(Text)
    created_at_timestamp = mapped_column(DateTime)
    failedAttempts = mapped_column(Text)
    fanart = mapped_column(Text)
    ffprobe_cache = mapped_column(LargeBinary)
    file_size = mapped_column(BigInteger)
    format = mapped_column(Text)
    imdbId = mapped_column(Text)
    missing_subtitles = mapped_column(Text)
    monitored = mapped_column(Text)
    movie_file_id = mapped_column(Integer)
    originalLanguage = mapped_column(Text)
    overview = mapped_column(Text)
    path = mapped_column(Text, nullable=False, unique=True)
    poster = mapped_column(Text)
    profileId = mapped_column(Integer, ForeignKey('table_languages_profiles.profileId', ondelete='SET NULL'), index=True)
    radarrId = mapped_column(Integer, primary_key=True)
    resolution = mapped_column(Text)
    sceneName = mapped_column(Text)
    sortTitle = mapped_column(Text)
    subtitles = mapped_column(Text)
    tags = mapped_column(Text)
    title = mapped_column(Text, nullable=False)
    tmdbId = mapped_column(Text, nullable=False, unique=True)
    updated_at_timestamp = mapped_column(DateTime)
    video_codec = mapped_column(Text)
    year = mapped_column(Text)

    def to_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TableMoviesRootfolder(Base):
    __tablename__ = 'table_movies_rootfolder'

    accessible = mapped_column(Integer)
    error = mapped_column(Text)
    id = mapped_column(Integer, primary_key=True)
    path = mapped_column(Text)


class TableSubsyncEngineFailure(Base):
    __tablename__ = 'subsync_engine_failures'
    __table_args__ = (
        Index('ix_subsync_engine_failures_path_engine', 'subtitle_path', 'engine', unique=True),
    )

    id = mapped_column(Integer, primary_key=True)
    subtitle_path = mapped_column(Text, nullable=False)
    engine = mapped_column(Text, nullable=False)
    consecutive_failures = mapped_column(Integer, nullable=False, default=0)
    is_skipped = mapped_column(Integer, nullable=False, default=0)
    last_error = mapped_column(Text)
    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at = mapped_column(DateTime, nullable=False, default=datetime.now)


class TableSettingsLanguages(Base):
    __tablename__ = 'table_settings_languages'

    code3 = mapped_column(Text, primary_key=True)
    code2 = mapped_column(Text)
    code3b = mapped_column(Text)
    enabled = mapped_column(Integer)
    name = mapped_column(Text, nullable=False)


class TableSettingsNotifier(Base):
    __tablename__ = 'table_settings_notifier'

    name = mapped_column(Text, primary_key=True)
    enabled = mapped_column(Integer)
    url = mapped_column(Text)


class TableShows(Base):
    __tablename__ = 'table_shows'

    tvdbId = mapped_column(Integer)
    alternativeTitles = mapped_column(Text)
    audio_language = mapped_column(Text)
    created_at_timestamp = mapped_column(DateTime)
    ended = mapped_column(Text)
    fanart = mapped_column(Text)
    imdbId = mapped_column(Text)
    lastAired = mapped_column(Text)
    monitored = mapped_column(Text)
    originalLanguage = mapped_column(Text)
    overview = mapped_column(Text)
    path = mapped_column(Text, nullable=False, unique=True)
    poster = mapped_column(Text)
    profileId = mapped_column(Integer, ForeignKey('table_languages_profiles.profileId', ondelete='SET NULL'), index=True)
    seriesType = mapped_column(Text)
    sonarrSeriesId = mapped_column(Integer, primary_key=True)
    sortTitle = mapped_column(Text)
    tags = mapped_column(Text)
    title = mapped_column(Text, nullable=False)
    updated_at_timestamp = mapped_column(DateTime)
    year = mapped_column(Text)

    def to_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TableShowsRootfolder(Base):
    __tablename__ = 'table_shows_rootfolder'

    accessible = mapped_column(Integer)
    error = mapped_column(Text)
    id = mapped_column(Integer, primary_key=True)
    path = mapped_column(Text)


class TableProviderHubCatalogSource(Base):
    __tablename__ = 'provider_hub_catalog_sources'

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(Text, nullable=False, unique=True)
    type = mapped_column(Text, nullable=False)
    url = mapped_column(Text, nullable=False)
    enabled = mapped_column(Integer, nullable=False, default=1)
    trust_key = mapped_column(Text)
    etag = mapped_column(Text)
    last_checked_at = mapped_column(DateTime)
    last_error = mapped_column(Text)


class TableProviderHubCatalogEntry(Base):
    __tablename__ = 'provider_hub_catalog_entries'

    id = mapped_column(Integer, primary_key=True)
    source_id = mapped_column(Integer, ForeignKey('provider_hub_catalog_sources.id', ondelete='CASCADE'), index=True)
    provider_id = mapped_column(Text, nullable=False, index=True)
    version = mapped_column(Text, nullable=False)
    manifest_json = mapped_column(Text, nullable=False)
    bundle_url = mapped_column(Text)
    sha256 = mapped_column(Text)
    signature = mapped_column(Text)
    compatibility = mapped_column(Text)
    published_at = mapped_column(DateTime)
    deprecated = mapped_column(Integer, nullable=False, default=0)


class TableProviderHubInstallation(Base):
    __tablename__ = 'provider_hub_installations'

    provider_id = mapped_column(Text, primary_key=True)
    active_version = mapped_column(Text)
    staged_version = mapped_column(Text)
    active_path = mapped_column(Text)
    staged_path = mapped_column(Text)
    state = mapped_column(Text, nullable=False, default='inactive')
    pending_restart = mapped_column(Integer, nullable=False, default=0)
    installed_at = mapped_column(DateTime)
    activated_at = mapped_column(DateTime)
    last_error = mapped_column(Text)
    manifest_json = mapped_column(Text)


class TableProviderHubConfig(Base):
    __tablename__ = 'provider_hub_config'

    provider_id = mapped_column(Text, primary_key=True)
    enabled = mapped_column(Integer, nullable=False, default=0)
    priority = mapped_column(Integer)
    config_json = mapped_column(Text, nullable=False, default='{}')
    schema_version = mapped_column(Integer, nullable=False, default=1)
    updated_at = mapped_column(DateTime, nullable=False, default=datetime.now)


class TableProviderHubSecret(Base):
    __tablename__ = 'provider_hub_secrets'

    id = mapped_column(Integer, primary_key=True)
    provider_id = mapped_column(Text, nullable=False, index=True)
    field = mapped_column(Text, nullable=False)
    encrypted_value = mapped_column(Text, nullable=False)
    updated_at = mapped_column(DateTime, nullable=False, default=datetime.now)


class TableProviderHubJob(Base):
    __tablename__ = 'provider_hub_jobs'

    id = mapped_column(Text, primary_key=True)
    provider_id = mapped_column(Text, index=True)
    action = mapped_column(Text, nullable=False)
    state = mapped_column(Text, nullable=False)
    message = mapped_column(Text)
    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at = mapped_column(DateTime, nullable=False, default=datetime.now)


class TableProviderHubInstallEvent(Base):
    __tablename__ = 'provider_hub_install_events'

    id = mapped_column(Integer, primary_key=True)
    provider_id = mapped_column(Text, nullable=False, index=True)
    job_id = mapped_column(Text, index=True)
    action = mapped_column(Text, nullable=False)
    state = mapped_column(Text, nullable=False)
    message = mapped_column(Text)
    created_at = mapped_column(DateTime, nullable=False, default=datetime.now)


def init_db():
    # Idempotent: bazarr can end up importing `init` under both `bazarr.init`
    # and `init` aliases when tests cross module-namespace boundaries
    # (compat tests sometimes load via the parent-package path; everything
    # else uses the canonical sys.path-rooted path post-encryption-pass).
    # Each fresh import re-runs init.py which calls init_db, but the
    # underlying `database` is a scoped_session shared across instances,
    # so the second begin() raises "A transaction is already begun on
    # this Session". scoped_session proxies don't expose in_transaction
    # so we catch sqlalchemy's own signal directly.
    from sqlalchemy.exc import InvalidRequestError
    try:
        database.begin()
    except InvalidRequestError:
        # Already in a transaction from a previous init_db (likely
        # via a duplicate module import path). Safe to skip; the
        # metadata create_all below is itself idempotent.
        pass

    # Create tables if they don't exist.
    metadata.create_all(engine)

    # Resolve the DB engine/version and current migration revision once, at startup, and
    # stash them in env vars for /system/status to read. The status endpoint used to open a
    # fresh engine.connect() on every request to read the migration revision and never closed
    # it; on Postgres (QueuePool) that leaked a connection per call until the pool was
    # exhausted and requests blocked on pool_timeout (upstream #3361). SQLite was merely
    # wasteful. Computed here so the leak is gone and the values are cheap to serve.
    try:
        database_engine = engine.dialect.name.capitalize()
    except Exception:
        logger.exception("Failed to determine database engine type")
        database_engine = ""

    try:
        database_version = ".".join(str(x) for x in (engine.dialect.server_version_info or ()))
    except Exception:
        logger.exception("Failed to determine database engine version")
        database_version = ""

    os.environ["BAZARR_DB_ENGINE_VERSION"] = f"{database_engine} {database_version}"

    try:
        with engine.connect() as connection:
            migration_revision = MigrationContext.configure(connection).get_current_revision()
    except Exception:
        logger.exception("Failed to determine database migration revision")
        migration_revision = "unknown"
    os.environ["BAZARR_DB_MIGRATION_VERSION"] = migration_revision or "unknown"


def create_db_revision(app):
    logging.info("Creating a new database revision for future migration")
    app.config["SQLALCHEMY_DATABASE_URI"] = url
    db = SQLAlchemy(app, metadata=metadata)
    with app.app_context():
        flask_migrate.Migrate(app, db, render_as_batch=True)
        flask_migrate.migrate(directory=migrations_directory)
        db.engine.dispose()


def migrate_db(app):
    log_sqlite_runtime_version(engine)
    logging.debug("Upgrading database schema")
    app.config["SQLALCHEMY_DATABASE_URI"] = url
    db = SQLAlchemy(app, metadata=metadata)

    insp = inspect(engine)
    alembic_temp_tables_list = [x for x in insp.get_table_names() if x.startswith('_alembic_tmp_')]
    for table in alembic_temp_tables_list:
        database.execute(text(f"DROP TABLE IF EXISTS {table}"))

    with app.app_context():
        flask_migrate.Migrate(app, db, render_as_batch=True)
        flask_migrate.upgrade(directory=migrations_directory)
        db.engine.dispose()

    # add the system table single row if it's not existing
    if not database.execute(
            select(System)) \
            .first():
        database.execute(
            insert(System)
            .values(configured='0', updated='0'))
    optimize_sqlite_database(engine)

    # Refresh the cached migration revision now that pending migrations have run. init_db()
    # caches it right after create_all (before this upgrade), so a boot with a pending
    # migration would otherwise leave /system/status reporting the pre-upgrade revision until
    # the next restart. migrate_db() runs before the webserver starts, so this stays current.
    try:
        with engine.connect() as connection:
            os.environ["BAZARR_DB_MIGRATION_VERSION"] = (
                MigrationContext.configure(connection).get_current_revision() or "unknown"
            )
    except Exception:
        logger.exception("Failed to refresh database migration revision after upgrade")


def get_exclusion_clause(exclusion_type):
    where_clause = []
    if exclusion_type == 'series':
        tagsList = settings.sonarr.excluded_tags
        for tag in tagsList:
            where_clause.append(~(TableShows.tags.contains(f"\'{tag}\'")))  # noqa: PERF401
    else:
        tagsList = settings.radarr.excluded_tags
        for tag in tagsList:
            where_clause.append(~(TableMovies.tags.contains(f"\'{tag}\'")))  # noqa: PERF401

    if exclusion_type == 'series':
        monitoredOnly = settings.sonarr.only_monitored
        if monitoredOnly:
            where_clause.append((TableEpisodes.monitored == 'True'))
            where_clause.append((TableShows.monitored == 'True'))
    else:
        monitoredOnly = settings.radarr.only_monitored
        if monitoredOnly:
            where_clause.append((TableMovies.monitored == 'True'))

    if exclusion_type == 'series':
        typesList = settings.sonarr.excluded_series_types
        for item in typesList:
            where_clause.append((TableShows.seriesType != item))  # noqa: PERF401

        exclude_season_zero = settings.sonarr.exclude_season_zero
        if exclude_season_zero:
            where_clause.append((TableEpisodes.season != 0))

    return where_clause


@region.cache_on_arguments()
def update_profile_id_list():
    return [{
        'profileId': x.profileId,
        'name': x.name,
        'cutoff': x.cutoff,
        'items': json.loads(x.items),
        'mustContain': ast.literal_eval(x.mustContain) if x.mustContain else [],
        'mustNotContain': ast.literal_eval(x.mustNotContain) if x.mustNotContain else [],
        'originalFormat': x.originalFormat,
        'tag': x.tag,
        'combine': json.loads(x.combine) if x.combine else None,
    } for x in database.execute(
        select(TableLanguagesProfiles.profileId,
               TableLanguagesProfiles.name,
               TableLanguagesProfiles.cutoff,
               TableLanguagesProfiles.items,
               TableLanguagesProfiles.mustContain,
               TableLanguagesProfiles.mustNotContain,
               TableLanguagesProfiles.originalFormat,
               TableLanguagesProfiles.tag,
               TableLanguagesProfiles.combine))
        .all()
    ]


def get_profiles_list(profile_id=None):
    profile_id_list = update_profile_id_list()

    if profile_id and profile_id != 'null':
        for profile in profile_id_list:
            if profile['profileId'] == profile_id:
                return profile
    else:
        return profile_id_list


def get_desired_languages(profile_id):
    for profile in update_profile_id_list():
        if profile['profileId'] == profile_id:
            return [x['language'] for x in profile['items']]


def get_profile_id_name(profile_id):
    for profile in update_profile_id_list():
        if profile['profileId'] == profile_id:
            return profile['name']


def get_profile_cutoff(profile_id):
    cutoff_language = None
    profile_id_list = update_profile_id_list()

    if profile_id and profile_id != 'null':
        cutoff_language = []
        for profile in profile_id_list:
            # Access by key, not positional .values() unpacking: the profile
            # dict can grow new keys (e.g. 'combine') and a fixed-arity unpack
            # would raise ValueError for every profile during indexing.
            profileId = profile['profileId']
            cutoff = profile['cutoff']
            items = profile['items']
            if cutoff:
                if profileId == int(profile_id):
                    for item in items:
                        if item['id'] == cutoff:
                            return [item]
                        elif cutoff == 65535:
                            cutoff_language.append(item)

        if not len(cutoff_language):
            cutoff_language = None

    return cutoff_language


def get_audio_profile_languages(audio_languages_list_str):
    from languages.get_languages import alpha2_from_language, alpha3_from_language, language_from_alpha2
    audio_languages = []

    und_default_language = language_from_alpha2(settings.general.default_und_audio_lang)

    try:
        audio_languages_list = ast.literal_eval(audio_languages_list_str or '[]')
    except ValueError:
        pass
    else:
        for language in audio_languages_list:
            if language:
                audio_languages.append(
                    {"name": language,
                     "code2": alpha2_from_language(language) or None,
                     "code3": alpha3_from_language(language) or None}
                )
            else:
                if und_default_language:
                    logging.debug(f"Undefined language audio track treated as {und_default_language}")  # noqa: G004
                    audio_languages.append(
                        {"name": und_default_language,
                         "code2": alpha2_from_language(und_default_language) or None,
                         "code3": alpha3_from_language(und_default_language) or None}
                    )

    return audio_languages


def get_profile_id(series_id=None, episode_id=None, movie_id=None):
    if series_id:
        data = database.execute(
            select(TableShows.profileId)
            .where(TableShows.sonarrSeriesId == series_id))\
            .first()
        if data:
            return data.profileId
    elif episode_id:
        data = database.execute(
            select(TableShows.profileId)
            .select_from(TableShows)
            .join(TableEpisodes)
            .where(TableEpisodes.sonarrEpisodeId == episode_id)) \
            .first()
        if data:
            return data.profileId

    elif movie_id:
        data = database.execute(
            select(TableMovies.profileId)
            .where(TableMovies.radarrId == movie_id))\
            .first()
        if data:
            return data.profileId

    return None


def convert_list_to_clause(arr: list):
    if isinstance(arr, list):
        return f"({','.join(str(x) for x in arr)})"
    else:
        return ""


def upgrade_languages_profile_values():
    for languages_profile in (database.execute(
            select(
                TableLanguagesProfiles.profileId,
                TableLanguagesProfiles.name,
                TableLanguagesProfiles.cutoff,
                TableLanguagesProfiles.items,
                TableLanguagesProfiles.mustContain,
                TableLanguagesProfiles.mustNotContain,
                TableLanguagesProfiles.originalFormat,
                TableLanguagesProfiles.tag)
            ))\
            .all():
        items = json.loads(languages_profile.items)
        for language in items:
            if language['hi'] == "only":
                language['hi'] = "True"
            elif language['hi'] in ["also", "never"]:
                language['hi'] = "False"

            if 'audio_exclude' not in language:
                language['audio_exclude'] = "False"

            if 'audio_only_include' not in language:
                language['audio_only_include'] = "False"

            if "translate_from" not in language:
                language["translate_from"] = None
        database.execute(
            update(TableLanguagesProfiles)
            .values({"items": json.dumps(items)})
            .where(TableLanguagesProfiles.profileId == languages_profile.profileId)
        )


def fix_languages_profiles_with_duplicate_ids():
    languages_profiles = database.execute(
        select(TableLanguagesProfiles.profileId, TableLanguagesProfiles.items, TableLanguagesProfiles.cutoff)).all()
    for languages_profile in languages_profiles:
        if languages_profile.cutoff:
            # ignore profiles that have a cutoff set
            continue
        languages_profile_ids = []
        languages_profile_has_duplicate = False
        languages_profile_items = json.loads(languages_profile.items)
        for items in languages_profile_items:
            if items['id'] in languages_profile_ids:
                languages_profile_has_duplicate = True
                break
            else:
                languages_profile_ids.append(items['id'])

        if languages_profile_has_duplicate:
            item_id = 0
            for items in languages_profile_items:
                item_id += 1
                items['id'] = item_id
            database.execute(
                update(TableLanguagesProfiles)
                .values({"items": json.dumps(languages_profile_items)})
                .where(TableLanguagesProfiles.profileId == languages_profile.profileId)
            )
