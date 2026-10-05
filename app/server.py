from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
MODEL = ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
WHISPER = ROOT / "tools" / "whisper-cli.exe"
TMP = ROOT / "tmp"
TMP.mkdir(exist_ok=True)
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

def ffmpeg_bin() -> str | None:
    local = ROOT / "tools" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")

def to_wav(src: Path) -> Path:
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg。請安裝 ffmpeg，或把 ffmpeg.exe 放進 tools\\")
    wav = src.with_suffix(".wav")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        capture_output=True, text=True, timeout=40,
    )
    if proc.returncode != 0 or not wav.exists():
        raise RuntimeError(proc.stderr[-400:] or "ffmpeg 轉檔失敗")
    return wav

def transcribe(wav: Path) -> str:
    if not WHISPER.exists():
        raise RuntimeError("找不到 tools\\whisper-cli.exe")
    if not MODEL.exists():
        raise RuntimeError("找不到 models\\ggml-breeze-asr-25-q5_0.bin，請先跑 install.bat")
    proc = subprocess.run(
        [str(WHISPER), "-m", str(MODEL), "-f", str(wav), "-l", "zh", "-np", "-nt", "-t", "6", "--prompt", PROMPT],
        capture_output=True, text=True, timeout=120,
    )
    text = " ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())
    if proc.returncode != 0 and not text:
        raise RuntimeError(proc.stderr[-400:] or "Breeze 辨識失敗")
    return text

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
    with urllib.request.urlopen(req, timeout=40) as resp:
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
    return {
        "room": room_id,
        "listen_url": f"http://{lan_ip()}:8780/r/{room_id}",
        "whisper": WHISPER.exists(),
        "model": MODEL.exists(),
        "ffmpeg": ffmpeg_bin() is not None,
        "translate": bool(os.getenv("OPENAI_API_KEY")),
    }

@app.get("/api/qr")
async def qr(room_id: str = "class") -> Response:
    import io
    import qrcode
    img = qrcode.make(f"http://{lan_ip()}:8780/r/{room_id}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")

@app.post("/api/push")
async def push(room_id: str = Form("class"), audio: UploadFile = File(...)) -> dict:
    state = room(room_id)
    stem = TMP / uuid.uuid4().hex
    src = stem.with_suffix(".webm")
    wav = None
    try:
        src.write_bytes(await audio.read())
        wav = await asyncio.to_thread(to_wav, src)
        zh = await asyncio.to_thread(transcribe, wav)
        if not zh:
            return {"ok": False, "detail": "這段沒聽到話"}
        en = await asyncio.to_thread(translate, zh)
        event = {"type": "final", "id": uuid.uuid4().hex[:10], "zh": zh, "en": en, "t": int(time.time() * 1000)}
        state["history"].append(event)
        state["history"] = state["history"][-80:]
        await broadcast(state, event)
        return {"ok": True, **event}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    finally:
        src.unlink(missing_ok=True)
        if wav:
            wav.unlink(missing_ok=True)

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
