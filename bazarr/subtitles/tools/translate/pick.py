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

# The queue jobs that carry a pick, so a job that finds the new translator switched off can say so instead of
# running on the old one without the pick. A request notes that its pick is on its way (``expect``), and the queue
# job it adds takes the note (``bind``). In memory: the queue itself is, and with the switch off nothing reads a
# table. Bounded, oldest first out.
_NOTED_LIMIT = 1000
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


def bind(queue_job_id) -> None:
    """The request added this queue job: it is a picked one when the request said so. Always clears the note."""
    expected = getattr(_local, 'expected', False)
    _local.expected = False
    if not expected or not queue_job_id:
        return
    _noted.pop(queue_job_id, None)
    _noted[queue_job_id] = True
    while len(_noted) > _NOTED_LIMIT:
        _noted.pop(next(iter(_noted)))


def take(queue_job_id) -> bool:
    """Whether this queue job is a picked one. Reading it forgets it."""
    return _noted.pop(queue_job_id, None) is not None
