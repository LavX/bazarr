"""Media sources: libraries of series, movies and sports that come from folders or a media server.

Seven tables: the sources, their libraries and the roots of each library, the groups (shows and leagues) and
the items (movies, episodes and sports events) a source supplies, and the history and blacklist of those items.
Every allowed value and every column the later source kinds need is here, so adding a kind needs no migration.

The history and blacklist rows keep snapshots of the item and source they were written for, and their links to
both are set to NULL when either is deleted. The downgrade drops the seven tables, history included.

Revision ID: b4d1f7a39c25
Revises: c3e8a1f5b972
"""
from alembic import op
import sqlalchemy as sa

revision = "b4d1f7a39c25"
down_revision = "c3e8a1f5b972"
branch_labels = None
depends_on = None

_ITEM_KINDS = "('movie', 'episode', 'sports_event')"
_IDENTITY_SOURCES = "('filename', 'tmdb', 'media_server', 'user')"
_IDENTITY_STATES = "('identified', 'parsed', 'ambiguous', 'unmatched')"
_WANTED = "missing_subtitles IS NOT NULL AND missing_subtitles != '[]'"


def _flag(name, default):
    return sa.Column(name, sa.Integer(), nullable=False, server_default=default)


def _timestamps():
    return (sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False))


def _tables():
    """The seven tables, with stand-ins for the parents they point at (those exist already)."""
    metadata = sa.MetaData()
    sa.Table("media_server_instances", metadata, sa.Column("id", sa.Text(), primary_key=True))
    sa.Table("table_languages_profiles", metadata, sa.Column("profileId", sa.Integer(), primary_key=True))
    sa.Table("arr_instances", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    profile = "table_languages_profiles.profileId"

    sources = sa.Table(
        "media_sources", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        _flag("enabled", "1"),
        sa.Column("media_server_instance_id", sa.Text(),
                  sa.ForeignKey("media_server_instances.id", ondelete="CASCADE")),
        _flag("scan_frequency_hours", "12"),
        sa.Column("last_scan_started_at", sa.DateTime()),
        sa.Column("last_scan_finished_at", sa.DateTime()),
        sa.Column("last_scan_status", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('folder', 'jellyfin', 'emby')", name="ck_media_sources_kind"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_media_sources_enabled"),
        sa.CheckConstraint("(kind = 'folder' AND media_server_instance_id IS NULL) "
                           "OR (kind IN ('jellyfin', 'emby') AND media_server_instance_id IS NOT NULL)",
                           name="ck_media_sources_server"),
        sa.Index("ux_media_sources_name", "name", unique=True),
    )
    libraries = sa.Table(
        "media_source_libraries", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("media_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("content_kind", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey(profile, ondelete="SET NULL")),
        _flag("enabled", "1"),
        sa.Column("last_scan_status", sa.Text()),
        sa.Column("last_scan_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint("content_kind IN ('movie', 'series', 'sports')",
                           name="ck_media_source_libraries_content_kind"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_media_source_libraries_enabled"),
        sa.Index("ux_media_source_libraries_locator", "source_id", "locator", unique=True),
        sa.Index("ix_media_source_libraries_profile_id", "profile_id"),
    )
    roots = sa.Table(
        "media_source_library_roots", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("media_source_libraries.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("server_path", sa.Text()),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("last_dev", sa.Text()),
        sa.Index("ux_media_source_library_roots_local", "library_id", "local_path", unique=True),
    )
    groups = sa.Table(
        "table_source_groups", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("media_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("media_source_libraries.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("dir_dev", sa.Text()),
        sa.Column("dir_ino", sa.Text()),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("external_id", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sort_title", sa.Text(), nullable=False),
        sa.Column("parsed_title", sa.Text()),
        sa.Column("year", sa.Text()),
        sa.Column("tvdb_id", sa.Integer()),
        sa.Column("tmdb_id", sa.Text()),
        sa.Column("imdb_id", sa.Text()),
        sa.Column("poster", sa.Text()),
        sa.Column("identity_source", sa.Text(), nullable=False),
        sa.Column("identity_state", sa.Text(), nullable=False),
        sa.Column("identity_candidates", sa.Text()),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey(profile, ondelete="SET NULL")),
        _flag("monitored", "1"),
        _flag("identify_pending", "0"),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('show', 'league')", name="ck_source_groups_kind"),
        sa.CheckConstraint(f"identity_source IN {_IDENTITY_SOURCES}", name="ck_source_groups_identity_source"),
        sa.CheckConstraint(f"identity_state IN {_IDENTITY_STATES}", name="ck_source_groups_identity_state"),
        sa.CheckConstraint("monitored IN (0, 1)", name="ck_source_groups_monitored"),
        sa.CheckConstraint("identify_pending IN (0, 1)", name="ck_source_groups_identify_pending"),
        sa.Index("ux_source_groups_key", "library_id", "group_key", unique=True),
        sa.Index("ix_source_groups_source_id", "source_id"),
        sa.Index("ix_table_source_groups_profile_id", "profile_id"),
    )
    items = sa.Table(
        "table_source_items", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("media_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("library_id", sa.Integer(), sa.ForeignKey("media_source_libraries.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("table_source_groups.id", ondelete="CASCADE")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("file_dev", sa.Text()),
        sa.Column("file_ino", sa.Text()),
        sa.Column("last_seen_at", sa.DateTime()),
        _flag("missing_scans", "0"),
        _flag("server_missing", "0"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sort_title", sa.Text(), nullable=False),
        sa.Column("parsed_title", sa.Text()),
        sa.Column("year", sa.Text()),
        sa.Column("episode_title", sa.Text()),
        sa.Column("season", sa.Integer()),
        sa.Column("episode", sa.Integer()),
        _flag("part_number", "0"),
        sa.Column("part_name", sa.Text()),
        sa.Column("event_date", sa.Text()),
        sa.Column("imdb_id", sa.Text()),
        sa.Column("tmdb_id", sa.Text()),
        sa.Column("tvdb_id", sa.Integer()),
        sa.Column("external_id", sa.Text()),
        sa.Column("identity_source", sa.Text(), nullable=False),
        sa.Column("identity_state", sa.Text(), nullable=False),
        sa.Column("identity_candidates", sa.Text()),
        sa.Column("scene_name", sa.Text()),
        sa.Column("file_size", sa.BigInteger()),
        sa.Column("file_mtime_ns", sa.BigInteger()),
        sa.Column("ffprobe_cache", sa.LargeBinary()),
        sa.Column("audio_language", sa.Text()),
        sa.Column("audio_codec", sa.Text()),
        sa.Column("video_codec", sa.Text()),
        sa.Column("resolution", sa.Text()),
        sa.Column("format", sa.Text()),
        sa.Column("subtitles", sa.Text()),
        sa.Column("missing_subtitles", sa.Text()),
        sa.Column("failed_attempts", sa.Text()),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey(profile, ondelete="SET NULL")),
        _flag("monitored", "1"),
        _flag("arr_shadow", "0"),
        sa.Column("shadow_instance_id", sa.Integer(), sa.ForeignKey("arr_instances.id", ondelete="SET NULL")),
        sa.Column("shadow_owner", sa.Text()),
        sa.Column("shadow_arr_table", sa.Text()),
        sa.Column("shadow_arr_row_id", sa.Integer()),
        sa.Column("shadow_arr_path", sa.Text()),
        sa.Column("shadow_arr_file_id", sa.Integer()),
        sa.Column("shadow_rung", sa.Text()),
        _flag("identify_pending", "0"),
        *_timestamps(),
        sa.Column("indexed_at", sa.DateTime()),
        sa.CheckConstraint(f"kind IN {_ITEM_KINDS}", name="ck_source_items_kind"),
        sa.CheckConstraint("(kind = 'movie' AND group_id IS NULL) "
                           "OR (kind IN ('episode', 'sports_event') AND group_id IS NOT NULL)",
                           name="ck_source_items_parent"),
        sa.CheckConstraint(f"identity_source IN {_IDENTITY_SOURCES}", name="ck_source_items_identity_source"),
        sa.CheckConstraint(f"identity_state IN {_IDENTITY_STATES}", name="ck_source_items_identity_state"),
        sa.CheckConstraint("monitored IN (0, 1)", name="ck_source_items_monitored"),
        sa.CheckConstraint("arr_shadow IN (0, 1)", name="ck_source_items_arr_shadow"),
        sa.CheckConstraint("server_missing IN (0, 1)", name="ck_source_items_server_missing"),
        sa.CheckConstraint("identify_pending IN (0, 1)", name="ck_source_items_identify_pending"),
        sa.CheckConstraint(f"shadow_arr_table IN {_ITEM_KINDS}", name="ck_source_items_shadow_arr_table"),
        sa.CheckConstraint("shadow_rung IN ('path', 'inode', 'attributes')", name="ck_source_items_shadow_rung"),
        sa.Index("ux_source_items_path", "path", unique=True),
        sa.Index("ix_source_items_file_identity", "file_ino", "file_dev"),
        sa.Index("ix_source_items_source_id", "source_id"),
        sa.Index("ix_source_items_library_id", "library_id"),
        sa.Index("ix_source_items_group_id", "group_id"),
        sa.Index("ix_source_items_kind", "kind"),
        sa.Index("ix_source_items_file_size", "file_size"),
        sa.Index("ix_source_items_wanted", "id", sqlite_where=sa.text(_WANTED), postgresql_where=sa.text(_WANTED)),
        sa.Index("ix_table_source_items_profile_id", "profile_id"),
    )
    history = sa.Table(
        "table_source_history", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("table_source_items.id", ondelete="SET NULL")),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("media_sources.id", ondelete="SET NULL")),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("item_title", sa.Text()),
        sa.Column("group_title", sa.Text()),
        sa.Column("source_name", sa.Text()),
        sa.Column("action", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("language", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("ai_translated", sa.Integer()),
        sa.Column("score", sa.Integer()),
        sa.Column("score_out_of", sa.Integer()),
        sa.Column("subs_id", sa.Text()),
        sa.Column("subtitles_path", sa.Text()),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("video_path", sa.Text()),
        sa.Column("matched", sa.Text()),
        sa.Column("not_matched", sa.Text()),
        sa.Column("upgradedFromId", sa.Integer(), sa.ForeignKey("table_source_history.id")),
        sa.CheckConstraint(f"item_kind IN {_ITEM_KINDS}", name="ck_table_source_history_item_kind"),
        sa.CheckConstraint("ai_translated IN (0, 1)", name="ck_table_source_history_ai_translated"),
        sa.Index("ix_table_source_history_video_path_language_timestamp", "video_path", "language", "timestamp"),
        sa.Index("ix_table_source_history_timestamp", "timestamp"),
        sa.Index("ix_table_source_history_events", "timestamp",
                 sqlite_where=sa.text("action != 7"), postgresql_where=sa.text("action != 7")),
        sa.Index("ix_table_source_history_upgraded_from_id", "upgradedFromId"),
        sa.Index("ix_table_source_history_item_id", "item_id"),
        sa.Index("ix_table_source_history_kind_timestamp", "item_kind", "timestamp"),
    )
    blacklist = sa.Table(
        "table_source_blacklist", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("table_source_items.id", ondelete="SET NULL")),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("media_sources.id", ondelete="SET NULL")),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("video_path", sa.Text()),
        sa.Column("item_title", sa.Text()),
        sa.Column("group_title", sa.Text()),
        sa.Column("source_name", sa.Text()),
        sa.Column("provider", sa.Text()),
        sa.Column("subs_id", sa.Text()),
        sa.Column("language", sa.Text()),
        sa.Column("timestamp", sa.DateTime()),
        sa.CheckConstraint(f"item_kind IN {_ITEM_KINDS}", name="ck_table_source_blacklist_item_kind"),
        sa.Index("ix_table_source_blacklist_item_id", "item_id"),
        sa.Index("ix_table_source_blacklist_subs_id", "subs_id"),
    )
    return [sources, libraries, roots, groups, items, history, blacklist]


def upgrade():
    # init_db() builds every model table before the migrations run, so a fresh install, and an install
    # upgraded by a build that created them, already have these. Each is made only when it is missing.
    bind = op.get_bind()
    for table in _tables():
        table.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for table in reversed(_tables()):
        table.drop(bind, checkfirst=True)
