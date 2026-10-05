from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class AudioError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def read_limited(chunks: list[bytes], limit: int) -> bytes:
    total = 0
    out = []
    for block in chunks:
        total += len(block)
        if total > limit:
            raise AudioError(413, f"音訊超過 {limit} bytes，已拒絕")
        out.append(block)
    if total == 0:
        raise AudioError(400, "沒有收到音訊")
    return b"".join(out)


def ffmpeg_bin(root: Path) -> str | None:
    local = root / "tools" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    found = shutil.which("ffmpeg")
    return found


def convert_to_wav(src: Path, work: Path, ffmpeg: str, timeout: int = 40) -> Path:
    wav = work / "audio.wav"
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0 or not wav.exists():
        raise AudioError(422, (proc.stderr or "ffmpeg 轉檔失敗")[-400:])
    return wav


def wav_duration_seconds(wav: Path) -> float | None:
    if wav.stat().st_size < 44:
        return 0.0
    # 16 kHz mono s16le: payload bytes / 32000
    payload = max(0, wav.stat().st_size - 44)
    return payload / 32000
