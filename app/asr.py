from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


@dataclass
class AsrResult:
    ok: bool
    text: str = ""
    error: str = ""
    loaded_once: bool = False


class CliAsr:
    """每次呼叫都啟動一次 whisper-cli。這不是常駐模型。"""

    def __init__(self, whisper: Path, model: Path, runner: Callable | None = None, threads: int = 6):
        self.whisper = whisper
        self.model = model
        self.runner = runner
        self.threads = threads
        self.calls = 0

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        self.calls += 1
        if not self.whisper.exists():
            return AsrResult(ok=False, error="找不到 tools/whisper-cli.exe")
        if not self.model.exists():
            return AsrResult(ok=False, error="找不到 Breeze 模型。請先安裝，不要改走雲端辨識。")
        import subprocess

        cmd = [
            str(self.whisper), "-m", str(self.model), "-f", str(wav),
            "-l", "zh", "-np", "-nt", "-t", str(self.threads), "--prompt", prompt,
        ]
        run = self.runner or subprocess.run
        try:
            proc = run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return AsrResult(ok=False, error="whisper-cli 逾時", loaded_once=False)
        text = " ".join(line.strip() for line in (proc.stdout or "").splitlines() if line.strip())
        if proc.returncode != 0:
            err = (proc.stderr or "")[-400:] or "Breeze 辨識失敗"
            return AsrResult(ok=False, text=text, error=err, loaded_once=False)
        return AsrResult(ok=True, text=text, loaded_once=False)


def require_loopback_url(base_url: str) -> str:
    """Accept only a fully parsed loopback URL. A string prefix is not enough."""
    try:
        parts = urlsplit((base_url or "").strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise ValueError("常駐辨識網址無法解析") from exc
    if parts.scheme not in {"http", "https"}:
        raise ValueError("常駐辨識網址必須是 http 或 https")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("常駐辨識只允許 loopback，拒絕 " + (host or "空位址"))
    if parts.username is not None or parts.password is not None:
        raise ValueError("常駐辨識網址不能帶帳號")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("常駐辨識網址的埠不正確")
    return base_url.strip()


class ResidentAsr:
    """Local whisper-server adapter. A ready flag without a transport or process is not inference."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8178",
        transport: Callable | None = None,
        server_bin: Path | None = None,
        model: Path | None = None,
        popen: Callable | None = None,
        threads: int = 6,
    ):
        self.base_url = require_loopback_url(base_url)
        self.transport = transport
        self.server_bin = Path(server_bin) if server_bin else None
        self.model = Path(model) if model else None
        self.popen = popen
        self.threads = threads
        self.calls = 0
        self.loads = 0
        self.restarts = 0
        self.max_restarts = 2
        self.ready = False
        self.proc = None
        self.last_error = ""

    def health(self) -> bool:
        if self.transport is not None and self.ready and self.loads >= 1:
            return True
        if self.proc is not None and self.proc.poll() is None and self.ready:
            return True
        return False

    def mark_ready_for_test(self) -> None:
        """Old shortcut. It does not load a model and must not produce caption text."""
        self.ready = True

    def start(self) -> AsrResult:
        if self.transport is not None and self.server_bin is None:
            self.loads = 1
            self.ready = True
            self.last_error = ""
            return AsrResult(ok=True, loaded_once=True)
        if self.server_bin is None or not self.server_bin.exists():
            self.ready = False
            self.last_error = "找不到 whisper-server，沒有改走雲端。"
            return AsrResult(ok=False, error=self.last_error)
        if self.model is None or not self.model.exists():
            self.ready = False
            self.last_error = "找不到 Breeze 模型。請先安裝，不要改走雲端辨識。"
            return AsrResult(ok=False, error=self.last_error)
        parts = urlsplit(self.base_url)
        host = parts.hostname or "127.0.0.1"
        port = str(parts.port or 8178)
        cmd = [
            str(self.server_bin), "-m", str(self.model), "--host", host, "--port", port,
            "-l", "zh", "-t", str(self.threads),
        ]
        import subprocess
        opener = self.popen or subprocess.Popen
        try:
            # DEVNULL, not PIPE: an unread stderr pipe fills and stalls the server.
            self.proc = opener(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.ready = False
            self.last_error = f"whisper-server 無法啟動：{exc}"[:180]
            return AsrResult(ok=False, error=self.last_error)
        poll = getattr(self.proc, "poll", None)
        if self.proc is None or (callable(poll) and poll() is not None):
            self.proc = None
            self.ready = False
            self.last_error = "whisper-server 啟動後立刻結束，沒有改走雲端。"
            return AsrResult(ok=False, error=self.last_error)
        self.loads = 1
        self.ready = True
        self.last_error = ""
        return AsrResult(ok=True, loaded_once=True)

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        if self.ready and self.transport is None and self.proc is None:
            return AsrResult(ok=False, error="測試就緒標記不能代替推論。常駐服務沒有載入模型。", loaded_once=False)
        if not self.health():
            if self.restarts < self.max_restarts and (self.server_bin or self.transport):
                self.restarts += 1
                started = self.start()
                if not started.ok:
                    return AsrResult(ok=False, error=started.error or "常駐辨識還沒就緒，沒有改走雲端。")
            else:
                return AsrResult(ok=False, error=self.last_error or "常駐辨識還沒就緒，沒有改走雲端。")
        self.calls += 1
        try:
            if self.transport is not None:
                text = self.transport(wav, prompt)
            else:
                text = self._http_inference(wav, prompt)
        except Exception as exc:
            self.last_error = str(exc)[:180]
            return AsrResult(ok=False, error=self.last_error or "常駐辨識失敗", loaded_once=self.loads == 1)
        cleaned = str(text or "").strip()
        if not cleaned:
            return AsrResult(ok=False, error="常駐辨識沒有回傳文字", loaded_once=self.loads == 1)
        return AsrResult(ok=True, text=cleaned, loaded_once=self.loads == 1)

    def _http_inference(self, wav: Path, prompt: str) -> str:
        import json
        import secrets
        import urllib.request
        boundary = "breeze" + secrets.token_hex(16)
        audio = wav.read_bytes()
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n{prompt}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n"
            ).encode() + audio + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/inference",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return str(data.get("text") or "")

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        self.ready = False
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
