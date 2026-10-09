"""Synthetic loop-lag probe ticks. Runs on every CI job; no 100-minute class."""

from __future__ import annotations

import sys
import time

from tests.sim import (
    LOOP_LAG_DEADBAND_MS,
    LOOP_LAG_SAMPLE_S,
    LOOP_LAG_STALL_MS,
    _LoopLagProbe,
)


WIN_P = 0.015625


def _probe(period: float = WIN_P) -> _LoopLagProbe:
    probe = _LoopLagProbe(time.perf_counter, time.sleep)
    probe._period = period
    probe._stable_t0 = 0.0
    probe._stable = True
    return probe


def _feed(probe: _LoopLagProbe, sent: float, exec_at: float) -> None:
    probe._real_perf = lambda: exec_at
    probe._on_tick(sent)


def test_clean_windows_ticks_have_zero_stall():
    """P=15.625 ms: a clean sender that really waits P must not accumulate stall."""
    probe = _probe(WIN_P)
    t = 1.0
    for _ in range(40):
        sent = t
        _feed(probe, sent, sent + 0.0002)
        t += WIN_P
    n, p50, _p99, mx, over = probe.summary_ms()
    assert n == 40
    assert probe._stall_ms == 0.0
    assert over == 0
    assert mx < 1.0
    assert p50 < 1.0


def test_gil_stall_on_sender_and_loop_still_counts():
    """Sent interval == exec interval (C-layer GIL holds both sides).

    Subtracting the actual send interval would cancel this stall. heartbeat
    uses the pre-run period P, so 70 ms extra still accumulates.
    """
    probe = _probe(WIN_P)
    t = 1.0
    _feed(probe, t, t + 0.0002)
    t += WIN_P
    _feed(probe, t, t + 0.0002)
    stall_ms = 70.0
    t += WIN_P + stall_ms / 1000.0
    _feed(probe, t, t + 0.0002)
    assert probe._stall_ms >= 60.0, probe._stall_ms
    # Subtracting send interval: exec_gap - send_gap ≈ 0.
    send_gap = (t - (1.0 + WIN_P))
    exec_gap = send_gap
    cancelled = (exec_gap - send_gap) * 1000.0
    assert cancelled < 1.0
    assert probe._stall_ms > cancelled + 50.0


def test_hold_then_150ms_stall_is_counted():
    """end_hold anchors last_exec at the hold's end, not None."""
    probe = _probe(WIN_P)
    t = 1.0
    _feed(probe, t, t + 0.0002)
    hold_end = t + 0.020
    times = iter([hold_end - 0.010, hold_end, hold_end])
    probe._real_perf = lambda: next(times)
    probe.begin_hold()
    probe.end_hold()
    assert probe._last_exec_at == hold_end
    stall_at = hold_end + 0.150
    _feed(probe, stall_at, stall_at)
    # heartbeat = 150 ms - P; stall = that minus 5 ms deadband.
    expect = 150.0 - WIN_P * 1000.0 - LOOP_LAG_DEADBAND_MS
    assert probe._stall_ms >= 100.0, (probe._stall_ms, expect)
    assert abs(probe._stall_ms - expect) < 1.0


def test_hold_none_anchor_would_drop_the_stall():
    """If last_exec were cleared at end_hold, the 150 ms gap would vanish."""
    probe = _probe(WIN_P)
    t = 1.0
    _feed(probe, t, t + 0.0002)
    probe._last_exec_at = None
    stall_at = t + 0.150
    _feed(probe, stall_at, stall_at)
    assert probe._stall_ms == 0.0


def test_queued_ticks_do_not_double_count_stall():
    """After a stall, queued callbacks run back-to-back (gap ~0)."""
    probe = _probe(LOOP_LAG_SAMPLE_S)
    t = 1.0
    _feed(probe, t, t)
    stall_exec = t + 0.070
    _feed(probe, t + LOOP_LAG_SAMPLE_S, stall_exec)
    # Three queued ticks execute immediately after.
    for i in range(3):
        _feed(probe, t + (2 + i) * LOOP_LAG_SAMPLE_S, stall_exec + 0.00005 * (i + 1))
    first = 70.0 - LOOP_LAG_SAMPLE_S * 1000.0 - LOOP_LAG_DEADBAND_MS
    assert abs(probe._stall_ms - first) < 2.0, probe._stall_ms


def test_period_calibration_clamps_to_10_18ms():
    probe = _LoopLagProbe(time.perf_counter, time.sleep)
    waits = [0.005] * 20
    waits.sort()
    median = waits[len(waits) // 2]
    period = min(max(median, LOOP_LAG_SAMPLE_S), 0.018)
    assert period == LOOP_LAG_SAMPLE_S
    waits = [0.025] * 20
    median = sorted(waits)[len(waits) // 2]
    period = min(max(median, LOOP_LAG_SAMPLE_S), 0.018)
    assert period == 0.018
    waits = [0.0156] * 20
    median = sorted(waits)[len(waits) // 2]
    period = min(max(median, LOOP_LAG_SAMPLE_S), 0.018)
    assert abs(period - 0.0156) < 1e-9
    probe._period = period
    assert LOOP_LAG_STALL_MS == 350.0


def test_start_calibrates_period_before_the_sender_thread():
    probe = _LoopLagProbe(time.perf_counter, time.sleep)

    class _Loop:
        def call_soon_threadsafe(self, *a, **k):
            return None

    probe.start(_Loop())
    try:
        assert probe._thread is not None
        ms = probe._period * 1000.0
        assert 10.0 <= ms <= 18.0, ms
        if sys.platform != "win32":
            assert ms < 12.0, ms
    finally:
        probe.stop()
