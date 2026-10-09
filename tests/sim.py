"""Scaled headless stand-in for the host page, the audience socket, and a 100-minute class.

VirtualHost posts the same opt-in the host page sends (form wait_translation=0 and
header x-breeze-async-translation: 1). The default /api/push still waits for English;
tests/test_round2.py covers that and is left unchanged.

Times on the subtitle clock are scheduled (one period per segment, plus time spent
waiting on maxInflight). They are not taken from the wall clock, so a few milliseconds
of asyncio delay cannot stretch a 1000-segment SRT by minutes.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.error
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.asr import AsrResult
from app.server import create_app, rss_bytes
from app.settings import Settings
from app.translate import Translator
from tests.test_round2 import Socket, auth, copy_decoder, open_room, stop, token_of

# 0.01 rather than the spec's 0.005: a 6s slice is then 60ms, so a few milliseconds of
# ASGI overhead cannot fill maxInflight and look like the recorder paused. Windows
# uses 0.02: its default timer tick is about 15.6ms, so at 0.01 one virtual second is
# shorter than one clock tick and a push that sleeps through four ticks (ASR thread,
# decode, loop wakeups) already exceeds a 6s slice. Thresholds stay in virtual
# seconds either way; BREEZE_SIM_SCALE overrides the default.
_DEFAULT_SCALE = "0.02" if sys.platform == "win32" else "0.01"
SCALE = float(os.getenv("BREEZE_SIM_SCALE", _DEFAULT_SCALE))
SEGMENTS = int(os.getenv("BREEZE_SIM_SEGMENTS", "1000"))

_RUN_CACHE: dict[tuple, "SimReport"] = {}


def vms(real_s: float) -> float:
    """Real seconds to virtual milliseconds."""
    return real_s / SCALE * 1000


def virtual_s(real_s: float) -> float:
    return real_s / SCALE


@contextmanager
def no_gc_pause():
    """Collect, then disable automatic GC for one measured window.

    A full collection is a stop-the-world pause. The 100-minute class freezes
    and disables GC for the same reason. This shorter window does not freeze.
    The enabled state from before the window is restored on the way out.
    """
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@contextmanager
def watch_full_gc():
    """Record generation-2 collections that start inside the block.

    gc.callbacks runs on 3.11 and 3.13 before each collection. An empty list
    means no full GC. The callback is removed even when the block fails.
    """
    seen: list[int] = []

    def _on_gc(phase, info):
        if phase == "start" and int(info.get("generation", -1)) >= 2:
            seen.append(int(info["generation"]))

    gc.callbacks.append(_on_gc)
    try:
        yield seen
    finally:
        try:
            gc.callbacks.remove(_on_gc)
        except ValueError:
            pass


def vlimit(limit: float) -> float:
    """Spec threshold in virtual seconds.

    Windows is the only platform that gets any slack, and only 20%. Its timer
    tick is about 15.6 ms, which at the Windows sim scale is a large fraction
    of a short limit. A zero-wait check and the SRT end bound do not go through
    here: one Windows 3.11 run booked a 109 ms stall as 5.45 virtual seconds
    of recorder pause (scale 0.02) and the same stretch on the last cue. That
    is a simulator stall while a slot was still held, not a wider spec limit.
    """
    if sys.platform == "win32":
        return limit * 1.2
    return limit


_ACTIVE_CLOCK: "_ScaledClock | None" = None

# Wall-clock loop-lag probe. Not asyncio.sleep / call_later: the scaled clock
# jumps those timers and the sample comes back in ~0 ms.
#
# Hard gates (r162 / perf recheck2): n >= 100 and stall_ms <= 350.
# stall_ms is the stable-window sum of max(0, heartbeat_ms - 5). heartbeat is
# exec_at - last_exec - P, where P is the sender's Event.wait(0.010) median
# measured in start() before any work (clamped 10-18 ms). Subtracting the
# actual send interval is wrong: a C-layer GIL stall stretches send and exec
# together and cancels out.
#
# p50 / p99 / max / over30 / events are printed, not gated. p50 is not
# monotonic in stall length (phase of 10 ms samples vs 5 ms GIL switches).
# max is report-only because a known win-3.11 single 109 ms pause must not
# fail the class (main tests/sim.py vlimit docstring); that is a chief-reviewer
# ruling, not a loosening of a main-branch gate (main had no loop-lag gate).
LOOP_LAG_SAMPLE_S = 0.010
LOOP_LAG_OVER_MS = 30.0
LOOP_LAG_OVER_MAX = 10
LOOP_LAG_MAX_MS = 100.0
LOOP_LAG_P50_MS = 8.0
LOOP_LAG_MIN_N = 100
LOOP_LAG_CAL_N = 20
LOOP_LAG_PERIOD_CAP_S = 0.018
LOOP_LAG_DEADBAND_MS = 5.0
LOOP_LAG_STALL_MS = 350.0


def loop_lag_gate_enforced() -> bool:
    """Fail the lag gates unless BREEZE_SIM_LAG_REPORT_ONLY is an explicit opt-in.

    CI and local runs use the same hard gates (n, stall_ms). GitHub Actions
    sets GITHUB_ACTIONS but not this variable, so CI cannot report-only.
    """
    val = os.environ.get("BREEZE_SIM_LAG_REPORT_ONLY", "")
    return val.strip().lower() not in {"1", "true", "yes", "on"}


def mutation_tests_enabled() -> bool:
    """Opt-in 100-minute injection matrix. Default off; nightly / dispatch only."""
    val = os.environ.get("BREEZE_SIM_MUTATION", "")
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _loadavg():
    getter = getattr(os, "getloadavg", None)
    if getter is None:
        return None
    try:
        return tuple(float(x) for x in getter())
    except OSError:
        return None


def format_loop_lag_line(report: "SimReport") -> str:
    """One line of n / stall (hard) and p50 / p99 / max / over30 / events (report)."""

    def _fmt_load(item) -> str:
        if item is None:
            return "n/a"
        return ",".join(f"{x:.2f}" for x in item)

    load = report.loop_lag_load
    if load is None:
        load_s = "n/a"
    else:
        load_s = f"{_fmt_load(load[0])}->{_fmt_load(load[1])}"
    gate = "enforce" if loop_lag_gate_enforced() else "report-only (BREEZE_SIM_LAG_REPORT_ONLY)"
    holds = (
        f"holds={report.loop_lag_hold_n}/{report.loop_lag_hold_sum_ms:.1f}ms/"
        f"max={report.loop_lag_hold_max_ms:.1f}ms"
    )
    return (
        f"sim loop lag: n={report.loop_lag_n} stall={report.loop_lag_stall_ms:.1f}ms "
        f"period={report.loop_lag_period_ms:.3f}ms p50={report.loop_lag_p50_ms:.3f}ms "
        f"p99={report.loop_lag_p99_ms:.3f}ms max={report.loop_lag_max_ms:.3f}ms "
        f"over{LOOP_LAG_OVER_MS:.0f}={report.loop_lag_over_30ms} "
        f"events={report.loop_lag_events} event_max={report.loop_lag_event_max_ms:.1f}ms "
        f"load={load_s} {holds} gate={gate}"
    )


def _loop_sleeper(loop):
    """Object whose ``select(timeout)`` is what the running loop blocks in.

    Selector loops expose ``_selector``. CPython's ProactorEventLoop (Windows)
    aliases that same attribute to the IOCP proactor, which also has
    ``select(timeout)``. ``_proactor`` is the fallback if the alias is absent.
    Returning None is a hard error: a wall-clock fallback would count scheduler
    delay as recorder pause again.
    """
    for name in ("_selector", "_proactor"):
        sleeper = getattr(loop, name, None)
        if sleeper is not None and hasattr(sleeper, "select"):
            return sleeper
    return None


class _LoopLagProbe:
    """Real event-loop lag via a thread and call_soon_threadsafe.

    Per-sample lag is the larger of (1) callback time minus the thread's send
    time and (2) exec_at - last_exec - P, with P the calibrated sender period.
    (2) is what a GIL / C-layer stall looks like: the probe thread cannot take
    a send timestamp until the GIL is released, so (1) alone under-counts.
    Only the stable window (first upload through last upload) counts, and
    samples that overlap a GC / heap hold are dropped.

    The hard metric is stall_ms = sum(max(0, heartbeat_ms - 5)) over that
    window. Queued ticks run back-to-back (gap ~0) so they are not double
    counted. P is fixed before work starts, so a C-layer GIL that also stalls
    the sender still accumulates.
    """

    def __init__(self, real_perf, real_sleep) -> None:
        self._real_perf = real_perf
        self._real_sleep = real_sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop = None
        self._lags: list[float] = []
        self._period = LOOP_LAG_SAMPLE_S
        self._stall_ms = 0.0
        self._events = 0
        self._event_max_ms = 0.0
        self._holds: list[tuple[float, float]] = []
        self._hold_t0: float | None = None
        self._stable = False
        self._stable_t0: float | None = None
        self._stable_t1: float | None = None
        self._last_exec_at: float | None = None
        self.load_t0 = None
        self.load_t1 = None

    def start(self, loop) -> None:
        # Calibrate the sender's real wait period before any work runs: same
        # call as the sender (Event.wait), so the platform timer tick is measured.
        # Must not run this after workers start: a GIL spin inflates P and
        # under-counts stall.
        waits = []
        for _ in range(LOOP_LAG_CAL_N):
            t = self._real_perf()
            if self._stop.wait(LOOP_LAG_SAMPLE_S):
                return
            waits.append(self._real_perf() - t)
        waits.sort()
        median = waits[len(waits) // 2]
        self._period = min(max(median, LOOP_LAG_SAMPLE_S), LOOP_LAG_PERIOD_CAP_S)
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="sim-loop-lag", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._loop = None
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)

    def enter_stable(self) -> None:
        self._stable_t0 = self._real_perf()
        self.load_t0 = _loadavg()
        self._stable = True
        self._last_exec_at = None

    def leave_stable(self) -> None:
        self._stable_t1 = self._real_perf()
        self.load_t1 = _loadavg()
        self._stable = False

    def begin_hold(self) -> None:
        self._hold_t0 = self._real_perf()

    def end_hold(self) -> None:
        t0 = self._hold_t0
        self._hold_t0 = None
        if t0 is not None:
            self._holds.append((t0, self._real_perf()))
        # A hold is not loop stall; the next gap starts at the hold's end so a
        # stall that lands immediately after the hold is still counted.
        self._last_exec_at = self._real_perf()

    def summary_ms(self) -> tuple[int, float, float, float, int]:
        ms = [lag * 1000.0 for lag in self._lags]
        if not ms:
            self._events = 0
            self._event_max_ms = 0.0
            return 0, 0.0, 0.0, 0.0, 0
        over = sum(1 for item in ms if item > LOOP_LAG_OVER_MS)
        events = []
        cur = 0.0
        for item in ms:
            if item > LOOP_LAG_OVER_MS:
                cur = max(cur, item)
            elif cur:
                events.append(cur)
                cur = 0.0
        if cur:
            events.append(cur)
        self._events = len(events)
        self._event_max_ms = max(events) if events else 0.0
        return len(ms), percentile(ms, 0.50), percentile(ms, 0.99), max(ms), over

    def hold_summary_ms(self) -> tuple[int, float, float]:
        durs = [(end - start) * 1000.0 for start, end in self._holds]
        if not durs:
            return 0, 0.0, 0.0
        return len(durs), sum(durs), max(durs)

    def _run(self) -> None:
        while not self._stop.is_set():
            sent = self._real_perf()
            loop = self._loop
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._on_tick, sent)
                except RuntimeError:
                    pass
            self._stop.wait(LOOP_LAG_SAMPLE_S)

    def _on_tick(self, sent: float) -> None:
        if self._stop.is_set():
            return
        exec_at = self._real_perf()
        last_exec = self._last_exec_at
        self._last_exec_at = exec_at
        t0 = self._stable_t0
        if t0 is None:
            return
        end = self._stable_t1 if self._stable_t1 is not None else exec_at
        if sent < t0 or exec_at > end:
            return
        if self._hold_t0 is not None:
            return
        for hold0, hold1 in self._holds:
            if sent < hold1 and exec_at > hold0:
                return
            if last_exec is not None and last_exec < hold1 and exec_at > hold0:
                last_exec = None
        thread_lag = exec_at - sent
        heartbeat_lag = 0.0
        if last_exec is not None:
            heartbeat_lag = exec_at - last_exec - self._period
            extra_ms = heartbeat_lag * 1000.0 - LOOP_LAG_DEADBAND_MS
            if extra_ms > 0.0:
                self._stall_ms += extra_ms
            if heartbeat_lag < 0.0:
                heartbeat_lag = 0.0
        self._lags.append(max(thread_lag, heartbeat_lag))


class _ScaledClock:
    """Scaled clock for the paced 100-minute class.

    Slot waits, Chinese latency, and translation deadlines are read off the wall
    clock and divided by SCALE (0.01 here, 0.02 on Windows). A few real
    milliseconds of scheduler delay is then several virtual seconds, so a loaded
    interpreter books a recorder pause even though the scripted ASR is 1.5s inside
    a 6s slice.

    Two virtual axes share idle jumps (scripted sleep and asyncio timers) but
    split when a worker is in real work:

    * ``now`` / ``perf_counter``: waiting and host pacing. Advances only on
      those jumps, so scheduler delay is not multiplied into ``waiting_v_total``.
    * ``timeout_now`` / ``monotonic``: ``asyncio.timeout`` (decode 40s). Also
      advances 1:1 with time actually blocked in ``select`` while a worker is
      busy, so a 45s stuck decode still hits the 40s budget in real time.

    Callback time and the gap between select calls are not 1:1 on either axis.
    """

    def __init__(self) -> None:
        # Real clocks must be saved before install() replaces monotonic / perf_counter.
        self._real_monotonic = time.monotonic
        self._real_perf = time.perf_counter
        self._real_sleep = time.sleep
        self._lock = threading.Lock()
        self._origin_m = self._real_monotonic()
        self._origin_p = self._real_perf()
        self.now = 0.0
        self.timeout_now = 0.0
        # (deadline, done, cancel, original delay_s). delay_s lets _wait_slot
        # tell scripted ASR that fits in a 6s slice from a true backlog park.
        self._waiters: list[tuple[float, threading.Event, threading.Event | None, float]] = []
        self._released_park_s: list[float] = []
        self._pending = 0
        self._parked = 0
        self.active = False
        self._loop = None
        self._selector = None
        self._orig_select = None
        self._orig_wrap = None
        self._orig_aio_wrap = None
        # Released by the loop thread. If that never happens, fail the wait
        # instead of leaving a worker parked until the suite is killed.
        self._real_wait_s = 180.0
        self._last_real = self._origin_p
        self._went_backward = False
        self.probe: _LoopLagProbe | None = None

    def monotonic(self) -> float:
        with self._lock:
            return self._origin_m + self.timeout_now

    def perf_counter(self) -> float:
        with self._lock:
            return self._origin_p + self.now

    def pending_inc(self) -> None:
        with self._lock:
            self._pending += 1

    def pending_dec(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def sleep(self, delay_s: float, cancel: threading.Event | None = None) -> bool:
        """Park a worker until scaled time passes. True if cancel won."""
        if delay_s <= 0 or (cancel is not None and cancel.is_set()):
            return bool(cancel is not None and cancel.is_set())
        done = threading.Event()
        with self._lock:
            self._waiters.append((self.now + float(delay_s), done, cancel, float(delay_s)))
            self._parked += 1
        # The loop may already be inside select. Wake it so it observes this
        # waiter. Waiting on the loop thread would deadlock: select never runs.
        self._wake_loop()
        if not done.wait(self._real_wait_s):
            with self._lock:
                before = len(self._waiters)
                self._waiters = [item for item in self._waiters if item[1] is not done]
                if len(self._waiters) != before:
                    self._parked = max(0, self._parked - 1)
            raise TimeoutError("scaled clock did not release a worker wait")
        return bool(cancel is not None and cancel.is_set())

    def _wake_loop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._noop)
        except RuntimeError:
            return

    @staticmethod
    def _noop() -> None:
        return

    def _take_due_locked(self) -> list[threading.Event]:
        due: list[threading.Event] = []
        keep: list[tuple[float, threading.Event, threading.Event | None, float]] = []
        for deadline, done, cancel, delay_s in self._waiters:
            if deadline <= self.now + 1e-9 or (cancel is not None and cancel.is_set()):
                due.append(done)
                self._released_park_s.append(delay_s)
            else:
                keep.append((deadline, done, cancel, delay_s))
        if due:
            self._waiters = keep
            self._parked = max(0, self._parked - len(due))
        return due

    def take_released_parks(self) -> list[float]:
        with self._lock:
            out = list(self._released_park_s)
            self._released_park_s.clear()
            return out

    def _next_delta_locked(self, timeout: float | None) -> float | None:
        waiter_delta = None
        if self._waiters:
            nxt = min(item[0] for item in self._waiters)
            waiter_delta = max(0.0, nxt - self.now)
        if timeout is not None and timeout > 0:
            if waiter_delta is None:
                return float(timeout)
            return min(float(timeout), waiter_delta)
        return waiter_delta

    def _advance_locked(self, delta: float) -> None:
        if delta < 0:
            self._went_backward = True
            return
        if delta:
            self.now += delta
            self.timeout_now += delta

    def _advance_timeout_locked(self, delta: float) -> None:
        """1:1 real time onto the timeout axis only. Does not move waiting."""
        if delta < 0:
            self._went_backward = True
            return
        if delta:
            self.timeout_now += delta

    def sync_real(self) -> None:
        """Holds used to dump elapsed into 1:1 waiting; timeout 1:1 is select-only."""
        self._last_real = self._real_perf()

    def install(self) -> None:
        global _ACTIVE_CLOCK
        import asyncio.futures as aio_futures

        loop = asyncio.get_running_loop()
        selector = _loop_sleeper(loop)
        if selector is None:
            raise RuntimeError(
                "scaled clock needs loop._selector.select or loop._proactor.select; "
                "refusing to fall back to the wall clock"
            )
        self._loop = loop
        self._selector = selector
        self._orig_select = selector.select
        self._orig_wrap = aio_futures.wrap_future
        self._orig_aio_wrap = asyncio.wrap_future
        clock = self

        def select(timeout=None):
            # timeout 0: the loop already has ready callbacks, or a timer that is due.
            if not clock.active or timeout == 0:
                return clock._orig_select(timeout)
            ready = clock._orig_select(0)
            if ready:
                return ready
            wake: list[threading.Event] = []
            # Never block forever. A worker can park after we drop the lock;
            # an infinite select would then leave both sides waiting.
            poll = 0.02
            busy = 0
            with clock._lock:
                wake.extend(clock._take_due_locked())
                busy = clock._pending - clock._parked
                if busy > 0:
                    # Real work is in flight. Do not jump scripted time: that
                    # multiplied scheduler delay into waiting_v_total. Block a
                    # short real interval and 1:1 only the time spent inside
                    # select onto the timeout axis so asyncio.timeout still
                    # fires for a stuck decode.
                    # Only slice-fitting parks (ASR 1.5v) may catch up: a 40v
                    # translate park in the slow window must not move `now`
                    # while wait_slot is blocked, or waiting_v_total explodes.
                    slice_s = 6.0 * SCALE
                    short = [item for item in clock._waiters if item[3] <= slice_s + 1e-12]
                    if short:
                        nxt = min(item[0] for item in short)
                        waiter_delta = max(0.0, nxt - clock.now)
                        if timeout is not None and timeout > 0:
                            waiter_delta = min(waiter_delta, float(timeout))
                        if waiter_delta:
                            clock._advance_locked(waiter_delta)
                            wake.extend(clock._take_due_locked())
                    if timeout is not None and timeout > 0:
                        poll = min(0.001, float(timeout))
                    else:
                        poll = 0.001
                else:
                    delta = clock._next_delta_locked(timeout)
                    if delta is not None:
                        if delta > 0:
                            clock._advance_locked(delta)
                        wake.extend(clock._take_due_locked())
                        poll = 0.0
                    # delta is None: the loop is waiting on I/O, not a timer.
                    # Do not invent virtual time for that poll.
            for done in wake:
                done.set()
            if busy > 0:
                blocked_t0 = clock._real_perf()
                result = clock._orig_select(poll)
                blocked = clock._real_perf() - blocked_t0
                if blocked > 0:
                    with clock._lock:
                        clock._advance_timeout_locked(blocked)
                return result
            return clock._orig_select(poll)

        def wrap_future(future, *, loop=None):
            if not clock.active:
                return clock._orig_wrap(future, loop=loop)
            clock.pending_inc()
            try:
                afut = clock._orig_wrap(future, loop=loop)
            except BaseException:
                clock.pending_dec()
                raise

            def _dec(_fut) -> None:
                clock.pending_dec()

            # Dec on the loop, after the awaiter is chained, so a jump cannot
            # land between "thread finished" and "coroutine saw the result".
            if afut.done():
                _dec(afut)
            else:
                afut.add_done_callback(_dec)
            return afut

        try:
            selector.select = select
            aio_futures.wrap_future = wrap_future
            asyncio.wrap_future = wrap_future
            time.monotonic = self.monotonic
            time.perf_counter = self.perf_counter
        except BaseException:
            self.close()
            raise
        self.active = True
        _ACTIVE_CLOCK = self
        self._last_real = self._real_perf()
        self.probe = _LoopLagProbe(self._real_perf, self._real_sleep)
        self.probe.start(loop)

    def close(self) -> None:
        global _ACTIVE_CLOCK
        self.active = False
        if self.probe is not None:
            self.probe.stop()
            self.probe = None
        if _ACTIVE_CLOCK is self:
            _ACTIVE_CLOCK = None
        if self._selector is not None and self._orig_select is not None:
            self._selector.select = self._orig_select
        if self._orig_wrap is not None:
            import asyncio.futures as aio_futures

            aio_futures.wrap_future = self._orig_wrap
            if self._orig_aio_wrap is not None:
                asyncio.wrap_future = self._orig_aio_wrap
        time.monotonic = self._real_monotonic
        time.perf_counter = self._real_perf
        with self._lock:
            stranded = [done for _deadline, done, _cancel, _delay in self._waiters]
            self._waiters.clear()
            self._parked = 0
        for done in stranded:
            done.set()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def sim_settings(**over) -> Settings:
    # stop_flush_s / shutdown_flush_s exist on this branch (short stop and shutdown).
    # The spec's main Settings does not have them. Drop unknown fields so the same
    # suite can run there; on this branch every key below is a real field.
    base = dict(
        allow_testclient=True,
        translate_timeout_s=40 * SCALE,
        gap_wait_s=3 * SCALE,
        heartbeat_s=0.05,
        idle_timeout_s=5,
        # 8 virtual seconds, the product default (BREEZE_STOP_FLUSH=8). B-f1's cap
        # stays vlimit(3.5). A 2v flush does not catch a stop that sits out the
        # window: on the Windows scale that stop was measured under 4.2, so it
        # passed; at 8v the same stop is about 10v and fails. Stricter, not looser.
        stop_flush_s=8 * SCALE,
        shutdown_flush_s=0.2,
    )
    base.update(over)
    fields = getattr(Settings, "__dataclass_fields__", None)
    if fields is not None:
        base = {key: value for key, value in base.items() if key in fields}
    return Settings(**base)


class TextAsr:
    """Audio bytes are the Chinese line. delay_v is virtual seconds."""

    def __init__(self, delay_v: float = 1.5, gate=None, on_start=None):
        self.delay_v = delay_v
        self.gate = gate
        self.on_start = on_start
        self.calls = 0
        self.done_at: list[float] = []
        self.spent_v: list[float] = []
        self.clock_sleeps = 0
        self.seen: list[str] = []

    def transcribe(self, wav: Path, prompt: str = "") -> AsrResult:
        del prompt
        # The admit slot is still held here. Callers use this to observe pending.
        if self.on_start is not None:
            self.on_start()
        text = wav.read_bytes().decode()
        self.seen.append(text)
        self.calls += 1
        gate = self.gate(text) if callable(self.gate) else self.gate
        if isinstance(gate, dict):
            gate["started"].set()
            if not gate["release"].wait(5):
                raise TimeoutError("ASR gate was not released")
        delay_s = max(0.0, self.delay_v) * SCALE
        started = time.monotonic()
        clock = _ACTIVE_CLOCK
        if clock is not None and clock.active:
            clock.sleep(delay_s)
            self.clock_sleeps += 1
        else:
            time.sleep(delay_s)
        finished = time.monotonic()
        self.done_at.append(finished)
        self.spent_v.append((finished - started) / SCALE)
        return AsrResult(ok=True, text=text)


def _sleep_cancel(delay_s: float, cancel) -> bool:
    """Sleep delay_s. Return True if cancel fired first.

    The pipeline passes a cancel event, so the scripted translator waits on
    ``Event.wait`` rather than ``time.sleep``. Both are wall clocks. Under the
    scaled clock they park on that clock instead, or a loaded runner stretches
    every translation past its scaled deadline.
    """
    if delay_s <= 0:
        return bool(cancel is not None and cancel.is_set())
    clock = _ACTIVE_CLOCK
    if clock is not None and clock.active:
        return clock.sleep(delay_s, cancel)
    if cancel is None:
        time.sleep(delay_s)
        return False
    return cancel.wait(delay_s)


def _deadline_margin(room_s: float) -> float:
    """Real seconds to finish before the caller's translate deadline.

    Strictest of the three reviews of the Windows 3.12 failure (run
    37580495810): at least 50 ms, at least a tenth of the time still left,
    and at least 20 ms plus three monotonic ticks. A fixed 20 ms early wake
    was eaten by two 15.6 ms ticks (thread wait lands on the next tick,
    asyncio.timeout fires one tick early). The scripted line still occupies
    the worker until this margin before the deadline.
    """
    tick = time.get_clock_info("monotonic").resolution
    return max(0.05, float(room_s) * 0.1, 0.02 + 3 * tick)


class ScriptedTranslator(Translator):
    """plan(zh) -> ("ok", delay_v) | ("raise", exc) | ("block", event)."""

    def __init__(self, plan):
        super().__init__(enabled=True, key="test-key")
        self.plan = plan
        self.started: list[tuple[str, float]] = []
        self.finished: list[tuple[str, float]] = []

    def translate(self, zh, glossary=None, context=None, deadline=None, cancel=None):
        del glossary, context
        self.calls += 1
        self.started.append((zh, time.monotonic()))
        try:
            action = self.plan(zh) if callable(self.plan) else self.plan
            kind = action[0]
            if kind == "ok":
                delay = float(action[1]) * SCALE
                if deadline is not None:
                    # Finish inside the caller's timeout. A sleep equal to the deadline
                    # races asyncio.wait_for and comes back as "timeout" instead of English.
                    # The margin has to cover a coarse monotonic clock, not a fixed 20 ms.
                    room = deadline - time.monotonic()
                    delay = min(delay, max(0.0, room - _deadline_margin(room)))
                if _sleep_cancel(delay, cancel):
                    return _timeout_result()
                return _ok_result(zh)
            if kind == "raise":
                raise action[1]
            if kind == "block":
                event = action[1]
                while not event.is_set():
                    if cancel is not None and cancel.is_set():
                        return _timeout_result()
                    if deadline is not None and time.monotonic() >= deadline:
                        return _timeout_result()
                    if event.wait(0.01):
                        break
                if cancel is not None and cancel.is_set():
                    return _timeout_result()
                return _ok_result(zh)
            raise RuntimeError(f"unknown plan {kind}")
        finally:
            self.finished.append((zh, time.monotonic()))


def _ok_result(zh: str):
    from app.translate import TranslateResult

    return TranslateResult("EN " + zh, "ok")


def _timeout_result():
    from app.translate import TranslateResult

    return TranslateResult("", "timeout", "英譯逾時，不假設沒有計費。中文仍保留")


class _Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, req, timeout=None):
        del req, timeout
        if not self.responses:
            raise AssertionError("FakeOpener ran out of responses")
        item = self.responses.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return _Body(item)


class HttpPlanTranslator(Translator):
    def __init__(self, responses, slept: list):
        super().__init__(enabled=True, key="test-key", opener=FakeOpener(responses), sleeper=slept.append)


def http_error(code: int, body: bytes = b"", retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions",
        code,
        "err",
        headers,
        io.BytesIO(body),
    )


class Listener:
    """Socket plus a pump that records messages and answers ping."""

    def __init__(self, app, room: str, cursor: int = 0):
        query = f"/ws/listen?room_id={room}&cursor={cursor}"
        self.sock = Socket(app, query)
        self.messages: list[dict] = []
        self.closed = False
        self.close_code = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        await self.sock.__aenter__()
        self._task = asyncio.create_task(self._pump())
        return self

    async def _pump(self) -> None:
        while True:
            # A plain get, not wait_for(get(), timeout): on Python 3.11 wait_for can
            # swallow a cancel that lands as the inner get completes, and close()
            # would then wait forever on a pump that keeps looping.
            try:
                msg = await self.sock.out.get()
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            if msg.get("type") == "websocket.send":
                data = json.loads(msg.get("text") or "{}")
                if isinstance(data, dict):
                    data["_recv_mono"] = now
                    self.messages.append(data)
                    if data.get("type") == "ping":
                        await self.sock.inc.put({
                            "type": "websocket.receive",
                            "text": json.dumps({"type": "pong"}),
                        })
                continue
            if msg.get("type") == "websocket.close":
                self.closed = True
                self.close_code = msg.get("code")
                self.messages.append({"type": "websocket.close", "code": msg.get("code"), "_recv_mono": now})
                return

    def captions(self) -> list[dict]:
        latest: dict[str, dict] = {}
        for msg in self.messages:
            ident = msg.get("id")
            if not ident or msg.get("type") in {"ping", "hello", "room_unavailable", "captions_cleared", "websocket.close"}:
                continue
            prev = latest.get(ident)
            if prev is None or int(msg.get("version") or 1) >= int(prev.get("version") or 1):
                latest[ident] = msg
        return list(latest.values())

    async def wait_for(self, pred, timeout_v: float) -> bool:
        # Floor the wait in real time so a 5 ms virtual budget cannot flake the poll,
        # but callers assert on recorded _recv_mono, not on this deadline.
        deadline = time.monotonic() + max(timeout_v * SCALE, 1.0)
        while time.monotonic() < deadline:
            if pred():
                return True
            await asyncio.sleep(0.005)
        return bool(pred())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.sock.close()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False


def _body_json(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cached_push_rows(rows: list[dict]) -> list[dict]:
    """Seq and status only. The httpx Response and its body stay out of the cache."""
    return [{"seq": int(row["seq"]), "status": int(row["status"])} for row in rows]


async def post_segment(client, token, room, session, seq, payload: bytes, t0_ms: int, t1_ms: int, *, retry: bool = False):
    """Host-page opt-in: wait_translation=0 and x-breeze-async-translation: 1."""
    headers = {**auth(token), "x-breeze-async-translation": "1"}
    data = {
        "room_id": room,
        "session_id": session,
        "seq": str(seq),
        "t0_ms": str(t0_ms),
        "t1_ms": str(t1_ms),
        "wait_translation": "0",
    }
    if retry:
        data["retry"] = "1"
        headers["x-breeze-retry"] = "1"
    return await client.post(
        "/api/push",
        params={"room_id": room, "session_id": session, "seq": str(seq)},
        data=data,
        files={"audio": ("a.webm", payload, "audio/webm")},
        headers=headers,
    )


class VirtualHost:
    """recorder_machine.js with the host page's async-translation opt-in."""

    def __init__(
        self, client, token, room, session, scale=None, max_inflight=2, period_v=6.0, retry_429_v=0.8,
        hold_for_retry: bool = False,
    ):
        self.client = client
        self.token = token
        self.room = room
        self.session = session
        self.scale = SCALE if scale is None else scale
        self.max_inflight = max_inflight
        self.period_v = period_v
        self.retry_429_v = retry_429_v
        # host.html retries a 429 inside the upload and keeps recording until
        # maxInflight uploads are out. Only a one-slot queue must hold the
        # recorder for that retry, or the next slice takes the slot.
        self.hold_for_retry = hold_for_retry
        # Cleared only around a stop-the-world sample so that sample is not
        # inside the upload whose latency we measure. Set means slices may start.
        self._release_slice = asyncio.Event()
        self._release_slice.set()
        self.waiting: list[tuple[float, float]] = []
        self.responses: list[dict] = []
        self.retries: list[int] = []
        self.segment_end_mono: dict[int, float] = {}
        self.clock_ms = 0
        self.stop_elapsed_v = None
        self._tasks: set[asyncio.Task] = set()
        self._retry_tasks: set[asyncio.Task] = set()
        self._all: list[asyncio.Task] = []
        self._active_posts = 0
        self.max_posts = 0

    def _active_tasks(self) -> set[asyncio.Task]:
        self._tasks = {task for task in self._tasks if not task.done()}
        return set(self._tasks)

    def hold_new_slices(self) -> None:
        """Park uploads that have not started. Already-running slices are left alone."""
        self._release_slice.clear()

    def release_new_slices(self) -> None:
        self._release_slice.set()

    async def upload_one(self, seq: int, t0_ms: int, t1_ms: int, text: str):
        # Event.wait() returns immediately when the event is set, without yielding.
        # A cleared event parks this slice until the heap sample is done, and
        # segment_end is taken after that so the sample is not latency.
        await self._release_slice.wait()
        payload = text.encode()
        # Same clock as _recv_mono. Do not switch this one to perf_counter.
        self.segment_end_mono[seq] = time.monotonic()
        # perf_counter, not monotonic: Windows 3.11 monotonic steps by ~15.6 ms.
        started = time.perf_counter()
        self._active_posts += 1
        self.max_posts = max(self.max_posts, self._active_posts)
        try:
            resp = await post_segment(self.client, self.token, self.room, self.session, seq, payload, t0_ms, t1_ms)
            if resp.status_code == 429:
                # host.html sleeps 800ms and posts this same upload once more.
                # With max_queue=1 that retry has to wait until the other upload
                # releases the only slot, and the recorder must not start a slice
                # that would take it. A wider queue retries immediately.
                self.retries.append(seq)
                current = asyncio.current_task()
                if current is not None and self.hold_for_retry:
                    self._retry_tasks.add(current)
                try:
                    if self.hold_for_retry:
                        holders = [task for task in self._active_tasks() if task not in self._retry_tasks]
                        if holders:
                            await asyncio.wait(holders)
                    await asyncio.sleep(self.retry_429_v * self.scale)
                    resp = await post_segment(
                        self.client, self.token, self.room, self.session, seq, payload, t0_ms, t1_ms, retry=True,
                    )
                finally:
                    if current is not None:
                        self._retry_tasks.discard(current)
        finally:
            self._active_posts -= 1
        elapsed_v = (time.perf_counter() - started) / self.scale
        self.responses.append({
            "seq": seq,
            "status": resp.status_code,
            "elapsed_v": elapsed_v,
            "body": _body_json(resp),
            "t0_ms": t0_ms,
            "t1_ms": t1_ms,
        })
        return resp

    async def _wait_slot(self) -> None:
        while True:
            active = self._active_tasks()
            if not self._retry_tasks and len(active) < self.max_inflight:
                return
            if not active:
                return
            clock = _ACTIVE_CLOCK
            if clock is not None:
                clock.take_released_parks()
            start_ms = self.clock_ms
            # perf_counter, not monotonic: Windows 3.11 monotonic steps by ~15.6 ms.
            real = time.perf_counter()
            await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            # Book every real pause, including those under one virtual second.
            # An immediate return (a free slot) never reaches this wait.
            # Round-to-zero is the scheduler, not a pause the clock can see.
            #
            # Subtract scaled-clock parks that fit in one 6s slice. TextAsr
            # 1.5v always fits; a loop stall can defer that park until this
            # wait, which used to book waiting 1.5–3.0 v (exactly ASR delay_v)
            # and fail waiting_v_total < 1. Parks longer than a slice
            # (ASR 7.5v / 9v) are a real backlog and still count.
            spent_s = time.perf_counter() - real
            parks = clock.take_released_parks() if clock is not None else []
            slice_s = self.period_v * self.scale
            short_s = sum(delay for delay in parks if delay <= slice_s + 1e-12)
            spent_ms = int(round(max(0.0, spent_s - short_s) / self.scale * 1000))
            if spent_ms > 0:
                self.clock_ms += spent_ms
                self.waiting.append((start_ms / 1000, self.clock_ms / 1000))

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(
        self, count: int, text_of=None, pace: bool = True, on_each=None, drain: bool = True, before_slice=None,
    ) -> None:
        """Record ``count`` slices. drain=False returns once the last slice is handed to
        upload, like pressing stop right after speaking; stop() then settles uploads.
        before_slice(seq) runs before the slot check, outside any measured wait.
        An async hook is awaited; a hook that returns without awaiting does not
        yield, so arming a 429 storm still beats the upload created last slice."""
        text_of = text_of or (lambda i: f"第{i}句")
        for seq in range(1, count + 1):
            if before_slice is not None:
                hooked = before_slice(seq)
                if asyncio.iscoroutine(hooked):
                    await hooked
            await self._wait_slot()
            if pace:
                await asyncio.sleep(self.period_v * self.scale)
                t0 = self.clock_ms
                self.clock_ms += int(round(self.period_v * 1000))
                t1 = self.clock_ms
            else:
                t0 = int(round((seq - 1) * self.period_v * 1000))
                t1 = int(round(seq * self.period_v * 1000))
            text = text_of(seq)
            task = asyncio.create_task(self.upload_one(seq, t0, t1, text))
            self._all.append(task)
            self._track(task)
            if on_each is not None:
                task.add_done_callback(lambda done, seq=seq: on_each(seq))
        if not drain:
            return
        if self._tasks:
            await asyncio.wait(self._tasks)
        for task in self._all:
            task.result()

    async def stop(self):
        """Wait for uploads already started, then POST /api/session/end. No flush=0."""
        # Same clock as the upload wait. monotonic() on Windows 3.11 is one tick wide.
        real = time.perf_counter()
        if self._tasks:
            await asyncio.wait(self._tasks)
        resp = await self.client.post(
            "/api/session/end",
            json={
                "room_id": self.room,
                "session_id": self.session,
                "last_seq": len(self._all),
            },
            headers={**auth(self.token), "content-type": "application/json"},
        )
        self.stop_elapsed_v = (time.perf_counter() - real) / self.scale
        return resp

    def release_upload_results(self) -> None:
        """Drop push rows and finished tasks so their httpx responses can be freed."""
        self.responses.clear()
        self._all.clear()
        self._tasks.clear()
        self._retry_tasks.clear()

    @property
    def waiting_v_total(self) -> float:
        return sum(end - start for start, end in self.waiting)


