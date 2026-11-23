"""Plugin releases in tables of their own.

The Plugin Hub state is one document row (``provider_hub_state``). The active and the staged release of each
installation move out of it into ``provider_hub_releases``, with the capability bindings of each release in
``provider_hub_release_bindings``. The rest of the installation row (settings, whether it is on, where it came from,
the revocation and hold marks) stays in the document.

Nothing is lost: every installation row that carries an active or a staged version gets one release row per slot,
and the fields that moved are taken out of the document in the same transaction. A row in an odd shape (a version
with values of a type the tables do not hold) keeps its fields in the document, which the store reads as it did.
The downgrade puts the fields back into the document and drops the tables, so the previous revision reads the
state it always read. The release history the new code keeps (the previous release, the last failure) is not
carried back.

Revision ID: c3e8a1f5b972
Revises: a7c2e9f4b183
"""
import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "c3e8a1f5b972"
down_revision = "a7c2e9f4b183"
branch_labels = None
depends_on = None

_STATE = "provider_hub_state"
_RELEASES = "provider_hub_releases"
_BINDINGS = "provider_hub_release_bindings"

# What this revision moves. A later change to the release model makes its own revision: this one keeps moving
# exactly these fields.
_ACTIVE = ("active_version", "active_path", "python_path", "manifest", "supplied_by", "activated_at")
_STAGED = ("staged_version", "staged_path", "staged_python_path", "staged_manifest", "staged_supplied_by")
# What each key after the version is, in order. ``fields`` records which of them the row carried.
_NAMES = ("path", "python_path", "manifest", "supplied_by", "activated_at")
_NULL_BINDINGS = "supplied_by:null"


