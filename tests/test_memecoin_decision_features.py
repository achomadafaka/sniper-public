"""decision_features_v1: what a fast lane already held at its decision, stamped after the outcome
(spec docs/superpowers/specs/2026-10-14-fast-lane-decision-features-design.md). Measurement only."""
import pytest

from sniper.services.memecoin_market_state import lifecycle_for
from sniper.settings import memecoin as cfg


@pytest.mark.parametrize("age,runup,want", [
    (None, None, "unknown"), (0.0, None, "fresh"), (3600.0, None, "fresh"),
    (3600.1, None, "young"), (86400.0, None, "young"), (86400.1, None, "mature"),
])
def test_lifecycle_for_matches_the_classifier_thresholds(age, runup, want):
    assert lifecycle_for(age, runup) == want


def test_a_runup_past_the_post_peak_threshold_after_a_day_is_revival():
    assert lifecycle_for(90000.0, cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT) == "revival"
    assert lifecycle_for(90000.0, cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT - 1) == "mature"


# --- the cached-stats builder and the trace carry ----------------------------------------------

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services import memecoin_decision_features as df
from sniper.services.memecoin_entry_trace import EntryAttemptTrace

AT = "2026-10-14T12:00:00+00:00"
AT_S = datetime.fromisoformat(AT).timestamp()
STATS_FIELDS = ("pair_age_s", "volume_m5_usd", "volume_h1_usd", "liquidity_usd", "stats_age_s")


def _held(stats, fetched_at=1000.0, read_at=1005.0, source="lane_cache"):
    return {"entry": (stats, fetched_at) if stats is not None else None, "read_at": read_at,
            "source": source}


def test_cached_stats_stamp_their_age_a_known_zero_and_the_classifier_lifecycle():
    stats = {"pair_created_at": (AT_S - 7200) * 1000, "volume_m5": 0.0, "volume_h1": 1200.0,
             "liquidity": 40000.0}
    got = df.from_cached_stats(decision_at=AT, lane="rh_fast_lane", held=_held(stats), their_usd=400.0)
    assert got["schema"] == "decision_features_v1" and got["lane"] == "rh_fast_lane"
    assert got["decision_at"] == AT and got["stats_source"] == "lane_cache"
    assert got["pair_age_s"] == 7200.0 and got["pair_age_basis"] == "provider_pair_created_at"
    assert got["volume_m5_usd"] == 0.0 and "volume_m5_usd_reason" not in got   # zero is known
    assert got["volume_h1_usd"] == 1200.0 and got["stats_age_s"] == 5.0
    assert got["lifecycle"] == "young"
    assert got["liquidity_usd"] == 40000.0 and got["liquidity_basis"] == "provider_cached"
    assert got["source_impact_pct"] == 1.0


def test_no_cached_stats_is_null_with_a_reason_never_a_default():
    got = df.from_cached_stats(decision_at=AT, lane="rh_fast_lane", held=_held(None), their_usd=400.0)
    for key in STATS_FIELDS:
        assert got[key] is None and got[key + "_reason"] == "stats_not_cached"
    assert got["lifecycle"] is None and got["lifecycle_reason"] == "stats_not_cached"
    assert got["source_impact_pct"] is None


def test_a_decision_that_never_read_stats_says_so():
    got = df.from_cached_stats(decision_at=AT, lane="rh_fast_lane", held={}, their_usd=400.0)
    for key in STATS_FIELDS:
        assert got[key] is None and got[key + "_reason"] == "stats_not_read"


def test_fields_the_provider_left_out_are_null_with_their_own_reason():
    got = df.from_cached_stats(decision_at=AT, lane="rh_fast_lane",
                               held=_held({"volume_m5": "nan", "liquidity": -1}), their_usd=None)
    assert got["pair_age_s"] is None and got["pair_age_s_reason"] == "not_in_stats"
    assert got["volume_m5_usd"] is None and got["volume_m5_usd_reason"] == "not_in_stats"
    assert got["liquidity_usd"] is None and got["liquidity_usd_reason"] == "not_in_stats"
    assert got["stats_age_s"] == 5.0 and got["source_impact_pct"] is None


