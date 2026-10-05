from __future__ import annotations

class RoomPipeline:
    def __init__(self) -> None:
        self.results: dict[tuple[str, int], dict] = {}

    def accept(self, room_id: str, session_id: str, seq: int, zh: str, en: str = "", translate_error: str = "") -> tuple[dict, bool]:
        key = (session_id, seq)
        if key in self.results:
            return self.results[key], False
        event = {
            "type": "final",
            "room_id": room_id,
            "session_id": session_id,
            "seq": seq,
            "zh": zh,
            "en": en,
            "translate_status": "error" if translate_error else ("ok" if en else "off"),
            "error": translate_error,
        }
        self.results[key] = event
        return event, True

    def update_translation(self, session_id: str, seq: int, en: str) -> dict | None:
        event = self.results.get((session_id, seq))
        if not event:
            return None
        event["en"] = en
        event["translate_status"] = "ok"
        event["error"] = ""
        event["type"] = "update"
        return event

    def ordered(self, session_id: str) -> list[dict]:
        rows = [v for (sid, _), v in self.results.items() if sid == session_id]
        return sorted(rows, key=lambda item: item["seq"])
