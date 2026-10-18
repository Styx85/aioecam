"""Exception hierarchy. Everything raised by aioecam derives from EcamError."""


class EcamError(Exception):
    """Base class for all aioecam errors."""


class FrameError(EcamError):
    """An ECAM frame is malformed or fails its CRC check."""


class NotConnected(EcamError):
    """No active session to the machine."""


class CommandTimeout(EcamError):
    """The machine did not confirm a command in time."""


class CommandRejected(EcamError):
    """The machine refused a command (e.g. not ready, missing accessory)."""


class AuthError(EcamError):
    """Cloud credentials or tokens were rejected."""
