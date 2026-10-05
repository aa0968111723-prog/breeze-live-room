from __future__ import annotations

import re

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RoomIdError(ValueError):
    pass


def validate_room_id(raw: str) -> str:
    room_id = (raw or "").strip()
    if not ROOM_RE.fullmatch(room_id):
        raise RoomIdError("房間代號只能用英文、數字、底線和減號，長度 1 到 64。不接受空白、中文或過長代號。")
    return room_id
