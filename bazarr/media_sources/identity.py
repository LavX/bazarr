# coding=utf-8
"""Which file is which: the same-file rules, matching a discovered file to a stored item, the handover of an item
between libraries, and the arr shadows.

One physical file is one source item, whatever paths reach it. The rules here never write: they answer, and the
scanner writes.
"""
import errno
import os
from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
from functools import cache
from itertools import chain

import sqlalchemy as sa
from utilities.path_case import windows_path_case_mapping, windows_path_casefold

FileStat = namedtuple('FileStat', 'dev ino size mtime_ns')

# The rungs of "same file", in the order they are tried. They are also what a shadow records.
RUNG_PATH, RUNG_INODE, RUNG_ATTRIBUTES = 'path', 'inode', 'attributes'
_LOCAL_WINDOWS_PATHS = os.name == 'nt'


def unsigned(value):
    """st_dev and st_ino as unsigned decimal text, whatever width the platform reports them at."""
    return str(value & 0xFFFFFFFFFFFFFFFF)


def file_content_stamp(file_stat):
    """Physical file identity and content attributes, independent of its path."""
    return file_stat.dev, file_stat.ino, file_stat.size, file_stat.mtime_ns


def stored_content_stamp(row):
    return row.file_dev, row.file_ino, row.file_size, row.file_mtime_ns


def stat_file(path):
    """``FileStat`` of a file, or None when it cannot be read. The one place a file is statted to decide whether
    two paths are the same file, so that a test can count the stats."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return FileStat(unsigned(info.st_dev), unsigned(info.st_ino), info.st_size, info.st_mtime_ns)


def path_is_gone(path):
    """True only when the file is known to be absent. Any other failure to stat it means "do not know"."""
    try:
        os.stat(path)
    except OSError as error:
        return error.errno in (errno.ENOENT, errno.ENOTDIR)
    except ValueError:
        return False
    return False


def path_can_release(path, found):
    """A foreign path may release a file only if it is absent or holds a different physical file."""
    try:
        info = os.stat(path)
    except OSError as error:
        return error.errno in (errno.ENOENT, errno.ENOTDIR)
    except ValueError:
        return False
    return (unsigned(info.st_dev), unsigned(info.st_ino)) != (found.dev, found.ino)


def same_file(first, second, *, attributes=False, stat_first=None, stat_second=None):
    """The rung at which two paths are one file, or None.

    1. Equal paths, or equal real paths: ``'path'``.
    2. With both reachable, an equal ``(st_dev, st_ino)`` (hardlinks, bind mounts, symlink targets): ``'inode'``.
    3. Only with ``attributes=True``, when ``st_dev`` differs: an equal file name, size and ``st_mtime_ns``:
       ``'attributes'``. Two mounts of one network share report different devices, so rung 2 cannot see them.
       It is used for arr shadowing only: where a false match costs a copy that stays listed and searchable by
       hand. Between sources it is never used, so an ``rsync -a`` mirror keeps its own item and subtitles.

    A caller that already holds a file's stat passes it, and nothing is statted twice."""
    if first == second:
        return RUNG_PATH
    try:
        if os.path.realpath(first) == os.path.realpath(second):
            return RUNG_PATH
    except (OSError, ValueError):
        pass
    stat_first = stat_first or stat_file(first)
    stat_second = stat_second or stat_file(second)
    if stat_first is None or stat_second is None:
        return None
    if (stat_first.dev, stat_first.ino) == (stat_second.dev, stat_second.ino):
        return RUNG_INODE
    if (attributes and stat_first.dev != stat_second.dev
            and os.path.basename(first) == os.path.basename(second)
            and stat_first.size == stat_second.size and stat_first.mtime_ns == stat_second.mtime_ns):
        return RUNG_ATTRIBUTES
    return None


# Matching a discovered file to a stored item

