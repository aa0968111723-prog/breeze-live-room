import asyncio
import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.server import create_app
from app.settings import Settings
from app.translate import TranslateResult, Translator

ALLOWED_STATUS = {
    "queued",
    "decoding",
    "transcribing",
    "zh_ready",
    "ready",
    "silent",
    "translate_failed",
    "missing",
    "error",
    "timeout",
    "cancelled",
}


class EchoAsr:
    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        del prompt
        return AsrResult(ok=True, text=wav.read_bytes().decode())


def copy_decoder(src: Path, work: Path) -> Path:
    wav = work / "audio.wav"
    wav.write_bytes(src.read_bytes())
    return wav


def settings_with(**kwargs):
    """Drop fields the original Settings dataclass does not have, so these tests run on it."""
    fields = getattr(Settings, "__dataclass_fields__", {})
    return Settings(**{key: value for key, value in kwargs.items() if key in fields})


def app_for(**kwargs):
    settings = kwargs.pop("settings", None) or Settings(allow_testclient=True)
    return create_app(
        settings,
        asr=kwargs.pop("asr", None) or EchoAsr(),
        translator=kwargs.pop("translator", None) or Translator(enabled=True, key=""),
        decoder=kwargs.pop("decoder", None) or copy_decoder,
    )


def auth(token):
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1"}


async def token_of(app, client):
    resp = await client.get("/api/host-token")
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def push(client, token, room, session, seq, payload, *, wait_translation=None, async_header=False):
    headers = auth(token)
    if async_header:
        headers["x-breeze-async-translation"] = "1"
    data = {"room_id": room, "session_id": session, "seq": str(seq)}
    if wait_translation is not None:
        data["wait_translation"] = wait_translation
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data=data,
        files={"audio": ("a.webm", payload, "audio/webm")},
        headers=headers,
    )


async def stop(app):
    await app.state.shutdown()


def versions_of(app, room):
    found = {}
    for item in app.state.bus._log.get(room, []):
        if not item.get("id"):
            continue
        found.setdefault(item["id"], []).append(int(item["version"]))
    return found


def assert_versions_only_increase(app, room):
    for seg_id, versions in versions_of(app, room).items():
        assert versions == sorted(versions), (seg_id, versions)
        assert len(versions) == len(set(versions)), (seg_id, versions)


class HoldEnglish(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="test-key")
        self.release = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.order = []
        self.threads = []

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            if self.inflight >= 2:
                self.started.set()
        self.release.wait(5)
        with self.lock:
            self.inflight -= 1
            self.order.append(zh)
        return TranslateResult("EN " + zh, "ok")


class RacingEnglish(Translator):
    """Blocks until two translations are inside translate(), then finishes the fast line first."""

    def __init__(self):
        super().__init__(enabled=True, key="test-key")
        self.barrier = threading.Barrier(2, timeout=5)
        self.lock = threading.Lock()
        self.max_inflight = 0
        self.inflight = 0
        self.order = []
        self.threads = []

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            return TranslateResult("", "error", "英譯失敗，中文仍保留")
        time.sleep(0.2 if "慢" in zh else 0.04)
        with self.lock:
            self.order.append(zh)
            self.inflight -= 1
        return TranslateResult("EN " + zh, "ok")


class SleepEnglish(Translator):
    def __init__(self, delay: float):
        super().__init__(enabled=True, key="test-key")
        self.delay = delay

    def translate(self, zh: str, glossary=None, context=None) -> TranslateResult:
        del glossary, context
        self.calls += 1
        time.sleep(self.delay)
        return TranslateResult("EN " + zh, "ok")


