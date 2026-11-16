"""The worker wire protocol's public names, for a worker built without the plugin runner.

These come from the files shared with the Bazarr+ host. Most plugins never
need them: subclass a kind's base class and let :mod:`bazarr_plugin_sdk.runner`
serve it.
"""

from ._protocol import frames
from ._protocol.worker_channel import (
    ChannelClosed,
    HostCallFailed,
    Operation,
    OperationCancelled,
    OperationError,
    OperationResult,
    WorkerChannel,
)

__all__ = [
    "ChannelClosed",
    "HostCallFailed",
    "Operation",
    "OperationCancelled",
    "OperationError",
    "OperationResult",
    "WorkerChannel",
    "frames",
]
