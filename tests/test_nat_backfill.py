"""Classroom NAT: many listeners on one public IP all get the restart backfill.

A pure per-IP cap of 8 left everyone after the eighth phone with a hello that
had no backfill and no reason. The client then cleared the screen. Denied
replays are explicit (backfill_deferred + retry_after) and do not include
another room's captions.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.settings import Settings
from app.translate import Translator
from tests.test_listen_key import _hello, _zh, drive
from tests.test_round2 import app_for, open_room, push, stop, token_of


LINES = ("甲", "乙", "丙", "丁")


async def _class_with_lines(tmp_path, **settings):
    path = tmp_path / "captions.sqlite3"
    base = dict(allow_testclient=True, translate=False, data_path=str(path), max_listeners=80)
    base.update(settings)
    app = app_for(settings=Settings(**base), translator=Translator(enabled=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        token = await token_of(app, client)
        await open_room(client, token, "class")
        await open_room(client, token, "other")
        for seq, text in enumerate(LINES, start=1):
            made = await push(client, token, "class", "s", seq, text.encode(), t0_ms=(seq - 1) * 1000, t1_ms=seq * 1000)
            assert made.status_code == 200, made.text
        other = await push(client, token, "other", "s", 1, "別班".encode(), t0_ms=0, t1_ms=1000)
        assert other.status_code == 200, other.text
    await stop(app)
    return path


@pytest.mark.anyio
async def test_sixty_listeners_same_ip_get_full_backfill_after_restart(tmp_path):
    path = await _class_with_lines(tmp_path)
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, data_path=str(path), max_listeners=80),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "other")

            async def one(index):
                # Stale cursor from before the restart. Same public IP, own client id.
                return await drive(
                    app,
                    f"/ws/listen?room_id=class&cursor=5000&cid=phone{index}",
                    client=("203.0.113.50", 10000 + index),
                )

            messages = await asyncio.gather(*(one(index) for index in range(60)))
            assert len(messages) == 60
            for item in messages:
                hello = _hello(item)
                assert hello.get("backfill_deferred") is not True
                assert _zh(hello.get("backfill")) == list(LINES)
                blob = str(hello)
                assert "別班" not in blob

            foreign = _hello(await drive(
                app,
                "/ws/listen?room_id=other&cursor=5000&cid=phone-other",
                client=("203.0.113.50", 20000),
            ))
            assert _zh(foreign.get("backfill")) == ["別班"]
            assert not any(text in _zh(foreign.get("backfill")) for text in LINES)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_deferred_backfill_names_the_wait_and_hides_captions():
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=1,
            replay_client_per_minute=8,
            history_limit=1,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            made = await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)
            assert made.status_code == 200, made.text
            first = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=phone-a", client=("198.51.100.9", 5000),
            ))
            assert _zh(first.get("backfill")) == ["甲", "乙"]
            second = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=phone-b", client=("198.51.100.9", 5001),
            ))
            assert second.get("backfill_deferred") is True
            assert "backfill" not in second
            assert int(second.get("retry_after_ms") or 0) >= 250
            assert int(second.get("retry_after") or 0) == int(second["retry_after_ms"])
            # The live window may still carry the newest line. The older line lives
            # only in the full backfill, and that list is not sent.
            assert "甲" not in str(second)
            assert "乙" in _zh(second.get("history"))
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_replay_limit_is_per_client_as_well_as_per_ip():
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=180,
            replay_client_per_minute=1,
            history_limit=1,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            made = await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)
            assert made.status_code == 200, made.text
            first = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=same-phone", client=("198.51.100.10", 5000),
            ))
            assert _zh(first.get("backfill")) == ["甲", "乙"]
            again = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=same-phone", client=("198.51.100.10", 5001),
            ))
            assert again.get("backfill_deferred") is True
            assert "甲" not in str(again)
            other = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1&cid=other-phone", client=("198.51.100.10", 5002),
            ))
            assert _zh(other.get("backfill")) == ["甲", "乙"]
    finally:
        await stop(app)
