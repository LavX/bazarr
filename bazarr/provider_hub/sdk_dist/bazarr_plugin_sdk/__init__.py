"""Bazarr+ plugin SDK: the contract between a plugin and the Bazarr+ host.

A plugin subclasses the base class of its kind (for translators,
:class:`bazarr_plugin_sdk.translator.v1.TranslatorPlugin`), raises
:class:`PluginError` for failures the host should act on, and reports progress,
holds, usage and checkpoints through the :class:`PluginContext` it receives.
The wire between worker and host is private to this package and Bazarr+; the
Python interface of each kind is the contract.

Standard library only.
"""

__version__ = "0.1.0"

from ._protocol.frames import PROTOCOL_MAJOR, PROTOCOL_MINOR
from .context import PluginContext
from .errors import ERROR_CODES, PluginError

__all__ = ["ERROR_CODES", "PROTOCOL_MAJOR", "PROTOCOL_MINOR", "PluginContext", "PluginError", "__version__"]
