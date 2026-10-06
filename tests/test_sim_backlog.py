"""B-a. A paced class must not stall the recorder, and latency must not drift.

The shared run is the host page's opt-in (wait_translation=0, x-breeze-async-translation: 1).
Latency is per segment: listener zh_ready wall time minus the moment that segment's
recording ended, converted with SCALE. It is not "virtual now minus ideal t1", which
would accumulate asyncio delay across 1000 segments.
"""

import pytest

from tests.sim import SEGMENTS, latencies, percentile, run_100min, vlimit


@pytest.fixture(scope="module")
def report():
    return run_100min()


def test_100min_session_never_waits(report):
    """B-a1. ASR 1.5s and a 6s period stay inside maxInflight=2, so the host never waits.

    Segments 600-615 are rejected once each and then accepted on the host's single
    retry. Pending is sampled while a slot is held, not only after the upload returns.
    """
    assert report.waiting_v_total == 0, report.waiting
    assert report.metrics, "expected a metrics snapshot at least every 100 segments"
    assert report.storm_rejects == 16
    assert 1 <= report.pending_peak <= 2
    assert report.retries == list(range(600, 616))
    for snap in report.metrics:
        assert snap["pending"] <= 2
        assert snap["oldest_wait_ms"] / 1000 / report_scale() <= vlimit(15)
        if int(snap["seq"]) < 600:
            assert snap["rejected"] == 0
        elif int(snap["seq"]) >= 700:
            assert snap["rejected"] == report.storm_rejects
        else:
            assert 1 <= snap["rejected"] <= report.storm_rejects
    assert report.final_metrics["missing"] == 0
    assert report.final_metrics["pending"] == 0
    assert report.final_metrics["rejected"] == report.storm_rejects
    assert report.missing == 0
    assert [int(row["seq"]) for row in report.export_json] == list(range(1, SEGMENTS + 1))


def report_scale():
    from tests.sim import SCALE

    return SCALE


def test_100min_latency_no_drift(report):
    """B-a2. Thresholds are the spec's 10/15/20/3. Windows may add 20% and nothing else."""
    values = latencies(report)
    assert len(values) == SEGMENTS
    first = values[:100]
    last = values[-100:]
    p50 = percentile(values, 0.50)
    p95 = percentile(values, 0.95)
    drift = percentile(last, 0.50) - percentile(first, 0.50)
    assert p50 <= vlimit(10)
    assert p95 <= vlimit(15)
    assert max(values) <= vlimit(20)
    assert drift <= vlimit(3)


def test_100min_slow_translation_catches_up(report):
    """Segments 300-330 take the full translate budget. English is caught up after that."""
    assert report.translate_skipped > 0
    assert report.final_metrics["translate_queued"] == 0
    slow = [row for row in report.export_json if 300 <= int(row.get("seq") or 0) <= 330]
    assert len(slow) == 31
    later = [row for row in report.export_json if int(row.get("seq") or 0) >= 450]
    assert len(later) == SEGMENTS - 449
    assert all(row.get("en") for row in later)


def test_100min_structures_bounded(report):
    """B-a3. After segment 600 the live windows stay at their defaults."""
    later = [snap for snap in report.metrics if snap["seq"] >= 600]
    assert later
    for snap in later:
        assert snap["results"] <= 500
        assert snap["held"] <= 32
        assert snap["translate_queued"] <= 4
        assert snap["log"] <= 200
        assert snap["by_room"] <= 200
    at_500 = report.results_at.get(500, report.results_at.get(600, 0))
    for seq, count in report.results_at.items():
        if seq >= 500:
            assert count <= 500
            assert count <= max(at_500, 500)
    assert report.bus_log <= 200
    assert report.bus_by_room <= 200
    # App heap after a collection with GC on, at 500 / 750 / 1000. RSS for those
    # same points is on the report; the client keeps its own buffers, so the bound
    # is the server heap. Growth must flatten, or stay under 5 MB across the last 500.
    assert report.tracemalloc_500 > 0
    early = report.tracemalloc_750 - report.tracemalloc_500
    late = report.tracemalloc_1000 - report.tracemalloc_750
    growth = report.tracemalloc_1000 - report.tracemalloc_500
    assert late <= max(early, 0) / 2 or growth < 5 * 1024 * 1024, (early, late, growth)


def test_emitted_segs_bounded(report):
    """B-a4. _emitted_segs is trimmed with the room caption index (default 5000),
    which is what retranslate still looks up. A 1000-segment class is under that cap,
    so the class keeps one emitted key and one caption-state row per segment.
    The spec's 536 (max_results + held + queue) would drop keys retranslate needs;
    that cap is a product choice, pinned by test_emitted_segment_index_respects_caption_cap.
    """
    assert report.state_count == SEGMENTS
    assert report.emitted == SEGMENTS


@pytest.mark.anyio
async def test_forced_backlog_reports_waiting_and_recovers():
    """B-a5. ASR slower than the 6s cut produces a waiting interval, then every line is exported."""
    from tests.sim import Listener, ScriptedTranslator, TextAsr, VirtualHost, export_json, open_room, serving

    async with serving(asr=TextAsr(9.0), translator=ScriptedTranslator(lambda zh: ("ok", 0.0))) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "backlog")
            await host.run(50)
            assert host.waiting_v_total > 0
            assert host.max_posts <= 2
            assert await listener.wait_for(lambda: len({m.get("seq") for m in listener.captions()}) >= 50, 30)
        rows = await export_json(client, token, "class")
        seqs = [row["seq"] for row in rows]
        assert seqs == list(range(1, 51))
        for snap_pending in (app.state.pipeline.stats()["pending"],):
            assert snap_pending <= 2
        assert app.state.pipeline.stats()["pending"] == 0
        for start, end in host.waiting:
            for row in rows:
                t0 = (row.get("t0_ms") or 0) / 1000
                t1 = (row.get("t1_ms") or 0) / 1000
                overlaps = t0 < end and t1 > start
                assert not overlaps


@pytest.mark.anyio
async def test_429_retry_lands_once():
    """B-a6. max_queue=1. A 4s ASR does not overlap a 6s period, so this host uses a 3.5s
    period: the next upload starts while the previous slot is still held, the retry 0.8s
    later lands, and each seq is exported once.

    Opt-in matches host.html (wait_translation=0, x-breeze-async-translation: 1). On this
    branch the admit slot is released at Chinese, not after English. The one 800ms-scaled
    retry stays in flight, and the recorder does not start another slice while it is
    pending, or that slice would take the only queue slot.
    """
    from tests.sim import Listener, TextAsr, VirtualHost, export_json, open_room, serving, sim_settings

    async with serving(settings=sim_settings(max_queue=1, translate=False), asr=TextAsr(4.0)) as (app, client, token):
        await open_room(client, token, "class")
        async with Listener(app, "class") as listener:
            host = VirtualHost(client, token, "class", "retry", period_v=3.5)
            await host.run(30)
            assert host.retries, "expected at least one 429"
            assert await listener.wait_for(
                lambda: len({m.get("seq") for m in listener.captions() if m.get("zh")}) >= 30,
                40,
            )
            heard = {m.get("seq") for m in listener.captions()}
            assert set(host.retries) <= heard
        rows = await export_json(client, token, "class")
        seqs = [row["seq"] for row in rows]
        assert seqs == list(range(1, 31))
        assert len(seqs) == len(set(seqs))
