"""Cold BSC safety overlaps inputs; only a reserved attempt consumes its verdict."""
import threading
import time
import statistics

import pytest

from test_memecoin_bsc_fast_lane_live import SUPPLY, _miss, _payload, _signal, live56


def _cold(built):
    lane, ledger, _, sender, _ = built
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    live.inline_facts_fn = lambda m: (18, SUPPLY)
    live.stats_fn = lambda m: lane["dex"].stats(m, network_id="56")
    return lane, ledger, sender, live, mint


def test_safety_overlaps_inputs_but_verdict_is_consumed_after_reservation(live56):
    lane, ledger, sender, live, mint = _cold(live56)
    started = threading.Event()
    consumed = []
    answer = live.cache.cached_answer

    def safety(m, usd=None):
        started.set()
        return True, None

    def facts(m):
        assert started.wait(0.3), "BSC serialized safety after its cold inputs"
        return 18, SUPPLY

    def verdict(m, label):
        pending = ledger.execute("SELECT status,tx_sig FROM memecoin_pending_entries").fetchone()
        assert pending is not None and pending["status"] == "reserved" and pending["tx_sig"] is None
        consumed.append(m)
        return answer(m, label)

    live.inline_safety_fn, live.inline_facts_fn = safety, facts
    live.cache.cached_answer = verdict
    rec = _signal(lane)
    assert rec["parallel_safety"] == "used" and consumed == [mint] and sender.raws, rec


@pytest.mark.parametrize("result,reason", [
    ((False, "honeypot"), "inline_unsafe:honeypot"),
    (TimeoutError("read timeout"), "inline_safety_error:TimeoutError"),
    (RuntimeError("rate limited"), "inline_safety_error:RuntimeError"),
])
def test_parallel_refusal_never_signs_or_caches(live56, result, reason):
    lane, _, sender, live, mint = _cold(live56)

    def safety(m, usd=None):
        if isinstance(result, Exception):
            raise result
        return result

    live.inline_safety_fn = safety
    rec = _signal(lane)
    assert rec.get("parallel_safety") == "used" and rec["reason"] == reason, rec
    assert not sender.raws and live.cache.reason(mint, now=time.time()) == "no_safety_verdict"


def test_failed_inputs_abandon_worker_without_publishing_its_pass(live56):
    lane, _, sender, live, mint = _cold(live56)
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    live.inline_facts_fn = lambda m: (_ for _ in ()).throw(RuntimeError("supply unavailable"))
    rec = _signal(lane)
    assert rec.get("parallel_safety") == "abandoned", rec
    live._safety_future.result(timeout=1)
    assert not sender.raws and live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert all(not key.startswith("_") for key in rec)


def test_parallel_marker_is_persisted_without_changing_old_rows(live56):
    import sqlite3
    from sniper.services import memecoin_bsc_fast_lane as bsc

    lane, _, _, live, _ = _cold(live56)
    with sqlite3.connect(lane["db_path"]) as conn:
        conn.executescript(bsc._LIVE_SCHEMA.replace(", parallel_safety TEXT", ""))
        conn.execute("INSERT INTO bsc_lane_live(reason) VALUES ('old row')")
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    _signal(lane)
    with sqlite3.connect(lane["db_path"]) as conn:
        assert conn.execute("SELECT parallel_safety FROM bsc_lane_live ORDER BY id").fetchall() == [(None,), ("used",)]


def test_overlap_reduces_elapsed_for_identical_fixed_reads(live56):
    lane, _, _, live, _ = _cold(live56)
    start = live._start_safety
    live.generation_fn = lambda: 0.0
    live.max_sends_per_hour = 100
    measurements = {"sequential": [], "parallel": []}
    live.inline_safety_fn = lambda m, usd=None: (time.sleep(0.15) or (True, None))
    live.inline_facts_fn = lambda m: (time.sleep(0.05) or (18, SUPPLY))
    live.stats_fn = lambda m: (time.sleep(0.04) or lane["dex"].stats(m, network_id="56"))
    for _ in range(12):
        for mode in measurements:
            _miss(live, _payload()["mint"], stale=True)
            live.snapshot._last["read_at"] = time.time()
            live._start_safety = start if mode == "parallel" else lambda m, r: None
            rec = _signal(lane)
            assert rec["reason"].startswith("rejected:"), rec
            measurements[mode].append(rec["busy_ms"])
    percentiles = {mode: {"p50": round(statistics.median(values), 1),
                          "p90": round(sorted(values)[10], 1)}
                   for mode, values in measurements.items()}
    print("fixed-read BSC handle milliseconds (12 paired samples):", percentiles)
    assert percentiles["parallel"]["p50"] < percentiles["sequential"]["p50"] * 0.9
    assert percentiles["parallel"]["p90"] < percentiles["sequential"]["p90"] * 0.9


def test_timed_out_worker_is_not_queued_and_next_attempt_uses_sequential_safety(live56):
    lane, _, sender, live, mint = _cold(live56)
    release = threading.Event()
    live.inline_deadline_s = 0.03
    live.inline_safety_fn = lambda m, usd=None: release.wait(1) and (True, None)
    try:
        rec = _signal(lane)
        assert rec.get("parallel_safety") == "used" and rec["reason"] == "inline_unsafe:safety_deadline_exceeded", rec
        assert not sender.raws and live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
        live.inline_deadline_s = 2
        live.inline_safety_fn = lambda m, usd=None: (True, None)
        rec = _signal(lane)
        assert rec["parallel_safety"] == "busy" and sender.raws, rec
    finally:
        release.set()
        if live._safety_future is not None:
            live._safety_future.result(timeout=1)


