from __future__ import annotations

import asyncio
import io
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.asr import CliAsr, ResidentAsr
from app.audio import AudioError, convert_to_wav, ffmpeg_bin, wav_duration_seconds
from app.auth import new_host_token, require_host, require_local_host
from app.dispatch import ListenerSlot, RoomBus
from app.pipeline import Pipeline, PipelineError, Segment
from app.rooms import RoomBook, RoomIdError, validate_room_id, validate_session_id
from app.settings import Settings, fill_process_environ
from app.share import list_share_hosts, listen_url
from app.store import CaptionStore
from app.textutil import export_text, parse_glossary
from app.translate import Translator

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
DEFAULT_WHISPER = ROOT / "tools" / "whisper-cli.exe"
DEFAULT_SERVER = ROOT / "tools" / "whisper-server.exe"
TMP = ROOT / "tmp"
PROMPT = "以下是台灣國語的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"

_TRACKED: list[FastAPI] = []


def rss_bytes() -> int:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident = int(handle.read().split()[1])
        return resident * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


class Conn:
    def __init__(self, ws: WebSocket, maxsize: int):
        self.ws = ws
        self.slot = ListenerSlot(ws.send_json, maxsize=maxsize)
        self.slot.last_pong = time.monotonic()


def _content_too_large(request: Request, settings: Settings) -> bool:
    raw = request.headers.get("content-length")
    if not raw:
        return False
    try:
        size = int(raw)
    except ValueError:
        return False
    return size > settings.max_audio_bytes + 65536


def _early_key(request: Request) -> tuple[str, str, int] | None:
    room = request.query_params.get("room_id")
    session = request.query_params.get("session_id")
    seq = request.query_params.get("seq")
    if not room or not session or not seq:
        return None
    try:
        return (validate_room_id(room), validate_session_id(session), int(seq))
    except (RoomIdError, ValueError):
        return None


async def _wait_until_join_ready(pipeline: Pipeline, key: tuple[str, str, int]) -> None:
    """Owner has reserved this segment but not registered a flight. Do not take another slot."""
    while key in pipeline._reserved or key in pipeline._active:
        flight = pipeline._flight.get(key)
        if flight is not None and not flight.done():
            return
        await asyncio.sleep(0.01)


