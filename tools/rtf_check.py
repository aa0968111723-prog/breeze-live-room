#!/usr/bin/env python3
"""Measure recognition real-time factor on this machine.

  python tools/rtf_check.py metrics --base http://127.0.0.1:8780
  python tools/rtf_check.py run --n 4 --audio sample.wav

PASS means this run's session RTF p95 is below 0.9. Device acceptance stays
尚未驗證 until someone runs this on the real host. The host token and the
transcript are never printed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.asr import CliAsr, ResidentAsr  # noqa: E402
from app.audio import ffmpeg_bin, riff_duration_seconds  # noqa: E402
from app.rtf import RtfMeter  # noqa: E402
from app.settings import Settings, fill_process_environ  # noqa: E402

P95_LIMIT = 0.9
UNVERIFIED = "實機結果尚未驗證。請在真正的主持機上執行後，才把數字當成那一台的測量。"
PROMPT = "以下是台灣國語的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"


def _open(url: str, token: str | None, timeout: float) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _redact(text: str, token: str | None) -> str:
    if not token or not text:
        return text
    return text.replace(token, "[redacted]")


def fetch_token(base: str) -> str:
    status, raw = _open(base + "/api/host-token", None, 5)
    if status == 403:
        raise SystemExit("拿不到主持權杖（403）。請在主持機本機對 127.0.0.1 執行，不要從別的裝置跑。")
    if status != 200:
        raise SystemExit(f"拿不到主持權杖（HTTP {status}）。請確認服務已在 {base} 啟動。")
    try:
        token = json.loads(raw.decode("utf-8")).get("token")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise SystemExit("主持權杖回應無法解讀。沒有印出內容。")
    return token


def fetch_metrics(base: str, token: str) -> dict:
    status, raw = _open(base + "/api/metrics", token, 10)
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        raise SystemExit(_redact(f"GET /api/metrics 失敗（HTTP {status}）", token))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(_redact(f"GET /api/metrics 不是 JSON：{exc}", token)) from exc
    if not isinstance(payload, dict):
        raise SystemExit("GET /api/metrics 不是物件。")
    return payload


def build_asr():
    """The configured local recognizer. Missing pieces do not fall through to a cloud API."""
    fill_process_environ(ROOT / ".env")
    settings = Settings.from_env()
    model = Path(settings.model_path) if settings.model_path else ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
    whisper = Path(settings.whisper_path) if settings.whisper_path else ROOT / "tools" / "whisper-cli.exe"
    server = Path(settings.server_path) if settings.server_path else ROOT / "tools" / "whisper-server.exe"
    if not model.is_file():
        raise SystemExit("缺少 Breeze 模型，請先執行 install.bat。不會改走雲端辨識。")
    if settings.asr_mode == "resident":
        asr = ResidentAsr(
            settings.resident_url,
            server_bin=server,
            model=model,
            threads=settings.asr_threads,
            startup_timeout_s=settings.resident_startup_s,
            inference_timeout_s=settings.asr_timeout_s,
            audio_context=settings.asr_audio_context,
            beam_size=settings.asr_beam_size,
            best_of=settings.asr_best_of,
        )
        started = asr.start()
        if not started.ok:
            asr.close()
            raise SystemExit((started.error or "常駐辨識沒有就緒") + "。不會改走雲端辨識。")
        return asr
    if not whisper.is_file():
        raise SystemExit("缺少 whisper-cli。請先執行 install.bat。不會改走雲端辨識。")
    return CliAsr(
        whisper,
        model,
        threads=settings.asr_threads,
        timeout_s=settings.asr_timeout_s,
        audio_context=settings.asr_audio_context,
        beam_size=settings.asr_beam_size,
        best_of=settings.asr_best_of,
    )


def _decode_wav(path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    duration = riff_duration_seconds(path)
    if duration and duration > 0:
        return path, None
    ffmpeg = ffmpeg_bin(ROOT)
    if not ffmpeg:
        raise SystemExit("這不是 WAV，也找不到 ffmpeg，所以沒有可靠的音訊長度。不會用檔案大小去猜。")
    temporary = tempfile.TemporaryDirectory(prefix="breeze-rtf-")
    dest = Path(temporary.name) / "slice.wav"
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        temporary.cleanup()
        raise SystemExit("ffmpeg 轉檔逾時，沒有量到音訊長度。")
    if proc.returncode != 0 or not (riff_duration_seconds(dest) or 0) > 0:
        temporary.cleanup()
        raise SystemExit("無法解出這段音訊的長度。不會用檔案大小去猜。")
    return dest, temporary


def measure_slices(transcribe, wav: Path, n: int) -> list[tuple[float, float]]:
    """Time n recognition calls. Each row is (asr_seconds, audio_seconds) from the WAVE duration."""
    audio_s = riff_duration_seconds(wav)
    if not audio_s or audio_s <= 0:
        raise SystemExit("讀不到這段 WAV 的長度。")
    samples: list[tuple[float, float]] = []
    for index in range(n):
        started = time.monotonic()
        result = transcribe(wav, PROMPT)
        elapsed = time.monotonic() - started
        if not getattr(result, "ok", False):
            detail = getattr(result, "error", "") or "辨識失敗"
            raise SystemExit(f"第 {index + 1} 段辨識失敗：{str(detail)[:180]}")
        samples.append((elapsed, float(audio_s)))
    return samples


def snapshot_from_pairs(pairs: list[tuple[float, float]]) -> dict:
    meter = RtfMeter()
    for asr_s, audio_s in pairs:
        meter.record(asr_s, audio_s, session=("rtf-check", "run"))
    return meter.snapshot()


def _fmt_ms(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _fmt_rtf(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.3f}"


def _scope_lines(title: str, scope: dict) -> list[str]:
    count = int(scope.get("count") or 0)
    limit = int(scope.get("limit") or 0)
    lines = [f"{title} {count}／{limit} 段"]
    for label, key in (("辨識毫秒", "asr_ms"), ("音訊毫秒", "audio_ms")):
        block = scope.get(key) or {}
        lines.append(
            f"  {label} p50 {_fmt_ms(block.get('p50'))}  p95 {_fmt_ms(block.get('p95'))}  最大 {_fmt_ms(block.get('max'))}"
        )
    rtf = scope.get("rtf") or {}
    lines.append(
        f"  RTF p50 {_fmt_rtf(rtf.get('p50'))}  p95 {_fmt_rtf(rtf.get('p95'))}  最大 {_fmt_rtf(rtf.get('max'))}"
    )
    return lines


def render(snapshot: dict, source: str) -> tuple[str, int]:
    """Text report and process exit code. p95 < 0.9 passes; a missing sample fails."""
    rtf = snapshot.get("rtf") if isinstance(snapshot, dict) else None
    if not isinstance(rtf, dict) or not isinstance(rtf.get("session"), dict):
        text = "這份結果沒有 rtf。請更新到會回報辨識即時率的版本。\n" + UNVERIFIED
        return text, 1
    session = rtf["session"]
    window = rtf.get("window") if isinstance(rtf.get("window"), dict) else {"count": 0, "limit": 0}
    p95 = (session.get("rtf") or {}).get("p95")
    lines = [f"來源：{source}"]
    backlog = snapshot.get("backlog_audio_s")
    if backlog is not None:
        lines.append(f"等待辨識的音訊：{float(backlog):.3f} 秒")
    lines.extend(_scope_lines("近期", window))
    lines.extend(_scope_lines("本場以來", session))
    if p95 is None:
        lines.append(f"結果：FAIL（沒有 RTF 樣本，門檻 p95 < {P95_LIMIT}）")
        code = 1
    elif float(p95) < P95_LIMIT:
        lines.append(f"結果：PASS（本場 RTF p95 {_fmt_rtf(p95)} < {P95_LIMIT}）")
        code = 0
    else:
        lines.append(f"結果：FAIL（本場 RTF p95 {_fmt_rtf(p95)} >= {P95_LIMIT}）")
        code = 1
    lines.append(UNVERIFIED)
    return "\n".join(lines), code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量這台電腦的辨識即時率（RTF）。不會印出主持權杖或逐字稿。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    metrics = sub.add_parser("metrics", help="讀正在跑的服務的 /api/metrics")
    metrics.add_argument("--base", default="http://127.0.0.1:8780")
    run = sub.add_parser("run", help="用目前的本機辨識設定跑 N 段樣本")
    run.add_argument("--n", type=int, default=4)
    run.add_argument("--audio", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "metrics":
        base = str(args.base).rstrip("/")
        token = fetch_token(base)
        payload = fetch_metrics(base, token)
        text, code = render(payload, source=base + "/api/metrics")
        print(text)
        return code
    if not 1 <= args.n <= 30:
        parser.error("段數須在 1 到 30")
    audio = Path(args.audio)
    if not audio.is_file():
        raise SystemExit("找不到音訊檔。")
    wav, temporary = _decode_wav(audio)
    asr = None
    try:
        asr = build_asr()
        pairs = measure_slices(asr.transcribe, wav, args.n)
    finally:
        if temporary is not None:
            temporary.cleanup()
        close = getattr(asr, "close", None)
        if close is not None:
            close()
    text, code = render(snapshot_from_pairs(pairs), source=f"本機辨識 {args.n} 段 {audio.name}")
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