# ---- MEMECOIN_BSC_FAST_PARALLEL_SAFETY (Gamma LOW 1 on 223c22065): rollback is a setting, default on ----

@pytest.mark.parametrize("raw, on", [(None, True), ("1", True), ("on", True), ("0", False), ("off", False),
                                     ("false", False), ("no", False), (" OFF ", False), ("maybe", True)])
def test_the_knob_is_on_unless_explicitly_off(monkeypatch, raw, on):
    from sniper.settings import memecoin as cfg
    if raw is None:
        monkeypatch.delenv("MEMECOIN_BSC_FAST_PARALLEL_SAFETY", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_BSC_FAST_PARALLEL_SAFETY", raw)
    assert cfg.memecoin_bsc_fast_parallel_safety() is on


def test_with_the_knob_off_the_cold_safety_runs_after_the_inputs_and_still_signs(live56, monkeypatch):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_PARALLEL_SAFETY", "0")
    lane, ledger, sender, live, mint = _cold(live56)
    order = []

    def safety(m, usd=None):
        order.append("safety")
        return True, None

    def facts(m):
        order.append("facts")
        return 18, SUPPLY
    live.inline_safety_fn, live.inline_facts_fn = safety, facts
    rec = _signal(lane)
    assert order[:2] == ["facts", "safety"] and "parallel_safety" not in rec and sender.raws, (order, rec)


def test_known_final_clip_shortage_does_not_wait_for_cold_safety(live56, monkeypatch):
    from sniper.settings import memecoin as cfg
    lane, ledger, sender, live, mint = _cold(live56)
    monkeypatch.setattr(cfg, 'MEMECOIN_TRADE_SIZE_USD', 96.72)
    monkeypatch.setattr(cfg, 'MEMECOIN_MAX_TOTAL_EXPOSURE_USD', 10000.0)
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_REGIME_RISK_SCALE', 1.0)
    live.clip_usd = 96.72
    live.snapshot._last['native_wei'] = 10 ** 17
    live.snapshot._last['read_at'] = time.time()
    started, release = threading.Event(), threading.Event()
    def blocked(m, usd=None):
        started.set()
        release.wait(2)
        return True, None
    live.inline_safety_fn = blocked
    try:
        begin = time.perf_counter()
        rec = _signal(lane)
        elapsed = time.perf_counter() - begin
        assert rec['reason'] == 'float_short', rec
        funding = rec['execution_evidence']['funding_precheck']
        assert funding['approved_clip_usd'] == pytest.approx(96.72)
        assert funding['clip_wei'] == 161200000000000000
        assert rec['execution_evidence']['safety_warmup']['cache_miss_reason'] == 'no_safety_verdict'
        assert elapsed < 0.5, elapsed
        assert started.is_set() and not sender.raws
        assert rec['parallel_safety'] == 'abandoned'
        pending = ledger.execute('SELECT status,tx_sig FROM memecoin_pending_entries').fetchone()
        assert pending['status'] == 'failed' and pending['tx_sig'] is None
        release.set()
        live._safety_future.result(timeout=1)
        assert live.cache.reason(mint, now=time.time()) == 'no_safety_verdict'
        assert rec['safety_path'] == 'inline' and 'safety_ms' not in rec
    finally:
        release.set()


def test_precheck_unknowns_do_not_prove_shortage_and_final_smaller_clip_fits(live56):
    lane, _, _, live, _ = _cold(live56)
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    factory, callbacks = live.hook_factory, []
    def hook(contexts, stats):
        callbacks.append(contexts['56']['native_funding_precheck_fn'])
        return factory(contexts, stats)
    live.hook_factory = hook
    live.snapshot._last['native_wei'] = 10 ** 17
    live.snapshot._last['read_at'] = time.time()
    rec = _signal(lane)
    precheck = callbacks[0]
    original_generation = live.generation_fn
    live.generation_fn = lambda: 0.0
    assert precheck(39.0) is None
    assert precheck(96.72) == 'float_short'
    # The captured snapshot belongs to this attempt, not a later refresh.
    snapshot = next(c.cell_contents for c in precheck.__closure__
                    if isinstance(c.cell_contents, dict) and 'native_wei' in c.cell_contents)
    for value in (None, True, -1, '100000000000000000', float('nan')):
        snapshot['native_wei'] = value
        assert precheck(96.72) is None
    snapshot['native_wei'] = 10 ** 17
    snapshot['read_at'] = time.time() + 1
    assert precheck(96.72) is None
    snapshot['read_at'] = time.time() - live.max_snapshot_age_s - 1
    assert precheck(96.72) is None
    snapshot['read_at'] = time.time()
    live.generation_fn = lambda: snapshot['read_at'] + 1
    assert precheck(96.72) is None
    for generation in (None, True, float("nan"), "generation_inflight"):
        live.generation_fn = lambda: generation
        assert precheck(96.72) is None
    live.generation_fn = lambda: 0.0
    samples = []
    for _ in range(1000):
        begin = time.perf_counter_ns()
        assert precheck(39.0) is None
        samples.append((time.perf_counter_ns() - begin) / 1000)
    print('funding precheck arithmetic microseconds p50/p90:',
          statistics.median(samples), sorted(samples)[900])
    live.generation_fn = original_generation
    snapshot['read_at'] = time.time()
    samples = []
    for _ in range(1000):
        begin = time.perf_counter_ns()
        assert precheck(39.0) is None
        samples.append((time.perf_counter_ns() - begin) / 1000)
    print('funding precheck including existing local generation-read microseconds p50/p90:',
          statistics.median(samples), sorted(samples)[900])