_CUE_RE = re.compile(
    r"^(\d+)\n(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})\n(.+)$",
    re.S,
)


def _stamp_ms(stamp: str) -> int:
    hours, minutes, rest = stamp.split(":")
    seconds, millis = rest.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def parse_srt(text: str) -> list[tuple[int, int, int, str]]:
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        return []
    blocks = re.split(r"\n[ \t]*\n", body)
    cues = []
    for block in blocks:
        piece = block.strip("\n")
        match = _CUE_RE.fullmatch(piece)
        if not match:
            raise ValueError(f"SRT cue does not match the strict pattern: {piece!r}")
        cues.append((int(match.group(1)), _stamp_ms(match.group(2)), _stamp_ms(match.group(3)), match.group(4)))
    return cues


@dataclass
class SimReport:
    segments: int
    room: str
    session: str
    db_path: str
    srt: str
    export_json: list
    metrics: list[dict] = field(default_factory=list)
    waiting: list[tuple[float, float]] = field(default_factory=list)
    waiting_v_total: float = 0.0
    segment_end_mono: dict[int, float] = field(default_factory=dict)
    zh_ready_mono: dict[int, float] = field(default_factory=dict)
    # seq and status only. Push bodies and httpx Response objects stay out of _RUN_CACHE.
    responses: list[dict] = field(default_factory=list)
    final_metrics: dict = field(default_factory=dict)
    results_at: dict[int, int] = field(default_factory=dict)
    emitted: int = 0
    bus_log: int = 0
    bus_by_room: int = 0
    state_count: int = 0
    missing: int = 0
    silent: int = 0
    tracemalloc_500: int = 0
    tracemalloc_750: int = 0
    tracemalloc_1000: int = 0
    rss_0: int = 0
    rss_250: int = 0
    rss_500: int = 0
    rss_750: int = 0
    rss_1000: int = 0
    pending_peak: int = 0
    storm_rejects: int = 0
    retries: list[int] = field(default_factory=list)
    translate_skipped: int = 0
    # seq, translate_status, has_en. From caption state, not the export file.
    translate_rows: list = field(default_factory=list)
    translate_queued_at_export: int = 0
    translate_busy_at_export: int = 0
    loop_lag_n: int = 0
    loop_lag_p50_ms: float = 0.0
    loop_lag_p99_ms: float = 0.0
    loop_lag_max_ms: float = 0.0
    loop_lag_over_30ms: int = 0
    loop_lag_stall_ms: float = 0.0
    loop_lag_period_ms: float = 0.0
    loop_lag_events: int = 0
    loop_lag_event_max_ms: float = 0.0
    loop_lag_load: tuple | None = None
    loop_lag_hold_n: int = 0
    loop_lag_hold_sum_ms: float = 0.0
    loop_lag_hold_max_ms: float = 0.0
    clock_installed: bool = False
    clock_monotonic_patched: bool = False
    clock_perf_patched: bool = False
    clock_virtual_s: float = 0.0
    clock_paced_s: float = 0.0
    clock_went_backward: bool = True
    asr_virtual_median_s: float = 0.0
    asr_clock_sleeps: int = 0