def _guard(bind):
    """The statements here bind the whole state document, which can hold plaintext credentials until the secret
    migration has run: keep the values out of error text and of the records SQLAlchemy logs itself."""
    bind.engine.hide_parameters = True
    try:
        from provider_hub.state_store import hide_parameters
        hide_parameters(bind)
    except ImportError:
        pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _create_tables(bind):
    tables = set(sa.inspect(bind).get_table_names())
    if _RELEASES not in tables:
        op.create_table(
            _RELEASES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("plugin_id", sa.Text(), nullable=False),
            sa.Column("version", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("bundle_path", sa.Text()),
            sa.Column("python_path", sa.Text()),
            sa.Column("bundle_digest", sa.Text()),
            sa.Column("env_key", sa.Text()),
            sa.Column("manifest", sa.Text()),
            sa.Column("error", sa.Text()),
            sa.Column("source_id", sa.Text()),
            sa.Column("origin", sa.Text()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("activated_at", sa.Text()),
            sa.Column("retired_at", sa.Text()),
            sa.Column("ended_seq", sa.Integer()),
            sa.Column("fields", sa.Text()),
            sa.CheckConstraint("state IN ('staged', 'active', 'draining', 'retired', 'failed')",
                               name="ck_provider_hub_releases_state"),
        )
        op.create_index("ix_provider_hub_releases_plugin_id", _RELEASES, ["plugin_id"])
        op.create_index(
            "ux_provider_hub_releases_slot", _RELEASES, ["plugin_id", "state"], unique=True,
            sqlite_where=sa.text("state IN ('staged', 'active')"),
            postgresql_where=sa.text("state IN ('staged', 'active')"))
    if _BINDINGS not in tables:
        op.create_table(
            _BINDINGS,
            sa.Column("release_id", sa.Integer(), sa.ForeignKey(f"{_RELEASES}.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("capability", sa.Text(), primary_key=True),
            sa.Column("supplier_id", sa.Text(), nullable=False),
        )


def _text_or_none(value):
    return value is None or isinstance(value, str)


def _slot(row, keys, with_activation):
    """The release one slot of an installation row describes, or None when the slot is not a well formed one."""
    version = row.get(keys[0])
    if not isinstance(version, str) or not version:
        return None
    path, python, manifest, supplied = (row.get(key) for key in keys[1:5])
    activated = row.get(keys[5]) if with_activation else None
    bindings_ok = supplied is None or (isinstance(supplied, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in supplied.items()))
    if not (_text_or_none(path) and _text_or_none(python) and _text_or_none(activated)
            and (manifest is None or isinstance(manifest, dict)) and bindings_ok):
        return None
    digest = manifest.get("bundle_sha256") if isinstance(manifest, dict) else None
    names = _NAMES if with_activation else _NAMES[:4]
    present = [name for name, key in zip(names, keys[1:]) if key in row]
    if "supplied_by" in present and supplied is None:
        # A null map and an empty one are different values of the row: the null is remembered.
        present.append(_NULL_BINDINGS)
    return {
        "present": present,
        "version": version, "bundle_path": path, "python_path": python,
        "manifest": None if manifest is None else json.dumps(manifest, sort_keys=True),
        "bundle_digest": digest if isinstance(digest, str) and digest else None,
        "supplied_by": dict(supplied or {}), "activated_at": activated,
    }


def _legacy_catalog_fields(document, row, manifest):
    """Keep a pre-registration community catalog binding when the saved manifest corroborates it."""
    source_id = row.get("source_id")
    if (row.get("origin") != "catalog" or row.get("source_bound", True) is not True
            or not isinstance(source_id, str) or source_id == "official"):
        return []
    sources = document.get("catalog_sources")
    source = sources.get(source_id) if isinstance(sources, dict) else None
    url = source.get("url") if isinstance(source, dict) and not source.get("registration") else None
    manifest_source = manifest.get("source") if isinstance(manifest, dict) else None
    if isinstance(url, str) and url and isinstance(manifest_source, dict) and manifest_source.get("catalog_url") == url:
        return ["catalog_registration:legacy"]
    return []


def move_state_into_releases(bind):
    """Create a release row for each active and staged slot of the stored document and take the fields out of it.

    Skips an installation that already has release rows, so a second run, or a database the store has already
    written releases into, is left alone. Returns the number of releases created.
    """
    state = sa.table(_STATE, sa.column("id", sa.Integer), sa.column("revision", sa.Integer),
                     sa.column("document", sa.Text), sa.column("updated_at", sa.DateTime))
    releases = sa.Table(
        _RELEASES, sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
        *(sa.Column(name, sa.Text) for name in (
            "plugin_id", "version", "state", "bundle_path", "python_path", "bundle_digest", "env_key", "manifest",
            "error", "source_id", "origin", "created_at", "activated_at", "retired_at", "fields")),
        sa.Column("ended_seq", sa.Integer))
    bindings = sa.table(_BINDINGS, sa.column("release_id"), sa.column("capability"), sa.column("supplier_id"))
    _guard(bind)
    found = bind.execute(sa.select(state.c.revision, state.c.document).where(state.c.id == 1)).first()
    if found is None:
        return 0
    try:
        document = json.loads(found.document)
    except ValueError:
        return 0
    installations = document.get("installations") if isinstance(document, dict) else None
    if not isinstance(installations, dict):
        return 0
    have = {row[0] for row in bind.execute(sa.select(releases.c.plugin_id).distinct())}
    created = 0
    changed = False
    stamp = _now()
    for plugin_id, row in installations.items():
        if not isinstance(row, dict) or plugin_id in have:
            continue
        for keys, slot_state, with_activation in ((_ACTIVE, "active", True), (_STAGED, "staged", False)):
            slot = _slot(row, keys, with_activation)
            if slot is None:
                continue
            catalog_fields = _legacy_catalog_fields(document, row, row.get(keys[3]))
            if catalog_fields:
                # v2.7.0 recorded the source URL and id before the explicit binding flag existed.
                row.setdefault("source_bound", True)
            fields = slot["present"] + catalog_fields
            result = bind.execute(sa.insert(releases).values(
                plugin_id=plugin_id, version=slot["version"], state=slot_state, bundle_path=slot["bundle_path"],
                python_path=slot["python_path"], bundle_digest=slot["bundle_digest"], env_key=None,
                manifest=slot["manifest"], error=None, created_at=stamp,
                source_id=row.get("source_id") if isinstance(row.get("source_id"), str) else None,
                origin=row.get("origin") if isinstance(row.get("origin"), str) else None,
                activated_at=slot["activated_at"] if slot_state == "active" else None, retired_at=None,
                ended_seq=None, fields=json.dumps(fields)))
            release_id = result.inserted_primary_key[0]
            if slot["supplied_by"]:
                bind.execute(sa.insert(bindings), [
                    {"release_id": release_id, "capability": capability, "supplier_id": supplier}
                    for capability, supplier in sorted(slot["supplied_by"].items())])
            for key in keys:
                row.pop(key, None)
            created += 1
            changed = True
    if changed:
        done = bind.execute(
            sa.update(state).where(state.c.id == 1, state.c.revision == found.revision)
            .values(document=json.dumps(document, sort_keys=True), revision=found.revision + 1,
                    updated_at=datetime.now()))
        if done.rowcount != 1:
            raise RuntimeError("the Plugin Hub state changed while its releases were being moved; run the upgrade again")
    return created


def put_releases_back(bind):
    """Write the active and staged release of each installation back into its row of the document."""
    tables = set(sa.inspect(bind).get_table_names())
    if _RELEASES not in tables or _STATE not in tables:
        return
    state = sa.table(_STATE, sa.column("id", sa.Integer), sa.column("revision", sa.Integer),
                     sa.column("document", sa.Text), sa.column("updated_at", sa.DateTime))
    releases = sa.table(_RELEASES, *(sa.column(name) for name in (
        "id", "plugin_id", "version", "state", "bundle_path", "python_path", "manifest", "activated_at", "fields")))
    bindings = sa.table(_BINDINGS, sa.column("release_id"), sa.column("capability"), sa.column("supplier_id"))
    _guard(bind)
    found = bind.execute(sa.select(state.c.revision, state.c.document).where(state.c.id == 1)).first()
    if found is None:
        return
    try:
        document = json.loads(found.document)
    except ValueError:
        return
    installations = document.get("installations") if isinstance(document, dict) else None
    if not isinstance(installations, dict):
        return
    supplied: dict[int, dict] = {}
    for release_id, capability, supplier in bind.execute(
            sa.select(bindings.c.release_id, bindings.c.capability, bindings.c.supplier_id)):
        supplied.setdefault(release_id, {})[capability] = supplier
    changed = False
    for release in bind.execute(sa.select(releases).where(releases.c.state.in_(("active", "staged")))):
        row = installations.get(release.plugin_id)
        if not isinstance(row, dict):
            continue
        active = release.state == "active"
        keys = _ACTIVE if active else _STAGED
        names = _NAMES if active else _NAMES[:4]
        present = set(json.loads(release.fields)) if release.fields else set(names)
        values = {
            "path": release.bundle_path, "python_path": release.python_path,
            "manifest": json.loads(release.manifest) if release.manifest else None,
            "supplied_by": None if _NULL_BINDINGS in present else supplied.get(release.id, {}),
            "activated_at": release.activated_at,
        }
        row[keys[0]] = release.version
        for name, key in zip(names, keys[1:]):
            if name in present:
                row[key] = values[name]
            else:
                row.pop(key, None)
        changed = True
    if changed:
        done = bind.execute(
            sa.update(state).where(state.c.id == 1, state.c.revision == found.revision)
            .values(document=json.dumps(document, sort_keys=True), revision=found.revision + 1,
                    updated_at=datetime.now()))
        if done.rowcount != 1:
            # The tables are dropped next: nothing may go if the fields did not get back into the document.
            raise RuntimeError("the Plugin Hub state changed while its releases were being put back; run the downgrade again")


def upgrade():
    bind = op.get_bind()
    _create_tables(bind)
    if _STATE in sa.inspect(bind).get_table_names():
        move_state_into_releases(bind)


def downgrade():
    bind = op.get_bind()
    put_releases_back(bind)
    tables = set(sa.inspect(bind).get_table_names())
    if _BINDINGS in tables:
        op.drop_table(_BINDINGS)
    if _RELEASES in tables:
        op.drop_table(_RELEASES)
