"""Opt-in loop-lag mutation matrix. Default off: set BREEZE_SIM_MUTATION=1.

Each case is a fresh class with Pipeline._process monkeypatched, or with ASR
parks split. Must-red cells fail a stall or waiting gate; must-green cells
pass; the per-segment verdict is monotonic in stall length. Not collected on
every push. Windows cases use either a flat 15.625 ms tick or the jittered
J model (uniform 0–6.5 ms plus a 3% extra tick, with mono/resolution/select).
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

import tests.sim as simmod
from tests.sim import (
    LOOP_LAG_EVENT_MS,
    LOOP_LAG_JITTER_MS,
    LOOP_LAG_MIN_N,
    _RUN_CACHE,
    format_loop_lag_line,
    mutation_tests_enabled,
    parse_srt,
    run_100min,
)


pytestmark = pytest.mark.skipif(
    not mutation_tests_enabled(),
    reason="opt-in: set BREEZE_SIM_MUTATION=1",
)

_WIN_TICK = 0.015625
_PER_SEGMENT_MS = (20, 30, 70, 150)
_SWEEP_MS = (10, 15, 20, 25, 30, 40, 70, 150)


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
def _jittered_windows():
    """J model: Event waits gain U(0, 6.5 ms) and a 3% extra 15.625 ms tick.

    Also floors monotonic, sets the loop clock resolution, and rounds selector
    timeouts up to the next tick. Restores every patch, including on failure.
    """
    import asyncio.base_events as be
    import asyncio.selector_events as se

    tick = _WIN_TICK
    rng = random.Random(1)
    real_mono = time.monotonic
    orig_cond = threading.Condition.wait
    orig_loop_init = be.BaseEventLoop.__init__
    orig_sel_init = se.BaseSelectorEventLoop.__init__

    def monotonic():
        return math.floor(real_mono() / tick) * tick

    def cond_wait(self, timeout=None):
        if timeout is not None and timeout > 0:
            extra = rng.uniform(0.0, 0.0065)
            if rng.random() < 0.03:
                extra += tick
            timeout = timeout + extra
        return orig_cond(self, timeout)

    def loop_init(self, *a, **k):
        orig_loop_init(self, *a, **k)
        self._clock_resolution = tick

    def sel_init(self, selector=None):
        orig_sel_init(self, selector)
        sel = self._selector
        real = sel.select

        def select(timeout=None):
            if timeout is not None and timeout > 0:
                end = time.perf_counter() + timeout
                timeout = math.ceil(end / tick) * tick - time.perf_counter()
                if timeout < 0:
                    timeout = 0.0
            return real(timeout)

        sel.select = select

    time.monotonic = monotonic
    threading.Condition.wait = cond_wait
    be.BaseEventLoop.__init__ = loop_init
    se.BaseSelectorEventLoop.__init__ = sel_init
    try:
        yield
    finally:
        time.monotonic = real_mono
        threading.Condition.wait = orig_cond
        be.BaseEventLoop.__init__ = orig_loop_init
        se.BaseSelectorEventLoop.__init__ = orig_sel_init


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


def _fresh_report(
    *,
    kind=None,
    ms=None,
    seqs=None,
    win=False,
    jitter=False,
    busy=0,
    segments=None,
    parks_for=None,
):
    if win and jitter:
        raise ValueError("win tick and J jitter are different clocks")
    _RUN_CACHE.clear()
    env_ga = os.environ.get("GITHUB_ACTIONS")
    os.environ["GITHUB_ACTIONS"] = "true"
    os.environ.pop("BREEZE_SIM_LAG_REPORT_ONLY", None)
    old_segments = simmod.SEGMENTS
    orig_asr = simmod.TextAsr
    if segments is not None:
        simmod.SEGMENTS = segments
    if parks_for is not None:
        class _Asr(orig_asr):
            def _parks_v(self, text: str) -> list[float]:
                return list(parks_for(text, self.delay_v))

        simmod.TextAsr = _Asr
    try:
        with _inject(kind, ms, seqs):
            timer = _jittered_windows() if jitter else (_win_event_tick() if win else _null())
            with timer:
                with _busy_loops(busy) if busy else _null():
                    report = run_100min()
    finally:
        simmod.TextAsr = orig_asr
        simmod.SEGMENTS = old_segments
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


def _lag_red(report) -> bool:
    return (
        report.loop_lag_event_ms > LOOP_LAG_EVENT_MS
        or report.loop_lag_jitter_ms > LOOP_LAG_JITTER_MS
    )


def _red(report, line: str) -> None:
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert _lag_red(report), line


def _green(report, line: str, *, waiting: bool = True, waiting_zero: bool = False) -> None:
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert report.loop_lag_event_ms <= LOOP_LAG_EVENT_MS, line
    assert report.loop_lag_jitter_ms <= LOOP_LAG_JITTER_MS, line
    if waiting_zero:
        assert report.waiting_v_total == 0, (report.waiting, line)
    elif waiting:
        assert report.waiting_v_total < 1, (report.waiting, line)


def _srt_drift(report) -> float:
    cues = parse_srt(report.srt)
    return abs(cues[-1][2] / 1000.0 - report.segments * 6)


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
    _green(report, line, waiting_zero=True)


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
    _green(report, line, waiting_zero=True)
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
        red = _lag_red(report)
        rows.append((ms, red, report.loop_lag_event_ms, report.loop_lag_jitter_ms, report.loop_lag_stall_ms))
        with capsys.disabled():
            print(
                f"mut sweep {kind} {ms}ms red={red} "
                f"event={report.loop_lag_event_ms:.1f} jitter={report.loop_lag_jitter_ms:.1f} {line}",
                flush=True,
            )
        if seen_red:
            assert red, (kind, rows)
        if red:
            seen_red = True
    assert seen_red, (kind, rows)
    assert rows[0][0] == 10
    assert 25 in _SWEEP_MS and 40 in _SWEEP_MS


def test_three_109ms_must_red(capsys):
    """Three 109 ms stalls sum past the 250 ms event gate. Two are report-only."""
    report = _fresh_report(kind="sleep", ms=109, seqs={200, 500, 800})
    line = _line(report)
    with capsys.disabled():
        print(f"mut 3x109 {line} waiting={report.waiting_v_total}", flush=True)
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert report.loop_lag_event_ms > LOOP_LAG_EVENT_MS, line


def test_two_109ms_linux_report_only(capsys):
    report = _fresh_report(kind="sleep", ms=109, seqs={400, 800})
    line = _line(report)
    with capsys.disabled():
        print(f"mut 2x109 linux report-only {line} waiting={report.waiting_v_total}", flush=True)
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line


def test_two_109ms_jitter_report_only(capsys):
    report = _fresh_report(kind="sleep", ms=109, seqs={400, 800}, jitter=True)
    line = _line(report)
    with capsys.disabled():
        print(f"mut 2x109 J report-only {line} waiting={report.waiting_v_total}", flush=True)
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line


def test_clean_jitter_1000_must_green(capsys):
    report = _fresh_report(jitter=True, segments=1000)
    line = _line(report)
    with capsys.disabled():
        print(f"mut cleanJ1000 {line} waiting={report.waiting_v_total} period={report.loop_lag_period_ms}", flush=True)
    _green(report, line)
    assert 11.0 <= report.loop_lag_period_ms <= 18.0, report.loop_lag_period_ms


def test_clean_jitter_3000_must_green(capsys):
    report = _fresh_report(jitter=True, segments=3000)
    line = _line(report)
    with capsys.disabled():
        print(f"mut cleanJ3000 {line} waiting={report.waiting_v_total} period={report.loop_lag_period_ms}", flush=True)
    _green(report, line)
    assert 11.0 <= report.loop_lag_period_ms <= 18.0, report.loop_lag_period_ms


def test_jitter_three_150ms_must_red(capsys):
    report = _fresh_report(kind="sleep", ms=150, seqs={200, 500, 800}, jitter=True)
    line = _line(report)
    with capsys.disabled():
        print(f"mut J 3x150 {line} waiting={report.waiting_v_total}", flush=True)
    assert report.loop_lag_n >= LOOP_LAG_MIN_N, line
    assert report.loop_lag_event_ms > LOOP_LAG_EVENT_MS, line


def _every(parts: list[float], step: int = 10):
    def parks(text: str, delay_v: float) -> list[float]:
        digits = "".join(ch for ch in text if ch.isdigit())
        seq = int(digits) if digits else 0
        if seq and seq % step == 0:
            return list(parts)
        return [delay_v]

    return parks


def _assert_backlog_red(report, line: str, min_waiting: float) -> None:
    drift = _srt_drift(report)
    with pytest.raises(AssertionError):
        assert report.waiting_v_total < 1
    with pytest.raises(AssertionError):
        assert drift <= 2
    assert report.waiting_v_total >= min_waiting, (report.waiting_v_total, drift, line)
    assert drift > 2, (drift, report.waiting_v_total, line)


def test_asr_7v_split_2_must_red(capsys):
    """7.0v as 2×3.5v. Both parks fit in a slice; waiting must stay >= 900."""
    report = _fresh_report(parks_for=lambda text, delay: [3.5, 3.5])
    line = _line(report)
    drift = _srt_drift(report)
    with capsys.disabled():
        print(f"mut asr7x2 waiting={report.waiting_v_total:.3f} srt_drift={drift:.3f} {line}", flush=True)
    _assert_backlog_red(report, line, 900)


def test_asr_12v_split_4_must_red(capsys):
    report = _fresh_report(parks_for=lambda text, delay: [3.0, 3.0, 3.0, 3.0])
    line = _line(report)
    drift = _srt_drift(report)
    with capsys.disabled():
        print(f"mut asr12x4 waiting={report.waiting_v_total:.3f} srt_drift={drift:.3f} {line}", flush=True)
    _assert_backlog_red(report, line, 5000)


def test_sparse7_split_must_red(capsys):
    """Every 10th segment is a 7v park split into two 3.5v parks."""
    report = _fresh_report(parks_for=_every([3.5, 3.5]))
    line = _line(report)
    drift = _srt_drift(report)
    with capsys.disabled():
        print(f"mut SPARSE7 waiting={report.waiting_v_total:.3f} srt_drift={drift:.3f} {line}", flush=True)
    _assert_backlog_red(report, line, 1)


def test_sparse_1_5_3_5_2_must_red(capsys):
    report = _fresh_report(parks_for=_every([1.5, 3.5, 2.0]))
    line = _line(report)
    drift = _srt_drift(report)
    with capsys.disabled():
        print(f"mut sparse1.5+3.5+2 waiting={report.waiting_v_total:.3f} srt_drift={drift:.3f} {line}", flush=True)
    _assert_backlog_red(report, line, 1)


def test_sparse_1_5_5_5_must_red(capsys):
    report = _fresh_report(parks_for=_every([1.5, 5.5]))
    line = _line(report)
    drift = _srt_drift(report)
    with capsys.disabled():
        print(f"mut sparse1.5+5.5 waiting={report.waiting_v_total:.3f} srt_drift={drift:.3f} {line}", flush=True)
    _assert_backlog_red(report, line, 1)
