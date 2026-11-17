# coding=utf-8
"""A manual pick for one translation: one back end, or one engine that has no back ends.

The job record has one field for a pick, ``pinned_connection_id``, and a whole number in it says which kind of pick
it is: a connection's id as it is, or an engine's id as its negative. Engines and connections both number from 1,
so the two never meet, and the record needs no field of its own for an engine. This module is the only place that
knows the encoding. It is pure, so every caller can use it with the new translator off.
"""
from __future__ import annotations

BOTH = 'Pick a back end or an engine, not both.'


def pin_value(connection_id=None, engine_id=None):
    """What the job record carries for this pick, or None for no pick. Raises ``ValueError`` for both at once."""
    if connection_id is not None and engine_id is not None:
        raise ValueError(BOTH)
    if connection_id is not None:
        return int(connection_id)
    if engine_id is not None:
        return -int(engine_id)
    return None


def split_pin(pin):
    """``("engine", id)`` or ``("connection", id)`` for a value ``pin_value`` made."""
    return ('engine', -pin) if pin < 0 else ('connection', pin)
