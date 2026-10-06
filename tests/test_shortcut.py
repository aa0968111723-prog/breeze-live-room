"""Desktop shortcut install: non-ASCII TEMP and required-shortcut failures."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

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
POWERSHELL_MISSING_MESSAGE = "無法執行 powershell.exe。"
LINK_NAMES = ("Breeze Live Room", "Breeze Update", "Breeze Doctor")
BAT_NAMES = ("start.bat", "update.bat", "doctor.bat")


def _runner(returncode):
    calls = []

    def runner(args, cwd=None, **kwargs):
        calls.append((args, cwd, kwargs))
        return subprocess.CompletedProcess(args, returncode)

    return calls, runner


def _shortcut_text() -> str:
    return (ROOT / "install-shortcut.ps1").read_text(encoding="utf-8-sig")


def _detail(proc: subprocess.CompletedProcess) -> str:
    return f"returncode={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def _env_without(env: dict, *names: str) -> dict:
    blocked = {name.upper() for name in names}
    return {key: value for key, value in env.items() if key.upper() not in blocked}


def _powershell_pipe_text(value: str) -> str:
    # powershell.exe 5.1 writes UTF-16 LE into a pipe. Decoded as UTF-8, ASCII
    # text keeps NUL bytes between characters; drop them so assertions can read it.
    if value and "\x00" in value:
        return value.replace("\x00", "")
    return value


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


@pytest.mark.parametrize("exc_type", [FileNotFoundError, OSError])
def test_install_shortcut_missing_powershell_is_nonfatal(monkeypatch, tmp_path, capsys, exc_type):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)

    def runner(*args, **kwargs):
        raise exc_type("powershell")

    assert install_shortcut(tmp_path, runner=runner) == 0
    captured = capsys.readouterr()
    assert POWERSHELL_MISSING_MESSAGE in captured.out
    assert FAILURE_MESSAGE in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("exc_type", [FileNotFoundError, OSError])
def test_install_shortcut_missing_powershell_is_fatal_when_required(monkeypatch, tmp_path, capsys, exc_type):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")

    def runner(*args, **kwargs):
        raise exc_type("powershell")

    assert install_shortcut(tmp_path, runner=runner) == 1
    captured = capsys.readouterr()
    assert POWERSHELL_MISSING_MESSAGE in captured.out
    assert FAILURE_MESSAGE in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_main_delegates_shortcut_install():
    source = (ROOT / "scripts" / "install_runtime.py").read_text(encoding="utf-8")
    assert "def install_shortcut(root, runner=subprocess.run)" in source
    assert "install_shortcut(ROOT)" in source


def test_install_shortcut_script_is_utf8_bom_lf():
    raw = (ROOT / "install-shortcut.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install-shortcut.ps1 must start with a UTF-8 BOM"
    assert b"\r" not in raw


def test_desktop_path_param_defaults_to_known_folder_when_empty():
    text = _shortcut_text()
    assert re.search(r"\[string\]\s*\$DesktopPath", text)
    assert text.count("GetFolderPath('Desktop')") == 1
    empty_at = text.index("IsNullOrEmpty($DesktopPath)")
    folder_at = text.index("GetFolderPath('Desktop')", empty_at)
    else_at = text.index("else", folder_at)
    given_at = text.index("$DesktopPath", else_at)
    assert empty_at < folder_at < else_at < given_at


def test_non_ascii_temp_guard_runs_before_add_type_and_temp_is_restored():
    text = _shortcut_text()
    add_at = text.index("Add-Type")
    before = text[:add_at]
    assert "Test-BreezeNonAsciiText" in before
    assert "$env:TEMP" in before
    assert "$env:TMP" in before
    here_end = text.index("'@", add_at)
    tail = text[here_end:]
    finally_at = tail.index("finally")
    restored = tail[finally_at:]
    assert re.search(r"\$env:TEMP\s*=", restored)
    assert re.search(r"\$env:TMP\s*=", restored)


def test_ascii_temp_candidates_use_programdata_public_and_systemdrive():
    text = _shortcut_text()
    for token in ("$env:ProgramData", "$env:PUBLIC", "$env:SystemDrive", r"BreezeLiveRoom\tmp", "BreezeLiveRoomTmp"):
        assert token in text


def test_ascii_fallback_is_announced_and_cleaned_up_after_temp_restore():
    text = _shortcut_text()
    assert "Using ASCII TEMP for shortcut helper:" in text
    here_end = text.index("'@", text.index("Add-Type"))
    restored = text[here_end:]
    finally_at = restored.index("finally")
    tail = restored[finally_at:]
    cleanup_at = tail.index("Remove-BreezeCreatedDirectory")
    assert tail.index("$env:TEMP") < cleanup_at
    assert tail.index("$env:TMP") < cleanup_at
    assert "Get-BreezeTopmostMissingAncestor" in text


def test_checkonly_exit_codes_for_missing_and_wrong_target():
    text = _shortcut_text()
    missing = text.index("Shortcut not found:")
    wrong = text.index("Shortcut target or directory is incorrect")
    assert text.index("$CheckOnly") < missing
    assert "exit 2" in text[missing:missing + 180]
    assert "exit 1" in text[wrong:wrong + 220]


def test_shortcut_names_and_targets():
    text = _shortcut_text()
    for name in (*LINK_NAMES, *BAT_NAMES):
        assert name in text


def test_shortcut_script_does_not_use_wscript_shell():
    text = _shortcut_text()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert "WScript.Shell" not in code
    assert "IBreezeShellLinkW" in text


def _assert_links(desktop, detail: str) -> None:
    found = sorted(path.name for path in desktop.glob("*.lnk"))
    expected = sorted(f"{name}.lnk" for name in LINK_NAMES)
    assert found == expected, detail


@pytest.fixture
def shortcut_workspace(tmp_path):
    """Install dir, desktop, and non-ASCII TEMP/TMP for Windows shortcut runs."""
    install_dir = tmp_path / "Breeze test 測試"
    install_dir.mkdir()
    shutil.copyfile(ROOT / "install-shortcut.ps1", install_dir / "install-shortcut.ps1")
    for name in BAT_NAMES:
        (install_dir / name).write_bytes(b"@echo off\r\n")
    desktop = tmp_path / "桌面 測試"
    desktop.mkdir()
    temp_dir = tmp_path / "tmp 測試"
    temp_dir.mkdir()
    base_env = _env_without(os.environ.copy(), "TEMP", "TMP")
    base_env["TEMP"] = str(temp_dir)
    base_env["TMP"] = str(temp_dir)

    def child_env(**overrides):
        env = dict(base_env)
        for name, value in overrides.items():
            env = _env_without(env, name)
            env[name] = str(value)
        return env

    def run(*args, env=None, cwd=None):
        work = install_dir if cwd is None else cwd
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(work / "install-shortcut.ps1"),
                "-DesktopPath",
                str(desktop),
                *args,
            ],
            cwd=str(work),
            env=base_env if env is None else env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        proc.stdout = _powershell_pipe_text(proc.stdout or "")
        proc.stderr = _powershell_pipe_text(proc.stderr or "")
        return proc

    return SimpleNamespace(
        tmp_path=tmp_path,
        install_dir=install_dir,
        desktop=desktop,
        temp_dir=temp_dir,
        base_env=base_env,
        child_env=child_env,
        run=run,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_created_when_temp_is_non_ascii(shortcut_workspace):
    """Add-Type must still create shortcuts when TEMP itself is a non-ASCII path."""
    ws = shortcut_workspace
    created = ws.run()
    detail = _detail(created)
    assert created.returncode == 0, detail
    for name in LINK_NAMES:
        assert (ws.desktop / f"{name}.lnk").is_file(), f"missing {name}\n{detail}"
    checked = ws.run("-CheckOnly")
    detail = _detail(checked)
    assert checked.returncode == 0, detail
    (ws.desktop / "Breeze Update.lnk").unlink()
    missing = ws.run("-CheckOnly")
    detail = _detail(missing)
    assert missing.returncode == 2, detail


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_checkonly_fails_when_install_dir_moved(shortcut_workspace):
    ws = shortcut_workspace
    created = ws.run()
    assert created.returncode == 0, _detail(created)
    moved = ws.tmp_path / "Breeze moved 搬移"
    ws.install_dir.rename(moved)
    checked = ws.run("-CheckOnly", cwd=moved)
    detail = _detail(checked)
    assert checked.returncode == 1, detail
    assert "Shortcut target or directory is incorrect" in checked.stdout, detail


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_create_is_idempotent(shortcut_workspace):
    ws = shortcut_workspace
    first = ws.run()
    second = ws.run()
    assert first.returncode == 0, _detail(first)
    assert second.returncode == 0, _detail(second)
    _assert_links(ws.desktop, _detail(second))
    checked = ws.run("-CheckOnly")
    assert checked.returncode == 0, _detail(checked)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_ascii_fallback_uses_public_and_removes_created_dir(shortcut_workspace):
    ws = shortcut_workspace
    program_data = ws.tmp_path / "程式資料"
    public = ws.tmp_path / "public_ascii"
    program_data.mkdir()
    public.mkdir()
    proc = ws.run(env=ws.child_env(ProgramData=program_data, PUBLIC=public))
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    _assert_links(ws.desktop, detail)
    assert os.path.join("public_ascii", "BreezeLiveRoom", "tmp") in proc.stdout, detail
    assert not (public / "BreezeLiveRoom").exists(), detail


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_ascii_fallback_uses_systemdrive_and_removes_created_dir(shortcut_workspace):
    # SystemDrive is overridden only in the child environment. PATH and SystemRoot
    # stay inherited, and powershell.exe is started with -NoProfile, so pointing
    # SystemDrive at a directory (not a drive letter) does not affect host startup.
    # The script joins BreezeLiveRoomTmp onto that directory.
    ws = shortcut_workspace
    program_data = ws.tmp_path / "程式資料"
    public = ws.tmp_path / "公共資料"
    system_drive = ws.tmp_path / "systemdrive_ascii"
    program_data.mkdir()
    public.mkdir()
    system_drive.mkdir()
    proc = ws.run(env=ws.child_env(ProgramData=program_data, PUBLIC=public, SystemDrive=system_drive))
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    _assert_links(ws.desktop, detail)
    created = system_drive / "BreezeLiveRoomTmp"
    assert os.path.join("systemdrive_ascii", "BreezeLiveRoomTmp") in proc.stdout, detail
    assert not created.exists(), detail


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_does_not_delete_preexisting_ascii_temp(shortcut_workspace):
    ws = shortcut_workspace
    program_data = ws.tmp_path / "程式資料"
    public = ws.tmp_path / "public_ascii"
    program_data.mkdir()
    target = public / "BreezeLiveRoom" / "tmp"
    target.mkdir(parents=True)
    marker = target / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    proc = ws.run(env=ws.child_env(ProgramData=program_data, PUBLIC=public))
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    _assert_links(ws.desktop, detail)
    assert os.path.join("public_ascii", "BreezeLiveRoom", "tmp") in proc.stdout, detail
    assert marker.is_file(), detail
    assert marker.read_text(encoding="utf-8") == "keep", detail


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell")
def test_shortcut_fails_when_no_ascii_temp_candidate(shortcut_workspace):
    ws = shortcut_workspace
    program_data = ws.tmp_path / "程式資料"
    public = ws.tmp_path / "公共資料"
    system_drive = ws.tmp_path / "系統碟"
    for path in (program_data, public, system_drive):
        path.mkdir()
    proc = ws.run(env=ws.child_env(ProgramData=program_data, PUBLIC=public, SystemDrive=system_drive))
    detail = _detail(proc)
    assert proc.returncode != 0, detail
    assert "No ASCII-only writable directory" in (proc.stdout + proc.stderr), detail
    assert list(ws.desktop.glob("*.lnk")) == [], detail
