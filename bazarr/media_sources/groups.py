# coding=utf-8
"""Resolving groups: which show each directory of a folder library holds, decided once per scan.

A path is only a name, and a directory's inode can be reused or, on a path-hashing filesystem, handed back to a
new directory at the same path. Neither may decide which show a directory holds. The evidence is a group's held
items: its items matched this scan by file identity, within this library. A group follows its items, whatever the
old directory now holds.

This module only plans: it reads nothing but the stats it is given and writes nothing. The scanner applies the
plan in one transaction, before any item is written, and only when the root guard holds.
"""
import errno
import os
from collections import namedtuple
from dataclasses import dataclass, field
from stat import S_ISDIR

from .identity import unsigned

#: ``status`` is ``'dir'`` (a readable directory), ``'absent'`` (stat says it is not there) or ``'unreadable'``
#: (any other failure: a permission error, a stale handle).
DirState = namedtuple('DirState', 'status dev ino')

_NOTE = "absorbed {titles}; their identity, profile and monitored settings were not carried"


def stat_directory(path):
    try:
        info = os.stat(path)
    except OSError as error:
        return DirState('absent' if error.errno in (errno.ENOENT, errno.ENOTDIR) else 'unreadable', None, None)
    except ValueError:
        return DirState('absent', None, None)
    if not S_ISDIR(info.st_mode):
        return DirState('absent', None, None)
    return DirState('dir', unsigned(info.st_dev), unsigned(info.st_ino))


def temporary_key(group_id):
    return f'pending:{group_id}'


@dataclass
class GroupPlan:
    #: Groups that stay on their own directory, with that directory's identity to record.
    stay: dict = field(default_factory=dict)
    #: Groups that move: group id -> the directory they take.
    move: dict = field(default_factory=dict)
    #: Groups that are gone: their items join the groups of the directories they are in.
    gone: set = field(default_factory=set)
    #: Groups whose directory could not be read: left exactly as they are, neither found nor gone.
    untouched: set = field(default_factory=set)
    #: The note a group that took a directory from others carries.
    notes: dict = field(default_factory=dict)
    #: Groups whose identity is looked up again from their directory name (never one the user set).
    reidentify: set = field(default_factory=set)
    #: Directory -> the group that holds it after resolution.
    target: dict = field(default_factory=dict)
    #: Directory -> its stat, for every directory resolution looked at.
    states: dict = field(default_factory=dict)
    #: One INFO line per merge.
    log: list = field(default_factory=list)

    @property
    def writes_nothing(self):
        return not (self.stay or self.move or self.gone or self.notes)


def plan_groups(groups, held_dirs, new_content, dir_state, *, dev_changed=False, protected=()):
    """Decide each group's fate.

    ``groups``: the library's group rows. ``held_dirs``: group id -> the directory of each of its held items.
    ``new_content``: directory -> how many new files and path-only matches it holds. ``dir_state(path)``: a
    ``DirState``. Steps 1 to 5 of the rules; step 6 (a fresh group for a directory nobody holds) happens when the
    first item is written, and step 7 (applying keys) in ``apply_plan``."""
    plan = GroupPlan()
    ordered = sorted(groups, key=lambda group: group.id)

    def state(path):
        if path not in plan.states:
            plan.states[path] = dir_state(path)
        return plan.states[path]

    claims = {}  # directory -> [(group id, 'stay' | 'move')]
    undecided = []
    for group in ordered:
        if group.id in protected or state(group.group_key).status == 'unreadable':
            plan.untouched.add(group.id)
            claims.setdefault(group.group_key, []).append((group.id, 'stay'))
            continue
        dirs = held_dirs.get(group.id, [])
        if group.group_key in dirs:
            # 1. Stays: a held item is still under its own key path.
            claims.setdefault(group.group_key, []).append((group.id, 'stay'))
        elif dirs:
            distinct = set(dirs)
            if len(distinct) == 1:
                # 2. Moves: every held item is in one other directory. A rename, whatever the old path holds.
                claims.setdefault(next(iter(distinct)), []).append((group.id, 'move'))
            else:
                # 4. Scattered over several other directories with none left at home: gone.
                plan.gone.add(group.id)
        else:
            undecided.append(group)

    targeted = set(claims)
    stay_three = set()
    for group in undecided:
        # 3. No held items: the group stays only on a directory that is still there, that no group of step 1 or
        # 2 targets, and that is either untouched or holds nothing new.
        current = state(group.group_key)
        gains = new_content.get(group.group_key, 0) > 0
        untouched_dir = (current.status == 'dir' and group.dir_ino is not None and group.dir_ino == current.ino
                         and (dev_changed or group.dir_dev == current.dev))
        if current.status == 'dir' and group.group_key not in targeted and (untouched_dir or not gains):
            claims.setdefault(group.group_key, []).append((group.id, 'stay'))
            stay_three.add(group.id)
            if gains and group.identity_source != 'user':
                plan.reidentify.add(group.id)
        else:
            plan.gone.add(group.id)

    by_id = {group.id: group for group in ordered}
    for directory, claimed in sorted(claims.items()):
        stayers = [group_id for group_id, how in claimed if how == 'stay']
        movers = sorted((group_id for group_id, how in claimed if how == 'move'),
                        key=lambda group_id: (-held_dirs[group_id].count(directory), group_id))
        if stayers:
            # A group staying on its own path always keeps it.
            winner, losers, moves = stayers[0], movers, False
        else:
            # 5. One group per directory: the one with the most held items in it, then the lowest id.
            winner, losers, moves = movers[0], movers[1:], True
        plan.target[directory] = winner
        if moves:
            plan.move[winner] = directory
            state(directory)
            if by_id[winner].identity_source != 'user':
                plan.reidentify.add(winner)
        elif winner not in plan.untouched:
            plan.stay[winner] = state(directory)
        plan.gone.update(losers)
        if losers:
            named = ', '.join(by_id[loser].title for loser in sorted(losers))
            plan.notes[winner] = _NOTE.format(titles=named)
            plan.log.append(
                f"Media source groups: {by_id[winner].title} (group {winner}) holds {directory} and absorbed "
                + ', '.join(f"{by_id[loser].title} (group {loser})" for loser in sorted(losers)))
    return plan


def apply_plan(plan, connection, update_group, identity_values):
    """Apply a plan in the caller's transaction: every moving and every gone group first takes a temporary key
    unique to it, then each moving group takes its final key. No two groups want one final key, since the plan
    leaves at most one group per directory, so the unique key is never hit."""
    for group_id in sorted(set(plan.move) | plan.gone):
        update_group(group_id, connection, group_key=temporary_key(group_id))
    for group_id, directory in sorted(plan.move.items()):
        values = dict(group_key=directory, dir_dev=plan.states[directory].dev, dir_ino=plan.states[directory].ino)
        values.update(identity_values.get(group_id, {}))
        update_group(group_id, connection, **values)
    for group_id, current in sorted(plan.stay.items()):
        values = dict(dir_dev=current.dev, dir_ino=current.ino)
        values.update(identity_values.get(group_id, {}))
        update_group(group_id, connection, **values)
    for group_id, note in sorted(plan.notes.items()):
        update_group(group_id, connection, resolution_note=note)
