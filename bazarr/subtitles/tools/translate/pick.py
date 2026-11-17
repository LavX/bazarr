# coding=utf-8
"""A manual pick for one translation: one back end, or one engine that has no back ends.

The job record has one field for a pick, ``pinned_connection_id``, and a whole number in it says which kind of pick
it is: a connection's id as it is, or an engine's id as its negative. Engines and connections both number from 1,
so the two never meet, and the record needs no field of its own for an engine. This module is the only place that
knows the encoding. It is pure, so every caller can use it with the new translator off.
"""
from __future__ import annotations

import threading

BOTH = 'Pick a back end or an engine, not both.'


def _id(value, name):
    # A sign is what tells the two kinds apart in the record, so an id below 1 must never get that far.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a whole number of 1 or more')
    return value


def pin_value(connection_id=None, engine_id=None):
    """What the job record carries for this pick, or None for no pick.

    Raises ``ValueError`` for both at once, and for an id that is not a whole number of 1 or more.
    """
    if connection_id is not None and engine_id is not None:
        raise ValueError(BOTH)
    if connection_id is not None:
        return _id(connection_id, 'connection_id')
    if engine_id is not None:
        return -_id(engine_id, 'engine_id')
    return None


def split_pin(pin):
    """``("engine", id)`` or ``("connection", id)`` for a value ``pin_value`` made."""
    return ('engine', -pin) if pin < 0 else ('connection', pin)


SWITCHED_OFF_REASON = 'pick_switched_off'
SWITCHED_OFF = ('The new translator was switched off while this translation waited, so the back end or engine '
                'you picked was not used. Translate again once it is back on.')

# The requests whose queued job carries a pick, so a job that finds the new translator switched off can say so
# instead of running on the old one without the pick. They are kept by the request's own key (its source and
# target), never by the queue job's id: a retry through the Jobs page adds a new queue job for the same call, and it
# is still the picked one. A request notes that its pick is on its way (``expect``), and the queue job it adds binds
# it (``bind``). Any later request for the same key, picked or not, replaces the note, and a translation that
# finishes forgets it. In memory: the queue itself is, and with the switch off nothing reads a table. Bounded,
# oldest first out.
_NOTED_LIMIT = 20000
_noted: dict = {}
_local = threading.local()


def expect() -> None:
    """The request this thread is making carries a pick: the next queue job it adds is a picked one."""
    _local.expected = True


def expecting() -> bool:
    """Whether the request this thread is making has just recorded a pick."""
    return getattr(_local, 'expected', False)


def forget_expected() -> None:
    """This thread's request carries no pick."""
    _local.expected = False


def bind(key) -> None:
    """The request for this key has queued its job (or joined one): it is a picked one when the request said so.

    Always clears the thread's note, so a request that never got here cannot lend its pick to the next one.
    """
    expected = getattr(_local, 'expected', False)
    _local.expected = False
    _noted.pop(key, None)
    if not expected:
        return
    _noted[key] = True
    while len(_noted) > _NOTED_LIMIT:
        _noted.pop(next(iter(_noted)))


def noted(key) -> bool:
    """Whether the queued job for this key was made for a pick. Reading it keeps it: a retry asks again."""
    return key in _noted


def forget(key) -> None:
    """The translation for this key is done: its pick no longer waits on anything."""
    _noted.pop(key, None)
