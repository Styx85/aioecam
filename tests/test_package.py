import aioecam
from aioecam import EcamError, FrameError


def test_version_is_set() -> None:
    assert aioecam.__version__


def test_exception_hierarchy() -> None:
    assert issubclass(FrameError, EcamError)