async def _read_upload(upload, limit: int) -> bytes:
    if upload is None or isinstance(upload, str):
        raise AudioError(400, "沒有收到音訊")
    chunks = []
    total = 0
    while True:
        block = await upload.read(64 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise AudioError(413, f"音訊超過 {limit} bytes，已拒絕")
        chunks.append(block)
    if total == 0:
        raise AudioError(400, "沒有收到音訊")
    return b"".join(chunks)


def _field(form, request: Request, name: str) -> str:
    value = form.get(name)
    if value is None or isinstance(value, str) and value == "":
        value = request.query_params.get(name, "")
    return str(value or "")


def _optional_ms(form, request: Request, name: str) -> int | None:
    raw = _field(form, request, name)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _public_result(done: Segment) -> JSONResponse:
    if done.status == "timeout":
        code = 408
    elif done.status == "cancelled":
        code = 409
    elif done.status == "error":
        code = 422
    else:
        code = 200
    body = done.public()
    body["ok"] = code == 200
    body["detail"] = done.error
    return JSONResponse(status_code=code, content=body)


def create_app(settings: Settings | None = None, asr=None, translator: Translator | None = None, decoder=None) -> FastAPI:
    if settings is None:
        fill_process_environ()
        settings = Settings.from_env()
    token = new_host_token()
    translator = translator or Translator(
        enabled=settings.translate,
        key=os.getenv("OPENAI_API_KEY", ""),
        token_budget=settings.token_budget,
    )
    model = Path(settings.model_path) if settings.model_path else DEFAULT_MODEL
    whisper = Path(settings.whisper_path) if settings.whisper_path else DEFAULT_WHISPER
    resident_error = ""
    if asr is None:
        if settings.asr_mode == "resident":
            resident = ResidentAsr(
                settings.resident_url,
                server_bin=DEFAULT_SERVER if DEFAULT_SERVER.exists() else whisper,
                model=model,
                threads=settings.asr_threads,
            )
            started = resident.start()
            if started.ok:
                asr = resident
            else:
                resident_error = started.error
                asr = CliAsr(whisper, model, threads=settings.asr_threads)
        else:
            asr = CliAsr(whisper, model, threads=settings.asr_threads)
    book = RoomBook(settings.max_rooms, settings.room_idle_s)
    bus = RoomBus(settings.history_limit)
    store = CaptionStore(settings.data_path or None)
    share_override = {"host": settings.share_host}
    tasks: list[asyncio.Task] = []

    def current_host() -> str | None:
        host = (share_override["host"] or "").strip()
        return host or None

    def on_event(event: dict):
        snap = bus.publish(event)
        if snap is None:
            return None
        room = book.get(str(snap.get("room_id") or ""))
        if room is not None:
            room["history"] = bus.history(snap["room_id"])
            room["last_active"] = time.monotonic()
            dead = []
            for conn in list(room["listeners"]):
                if not conn.slot.offer(snap):
                    dead.append(conn)
            for conn in dead:
                room["listeners"].discard(conn)
        if store.enabled:
            store.save(snap)
        return snap

    pipeline = Pipeline(asr, translator, PROMPT, TMP, settings, on_event=on_event)

    def decode(src: Path, work: Path) -> Path:
        if decoder:
            return decoder(src, work)
        ffmpeg = ffmpeg_bin(ROOT)
        if not ffmpeg:
            raise AudioError(422, "找不到 ffmpeg。請安裝 ffmpeg，或把 ffmpeg.exe 放進 tools\\")
        wav = convert_to_wav(src, work, ffmpeg, timeout=int(settings.decode_timeout_s))
        seconds = wav_duration_seconds(wav)
        if seconds is not None and seconds > settings.max_audio_seconds:
            raise AudioError(413, f"音訊長於 {settings.max_audio_seconds} 秒，已拒絕")
        return wav

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(1)
            for room_id in book.sweep():
                bus.drop(room_id)
                pipeline.drop_room(room_id)
            if store.enabled:
                store.purge_expired(settings.caption_ttl_s)

    async def shutdown() -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        await pipeline.aclose()
        if hasattr(asr, "close"):
            asr.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.ensure_workers()
        tasks.append(asyncio.create_task(sweep_loop()))
        yield
        await shutdown()

    app = FastAPI(title="breeze-live-room", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.settings = settings
    app.state.token = token
    app.state.pipeline = pipeline
    app.state.rooms = book.rooms
    app.state.room_book = book
    app.state.bus = bus
    app.state.translator = translator
    app.state.asr = asr
    app.state.store = store
    app.state.share_override = share_override
    app.state.shutdown = shutdown
    app.state.resident_error = resident_error

    def share_for(room_id: str) -> str | None:
        return listen_url(room_id, settings.port, settings.share_scheme, current_host())

    @app.get("/")
    async def host_page() -> FileResponse:
        return FileResponse(STATIC / "host.html")

    @app.get("/r/{room_id}")
    async def room_page(room_id: str) -> FileResponse:
        try:
            validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(STATIC / "room.html")

    @app.get("/api/host-token")
    async def host_token(request: Request) -> Response:
        require_local_host(request, settings)
        return JSONResponse({"token": token}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.get("/api/setup")
    async def setup(room_id: str = "class") -> dict:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = share_for(room_id)
        resident_ready = isinstance(asr, ResidentAsr) and asr.health()
        return {
            "room": room_id,
            "listen_url": url,
            "share_ready": url is not None,
            "share_message": None if url else "尚無可供其他裝置使用的連結",
            "share_hosts": list_share_hosts(),
            "share_host": current_host() or "",
            "port": settings.port,
            "whisper": whisper.exists(),
            "model": model.exists(),
            "ffmpeg": ffmpeg_bin(ROOT) is not None,
            "translate_configured": bool(translator.key) and settings.translate,
            "translate_verified": False,
            "translate_label": translator.status_label(),
            "asr_mode": "resident" if resident_ready else "cli",
            "model_reloads_each_segment": not resident_ready,
            "resident_error": resident_error,
            "host_token": None,
            "queue": pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "storage": store.enabled,
        }

    @app.get("/api/qr")
    async def qr(room_id: str = "class") -> Response:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = share_for(room_id)
        if not url:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "尚無可供其他裝置使用的連結"})
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png")

    @app.post("/api/rooms/open")
    async def open_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or "class"))
        book.open(room_id)
        return {"ok": True, "room": room_id, "listen_url": share_for(room_id)}

    @app.post("/api/rooms/touch")
    async def touch_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            raise HTTPException(status_code=404, detail="房間不存在或已結束")
        book.touch(room_id)
        return {"ok": True}

    @app.post("/api/rooms/close")
    async def close_room(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        room = book.close(room_id)
        if room:
            for conn in list(room["listeners"]):
                conn.slot.offer({"type": "room_unavailable", "room_id": room_id, "reason": "ended"})
            room["listeners"].clear()
        removed = book.sweep()
        for gone in removed:
            bus.drop(gone)
            pipeline.drop_room(gone)
        return {"ok": True}

    @app.post("/api/session/active")
    async def session_active(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        if book.get(room_id) is None:
            book.open(room_id)
        book.set_session_active(room_id, bool(body.get("active")))
        return {"ok": True}

    @app.post("/api/session/end")
    async def session_end(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        pipeline.end_session(room_id, session_id)
        book.set_session_active(room_id, False)
        return {"ok": True}

    @app.post("/api/segment/missing")
    async def segment_missing(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        if book.get(room_id) is None:
            book.open(room_id)
        segment = pipeline.mark_missing(room_id, session_id, seq, str(body.get("reason") or "主持端放棄這段"))
        return {"ok": True, **segment.public()}

    @app.post("/api/segment/cancel")
    async def segment_cancel(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        if seq < 1:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        try:
            segment = pipeline.request_cancel(room_id, session_id, seq)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status == "cancelled", **segment.public()}

    @app.post("/api/segment/retranslate")
    async def retranslate(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or ""))
        seq = int(body.get("seq") or 0)
        zh = body.get("zh")
        try:
            segment = await pipeline.retranslate(room_id, session_id, seq, zh if isinstance(zh, str) else None)
        except PipelineError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"ok": segment.status != "error", **segment.public()}

    @app.post("/api/glossary")
    async def set_glossary(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        room_id = validate_room_id(str(body.get("room_id") or ""))
        session_id = validate_session_id(str(body.get("session_id") or "default"))
        rows = parse_glossary(str(body.get("text") or ""))
        pipeline.glossary[(room_id, session_id)] = rows
        return {"ok": True, "count": len(rows)}

    @app.post("/api/share-host")
    async def set_share_host(request: Request) -> dict:
        require_host(request, token, settings)
        body = await _json(request)
        share_override["host"] = str(body.get("host") or "").strip()
        return {"ok": True, "listen_url": share_for(str(body.get("room_id") or "class"))}

    @app.get("/api/metrics")
    async def metrics(request: Request) -> dict:
        require_host(request, token, settings)
        return {
            **pipeline.stats(),
            "listeners": sum(len(item["listeners"]) for item in book.rooms.values()),
            "rooms": book._active_count(),
            "rss_bytes": rss_bytes(),
            "tokens_used": translator.tokens_used,
            "price": translator.price_note(),
        }

    @app.get("/api/export")
    async def export(request: Request, room_id: str, kind: str = "txt") -> Response:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        if kind not in {"txt", "json", "srt", "vtt"}:
            raise HTTPException(status_code=400, detail="不支援的匯出格式")
        try:
            payload = export_text(bus.history(room_id), kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media = "application/json" if kind == "json" else "text/plain; charset=utf-8"
        return Response(payload, media_type=media, headers={"Cache-Control": "no-store"})

    @app.delete("/api/captions")
    async def delete_captions(request: Request, room_id: str) -> dict:
        require_host(request, token, settings)
        room_id = validate_room_id(room_id)
        removed = store.delete_room(room_id)
        bus.drop(room_id)
        room = book.get(room_id)
        if room is not None:
            room["history"] = []
        return {"ok": True, "deleted": removed}

    @app.post("/api/push")
    async def push(request: Request) -> dict:
        require_host(request, token, settings)
        if _content_too_large(request, settings):
            raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        query_key = _early_key(request)
        reserved = False
        reserved_key = None
        if not (query_key and pipeline.joinable_without_slot(query_key)):
            if not pipeline.try_admit_count():
                raise HTTPException(status_code=429, detail="辨識佇列已滿，請稍後再送")
            reserved = True
            if query_key:
                pipeline.note_reserved(query_key)
                reserved_key = query_key
        form = None
        try:
            form = await request.form()
            try:
                room_id = validate_room_id(_field(form, request, "room_id"))
                session_id = validate_session_id(_field(form, request, "session_id"))
            except RoomIdError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                seq = int(_field(form, request, "seq"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="段落序號不正確") from exc
            if seq < 1 or seq > 100000:
                raise HTTPException(status_code=400, detail="段落序號不正確")
            key = (room_id, session_id, seq)
            # A speculative slot must not stack on a segment someone else already owns.
            if reserved and reserved_key != key and pipeline.joinable_without_slot(key):
                pipeline.release_slot()
                if reserved_key:
                    pipeline.clear_reserved(reserved_key)
                reserved = False
                reserved_key = None
            elif reserved_key and reserved_key != key:
                pipeline.clear_reserved(reserved_key)
                pipeline.note_reserved(key)
                reserved_key = key
            elif reserved and reserved_key is None:
                pipeline.note_reserved(key)
                reserved_key = key
            book.open(room_id)
            retry = _field(form, request, "retry") == "1" or request.headers.get("x-breeze-retry") == "1"
            segment = Segment(
                room_id=room_id,
                session_id=session_id,
                seq=seq,
                t0_ms=_optional_ms(form, request, "t0_ms"),
                t1_ms=_optional_ms(form, request, "t1_ms"),
            )
            held = reserved
            reserved = False
            try:
                raw = await _read_upload(form.get("audio"), settings.max_audio_bytes)
            except AudioError as exc:
                pipeline.fail_received(segment, exc.detail)
                if held:
                    pipeline.release_slot()
                    held = False
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            if not held and (key in pipeline._reserved or key in pipeline._active):
                await _wait_until_join_ready(pipeline, key)
            try:
                done = await pipeline.submit(segment, raw, decode, slot_held=held, retry=retry, owner=held)
            except AudioError as exc:
                recorded = pipeline.get(segment.room_id, segment.session_id, segment.seq)
                if recorded is not None and recorded.status in {"error", "timeout", "cancelled"}:
                    body = recorded.public()
                    body["ok"] = False
                    body["detail"] = exc.detail or recorded.error
                    # AudioError keeps its own status. _public_result would turn every error into 422.
                    return JSONResponse(status_code=exc.status, content=body)
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            except PipelineError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            return _public_result(done)
        finally:
            if reserved:
                pipeline.release_slot()
            if reserved_key:
                pipeline.clear_reserved(reserved_key)
            if form is not None:
                await form.close()

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket, room_id: str = "class", cursor: int = 0) -> None:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError:
            await ws.close(code=1008)
            return
        await ws.accept()
        room = book.get(room_id)
        if room is None:
            await ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "unknown_or_ended"})
            await ws.close(code=4404)
            return
        if len(room["listeners"]) >= settings.max_listeners:
            await ws.send_json({"type": "room_unavailable", "room_id": room_id, "reason": "full"})
            await ws.close(code=1013)
            return
        conn = Conn(ws, settings.listener_queue)
        room["listeners"].add(conn)
        resumed = bus.since(room_id, cursor)
        hello = {
            "type": "hello",
            "history": bus.history(room_id) if cursor <= 0 else [],
            "events": resumed["events"] if cursor > 0 else [],
            "gap": bool(resumed["gap"]) if cursor > 0 else False,
            "latest_cursor": bus.latest_cursor(room_id),
            "oldest_cursor": resumed["oldest_cursor"],
            "room_id": room_id,
        }
        try:
            await ws.send_json(hello)
        except Exception:
            room["listeners"].discard(conn)
            return
        conn.slot.start()
        ping_task = asyncio.create_task(_ping(conn, settings))
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=settings.idle_timeout_s)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "pong":
                    conn.slot.last_pong = time.monotonic()
        except WebSocketDisconnect:
            pass
        finally:
            ping_task.cancel()
            room["listeners"].discard(conn)
            await conn.slot.close()

    _TRACKED.append(app)
    return app


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="需要 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="需要 JSON 物件")
    return body


async def _ping(conn: Conn, settings: Settings) -> None:
    while True:
        await asyncio.sleep(max(settings.heartbeat_s, 0.05))
        if time.monotonic() - conn.slot.last_pong > settings.idle_timeout_s:
            try:
                await conn.ws.close(code=1001)
            except Exception:
                pass
            return
        if not conn.slot.offer({"type": "ping", "t": time.time()}):
            return


async def shutdown_tracked() -> None:
    while _TRACKED:
        app = _TRACKED.pop()
        shutdown = getattr(app.state, "shutdown", None)
        if shutdown:
            await shutdown()


app = create_app()
