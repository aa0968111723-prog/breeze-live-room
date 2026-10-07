"""Bounded recognition real-time factor (RTF = asr_ms / audio_ms).

A class is about 1000 slices. The recent window keeps 200 samples and the
current session keeps a fixed cap, so a long run cannot grow this store.
"""
from __future__ import annotations

from collections import deque

WINDOW_LIMIT = 200
# Above one 100-minute class (~1000 slices) plus retries, still fixed.
SESSION_LIMIT = 4096


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
    """Recent-window and current-session recognition samples, plus audio still waiting."""

    def __init__(self) -> None:
        self._window: deque[tuple[int, int, float]] = deque(maxlen=WINDOW_LIMIT)
        self._session: deque[tuple[int, int, float]] = deque(maxlen=SESSION_LIMIT)
        self._session_key: tuple[str, str] | None = None
        self._waiting: dict[tuple, float] = {}

    def note_waiting(self, key: tuple, seconds: float) -> None:
        if seconds and seconds > 0:
            self._waiting[key] = float(seconds)

    def clear_waiting(self, key: tuple) -> None:
        self._waiting.pop(key, None)

    def record(self, asr_s: float, audio_s: float, session: tuple[str, str] | None = None) -> None:
        self.record_ms(int(round(float(asr_s) * 1000)), int(round(float(audio_s) * 1000)), session)

    def record_ms(self, asr_ms: int, audio_ms: int, session: tuple[str, str] | None = None) -> None:
        if audio_ms <= 0:
            return
        if session is not None and session != self._session_key:
            self._session_key = session
            self._session.clear()
        sample = (max(0, int(asr_ms)), int(audio_ms), max(0, int(asr_ms)) / int(audio_ms))
        self._window.append(sample)
        self._session.append(sample)

    def snapshot(self) -> dict:
        return {
            "backlog_audio_s": round(sum(self._waiting.values()), 3),
            "rtf": {
                "window": summarize(list(self._window), WINDOW_LIMIT),
                "session": summarize(list(self._session), SESSION_LIMIT),
            },
        }
