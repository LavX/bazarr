"""The ``bazarr.translator.v1`` kind.

A translator plugin subclasses :class:`TranslatorPlugin` and is named as the
runtime entry of its manifest. Each method may be a plain function or a
coroutine function; plain functions run in a worker thread so a blocking
library (an HTTP client without asyncio support, for example) never stalls the
event loop that answers cancel and heartbeat.

``options`` and ``test`` take ``secrets`` as an argument; ``translate`` finds
them under ``request["secrets"]``. Either way they come from the host's
separate ``secrets`` object, for that call only. Nothing reads secrets from the
environment or argv. A method that declares a ``ctx`` parameter receives the call's
:class:`~bazarr_plugin_sdk.context.PluginContext`.

The shapes of ``config``, ``request`` and the results are defined by the host
and the plugin's manifest, not by this module; the SDK carries them as JSON.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..context import PluginContext
from ..kinds import KindSpec, MethodSpec

KIND_API = "bazarr.translator.v1"


class TranslatorPlugin:
    """Base class of a translator plugin. Construct it without arguments."""

    kind_apis: ClassVar[tuple[str, ...]] = (KIND_API,)
    # The most translate calls this worker takes at once. The host never exceeds it.
    max_in_flight: ClassVar[int] = 1

    def describe(self) -> dict[str, Any]:
        """Runtime facts only: back-end types served, supported languages, limits.

        Settings schemas come from the signed manifest, not from here.
        """
        raise NotImplementedError

    def validate(self, config: dict[str, Any]) -> dict[str, Any]:
        """Normalize and check ``config`` without a network call.

        Return the normalized config or raise ``PluginError("invalid_config", ...)``
        with ``field_errors``.
        """
        return config

    def migrate(
        self, from_version: int, settings: dict[str, Any], connections: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Bring settings saved under an older ``settings_version`` to the current one.

        Return ``{"settings": ..., "connections": ...}``. No network call and no
        secret values.
        """
        return {"settings": settings, "connections": connections}

    async def options(
        self, field: str, config: dict[str, Any], secrets: dict[str, Any], ctx: PluginContext | None = None
    ) -> list[dict[str, Any]]:
        """Live choices for a settings field, such as models with prices."""
        return []

    async def test(
        self, config: dict[str, Any], secrets: dict[str, Any], ctx: PluginContext | None = None
    ) -> dict[str, Any]:
        """A real call that confirms the credentials and model."""
        raise NotImplementedError

    async def translate(self, request: dict[str, Any], ctx: PluginContext) -> dict[str, Any]:
        """Translate the request. Report progress, holds, usage and checkpoints through ``ctx``."""
        raise NotImplementedError


SPEC = KindSpec(
    kind_api=KIND_API,
    wire_kind="translator",
    major=1,
    minor=0,
    base_class=TranslatorPlugin,
    methods={
        "describe": MethodSpec("describe"),
        "validate": MethodSpec("validate", ("config",)),
        "migrate": MethodSpec("migrate", ("from_version", "settings", "connections")),
        "options": MethodSpec("options", ("field", "config"), secrets=True),
        "test": MethodSpec("test", ("config",), secrets=True),
        "translate": MethodSpec("translate", ("request",), embed_secrets_in="request"),
    },
)

__all__ = ["KIND_API", "SPEC", "TranslatorPlugin"]