def test_past_a_day_without_runup_revival_and_mature_cannot_be_told_apart():
    stats = {"pair_created_at": (AT_S - 90000) * 1000}
    got = df.from_cached_stats(decision_at=AT, lane="rh_fast_lane", held=_held(stats), their_usd=1.0)
    assert got["pair_age_s"] == 90000.0
    assert got["lifecycle"] is None and got["lifecycle_reason"] == "runup_not_held"


def _conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_memecoin_tables(connection)
    return connection


def _ev(**kw):
    return SimpleNamespace(fomo_event_id="rhfast:x", signal_source="rh_fast_lane",
                           network_id="4663", mint="0x" + "ab" * 20, source_user_id="u",
                           side="buy", **kw)


def _trace(conn, ev):
    return EntryAttemptTrace.start(conn, ev, AT, SimpleNamespace(perf_counter=lambda: 1.0))


def _trace_json(conn):
    return json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])


def test_the_capture_is_built_at_finish_with_the_traces_own_decision_time():
    calls, conn = [], _conn()

    def capture(*, decision_at):
        calls.append(decision_at)
        return {"schema": df.SCHEMA, "seen": decision_at}
    tr = _trace(conn, _ev(decision_features=capture, source_evidence={"their_size_usd": 1.0}))
    assert calls == []                                # nothing built before the outcome
    assert "decision_features" not in _trace_json(conn)   # nor in the shell row's JSON
    tr.finish("rejected", "mcap over ceiling")
    assert _trace_json(conn)["decision_features"] == {"schema": df.SCHEMA, "seen": AT}
    assert calls == [AT]


def test_a_raising_capture_keeps_the_trace_and_records_the_error():
    conn = _conn()

    def boom(*, decision_at):
        raise ValueError("bad")
    tr = _trace(conn, _ev())
    tr.note_decision_features(boom)
    assert tr.finish("filled", "filled")
    row = conn.execute("SELECT terminal_status, trace_json FROM memecoin_entry_attempts").fetchone()
    assert row["terminal_status"] == "filled"
    assert json.loads(row["trace_json"])["decision_features"] == {"schema": df.SCHEMA,
                                                                  "error": "ValueError"}


def test_no_capture_writes_no_key_and_a_late_note_is_ignored():
    conn = _conn()
    tr = _trace(conn, _ev())
    tr.finish("rejected", "x")
    tr.note_decision_features(lambda *, decision_at: {"late": True})
    assert "decision_features" not in _trace_json(conn)


# --- 4663 rh lane: native and shared routes ----------------------------------------------------

def test_a_native_route_refusal_carries_the_stats_the_hook_was_given(tmp_path):
    from test_memecoin_rh_fast_live import PAYLOAD, Chain, _accepting_sender, _ledger, _live
    seen, given = [], {}

    class RecordingHook:
        def __init__(self, contexts, stats):
            given["stats"] = stats

        def attempt(self, conn, ev):
            seen.append(ev)
            return {"status": "rejected", "reason": "mcap over ceiling"}
    stats = {"price": 1.0, "volume_m5": 50.0, "liquidity": 1e4}
    live = _live(_ledger(), _accepting_sender(), Chain(), lock_dir=str(tmp_path),
                 hook_factory=RecordingHook)
    live.handle(dict(PAYLOAD), recv_epoch=1000.0, safety_reason=None, stats=(stats, 1000.0))
    assert given["stats"] == stats                      # the hook's view is unchanged
    got = df.resolve(seen[0].decision_features, AT)
    assert got["lane"] == "rh_fast_lane" and got["stats_source"] == "lane_cache"
    assert got["volume_m5_usd"] == 50.0 and got["stats_age_s"] == 5.0   # host clock 1005
    assert got["source_impact_pct"] == pytest.approx(65.3 / 1e4 * 100)