def identity_holds(row, found, ignore_dev=False):
    """A row's file identity holds for a discovered file when the inode (and the device, unless the root's device
    changed) and the size and mtime are all equal, or when the path, the size and the mtime are all equal (a
    restore that kept the times, or a filesystem whose inodes are not stable). Inode reuse after a delete would
    also need an equal size and an equal mtime to the nanosecond.

    A filesystem that derives a file's inode from its path (mergerfs with ``inodecalc=path-hash``) gives a renamed
    file a new inode, so a rename there is not recognised: the old row is deleted and the file is added as new.
    That setting is not supported for a library."""
    if row.file_size != found.size or row.file_mtime_ns != found.mtime_ns:
        return False
    if row.path == found.path:
        return True
    return row.file_ino is not None and row.file_ino == found.ino and (ignore_dev or row.file_dev == found.dev)


@dataclass
class Matching:
    """The outcome of matching a library's discovered files to its stored rows. Nothing is written yet."""
    #: ``(row, found)``: the row's identity holds at its own path. ``alias`` rows are the exception: the file was
    #: found at another path while the row's own path still holds it, and the row is left where it is.
    unchanged: list = field(default_factory=list)
    alias_unchanged: list = field(default_factory=list)
    #: ``(row, found)``: the row's file is at another path of this library.
    moved: list = field(default_factory=list)
    #: ``(holder, found, alias)``: a row of another library whose file moved here or is held through a live
    #: alias by a disabled library.
    handovers: list = field(default_factory=list)
    #: ``(holder, found)``: a row of another content kind closes when the destination is written.
    closed_foreign: list = field(default_factory=list)
    #: ``(found, holder)``: another library's row that would have been closed or have followed the file, but
    #: whose library cannot vouch for it right now (unmounted, remounted, or its path cannot be read). Nothing is
    #: done: that library's own next scan decides.
    deferred_foreign: list = field(default_factory=list)
    #: ``(found, holder)``: the file is an alias of one an enabled library holds. Not inserted.
    supplied: list = field(default_factory=list)
    #: ``(found, holder)``: a disabled holder of another content kind. Not adopted, not inserted.
    held_elsewhere: list = field(default_factory=list)
    #: ``(row, found)``: the content at the row's path changed.
    path_only: list = field(default_factory=list)
    new: list = field(default_factory=list)
    #: Rows matched in neither pass.
    unmatched: list = field(default_factory=list)
    #: The unmatched rows whose path now holds a file another row claimed: the path was reused.
    displaced: list = field(default_factory=list)
    #: Unmatched rows whose path was claimed but nothing in this library can take it this scan.
    protected: list = field(default_factory=list)

    @property
    def identity_matched(self):
        return len(self.unchanged) + len(self.moved) + len(self.alias_unchanged)


def _holds_live(row_path, found):
    """Whether the row's own stored path still holds the file ``found`` describes."""
    if row_path == found.path:
        return True
    return same_file(row_path, found.path, stat_second=FileStat(found.dev, found.ino, found.size, found.mtime_ns)
                     ) is not None


