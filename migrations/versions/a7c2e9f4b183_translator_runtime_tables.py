"""The translation runtime tables: engine instances, connections, secrets, jobs, checkpoints, history, usage and budgets.

Revision ID: a7c2e9f4b183
Revises: b6d9f3a1c872
"""

from alembic import op
import sqlalchemy as sa

revision = 'a7c2e9f4b183'
down_revision = 'b6d9f3a1c872'
branch_labels = None
depends_on = None

INSTANCES = 'translator_engine_instances'
CONNECTIONS = 'translator_connections'
SECRETS = 'translator_secrets'
JOBS = 'translator_jobs'
CHECKPOINTS = 'translator_checkpoints'
HISTORY = 'translation_history'
HISTORY_CONNECTIONS = 'translation_history_connections'
USAGE = 'translation_usage'
BUDGETS = 'translation_budgets'
LEGACY_INDEX = 'ux_translator_engine_instances_legacy'
LEGACY_ENGINES = "plugin_id IN ('lingarr')"
ACTIVE_STATES = "state IN ('queued', 'running', 'awaiting_retry')"

# Parents before the tables that point at them. A downgrade drops them in the opposite order.
TABLES = (INSTANCES, CONNECTIONS, SECRETS, JOBS, CHECKPOINTS, HISTORY, HISTORY_CONNECTIONS, USAGE, BUDGETS)


