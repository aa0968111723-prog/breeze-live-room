"""P1-1 / N-1. Audience sockets need an allowed origin and this open's listen key.

A missing Origin is allowed on purpose: the pytest sockets and scripts/device_check.py
do not send one. Browsers always send Origin, and a present Origin must match the
allowlist (loopback, allowed_hosts, or the share host printed on the QR code).
The Host header is always checked, so a missing Origin is not a DNS-rebinding hole.
A refused listen is accepted, named, then closed, and never gets hello or a caption.
Closing before accept is an HTTP 403, which a browser shows as 1006.
"""

import json
import time
import urllib.parse
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.server import _ReplayGate
from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, app_for, auth, open_room, push, stop, token_of


ROOT = Path(__file__).resolve().parents[1]


def _headers(host="127.0.0.1:8780", origin=None, authorization=None):
    rows = [(b"host", host.encode())]
    if origin is not None:
        rows.append((b"origin", origin.encode()))
    if authorization is not None:
        rows.append((b"authorization", authorization.encode()))
    return rows


async def drive(app, path, *, client=("127.0.0.1", 5000), headers=None, with_key=True, timeout=2.0):
    """Speak ASGI websocket without requiring accept. Returns the raw messages."""
    import asyncio

    from tests.test_round2 import append_listen_key

    if with_key:
        path = append_listen_key(app, path)
    path, _, query = path.partition("?")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers if headers is not None else _headers(),
        "client": client,
        "server": ("127.0.0.1", 8780),
        "subprotocols": [],
    }
    out: asyncio.Queue = asyncio.Queue()
    inc: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await inc.get()

    async def send(message):
        await out.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    await inc.put({"type": "websocket.connect"})
    messages = []
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(out.get(), max(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            messages.append(msg)
            if msg.get("type") == "websocket.close":
                break
            if msg.get("type") != "websocket.send":
                continue
            try:
                body = json.loads(msg.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(body, dict) and body.get("type") == "hello":
                break
        return messages
    finally:
        await inc.put({"type": "websocket.disconnect", "code": 1000})
        if not task.done():
            try:
                await asyncio.wait_for(task, 1)
            except Exception:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


def _sent_text(messages) -> str:
    parts = []
    for msg in messages:
        if msg.get("type") == "websocket.send":
            parts.append(msg.get("text") or "")
    return "\n".join(parts)


def _close_code(messages):
    for msg in messages:
        if msg.get("type") == "websocket.close":
            return msg.get("code")
    return None


def _assert_rejected(messages, code):
    """Accept, then the refusal, then the close. No hello and no caption."""
    kinds = [msg.get("type") for msg in messages]
    assert "websocket.accept" in kinds, messages
    assert "websocket.send" in kinds, messages
    assert "websocket.close" in kinds, messages
    assert kinds.index("websocket.accept") < kinds.index("websocket.send") < kinds.index("websocket.close")
    assert _close_code(messages) == code
    sent = []
    for msg in messages:
        if msg.get("type") != "websocket.send":
            continue
        sent.append(json.loads(msg.get("text") or "{}"))
    assert len(sent) == 1, sent
    note = sent[0]
    assert note.get("type") == "room_unavailable"
    assert "backfill" not in note and "history" not in note and "zh" not in note and "events" not in note
    assert "hello" not in _sent_text(messages)
    if code == 4401:
        assert note.get("reason") == "link_invalid"
    elif code == 1008:
        assert note.get("reason") == "rejected"
    else:
        raise AssertionError(code)


def _hello(messages) -> dict:
    for msg in messages:
        if msg.get("type") != "websocket.send":
            continue
        body = json.loads(msg.get("text") or "{}")
        if isinstance(body, dict) and body.get("type") == "hello":
            return body
    raise AssertionError(messages)


def _zh(rows) -> list:
    return [item.get("zh") for item in rows or [] if isinstance(item, dict)]


async def _close_room(client, token, room):
    resp = await client.post(
        "/api/rooms/close",
        json={"room_id": room},
        headers={**auth(token), "content-type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    return resp


@pytest.mark.anyio
async def test_bad_origin_and_host_rejected_without_hello():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            await push(client, token, "class", "s", 1, "不該漏出".encode())
            share = "203.0.113.10"
            quoted = urllib.parse.quote(key, safe="")
            path = f"/ws/listen?room_id=class&k={quoted}&replay=1"

            evil = await drive(
                app, path, with_key=False,
                headers=_headers(origin="http://evil.example:8780"),
            )
            _assert_rejected(evil, 1008)
            assert "不該漏出" not in _sent_text(evil)

            # A cross-site page targeting the share host still sends the page's Origin.
            await client.post(
                "/api/share-host",
                json={"host": share, "room_id": "class"},
                headers={**auth(token), "content-type": "application/json"},
            )
            crossed = await drive(
                app, path, with_key=False,
                headers=_headers(host=f"{share}:8780", origin="http://evil.example:8780"),
            )
            _assert_rejected(crossed, 1008)

            rebound = await drive(
                app, path, with_key=False,
                headers=_headers(host="127.0.0.1.evil.example:8780"),
            )
            _assert_rejected(rebound, 1008)
            wrong_port = await drive(
                app, path, with_key=False,
                headers=_headers(host="127.0.0.1:9999"),
            )
            _assert_rejected(wrong_port, 1008)

            # Not a share host yet: a LAN origin is not a blanket allow.
            stranger = await drive(
                app, path, with_key=False,
                headers=_headers(host="203.0.113.9:8780", origin="http://203.0.113.9:8780"),
            )
            _assert_rejected(stranger, 1008)

            phone = await drive(
                app, path, with_key=False,
                headers=_headers(host=f"{share}:8780", origin=f"http://{share}:8780"),
            )
            assert any(msg.get("type") == "websocket.accept" for msg in phone)
            hello = _hello(phone)
            assert hello["room_id"] == "class"
            assert "不該漏出" in _zh(hello.get("backfill"))

            # Non-browser clients omit Origin. Host still has to match.
            blank = await drive(app, path, with_key=False, headers=_headers(origin=""))
            assert any(msg.get("type") == "websocket.accept" for msg in blank)
            assert _hello(blank)["type"] == "hello"
            missing = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=True, headers=_headers())
            assert _hello(missing)["type"] == "hello"

            illegal = await drive(app, "/ws/listen?room_id=bad%20room", with_key=False)
            _assert_rejected(illegal, 1008)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_missing_or_wrong_listen_key_rejected_without_hello():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            again = await open_room(client, token, "class")
            assert again.json()["listen_key"] == key
            await push(client, token, "class", "s", 1, "只有持鑰聽得到".encode())

            missing = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=False)
            _assert_rejected(missing, 4401)
            assert "只有持鑰聽得到" not in _sent_text(missing)

            wrong = await drive(app, "/ws/listen?room_id=class&replay=1&k=wrong-key", with_key=False)
            _assert_rejected(wrong, 4401)

            # The host token is not accepted in the query string, and the listen key is not a bearer token.
            as_query = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(token, safe=""),
                with_key=False,
            )
            _assert_rejected(as_query, 4401)
            as_bearer = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + key),
            )
            _assert_rejected(as_bearer, 4401)

            host = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + token),
            )
            assert any(msg.get("type") == "websocket.accept" for msg in host)
            assert "只有持鑰聽得到" in _zh(_hello(host).get("backfill"))

            # A rejected key must not take the only listener slot.
            tight = app_for(
                settings=Settings(allow_testclient=True, translate=False, max_listeners=1),
                translator=Translator(enabled=False),
            )
            try:
                async with AsyncClient(transport=ASGITransport(app=tight), base_url="http://127.0.0.1:8780") as other:
                    other_token = await token_of(tight, other)
                    opened_tight = await open_room(other, other_token, "class")
                    tight_key = opened_tight.json()["listen_key"]
                    denied = await drive(tight, "/ws/listen?room_id=class&k=nope", with_key=False)
                    _assert_rejected(denied, 4401)
                    allowed = await drive(
                        tight,
                        "/ws/listen?room_id=class&k=" + urllib.parse.quote(tight_key, safe=""),
                        with_key=False,
                    )
                    assert _hello(allowed)["type"] == "hello"
            finally:
                await stop(tight)
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_reopen_rejects_old_key_and_limits_replay_to_this_open():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = await open_room(client, token, "class")
            old_key = first.json()["listen_key"]
            pushed = await push(client, token, "class", "s", 1, "上一堂".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            await _close_room(client, token, "class")

            exported = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            assert exported.status_code == 200, exported.text
            assert "上一堂" in exported.text
            srt = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert srt.status_code == 200, srt.text
            assert "上一堂" in srt.text

            second = await open_room(client, token, "class")
            new_key = second.json()["listen_key"]
            assert new_key and new_key != old_key

            stale = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(old_key, safe=""),
                with_key=False,
            )
            _assert_rejected(stale, 4401)
            assert "上一堂" not in _sent_text(stale)

            fresh = await drive(
                app,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(new_key, safe=""),
                with_key=False,
            )
            hello = _hello(fresh)
            blob = json.dumps(hello, ensure_ascii=False)
            assert "上一堂" not in blob
            assert _zh(hello.get("history")) == []
            assert _zh(hello.get("backfill")) == []

            # A new class gets a new session id, the same way the host page does.
            newer = await push(client, token, "class", "t", 1, "這一堂".encode(), t0_ms=0, t1_ms=1000)
            assert newer.status_code == 200, newer.text
            live = await drive(app, "/ws/listen?room_id=class&replay=1", with_key=True)
            hello = _hello(live)
            assert "這一堂" in _zh(hello.get("backfill"))
            assert "上一堂" not in json.dumps(hello, ensure_ascii=False)

            # A cursor past the live window is the same backfill, still limited to this open.
            gapped = await drive(app, "/ws/listen?room_id=class&cursor=1000000000", with_key=True)
            gap_hello = _hello(gapped)
            assert gap_hello["gap"] is True
            assert "上一堂" not in json.dumps(gap_hello, ensure_ascii=False)
            assert "這一堂" in _zh(gap_hello.get("backfill"))

            hosted = await drive(
                app, "/ws/listen?room_id=class&replay=1", with_key=False,
                headers=_headers(authorization="Bearer " + token),
            )
            hosted_hello = _hello(hosted)
            assert "這一堂" in _zh(hosted_hello.get("backfill"))
            assert "上一堂" not in json.dumps(hosted_hello, ensure_ascii=False)

            both = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            rows = json.loads(both.text)
            assert "上一堂" in _zh(rows) and "這一堂" in _zh(rows)
            srt_both = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert "上一堂" in srt_both.text and "這一堂" in srt_both.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_replay_backfill_is_rate_limited_per_ip():
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, replay_per_minute=1, history_limit=1),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "class", "s", 2, "乙".encode(), t0_ms=1000, t1_ms=2000)

            first = _hello(await drive(app, "/ws/listen?room_id=class&replay=1", client=("127.0.0.1", 5000)))
            assert "甲" in _zh(first.get("backfill"))
            assert "甲" not in _zh(first.get("history"))

            second = _hello(await drive(app, "/ws/listen?room_id=class&replay=1", client=("127.0.0.1", 5001)))
            assert "backfill" not in second
            assert "甲" not in json.dumps(second, ensure_ascii=False)
            assert "乙" in _zh(second.get("history"))

            other = _hello(await drive(
                app, "/ws/listen?room_id=class&replay=1", client=("198.51.100.8", 5000),
            ))
            assert "甲" in _zh(other.get("backfill"))

            # Live captions are not counted against the replay budget.
            async with Socket(app, "/ws/listen?room_id=class&cursor=0") as sock:
                hello = await sock.recv()
                assert hello["type"] == "hello"
                assert "backfill" not in hello
                made = await push(client, token, "class", "s", 3, "丙".encode(), t0_ms=2000, t1_ms=3000)
                assert made.status_code == 200, made.text
                seen = None
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and seen is None:
                    msg = await sock.recv(timeout=max(0.1, deadline - time.monotonic()))
                    if msg.get("zh") == "丙":
                        seen = msg
                assert seen is not None
    finally:
        await stop(app)