def _shared_live(cache_stats, provider_stats=None):
    from collections import deque
    from sniper.services import memecoin_rh_fast_lane as lane
    from test_memecoin_pons_counter_route import CURVE, MINT, TAKER
    from test_memecoin_rh_source_binding import SIG
    from rh_source_fill_fixture import proof_for
    sig = dict(SIG, mint=MINT, pool=CURVE, src_out=10**24, paid_usd=100,
               route_kind="pons_shared", post_ratio=None)
    sig["source_fill"] = proof_for(sig, venue="pons_v2")
    sig["source_fill"]["source_post"] = dict(
        reason="pons_post_state_unavailable", venue="pons_v2", pool=CURVE, pool_input_atomic="100",
        pool_output_atomic=str(10**24), receipt_routes_complete=True)
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.stopped = False  # production LiveLane.__init__ always initializes operator stop state
    put = []
    live.cache = SimpleNamespace(stats=lambda mint, now: cache_stats,
                                 put_market_stats=lambda mint, stats, now: put.append(stats))
    live.stats_fn = lambda mint: provider_stats
    live.host_clock = lambda: 1005.0
    live.ctx, live.contexts, live.conn = dict(address=TAKER), {}, None
    live.clip_usd, live.fills, live.send_times = 50, 0, deque()
    seen, read = [], []

    def factory(contexts, stats_fn):
        def hook(conn, ev):
            read.append(stats_fn(ev.mint))               # the decision's own read
            seen.append(ev)
            return dict(status="rejected", reason="fixture_only")
        return hook
    live.shared_hook_factory = factory
    assert live._handle_shared(sig, {})["reason"] == "fixture_only"
    return seen[0], read


def test_a_shared_route_records_the_cached_stats_its_decision_read():
    stats = {"volume_m5": 50.0, "liquidity": 1e4}
    ev, read = _shared_live((stats, 1000.0))
    assert read == [stats]                              # the hook's view is unchanged
    got = df.resolve(ev.decision_features, AT)
    assert got["stats_source"] == "lane_cache" and got["stats_age_s"] == 5.0
    assert got["volume_m5_usd"] == 50.0 and got["source_impact_pct"] == 1.0


def test_a_shared_route_cache_miss_records_the_inline_read_or_its_absence():
    stats = {"volume_m5": 7.0}
    ev, read = _shared_live(None, provider_stats=stats)
    got = df.resolve(ev.decision_features, AT)
    assert read == [stats] and got["stats_source"] == "inline_read" and got["stats_age_s"] == 0.0
    ev, read = _shared_live(None, provider_stats=None)
    got = df.resolve(ev.decision_features, AT)
    assert read == [None] and got["volume_m5_usd_reason"] == "stats_not_cached"


# --- PumpSwap migration-observed time ----------------------------------------------------------

def test_only_a_live_migration_records_its_observed_time():
    from sniper.execution.pumpswap_buy import PumpSwapPoolCache
    cache = PumpSwapPoolCache(rpc=None)
    cache.note_migration("PoolA", 1000.5)
    cache.offer_stream("PoolB")                     # preparation / warm path: no migration time
    assert cache.migration_observed_at("PoolA") == 1000.5
    assert cache.migration_observed_at("PoolB") is None


def test_the_migration_record_is_bounded_like_the_pool_store():
    from sniper.execution.pumpswap_buy import PumpSwapPoolCache
    cache = PumpSwapPoolCache(rpc=None, store_cap=2)
    for i, pool in enumerate(("A", "B", "C")):
        cache.note_migration(pool, float(i))
    assert cache.migration_observed_at("A") is None
    assert (cache.migration_observed_at("B"), cache.migration_observed_at("C")) == (1.0, 2.0)