def create_translator_runtime_tables(bind):
    """Create whichever of the nine tables is missing. Returns the names it created, in creation order.

    Written against a bind so the same code runs on SQLite and PostgreSQL and the tests can call it
    directly. The boot's create_all leaves these tables out (MIGRATION_ONLY_TABLES), so this is what builds
    them, on a fresh install as on an upgrade; the create-if-missing check only covers a database a test
    build already touched. Money is whole micro-dollars (millionths of a dollar), never a float.
    """
    existing = set(sa.inspect(bind).get_table_names())
    created = []
    metadata = sa.MetaData()
    # A table whose parent is already there needs the parent in the metadata for its foreign key to resolve.
    parents = [name for name in (INSTANCES, HISTORY) if name in existing]
    if parents:
        metadata.reflect(bind, only=parents)

    if INSTANCES not in existing:
        sa.Table(
            INSTANCES, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('plugin_id', sa.Text(), nullable=False),
            sa.Column('name', sa.Text(), nullable=False, server_default=''),
            sa.Column('enabled', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('settings_json', sa.Text(), nullable=False, server_default='{}'),
            sa.Column('settings_version', sa.Integer(), nullable=True),
            sa.Column('plugin_version', sa.Text(), nullable=True),
            sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('awaiting_secrets', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Index('ix_translator_engine_instances_plugin_id', 'plugin_id'),
            # An engine Bazarr+ serves itself (Lingarr) has one instance, whatever number of requests first list it
            # at once. The runtime's own in-transaction check is the main guard; this index is the one that holds
            # across processes. The model declares the same index, so a fresh install and an upgrade agree.
            sa.Index(LEGACY_INDEX, 'plugin_id', unique=True,
                     sqlite_where=sa.text(LEGACY_ENGINES), postgresql_where=sa.text(LEGACY_ENGINES)),
        )
        created.append(INSTANCES)

    if CONNECTIONS not in existing:
        sa.Table(
            CONNECTIONS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('instance_id', sa.Integer(), sa.ForeignKey(f'{INSTANCES}.id', ondelete='CASCADE'),
                      nullable=False),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('plugin_id', sa.Text(), nullable=False, server_default=''),
            sa.Column('backend_type', sa.Text(), nullable=False),
            sa.Column('model', sa.Text(), nullable=False, server_default=''),
            sa.Column('settings_json', sa.Text(), nullable=False, server_default='{}'),
            sa.Column('settings_version', sa.Integer(), nullable=True),
            sa.Column('plugin_version', sa.Text(), nullable=True),
            sa.Column('price_input_per_million', sa.Float(), nullable=False, server_default='0'),
            sa.Column('price_output_per_million', sa.Float(), nullable=False, server_default='0'),
            sa.Column('enabled', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('awaiting_secrets', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('instance_id', 'position', name='ux_translator_connections_position'),
            sa.Index('ix_translator_connections_instance_id', 'instance_id'),
        )
        created.append(CONNECTIONS)

    if SECRETS not in existing:
        sa.Table(
            SECRETS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('owner_kind', sa.Text(), nullable=False),
            sa.Column('owner_id', sa.Integer(), nullable=False),
            sa.Column('field', sa.Text(), nullable=False),
            sa.Column('encrypted_value', sa.Text(), nullable=False),
            sa.Column('destinations_json', sa.Text(), nullable=False, server_default='[]'),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('owner_kind', 'owner_id', 'field', name='ux_translator_secrets_owner_field'),
            sa.CheckConstraint("owner_kind IN ('instance', 'connection')", name='ck_translator_secrets_owner_kind'),
        )
        created.append(SECRETS)

    if JOBS not in existing:
        sa.Table(
            JOBS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('source_path', sa.Text(), nullable=False),
            sa.Column('target', sa.Text(), nullable=False),
            sa.Column('state', sa.Text(), nullable=False, server_default='queued'),
            sa.Column('queue_job_id', sa.Integer(), nullable=True),
            sa.Column('manual', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('pinned_connection_id', sa.Integer(), nullable=True),
            sa.Column('engine_instance_id', sa.Integer(), nullable=True),
            sa.Column('settings_revision', sa.Integer(), nullable=True),
            sa.Column('release_digest', sa.Text(), nullable=True),
            sa.Column('source_digest', sa.Text(), nullable=True),
            sa.Column('error_class', sa.Text(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('retry_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.CheckConstraint("state IN ('queued', 'running', 'awaiting_retry', 'completed', 'failed', "
                               "'cancelled', 'superseded')", name='ck_translator_jobs_state'),
            sa.Index('ux_translator_jobs_active', 'source_path', 'target', unique=True,
                     sqlite_where=sa.text(ACTIVE_STATES), postgresql_where=sa.text(ACTIVE_STATES)),
            sa.Index('ix_translator_jobs_state', 'state'),
        )
        created.append(JOBS)

    if CHECKPOINTS not in existing:
        sa.Table(
            CHECKPOINTS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('source_path', sa.Text(), nullable=False),
            sa.Column('source_digest', sa.Text(), nullable=False),
            sa.Column('target', sa.Text(), nullable=False),
            sa.Column('engine_instance_id', sa.Integer(), nullable=False),
            sa.Column('settings_revision', sa.Integer(), nullable=False),
            sa.Column('release_digest', sa.Text(), nullable=False, server_default=''),
            sa.Column('batches_json', sa.Text(), nullable=False, server_default='{}'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('source_path', 'source_digest', 'target', 'engine_instance_id',
                                'settings_revision', 'release_digest', name='ux_translator_checkpoints_key'),
            sa.Index('ix_translator_checkpoints_source', 'source_path', 'target'),
            sa.Index('ix_translator_checkpoints_expires_at', 'expires_at'),
        )
        created.append(CHECKPOINTS)

    if HISTORY not in existing:
        sa.Table(
            HISTORY, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('job_id', sa.Text(), nullable=False),
            sa.Column('media_type', sa.Text(), nullable=True),
            sa.Column('media_id', sa.Integer(), nullable=True),
            sa.Column('media_title', sa.Text(), nullable=True),
            sa.Column('arr_instance_id', sa.Integer(), nullable=True),
            sa.Column('source_language', sa.Text(), nullable=True),
            sa.Column('target_language', sa.Text(), nullable=True),
            sa.Column('plugin_id', sa.Text(), nullable=True),
            sa.Column('plugin_version', sa.Text(), nullable=True),
            sa.Column('engine_instance_id', sa.Integer(), nullable=True),
            sa.Column('status', sa.Text(), nullable=False, server_default='running'),
            sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('cost_micros', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('error_class', sa.Text(), nullable=True),
            sa.Column('error_detail', sa.Text(), nullable=True),
            sa.Column('failovers', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('declined_by_instance_id', sa.Integer(), nullable=True),
            sa.Column('declined_reason', sa.Text(), nullable=True),
            sa.Column('taken_over_by_instance_id', sa.Integer(), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('job_id', name='ux_translation_history_job_id'),
            sa.Index('ix_translation_history_created_at', 'created_at'),
            sa.Index('ix_translation_history_arr_instance_id', 'arr_instance_id'),
            sa.Index('ix_translation_history_status', 'status'),
        )
        created.append(HISTORY)

    if HISTORY_CONNECTIONS not in existing:
        sa.Table(
            HISTORY_CONNECTIONS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('history_id', sa.Integer(), sa.ForeignKey(f'{HISTORY}.id', ondelete='CASCADE'),
                      nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=False),
            sa.Column('backend_type', sa.Text(), nullable=False, server_default=''),
            sa.Column('models_json', sa.Text(), nullable=False, server_default='[]'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('batches', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('cost_micros', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('failed_over', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_error_class', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('history_id', 'connection_id', name='ux_translation_history_connections_job'),
            sa.Index('ix_translation_history_connections_history_id', 'history_id'),
        )
        created.append(HISTORY_CONNECTIONS)

    if USAGE not in existing:
        sa.Table(
            USAGE, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('job_id', sa.Text(), nullable=False),
            sa.Column('call_id', sa.Text(), nullable=False),
            sa.Column('connection_id', sa.Integer(), nullable=True),
            sa.Column('backend_type', sa.Text(), nullable=False, server_default=''),
            sa.Column('model', sa.Text(), nullable=False, server_default=''),
            sa.Column('month', sa.Text(), nullable=False),
            sa.Column('worst_case_micros', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('actual_micros', sa.BigInteger(), nullable=True),
            sa.Column('state', sa.Text(), nullable=False, server_default='reserved'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('settled_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('job_id', 'call_id', name='ux_translation_usage_call'),
            sa.Index('ix_translation_usage_job_id', 'job_id'),
            sa.Index('ix_translation_usage_month', 'month'),
        )
        created.append(USAGE)

    if BUDGETS not in existing:
        sa.Table(
            BUDGETS, metadata,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('job_id', sa.Text(), nullable=False),
            sa.Column('month', sa.Text(), nullable=False),
            sa.Column('budget_micros', sa.BigInteger(), nullable=True),
            sa.Column('state', sa.Text(), nullable=False, server_default='active'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('job_id', name='ux_translation_budgets_job_id'),
            sa.Index('ix_translation_budgets_month_state', 'month', 'state'),
        )
        created.append(BUDGETS)

    metadata.create_all(bind, tables=[metadata.tables[name] for name in created])
    if INSTANCES in existing:
        _ensure_legacy_index(bind)
    return created


def _ensure_legacy_index(bind):
    """The instances table of a test build can predate the index: add it when it is not there."""
    if LEGACY_INDEX in {item['name'] for item in sa.inspect(bind).get_indexes(INSTANCES)}:
        return
    bind.execute(sa.text(f"CREATE UNIQUE INDEX {LEGACY_INDEX} ON {INSTANCES} (plugin_id) WHERE {LEGACY_ENGINES}"))


def upgrade():
    create_translator_runtime_tables(op.get_bind())


def downgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    # Children first: connections point at instances, per-connection history at history.
    for table in reversed(TABLES):
        if table in existing:
            op.drop_table(table)
