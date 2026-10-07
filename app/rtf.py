"""Bounded recognition real-time factor (RTF = asr_ms / audio_ms).

A class is about 1000 slices. The recent window keeps 200 samples. Each
(room, session) keeps its own capped series, so two rooms that take turns
do not wipe each other. A new session in the same room replaces only that
room. ASR timeouts are counted and are not samples.
"""
from __future__ import annotations

from collections import deque

WINDOW_LIMIT = 200
# Above one 100-minute class (~1000 slices) plus retries, still fixed.
SESSION_LIMIT = 4096
# One live session per room. Well above the default room cap, still fixed.
SESSION_ROOM_CAP = 32


def percentile(values: list[float], p: float) -> float:
    """Linear rank, same shape as the simulation helper. Empty input is refused."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _num(value: float) -> int | float:
    number = round(float(value), 6)
    if number == int(number) and abs(number) < 10**12:
        return int(number)
    return number


def _stat(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "max": None}
    return {
        "p50": _num(percentile(values, 0.50)),
        "p95": _num(percentile(values, 0.95)),
        "max": _num(max(values)),
    }


def summarize(samples: list[tuple[int, int, float]], limit: int) -> dict:
    return {
        "count": len(samples),
        "limit": limit,
        "asr_ms": _stat([row[0] for row in samples]),
        "audio_ms": _stat([row[1] for row in samples]),
        "rtf": _stat([row[2] for row in samples]),
    }


class RtfMeter:
    """Recent-window and per-room session samples, plus audio still waiting."""

    def __init__(self) -> None:
        self._window: deque[tuple[int, int, float]] = deque(maxlen=WINDOW_LIMIT)
        # Insertion order is least-recently updated first. Touching a session moves it to the end.
        self._sessions: dict[tuple[str, str], deque[tuple[int, int, float]]] = {}
        self._timeouts: dict[tuple[str, str], int] = {}
        self._latest: tuple[str, str] | None = None
        self._waiting: dict[tuple, float] = {}
        self._asr_timeouts = 0

    def note_waiting(self, key: tuple, seconds: float) -> None:
        if seconds and seconds > 0:
            self._waiting[key] = float(seconds)

    def clear_waiting(self, key: tuple) -> None:
        self._waiting.pop(key, None)

    def note_timeout(self, session: tuple[str, str] | None = None) -> None:
        """Count one ASR timeout. Do not append an RTF sample."""
        self._asr_timeouts += 1
        if session is None:
            return
        self._bucket(session)
        self._timeouts[session] = self._timeouts.get(session, 0) + 1
        self._latest = session

    def drop_room(self, room_id: str) -> None:
        """Forget one ended room's samples. The process-wide timeout total stays."""
        stale = [key for key in self._sessions if key[0] == room_id]
        for key in stale:
            self._sessions.pop(key, None)
            self._timeouts.pop(key, None)
        waiting = [key for key in self._waiting if isinstance(key, tuple) and key and key[0] == room_id]
        for key in waiting:
            self._waiting.pop(key, None)
        if self._latest is not None and self._latest[0] == room_id:
            self._latest = next(reversed(self._sessions), None)

    def record(self, asr_s: float, audio_s: float, session: tuple[str, str] | None = None) -> None:
        self.record_ms(int(round(float(asr_s) * 1000)), int(round(float(audio_s) * 1000)), session)

    def record_ms(self, asr_ms: int, audio_ms: int, session: tuple[str, str] | None = None) -> None:
        if audio_ms <= 0:
            return
        sample = (max(0, int(asr_ms)), int(audio_ms), max(0, int(asr_ms)) / int(audio_ms))
        self._window.append(sample)
        key = session if session is not None else self._latest
        if key is None:
            key = ("", "")
        self._bucket(key).append(sample)
        self._latest = key

    def _bucket(self, session: tuple[str, str]) -> deque[tuple[int, int, float]]:
        """One live session per room. A different session replaces only that room."""
        room = session[0]
        if room:
            stale = [key for key in self._sessions if key[0] == room and key != session]
            for key in stale:
                self._sessions.pop(key, None)
                self._timeouts.pop(key, None)
        bucket = self._sessions.pop(session, None)
        if bucket is None:
            bucket = deque(maxlen=SESSION_LIMIT)
        self._sessions[session] = bucket
        while len(self._sessions) > SESSION_ROOM_CAP:
            old = next(iter(self._sessions))
            if old == session:
                break
            self._sessions.pop(old, None)
            self._timeouts.pop(old, None)
        return bucket

    def snapshot(self, session: tuple[str, str] | None = None) -> dict:
        """``rtf.session`` is the requested session, or the one updated most recently."""
        chosen = session if session is not None else self._latest
        bucket = self._sessions.get(chosen) if chosen is not None else None
        samples = list(bucket) if bucket is not None else []
        sessions = []
        for key, rows in self._sessions.items():
            item = summarize(list(rows), SESSION_LIMIT)
            item["room_id"] = key[0]
            item["session_id"] = key[1]
            item["asr_timeouts"] = int(self._timeouts.get(key, 0))
            sessions.append(item)
        return {
            "backlog_audio_s": round(sum(self._waiting.values()), 3),
            "asr_timeouts": self._asr_timeouts,
            "rtf": {
                "window": summarize(list(self._window), WINDOW_LIMIT),
                "session": summarize(samples, SESSION_LIMIT),
                "sessions": sessions,
            },
        }
