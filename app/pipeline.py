from __future__ import annotations

import asyncio
import hashlib
import inspect
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.asr import AsrResult
from app.audio import AudioError, wav_duration_seconds, wav_rms
from app.settings import Settings
from app.textutil import annotate_question
from app.translate import TranslateResult, Translator

FAILURES = {"error", "missing", "timeout", "cancelled"}
TERMINAL = FAILURES | {"ready", "translate_failed", "silent", "zh_ready"}


class PipelineError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Segment:
    room_id: str
    session_id: str
    seq: int
    zh: str = ""
    zh_raw: str = ""
    en: str = ""
    status: str = "queued"
    translate_status: str = ""
    error: str = ""
    version: int = 1
    session_ord: int = 0
    t0_ms: int | None = None
    t1_ms: int | None = None
    received_at: float = field(default_factory=time.time)
    cursor: int = 0
    translate_queued: bool = False
    room_gen: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.room_id, self.session_id, self.seq)

    @property
    def id(self) -> str:
        return f"{self.room_id}:{self.session_id}:{self.seq}"

    def public(self) -> dict:
        return {
            "type": "final" if self.status in {"ready", "silent"} else "update",
            "id": self.id,
            "room_id": self.room_id,
            "session_id": self.session_id,
            "session_ord": self.session_ord,
            "seq": self.seq,
            "version": self.version,
            "zh": self.zh,
            "zh_raw": self.zh_raw,
            "en": self.en,
            "status": self.status,
            "translate_status": self.translate_status,
            "error": self.error,
            "cursor": self.cursor,
            "t0_ms": self.t0_ms,
            "t1_ms": self.t1_ms,
        }


