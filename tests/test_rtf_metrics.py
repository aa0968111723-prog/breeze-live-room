"""Recognition RTF on /api/metrics: real slice duration, bounded memory, same host auth."""
from __future__ import annotations

import asyncio
import struct
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.audio import riff_duration_seconds
from app.rtf import SESSION_LIMIT, RtfMeter, percentile
from app.server import create_app
from app.settings import Settings
from app.translate import Translator

EXISTING = {
    "pending",
    "inflight",
    "oldest_wait_ms",
    "last_process_ms",
    "rejected",
    "missing",
    "held",
    "results",
    "translate_queued",
    "translate_skipped",
    "listeners",
    "rooms",
    "rss_bytes",
    "tokens_used",
    "price",
    "store_errors",
    "storage_recovered",
}


def wave_bytes(seconds: float = 1.0, extra: bytes = b"") -> bytes:
    """16 kHz mono PCM. extra is a LIST chunk so a size guess is not the real duration."""
    rate = 16000
    frames = int(round(seconds * rate))
    pcm = b"\x00\x00" * frames
    fmt = b"fmt " + struct.pack("<I", 16) + struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    chunks = fmt
    if extra:
        pad = b"\x00" if len(extra) % 2 else b""
        chunks += b"LIST" + struct.pack("<I", len(extra)) + extra + pad
    chunks += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def copy_decoder(src: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    wav.write_bytes(src.read_bytes())
    return wav


class TimedAsr:
    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        time.sleep(self.delay)
        self.calls += 1
        return AsrResult(ok=True, text="中文")


class GateAsr:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        self.started.set()
        assert self.release.wait(3), "RTF gate was not released"
        return AsrResult(ok=True, text="中文")


def app_for(asr, **over):
    fields = dict(allow_testclient=True, max_audio_bytes=2_000_000)
    fields.update(over)
    return create_app(
        Settings(**fields),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=copy_decoder,
    )


def auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1"}


async def token_of(app, client) -> str:
    resp = await client.get("/api/host-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def push(client, token, seq, payload, session="s"):
    return await client.post(
        "/api/push",
        params={"room_id": "class", "session_id": session, "seq": str(seq)},
        data={
            "room_id": "class",
            "session_id": session,
            "seq": str(seq),
            "wait_translation": "0",
        },
        files={"audio": ("a.wav", payload, "audio/wav")},
        headers={**auth(token), "x-breeze-async-translation": "1"},
    )


def test_recognition_defaults_stay_put():
    fresh = Settings()
    from_env = Settings.from_env({})
    assert fresh.asr_mode == "cli" and from_env.asr_mode == "cli"
    assert fresh.silence_rms == 0.0 and from_env.silence_rms == 0.0
    assert fresh.asr_threads == 6 and from_env.asr_threads == 6
    assert fresh.asr_workers == 1 and from_env.asr_workers == 1
    assert fresh.model_path == "" and from_env.model_path == ""


def test_rtf_meter_percentiles_window_and_session_cap():
    assert SESSION_LIMIT >= 1000
    meter = RtfMeter()
    meter.record_ms(50, 0, ("room", "class"))
    assert meter.snapshot()["rtf"]["session"]["count"] == 0
    meter.note_waiting(("room", "class", 1), 1.25)
    meter.note_waiting(("room", "class", 2), 2.5)
    assert meter.snapshot()["backlog_audio_s"] == 3.75
    meter.clear_waiting(("room", "class", 1))
    assert meter.snapshot()["backlog_audio_s"] == 2.5
    meter.clear_waiting(("room", "class", 2))
    assert meter.snapshot()["backlog_audio_s"] == 0

    for value in (100, 200, 300, 400, 500):
        meter.record_ms(value, 1000, ("room", "class"))
    session = meter.snapshot()["rtf"]["session"]
    assert session["count"] == 5
    assert session["limit"] == SESSION_LIMIT
    assert session["asr_ms"] == {"p50": 300, "p95": 480, "max": 500}
    assert session["audio_ms"] == {"p50": 1000, "p95": 1000, "max": 1000}
    assert session["rtf"]["p50"] == pytest.approx(0.3)
    assert session["rtf"]["p95"] == pytest.approx(0.48)
    assert session["rtf"]["max"] == pytest.approx(0.5)
    assert session["asr_ms"]["p50"] == pytest.approx(percentile([100, 200, 300, 400, 500], 0.50))
    assert session["asr_ms"]["p95"] == pytest.approx(percentile([100, 200, 300, 400, 500], 0.95))

    meter.record_ms(900, 1000, ("room", "next"))
    reset = meter.snapshot()["rtf"]
    assert reset["session"]["count"] == 1
    assert reset["session"]["asr_ms"]["max"] == 900
    assert reset["window"]["count"] == 6
    assert reset["window"]["limit"] == 200

    full = RtfMeter()
    for index in range(1, 1001):
        full.record_ms(index, 6000, ("room", "class"))
    held = full.snapshot()["rtf"]
    assert held["session"]["count"] == 1000
    assert held["window"]["count"] == 200
    assert held["window"]["asr_ms"]["max"] == 1000
    # Last 200 of 1..1000 are 801..1000.
    assert held["window"]["asr_ms"]["p50"] == pytest.approx(percentile(list(range(801, 1001)), 0.50))

    capped = RtfMeter()
    for index in range(1, SESSION_LIMIT + 6):
        capped.record_ms(index, 1000, ("room", "long"))
    top = capped.snapshot()["rtf"]
    assert top["session"]["count"] == SESSION_LIMIT
    assert top["session"]["asr_ms"]["max"] == SESSION_LIMIT + 5
    assert top["window"]["count"] == 200
    assert top["window"]["asr_ms"]["max"] == SESSION_LIMIT + 5


def test_host_metrics_line_keeps_existing_labels_and_adds_rtf():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    line = text.split("metrics.textContent = ", 1)[1].split(";", 1)[0]
    for label in ("待處理 ", "最久等待 ", "上次辨識 ", "拒絕 ", "缺段 ", "聽眾 ", "記憶體 "):
        assert label in line
    assert "辨識即時率 p50 " in line and "p95 " in line
    assert line.index("上次辨識 ") < line.index("辨識即時率 p50 ")
    assert 'data.last_process_ms ?? "—"' in line


@pytest.mark.anyio
async def test_metrics_rtf_uses_wave_duration_and_known_asr_speed(tmp_path):
    blob = wave_bytes(1.0, extra=b"INFO" + b"\x00" * 80)
    wav = tmp_path / "slice.wav"
    wav.write_bytes(blob)
    real = riff_duration_seconds(wav)
    assert real == pytest.approx(1.0)
    naive_ms = int(round(max(0, len(blob) - 44) / 32000 * 1000))
    assert naive_ms != 1000

    asr = TimedAsr(0.2)
    app = app_for(asr)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            before = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(before)
            assert before["last_process_ms"] is None
            assert before["pending"] == 0 and before["rejected"] == 0 and before["missing"] == 0
            assert before["rtf"]["window"]["count"] == 0
            assert before["rtf"]["session"]["count"] == 0
            assert before["backlog_audio_s"] == 0

            resp = await push(client, token, 1, blob)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "中文"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["pending"] == 0
            assert body["rejected"] == 0
            assert body["missing"] == 0
            assert body["backlog_audio_s"] == 0
            assert isinstance(body["last_process_ms"], int)
            session = body["rtf"]["session"]
            window = body["rtf"]["window"]
            assert session["count"] == 1 and window["count"] == 1
            assert window["limit"] == 200
            assert session["audio_ms"] == {"p50": 1000, "p95": 1000, "max": 1000}
            assert session["audio_ms"]["p50"] != naive_ms
            asr_ms = session["asr_ms"]["p50"]
            assert session["asr_ms"]["p95"] == asr_ms
            assert session["asr_ms"]["max"] == asr_ms
            assert 100 <= asr_ms <= 5000
            assert session["rtf"]["p50"] == pytest.approx(asr_ms / 1000)
            assert session["rtf"]["p95"] == pytest.approx(asr_ms / 1000)
            assert session["rtf"]["max"] == pytest.approx(asr_ms / 1000)
            assert window["rtf"]["p95"] == session["rtf"]["p95"]
            # last_process_ms is still the whole slice, not replaced by the RTF sample.
            assert body["last_process_ms"] + 50 >= asr_ms
            assert asr.calls == 1
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_metrics_backlog_is_decoded_audio_still_waiting():
    asr = GateAsr()
    app = app_for(asr, asr_workers=1)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(asr.started.wait, 2)
            second = asyncio.create_task(push(client, token, 2, blob))
            seen = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                seen = (await client.get("/api/metrics", headers=auth(token))).json()
                if seen["backlog_audio_s"] >= 1.99 and seen["pending"] == 2:
                    break
                await asyncio.sleep(0.02)
            assert seen is not None
            assert seen["backlog_audio_s"] == pytest.approx(2.0, abs=0.001)
            assert seen["pending"] == 2
            assert seen["rejected"] == 0
            assert seen["missing"] == 0
            assert isinstance(seen["oldest_wait_ms"], int) and seen["oldest_wait_ms"] >= 0
            asr.release.set()
            done = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in done] == [200, 200]
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_audio_s"] == 0
            assert after["pending"] == 0
            assert after["rtf"]["session"]["count"] == 2
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
            assert asr.calls == 2
    finally:
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_metrics_window_is_bounded_and_non_host_is_rejected():
    app = app_for(TimedAsr(0))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            meter = app.state.pipeline._rtf
            for index in range(1, 251):
                meter.record_ms(index, 1000, ("class", "s"))
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["rtf"]["window"]["count"] == 200
            assert body["rtf"]["window"]["limit"] == 200
            assert body["rtf"]["window"]["asr_ms"]["max"] == 250
            assert body["rtf"]["session"]["count"] == 250
            assert body["rtf"]["session"]["limit"] >= 1000
            assert body["last_process_ms"] is None
            assert body["pending"] == 0 and body["rejected"] == 0 and body["missing"] == 0

            missing = await client.get("/api/metrics")
            assert missing.status_code == 401
            bad = await client.get("/api/metrics", headers={"authorization": "Bearer nope"})
            assert bad.status_code == 401
            evil = await client.get(
                "/api/metrics",
                headers={**auth(token), "origin": "http://evil.example"},
            )
            assert evil.status_code == 403
            assert token not in missing.text and token not in bad.text and token not in evil.text
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_new_session_resets_rtf_and_non_wave_is_not_guessed():
    asr = TimedAsr(0)
    app = app_for(asr)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            blob = wave_bytes(1.0)
            first = await push(client, token, 1, blob, session="one")
            second = await push(client, token, 1, blob, session="two")
            assert first.status_code == 200 and second.status_code == 200
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["rtf"]["session"]["count"] == 1
            assert body["rtf"]["window"]["count"] == 2

            noise = await push(client, token, 2, b"x" * 200, session="two")
            assert noise.status_code == 200, noise.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            # 200 raw bytes are not a WAVE file. A size guess would be a few milliseconds.
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 2
            assert asr.calls == 3
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_silence_skip_does_not_record_rtf():
    asr = TimedAsr(0)
    app = app_for(asr, silence_rms=1_000_000)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(client, token, 1, wave_bytes(1.0))
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "silent"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["rtf"]["session"]["count"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["backlog_audio_s"] == 0
            assert body["last_process_ms"] is None
            assert asr.calls == 0
    finally:
        await app.state.shutdown()
