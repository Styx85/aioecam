"""Small network helpers."""

from __future__ import annotations

import socket


def source_ip_towards(device_ip: str) -> str:
    """The local address the OS would use to reach ``device_ip`` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((device_ip, 80))
        address: str = sock.getsockname()[0]
        return address