class Pipeline:
    def __init__(self, asr, translator: Translator, prompt: str, tmp: Path, settings: Settings | None = None, on_event=None):
        self.asr = asr
        self.translator = translator
        self.prompt = prompt
        self.tmp = tmp
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings()
        self.on_event = on_event
        self._asr_slots = asyncio.Semaphore(self.settings.asr_workers)
        self.results: dict[tuple[str, str, int], Segment] = {}
        self._hashes: dict[tuple[str, str, int], str] = {}
        self._waiters: dict[tuple[str, str, int], list[asyncio.Future]] = {}
        self.events: list[dict] = []
        self.broadcasts = self.events
        self._held: dict[tuple[str, str], dict[int, Segment]] = {}
        self._next: dict[tuple[str, str], int] = {}
        self._max_seq: dict[tuple[str, str], int] = {}
        self._gap_since: dict[tuple[str, str, int], float] = {}
        self._active: set[tuple[str, str, int]] = set()
        self._closed: set[tuple[str, str]] = set()
        self._session_ord: dict[tuple[str, str], int] = {}
        self._room_sessions: dict[str, int] = {}
        self._seen_versions: set[tuple] = set()
        self._seen_order: deque = deque()
        self._emitted_segs: set[tuple[str, str, int]] = set()
        self._emit_waiters: dict[tuple[str, str, int], list[asyncio.Future]] = {}
        self._slots = 0
        self._bytes = 0
        self._reserved: set[tuple[str, str, int]] = set()
        self._flight: dict[tuple[str, str, int], asyncio.Future] = {}
        self._cancel: set[tuple[str, str, int]] = set()
        self._room_gen: dict[str, int] = {}
        self.inflight = 0
        self.rejected = 0
        self.missing_count = 0
        self.oldest_wait_started: float | None = None
        self.last_process_s: float | None = None
        self._translate_q: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []
        self._workers = False
        self.glossary: dict[tuple[str, str], list[dict]] = {}

    def get(self, room_id: str, session_id: str, seq: int) -> Segment | None:
        return self.results.get((room_id, session_id, seq))

    def try_admit(self, limit: int | None = None) -> bool:
        return self.try_admit_count() if limit is None else self._admit_with_limit(limit)

    def _admit_with_limit(self, limit: int) -> bool:
        if self._slots >= limit:
            self.rejected += 1
            return False
        self._take_slot()
        return True

    def try_admit_count(self) -> bool:
        if self._slots >= self.settings.max_queue:
            self.rejected += 1
            return False
        if self._bytes >= self.settings.max_inflight_bytes:
            self.rejected += 1
            return False
        self._take_slot()
        return True

    def _take_slot(self) -> None:
        self._slots += 1
        self.inflight = self._slots
        if self.oldest_wait_started is None:
            self.oldest_wait_started = time.monotonic()

    def release_admit(self) -> None:
        self.release_slot()

    def release_slot(self, nbytes: int = 0) -> None:
        self._slots = max(0, self._slots - 1)
        if nbytes:
            self._bytes = max(0, self._bytes - nbytes)
        self.inflight = self._slots
        if self._slots == 0:
            self.oldest_wait_started = None

    def joinable_without_slot(self, key: tuple[str, str, int]) -> bool:
        flight = self._flight.get(key)
        if flight is not None and not flight.done():
            return True
        return key in self._active or key in self._reserved or key in self.results

    def note_reserved(self, key: tuple[str, str, int]) -> None:
        self._reserved.add(key)

    def clear_reserved(self, key: tuple[str, str, int]) -> None:
        self._reserved.discard(key)

    def stats(self) -> dict:
        oldest = 0
        if self._slots and self.oldest_wait_started is not None:
            oldest = int((time.monotonic() - self.oldest_wait_started) * 1000)
        return {
            "pending": self._slots,
            "inflight": len(self._active),
            "oldest_wait_ms": oldest,
            "last_process_ms": None if self.last_process_s is None else int(self.last_process_s * 1000),
            "rejected": self.rejected,
            "missing": self.missing_count,
            "held": sum(len(rows) for rows in self._held.values()),
            "results": len(self.results),
            "translate_queued": 0 if self._translate_q is None else self._translate_q.qsize(),
        }

    def ensure_workers(self) -> None:
        if self._workers:
            return
        self._workers = True
        self._translate_q = asyncio.Queue(maxsize=self.settings.translate_queue)
        self._tasks.append(asyncio.create_task(self._translate_loop()))
        self._tasks.append(asyncio.create_task(self._gap_loop()))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._workers = False
        for waiters in list(self._waiters.values()):
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._waiters.clear()
        for waiters in list(self._emit_waiters.values()):
            for fut in waiters:
                if not fut.done():
                    fut.cancel()
        self._emit_waiters.clear()
        for fut in list(self._flight.values()):
            if not fut.done():
                fut.cancel()
        self._flight.clear()

    def drop_room(self, room_id: str) -> None:
        """Forget one ended room. A later host open uses a new generation, so late jobs cannot refill it."""
        self._room_gen[room_id] = self._room_gen.get(room_id, 1) + 1
        keys = {key for key in self.results if key[0] == room_id}
        keys.update(key for key in self._flight if key[0] == room_id)
        keys.update(key for key in self._active if key[0] == room_id)
        keys.update(key for key in self._reserved if key[0] == room_id)
        keys.update(key for key in self._hashes if key[0] == room_id)
        keys.update(key for key in self._waiters if key[0] == room_id)
        keys.update(key for key in self._emit_waiters if key[0] == room_id)
        keys.update(key for key in self._emitted_segs if key[0] == room_id)
        keys.update(key for key in self._cancel if key[0] == room_id)
        for key in keys:
            self.results.pop(key, None)
            self._hashes.pop(key, None)
            self._active.discard(key)
            self._reserved.discard(key)
            self._emitted_segs.discard(key)
            self._cancel.discard(key)
            for fut in self._waiters.pop(key, []):
                if not fut.done():
                    fut.cancel()
            for fut in self._emit_waiters.pop(key, []):
                if not fut.done():
                    fut.cancel()
            flight = self._flight.pop(key, None)
            if flight is not None and not flight.done():
                flight.cancel()
        groups = {group for group in self._held if group[0] == room_id}
        groups.update(group for group in self._next if group[0] == room_id)
        groups.update(group for group in self._max_seq if group[0] == room_id)
        groups.update(group for group in self._closed if group[0] == room_id)
        groups.update(group for group in self._session_ord if group[0] == room_id)
        groups.update(group for group in self.glossary if group[0] == room_id)
        for group in groups:
            self._held.pop(group, None)
            self._next.pop(group, None)
            self._max_seq.pop(group, None)
            self._closed.discard(group)
            self._session_ord.pop(group, None)
            self.glossary.pop(group, None)
        self._room_sessions.pop(room_id, None)
        for stamp in [stamp for stamp in self._gap_since if stamp[0] == room_id]:
            self._gap_since.pop(stamp, None)

    def _ord(self, room_id: str, session_id: str) -> int:
        key = (room_id, session_id)
        if key not in self._session_ord:
            count = self._room_sessions.get(room_id, 0) + 1
            self._room_sessions[room_id] = count
            self._session_ord[key] = count
        return self._session_ord[key]

    def _stamp_gen(self, segment: Segment) -> None:
        if not segment.room_gen:
            segment.room_gen = self._room_gen.setdefault(segment.room_id, 1)

    def _stale(self, segment: Segment) -> bool:
        # Generation is stamped once. drop_room bumps it; a late job must not adopt the new one.
        if not segment.room_gen:
            return False
        return segment.room_gen != self._room_gen.get(segment.room_id)

    def _note(self, segment: Segment) -> None:
        group = (segment.room_id, segment.session_id)
        self._stamp_gen(segment)
        if self._stale(segment):
            return
        segment.session_ord = self._ord(segment.room_id, segment.session_id)
        self._max_seq[group] = max(self._max_seq.get(group, 0), segment.seq)

    def _mark_emitted(self, segment: Segment) -> None:
        self._emitted_segs.add(segment.key)
        for fut in self._emit_waiters.pop(segment.key, []):
            if not fut.done():
                fut.set_result(True)

    async def _wait_emitted(self, segment: Segment) -> None:
        if segment.key in self._emitted_segs:
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._emit_waiters.setdefault(segment.key, []).append(fut)
        await fut

    def _emit(self, segment: Segment) -> None:
        if self._stale(segment):
            # Do not mark emitted: that would unblock zh_ready into a translation wait
            # for a caption that will never be queued.
            return
        marker = (*segment.key, segment.version)
        if marker in self._seen_versions:
            self._mark_emitted(segment)
            return
        self._seen_versions.add(marker)
        self._seen_order.append(marker)
        while len(self._seen_order) > self.settings.max_results * 4:
            self._seen_versions.discard(self._seen_order.popleft())
        event = {
            "type": "caption",
            "id": segment.id,
            "room_id": segment.room_id,
            "session_id": segment.session_id,
            "session_ord": segment.session_ord,
            "seq": segment.seq,
            "version": segment.version,
            "zh": segment.zh,
            "zh_raw": segment.zh_raw,
            "en": segment.en,
            "status": segment.status,
            "translate_status": segment.translate_status,
            "error": segment.error,
            "t0_ms": segment.t0_ms,
            "t1_ms": segment.t1_ms,
        }
        self.events.append(dict(event))
        if len(self.events) > self.settings.history_limit * 2:
            del self.events[: len(self.events) - self.settings.history_limit * 2]
        self._mark_emitted(segment)
        if segment.status == "zh_ready" and not segment.translate_queued:
            self._queue_translate(segment)
        if self.on_event:
            delivered = self.on_event(dict(event))
            if isinstance(delivered, dict) and delivered.get("cursor"):
                segment.cursor = int(delivered["cursor"])
                event["cursor"] = segment.cursor

    def _release(self, segment: Segment) -> None:
        self._note(segment)
        if self._stale(segment):
            self._wake(segment.key, segment)
            return
        group = (segment.room_id, segment.session_id)
        self.results[segment.key] = segment
        nxt = self._next.get(group, 1)
        if segment.seq < nxt:
            # Already ordered. Retry must broadcast this version now; parking it in
            # _held would never drain, and zh_ready would wait for English forever.
            held = self._held.get(group)
            if held is not None:
                held.pop(segment.seq, None)
            self._emit(segment)
            return
        held = self._held.setdefault(group, {})
        held[segment.seq] = segment
        self._drain(group)
        self._force_held_cap(group)

    def _drain(self, group: tuple[str, str]) -> None:
        held = self._held.setdefault(group, {})
        nxt = self._next.get(group, 1)
        while nxt in held:
            item = held.pop(nxt)
            self._emit(item)
            nxt += 1
            self._next[group] = nxt
            self._gap_since.pop((*group, nxt), None)

    def _force_held_cap(self, group: tuple[str, str]) -> None:
        held = self._held.get(group, {})
        guard = 0
        while len(held) > self.settings.max_held and guard < self.settings.max_held + 2:
            guard += 1
            nxt = self._next.get(group, 1)
            if nxt in held:
                self._drain(group)
                continue
            self.mark_missing(group[0], group[1], nxt, "缺段堆積已達上限，先跳過這個序號")
            held = self._held.get(group, {})

    def mark_missing(self, room_id: str, session_id: str, seq: int, reason: str) -> Segment:
        key = (room_id, session_id, seq)
        existing = self.results.get(key)
        if key in self._active:
            return existing if existing is not None else Segment(
                room_id=room_id, session_id=session_id, seq=seq, status="transcribing"
            )
        if existing and existing.status not in {"queued"} and key not in self._held.get((room_id, session_id), {}):
            self._drain((room_id, session_id))
            return existing
        if existing and existing.status not in {"queued", "decoding", "transcribing"} and key not in self._active:
            if existing.seq in self._held.get((room_id, session_id), {}):
                self._drain((room_id, session_id))
            return existing
        segment = Segment(
            room_id=room_id,
            session_id=session_id,
            seq=seq,
            status="missing",
            error=reason,
            version=1,
        )
        self.results[key] = segment
        self.missing_count += 1
        self._active.discard(key)
        self._release(segment)
        self._wake(key, segment)
        return segment

    def end_session(self, room_id: str, session_id: str) -> None:
        group = (room_id, session_id)
        self._closed.add(group)
        max_seq = self._max_seq.get(group, 0)
        seq = self._next.get(group, 1)
        while seq <= max_seq:
            key = (room_id, session_id, seq)
            if key in self._active:
                seq += 1
                continue
            current = self.results.get(key)
            if current is None or current.status == "queued":
                self.mark_missing(room_id, session_id, seq, "會話結束，這段沒有收到")
            seq += 1
        self._drain(group)

    def fail_received(self, segment: Segment, detail: str, status: str = "error") -> Segment:
        segment.status = status
        segment.error = detail[:180]
        segment.version = max(segment.version, 1)
        self._active.discard(segment.key)
        self._release(segment)
        self._wake(segment.key, segment)
        return segment

    def request_cancel(self, room_id: str, session_id: str, seq: int) -> Segment:
        key = (room_id, session_id, seq)
        current = self.results.get(key)
        if current and current.status in {"ready", "silent", "translate_failed", "zh_ready"} and key not in self._active:
            raise PipelineError(409, "這段已經送出")
        self._cancel.add(key)
        if key in self._active:
            return current or Segment(room_id=room_id, session_id=session_id, seq=seq, status="transcribing")
        return self.fail_received(
            Segment(room_id=room_id, session_id=session_id, seq=seq),
            "主持端取消這段",
            status="cancelled",
        )

    async def submit(self, segment: Segment, audio: bytes, decoder, *, slot_held: bool, retry: bool = False, owner: bool = False) -> Segment:
        del owner  # Admission is synchronous; the flight future replaces the old owner spin.
        self.ensure_workers()
        if (segment.room_id, segment.session_id) in self._closed:
            if slot_held:
                self.release_slot()
            raise PipelineError(409, "這個會話已結束")
        digest = hashlib.sha256(audio).hexdigest()
        # No await before the flight is registered, so two tasks cannot both become the owner.
        inflight = self._flight.get(segment.key)
        if inflight is not None and not inflight.done():
            stored = self._hashes.get(segment.key)
            if stored and stored != digest:
                if slot_held:
                    self.release_slot()
                raise PipelineError(409, "同一段的內容不同，已拒絕替換")
            if slot_held:
                self.release_slot()
            return await inflight
        existing = self.results.get(segment.key)
        stored = self._hashes.get(segment.key)
        if existing and stored == digest:
            reprocess = retry and existing.status in FAILURES and segment.key not in self._active
            if not reprocess:
                if slot_held:
                    self.release_slot()
                return await self._wait_result(existing)
            segment.version = max(existing.version + 1, 1)
        elif existing and stored != digest:
            if not (retry and existing.status in FAILURES and segment.key not in self._active):
                if slot_held:
                    self.release_slot()
                raise PipelineError(409, "同一段的內容不同，已拒絕替換")
            segment.version = max(existing.version + 1, 1)
        if not slot_held:
            if not self.try_admit_count():
                raise PipelineError(429, "辨識佇列已滿，請稍後再送")
            slot_held = True
        if self._bytes + len(audio) > self.settings.max_inflight_bytes:
            self.release_slot()
            self.rejected += 1
            raise PipelineError(429, "在途音訊太多，請稍後再送")
        self._bytes += len(audio)
        self._hashes[segment.key] = digest
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._flight[segment.key] = fut
        holder = {"held": slot_held, "bytes": len(audio)}
        try:
            result = await self._process(segment, audio, decoder, holder)
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:
            if holder["held"]:
                self.release_slot(holder["bytes"])
                holder["held"] = False
            if not fut.done():
                if isinstance(exc, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(exc)
                    fut.exception()
            raise
        finally:
            if self._flight.get(segment.key) is fut:
                self._flight.pop(segment.key, None)

    def _future(self, segment: Segment) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if segment.room_gen:
            setattr(fut, "room_gen", segment.room_gen)
        return fut

    def _result_ready(self, segment: Segment) -> bool:
        if self._stale(segment):
            return True
        if segment.status in TERMINAL and segment.status != "zh_ready":
            return True
        # Ordered Chinese with no translation queued will never wake a waiter.
        return segment.status == "zh_ready" and segment.key in self._emitted_segs and not segment.translate_queued

    async def _wait_result(self, segment: Segment) -> Segment:
        if self._result_ready(segment):
            return segment
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        return await fut

    def _discard_waiter(self, key: tuple[str, str, int], fut: asyncio.Future) -> None:
        waiters = self._waiters.get(key)
        if not waiters:
            return
        remaining = [item for item in waiters if item is not fut]
        if remaining:
            self._waiters[key] = remaining
        else:
            self._waiters.pop(key, None)

    def _wake(self, key: tuple[str, str, int], segment: Segment) -> None:
        gen = segment.room_gen
        matched: list[asyncio.Future] = []
        keep: list[asyncio.Future] = []
        for fut in self._waiters.pop(key, []):
            fut_gen = getattr(fut, "room_gen", None)
            if fut_gen is not None and gen and fut_gen != gen:
                keep.append(fut)
            else:
                matched.append(fut)
        if keep:
            self._waiters[key] = keep
        for fut in matched:
            if not fut.done():
                fut.set_result(segment)

    def _release_holder(self, holder: dict) -> None:
        if holder["held"]:
            self.release_slot(holder["bytes"])
            holder["held"] = False

    async def _process(self, segment: Segment, audio: bytes, decoder, holder: dict) -> Segment:
        self._note(segment)
        if not self._stale(segment):
            self.results[segment.key] = segment
            self._active.add(segment.key)
        self._reserved.discard(segment.key)
        work = self.tmp / uuid.uuid4().hex
        started = time.monotonic()
        try:
            segment.status = "decoding"
            try:
                wav = await asyncio.wait_for(
                    asyncio.to_thread(self._decode_sync, work, audio, decoder),
                    timeout=self.settings.decode_timeout_s,
                )
            except asyncio.TimeoutError:
                self.fail_received(segment, "轉檔逾時", status="timeout")
                return segment
            except AudioError as exc:
                self.fail_received(segment, exc.detail, status="error")
                raise
            except Exception as exc:
                self.fail_received(segment, str(exc)[:180] or "解碼失敗", status="error")
                return segment
            if segment.key in self._cancel:
                self.fail_received(segment, "主持端取消這段", status="cancelled")
                return segment
            seconds = wav_duration_seconds(wav)
            if seconds is not None and seconds > self.settings.max_audio_seconds:
                self.fail_received(segment, f"音訊長於 {self.settings.max_audio_seconds} 秒，已拒絕")
                raise AudioError(413, segment.error)
            rms = wav_rms(wav)
            if (
                self.settings.silence_rms > 0
                and rms is not None
                and rms < self.settings.silence_rms
                and (seconds or 0) >= 0.3
            ):
                segment.status = "silent"
                segment.error = "這段太安靜，沒有送去辨識"
                segment.zh = ""
                self._release(segment)
                return segment
            segment.status = "transcribing"
            try:
                async with self._asr_slots:
                    asr: AsrResult = await asyncio.wait_for(
                        asyncio.to_thread(self.asr.transcribe, wav, self.prompt),
                        timeout=self.settings.asr_timeout_s,
                    )
            except asyncio.TimeoutError:
                self.fail_received(segment, "辨識逾時", status="timeout")
                return segment
            except Exception as exc:
                self.fail_received(segment, str(exc)[:180] or "辨識失敗", status="error")
                return segment
            self.last_process_s = time.monotonic() - started
            if segment.key in self._cancel:
                self.fail_received(segment, "主持端取消這段", status="cancelled")
                return segment
            text = (asr.text or "").strip()
            if not asr.ok or not text:
                if asr.ok or not asr.error:
                    segment.status = "silent"
                    segment.error = asr.error or "這段沒聽到話"
                    segment.zh_raw = text
                    segment.zh = ""
                    self._release(segment)
                    return segment
                segment.status = "error"
                segment.error = asr.error or "辨識失敗"
                segment.zh_raw = text
                segment.zh = ""
                self._release(segment)
                return segment
            segment.zh_raw = text
            segment.zh = annotate_question(text)
            segment.status = "zh_ready"
            # Retry sets version to max(existing + 1, 1). Keep it; do not hard-reset to 1.
            segment.version = max(segment.version, 1)
            segment.error = ""
            self._release(segment)
            if self._stale(segment):
                return segment
        finally:
            self._active.discard(segment.key)
            self._release_holder(holder)
            shutil.rmtree(work, ignore_errors=True)
            if segment.status not in TERMINAL:
                if self._stale(segment):
                    self._wake(segment.key, segment)
                else:
                    task = asyncio.current_task()
                    cancelling = task is not None and task.cancelling()
                    self.fail_received(
                        segment,
                        "主持端取消這段" if cancelling else "辨識中斷",
                        status="cancelled" if cancelling else "error",
                    )
            elif segment.status != "zh_ready":
                self._wake(segment.key, segment)
            self._trim_results()
        if segment.status != "zh_ready" or self._stale(segment):
            return segment
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        try:
            if segment.key not in self._emitted_segs:
                await self._wait_emitted(segment)
            if self._stale(segment) or not segment.translate_queued:
                self._discard_waiter(segment.key, fut)
                return segment
            if not fut.done():
                await fut
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if self._stale(segment):
                return segment
            raise
        if self._stale(segment):
            return segment
        return self.results.get(segment.key, segment)

    def _queue_translate(self, segment: Segment) -> None:
        if self._stale(segment):
            return
        self.ensure_workers()
        assert self._translate_q is not None
        segment.translate_queued = True
        try:
            self._translate_q.put_nowait(segment)
        except asyncio.QueueFull:
            if self._stale(segment):
                self._wake(segment.key, segment)
                return
            segment.translate_status = "queue_full"
            segment.error = "英譯佇列已滿，中文仍保留"
            segment.status = "translate_failed"
            segment.version += 1
            self.results[segment.key] = segment
            self._emit(segment)
            self._wake(segment.key, segment)

    def _decode_sync(self, work: Path, audio: bytes, decoder):
        work.mkdir(parents=True, exist_ok=True)
        src = work / "in.bin"
        src.write_bytes(audio)
        return decoder(src, work)

    async def _enqueue_translation(self, segment: Segment) -> None:
        if self._stale(segment):
            return
        if segment.key not in self._emitted_segs:
            await self._wait_emitted(segment)
        if self._stale(segment) or segment.key not in self._emitted_segs:
            return
        fut = self._future(segment)
        self._waiters.setdefault(segment.key, []).append(fut)
        assert self._translate_q is not None
        try:
            self._translate_q.put_nowait(segment)
        except asyncio.QueueFull:
            if not self._stale(segment):
                segment.translate_status = "queue_full"
                segment.error = "英譯佇列已滿，中文仍保留"
                segment.status = "translate_failed"
                segment.version = segment.version + 1
                self.results[segment.key] = segment
                self._emit(segment)
            self._wake(segment.key, segment)
            return
        await fut

    async def _translate_loop(self) -> None:
        assert self._translate_q is not None
        while True:
            segment = await self._translate_q.get()
            try:
                await self._apply_translation(segment)
            except Exception:
                if not self._stale(segment) and segment.key in self._emitted_segs:
                    segment.en = ""
                    segment.translate_status = "error"
                    segment.error = "英譯失敗，中文仍保留"
                    segment.status = "translate_failed"
                    segment.version = segment.version + 1
                    self.results[segment.key] = segment
                    self._emit(segment)
            finally:
                self._wake(segment.key, segment)
                self._translate_q.task_done()

    async def _apply_translation(self, segment: Segment) -> None:
        if self._stale(segment) or segment.key not in self._emitted_segs:
            return
        glossary = self.glossary.get((segment.room_id, segment.session_id), [])
        context = self._context(segment)
        try:
            kwargs = {}
            params = inspect.signature(self.translator.translate).parameters
            if "glossary" in params:
                kwargs["glossary"] = glossary
            if "context" in params:
                kwargs["context"] = context
            translated: TranslateResult = await asyncio.wait_for(
                asyncio.to_thread(self.translator.translate, segment.zh, **kwargs),
                timeout=self.settings.translate_timeout_s,
            )
            if self._stale(segment):
                return
            segment.en = translated.text or ""
            segment.translate_status = translated.status
            segment.error = translated.detail
            segment.status = "ready" if translated.status in {"ok", "off", "no_key"} else "translate_failed"
        except asyncio.TimeoutError:
            if self._stale(segment):
                return
            segment.en = ""
            segment.translate_status = "timeout"
            segment.error = "英譯逾時，不假設沒有計費。中文仍保留"
            segment.status = "translate_failed"
        except Exception:
            if self._stale(segment):
                return
            segment.en = ""
            segment.translate_status = "error"
            segment.error = "英譯失敗，中文仍保留"
            segment.status = "translate_failed"
        if self._stale(segment):
            return
        segment.version = segment.version + 1
        self.results[segment.key] = segment
        self._emit(segment)

    def _context(self, segment: Segment) -> list[str]:
        rows = []
        for key, item in self.results.items():
            if key[0] == segment.room_id and key[1] == segment.session_id and key[2] < segment.seq and item.zh:
                rows.append((key[2], item.zh))
        rows.sort()
        return [text for _, text in rows[-4:]]

    async def _gap_loop(self) -> None:
        tick = min(0.05, max(self.settings.gap_wait_s, 0.01))
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            for group, max_seq in list(self._max_seq.items()):
                if group in self._closed:
                    continue
                nxt = self._next.get(group, 1)
                if nxt > max_seq or nxt in self._held.get(group, {}):
                    continue
                if (group[0], group[1], nxt) in self._active:
                    continue
                stamp = (*group, nxt)
                started = self._gap_since.get(stamp)
                if started is None:
                    self._gap_since[stamp] = now
                    continue
                if now - started >= self.settings.gap_wait_s:
                    self._gap_since.pop(stamp, None)
                    self.mark_missing(group[0], group[1], nxt, "缺段：等待上限已到，後面的字幕繼續")

    def _trim_results(self) -> None:
        if len(self.results) <= self.settings.max_results:
            return
        victims = []
        for key, segment in self.results.items():
            group = (key[0], key[1])
            if key[2] < self._next.get(group, 1) and key not in self._active and segment.status not in {"queued", "decoding", "transcribing", "zh_ready"}:
                victims.append((segment.received_at, key))
        victims.sort()
        extra = len(self.results) - self.settings.max_results
        for _, key in victims[:extra]:
            self.results.pop(key, None)
            self._hashes.pop(key, None)

    async def retranslate(self, room_id: str, session_id: str, seq: int, zh: str | None = None) -> Segment:
        self.ensure_workers()
        segment = self.results.get((room_id, session_id, seq))
        if segment is None or not (segment.zh or zh):
            raise PipelineError(404, "找不到這段字幕")
        if zh is not None:
            segment.zh_raw = segment.zh_raw or segment.zh
            segment.zh = annotate_question(zh.strip())
        segment.status = "zh_ready"
        await self._enqueue_translation(segment)
        return segment
