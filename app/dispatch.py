from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class RoomBus:
    """Per-room event log. A publish never lands in another room's history."""

    def __init__(self, limit: int = 200):
        self.limit = limit
        self.by_room: dict[str, list[dict]] = {}
        self._log: dict[str, list[dict]] = {}
        self._seen: dict[str, set[tuple[str, int]]] = {}
        self._ver: dict[str, dict[str, int]] = {}

    def publish(self, event: dict) -> dict | None:
        room = str(event.get("room_id") or "")
        snap = {key: event.get(key) for key in (
            "type", "id", "room_id", "session_id", "session_ord", "seq", "version",
            "zh", "en", "status", "translate_status", "error", "t0_ms", "t1_ms", "zh_raw",
        )}
        snap["type"] = snap.get("type") or "caption"
        snap["room_id"] = room
        seg_id = str(snap.get("id") or "")
        if seg_id:
            snap["id"] = seg_id
        version = int(snap.get("version") or 1)
        snap["version"] = version
        if seg_id:
            seen = self._seen.setdefault(room, set())
            marker = (seg_id, version)
            if marker in seen:
                return None
            best = self._ver.get(room, {}).get(seg_id)
            if best is not None and version <= best:
                seen.add(marker)
                return None
            seen.add(marker)
            self._ver.setdefault(room, {})[seg_id] = version
        log = self._log.setdefault(room, [])
        snap["cursor"] = (log[-1]["cursor"] + 1) if log else 1
        stored = dict(snap)
        log.append(stored)
        if len(log) > self.limit:
            del log[: len(log) - self.limit]
        rows = self.by_room.setdefault(room, [])
        replaced = False
        for index, item in enumerate(rows):
            if seg_id and str(item.get("id") or "") == seg_id:
                if version >= int(item.get("version") or 0):
                    rows[index] = stored
                replaced = True
                break
        if not replaced:
            rows.append(stored)
        rows.sort(key=lambda item: (int(item.get("session_ord") or 0), int(item.get("seq") or 0), int(item.get("cursor") or 0)))
        if len(rows) > self.limit:
            del rows[: len(rows) - self.limit]
        self.by_room[room] = rows
        return dict(stored)

    def history(self, room_id: str) -> list[dict]:
        return [dict(item) for item in self.by_room.get(room_id, [])]

    def since(self, room_id: str, cursor: int) -> dict:
        log = self._log.get(room_id, [])
        if not log:
            return {"events": [], "gap": False, "oldest_cursor": 0, "latest_cursor": 0}
        oldest = int(log[0]["cursor"])
        latest = int(log[-1]["cursor"])
        gap = cursor > 0 and oldest > cursor + 1
        events = [dict(item) for item in log if int(item["cursor"]) > cursor]
        return {"events": events, "gap": gap, "oldest_cursor": oldest, "latest_cursor": latest}

    def latest_cursor(self, room_id: str) -> int:
        log = self._log.get(room_id, [])
        if not log:
            return 0
        return int(log[-1]["cursor"])

    def drop(self, room_id: str) -> None:
        self.by_room.pop(room_id, None)
        self._log.pop(room_id, None)
        self._seen.pop(room_id, None)
        self._ver.pop(room_id, None)
        self._ver.pop(room_id, None)


class ListenerSlot:
    """Bounded per-connection send queue. offer() never waits on the socket."""

    def __init__(self, sender: Callable[[dict], Awaitable[None]], maxsize: int = 32, send_timeout: float = 2.0):
        self.sender = sender
        self.q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.send_timeout = send_timeout
        self.task: asyncio.Task | None = None
        self.alive = True
        self.dropped = 0
        self.last_pong = 0.0

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                msg = await self.q.get()
                if msg is None:
                    return
                await asyncio.wait_for(self.sender(msg), timeout=self.send_timeout)
        except Exception:
            self.alive = False
        finally:
            self.alive = False

    def offer(self, msg: dict) -> bool:
        if not self.alive:
            return False
        try:
            self.q.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self.alive = False
            return False

    async def close(self) -> None:
        self.alive = False
        try:
            self.q.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=0.2)
            except Exception:
                self.task.cancel()