def test_a_queued_migration_keeps_the_time_it_was_heard_not_the_drain_time(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from sniper.services import memecoin_slot_graduation as grad
    from sniper.util import clock
    import threading
    real_thread = threading.Thread
    targets, noted = {}, []

    class Thread:
        def __init__(self, *, target, name, daemon):
            targets[name] = target

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    monkeypatch.setattr(grad, "mode", lambda: "off")
    now = [1000.0]
    monkeypatch.setattr(clock, "real_utc_now", lambda path=None: now[0])

    class Stop(BaseException):
        pass

    def note(pool, observed_at):
        noted.append((pool, observed_at))
        raise Stop()
    pools = SimpleNamespace(stats={}, offer_stream=lambda pool: None, note_migration=note)

    def signal(*_, on_migration, **kw):
        on_migration("early-pool")
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_curve": object(), "pump_amm": SimpleNamespace(pools=pools)},
                         signal_start_fn=signal, migration_on_signal=True, sol_usd_fn=lambda: None,
                         log=lambda _: None, roster_path="unused", conn_factory=object,
                         readonly_conn_factory=object)
    now[0] = 2000.0                                 # the consumer drains later
    done = []

    def drain():                                    # a broken wiring blocks on the queue: bounded
        try:
            targets["slot-lane-migrations"]()
        except Stop:
            done.append(True)
    worker = real_thread(target=drain, daemon=True)
    worker.start()
    worker.join(5.0)
    assert done and noted == [("early-pool", 1000.0)]


# --- Solana slot lane --------------------------------------------------------------------------

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _slot_buy(**kw):
    base = dict(venue="pump_curve", pool=None, mint="M", quote_mint=None,
                virtual_sol_reserves=40 * 10**9, virtual_token_reserves=8 * 10**14,
                real_sol_reserves=10 * 10**9, real_token_reserves=5 * 10**14)
    base.update(kw)
    return SimpleNamespace(**base)


def _slot(buy, pool_facts=None, migrated=None, their_usd=150.0):
    return df.from_slot_buy(decision_at=AT, buy=buy, their_usd=their_usd, sol_usd=150.0,
                            pool_facts=pool_facts, migration_observed_at=migrated)


def test_a_curve_buy_stamps_its_post_trade_reserves_and_no_pair_age():
    got = _slot(_slot_buy())
    assert got["lane"] == "slot_lane" and got["venue"] == "pump_curve"
    assert got["curve_real_quote_atomic"] == 10 * 10**9 and got["curve_virtual_token_atomic"] == 8 * 10**14
    assert (got["quote_mint"], got["quote_decimals"]) == (WSOL, 9)
    assert got["liquidity_usd"] == 1500.0 and got["liquidity_basis"] == "curve_post_trade_reserves"
    assert got["pair_age_s"] is None and got["pair_age_s_reason"] == "not_in_event"
    assert got["lifecycle"] is None and got["lifecycle_reason"] == "not_in_event"
    for key in ("volume_m5_usd", "volume_h1_usd", "stats_age_s"):
        assert got[key] is None and got[key + "_reason"] == "not_held"
    assert got["source_impact_pct"] == 10.0


def test_a_non_sol_curve_is_never_priced_at_sol():
    got = _slot(_slot_buy(virtual_sol_reserves=0, quote_mint="QuoteX"))
    assert got["quote_mint"] == "QuoteX" and got["quote_decimals"] is None
    assert got["liquidity_usd"] is None and got["liquidity_usd_reason"] == "quote_usd_not_held"
    assert got["source_impact_pct"] is None


def test_a_pumpswap_buy_after_a_live_migration_has_a_pair_age():
    pool = {"quote_mint": WSOL, "quote_decimals": 9, "virtual_quote": 5 * 10**9}
    got = _slot(_slot_buy(venue="pump_amm", pool="P", virtual_sol_reserves=None,
                          virtual_token_reserves=None, real_sol_reserves=20 * 10**9,
                          real_token_reserves=9 * 10**14), pool, migrated=AT_S - 120.0)
    assert got["pool_quote_reserve_atomic"] == 20 * 10**9 and got["pool_base_reserve_atomic"] == 9 * 10**14
    assert got["pool_virtual_quote_atomic"] == 5 * 10**9
    assert got["liquidity_usd"] == 6000.0           # 2 x real quote x SOL USD; virtual never added
    assert got["liquidity_basis"] == "pool_pre_trade_reserves"
    assert got["pair_age_s"] == 120.0 and got["pair_age_basis"] == "migration_observed"
    assert got["lifecycle"] == "fresh"


