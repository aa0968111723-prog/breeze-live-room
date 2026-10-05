from __future__ import annotations

class RoomBus:
    """Events stay with their room. A request never replays another room's slice."""

    def __init__(self) -> None:
        self.by_room: dict[str, list[dict]] = {}

    def publish(self, event: dict) -> None:
        room = event.get("room_id") or ""
        rows = self.by_room.setdefault(room, [])
        for index, item in enumerate(rows):
            if item.get("id") == event.get("id") and item.get("version") == event.get("version"):
                return
            if item.get("id") == event.get("id"):
                rows[index] = event
                return
        rows.append(event)
        self.by_room[room] = rows[-80:]

    def history(self, room_id: str) -> list[dict]:
        return list(self.by_room.get(room_id, []))
