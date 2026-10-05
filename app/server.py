from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.asr import CliAsr
from app.audio import AudioError, convert_to_wav, ffmpeg_bin, read_limited, wav_duration_seconds
from app.auth import new_host_token, require_host, require_local_host
from app.pipeline import Pipeline, Segment
from app.rooms import RoomIdError, validate_room_id
from app.settings import Settings
from app.share import listen_url
from app.translate import Translator

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
WHISPER = ROOT / "tools" / "whisper-cli.exe"
TMP = ROOT / "tmp"
PROMPT = "以下是普通話的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"


def _upsert(history: list, event: dict) -> None:
    for index, item in enumerate(history):
        if item.get("id") == event.get("id"):
            history[index] = event
            return
    history.append(event)


def create_app(settings: Settings | None = None, asr=None, translator: Translator | None = None, decoder=None) -> FastAPI:
    settings = settings or Settings.from_env()
    token = new_host_token()
    translator = translator or Translator(
        enabled=settings.translate,
        key=os.getenv("OPENAI_API_KEY", ""),
    )
    asr = asr or CliAsr(WHISPER, MODEL)
    pipeline = Pipeline(asr, translator, PROMPT, TMP)
    rooms: dict[str, dict] = {}

    app = FastAPI(title="breeze-live-room")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.settings = settings
    app.state.token = token
    app.state.pipeline = pipeline
    app.state.rooms = rooms
    app.state.translator = translator
    app.state.asr = asr

    def state_for(room_id: str) -> dict:
        if room_id not in rooms:
            if len(rooms) >= settings.max_rooms:
                raise HTTPException(status_code=429, detail="房間數已達上限")
            rooms[room_id] = {"listeners": set(), "history": []}
        return rooms[room_id]

    def decode(src: Path, work: Path) -> Path:
        if decoder:
            return decoder(src, work)
        ffmpeg = ffmpeg_bin(ROOT)
        if not ffmpeg:
            raise AudioError(422, "找不到 ffmpeg。請安裝 ffmpeg，或把 ffmpeg.exe 放進 tools\\")
        wav = convert_to_wav(src, work, ffmpeg)
        seconds = wav_duration_seconds(wav)
        if seconds is not None and seconds > settings.max_audio_seconds:
            raise AudioError(413, f"音訊長於 {settings.max_audio_seconds} 秒，已拒絕")
        return wav

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
    async def host_token(request: Request) -> dict:
        require_local_host(request, settings.allow_testclient)
        return {"token": token}

    @app.get("/api/setup")
    async def setup(room_id: str = "class") -> dict:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = listen_url(room_id, settings.port, settings.share_scheme)
        key_set = bool(os.getenv("OPENAI_API_KEY"))
        return {
            "room": room_id,
            "listen_url": url,
            "share_ready": url is not None,
            "share_message": None if url else "尚無可供其他裝置使用的連結",
            "whisper": WHISPER.exists(),
            "model": MODEL.exists(),
            "ffmpeg": ffmpeg_bin(ROOT) is not None,
            "translate_configured": key_set and settings.translate,
            "translate_verified": False,
            "translate_label": translator.status_label(),
            "asr_mode": "cli",
            "model_reloads_each_segment": True,
            "host_token": None,
        }

    @app.get("/api/qr")
    async def qr(room_id: str = "class") -> Response:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        url = listen_url(room_id, settings.port, settings.share_scheme)
        if not url:
            return JSONResponse(status_code=409, content={"ok": False, "detail": "尚無可供其他裝置使用的連結"})
        import io
        import qrcode
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png")

    @app.post("/api/push")
    async def push(
        request: Request,
        room_id: str = Form(...),
        session_id: str = Form(...),
        seq: int = Form(...),
        audio: UploadFile = File(...),
    ) -> dict:
        require_host(request, token)
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if seq < 1 or seq > 100000:
            raise HTTPException(status_code=400, detail="段落序號不正確")
        if not session_id or len(session_id) > 64:
            raise HTTPException(status_code=400, detail="會話代號不正確")
        blocks = []
        while True:
            block = await audio.read(64 * 1024)
            if not block:
                break
            blocks.append(block)
            if sum(len(b) for b in blocks) > settings.max_audio_bytes:
                raise HTTPException(status_code=413, detail=f"音訊超過 {settings.max_audio_bytes} bytes，已拒絕")
        try:
            raw = read_limited(blocks, settings.max_audio_bytes)
        except AudioError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        state = state_for(room_id)
        if len(pipeline.results) >= settings.max_queue and pipeline.get(room_id, session_id, seq) is None:
            waiting = sum(1 for item in pipeline.results.values() if item.status == "queued")
            if waiting >= settings.max_queue:
                raise HTTPException(status_code=429, detail="辨識佇列已滿，請稍後再送")
        segment = Segment(room_id=room_id, session_id=session_id, seq=seq)
        before = len(pipeline.broadcasts)
        try:
            done = await pipeline.submit(segment, raw, decode)
        except AudioError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        for event in pipeline.broadcasts[before:]:
            _upsert(state["history"], event)
            await _broadcast(state, event)
        state["history"] = state["history"][-settings.history_limit :]
        return {"ok": done.status != "error", **done.public()}

    @app.websocket("/ws/listen")
    async def listen(ws: WebSocket, room_id: str = "class") -> None:
        try:
            room_id = validate_room_id(room_id)
        except RoomIdError:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            state = state_for(room_id)
        except HTTPException:
            await ws.close(code=1013)
            return
        if len(state["listeners"]) >= settings.max_listeners:
            await ws.close(code=1013)
            return
        state["listeners"].add(ws)
        try:
            await ws.send_json({"type": "hello", "history": state["history"][-40:]})
        except Exception:
            state["listeners"].discard(ws)
            return
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state["listeners"].discard(ws)

    return app


async def _broadcast(state: dict, payload: dict) -> None:
    import asyncio
    dead = []
    for ws in list(state["listeners"]):
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=2)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state["listeners"].discard(ws)


app = create_app()