def test_replay_per_minute_setting_defaults_and_rejects_zero():
    # Classroom floor stays. 180 is the highest accepted address cap; the
    # behaviour tests below are what keep a rotating client id inside it.
    assert Settings().replay_per_minute >= 120
    assert Settings().replay_per_minute <= 180
    assert Settings().replay_client_per_minute == 8
    assert Settings().replay_client_per_minute <= Settings().replay_per_minute
    assert Settings.from_env({"BREEZE_REPLAY_PER_MINUTE": "3"}).replay_per_minute == 3
    assert Settings.from_env({"BREEZE_REPLAY_CLIENT_PER_MINUTE": "4"}).replay_client_per_minute == 4
    with pytest.raises(ValueError, match="BREEZE_REPLAY_PER_MINUTE"):
        Settings(replay_per_minute=0)
    with pytest.raises(ValueError, match="BREEZE_REPLAY_CLIENT_PER_MINUTE"):
        Settings(replay_client_per_minute=0)


def _wire_bytes(rows) -> int:
    return len(json.dumps(list(rows or []), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _full_replay(hello: dict) -> bool:
    """A full replay is a hello whose backfill list is present and not deferred."""
    if hello.get("backfill_deferred") is True or "backfill" not in hello:
        return False
    rows = hello.get("backfill")
    assert isinstance(rows, list) and rows, hello
    assert len(rows) <= 200
    assert _wire_bytes(rows) <= 100 * 1024
    return True


def _spoofed(index: int):
    """Forwarded headers a proxy might add. The listen path must ignore them."""
    return _headers() + [
        (b"x-forwarded-for", f"198.51.100.{index % 200}, 203.0.113.9".encode()),
        (b"x-real-ip", f"203.0.113.{index % 200}".encode()),
        (b"forwarded", f'for=192.0.2.{index % 200};proto=http'.encode()),
    ]


@pytest.mark.anyio
async def test_same_ip_rotating_cids_stay_inside_the_replay_cap():
    """One TCP address, three bursts of 40 replay hellos.

    Distinct well-formed client ids all succeed (40 is under the address cap,
    and each id is used once). Omitting the id, or repeating one id, stops at
    the per-client cap. The address total of the three bursts stays within the
    address cap: a client id can only tighten it.
    """
    ip_cap = Settings().replay_per_minute
    client_cap = Settings().replay_client_per_minute
    assert 40 <= ip_cap <= 180
    assert client_cap == 8
    app = app_for(
        settings=Settings(allow_testclient=True, translate=False, max_listeners=100, history_limit=8),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text

            async def burst(query_for, count, port_base):
                full = 0
                for index in range(count):
                    hello = _hello(await drive(
                        app,
                        "/ws/listen?room_id=class&replay=1" + query_for(index),
                        client=("203.0.113.40", port_base + index),
                    ))
                    if _full_replay(hello):
                        full += 1
                        assert "甲" in _zh(hello.get("backfill"))
                    else:
                        assert hello.get("backfill_deferred") is True
                        assert "backfill" not in hello
                return full

            rotating = await burst(lambda index: f"&cid=phone-{index:02d}", 40, 5000)
            missing = await burst(lambda index: "", 40, 6000)
            fixed = await burst(lambda index: "&cid=same-phone", 40, 7000)
            assert rotating == 40
            assert missing == client_cap
            assert fixed == client_cap
            assert rotating + missing + fixed <= ip_cap
            assert missing < rotating and fixed < rotating
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_forwarded_headers_cannot_reset_the_tcp_replay_cap():
    """X-Forwarded-For, X-Real-IP, and Forwarded are not the replay address."""
    cap = 5
    app = app_for(
        settings=Settings(
            allow_testclient=True,
            translate=False,
            replay_per_minute=cap,
            replay_client_per_minute=8,
            max_listeners=80,
            history_limit=4,
        ),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await open_room(client, token, "class")
            made = await push(client, token, "class", "s", 1, "甲".encode(), t0_ms=0, t1_ms=1000)
            assert made.status_code == 200, made.text
            exhausted = "203.0.113.9"
            full = 0
            for index in range(40):
                hello = _hello(await drive(
                    app,
                    f"/ws/listen?room_id=class&replay=1&cid=rot-{index:02d}",
                    client=(exhausted, 8000 + index),
                    headers=_spoofed(index),
                ))
                if _full_replay(hello):
                    full += 1
            assert full == cap
            other = _hello(await drive(
                app,
                "/ws/listen?room_id=class&replay=1&cid=other-net",
                client=("198.51.100.8", 8100),
                headers=_headers() + [
                    (b"x-forwarded-for", exhausted.encode()),
                    (b"x-real-ip", exhausted.encode()),
                    (b"forwarded", f"for={exhausted}".encode()),
                ],
            ))
            assert _full_replay(other)
            still = _hello(await drive(
                app,
                "/ws/listen?room_id=class&replay=1&cid=rot-extra",
                client=(exhausted, 8101),
                headers=_spoofed(99),
            ))
            assert still.get("backfill_deferred") is True
            assert "backfill" not in still
    finally:
        await stop(app)


def test_replay_gate_address_cap_bounds_rotating_cids_and_keys():
    """Production limits: rotating ids stop at the address cap, and the tables stay bounded."""
    cap = Settings().replay_per_minute
    client_cap = Settings().replay_client_per_minute
    now = {"t": 1000.0}
    gate = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert gate.ip_limit == cap
    assert gate.limit == cap
    assert gate.client_limit == client_cap
    assert _ReplayGate(cap, cap + 50, clock=lambda: now["t"]).client_limit == cap
    allowed = 0
    for index in range(cap + 40):
        ok, retry = gate.allow("203.0.113.9", f"cid-{index}")
        if ok:
            allowed += 1
        else:
            assert retry >= 250
    assert allowed == cap
    assert gate.allow("203.0.113.9", "cid-extra")[0] is False
    assert len(gate._client_hits) == cap
    assert ("203.0.113.9", "cid-extra") not in gate._client_hits
    assert gate.allow("198.51.100.8", "cid-0")[0] is True

    fixed = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert sum(fixed.allow("203.0.113.9", "same")[0] for _ in range(40)) == client_cap
    assert list(fixed._client_hits) == [("203.0.113.9", "same")]
    missing = _ReplayGate(cap, client_cap, clock=lambda: now["t"])
    assert sum(missing.allow("203.0.113.9", "")[0] for _ in range(40)) == client_cap
    assert list(missing._client_hits) == [("203.0.113.9", "")]

    now["t"] = 0.0
    denied = _ReplayGate(1, 1, clock=lambda: now["t"], max_keys=4)
    assert denied.allow("10.0.0.1", "kept")[0] is True
    for index in range(30):
        assert denied.allow("10.0.0.1", f"nope-{index}")[0] is False
    assert list(denied._client_hits) == [("10.0.0.1", "kept")]
    assert len(denied._ip_hits) == 1

    now["t"] = 10.0
    bounded = _ReplayGate(2, 2, window_s=60.0, clock=lambda: now["t"], max_keys=2)
    assert bounded.allow("10.1.0.1", "a")[0] is True
    now["t"] = 20.0
    assert bounded.allow("10.1.0.2", "b")[0] is True
    now["t"] = 30.0
    assert bounded.allow("10.1.0.3", "c")[0] is True
    assert "10.1.0.1" not in bounded._ip_hits
    assert ("10.1.0.1", "a") not in bounded._client_hits
    assert set(bounded._ip_hits) == {"10.1.0.2", "10.1.0.3"}
    assert len(bounded._client_hits) == 2
    now["t"] = 80.0
    assert bounded.allow("10.1.0.9", "fresh")[0] is True
    assert "10.1.0.2" not in bounded._ip_hits
    assert ("10.1.0.2", "b") not in bounded._client_hits
    assert "10.1.0.3" in bounded._ip_hits
    assert len(bounded._ip_hits) <= 2
    assert len(bounded._client_hits) <= 2

    now["t"] = 0.0
    again = _ReplayGate(1, 1, window_s=60.0, clock=lambda: now["t"])
    assert again.allow("10.2.0.1", "a")[0] is True
    assert again.allow("10.2.0.1", "a")[0] is False
    now["t"] = 61.0
    assert again.allow("10.2.0.1", "a")[0] is True
    assert len(again._ip_hits["10.2.0.1"]) == 1
    assert len(again._client_hits[("10.2.0.1", "a")]) == 1


@pytest.mark.anyio
async def test_host_export_keeps_previous_class_across_restart(tmp_path):
    path = tmp_path / "captions.sqlite3"
    settings = Settings(allow_testclient=True, translate=False, data_path=str(path))
    first = app_for(settings=settings, translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=first), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(first, client)
            await open_room(client, token, "class")
            pushed = await push(client, token, "class", "s", 1, "上一堂".encode(), t0_ms=0, t1_ms=1000)
            assert pushed.status_code == 200, pushed.text
            await _close_room(client, token, "class")
            await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
    finally:
        await stop(first)

    resumed = app_for(
        settings=Settings(allow_testclient=True, translate=False, data_path=str(path)),
        translator=Translator(enabled=False),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=resumed), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(resumed, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            hello = _hello(await drive(
                resumed,
                "/ws/listen?room_id=class&replay=1&k=" + urllib.parse.quote(key, safe=""),
                with_key=False,
            ))
            assert "上一堂" not in json.dumps(hello, ensure_ascii=False)
            exported = await client.get("/api/export", params={"room_id": "class", "kind": "json"}, headers=auth(token))
            assert exported.status_code == 200, exported.text
            rows = json.loads(exported.text)
            assert "上一堂" in _zh(rows)
            srt = await client.get("/api/export", params={"room_id": "class", "kind": "srt"}, headers=auth(token))
            assert "上一堂" in srt.text
            assert token not in exported.text
            assert token not in srt.text
    finally:
        await stop(resumed)


@pytest.mark.anyio
async def test_listen_key_does_not_cross_rooms_and_unknown_rooms_stay_unknown():
    app = app_for(settings=Settings(allow_testclient=True, translate=False), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            room_a = (await open_room(client, token, "rooma")).json()["listen_key"]
            room_b = (await open_room(client, token, "roomb")).json()["listen_key"]
            assert room_a != room_b
            await push(client, token, "rooma", "s", 1, "甲房".encode(), t0_ms=0, t1_ms=1000)
            await push(client, token, "roomb", "s", 1, "乙房".encode(), t0_ms=0, t1_ms=1000)

            crossed = await drive(
                app,
                "/ws/listen?room_id=roomb&replay=1&k=" + urllib.parse.quote(room_a, safe=""),
                with_key=False,
            )
            _assert_rejected(crossed, 4401)
            assert "甲房" not in _sent_text(crossed) and "乙房" not in _sent_text(crossed)

            own = _hello(await drive(
                app,
                "/ws/listen?room_id=roomb&replay=1&k=" + urllib.parse.quote(room_b, safe=""),
                with_key=False,
            ))
            assert "乙房" in _zh(own.get("backfill"))
            assert "甲房" not in json.dumps(own, ensure_ascii=False)

            before = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
            unknown = await drive(app, "/ws/listen?room_id=nosuchroom", with_key=False)
            assert any(msg.get("type") == "websocket.accept" for msg in unknown)
            note = json.loads(next(msg["text"] for msg in unknown if msg.get("type") == "websocket.send"))
            assert note["type"] == "room_unavailable"
            assert note["reason"] == "unknown_or_ended"
            assert _close_code(unknown) == 4404
            after = (await client.get("/api/metrics", headers=auth(token))).json()["rooms"]
            assert after == before
            assert "nosuchroom" not in app.state.rooms

            async with Socket(app, "/ws/listen?room_id=roomb") as sock:
                hello = await sock.recv()
                assert hello["type"] == "hello"
                await push(client, token, "rooma", "s", 2, "甲房第二句".encode(), t0_ms=1000, t1_ms=2000)
                leaked = False
                try:
                    msg = await sock.recv(timeout=0.4)
                except Exception:
                    msg = None
                if isinstance(msg, dict) and (msg.get("zh") or "") == "甲房第二句":
                    leaked = True
                assert leaked is False
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_public_setup_and_qr_omit_listen_key_until_the_host_asks(monkeypatch):
    captured = {}

    def fake_make(url):
        captured["url"] = url

        class Img:
            def save(self, buf, kind):
                buf.write(b"\x89PNG\r\n")

        return Img()

    import qrcode
    monkeypatch.setattr(qrcode, "make", fake_make)

    app = app_for(settings=Settings(allow_testclient=True, translate=False, share_host="203.0.113.10"), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            opened = await open_room(client, token, "class")
            key = opened.json()["listen_key"]
            assert "?k=" in (opened.json()["listen_url"] or "")
            assert key in opened.json()["listen_url"]

            public = (await client.get("/api/setup", params={"room_id": "class"})).json()
            assert "listen_key" not in public
            assert public["host_token"] is None
            assert public["listen_url"] == "http://203.0.113.10:8780/r/class"
            assert key not in json.dumps(public)
            assert token not in json.dumps(public)

            qr = await client.get("/api/qr", params={"room_id": "class"})
            assert qr.status_code == 200
            assert captured["url"] == "http://203.0.113.10:8780/r/class"
            assert key not in qr.content.decode("latin1")
            assert token.encode() not in qr.content

            wrong = (await client.get(
                "/api/setup",
                params={"room_id": "class"},
                headers={"authorization": "Bearer nope", "origin": "http://127.0.0.1:8780"},
            )).json()
            assert "listen_key" not in wrong
            assert "k=" not in (wrong.get("listen_url") or "")

            evil = (await client.get(
                "/api/setup",
                params={"room_id": "class"},
                headers={"authorization": f"Bearer {token}", "origin": "http://evil.example"},
            )).json()
            assert "listen_key" not in evil
            assert key not in json.dumps(evil)

            host = (await client.get("/api/setup", params={"room_id": "class"}, headers=auth(token))).json()
            assert host["listen_key"] == key
            assert host["listen_url"] == "http://203.0.113.10:8780/r/class?k=" + urllib.parse.quote(key, safe="")
            assert token not in json.dumps(host)

            hosted_qr = await client.get("/api/qr", params={"room_id": "class"}, headers=auth(token))
            assert hosted_qr.status_code == 200
            assert captured["url"] == host["listen_url"]
            assert key in captured["url"]
            assert token not in captured["url"]
    finally:
        await stop(app)


def test_audience_page_and_host_page_pass_the_listen_key():
    room = (ROOT / "app/static/room.html").read_text(encoding="utf-8")
    host = (ROOT / "app/static/host.html").read_text(encoding="utf-8")
    client = (ROOT / "app/static/room_client.js").read_text(encoding="utf-8")
    checker = (ROOT / "scripts/device_check.py").read_text(encoding="utf-8")
    assert 'URLSearchParams(location.search).get("k")' in room
    assert '&k=" + encodeURIComponent(listenKey)' in room
    assert 'authorization: "Bearer " + token' in host
    assert "data.listen_key" in host
    assert '&k=" + encodeURIComponent(listenKey)' in host
    # room_client.js calls url() on every attempt, so a reconnect keeps the key.
    address = client[client.index("function address()"): client.index("function isCaption")]
    assert "url()" in address
    assert "不會改走匿名連線" in checker
    assert "&cursor=0&replay=1&k=" in checker
