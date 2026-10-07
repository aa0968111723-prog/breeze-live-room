"""Recognition RTF on /api/metrics: real slice duration, bounded memory, same host auth."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.audio import riff_duration_seconds
from app.rtf import SESSION_LIMIT, SESSION_ROOM_CAP, RtfMeter, percentile
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


async def push(client, token, seq, payload, session="s", room="class"):
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data={
            "room_id": room,
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
    waiting = meter.snapshot()
    assert waiting["backlog_audio_s"] == 3.75
    assert waiting["backlog_s"] == 3.75
    assert waiting["backlog_estimated"] is False
    meter.clear_waiting(("room", "class", 1))
    assert meter.snapshot()["backlog_audio_s"] == 2.5
    assert meter.snapshot()["backlog_s"] == 2.5
    meter.clear_waiting(("room", "class", 2))
    assert meter.snapshot()["backlog_audio_s"] == 0
    assert meter.snapshot()["backlog_s"] == 0

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
    assert "辨識即時率" not in line
    assert "辨識速度" not in line
    assert 'data.last_process_ms ?? "—"' in line
    phase = text.index('id="phase"')
    speed = text.index('id="rtf-speed"')
    captions = text.index('id="caption-en"')
    assert phase < speed < captions
    assert "辨識速度：開始聽之後才有數字" in text
    assert "最近 200 段" in text
    assert "（低於 0.9 才跟得上）" in text
    assert "狀態：" in text
    for word in ("跟得上", "接近上限", "跟不上，字幕會延遲"):
        assert word in text


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
            assert before["backlog_s"] == 0
            assert before["asr_timeouts"] == 0
            assert before["silent_skipped"] == 0
            assert before["asr_empty"] == 0
            assert before["asr_rtf_p95"] is None

            resp = await push(client, token, 1, blob)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "中文"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["pending"] == 0
            assert body["rejected"] == 0
            assert body["missing"] == 0
            assert body["backlog_audio_s"] == 0
            assert body["backlog_s"] == 0
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
            assert body["asr_rtf_p50"] == window["rtf"]["p50"]
            assert body["asr_rtf_p95"] == window["rtf"]["p95"]
            assert body["asr_samples"] == 1
            assert session["decode_ms"]["p50"] == window["decode_ms"]["p50"]
            assert session["asr_wait_ms"]["p95"] == window["asr_wait_ms"]["p95"]
            assert body["asr_timeouts"] == 0
            assert body["silent_skipped"] == 0
            assert body["asr_empty"] == 0
            # last_process_ms is decode plus recognition, not the RTF numerator and not slot wait.
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
                # The slice already inside ASR is not backlog. Only the decoded slice still waiting for a slot is.
                if asr.calls == 1 and seen["pending"] == 2 and seen["backlog_audio_s"] <= 1.01:
                    break
                await asyncio.sleep(0.02)
            assert seen is not None
            assert asr.calls == 1
            assert seen["backlog_audio_s"] == pytest.approx(1.0, abs=0.001)
            assert seen["backlog_s"] == pytest.approx(1.0, abs=0.001)
            assert seen["backlog_estimated"] is False
            assert seen["backlog_by_room"] == {"class": pytest.approx(1.0, abs=0.001)}
            assert seen["pending"] == 2
            assert seen["rejected"] == 0
            assert seen["missing"] == 0
            assert isinstance(seen["oldest_wait_ms"], int) and seen["oldest_wait_ms"] >= 0
            asr.release.set()
            done = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in done] == [200, 200]
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_audio_s"] == 0
            assert after["backlog_s"] == 0
            assert after["backlog_estimated"] is False
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
            assert body["backlog_s"] == 0
            assert body["asr_timeouts"] == 0
            assert body["silent_skipped"] == 1
            assert body["asr_empty"] == 0
            assert body["last_process_ms"] is None
            assert asr.calls == 0
    finally:
        await app.state.shutdown()


def _rows(snapshot: dict) -> dict[tuple[str, str], dict]:
    return {(row["room_id"], row["session_id"]): row for row in snapshot["rtf"]["sessions"]}


def test_timeout_does_not_enter_rtf_percentiles():
    meter = RtfMeter()
    meter.record_ms(200, 1000, ("room", "class"))
    meter.note_timeout(("room", "class"))
    meter.note_timeout(("room", "class"))
    snap = meter.snapshot()
    assert snap["asr_timeouts"] == 2
    assert snap["rtf"]["session"]["count"] == 1
    assert snap["rtf"]["window"]["count"] == 1
    assert snap["rtf"]["session"]["asr_ms"]["max"] == 200
    assert snap["rtf"]["session"]["rtf"]["p95"] == pytest.approx(0.2)
    assert _rows(snap)[("room", "class")]["asr_timeouts"] == 2
    # A new session drops that room's samples only. The timeout total stays.
    meter.note_timeout(("room", "next"))
    after = meter.snapshot()
    assert after["asr_timeouts"] == 3
    assert after["rtf"]["session"]["count"] == 0
    assert after["rtf"]["session"]["rtf"]["p95"] is None
    assert after["rtf"]["window"]["count"] == 1
    meter.drop_room("room")
    dropped = meter.snapshot()
    assert dropped["asr_timeouts"] == 3
    assert dropped["rtf"]["sessions"] == []
    assert dropped["rtf"]["window"]["count"] == 1


def test_alternating_rooms_do_not_wipe_session_rtf():
    meter = RtfMeter()
    for _ in range(40):
        meter.record_ms(100, 1000, ("east", "live"))
        meter.record_ms(900, 1000, ("west", "live"))
    rows = _rows(meter.snapshot())
    assert rows[("east", "live")]["count"] == 40
    assert rows[("west", "live")]["count"] == 40
    assert rows[("east", "live")]["rtf"]["p95"] == pytest.approx(0.1)
    assert rows[("west", "live")]["rtf"]["p95"] == pytest.approx(0.9)
    assert rows[("east", "live")]["asr_ms"]["max"] == 100
    assert rows[("west", "live")]["asr_ms"]["max"] == 900
    # Default snapshot follows the latest update, which still has every east sample.
    assert meter.snapshot()["rtf"]["session"]["count"] == 40
    assert meter.snapshot(("west", "live"))["rtf"]["session"]["count"] == 40
    meter.record_ms(50, 1000, ("east", "next"))
    rows = _rows(meter.snapshot())
    assert ("east", "live") not in rows
    assert rows[("east", "next")]["count"] == 1
    assert rows[("east", "next")]["asr_ms"]["max"] == 50
    assert rows[("west", "live")]["count"] == 40
    assert rows[("west", "live")]["rtf"]["p95"] == pytest.approx(0.9)
    assert meter.snapshot()["rtf"]["window"]["count"] == 81


def test_session_buckets_stay_bounded():
    meter = RtfMeter()
    total = SESSION_ROOM_CAP + 5
    for index in range(total):
        meter.record_ms(index + 1, 1000, (f"room{index}", "s"))
    rows = meter.snapshot()["rtf"]["sessions"]
    assert len(rows) == SESSION_ROOM_CAP
    ids = {row["room_id"] for row in rows}
    assert "room0" not in ids
    assert f"room{total - 1}" in ids
    assert meter.snapshot()["rtf"]["window"]["count"] == total
    assert meter.snapshot()["rtf"]["session"]["count"] == 1


def test_stop_path_does_not_block_on_the_rtf_meter():
    text = Path("app/pipeline.py").read_text(encoding="utf-8")
    end = text.split("async def end_session", 1)[1].split("\n    def fail_received", 1)[0]
    assert "_rtf" not in end
    before = text.split('self.fail_received(segment, "辨識逾時", status="timeout")', 1)[0]
    handler = before.rsplit("except asyncio.TimeoutError:", 1)[1]
    assert "note_timeout" in handler
    assert "record_s" not in handler
    finally_body = text.split("self._rtf.clear_waiting(segment.key)", 1)[1].split("self.last_process_s", 1)[0]
    assert "snapshot" not in finally_body
    assert "sleep" not in finally_body
    assert ".record(" in finally_body
    meter = Path("app/rtf.py").read_text(encoding="utf-8")
    for banned in ("time.sleep", "asyncio.", "subprocess", "threading", "Lock(", "socket.", "open("):
        assert banned not in meter, banned


def _contrast(foreground: str, background: str) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        if scaled <= 0.04045:
            return scaled / 12.92
        return ((scaled + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        color = color.lstrip("#")
        red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
        return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)

    lighter = max(luminance(foreground), luminance(background))
    darker = min(luminance(foreground), luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def test_host_speed_line_contrast_and_status_words():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    for selector in ("#rtf-speed.speed-empty", "#rtf-speed.speed-ok", "#rtf-speed.speed-warn", "#rtf-speed.speed-bad"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", text)
        assert block, selector
        body = block.group(1)
        colors = {}
        for name in ("color", "background"):
            found = re.search(rf"(?:^|;)\s*{name}\s*:\s*(#[0-9a-fA-F]{{6}})", body)
            assert found, (selector, name, body)
            colors[name] = found.group(1)
        ratio = _contrast(colors["color"], colors["background"])
        assert ratio >= 4.5, (selector, colors, ratio)
    view = text.split("function recognitionSpeedView", 1)[1].split("function paintRecognitionSpeed", 1)[0]
    for word in ("跟得上", "接近上限", "跟不上，字幕會延遲"):
        assert word in view
    assert 'p95 >= 0.9' in view and 'p95 >= 0.7' in view


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(name)


def test_host_speed_line_wording_for_each_band():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = Path("app/static/host.html").read_text(encoding="utf-8")
    function = _extract_function(source, "recognitionSpeedView")

    def sample(p50, p95, count=4, limit=200):
        return {"rtf": {"window": {"count": count, "limit": limit, "rtf": {"p50": p50, "p95": p95}}}}

    def line(p50, p95, word, limit=200):
        return f"辨識速度：一般 {p50:.2f}、最慢 {p95:.2f}（低於 0.9 才跟得上）。最近 {limit} 段。狀態：{word}"

    cases = [
        {"data": None, "text": "辨識速度：開始聽之後才有數字", "tone": "speed-empty"},
        {"data": {}, "text": "辨識速度：開始聽之後才有數字", "tone": "speed-empty"},
        {"data": sample(None, None, count=0), "text": "辨識速度：開始聽之後才有數字", "tone": "speed-empty"},
        {"data": sample(0.42, 0.50), "text": line(0.42, 0.50, "跟得上"), "tone": "speed-ok", "absent": ["接近上限", "跟不上"]},
        {"data": sample(0.69, 0.69), "text": line(0.69, 0.69, "跟得上"), "tone": "speed-ok", "absent": ["接近上限", "跟不上"]},
        {"data": sample(0.70, 0.70), "text": line(0.70, 0.70, "接近上限"), "tone": "speed-warn", "absent": ["跟不上"]},
        {"data": sample(0.80, 0.89), "text": line(0.80, 0.89, "接近上限"), "tone": "speed-warn", "absent": ["跟不上"]},
        {"data": sample(0.90, 0.90), "text": line(0.90, 0.90, "跟不上，字幕會延遲"), "tone": "speed-bad"},
        {"data": sample(1.20, 1.40), "text": line(1.20, 1.40, "跟不上，字幕會延遲"), "tone": "speed-bad"},
    ]
    script = function + """