async def wait_for_english(app, room, count, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready = [item for item in app.state.bus.history(room) if item.get("en")]
        if len(ready) >= count:
            return app.state.bus.history(room)
        await asyncio.sleep(0.02)
    return app.state.bus.history(room)


@pytest.mark.anyio
async def test_opt_in_push_returns_chinese_before_slow_english():
    translator = HoldEnglish()
    app = app_for(translator=translator)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def form_opt(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            async def header_opt(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    async_header=True,
                )

            try:
                form_resp, header_resp = await asyncio.wait_for(
                    asyncio.gather(form_opt(1, "般若"), header_opt(2, "空性")),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                raise AssertionError("opt-in push blocked until English finished")
            for resp, text in ((form_resp, "般若"), (header_resp, "空性")):
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["zh"] == text
                assert body["en"] == ""
                assert body["status"] == "zh_ready"
                assert body["status"] in ALLOWED_STATUS
            assert translator.release.is_set() is False
            translator.release.set()
            history = await wait_for_english(app, "class", 2)
        by_seq = {item["seq"]: item for item in history}
        assert by_seq[1]["en"] == "EN 般若"
        assert by_seq[1]["zh"] == "般若"
        assert by_seq[2]["en"] == "EN 空性"
        assert by_seq[2]["zh"] == "空性"
        assert all(item["status"] in ALLOWED_STATUS for item in history)
        assert_versions_only_increase(app, "class")
        assert by_seq[1]["version"] >= 2
        assert by_seq[2]["version"] >= 2
    finally:
        translator.release.set()
        await stop(app)


@pytest.mark.anyio
async def test_parallel_translations_finish_out_of_order_on_their_own_segments():
    translator = RacingEnglish()
    app = app_for(translator=translator, settings=settings_with(allow_testclient=True, translate_workers=2))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(one(1, "慢句"), one(2, "快句")),
                    timeout=1.5,
                )
            except asyncio.TimeoutError:
                raise AssertionError("push waited on the single translation worker")
            assert [item.status_code for item in results] == [200, 200]
            assert all(item.json()["en"] == "" and item.json()["status"] == "zh_ready" for item in results)
            history = await wait_for_english(app, "class", 2, timeout=2.0)
        assert translator.max_inflight >= 2
        assert translator.order and "快" in translator.order[0]
        assert all(name.startswith("breeze-translate") for name in translator.threads)
        by_seq = {item["seq"]: item for item in history}
        assert by_seq[1]["zh"] == "慢句" and by_seq[1]["en"] == "EN 慢句"
        assert by_seq[2]["zh"] == "快句" and by_seq[2]["en"] == "EN 快句"
        assert_versions_only_increase(app, "class")
    finally:
        translator.barrier.abort()
        await stop(app)


@pytest.mark.anyio
async def test_async_pushes_return_while_translation_is_still_queued():
    translator = SleepEnglish(0.4)
    app = app_for(translator=translator)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq):
                return await push(
                    client, token, "class", "s", seq, f"第{seq}句".encode(),
                    wait_translation="0",
                )

            started = time.monotonic()
            try:
                results = await asyncio.wait_for(asyncio.gather(*(one(seq) for seq in range(1, 7))), timeout=1.2)
            except asyncio.TimeoutError:
                raise AssertionError("six pushes waited out serial translation")
            elapsed = time.monotonic() - started
            assert elapsed < 1.2
            assert all(item.status_code == 200 for item in results)
            for seq, item in enumerate(results, start=1):
                body = item.json()
                assert body["zh"] == f"第{seq}句"
                assert body["en"] == ""
                assert body["status"] == "zh_ready"
            history = await wait_for_english(app, "class", 6, timeout=3.0)
        by_seq = {item["seq"]: item for item in history}
        assert len(by_seq) == 6
        for seq in range(1, 7):
            assert by_seq[seq]["zh"] == f"第{seq}句"
            assert by_seq[seq]["en"] == f"EN 第{seq}句"
            assert by_seq[seq]["status"] == "ready"
        assert translator.calls == 6
        assert_versions_only_increase(app, "class")
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_queued_translation_past_the_timeout_is_skipped_without_dropping_chinese():
    translator = HoldEnglish()
    app = app_for(
        translator=translator,
        settings=settings_with(allow_testclient=True, translate_workers=1, translate_timeout_s=2.0, translate_queue=4),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def one(seq, text):
                return await push(
                    client, token, "class", "s", seq, text.encode(),
                    wait_translation="0", async_header=True,
                )

            try:
                first, second, third = await asyncio.wait_for(
                    asyncio.gather(one(1, "先走"), one(2, "太舊"), one(3, "也太舊")),
                    timeout=2,
                )
            except asyncio.TimeoutError:
                raise AssertionError("push blocked on the single translation worker before a stale line could be skipped")
            assert first.json()["zh"] == "先走" and first.json()["en"] == ""
            for item in (first, second, third):
                assert item.status_code == 200
                assert item.json()["zh"]
                assert item.json()["status"] in ALLOWED_STATUS
            deadline = time.monotonic() + 2
            while translator.calls < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert translator.calls == 1
            queued = app.state.pipeline._translate_q._queue
            assert list(queued), "expected later segments to still be waiting on the single worker"
            aged = []
            for item in list(queued):
                epoch, _enqueued_at, segment = item
                aged.append((epoch, time.monotonic() - 5, segment))
            queued.clear()
            queued.extend(aged)
            translator.release.set()
            history = await wait_for_english(app, "class", 1, timeout=2)
            skipped = [item for item in history if item.get("translate_status") == "skipped"]
            assert len(skipped) >= 2
            assert {item["zh"] for item in skipped} >= {"太舊", "也太舊"}
            for item in skipped:
                assert item["en"] == ""
                assert item["status"] == "translate_failed"
                assert item["status"] in ALLOWED_STATUS
            done = {item["seq"]: item for item in history}
            assert done[1]["zh"] == "先走" and done[1]["en"] == "EN 先走"
            assert translator.calls == 1
            assert_versions_only_increase(app, "class")
    finally:
        translator.release.set()
        await stop(app)


def test_translate_deadline_bounds_retry_sleep():
    slept = []

    def opener(req, timeout=40):
        del req
        raise TimeoutError()

    def sleeper(delay: float) -> None:
        slept.append(delay)
        time.sleep(delay)

    translator = Translator(
        enabled=True,
        key="k",
        max_attempts=3,
        max_backoff=30,
        sleeper=sleeper,
        opener=opener,
    )
    past = translator.translate("般若", deadline=time.monotonic() - 1)
    assert past.status == "timeout"
    assert past.detail == "英譯逾時，不假設沒有計費。中文仍保留"
    assert translator.calls == 0
    assert slept == []
    started = time.monotonic()
    bounded = translator.translate("般若", deadline=started + 0.06)
    assert bounded.status == "timeout"
    assert "不假設沒有計費" in bounded.detail
    # Uncapped retries would sleep 0.2s then 0.4s. The deadline must cut that off.
    assert time.monotonic() - started < 0.4
    assert sum(slept) < 0.15


def test_translate_workers_defaults_to_two_and_rejects_zero():
    assert Settings().translate_workers == 2
    assert Settings.from_env({}).translate_workers == 2
    assert Settings.from_env({"BREEZE_TRANSLATE_WORKERS": "4"}).translate_workers == 4
    with pytest.raises(ValueError, match="BREEZE_TRANSLATE_WORKERS"):
        Settings(translate_workers=0)


def test_host_page_opts_into_async_translation_and_listens_for_english():
    text = Path("app/static/host.html").read_text(encoding="utf-8")
    upload = text.split("upload: async", 1)[1].split("onPhase", 1)[0]
    assert 'wait_translation", "0"' in upload
    assert "x-breeze-async-translation" in upload
    assert "x-breeze-retry" in upload
    assert "connectRoom" in text
    assert "/ws/listen?room_id=" in text
    assert "mergeCaptionUpdate" in text
