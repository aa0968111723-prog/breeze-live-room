from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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
        proc = run(cmd, capture_output=True, text=True, timeout=120)
        text = " ".join(line.strip() for line in (proc.stdout or "").splitlines() if line.strip())
        if proc.returncode != 0:
            err = (proc.stderr or "")[-400:] or "Breeze 辨識失敗"
            return AsrResult(ok=False, text=text, error=err, loaded_once=False)
        return AsrResult(ok=True, text=text, loaded_once=False)


class ResidentAsr:
    """只在本機 127.0.0.1 的 whisper-server 可用時啟用。未連上就不假裝已載入。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8178"):
        self.base_url = base_url.rstrip("/")
        self.calls = 0
        self.loads = 0
        self.ready = False

    def health(self) -> bool:
        if not self.base_url.startswith("http://127.0.0.1"):
            return False
        return self.ready

    def mark_ready_for_test(self) -> None:
        self.ready = True
        self.loads = 1

    def transcribe(self, wav: Path, prompt: str) -> AsrResult:
        if not self.health():
            return AsrResult(ok=False, error="常駐辨識還沒就緒，沒有改走雲端。")
        self.calls += 1
        return AsrResult(ok=True, text="", loaded_once=True)
