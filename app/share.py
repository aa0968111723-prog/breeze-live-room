from __future__ import annotations

import os
import socket
from urllib.parse import quote


def lan_ip() -> str | None:
    override = os.getenv("BREEZE_SHARE_HOST", "").strip()
    if override:
        return override
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not ip or ip.startswith("127."):
        return None
    return ip


def listen_url(room_id: str, port: int, scheme: str = "http") -> str | None:
    ip = lan_ip()
    if not ip:
        return None
    return f"{scheme}://{ip}:{port}/r/{quote(room_id)}"
