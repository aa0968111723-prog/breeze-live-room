from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
WHISPER = ROOT / "tools" / "whisper-cli.exe"
PROMPT = "以下是普通話的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。"

app = FastAPI(title="breeze-live-room")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

rooms: dict[str, dict] = {}

def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()

def room(room_id: str) -> dict:
    room_id = (room_id or "class")[:32]
    if room_id not in rooms:
        rooms[room_id] = {"listeners": set(), "history": []}
    return rooms[room_id]

async def broadcast(state: dict, payload: dict) -> None:
    dead = []
    for ws in list(state["listeners"]):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state["listeners"].discard(ws)

def transcribe(wav: Path) -> str:
    if not WHISPER.exists() or not MODEL.exists():
        return ""
    proc = subprocess.run(
        [str(WHISPER), "-m", str(MODEL), "-f", str(wav), "-l", "zh", "-np", "-nt", "-t", "6", "--prompt", PROMPT],
        capture_output=True, text=True, timeout=90,
    )
    return " ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())

def translate(zh: str) -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key or not zh:
        return ""
    import json
    import urllib.request
    body = json.dumps({
        "model": os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4.1-mini"),
        "messages": [
            {"role": "system", "content": "Translate the Traditional Chinese lecture line into natural English. Translate questions, do not answer them. Keep Buddhist terms stable."},
            {"role": "user", "content": zh},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()

@app.get("/")
async def host_page() -> FileResponse:
    return FileResponse(STATIC / "host.html")

@app.get("/r/{room_id}")
async def room_page(room_id: str) -> FileResponse:
    return FileResponse(STATIC / "room.html")

@app.get("/api/setup")
async def setup(room_id: str = "class") -> dict:
    ip = lan_ip()
    return {
        "room": room_id,
        "listen_url": f"http://{ip}:8780/r/{room_id}",
        "whisper": WHISPER.exists(),
        "model": MODEL.exists(),
        "translate": bool(os.getenv("OPENAI_API_KEY")),
    }

@app.get("/api/qr")
async def qr(room_id: str = "class") -> Response:
    import qrcode
    import io
    img = qrcode.make(f"http://{lan_ip()}:8780/r/{room_id}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")

@app.post("/api/chunk")
async def chunk(room_id: str = "class") -> dict:
    return {"error": "use multipart"}

@app.post("/api/audio")
async def audio(room_id: str = "class") -> dict:
    return {"ok": False, "detail": "post wav as form field audio"}

from fastapi import File, Form, UploadFile

@app.post("/api/push")
async def push(room_id: str = Form("class"), audio: UploadFile = File(...)) -> dict:
    state = room(room_id)
    raw = await audio.read()
    tmp = ROOT / "models" / f"{uuid.uuid4().hex}.wav"
    tmp.write_bytes(raw)
    try:
        zh = await asyncio.to_thread(transcribe, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not zh:
        return {"ok": False, "detail": "no transcript"}
    en = await asyncio.to_thread(translate, zh)
    event = {"type": "final", "id": uuid.uuid4().hex[:10], "zh": zh, "en": en, "t": int(time.time() * 1000)}
    state["history"].append(event)
    state["history"] = state["history"][-80:]
    await broadcast(state, event)
    return {"ok": True, **event}

@app.websocket("/ws/listen")
async def listen(ws: WebSocket, room_id: str = "class") -> None:
    await ws.accept()
    state = room(room_id)
    state["listeners"].add(ws)
    await ws.send_json({"type": "hello", "history": state["history"][-40:]})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        state["listeners"].discard(ws)