def match_library(found_files, rows, *, ignore_dev, foreign_lookup, library_content_kind,
                  foreign_vouched=lambda holder, found: True):
    """Match a library's discovered files to its stored rows, over the whole library and before any write.

    ``ignore_dev(found)`` says whether a file's root changed device since the last scan. ``foreign_lookup(inodes,
    paths)`` returns the rows of other libraries that may be the same files. Everything is processed in a fixed
    order, so the answer does not depend on the order the walk produced the files in.

    ``foreign_vouched(holder, found)`` says whether the holder's own library can vouch for the row being closed
    or moved away from it. Without it the row is left to that library's own next scan."""
    result = Matching()
    found_files = sorted(found_files, key=lambda found: found.path)
    rows = sorted(rows, key=lambda row: row.id)
    by_path = {found.path: found for found in found_files}
    by_ino = defaultdict(list)
    for found in found_files:
        by_ino[found.ino].append(found)
    claimed = {}  # path -> the row (or holder) id that claimed it
    matched_rows = set()

    # Pass 1, identity. A row that still holds the file at its own path comes first, so a file never goes to a
    # row elsewhere while its own row is right there.
    for row in rows:
        found = by_path.get(row.path)
        if found is not None and found.path not in claimed and identity_holds(row, found, ignore_dev(found)):
            claimed[found.path] = row.id
            matched_rows.add(row.id)
            result.unchanged.append((row, found))
    for row in rows:
        if row.id in matched_rows:
            continue
        candidates = [found for found in by_ino.get(row.file_ino, ())
                      if found.path not in claimed and identity_holds(row, found, ignore_dev(found))]
        if not candidates:
            continue
        found = candidates[0]
        claimed[found.path] = row.id
        matched_rows.add(row.id)
        if _holds_live(row.path, found):
            # The row's own path still holds the file: this path is an alias of it, not a rename.
            result.alias_unchanged.append((row, found))
        else:
            result.moved.append((row, found))

    # Identity against rows of other libraries, for the files no row of this library claimed.
    unclaimed = [found for found in found_files if found.path not in claimed]
    if unclaimed:
        holders = foreign_lookup([found.ino for found in unclaimed], [found.path for found in unclaimed])
        holders_by_file, holders_by_path = defaultdict(list), {}
        for holder in holders:
            holders_by_file[holder.file_dev, holder.file_ino].append(holder)
            holders_by_path[holder.path] = holder
        used = set()
        for found in unclaimed:
            candidates = {holder.id: holder for holder in holders_by_file.get((found.dev, found.ino), ())
                          if holder.id not in used and identity_holds(holder, found)}
            at_path = holders_by_path.get(found.path)
            if at_path is not None and at_path.id not in used:
                candidates[at_path.id] = at_path
            if not candidates:
                continue
            holder = sorted(candidates.values(), key=lambda holder: (
                not (holder.library_enabled and holder.source_enabled), holder.id))[0]
            alias = holder.path == found.path or _holds_live(holder.path, found)
            same_kind = holder.content_kind == library_content_kind
            used.add(holder.id)
            if alias:
                claimed[found.path] = holder.id
                if holder.library_enabled and holder.source_enabled:
                    result.supplied.append((found, holder))
                elif same_kind:
                    result.handovers.append((holder, found, True))
                else:
                    result.held_elsewhere.append((found, holder))
            elif not foreign_vouched(holder, found):
                claimed[found.path] = holder.id
                result.deferred_foreign.append((found, holder))
            elif same_kind:
                # The file physically moved here: the row follows its own identity, from an enabled library too.
                claimed[found.path] = holder.id
                result.handovers.append((holder, found, False))
            else:
                # It cannot carry the row into another kind of library: the row is closed, the file is new here.
                result.closed_foreign.append((holder, found))

    # Pass 2, path. A row no identity claimed, whose path holds a file nothing claimed: the content changed.
    for row in rows:
        if row.id in matched_rows:
            continue
        found = by_path.get(row.path)
        if found is not None and found.path not in claimed:
            claimed[found.path] = row.id
            matched_rows.add(row.id)
            result.path_only.append((row, found))

    replacement_paths = {found.path for _, found in result.moved}
    replacement_paths.update(found.path for _, found, _ in result.handovers)
    for row in rows:
        if row.id not in matched_rows:
            if row.path in replacement_paths:
                result.displaced.append(row)
            elif row.path in claimed:
                result.protected.append(row)
            else:
                result.unmatched.append(row)

    # Pass 3: whatever is left is new.
    result.new = [found for found in found_files if found.path not in claimed]
    return result


# Arr shadows

