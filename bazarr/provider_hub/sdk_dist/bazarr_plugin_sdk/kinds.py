"""What a plugin kind exposes over the wire.

A kind names the operations the host may request, the wire arguments of each, and
whether a method receives the request's secrets. The runner needs nothing else
to serve a plugin of that kind. A kind module registers one :class:`KindSpec`
per ``kind_api`` it serves; an older major stays registered next to a newer one
so a worker can serve a host that still speaks the old one.
"""

from __future__ import annotations

import importlib

from dataclasses import dataclass
from typing import Any

# kind_api -> module that defines SPEC. Imported on demand so a worker loads one kind only.
_KIND_MODULES = {
    "bazarr.translator.v1": "bazarr_plugin_sdk.translator.v1",
}


@dataclass(frozen=True)
class MethodSpec:
    """One callable operation.

    ``params`` are the wire argument names, all required. ``secrets`` says the
    method takes the call's ``secrets`` object as a keyword of that name.
    ``embed_secrets_in`` names a dict argument that receives the secrets under
    its ``"secrets"`` key instead, for a method whose request carries them.
    """

    name: str
    params: tuple[str, ...] = ()
    secrets: bool = False
    embed_secrets_in: str | None = None


@dataclass(frozen=True)
class KindSpec:
    """A kind API as the worker offers it in its hello.

    ``wire_kind`` and ``major``/``minor`` are what the handshake carries, so
    ``bazarr.translator.v1`` is offered as ``{"kind": "translator", "major": 1}``.
    """

    kind_api: str
    wire_kind: str
    major: int
    minor: int
    base_class: type
    methods: dict[str, MethodSpec]

    def offer(self) -> dict[str, Any]:
        return {"kind": self.wire_kind, "major": self.major, "minor": self.minor}


def known_kind_apis() -> tuple[str, ...]:
    return tuple(sorted(_KIND_MODULES))


def get_kind(kind_api: str) -> KindSpec | None:
    """The spec for ``kind_api``, or ``None`` when this SDK does not serve it."""
    module_name = _KIND_MODULES.get(kind_api)
    if module_name is None:
        return None
    module: Any = importlib.import_module(module_name)
    return module.SPEC
