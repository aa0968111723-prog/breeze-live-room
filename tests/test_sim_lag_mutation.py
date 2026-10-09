"""Opt-in loop-lag mutation matrix. Default off: set BREEZE_SIM_MUTATION=1.

Each case is a fresh 100-minute class with Pipeline._process monkeypatched.
Must-red cells fail the stall gate; must-green cells pass it; verdict is
monotonic in stall length. Not collected on every push (~25 minutes).
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from tests.sim import (
    LOOP_LAG_MIN_N,
    LOOP_LAG_STALL_MS,
    _RUN_CACHE,
    format_loop_lag_line,
    mutation_tests_enabled,
    run_100min,
)


pytestmark = pytest.mark.skipif(
    not mutation_tests_enabled(),
    reason="opt-in: set BREEZE_SIM_MUTATION=1",
)

_WIN_TICK = 0.015625
_PER_SEGMENT_MS = (20, 30, 70, 150)
_SWEEP_MS = (10, 15, 20, 30, 70, 150)


def _n_per_ms() -> float:
    n = 2_000_000
    t0 = time.perf_counter()
    sum(range(n))
    dt = time.perf_counter() - t0
    if dt <= 0:
        return 100_000.0
    return n / (dt * 1000.0)


_NPERMS = None


def nperms() -> float:
    global _NPERMS
    if _NPERMS is None:
        _NPERMS = _n_per_ms()
    return _NPERMS


@contextmanager
def _win_event_tick():
    orig = threading.Event.wait

    def wait(self, timeout=None):
        if timeout is not None and timeout > 0:
            timeout = math.ceil(timeout / _WIN_TICK) * _WIN_TICK
        return orig(self, timeout)

    threading.Event.wait = wait
    try:
        yield
    finally:
        threading.Event.wait = orig


@contextmanager
def _busy_loops(count: int):
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", "while True: pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(count)
    ]
    try:
        yield
    finally:
        for proc in procs:
            proc.kill()
        for proc in procs:
            proc.wait(timeout=5)


@contextmanager
def _inject(kind: str | None, ms: float | None, seqs: set[int] | None):
    if kind is None:
        yield
        return
    from app.pipeline import Pipeline

    orig = Pipeline._process
    rate = nperms()

    async def wrapped(self, segment, audio, decoder, holder):
        if segment.session_id == "sim100" and (seqs is None or segment.seq in seqs):
            if kind == "sleep":
                time.sleep(float(ms) / 1000.0)
            elif kind == "pyspin":
                t0 = time.thread_time()
                while time.thread_time() - t0 < float(ms) / 1000.0:
                    pass
            elif kind == "cgil":
                sum(range(int(float(ms) * rate)))
            else:
                raise ValueError(kind)
        return await orig(self, segment, audio, decoder, holder)

    Pipeline._process = wrapped
    try:
        yield
    finally:
        Pipeline._process = orig


def _fresh_report(*, kind=None, ms=None, seqs=None, win=False, busy=0):
    _RUN_CACHE.clear()
    env_ga = os.environ.get("GITHUB_ACTIONS")
    os.environ["GITHUB_ACTIONS"] = "true"
    os.environ.pop("BREEZE_SIM_LAG_REPORT_ONLY", None)
    try:
        with _inject(kind, ms, seqs):
            with _win_event_tick() if win else _null():
                with _busy_loops(busy) if busy else _null():
                    report = run_100min()
    finally:
        _RUN_CACHE.clear()
        if env_ga is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = env_ga
    return report


@contextmanager
def _null():
    yield


def _line(report) -> str:
    return format_loop_lag_line(report)


def _red(report, line: str) -> None:
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert report.loop_lag_stall_ms > LOOP_LAG_STALL_MS, line


def _green(report, line: str, *, waiting: bool = True) -> None:
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert report.loop_lag_stall_ms <= LOOP_LAG_STALL_MS, line
    if waiting:
        assert report.waiting_v_total < 1, (report.waiting, line)


@pytest.mark.parametrize("kind", ["sleep", "pyspin", "cgil"])
@pytest.mark.parametrize("ms", _PER_SEGMENT_MS)
def test_per_segment_must_red(kind, ms, capsys):
    report = _fresh_report(kind=kind, ms=ms)
    line = _line(report)
    with capsys.disabled():
        print(f"mut {kind} {ms}ms/seg {line} waiting={report.waiting_v_total}", flush=True)
    _red(report, line)


def test_single_500ms_must_red(capsys):
    report = _fresh_report(kind="sleep", ms=500, seqs={500})
    line = _line(report)
    with capsys.disabled():
        print(f"mut single500 {line} waiting={report.waiting_v_total}", flush=True)
    _red(report, line)


def test_three_150ms_must_red(capsys):
    report = _fresh_report(kind="sleep", ms=150, seqs={200, 500, 800})
    line = _line(report)
    with capsys.disabled():
        print(f"mut 3x150 {line} waiting={report.waiting_v_total}", flush=True)
    _red(report, line)


def test_single_109ms_must_green(capsys):
    report = _fresh_report(kind="sleep", ms=109, seqs={500})
    line = _line(report)
    with capsys.disabled():
        print(f"mut single109 {line} waiting={report.waiting_v_total}", flush=True)
    _green(report, line)


def test_clean_linux_must_green(capsys):
    report = _fresh_report()
    line = _line(report)
    with capsys.disabled():
        print(f"mut cleanL {line} waiting={report.waiting_v_total}", flush=True)
    _green(report, line)


def test_clean_windows_sim_must_green(capsys):
    report = _fresh_report(win=True)
    line = _line(report)
    with capsys.disabled():
        print(f"mut cleanW {line} waiting={report.waiting_v_total} period={report.loop_lag_period_ms}", flush=True)
    _green(report, line)
    assert 15.0 <= report.loop_lag_period_ms <= 18.0, report.loop_lag_period_ms


@pytest.mark.parametrize("busy", [8, 16])
def test_busy_loops_must_green(busy, capsys):
    report = _fresh_report(busy=busy)
    line = _line(report)
    with capsys.disabled():
        print(f"mut busy{busy} {line} waiting={report.waiting_v_total}", flush=True)
    _green(report, line)
    assert report.waiting_v_total == 0, report.waiting


def test_inject_under_load_must_red(capsys):
    report = _fresh_report(kind="sleep", ms=20, busy=8)
    line = _line(report)
    with capsys.disabled():
        print(f"mut load+sleep20 {line} waiting={report.waiting_v_total}", flush=True)
    _red(report, line)


@pytest.mark.parametrize("kind", ["sleep", "pyspin", "cgil"])
def test_verdict_monotonic_in_ms(kind, capsys):
    seen_red = False
    rows = []
    for ms in _SWEEP_MS:
        report = _fresh_report(kind=kind, ms=ms)
        line = _line(report)
        red = report.loop_lag_stall_ms > LOOP_LAG_STALL_MS
        rows.append((ms, red, report.loop_lag_stall_ms))
        with capsys.disabled():
            print(f"mut sweep {kind} {ms}ms red={red} {line}", flush=True)
        if seen_red:
            assert red, (kind, rows)
        if red:
            seen_red = True
    assert seen_red, (kind, rows)
    assert rows[0][0] == 10