@dataclass(frozen=True)
class ArrRow:
    """One arr row that may hold the same file as a source item: what it held, mapped to a Bazarr-visible path."""
    table: str          # 'movie', 'episode' or 'sports_event'
    row_id: int
    instance_id: int
    instance_name: str
    path: str           # mapped to the path Bazarr+ sees
    file_size: object   # nullable, and stale after an in-place remux until the next arr sync
    file_id: object


KIND_OF_TABLE = {'movie': 'radarr', 'episode': 'sonarr', 'sports_event': 'sportarr'}
TABLE_OF_KIND = {kind: table for table, kind in KIND_OF_TABLE.items()}


class ArrIndex:
    """The arr rows a source file can be shadowed by, read once per scan from the rows of enabled owners."""

    def __init__(self, rows=()):
        self.by_path = {}
        self.by_size = defaultdict(list)
        self.unsized_by_name = defaultdict(list)
        for row in sorted(rows, key=lambda row: (row.instance_id, row.table, row.row_id)):
            self.by_path.setdefault(row.path, row)
            if row.file_size is None:
                self.unsized_by_name[os.path.basename(row.path)].append(row)
            else:
                self.by_size[row.file_size].append(row)

    def __bool__(self):
        return bool(self.by_path)

    def match_path(self, path):
        """Stage one: the path or the real path of the file equals a mapped arr path. No size condition: the
        arr's size can be NULL or stale."""
        row = self.by_path.get(path)
        if row is not None:
            return row, RUNG_PATH
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return None
        row = self.by_path.get(real)
        return (row, RUNG_PATH) if row is not None else None

    def match_stat(self, path, stat):
        """Stage two: the rungs that stat, narrowed to the arr rows of equal size, plus the rows with no size and
        the same file name. A scan stats a handful of files, not a whole arr library."""
        candidates = list(self.by_size.get(stat.size, ())) + list(self.unsized_by_name.get(os.path.basename(path), ()))
        for row in candidates:
            rung = same_file(path, row.path, attributes=True, stat_first=stat)
            if rung is not None:
                return row, rung
        return None

    def match(self, path, stat):
        return self.match_path(path) or (self.match_stat(path, stat) if stat is not None else None)


SHADOW_COLUMNS = ('arr_shadow', 'shadow_instance_id', 'shadow_owner', 'shadow_arr_table', 'shadow_arr_row_id',
                  'shadow_arr_path', 'shadow_arr_file_id', 'shadow_rung')


def shadow_values(match):
    """The columns a shadow writes: that it is set, and which arr row, file and rung set it."""
    row, rung = match
    return dict(arr_shadow=1, shadow_instance_id=row.instance_id, shadow_owner=row.instance_name,
                shadow_arr_table=row.table, shadow_arr_row_id=row.row_id, shadow_arr_path=row.path,
                shadow_arr_file_id=row.file_id, shadow_rung=rung)


CLEARED_SHADOW = dict(arr_shadow=0, shadow_instance_id=None, shadow_owner=None, shadow_arr_table=None,
                      shadow_arr_row_id=None, shadow_arr_path=None, shadow_arr_file_id=None, shadow_rung=None)


def shadow_changes(row, match):
    """The values to write so that ``row`` (anything with the shadow columns) carries ``match``, or None when it
    already does."""
    wanted = shadow_values(match) if match is not None else CLEARED_SHADOW
    current = {name: getattr(row, name) for name in SHADOW_COLUMNS}
    if match is None and not current['arr_shadow'] and current['shadow_instance_id'] is None:
        return None
    return None if wanted == current else wanted


def master_toggles():
    from app.config import settings
    return {'sonarr': bool(settings.general.use_sonarr), 'radarr': bool(settings.general.use_radarr),
            'sportarr': bool(settings.general.use_sportarr)}