const cases = JSON.parse(process.argv[1]);
let failed = 0;
for (const item of cases) {
  const view = recognitionSpeedView(item.data);
  const problems = [];
  if (view.text !== item.text) problems.push("text " + JSON.stringify(view.text));
  if (view.tone !== item.tone) problems.push("tone " + view.tone);
  if (!String(view.title).includes("最近 200 段")) problems.push("title");
  for (const word of item.absent || []) {
    if (view.text.includes(word)) problems.push("unexpected " + word);
  }
  if (problems.length) {
    console.log(JSON.stringify(item.text), problems.join("; "));
    failed++;
  }
}
if (failed) process.exit(1);
"""
    proc = subprocess.run(
        [node, "-e", script, json.dumps(cases)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr




class TimeoutAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        time.sleep(0.25)
        return AsrResult(ok=True, text="太慢")


@pytest.mark.anyio
async def test_asr_timeout_is_not_an_rtf_sample():
    asr = TimeoutAsr()
    app = app_for(asr, asr_timeout_s=0.05, asr_workers=1)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            timed = await push(client, token, 1, blob)
            assert timed.status_code == 408, timed.text
            assert timed.json()["status"] == "timeout"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert EXISTING <= set(body)
            assert body["asr_timeouts"] == 1
            assert body["silent_skipped"] == 0
            assert body["asr_empty"] == 0
            assert body["last_process_ms"] is None
            assert body["rtf"]["session"]["count"] == 0
            assert body["rtf"]["window"]["count"] == 0
            assert body["rtf"]["session"]["rtf"]["p95"] is None
            assert body["rtf"]["window"]["rtf"]["p95"] is None
            assert body["backlog_audio_s"] == 0
            assert body["rejected"] == 0
            asr.transcribe = lambda wav, prompt: AsrResult(ok=True, text="中文")
            done = await push(client, token, 2, blob)
            assert done.status_code == 200, done.text
            assert done.json()["zh"] == "中文"
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["asr_timeouts"] == 1
            assert after["silent_skipped"] == 0
            assert after["asr_empty"] == 0
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 1
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
            assert after["rtf"]["session"]["rtf"]["p95"] is not None
            assert after["rtf"]["sessions"][0]["asr_timeouts"] == 1
    finally:
        await app.state.shutdown()


@pytest.mark.anyio
async def test_alternating_rooms_keep_their_own_rtf_session():
    asr = TimedAsr(0)
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            east = await push(client, token, 1, blob, session="live", room="east")
            west = await push(client, token, 1, blob, session="live", room="west")
            again = await push(client, token, 2, blob, session="live", room="east")
            assert [item.status_code for item in (east, west, again)] == [200, 200, 200]
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            rows = _rows(body)
            assert rows[("east", "live")]["count"] == 2
            assert rows[("west", "live")]["count"] == 1
            assert body["rtf"]["session"]["count"] == 2
            assert body["rtf"]["window"]["count"] == 3
            assert body["asr_timeouts"] == 0
            nxt = await push(client, token, 1, blob, session="next", room="east")
            assert nxt.status_code == 200, nxt.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            rows = _rows(after)
            assert ("east", "live") not in rows
            assert rows[("east", "next")]["count"] == 1
            assert rows[("west", "live")]["count"] == 1
            assert after["rtf"]["session"]["count"] == 1
            assert after["rtf"]["window"]["count"] == 4
            assert asr.calls == 4
    finally:
        await app.state.shutdown()


def test_skip_counters_stay_out_of_rtf_and_do_not_share_a_tally():
    meter = RtfMeter()
    meter.note_silent_skip(("east", "a"))
    meter.note_empty(("west", "b"))
    meter.note_timeout(("west", "b"))
    snap = meter.snapshot()
    assert snap["silent_skipped"] == 1
    assert snap["asr_empty"] == 1
    assert snap["asr_timeouts"] == 1
    assert snap["rtf"]["window"]["count"] == 0
    assert snap["asr_rtf_p95"] is None
    rows = _rows(snap)
    assert ("east", "a") not in rows
    assert rows[("west", "b")]["count"] == 0
    assert rows[("west", "b")]["asr_timeouts"] == 1
    assert rows[("west", "b")]["asr_empty"] == 1
    assert rows[("west", "b")]["silent_skipped"] == 0
    meter.record_ms(100, 1000, ("east", "a"), decode_ms=12, wait_ms=34)
    rows = _rows(meter.snapshot())
    assert rows[("east", "a")]["silent_skipped"] == 1
    assert rows[("east", "a")]["asr_empty"] == 0
    assert rows[("east", "a")]["count"] == 1
    assert rows[("east", "a")]["decode_ms"]["p50"] == 12
    assert rows[("east", "a")]["asr_wait_ms"]["p50"] == 34
    assert rows[("west", "b")]["asr_empty"] == 1
    before = meter.snapshot()["asr_rtf_p95"]
    meter.note_timeout(("east", "a"))
    meter.note_silent_skip(("east", "a"))
    after = meter.snapshot()
    assert after["asr_timeouts"] == 2
    assert after["silent_skipped"] == 2
    assert after["asr_empty"] == 1
    assert after["asr_rtf_p95"] == before
    assert after["rtf"]["window"]["count"] == 1
    meter.drop_room("east")
    dropped = meter.snapshot()
    assert ("east", "a") not in _rows(dropped)
    assert dropped["silent_skipped"] == 2
    assert dropped["asr_timeouts"] == 2
    assert dropped["asr_empty"] == 1


def test_backlog_is_per_room_and_an_estimate_until_the_duration_is_known():
    meter = RtfMeter()
    meter.note_waiting(("east", "s", 1), 1.5)
    meter.note_waiting(("west", "s", 1), 2.5, estimated=True)
    snap = meter.snapshot()
    assert snap["backlog_audio_s"] == 4.0
    assert snap["backlog_s"] == 4.0
    assert snap["backlog_estimated"] is True
    assert snap["backlog_by_room"] == {"east": 1.5, "west": 2.5}
    meter.clear_waiting(("east", "s", 1))
    cleared = meter.snapshot()
    assert cleared["backlog_audio_s"] == 2.5
    assert cleared["backlog_by_room"] == {"west": 2.5}
    assert cleared["backlog_estimated"] is True
    meter.drop_room("west")
    gone = meter.snapshot()
    assert gone["backlog_audio_s"] == 0
    assert gone["backlog_s"] == 0
    assert gone["backlog_estimated"] is False
    assert gone["backlog_by_room"] == {}


def test_percentile_cache_recomputes_only_when_a_sample_arrives(monkeypatch):
    calls = {"n": 0}
    real = percentile

    def wrapped(values, p):
        calls["n"] += 1
        return real(values, p)

    monkeypatch.setattr("app.rtf.percentile", wrapped)
    meter = RtfMeter()
    meter.record_ms(100, 1000, ("room", "s"), decode_ms=10, wait_ms=20)
    meter.record_ms(300, 1000, ("room", "s"), decode_ms=30, wait_ms=40)
    first = meter.snapshot()
    used = calls["n"]
    assert used > 0
    assert first["asr_rtf_p50"] == first["rtf"]["window"]["rtf"]["p50"]
    assert first["decode_ms_p50"] == pytest.approx(percentile([10, 30], 0.50))
    assert first["asr_wait_ms_p95"] == pytest.approx(percentile([20, 40], 0.95))
    assert first["rtf"]["sessions"][0]["recent"]["count"] == 2
    meter.note_waiting(("room", "s", 1), 1.5, estimated=True)
    meter.note_silent_skip(("room", "s"))
    meter.note_timeout(("room", "s"))
    second = meter.snapshot()
    assert calls["n"] == used
    assert second["rtf"]["window"] is first["rtf"]["window"]
    assert second["backlog_s"] == 1.5
    assert second["backlog_estimated"] is True
    assert second["silent_skipped"] == 1
    assert second["asr_timeouts"] == 1
    assert second["asr_rtf_p95"] == first["asr_rtf_p95"]
    meter.record_ms(900, 1000, ("room", "s"), decode_ms=50, wait_ms=60)
    third = meter.snapshot()
    assert calls["n"] > used
    assert third["rtf"]["window"] is not first["rtf"]["window"]
    assert third["asr_samples"] == 3
    assert third["asr_rtf_p95"] == third["rtf"]["window"]["rtf"]["p95"]
    assert third["backlog_s"] == 1.5
    assert third["silent_skipped"] == 1


class HoldAsr:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del wav, prompt
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(3), "first recognition was not released"
            return AsrResult(ok=True, text="先")
        return AsrResult(ok=True, text="後")


@pytest.mark.anyio
async def test_backlog_is_counted_from_upload_and_cleared_when_asr_starts():
    entered = threading.Event()
    release_decode = threading.Event()
    asr = GateAsr()

    def decoder(src: Path, work: Path) -> Path:
        entered.set()
        assert release_decode.wait(3), "decode was not released"
        return copy_decoder(src, work)

    app = create_app(
        Settings(allow_testclient=True, max_audio_bytes=2_000_000, segment_ms=6000),
        asr=asr,
        translator=Translator(enabled=True, key=""),
        decoder=decoder,
    )
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            task = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(entered.wait, 2)
            during = (await client.get("/api/metrics", headers=auth(token))).json()
            assert during["backlog_audio_s"] == pytest.approx(6.0)
            assert during["backlog_s"] == pytest.approx(6.0)
            assert during["backlog_estimated"] is True
            assert during["backlog_by_room"].get("class") == pytest.approx(6.0)
            assert asr.calls == 0
            release_decode.set()
            assert await asyncio.to_thread(asr.started.wait, 2)
            await asyncio.sleep(0.05)
            started = (await client.get("/api/metrics", headers=auth(token))).json()
            assert started["backlog_audio_s"] == 0
            assert started["backlog_s"] == 0
            assert started["backlog_estimated"] is False
            assert asr.calls == 1
            asr.release.set()
            done = await asyncio.wait_for(task, 3)
            assert done.status_code == 200, done.text
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["backlog_audio_s"] == 0
            assert after["rtf"]["session"]["audio_ms"]["max"] == 1000
    finally:
        release_decode.set()
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_last_process_excludes_asr_slot_wait():
    asr = HoldAsr()
    app = app_for(asr, asr_workers=1)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, 1, blob))
            assert await asyncio.to_thread(asr.started.wait, 2)
            second = asyncio.create_task(push(client, token, 2, blob))
            waiting = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                waiting = (await client.get("/api/metrics", headers=auth(token))).json()
                if asr.calls == 1 and waiting["pending"] == 2 and waiting["backlog_s"] <= 1.01:
                    break
                await asyncio.sleep(0.02)
            assert waiting is not None
            assert waiting["backlog_s"] == pytest.approx(1.0, abs=0.001)
            await asyncio.sleep(0.45)
            asr.release.set()
            done = await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert [item.status_code for item in done] == [200, 200]
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["asr_wait_ms_p95"] >= 400
            assert after["last_process_ms"] < after["asr_wait_ms_p95"] - 200
            assert after["decode_ms_p95"] is not None
            assert after["rtf"]["session"]["count"] == 2
            assert after["asr_rtf_p95"] == after["rtf"]["window"]["rtf"]["p95"]
            waits = [row[4] for row in app.state.pipeline._rtf._window]
            assert max(waits) >= 400
            assert min(waits) < 200
    finally:
        asr.release.set()
        await app.state.shutdown()


@pytest.mark.anyio
async def test_empty_asr_is_counted_apart_from_silence_and_timeouts():
    class Scripted:
        def __init__(self):
            self.calls = 0

        def transcribe(self, wav: Path, prompt: str) -> AsrResult:
            del wav, prompt
            self.calls += 1
            if self.calls == 1:
                return AsrResult(ok=True, text="   ")
            return AsrResult(ok=False, text="", error="辨識失敗")

    asr = Scripted()
    app = app_for(asr)
    blob = wave_bytes(1.0)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            empty = await push(client, token, 1, blob)
            assert empty.status_code == 200, empty.text
            assert empty.json()["status"] == "silent"
            body = (await client.get("/api/metrics", headers=auth(token))).json()
            assert body["asr_empty"] == 1
            assert body["silent_skipped"] == 0
            assert body["asr_timeouts"] == 0
            assert body["rtf"]["session"]["count"] == 1
            assert body["rtf"]["sessions"][0]["asr_empty"] == 1
            failed = await push(client, token, 2, blob)
            assert failed.status_code == 422, failed.text
            assert failed.json()["status"] == "error"
            after = (await client.get("/api/metrics", headers=auth(token))).json()
            assert after["asr_empty"] == 1
            assert after["silent_skipped"] == 0
            assert after["asr_timeouts"] == 0
            assert after["rtf"]["session"]["count"] == 2
            assert asr.calls == 2
    finally:
        await app.state.shutdown()
