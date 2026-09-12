"""Short-lived observations of explicit searches, separate from result handles."""
from copy import deepcopy
from threading import Lock
from time import monotonic

_lock = Lock()
_searches = {}
_TTL = 300
_LIMIT = 128


def begin(identity):
    with _lock:
        now = monotonic()
        for key in list(_searches):
            if now - _searches[key][0] > _TTL:
                del _searches[key]
        if identity in _searches:
            return False
        if len(_searches) >= _LIMIT:
            finished = [key for key, (_, observation) in _searches.items()
                        if observation["phase"] == "finished"]
            if not finished:
                return False
            del _searches[min(finished, key=lambda key: _searches[key][0])]
        _searches[identity] = (now, {"phase": "preparing", "providers": []})
        return True


def publish(identity, observation):
    with _lock:
        if identity in _searches:
            _searches[identity] = (monotonic(), deepcopy(observation))


def finish(identity):
    with _lock:
        if identity in _searches:
            _, observation = _searches[identity]
            _searches[identity] = (monotonic(), {**observation, "phase": "finished"})


def read(identity):
    with _lock:
        entry = _searches.get(identity)
        if entry is None:
            return None
        if monotonic() - entry[0] > _TTL:
            del _searches[identity]
            return None
        return deepcopy(entry[1])
