"""The device RTF tool: fake recognizer, and /api/metrics on a local test server."""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.asr import AsrResult
from app.server import create_app
from app.settings import Settings
from app.translate import Translator
from tests.test_rtf_metrics import copy_decoder, wave_bytes

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rtf_check", ROOT / "tools" / "rtf_check.py")
assert _spec is not None and _spec.loader is not None
rtf_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtf_check)


def test_render_pass_fail_and_unverified_note():
    slow, slow_code = rtf_check.render(rtf_check.snapshot_from_pairs([(0.9, 1.0), (1.2, 1.0)]), "假樣本")
    assert slow_code == 1
    assert "FAIL" in slow
    assert "尚未驗證" in slow
    assert "p95" in slow
    fast, fast_code = rtf_check.render(rtf_check.snapshot_from_pairs([(0.2, 1.0)]), "假樣本")
    assert fast_code == 0
    assert "PASS" in fast
    assert "0.200" in fast
    assert "尚未驗證" in fast
    empty, empty_code = rtf_check.render(rtf_check.snapshot_from_pairs([]), "假樣本")
    assert empty_code == 1
    assert "沒有 RTF 樣本" in empty


def test_run_with_fake_asr_prints_pass_and_hides_transcript(monkeypatch, capsys, tmp_path):
    wav = tmp_path / "slice.wav"
    wav.write_bytes(wave_bytes(1.0))
    seen = {}

    class Fake:
        def transcribe(self, path, prompt):
            del path, prompt
            time.sleep(0.05)
            return AsrResult(ok=True, text="不要印出這句逐字稿XYZ")

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(rtf_check, "build_asr", lambda: Fake())
    code = rtf_check.main(["run", "--n", "3", "--audio", str(wav)])
    captured = capsys.readouterr()
    assert code == 0
    assert "PASS" in captured.out
    assert "p95" in captured.out
    assert "尚未驗證" in captured.out
    assert "XYZ" not in captured.out and "XYZ" not in captured.err
    assert seen.get("closed") is True


def test_run_refuses_a_missing_model_without_cloud(monkeypatch, tmp_path):
    monkeypatch.setenv("BREEZE_MODEL", str(tmp_path / "missing.bin"))
    monkeypatch.setenv("BREEZE_ASR", "cli")
    wav = tmp_path / "slice.wav"
    wav.write_bytes(wave_bytes(0.5))
    with pytest.raises(SystemExit) as exc:
        rtf_check.main(["run", "--n", "1", "--audio", str(wav)])
    assert "不會改走雲端" in str(exc.value)


def test_run_does_not_guess_duration_from_a_non_wave_file(monkeypatch, tmp_path):
    bogus = tmp_path / "clip.bin"
    bogus.write_bytes(b"not-a-wave" * 4000)

    class Boom:
        def transcribe(self, path, prompt):
            del path, prompt
            raise AssertionError("非 WAV 不該在沒有可靠時長時送去辨識")

        def close(self):
            return None

    monkeypatch.setattr(rtf_check, "build_asr", lambda: Boom())
    with pytest.raises(SystemExit) as exc:
        rtf_check.main(["run", "--n", "1", "--audio", str(bogus)])
    message = str(exc.value)
    assert "不會用檔案大小去猜" in message or "無法解出" in message


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_metrics_command_against_test_server(capsys):
    port = _free_port()
    app = create_app(
        Settings(port=port, allow_testclient=True, translate=False, max_audio_bytes=2_000_000),
        asr=object(),
        translator=Translator(enabled=False, key=""),
        decoder=copy_decoder,
    )
    app.state.pipeline._rtf.record(0.3, 1.0, ("class", "live"))
    app.state.pipeline._rtf.record(0.5, 1.0, ("class", "live"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 5
        up = False
        while time.monotonic() < deadline:
            try:
                status, _raw = rtf_check._open(base + "/api/health", None, 0.5)
            except OSError:
                status = 0
            if status in {200, 503}:
                up = True
                break
            time.sleep(0.05)
        assert up, "test server did not start"
        code = rtf_check.main(["metrics", "--base", base])
        captured = capsys.readouterr()
        assert code == 0, captured.out + captured.err
        assert "PASS" in captured.out
        assert "p50 0.400" in captured.out
        assert "p95 0.490" in captured.out
        assert "尚未驗證" in captured.out
        assert "等待辨識的音訊：0.000 秒" in captured.out
        assert app.state.token not in captured.out
        assert app.state.token not in captured.err
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(3)