class _Mapper:
    """An arr instance's path mapping, loaded once, so mapping a hundred thousand rows asks the database once."""

    def __init__(self, forward, rules=(), *, sports=False):
        self.forward = forward
        self.rules = rules
        self.sports = sports

    def candidate_paths(self, path):
        from utilities import path_mappings as module

        if self.sports:
            mapped = (module.apply_sports_mapping(path, [rule], reverse=True, ignore_case=_LOCAL_WINDOWS_PATHS)
                      for rule in self.rules)
        else:
            mapped = (module._apply_mapping(path, [rule], reverse=True) for rule in self.rules)
        return {path, *mapped}


def _mapper_for(instance):
    """The mapper of an instance, or None when its stored mapping cannot be read."""
    from utilities import path_mappings as module
    if instance.kind == 'sportarr':
        try:
            mapping = module.read_sports_mappings(instance.path_mappings)
        except ValueError:
            return None
        return _Mapper(lambda path: module.apply_sports_mapping(path, mapping), mapping, sports=True)
    media_type = 'movie' if instance.kind == 'radarr' else 'series'
    own = module._instance_path_mapping(instance.id, media_type)
    if own is not None:
        return _Mapper(lambda path: module._apply_mapping(path, own, reverse=False), own)
    shared = module.path_mappings
    if media_type == 'movie':
        return _Mapper(shared.path_replace_movie, shared.path_mapping_movies)
    return _Mapper(shared.path_replace, shared.path_mapping_series)


