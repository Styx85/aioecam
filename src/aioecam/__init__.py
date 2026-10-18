"""Async local-first client for De'Longhi ECAM coffee machines.

Layers (see docs/plan.md):

- :mod:`aioecam.ecam`  - machine protocol codec, no I/O
- :mod:`aioecam.lan`   - Ayla LAN mode transport
- :mod:`aioecam.cloud` - Gigya/Ayla cloud, onboarding and fallback only
"""

from importlib.metadata import PackageNotFoundError, version

from aioecam.exceptions import (
    AuthError,
    CommandRejected,
    CommandTimeout,
    EcamError,
    FrameError,
    NotConnected,
)

try:
    __version__ = version("aioecam")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"

__all__ = [
    "AuthError",
    "CommandRejected",
    "CommandTimeout",
    "EcamError",
    "FrameError",
    "NotConnected",
    "__version__",
]
