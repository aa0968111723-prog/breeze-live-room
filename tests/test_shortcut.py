"""Desktop shortcut install: non-ASCII TEMP and required-shortcut failures."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

from scripts.install_runtime import ROOT, install_shortcut

SHORTCUT_ARGS = [
    "powershell",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    ".\\install-shortcut.ps1",
]
FAILURE_MESSAGE = "桌面捷徑未建立，仍可雙擊 start.bat。"


def _runner(returncode):
    calls = []

    def runner(args, cwd=None, **kwargs):
        calls.append((args, cwd, kwargs))
        return subprocess.CompletedProcess(args, returncode)

    return calls, runner


def test_install_shortcut_success_invokes_powershell(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    calls, runner = _runner(0)
    assert install_shortcut(tmp_path, runner=runner) == 0
    assert len(calls) == 1
    args, cwd, kwargs = calls[0]
    assert args == SHORTCUT_ARGS
    assert args[0] == "powershell"
    assert args[args.index("-File") + 1] == ".\\install-shortcut.ps1"
    assert cwd == tmp_path
    assert kwargs == {}
    assert FAILURE_MESSAGE not in capsys.readouterr().out


def test_install_shortcut_failure_is_fatal_when_required(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")
    calls, runner = _runner(3)
    assert install_shortcut(tmp_path, runner=runner) != 0
    assert calls[0][0] == SHORTCUT_ARGS
    assert calls[0][1] == tmp_path
    assert FAILURE_MESSAGE in capsys.readouterr().out


def test_install_shortcut_failure_without_require_is_nonfatal(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    calls, runner = _runner(1)
    assert install_shortcut(tmp_path, runner=runner) == 0
    assert calls[0][0] == SHORTCUT_ARGS
    assert calls[0][1] == tmp_path
    assert FAILURE_MESSAGE in capsys.readouterr().out


def test_install_shortcut_skip_does_not_call_runner(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BREEZE_SKIP_SHORTCUT", "1")
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")

    def runner(*args, **kwargs):
        raise AssertionError("runner should not be called when BREEZE_SKIP_SHORTCUT=1")

    assert install_shortcut(tmp_path, runner=runner) == 0
    assert capsys.readouterr().out == ""


def test_main_delegates_shortcut_install():
    source = (ROOT / "scripts" / "install_runtime.py").read_text(encoding="utf-8")
    assert "def install_shortcut(root, runner=subprocess.run)" in source
    assert "install_shortcut(ROOT)" in source


def test_install_shortcut_script_guards_non_ascii_temp_and_desktop_path():
    raw = (ROOT / "install-shortcut.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install-shortcut.ps1 must start with a UTF-8 BOM"
    text = raw.decode("utf-8-sig")
    assert re.search(r"param\s*\(.*\[string\]\s*\$DesktopPath", text, re.DOTALL)
    assert "GetFolderPath('Desktop')" in text
    add_at = text.find("Add-Type")
    assert add_at > 0
    before = text[:add_at]
    assert "127" in before
    assert re.search(r"\$env:TEMP\b[\s\S]{0,80}-or[\s\S]{0,80}\$env:TMP\b", before)
    assert re.search(r"\$env:TEMP\s*=", before)
    assert re.search(r"\$env:TMP\s*=", before)
    assert before.index("ProgramData") < before.index("PUBLIC") < before.index("SystemDrive")
    assert before.index("BreezeLiveRoom\\tmp") < before.index("BreezeLiveRoomTmp")
    saved_temp = re.search(r"(\$\w+)\s*=\s*\$env:TEMP\b", before)
    saved_tmp = re.search(r"(\$\w+)\s*=\s*\$env:TMP\b", before)
    assert saved_temp and saved_tmp
    assert saved_temp.group(1) != "$env:TEMP"
    restored = False
    for match in re.finditer(r"finally\s*\{([^}]*)\}", text):
        body = match.group(1)
        if "$env:TEMP" in body and "$env:TMP" in body:
            assert match.start() > add_at
            assert f"$env:TEMP = {saved_temp.group(1)}" in body
            assert f"$env:TMP = {saved_tmp.group(1)}" in body
            restored = True
            break
    assert restored, "TEMP and TMP must be restored in a finally block after Add-Type"
    missing = text.index("Shortcut not found:")
    assert "$CheckOnly" in text[:missing]
    assert "exit 2" in text[missing:missing + 180]
    wrong = text.index("Shortcut target or directory is incorrect:")
    assert "exit 1" in text[wrong:wrong + 180]
    for name in ("Breeze Live Room", "Breeze Update", "Breeze Doctor", "start.bat", "update.bat", "doctor.bat"):
        assert name in text
    assert "IBreezeShellLinkW" in text
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert "WScript.Shell" not in code


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_created_when_temp_is_non_ascii(tmp_path):
    """Add-Type must still create shortcuts when TEMP itself is a non-ASCII path."""
    install_dir = tmp_path / "Breeze test 測試"
    install_dir.mkdir()
    script = install_dir / "install-shortcut.ps1"
    shutil.copyfile(ROOT / "install-shortcut.ps1", script)
    for name in ("start.bat", "update.bat", "doctor.bat"):
        (install_dir / name).write_bytes(b"@echo off\r\n")
    desktop = tmp_path / "桌面 測試"
    desktop.mkdir()
    temp_dir = tmp_path / "tmp 測試"
    temp_dir.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.upper() in {"TEMP", "TMP"}:
            del env[key]
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)

    def run(*extra):
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-DesktopPath",
                str(desktop),
                *extra,
            ],
            cwd=install_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    created = run()
    detail = f"stdout:\n{created.stdout}\nstderr:\n{created.stderr}"
    assert created.returncode == 0, detail
    links = [desktop / f"{name}.lnk" for name in ("Breeze Live Room", "Breeze Update", "Breeze Doctor")]
    for link in links:
        assert link.is_file(), f"missing {link}\n{detail}"
    checked = run("-CheckOnly")
    detail = f"stdout:\n{checked.stdout}\nstderr:\n{checked.stderr}"
    assert checked.returncode == 0, detail
    links[1].unlink()
    missing = run("-CheckOnly")
    detail = f"stdout:\n{missing.stdout}\nstderr:\n{missing.stderr}"
    assert missing.returncode == 2, detail