class ArrOwners:
    """The arr instances that count as owners right now: enabled, with their kind's master toggle on. Shadows
    are judged against these and no others."""

    def __init__(self, instances, target=None):
        self.instances = {instance.id: instance for instance in instances}
        self.target = target
        self._mappers = {}

    @classmethod
    def load(cls, target=None):
        from app.database import TableArrInstances, database
        toggles = master_toggles()
        columns = (TableArrInstances.id, TableArrInstances.kind, TableArrInstances.name,
                   TableArrInstances.path_mappings)
        rows = (database if target is None else target).execute(
            sa.select(*columns).where(TableArrInstances.enabled == 1)).all()
        return cls([row for row in rows if toggles.get(row.kind)], target)

    def counts(self, instance_id):
        return instance_id in self.instances

    def mapper(self, instance_id):
        if instance_id not in self._mappers:
            self._mappers[instance_id] = _mapper_for(self.instances[instance_id])
        return self._mappers[instance_id]

    def _arr_row(self, instance, table, row_id, path, file_size, file_id):
        mapper = self.mapper(instance.id)
        if mapper is None or path is None:
            return None
        return ArrRow(table=table, row_id=row_id, instance_id=instance.id, instance_name=instance.name,
                      path=mapper.forward(path), file_size=file_size, file_id=file_id)

    @staticmethod
    def _columns(table):
        from app.database import TableEpisodes, TableMovies, TableSportsEvents
        model, file_id = {'movie': (TableMovies, TableMovies.movie_file_id),
                          'episode': (TableEpisodes, TableEpisodes.episode_file_id),
                          'sports_event': (TableSportsEvents, TableSportsEvents.file_id)}[table]
        return model, file_id

    def index(self, kinds=None):
        """An ``ArrIndex`` of every row of the owners (of those kinds, if given). Read once."""
        from app.database import database
        session = database if self.target is None else self.target
        rows = []
        for instance in self.instances.values():
            if kinds is not None and instance.kind not in kinds:
                continue
            table = TABLE_OF_KIND[instance.kind]
            model, file_id = self._columns(table)
            for row in session.execute(sa.select(model.id, model.path, model.file_size, file_id)
                                       .where(model.arr_instance_id == instance.id)).all():
                arr_row = self._arr_row(instance, table, row[0], row[1], row[2], row[3])
                if arr_row is not None:
                    rows.append(arr_row)
        return ArrIndex(rows)

    def fetch(self, table, row_id):
        """The arr row a shadow recorded, as it is now, or None when it is gone or its owner no longer counts."""
        from app.database import database
        session = database if self.target is None else self.target
        model, file_id = self._columns(table)
        row = session.execute(sa.select(model.id, model.path, model.file_size, file_id, model.arr_instance_id)
                              .where(model.id == row_id)).first()
        if row is None or row[4] not in self.instances:
            return None
        return self._arr_row(self.instances[row[4]], table, row[0], row[1], row[2], row[3])

    def fetch_many(self, pairs):
        """``{(table, row id): ArrRow}`` for the arr rows ``pairs`` name, read in one query per table. A row that
        is gone, or whose owner no longer counts, is not in the answer."""
        from app.database import database
        session = database if self.target is None else self.target
        wanted = defaultdict(set)
        for table, row_id in pairs:
            wanted[table].add(row_id)
        found = {}
        for table, ids in wanted.items():
            model, file_id = self._columns(table)
            ids = sorted(ids)
            for start in range(0, len(ids), 500):
                for row in session.execute(
                        sa.select(model.id, model.path, model.file_size, file_id, model.arr_instance_id)
                        .where(model.id.in_(ids[start:start + 500]))).all():
                    if row[4] in self.instances:
                        arr_row = self._arr_row(self.instances[row[4]], table, row[0], row[1], row[2], row[3])
                        if arr_row is not None:
                            found[(table, row[0])] = arr_row
        return found

    def path_stage(self, path):
        """The path stage alone, for one file, with no stat: one indexed lookup per owner on the item's path and
        real path, mapped back to each arr's own side. Used right before an automatic search."""
        from app.database import database
        session = database if self.target is None else self.target
        try:
            candidates = {path, os.path.realpath(path)}
        except (OSError, ValueError):
            candidates = {path}
        for instance in sorted(self.instances.values(), key=lambda instance: instance.id):
            mapper = self.mapper(instance.id)
            if mapper is None:
                continue
            table = TABLE_OF_KIND[instance.kind]
            model, file_id = self._columns(table)
            arr_paths = {mapped for candidate in candidates for mapped in mapper.candidate_paths(candidate)}
            path_column = model.path
            local_candidates = candidates
            if mapper.sports:
                codec = _windows_postgres_codec(session)
                arr_paths = {_sports_path_key(candidate, codec) for candidate in arr_paths}
                fold = _windows_sql_casefold(arr_paths, codec)
                path_column = _sports_sql_path_key(path_column, fold)
                if _LOCAL_WINDOWS_PATHS:
                    local_candidates = {_sports_path_key(candidate) for candidate in candidates}
            for row in session.execute(sa.select(model.id, model.path, model.file_size, file_id)
                                       .where(model.arr_instance_id == instance.id, path_column.in_(arr_paths))
                                       .order_by(model.id)).all():
                arr_row = self._arr_row(instance, table, row[0], row[1], row[2], row[3])
                if arr_row is None:
                    continue
                local_path = _sports_path_key(arr_row.path) if mapper.sports and _LOCAL_WINDOWS_PATHS else arr_row.path
                if local_path in local_candidates:
                    return arr_row, RUNG_PATH
        return None


def _mapped_sql_path(path, rules, *, reverse=False):
    """Use the same first matching Arr mapping for exact stored-path checks."""
    matches = []
    for remote, local, *_unused in rules:
        start, end = (local, remote) if reverse else (remote, local)
        if not start or not end or start == end:
            continue
        mapped = sa.func.replace(path, start, end)
        normalized = sa.case(
            (sa.func.substr(mapped, 1, 1) == '/', sa.func.replace(mapped, '\\', '/')),
            (sa.or_(sa.func.substr(mapped, 1, 2) == '\\\\', sa.func.substr(mapped, 2, 2) == ':\\'),
             sa.func.replace(mapped, '/', '\\')),
            else_=mapped,
        )
        matches.append((mapped != path, normalized))
    return sa.case(*matches, else_=path) if matches else path


