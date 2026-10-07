"""Capped upload body, then Starlette's multipart parser. Request.form is not replaced."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.settings import Settings
from tests.test_round2 import app_for, auth, stop, token_of


def _multipart(parts: list[tuple[str, bytes]], *, boundary: str = "breezebound") -> tuple[bytes, str]:
    """parts are (headers, body). headers is the raw header block without the blank line."""
    chunks: list[bytes] = []
    for headers, body in parts:
        chunks.append(f"--{boundary}\r\n".encode() + headers + b"\r\n\r\n" + body + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _text_part(name: str, value: str) -> tuple[bytes, bytes]:
    header = f'Content-Disposition: form-data; name="{name}"'.encode()
    return header, value.encode()


def _file_part(name: str, filename: str, payload: bytes, *, extra: str = "") -> tuple[bytes, bytes]:
    header = f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'
    if extra:
        header += "; " + extra
    header += "\r\nContent-Type: audio/webm"
    return header.encode(), payload


def test_default_audio_cap_fits_a_short_slice():
    settings = Settings()
    assert settings.max_audio_bytes == 2 * 1024 * 1024
    assert settings.max_audio_seconds == 30.0
    assert settings.upload_read_timeout_s == 20.0
    from_env = Settings.from_env({})
    assert from_env.max_audio_bytes == 2 * 1024 * 1024
    assert from_env.upload_read_timeout_s == 20.0


@pytest.mark.anyio
async def test_valid_upload_returns_chinese():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            resp = await client.post(
                "/api/push",
                data={"room_id": "class", "session_id": "s", "seq": "1"},
                files={"audio": ("a.webm", "課堂".encode(), "audio/webm")},
                headers=auth(token),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "課堂"
            assert app.state.asr.calls == 1
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_non_multipart_is_415_after_auth():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            naked = await client.post(
                "/api/push",
                content=b"{}",
                headers={"content-type": "application/json"},
            )
            assert naked.status_code == 401
            token = await token_of(app, client)
            resp = await client.post(
                "/api/push",
                content=b"{}",
                headers={**auth(token), "content-type": "application/json"},
            )
            assert resp.status_code == 415
            assert resp.json()["detail"] == "上傳格式不正確"
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_malformed_multipart_is_400():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            missing = await client.post(
                "/api/push",
                content=b"--no-boundary\r\n\r\n",
                headers={**auth(token), "content-type": "multipart/form-data"},
            )
            assert missing.status_code == 400, missing.text
            truncated, ctype = _multipart([
                _file_part("audio", "a.webm", b"hello"),
            ])
            truncated = truncated.split(b"\r\n--" )[0]  # drop the closing boundary
            cut = await client.post(
                "/api/push",
                content=truncated,
                headers={**auth(token), "content-type": ctype},
            )
            assert cut.status_code == 400, cut.text
            odd_header = (
                'Content-Disposition: form-data; name="audio"; filename*=undefined\'\'weird.wav\r\n'
                "Content-Type: audio/webm"
            ).encode()
            odd, odd_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                (odd_header, b"not-a-file"),
            ])
            named = await client.post(
                "/api/push",
                content=odd,
                headers={**auth(token), "content-type": odd_type},
            )
            assert named.status_code == 400, named.text
            assert named.status_code != 500
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_too_many_fields_and_files_are_400():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            fields = [_text_part(f"f{i}", "v") for i in range(17)]
            body, ctype = _multipart(fields)
            resp = await client.post(
                "/api/push",
                content=body,
                headers={**auth(token), "content-type": ctype},
            )
            assert resp.status_code == 400, resp.text
            two, two_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                _file_part("audio", "a.webm", b"one"),
                _file_part("extra", "b.webm", b"two"),
            ])
            files = await client.post(
                "/api/push",
                content=two,
                headers={**auth(token), "content-type": two_type},
            )
            assert files.status_code == 400, files.text
            fat = b"y" * (64 * 1024 + 8)
            wide, wide_type = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                (b'Content-Disposition: form-data; name="pad"', fat),
                _file_part("audio", "a.webm", b"one"),
            ])
            oversized = await client.post(
                "/api/push",
                content=wide,
                headers={**auth(token), "content-type": wide_type},
            )
            assert oversized.status_code == 400, oversized.text
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_chunked_oversize_is_413():
    limit = 1024
    app = app_for(settings=Settings(allow_testclient=True, max_audio_bytes=limit))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)

            async def chunks():
                yield b"x" * (limit + 65536 + 1)

            resp = await client.post(
                "/api/push",
                content=chunks(),
                headers={**auth(token), "content-type": "multipart/form-data; boundary=breezebound"},
            )
            assert resp.status_code == 413, resp.text
            assert str(limit) in resp.json()["detail"]
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_slow_body_times_out():
    app = app_for(settings=Settings(allow_testclient=True, upload_read_timeout_s=0.3))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=5) as client:
            token = await token_of(app, client)

            async def slow():
                yield b"--breezebound\r\n"
                await asyncio.sleep(2)
                yield b"--breezebound--\r\n"

            started = time.monotonic()
            resp = await asyncio.wait_for(
                client.post(
                    "/api/push",
                    content=slow(),
                    headers={**auth(token), "content-type": "multipart/form-data; boundary=breezebound"},
                ),
                timeout=3,
            )
            elapsed = time.monotonic() - started
            assert resp.status_code == 408, resp.text
            assert resp.json()["detail"] == "上傳逾時"
            assert elapsed < 2
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_asr_slot_is_not_held_while_the_body_is_read():
    app = app_for()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            body, ctype = _multipart([
                _text_part("room_id", "class"),
                _text_part("session_id", "s"),
                _text_part("seq", "1"),
                _file_part("audio", "a.webm", "還在讀".encode()),
            ])
            entered = asyncio.Event()
            release = asyncio.Event()
            first, rest = body[:24], body[24:]

            async def drip():
                yield first
                entered.set()
                await release.wait()
                yield rest

            task = asyncio.create_task(client.post(
                "/api/push",
                content=drip(),
                headers={**auth(token), "content-type": ctype},
            ))
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not task.done()
            assert app.state.pipeline.stats()["pending"] == 0
            assert not app.state.pipeline._reserved
            release.set()
            resp = await asyncio.wait_for(task, timeout=3)
            assert resp.status_code == 200, resp.text
            assert resp.json()["zh"] == "還在讀"
            assert app.state.pipeline.stats()["pending"] == 0
            assert app.state.asr.calls == 1
    finally:
        await stop(app)
