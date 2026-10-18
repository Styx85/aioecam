"""Small shared helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def remover(items: list[Any], item: Any) -> Callable[[], None]:
    """An unsubscribe callable that is safe to call more than once."""

    def unsubscribe() -> None:
        if item in items:
            items.remove(item)

    return unsubscribe