def _sports_path_key(path, codec=None):
    normalized = path.replace('\\', '/')
    if not _LOCAL_WINDOWS_PATHS:
        return normalized
    folded = windows_path_casefold(normalized)
    return _encodable_key(folded, codec) if codec else folded


def _encodable_key(key, codec):
    # A folded character can be absent from a legacy server encoding. A
    # backslash escape is unambiguous because the path uses forward slashes here.
    return key.encode(codec, 'backslashreplace').decode(codec)


@cache
def _postgres_windows_case_mapping(codec):
    singles, expansions = defaultdict(list), []
    for original, folded in windows_path_case_mapping().items():
        original = chr(original)
        if original != _encodable_key(original, codec):
            continue
        folded = _encodable_key(folded, codec)
        if len(folded) == 1:
            singles[folded].append(original)
        else:
            expansions.append((original, folded))
    return singles, expansions


def _windows_postgres_codec(session):
    if not _LOCAL_WINDOWS_PATHS:
        return None
    bind = session if isinstance(session, sa.engine.Connection) else session.get_bind()
    if bind.dialect.name != 'postgresql':
        return None
    from psycopg2.extensions import encodings

    connection = session if isinstance(session, sa.engine.Connection) else session.connection()
    encoding = connection.connection.driver_connection.get_parameter_status('server_encoding')
    return encodings[encoding.replace('_', '')]


def _windows_sql_casefold(keys=(), codec=None):
    if not _LOCAL_WINDOWS_PATHS:
        return None
    if codec is None:
        return sa.func.unicode_path_casefold

    # PostgreSQL's lower() uses its database locale. Use the same case mapping
    # as Python and SQLite. For membership against known folded keys, only
    # characters that can produce text in those keys can affect the result.
    symbols = set(chain.from_iterable(keys))
    singles, expansions = _postgres_windows_case_mapping(codec)
    rules = [(value, symbol) for symbol in sorted(symbols) for value in singles.get(symbol, ())]
    args = tuple(sa.literal(''.join(values)) for values in zip(*rules))
    relevant = [(before, after) for before, after in expansions
                if symbols.issuperset(after) and any(after in key for key in keys)]
    expansion_chars = sa.literal(''.join(before for before, _ in relevant))
    expanded = [(sa.literal(before), sa.literal(after)) for before, after in relevant]

    def casefold(path):
        folded = sa.func.translate(path, *args) if args else path
        folded = folded.collate('C')
        if expanded:
            # Most paths contain no character that needs a multi-character fold.
            without_expansions = sa.func.translate(folded, expansion_chars, '')
            replacement = folded
            for before, after in expanded:
                replacement = sa.func.replace(replacement, before, after)
            folded = sa.case((without_expansions != folded, replacement), else_=folded)
        return folded

    return casefold


def _sports_sql_path_key(path, fold):
    normalized = sa.func.replace(path, '\\', '/')
    return fold(normalized) if fold is not None else normalized


def _mapped_sports_sql_path(path, rules):
    normalized = sa.func.replace(path, '\\', '/')
    matches = []
    for remote, destination in sorted(rules, key=lambda pair: len(pair[0]), reverse=True):
        prefix = remote.replace('\\', '/').rstrip('/')
        suffix = sa.func.substr(normalized, len(prefix) + 1)
        mapped = sa.literal(destination.rstrip('/\\')).concat(suffix)
        windows_drive = destination[:1].isascii() and destination[:1].isalpha() and destination[1:3] == ':\\'
        if destination.startswith('\\\\') or windows_drive:
            mapped = sa.func.replace(mapped, '/', '\\')
        matches.extend(((sa.func.rtrim(normalized, '/') == prefix, destination),
                        (sa.func.substr(normalized, 1, len(prefix) + 1) == prefix + '/', mapped)))
    return sa.case(*matches, else_=path) if matches else path