def _zh_ready_times(listeners: list[Listener]) -> dict[int, float]:
    found: dict[int, float] = {}
    for listener in listeners:
        for msg in listener.messages:
            if msg.get("status") != "zh_ready" or msg.get("seq") is None:
                continue
            seq = int(msg["seq"])
            mono = float(msg.get("_recv_mono") or 0)
            if seq not in found or mono < found[seq]:
                found[seq] = mono
    return found


def caption_rows(app, room: str) -> list[dict]:
    """This branch's caption_state. The spec's main only keeps the live by_room list."""
    bus = app.state.bus
    state = getattr(bus, "caption_state", None)
    if state is not None:
        return list(state(room))
    return [dict(item) for item in bus.by_room.get(room, [])]


def _translations_idle(pipe) -> bool:
    queued = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
    return pipe._translate_busy <= 0 and queued == 0


async def _wait_translate_idle(pipe, timeout: float = 5.0) -> None:
    """Wait until no English is queued or in a worker.

    The admit slot is already released at Chinese, so this is not a recorder
    pause. Callers that then hold the loop (gc, tracemalloc) need the wait:
    the scaled translate budget is a few hundred real milliseconds, and a
    longer hold stores that line as a timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if _translations_idle(pipe):
            # A deferred queue offer is a call_soon. Let it land, then recheck.
            await asyncio.sleep(0)
            if _translations_idle(pipe):
                return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.005)


async def sample_after_translations(pipe, sample):
    """Run sample() only after in-flight English has landed.

    sample() may stop the loop. On Python 3.11 a tracemalloc walk of this
    process is longer than the scaled 40s translate budget, so a line the
    worker has already started would be published with no English.
    """
    await _wait_translate_idle(pipe)
    return sample()


async def _wait_translations(app, translator, count: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        stats = app.state.pipeline.stats()
        done = len(getattr(translator, "finished", []))
        # _translate_busy is this branch's worker counter. Main has no such attribute;
        # the queue size is enough to know the scripted translator has finished.
        busy = getattr(app.state.pipeline, "_translate_busy", 0)
        # A full translate queue drops the oldest line. That line never calls
        # the translator, so it is not in ``finished``; it still left the queue.
        skipped = int(getattr(app.state.pipeline, "translate_skipped", 0) or 0)
        if done + skipped >= count and stats.get("translate_queued", 0) == 0 and busy <= 0:
            return
        await asyncio.sleep(0.01)


def _seq_of(zh: str) -> int:
    digits = "".join(ch for ch in zh if ch.isdigit())
    return int(digits) if digits else 0


def _app_traced_bytes() -> int:
    """Live bytes allocated from app/. The sim client is not included."""
    if not tracemalloc.is_tracing():
        return 0
    root = (Path(__file__).resolve().parents[1] / "app").resolve()
    total = 0
    for stat in tracemalloc.take_snapshot().statistics("filename"):
        raw = stat.traceback[0].filename
        if not raw or raw.startswith("<"):
            continue
        filename = Path(raw).resolve()
        if filename == root or root in filename.parents:
            total += stat.size
    return total


def _sample_server_memory() -> tuple[int, int]:
    """One collection with GC enabled, then server heap and process RSS.

    The paced run leaves automatic GC off so a collection cannot be booked as
    a recorder pause. This turns it on for the sample only.
    """
    was = gc.isenabled()
    gc.enable()
    gc.collect()
    traced = _app_traced_bytes()
    rss = rss_bytes()
    if not was:
        gc.disable()
    return traced, rss


def _class_plan(zh: str):
    """2s English, except segments 300-330, which take almost the whole 40s budget.

    Slow is not a timeout. The translator finishes one deadline margin early,
    so those lines come back in English unless the queue drops them. A timeout
    in that window is a harness failure, not the backlog the test is measuring.
    """
    seq = _seq_of(zh)
    if 300 <= seq <= 330:
        return ("ok", 40.0)
    return ("ok", 2.0)


async def _run_100min_async(*, trace: bool) -> SimReport:
    # Wall time under SCALE is not the class clock. See _ScaledClock.
    clock = _ScaledClock()
    clock.install()
    try:
        return await _run_100min_impl(trace=trace)
    finally:
        clock.close()


async def _run_100min_impl(*, trace: bool) -> SimReport:
    room = "class"
    session = "sim100"
    root = Path(tempfile.mkdtemp(prefix="breeze-sim-"))
    db_path = root / "class.sqlite3"
    translator = ScriptedTranslator(_class_plan)
    observed = {"pipe": None, "pending": 0}

    def on_start() -> None:
        pipe = observed["pipe"]
        if pipe is None:
            return
        pending = int(pipe.stats()["pending"])
        if pending > observed["pending"]:
            observed["pending"] = pending

    asr = TextAsr(1.5, on_start=on_start)
    settings = sim_settings(data_path=str(db_path))
    app = create_app(settings, asr=asr, translator=translator, decoder=copy_decoder)
    observed["pipe"] = app.state.pipeline
    # At segment 600 the next 16 admits fail once each. The following admit
    # (the host's single retry) is let through. rejected counts those 429s.
    storm = {"rounds": 0, "let_pass": False, "rejects": 0}
    pipe = app.state.pipeline
    orig_admit = pipe.try_admit_count

    def storm_admit() -> bool:
        if storm["let_pass"]:
            storm["let_pass"] = False
            return orig_admit()
        if storm["rounds"] > 0:
            storm["rounds"] -= 1
            storm["let_pass"] = True
            pipe.rejected += 1
            storm["rejects"] += 1
            return False
        return orig_admit()

    pipe.try_admit_count = storm_admit
    # tracemalloc walks the whole process. On a 100-minute class that walk is
    # the stall, so the latency run leaves it off. The memory run opts in.
    started_trace = False
    if trace and not tracemalloc.is_tracing():
        tracemalloc.start()
        started_trace = True
    # Host, server and both listeners share one process here, and every real
    # millisecond is 1/SCALE virtual milliseconds. A full GC pass is a 30-90 ms
    # stop-the-world pause on a runner (measured on 3.11), which the recorder model
    # would book as a multi-second recorder pause that a browser plus a separate
    # server never see. Freeze what exists before the class, turn automatic GC off
    # for the paced run, and collect at slice boundaries before the slot check,
    # where a pause is not inside any measured wait.
    # A Windows 3.11 run booked 109 real ms (5.45 virtual s at scale 0.02) as
    # waiting and as the same SRT end drift. That is a stall while a slot was
    # still held, not a recorder backlog. Collecting before the slot check keeps
    # the pause out of the measured wait; the zero-wait and ±2s SRT bounds stay exact.
    # The same hold is longer than the scaled translate budget, so the sample
    # runs only after in-flight English has landed (sample_after_translations).
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.freeze()
    gc.disable()
    mem_at: dict[int, tuple[int, int]] = {}

    def collect_between_slices(seq: int):
        # The upload for seq-1 is created at the end of the previous slice and
        # admits on the next yield. Arming here makes that upload the first 429.
        # A plain return does not yield, so the upload cannot admit first.
        if seq == 601:
            storm["rounds"] = 16
        # 250 is an RSS point on the latency run only. The traced run still
        # collects here, but does not snapshot: that walk is the stall.
        rss_points = (250, 500, 750) if not trace else (500, 750)
        if seq not in rss_points and seq % 50 != 0:
            return None
        # The upload created at the end of the previous slice has not run yet.
        # Park it so the sample is not inside its Chinese latency, then let any
        # English already in flight land before the loop is held.

        async def _pause_for_sample() -> None:
            assert host is not None
            host.hold_new_slices()
            clock = _ACTIVE_CLOCK
            probe = None if clock is None else clock.probe
            try:
                if probe is not None:
                    probe.begin_hold()
                try:
                    if seq in rss_points:
                        mem_at[seq] = await sample_after_translations(pipe, _sample_server_memory)
                    else:
                        await sample_after_translations(pipe, gc.collect)
                finally:
                    if probe is not None:
                        probe.end_hold()
                    if clock is not None:
                        clock.sync_real()
            finally:
                host.release_new_slices()

        return _pause_for_sample()
    snapshots: list[dict] = []
    listeners: list[Listener] = []
    host: VirtualHost | None = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=30) as client:
            token = await token_of(app, client)
            await open_room(client, token, room)
            for _ in range(2):
                listener = Listener(app, room)
                await listener.__aenter__()
                listeners.append(listener)
            # Warm the push path once in a separate room before the class: first form
            # parse, decode and ASR threads, translate pool, store writer. That one-off
            # cost is tens of real ms on a CI runner (3.11 showed a 2.35 virtual s wait
            # at the very first slice only); a real server is warm long before the first
            # 6 s slice, and the scaled clock would magnify it 1/SCALE times.
            await open_room(client, token, "warmup")
            warm = await post_segment(client, token, "warmup", "warmup", 1, "預熱".encode(), 0, 6000)
            assert warm.status_code == 200, warm.text
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(getattr(translator, "finished", [])) < 1:
                await asyncio.sleep(0.01)
            # Baseline RSS for the latency run, after warmup, before segment 1.
            # The traced run does not sample here: a snapshot walk is the stall.
            if not trace:
                mem_at[0] = await sample_after_translations(pipe, _sample_server_memory)
            host = VirtualHost(client, token, room, session)
            pending_snaps: list[asyncio.Task] = []

            def on_each(seq: int) -> None:
                if seq % 100 != 0:
                    return
                pending_snaps.append(asyncio.create_task(_snapshot(app, client, token, seq, snapshots)))

            clock = _ACTIVE_CLOCK
            probe = None if clock is None else clock.probe
            if probe is not None:
                probe.enter_stable()
            try:
                await host.run(SEGMENTS, pace=True, on_each=on_each, before_slice=collect_between_slices)
            finally:
                if probe is not None:
                    probe.leave_stable()
            clock_installed = clock is not None and clock.active and _ACTIVE_CLOCK is clock
            # Bound methods are new objects on each lookup; identity is the instance.
            clock_monotonic_patched = getattr(time.monotonic, "__self__", None) is clock
            clock_perf_patched = getattr(time.perf_counter, "__self__", None) is clock
            clock_virtual_s = (clock.now / SCALE) if clock is not None and SCALE else 0.0
            clock_paced_s = host.clock_ms / 1000.0
            clock_went_backward = True if clock is None else clock._went_backward
            asr_virtual_median_s = percentile(asr.spent_v, 0.50) if asr.spent_v else 0.0
            asr_clock_sleeps = int(asr.clock_sleeps)
            if probe is not None:
                lag_n, lag_p50, lag_p99, lag_max, lag_over = probe.summary_ms()
                lag_stall = float(probe._stall_ms)
                lag_period = float(probe._period) * 1000.0
                lag_events = int(probe._events)
                lag_event_max = float(probe._event_max_ms)
                lag_load = (probe.load_t0, probe.load_t1)
                hold_n, hold_sum, hold_max = probe.hold_summary_ms()
            else:
                lag_n, lag_p50, lag_p99, lag_max, lag_over = 0, 0.0, 0.0, 0.0, 0
                lag_stall = 0.0
                lag_period = 0.0
                lag_events = 0
                lag_event_max = 0.0
                lag_load = None
                hold_n, hold_sum, hold_max = 0, 0.0, 0.0
            if pending_snaps:
                await asyncio.gather(*pending_snaps)
            await _snapshot(app, client, token, SEGMENTS, snapshots)
            # Settle English before the end sample. The sample blocks this thread
            # on a collection and a tracemalloc snapshot; doing that while the last
            # line is still queued trips the translate timeout (40s virtual).
            await _wait_translations(app, translator, SEGMENTS + 1)
            mem_at[1000] = await sample_after_translations(app.state.pipeline, _sample_server_memory)
            if gc_was_enabled:
                gc.enable()
            # flush is this branch's async store. Main writes each row before publish returns.
            flush = getattr(app.state.store, "flush", None)
            if flush is not None:
                await asyncio.to_thread(flush)
            # Sample the queue before export. A line still in flight would be
            # missing English in the file without a finished status yet.
            pipe = app.state.pipeline
            queued_at_export = 0 if pipe._translate_q is None else pipe._translate_q.qsize()
            busy_at_export = int(pipe._translate_busy)
            srt_resp = await client.get("/api/export", params={"room_id": room, "kind": "srt"}, headers=auth(token))
            json_resp = await client.get("/api/export", params={"room_id": room, "kind": "json"}, headers=auth(token))
            assert srt_resp.status_code == 200, srt_resp.text
            assert json_resp.status_code == 200, json_resp.text
            final = (await client.get("/api/metrics", headers=auth(token))).json()
            state = caption_rows(app, room)
            translate_rows = [
                {
                    "seq": int(row.get("seq") or 0),
                    "translate_status": str(row.get("translate_status") or ""),
                    "has_en": bool(row.get("en")),
                }
                for row in state
            ]
            pipe = app.state.pipeline
            _, rss_0 = mem_at.get(0, (0, 0))
            _, rss_250 = mem_at.get(250, (0, 0))
            traced_500, rss_500 = mem_at.get(500, (0, 0))
            traced_750, rss_750 = mem_at.get(750, (0, 0))
            traced_1000, rss_1000 = mem_at.get(1000, (0, 0))
            report = SimReport(
                segments=SEGMENTS,
                room=room,
                session=session,
                db_path=str(db_path),
                srt=srt_resp.text,
                export_json=json_resp.json(),
                metrics=list(snapshots),
                waiting=list(host.waiting),
                waiting_v_total=host.waiting_v_total,
                segment_end_mono=dict(host.segment_end_mono),
                zh_ready_mono=_zh_ready_times(listeners),
                responses=_cached_push_rows(host.responses),
                final_metrics=final,
                results_at={int(item["seq"]): int(item["results"]) for item in snapshots},
                emitted=sum(1 for key in pipe._emitted_segs if key[0] == room),  # the warm-up room is not the class
                bus_log=len(app.state.bus._log.get(room, [])),
                bus_by_room=len(app.state.bus.by_room.get(room, [])),
                state_count=len(state),
                missing=sum(1 for row in state if row.get("status") == "missing"),
                silent=sum(1 for row in state if row.get("status") == "silent"),
                tracemalloc_500=traced_500,
                tracemalloc_750=traced_750,
                tracemalloc_1000=traced_1000,
                rss_0=rss_0,
                rss_250=rss_250,
                rss_500=rss_500,
                rss_750=rss_750,
                rss_1000=rss_1000,
                pending_peak=int(observed["pending"]),
                storm_rejects=int(storm["rejects"]),
                retries=list(host.retries),
                translate_skipped=int(getattr(pipe, "translate_skipped", 0) or 0),
                translate_rows=translate_rows,
                translate_queued_at_export=queued_at_export,
                translate_busy_at_export=busy_at_export,
                loop_lag_n=lag_n,
                loop_lag_p50_ms=lag_p50,
                loop_lag_p99_ms=lag_p99,
                loop_lag_max_ms=lag_max,
                loop_lag_over_30ms=lag_over,
                loop_lag_stall_ms=lag_stall,
                loop_lag_period_ms=lag_period,
                loop_lag_events=lag_events,
                loop_lag_event_max_ms=lag_event_max,
                loop_lag_load=lag_load,
                loop_lag_hold_n=hold_n,
                loop_lag_hold_sum_ms=hold_sum,
                loop_lag_hold_max_ms=hold_max,
                clock_installed=clock_installed,
                clock_monotonic_patched=clock_monotonic_patched,
                clock_perf_patched=clock_perf_patched,
                clock_virtual_s=clock_virtual_s,
                clock_paced_s=clock_paced_s,
                clock_went_backward=clock_went_backward,
                asr_virtual_median_s=asr_virtual_median_s,
                asr_clock_sleeps=asr_clock_sleeps,
            )
            await host.stop()
    finally:
        for listener in listeners:
            await listener.close()
        await stop(app)
        if started_trace and tracemalloc.is_tracing():
            tracemalloc.stop()
        if gc_was_enabled:
            gc.enable()
        if host is not None:
            host.release_upload_results()
        # The report already copied the fields tests read. Drop the class,
        # including upload tasks and their httpx responses, before collecting.
        host = None
        app = None
        translator = None
        asr = None
        pipe = None
        observed = None
        orig_admit = None
        storm_admit = None
        collect_between_slices = None
        on_start = None
        on_each = None
        listeners = []
        client = None
        warm = None
        srt_resp = None
        json_resp = None
        final = None
        state = None
        snapshots = None
        pending_snaps = None
        gc.unfreeze()
        # Outside the paced class. Frees what the report did not keep and
        # resets gen2 before the next test's measured window.
        gc.collect()
    return report


async def _snapshot(app, client, token, seq: int, snapshots: list[dict]) -> None:
    resp = await client.get("/api/metrics", headers=auth(token))
    data = resp.json()
    current = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
    data = dict(data)
    data["seq"] = seq
    data["traced"] = current
    data["results"] = len(app.state.pipeline.results)
    data["log"] = len(app.state.bus._log.get("class", []))
    data["by_room"] = len(app.state.bus.by_room.get("class", []))
    snapshots.append(data)


def run_100min(*, trace: bool = False) -> SimReport:
    """One paced 100-minute class per process. Latency callers leave tracing off.

    trace=True is the memory run: same scale and the same class, plus a heap
    snapshot. It is not the report latency tests read.
    """
    key = (SEGMENTS, SCALE, "100min-v6", bool(trace))
    cached = _RUN_CACHE.get(key)
    if cached is not None:
        return cached
    report = asyncio.run(_run_100min_async(trace=bool(trace)))
    print(format_loop_lag_line(report), file=sys.__stderr__, flush=True)
    _RUN_CACHE[key] = report
    return report


@asynccontextmanager
async def serving(settings=None, asr=None, translator=None, decoder=None):
    app = create_app(
        settings or sim_settings(),
        asr=asr or TextAsr(0),
        translator=translator if translator is not None else Translator(enabled=False),
        decoder=decoder or copy_decoder,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780", timeout=30) as client:
        token = await token_of(app, client)
        try:
            yield app, client, token
        finally:
            await stop(app)


def latencies(report: SimReport) -> list[float]:
    values = []
    for seq, end in report.segment_end_mono.items():
        recv = report.zh_ready_mono.get(seq)
        if recv is None:
            continue
        values.append((recv - end) / SCALE)
    return values


async def export_json(client, token, room: str):
    resp = await client.get("/api/export", params={"room_id": room, "kind": "json"}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def export_srt(client, token, room: str) -> str:
    resp = await client.get("/api/export", params={"room_id": room, "kind": "srt"}, headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.text
