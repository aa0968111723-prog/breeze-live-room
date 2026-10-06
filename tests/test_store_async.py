import threading
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.store import CaptionStore
from tests.test_pipeline_repair import app_for, auth, push, settings_with, stop, token_of


def _event(seq: int, zh: str = "字") -> dict:
    return {
        "id": f"r:s:{seq}",
        "room_id": "r",
        "session_id": "s",
        "seq": seq,
        "version": 1,
        "zh": zh,
        "status": "ready",
        "t0_ms": 0,
        "t1_ms": 1000,
    }


@pytest.mark.anyio
async def test_store_save_runs_off_event_loop(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    seen = {}
    loop_ident = threading.current_thread().ident
    original = app.state.store._write_save

    def wrapped(event):
        seen["name"] = threading.current_thread().name
        seen["ident"] = threading.current_thread().ident
        return original(event)

    app.state.store._write_save = wrapped
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await push(client, token, "class", "s", 1, "字".encode(), async_header=True, wait_translation="0")
            assert resp.status_code == 200, resp.text
            app.state.store.flush()
        assert seen["name"].startswith("breeze-store")
        assert seen["ident"] != loop_ident
        assert app.state.store.room_rows("class")[0]["zh"] == "字"
    finally:
        await stop(app)


def test_store_writes_preserve_order(tmp_path):
    store = CaptionStore(tmp_path / "captions.sqlite3")
    order = []
    original = store._write_save

    def wrapped(event):
        order.append(event["id"])
        time.sleep(0.02)
        return original(event)

    store._write_save = wrapped
    try:
        store.submit_save(_event(1, "一"))
        store.submit_save(_event(2, "二"))
        store.flush()
        assert order == ["r:s:1", "r:s:2"]
        assert [row["zh"] for row in store.room_rows("r")] == ["一", "二"]
    finally:
        store.close()


def test_delete_room_ordered_after_pending_saves(tmp_path):
    store = CaptionStore(tmp_path / "captions.sqlite3")
    order = []
    original = store._write_save
    original_delete = store._delete_room_now

    def wrapped(event):
        order.append("save")
        time.sleep(0.04)
        return original(event)

    def wrapped_delete(room_id):
        order.append("delete")
        return original_delete(room_id)

    store._write_save = wrapped
    store._delete_room_now = wrapped_delete
    try:
        store.submit_save(_event(1))
        store.submit_save(_event(2))
        removed = store.enqueue_delete_room("r").result(timeout=3)
        assert removed == 2
        assert order == ["save", "save", "delete"]
        assert store.room_rows("r") == []
        store.save(_event(3, "新"))
        assert [row["seq"] for row in store.room_rows("r")] == [3]
    finally:
        store.close()


def test_shutdown_flushes_pending_saves(tmp_path):
    path = tmp_path / "captions.sqlite3"
    store = CaptionStore(path)
    started = threading.Event()
    release = threading.Event()
    original = store._write_save

    def wrapped(event):
        started.set()
        assert release.wait(3)
        return original(event)

    store._write_save = wrapped
    store.submit_save(_event(1, "存下"))
    assert started.wait(2)

    def let_go():
        time.sleep(0.05)
        release.set()

    threading.Thread(target=let_go, daemon=True).start()
    store.close()
    again = CaptionStore(path)
    try:
        rows = again.room_rows("r")
        assert [row["zh"] for row in rows] == ["存下"]
    finally:
        again.close()


@pytest.mark.anyio
async def test_store_error_logged_and_pipeline_not_wedged(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    original = app.state.store._write_save
    state = {"first": True}

    def wrapped(event):
        if state["first"] and int(event.get("seq") or 0) == 1:
            state["first"] = False
            raise RuntimeError("disk full")
        return original(event)

    app.state.store._write_save = wrapped
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await push(client, token, "class", "s", 1, "第一".encode(), async_header=True, wait_translation="0")
            second = await push(client, token, "class", "s", 2, "第二".encode(), async_header=True, wait_translation="0")
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            app.state.store.flush()
            assert app.state.store.errors >= 1
            rows = app.state.store.room_rows("class")
            assert any(row["zh"] == "第二" for row in rows)
            assert any(item.get("zh") == "第二" for item in app.state.bus.caption_state("class"))
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_sweep_loop_survives_purge_error(tmp_path):
    app = app_for(settings=settings_with(allow_testclient=True, translate=False, data_path=str(tmp_path / "captions.sqlite3")))
    calls = {"n": 0}

    def boom(_ttl):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("purge broke")
        return 0

    app.state.store.purge_expired = boom
    try:
        await app.state.sweep_once()
        await app.state.sweep_once()
        assert calls["n"] == 2
    finally:
        await stop(app)