def arr_claims_stored_path(path, target):
    """Exclude direct claims from every enabled Arr kind in the mixed SQL results.

    Files that need a stat retain the scanner's recorded shadow. For Sportarr,
    compare against the mapped paths of the active owners in one SQL set.
    """
    clauses = defaultdict(list)
    sports_paths = []
    postgres_keys = set()
    owners = ArrOwners.load(target)
    codec = _windows_postgres_codec(target)
    fold = None if codec else _windows_sql_casefold()
    for instance in owners.instances.values():
        mapper = owners.mapper(instance.id)
        if mapper is None:
            continue
        model, _ = ArrOwners._columns(TABLE_OF_KIND[instance.kind])
        if mapper.sports:
            owned = (model.arr_instance_id == instance.id, model.path.is_not(None))
            if codec:
                paths = target.scalars(sa.select(model.path).where(*owned))
                postgres_keys.update(_sports_path_key(mapper.forward(value), codec) for value in paths)
            else:
                mapped = _mapped_sports_sql_path(model.path, mapper.rules)
                if fold is not None:
                    mapped = _sports_sql_path_key(mapped, fold)
                sports_paths.append(sa.select(mapped.label('path')).where(*owned))
            continue
        claim = model.path == path
        if mapper.rules:
            candidates = [_mapped_sql_path(path, [rule], reverse=True) for rule in mapper.rules]
            alternatives = (model.path == candidate for candidate in candidates)
            claim = sa.and_(sa.or_(claim, *alternatives), _mapped_sql_path(model.path, mapper.rules) == path)
        clauses[model].append(sa.and_(model.arr_instance_id == instance.id, claim))
    claims = [sa.select(model.id).where(sa.or_(*conditions)).correlate(path.table).exists()
              for model, conditions in clauses.items()]
    if sports_paths:
        local = _sports_sql_path_key(path, fold) if fold is not None else path
        claims.append(local.in_(sa.union_all(*sports_paths)))
    if postgres_keys:
        from sqlalchemy.dialects.postgresql import ARRAY

        folded = _sports_sql_path_key(path, _windows_sql_casefold(postgres_keys, codec))
        keys = sa.literal(list(postgres_keys), ARRAY(sa.Text))
        claims.append(folded.in_(sa.select(sa.func.unnest(keys))))
    return sa.or_(sa.false(), *claims)


def revalidate_shadow(item, owners, queue_scan=None, fetched=None):
    """Judge one shadowed item against the row that set it, not by matching its path again.

    Returns ``('keep', None)``, ``('clear', None)`` or ``('refresh', match)`` where the match carries the new
    recorded values. The row must still exist, belong to ``shadow_instance_id`` and its owner must still count;
    with the same file id and mapped path it is kept with no stat. If it holds another file id or path (the arr
    replaced or moved the file, or SQLite gave a deleted id to a new row) the item's file is matched against it
    again through both stages. When the item's own file cannot be statted the shadow is kept and a scan of the
    item's library is queued, so the item never drops into the wanted rule as a missing file. ``fetched`` is the
    answer of ``owners.fetch_many`` for a whole batch of items; without it the row is read on its own."""
    if (item.shadow_instance_id is None or item.shadow_arr_table not in KIND_OF_TABLE
            or item.shadow_arr_row_id is None):
        return 'clear', None
    if not owners.counts(item.shadow_instance_id):
        return 'clear', None
    key = (item.shadow_arr_table, item.shadow_arr_row_id)
    row = fetched.get(key) if fetched is not None else owners.fetch(*key)
    if row is None or row.instance_id != item.shadow_instance_id:
        return 'clear', None
    if row.file_id == item.shadow_arr_file_id and row.path == item.shadow_arr_path:
        return 'keep', None
    stat = stat_file(item.path)
    if stat is None:
        if queue_scan is not None:
            queue_scan(item)
        return 'keep', None
    index = ArrIndex([row])
    match = index.match(item.path, stat)
    return ('refresh', match) if match is not None else ('clear', None)
