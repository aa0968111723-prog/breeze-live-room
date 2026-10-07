"""Bounded recognition real-time factor (RTF = asr_ms / audio_ms).

A class is about 1000 slices. The recent window keeps 200 samples. Each
(room, session) keeps its own capped series, so two rooms that take turns
do not wipe each other. A new session in the same room replaces only that
room. ASR timeouts are counted and are not samples.

Percentile summaries are cached per bucket and recomputed only when that
bucket receives a sample. Waiting audio, timeouts, and the skip counters
are read live so a metrics poll still sees them.

``last_process_ms`` is not stored here. It is the pipeline's decode-plus-
recognition time for the last slice and does not include ASR slot wait.
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


def _column(samples: list[tuple], index: int) -> list[float]:
    if not samples:
        return []
    if len(samples[0]) <= index:
        return [0.0] * len(samples) if index >= 3 else []
    return [row[index] for row in samples]


def summarize(samples: list[tuple], limit: int) -> dict:
    return {
        "count": len(samples),
        "limit": limit,
        "asr_ms": _stat(_column(samples, 0)),
        "audio_ms": _stat(_column(samples, 1)),
        "rtf": _stat(_column(samples, 2)),
        "decode_ms": _stat(_column(samples, 3)),
        "asr_wait_ms": _stat(_column(samples, 4)),
    }


class RtfMeter:
    """Recent-window and per-room session samples, plus audio still waiting.

    Waiting audio is counted from upload and removed when ASR starts on that
    segment. Before the decoded duration is known the slice is an estimate
    (``backlog_estimated``). Each room's waiting audio is separate.
    """

    def __init__(self) -> None:
        self._window: deque[tuple] = deque(maxlen=WINDOW_LIMIT)
        self._window_rev = 0
        self._window_cache: tuple[int, dict] | None = None
        # Insertion order is least-recently updated first. Touching a session moves it to the end.
        self._sessions: dict[tuple[str, str], deque] = {}
        self._bucket_rev: dict[tuple[str, str], int] = {}
        self._bucket_cache: dict[tuple[str, str], tuple[int, dict]] = {}
        self._timeouts: dict[tuple[str, str], int] = {}
        self._silent: dict[tuple[str, str], int] = {}
        self._empty: dict[tuple[str, str], int] = {}
        self._latest: tuple[str, str] | None = None
        # key -> (seconds, estimated). Key is the segment (room, session, seq).
        self._waiting: dict[tuple, tuple[float, bool]] = {}
        self._asr_timeouts = 0
        self._silent_skipped = 0
        self._asr_empty = 0
        self._last_rtf: float | None = None

    def note_waiting(self, key: tuple, seconds: float, estimated: bool = False) -> None:
        if seconds and seconds > 0:
            self._waiting[key] = (float(seconds), bool(estimated))

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

    def note_silent_skip(self, session: tuple[str, str] | None = None) -> None:
        """Silence gate skipped ASR. Not an RTF sample and not an empty result."""
        self._silent_skipped += 1
        if session is not None:
            self._silent[session] = self._silent.get(session, 0) + 1

    def note_empty(self, session: tuple[str, str] | None = None) -> None:
        """ASR returned no words. Not a timeout and not a silence-gate skip."""
        self._asr_empty += 1
        if session is not None:
            self._empty[session] = self._empty.get(session, 0) + 1

    def _forget_session(self, key: tuple[str, str]) -> None:
        self._sessions.pop(key, None)
        self._timeouts.pop(key, None)
        self._silent.pop(key, None)
        self._empty.pop(key, None)
        self._bucket_rev.pop(key, None)
        self._bucket_cache.pop(key, None)

    def drop_room(self, room_id: str) -> None:
        """Forget one ended room's samples. Process-wide counters stay."""
        stale = [key for key in self._sessions if key[0] == room_id]
        for key in stale:
            self._forget_session(key)
        for store in (self._timeouts, self._silent, self._empty):
            for key in [key for key in store if key[0] == room_id]:
                store.pop(key, None)
        waiting = [key for key in self._waiting if isinstance(key, tuple) and key and key[0] == room_id]
        for key in waiting:
            self._waiting.pop(key, None)
        if self._latest is not None and self._latest[0] == room_id:
            self._latest = next(reversed(self._sessions), None)

    def record(self, asr_s: float, audio_s: float, session: tuple[str, str] | None = None, decode_s: float = 0.0, wait_s: float = 0.0) -> None:
        self.record_ms(
            int(round(float(asr_s) * 1000)),
            int(round(float(audio_s) * 1000)),
            session,
            decode_ms=int(round(float(decode_s) * 1000)),
            wait_ms=int(round(float(wait_s) * 1000)),
        )

    def record_ms(self, asr_ms: int, audio_ms: int, session: tuple[str, str] | None = None, decode_ms: int = 0, wait_ms: int = 0) -> None:
        if audio_ms <= 0:
            return
        asr_value = max(0, int(asr_ms))
        audio_value = int(audio_ms)
        sample = (
            asr_value,
            audio_value,
            asr_value / audio_value,
            max(0, int(decode_ms)),
            max(0, int(wait_ms)),
        )
        self._window.append(sample)
        self._window_rev += 1
        self._last_rtf = sample[2]
        key = session if session is not None else self._latest
        if key is None:
            key = ("", "")
        self._bucket(key).append(sample)
        self._bucket_rev[key] = self._bucket_rev.get(key, 0) + 1
        self._latest = key

    def _bucket(self, session: tuple[str, str]) -> deque:
        """One live session per room. A different session replaces only that room."""
        room = session[0]
        if room:
            stale = [key for key in self._sessions if key[0] == room and key != session]
            for key in stale:
                self._forget_session(key)
        bucket = self._sessions.pop(session, None)
        if bucket is None:
            bucket = deque(maxlen=SESSION_LIMIT)
        self._sessions[session] = bucket
        while len(self._sessions) > SESSION_ROOM_CAP:
            old = next(iter(self._sessions))
            if old == session:
                break
            self._forget_session(old)
        return bucket

    def _summary_for(self, key: tuple[str, str], rows: deque) -> dict:
        rev = self._bucket_rev.get(key, 0)
        cached = self._bucket_cache.get(key)
        if cached is not None and cached[0] == rev:
            return cached[1]
        samples = list(rows)
        summary = summarize(samples, SESSION_LIMIT)
        summary["recent"] = summarize(samples[-WINDOW_LIMIT:], WINDOW_LIMIT)
        self._bucket_cache[key] = (rev, summary)
        return summary

    def _window_summary(self) -> dict:
        cached = self._window_cache
        if cached is not None and cached[0] == self._window_rev:
            return cached[1]
        summary = summarize(list(self._window), WINDOW_LIMIT)
        self._window_cache = (self._window_rev, summary)
        return summary

    def _backlog(self) -> tuple[float, bool, dict[str, float]]:
        raw: dict[str, float] = {}
        estimated = False
        total = 0.0
        for key, item in self._waiting.items():
            seconds, is_estimate = item
            total += seconds
            estimated = estimated or bool(is_estimate)
            room = key[0] if isinstance(key, tuple) and key else ""
            raw[room] = raw.get(room, 0.0) + seconds
        by_room = {room: round(value, 3) for room, value in raw.items() if value > 0}
        return round(total, 3), estimated, by_room

    def snapshot(self, session: tuple[str, str] | None = None) -> dict:
        """``rtf.session`` is the requested session, or the one updated most recently.

        ``asr_rtf_p50`` / ``asr_rtf_p95`` are the process-wide recent window.
        Each ``rtf.sessions`` row also has ``recent`` (that session's last 200).
        ``backlog_s`` matches ``backlog_audio_s``: audio received but not yet in ASR.
        """
        chosen = session if session is not None else self._latest
        window = self._window_summary()
        sessions = []
        chosen_summary = summarize([], SESSION_LIMIT)
        chosen_summary["recent"] = summarize([], WINDOW_LIMIT)
        for key, rows in self._sessions.items():
            summary = self._summary_for(key, rows)
            if key == chosen:
                chosen_summary = summary
            item = dict(summary)
            item["room_id"] = key[0]
            item["session_id"] = key[1]
            item["asr_timeouts"] = int(self._timeouts.get(key, 0))
            item["silent_skipped"] = int(self._silent.get(key, 0))
            item["asr_empty"] = int(self._empty.get(key, 0))
            sessions.append(item)
        if chosen is not None and chosen not in self._sessions:
            chosen_summary = summarize([], SESSION_LIMIT)
            chosen_summary["recent"] = summarize([], WINDOW_LIMIT)
        backlog_s, backlog_estimated, by_room = self._backlog()
        rtf_block = window["rtf"]
        return {
            "backlog_audio_s": backlog_s,
            "backlog_s": backlog_s,
            "backlog_estimated": backlog_estimated,
            "backlog_by_room": by_room,
            "asr_timeouts": self._asr_timeouts,
            "silent_skipped": self._silent_skipped,
            "asr_empty": self._asr_empty,
            "asr_rtf_last": None if self._last_rtf is None else _num(self._last_rtf),
            "asr_rtf_p50": rtf_block["p50"],
            "asr_rtf_p95": rtf_block["p95"],
            "asr_ms_p50": window["asr_ms"]["p50"],
            "asr_ms_p95": window["asr_ms"]["p95"],
            "asr_wait_ms_p50": window["asr_wait_ms"]["p50"],
            "asr_wait_ms_p95": window["asr_wait_ms"]["p95"],
            "decode_ms_p50": window["decode_ms"]["p50"],
            "decode_ms_p95": window["decode_ms"]["p95"],
            "asr_samples": window["count"],
            "rtf": {
                "window": window,
                "session": chosen_summary,
                "sessions": sessions,
            },
        }
