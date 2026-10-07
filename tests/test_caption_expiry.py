"""Expired captions are removed and the audience in that room is told.

A silent drop looked like a glitch. caption_ttl_s must be a positive finite
number so a sweep cannot wipe the bus every second.
"""

import asyncio
import math
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, app_for, open_room, push, stop, token_of


def test_caption_ttl_must_be_positive():
    Settings(caption_ttl_s=1)
    Settings(caption_ttl_s=0.5)
    for bad in (0, -1, math.inf, math.nan):
        with pytest.raises(ValueError, match="BREEZE_CAPTION_TTL 必須大於 0"):
            Settings(caption_ttl_s=bad)
    for raw in ("0", "-1", "inf", "nan"):
        with pytest.raises(ValueError, match="BREEZE_CAPTION_TTL 必須大於 0"):
            Settings.from_env({"BREEZE_CAPTION_TTL": raw})


@pytest.mark.anyio
async def test_expiring_captions_are_announced_only_in_that_room():
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, caption_ttl_s=30),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await open_room(client, token, "other")
            first = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            second = await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)
            foreign = await push(client, token, "other", "s", 1, "別班".encode(), t0_ms=0, t1_ms=1000)
            assert first.status_code == 200, first.text
            assert second.status_code == 200, second.text
            assert foreign.status_code == 200, foreign.text
            old_id = first.json()["id"]
            kept_id = second.json()["id"]
            app.state.bus._caption_at["class"][old_id] = time.time() - 120

            async with Socket(app, "/ws/listen?room_id=class", client=("127.0.0.1", 5101)) as room, Socket(
                app, "/ws/listen?room_id=other", client=("127.0.0.1", 5102)
            ) as other:
                hello = await room.recv()
                assert hello["type"] == "hello"
                other_hello = await other.recv()
                assert other_hello["type"] == "hello"
                assert "別班" in str(other_hello)
                await app.state.sweep_once()
                note = await room.recv()
                assert note["type"] == "captions_expired"
                assert note["room_id"] == "class"
                assert note["ids"] == [old_id]
                assert kept_id not in note["ids"]
                assert "別班" not in str(note)
                with pytest.raises(asyncio.TimeoutError):
                    await other.recv(timeout=0.3)
            assert old_id not in {item.get("id") for item in app.state.bus.history("class")}
            assert any(item.get("id") == kept_id for item in app.state.bus.history("class"))
            assert any(item.get("zh") == "別班" for item in app.state.bus.history("other"))
            assert old_id not in app.state.bus._tomb.get("class", ())
    finally:
        await stop(app)