def test_a_usdc_pool_is_priced_at_one_dollar():
    pool = {"quote_mint": USDC, "quote_decimals": 6, "virtual_quote": 0}
    got = _slot(_slot_buy(venue="pump_amm", pool="P", real_sol_reserves=3000 * 10**6), pool)
    assert got["liquidity_usd"] == 6000.0


def test_a_pool_learned_without_a_live_migration_is_null_with_a_reason():
    got = _slot(_slot_buy(venue="pump_amm", pool="P"), {"quote_mint": WSOL, "quote_decimals": 9})
    assert got["pair_age_s"] is None and got["pair_age_s_reason"] == "migration_not_observed"
    assert got["pool_virtual_quote_atomic"] is None


def test_a_migration_heard_after_the_decision_is_not_a_negative_age():
    got = _slot(_slot_buy(venue="pump_amm", pool="P"), {"quote_mint": WSOL, "quote_decimals": 9},
                migrated=AT_S + 3.0)
    assert got["pair_age_s"] is None and got["pair_age_s_reason"] == "migration_after_decision"


def test_a_non_sol_pool_is_never_priced_at_sol():
    got = _slot(_slot_buy(venue="pump_amm", pool="P"), {"quote_mint": "OtherMint", "quote_decimals": 6})
    assert got["liquidity_usd"] is None and got["liquidity_usd_reason"] == "quote_usd_not_held"


def test_an_uncached_pool_and_another_venue_say_what_is_missing():
    got = _slot(_slot_buy(venue="pump_amm", pool="P"))
    assert got["liquidity_usd"] is None and got["liquidity_usd_reason"] == "quote_usd_not_held"
    other = _slot(_slot_buy(venue="raydium_cpmm"))
    assert other["liquidity_usd_reason"] == "not_held" and other["pair_age_s_reason"] == "not_held"


@pytest.fixture
def ledger(tmp_path):
    connection = sqlite3.connect(tmp_path / "ledger.db")
    connection.row_factory = sqlite3.Row
    ensure_memecoin_tables(connection)
    connection.commit()
    yield connection
    connection.close()


def _attempt(conn):
    row = conn.execute("SELECT classification_json, trace_json FROM memecoin_entry_attempts").fetchone()
    return json.loads(row["classification_json"]), json.loads(row["trace_json"])


@pytest.mark.parametrize("leg_scale, want", [(0.75, "rejected"), (1.0, "filled")])
def test_slot_lane_attempts_are_stamped_refusals_included(ledger, tmp_path, monkeypatch, leg_scale, want):
    from test_slot_lane_scaled_sol import _buy, _lane
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_slot_lane as lane
    # No own-momentum read: it opens the real ledger on the shared 2-worker pool other tests wait on.
    monkeypatch.setattr(rr, "_start_momentum_read", lambda chain, mint: None)
    ln, _rec = _lane(ledger, tmp_path, monkeypatch, leg_scale=leg_scale, coin_scale=1.0, sol=2.0,
                     shadow=100.0, knob=False)
    result = ln.handle(_buy(venue="pump_curve", real_sol_reserves=10 * 10**9,
                            real_token_reserves=5 * 10**14))
    assert result["status"] == want, result
    if want == "rejected":
        assert result["reason"] == lane.SCALED_CLIP_REASON
    classification, trace = _attempt(ledger)
    features = trace["decision_features"]
    assert features["schema"] == "decision_features_v1" and features["lane"] == "slot_lane"
    assert features["decision_at"] == "2026-09-26T20:30:00+00:00"
    assert features["liquidity_usd"] == 1000.0      # 10 SOL x the lane's $100 SOL price
    assert "decision_features" not in json.dumps(classification)
