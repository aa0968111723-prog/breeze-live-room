from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.asr import AsrResult
from app.translate import TranslateResult, Translator


@dataclass
class Segment:
    room_id: str
    session_id: str
    seq: int
    zh: str = ""
    en: str = ""
    status: str = "queued"
    translate_status: str = ""
    error: str = ""
    version: int = 1
    received_at: float = field(default_factory=time.time)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.room_id, self.session_id, self.seq)

    @property
    def id(self) -> str:
        return f"{self.room_id}:{self.session_id}:{self.seq}"

    def public(self) -> dict:
        return {
            "type": "final" if self.status == "ready" else "update",
            "id": self.id,
            "room_id": self.room_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "version": self.version,
            "zh": self.zh,
            "en": self.en,
            "status": self.status,
            "translate_status": self.translate_status,
            "error": self.error,
        }


class Pipeline:
    def __init__(self, asr, translator: Translator, prompt: str, tmp: Path):
        self.asr = asr
        self.translator = translator
        self.prompt = prompt
        self.tmp = tmp
        self.tmp.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self.results: dict[tuple[str, str, int], Segment] = {}
        self.events: list[dict] = []
        self.broadcasts = self.events
        self._held: dict[tuple[str, str], dict[int, Segment]] = {}
        self._next: dict[tuple[str, str], int] = {}
        self._emitted: set[tuple[str, str, int]] = set()
        self.inflight = 0

    def get(self, room_id: str, session_id: str, seq: int) -> Segment | None:
        return self.results.get((room_id, session_id, seq))

    def try_admit(self, limit: int) -> bool:
        if self.inflight >= limit:
            return False
        self.inflight += 1
        return True

    def release_admit(self) -> None:
        self.inflight = max(0, self.inflight - 1)

    async def submit(self, segment: Segment, audio: bytes, decoder) -> Segment:
        existing = self.results.get(segment.key)
        if existing and existing.status in {"ready", "translate_failed", "zh_ready", "error", "missing"}:
            return existing
        work = self.tmp / uuid.uuid4().hex
        work.mkdir(parents=True, exist_ok=True)
        try:
            src = work / "in.bin"
            src.write_bytes(audio)
            try:
                wav = await asyncio.to_thread(decoder, src, work)
            except Exception as exc:
                segment.status = "error"
                segment.error = str(exc)[:180] or "解碼失敗"
                self.results[segment.key] = segment
                self._release(segment)
                return segment
            async with self._lock:
                asr: AsrResult = await asyncio.to_thread(self.asr.transcribe, wav, self.prompt)
            if not asr.ok or not (asr.text or "").strip():
                segment.status = "error"
                segment.error = asr.error or "這段沒聽到話"
                self.results[segment.key] = segment
                self._release(segment)
                return segment
            segment.zh = asr.text.strip()
            segment.status = "zh_ready"
            segment.version = 1
            self.results[segment.key] = segment
            self._release(segment)
            try:
                translated: TranslateResult = await asyncio.to_thread(self.translator.translate, segment.zh)
                segment.en = translated.text
                segment.translate_status = translated.status
                segment.error = translated.detail
                segment.status = "ready" if translated.status in {"ok", "off", "no_key"} else "translate_failed"
            except Exception:
                segment.en = ""
                segment.translate_status = "error"
                segment.error = "英譯失敗，中文仍保留"
                segment.status = "translate_failed"
            segment.version = 2
            self.results[segment.key] = segment
            self._update(segment)
            return segment
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def mark_missing(self, room_id: str, session_id: str, seq: int, reason: str) -> Segment:
        segment = Segment(room_id=room_id, session_id=session_id, seq=seq, status="missing", error=reason)
        self.results[segment.key] = segment
        self._release(segment)
        return segment

    def _release(self, segment: Segment) -> None:
        group = (segment.room_id, segment.session_id)
        held = self._held.setdefault(group, {})
        held[segment.seq] = segment
        nxt = self._next.get(group, 1)
        while nxt in held:
            item = held.pop(nxt)
            self.events.append(item.public())
            self._emitted.add((group[0], group[1], nxt))
            nxt += 1
            self._next[group] = nxt

    def _update(self, segment: Segment) -> None:
        if (segment.room_id, segment.session_id, segment.seq) in self._emitted:
            self.events.append(segment.public())
            return
        self._held.setdefault((segment.room_id, segment.session_id), {})[segment.seq] = segment
