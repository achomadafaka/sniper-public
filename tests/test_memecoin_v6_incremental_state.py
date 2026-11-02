from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_exit_market_state import compute_state
from sniper.services.memecoin_v6_events import V6FeedCursor, V6MarketEvent, event_identity
from sniper.services.memecoin_v6_incremental_state import (
    PoolAccumulator, PoolMeta, bootstrap_accumulators, rebuild_accumulator,
)
from sniper.services.memecoin_v6_engine import POLICY_ID


BASE = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
NOW = (BASE + timedelta(seconds=100)).isoformat()
POOL = "0x" + "cd" * 20
META = PoolMeta(
    network_id="4663", pool=POOL, mint="0x" + "ab" * 20,
    pool_kind="v2", quote_side=0,
)


def _event(height, amount0, amount1, *, kind="v2_swap", index=0, block_hash=None):
    event_at = (BASE + timedelta(seconds=height)).isoformat()
    return V6MarketEvent(
        network_id="4663", pool=POOL, block_height=height,
        block_hash=block_hash or f"0xblock{height}", tx_id=f"0xtx{height}",
        event_index=index, source_event_at=event_at, received_at=event_at,
        kind=kind, payload={"maker": "0xrouter", "amount0": amount0, "amount1": amount1},
    )


FIXTURE_EVENTS = tuple(
    _event(i, a0, a1)
    for i, (a0, a1) in enumerate(
        (("100", "-50"), ("-100", "60"), ("120", "-50"),
         ("-90", "55"), ("110", "-48"), ("-80", "50")), start=1)
) + (_event(6, "1000000", "2000000", kind="v2_sync", index=1),)


def _expected():
    swaps = []
    block_times = {}
    for event in FIXTURE_EVENTS:
        swaps.append({
            "kind": event.kind, "pool": event.pool, "block_no": event.block_height,
            "tx_sig": event.tx_id, "log_index": event.event_index, **event.payload,
        })
        block_times[event.block_height] = event.source_event_at
    state = compute_state(
        swaps, block_times,
        {"chain_id": META.network_id, "mint": META.mint, "pool": META.pool,
         "pool_kind": META.pool_kind, "position_id": 0, "quote_side": META.quote_side},
        now_ts=datetime.fromisoformat(NOW).timestamp(),
    )
    buy_share = state.buy_n / (state.buy_n + state.sell_n)
    return {
        "price": state.nominal_price, "prior_low": state.prior_low,
        "net": float(state.net_flow_quote), "arrival_rate": state.arrival_rate,
        "buy_share": buy_share, "realized_vol": state.realized_vol,
        "buy_n": state.buy_n, "sell_n": state.sell_n,
        "buy_vol_quote": state.buy_vol_quote, "sell_vol_quote": state.sell_vol_quote,
        "depth_pull_flag": state.depth_pull_flag,
        "liquidity_removed_fraction": state.liquidity_removed_fraction,
        "reserve_quote": state.reserve_quote, "reserve_delta": state.reserve_delta,
    }


def test_incremental_snapshot_matches_bounded_compute_state_fixture():
    acc = PoolAccumulator(META)
    for event in FIXTURE_EVENTS:
        assert acc.apply(event)
    facts = acc.snapshot(NOW)
    assert facts.scored_fields() == _expected()
    assert facts.capture_status == "trending"
    assert facts.tape_complete is True


def test_same_watermark_snapshot_is_cached_and_new_event_invalidates(monkeypatch):
    from sniper.services import memecoin_v6_incremental_state as incremental
    calls = []
    real_compute = incremental.compute_state

    def counted(*args, **kwargs):
        calls.append(True)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(incremental, "compute_state", counted)
    acc = PoolAccumulator(META)
    acc.apply(FIXTURE_EVENTS[0])
    first = acc.snapshot(NOW)
    repeated = acc.snapshot((datetime.fromisoformat(NOW) + timedelta(milliseconds=100)).isoformat())
    assert first is repeated
    assert len(calls) == 1

    acc.apply(FIXTURE_EVENTS[1])
    assert acc.snapshot(NOW) is not first
    assert len(calls) == 2


def test_complete_head_watermark_makes_quiet_pool_explicit_current_evidence():
    checked_at = (BASE + timedelta(seconds=500)).isoformat()
    acc = PoolAccumulator(replace(META, tape_checked_at=checked_at))
    for event in FIXTURE_EVENTS:
        acc.apply(event)

    facts = acc.snapshot(checked_at)

    assert facts.capture_status == "trending"
    assert facts.net == 0.0
    assert facts.arrival_rate == 0.0
    assert facts.realized_vol == 0.0
    assert facts.depth_pull_flag is False
    assert facts.provenance["depth"]["quality"] == "trusted"


def test_incremental_feature_extraction_is_bound_to_v6_identity(monkeypatch):
    from sniper.services import memecoin_v6_incremental_state as incremental
    captured = {}
    real_compute = incremental.compute_state

    def capture(swaps, block_times, contract, **kwargs):
        captured.update(contract)
        return real_compute(swaps, block_times, contract, **kwargs)

    monkeypatch.setattr(incremental, "compute_state", capture)
    acc = PoolAccumulator(META)
    for event in FIXTURE_EVENTS:
        acc.apply(event)
    acc.snapshot(NOW)
    assert captured["engine_version"] == POLICY_ID
    assert "4663" in captured["v3_depth_verified"]


def test_duplicate_event_is_a_noop():
    event = FIXTURE_EVENTS[0]
    acc = PoolAccumulator(META)
    assert acc.apply(event) is True
    assert acc.apply(event) is False
    assert acc.snapshot(NOW).market_evidence_id == event_identity(event)


def test_conflicting_block_hash_marks_gap_without_replacing_canonical_event():
    event = FIXTURE_EVENTS[0]
    acc = PoolAccumulator(META)
    assert acc.apply(event)
    assert acc.apply(replace(event, block_hash="0xreorg")) is False
    assert acc.health.gap is True
    assert acc.snapshot(NOW).tape_complete is False


def test_successful_feed_head_completes_startup_backfill_but_not_integrity_gap():
    startup = PoolAccumulator(replace(META, tape_complete=False))
    cursor = V6FeedCursor("4663", POOL, 20, "head:20")
    assert startup.restore_feed_cursor(cursor, current=True, checked_at=NOW)
    assert startup.health.current is True
    assert startup.meta.tape_checked_at == NOW

    broken = PoolAccumulator(META)
    assert broken.apply(FIXTURE_EVENTS[0])
    assert broken.apply(replace(FIXTURE_EVENTS[0], block_hash="0xreorg")) is False
    assert broken.restore_feed_cursor(cursor, current=True, checked_at=NOW)
    assert broken.health.current is False


def test_missing_source_time_marks_gap_and_required_facts_unknown():
    event = replace(FIXTURE_EVENTS[0], source_event_at=None)
    acc = PoolAccumulator(META)
    assert acc.apply(event)
    facts = acc.snapshot(NOW)
    assert acc.health.gap is True
    assert facts.capture_status == "data_insufficient"
    assert facts.price is None


def test_rebuild_is_deterministic_and_honors_cursor_identity():
    cursor = V6FeedCursor("4663", POOL, 6, event_identity(FIXTURE_EVENTS[-1]))
    rebuilt = rebuild_accumulator(FIXTURE_EVENTS, cursor, META)
    assert rebuilt.snapshot(NOW) == rebuilt.snapshot(NOW)
    assert rebuilt.cursor == cursor


def test_buffer_overflow_fails_closed_instead_of_silently_losing_window_data():
    acc = PoolAccumulator(META, max_events=3)
    for event in FIXTURE_EVENTS[:4]:
        acc.apply(event)
    assert acc.health.gap is True
    assert acc.snapshot(NOW).tape_complete is False


def test_startup_rebuild_reads_bounded_tape_once_and_binds_shared_pool():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at,"
        "token0_decimals,token1_decimals) VALUES(?,?,?,?,?,?,?,?)",
        ("4663", POOL, "0xquote", META.mint, 0, NOW, 18, 18),
    )
    rows = []
    times = {}
    for event in FIXTURE_EVENTS:
        rows.append({
            "chain_id": "4663", "pool": POOL, "kind": event.kind,
            "block_no": event.block_height, "block_hash": event.block_hash,
            "tx_sig": event.tx_id, "log_index": event.event_index,
            "observed_at": event.received_at, **event.payload,
        })
        times[event.block_height] = int(datetime.fromisoformat(event.source_event_at).timestamp())
    db_ticks.insert_ticks(tape, rows)
    db_ticks.backfill_block_times(tape, "4663", times, times.get, NOW)
    positions = [
        {"position_id": "p1", "network_id": "4663", "mint": META.mint},
        {"position_id": "p2", "network_id": "4663", "mint": META.mint},
    ]
    accumulators, bindings = bootstrap_accumulators(positions, {"4663": tape})
    assert len(accumulators) == 1
    assert bindings == {"p1": ("4663", POOL), "p2": ("4663", POOL)}
    facts = accumulators[("4663", POOL)].snapshot(NOW)
    assert facts.scored_fields() == _expected()


def test_startup_bounded_snapshot_does_not_invent_a_live_feed_gap():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at,"
        "token0_decimals,token1_decimals) VALUES(?,?,?,?,?,?,?,?)",
        ("4663", POOL, "0xquote", META.mint, 0, NOW, 18, 18),
    )
    rows, times = [], {}
    for event in FIXTURE_EVENTS:
        rows.append({
            "chain_id": "4663", "pool": POOL, "kind": event.kind,
            "block_no": event.block_height, "block_hash": event.block_hash,
            "tx_sig": event.tx_id, "log_index": event.event_index,
            "observed_at": event.received_at, **event.payload,
        })
        times[event.block_height] = int(datetime.fromisoformat(event.source_event_at).timestamp())
    db_ticks.insert_ticks(tape, rows)
    db_ticks.backfill_block_times(tape, "4663", times, times.get, NOW)

    accumulators, _ = bootstrap_accumulators(
        [{"position_id": "p1", "network_id": "4663", "mint": META.mint}],
        {"4663": tape}, max_events=3,
    )

    assert accumulators[("4663", POOL)].health.current is True

    opened_at = BASE.isoformat()
    db_ticks.record_gap(
        tape, "4663", 1, 10, "deep_catchup_skipped",
        (BASE - timedelta(seconds=1)).isoformat())
    accumulators, _ = bootstrap_accumulators(
        [{"position_id": "p2", "network_id": "4663", "mint": META.mint,
          "opened_at": opened_at}], {"4663": tape}, max_events=3)
    assert accumulators[("4663", POOL)].health.current is True

    db_ticks.record_gap(tape, "4663", 11, 20, "deep_catchup_skipped", NOW)
    accumulators, _ = bootstrap_accumulators(
        [{"position_id": "p3", "network_id": "4663", "mint": META.mint,
          "opened_at": opened_at}], {"4663": tape}, max_events=3)
    assert accumulators[("4663", POOL)].health.current is False


def test_startup_binds_v4_pool_key_without_legacy_pool_meta():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks
    from sniper.services.memecoin_evm_v4_events import v4_pool_id

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    key = {"currency0": "0x" + "00" * 20, "currency1": META.mint,
           "fee": 10_000, "tick_spacing": 200, "hooks": "0x" + "00" * 20}
    pool_id = v4_pool_id(key)
    positions = [{
        "position_id": "p-v4", "network_id": "4663", "mint": META.mint,
        "opened_at": BASE.isoformat(),
        "route_binding": {"protocol": "uniswap_v4", "pool_key": key},
    }]

    accumulators, bindings = bootstrap_accumulators(positions, {"4663": tape})

    assert bindings == {"p-v4": ("4663", pool_id)}
    accumulator = accumulators[("4663", pool_id)]
    assert accumulator.meta.pool_kind == "v3"
    assert accumulator.meta.quote_side == 0
    assert accumulator.health.current is False


def test_startup_rebuild_excludes_pre_fill_legacy_rows_from_v6_evidence():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at,"
        "token0_decimals,token1_decimals) VALUES(?,?,?,?,?,?,?,?)",
        ("4663", POOL, "0xquote", META.mint, 0, NOW, 18, 18),
    )
    tape.execute(
        "INSERT INTO pool_ticks(chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,"
        "observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("4663", POOL, "v2_swap", 0, "legacy", 0, "1", "-1", BASE.isoformat()),
    )
    rows, times = [], {}
    for event in FIXTURE_EVENTS:
        rows.append({
            "chain_id": "4663", "pool": POOL, "kind": event.kind,
            "block_no": event.block_height, "block_hash": event.block_hash,
            "tx_sig": event.tx_id, "log_index": event.event_index,
            "observed_at": event.received_at, **event.payload,
        })
        times[event.block_height] = int(datetime.fromisoformat(event.source_event_at).timestamp())
    db_ticks.insert_ticks(tape, rows)
    db_ticks.backfill_block_times(tape, "4663", times, times.get, NOW)
    opened_at = (BASE + timedelta(seconds=0.5)).isoformat()

    accumulators, _bindings = bootstrap_accumulators([
        {"position_id": "p1", "network_id": "4663", "mint": META.mint,
         "opened_at": opened_at},
    ], {"4663": tape})

    acc = accumulators[("4663", POOL)]
    assert acc.health.current is True
    assert acc.snapshot(NOW).scored_fields() == _expected()


def test_solana_bootstrap_without_a_durable_cursor_is_bound_but_incomplete():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks
    from sniper.services.memecoin_solana_tape import ensure_sol_cursor

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    ensure_sol_cursor(tape)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
        "VALUES('solana','VaultA','MintA','USDC',1,?)", (NOW,))
    tape.execute(
        "INSERT INTO sol_position_pools(position_id,mint,pool,venue_kind,resolved_at) "
        "VALUES(-1,'MintA','VaultA','amm',?)", (NOW,))
    positions = [{
        "position_id": "p1", "capture_position_id": -1,
        "network_id": "solana", "mint": "MintA",
    }]

    accumulators, bindings = bootstrap_accumulators(positions, {"solana": tape})

    assert bindings == {"p1": ("solana", "VaultA")}
    assert accumulators[("solana", "VaultA")].health.current is False
    assert accumulators[("solana", "VaultA")].snapshot(NOW).tape_complete is False


def test_solana_bootstrap_orders_equal_slot_events_by_transaction_identity():
    import sqlite3
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks
    from sniper.services.memecoin_solana_tape import ensure_sol_cursor

    tape = sqlite3.connect(":memory:")
    tape.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(tape)
    db_ticks.ensure_block_times(tape)
    ensure_feature_tables(tape)
    ensure_sol_cursor(tape)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
        "VALUES('solana','VaultA','MintA','USDC',1,?)", (NOW,))
    tape.execute(
        "INSERT INTO sol_position_pools(position_id,mint,pool,venue_kind,resolved_at) "
        "VALUES(-1,'MintA','VaultA','amm',?)", (NOW,))
    for signature, log_index, amount0 in (
            ("a-first-transaction", 5, "10"), ("z-second-transaction", 0, "12")):
        tape.execute(
            "INSERT INTO pool_ticks(chain_id,pool,kind,block_no,tx_sig,log_index,amount0,"
            "amount1,observed_at) VALUES('solana','VaultA','sol_swap',100,?,?,?,'-2',?)",
            (signature, log_index, amount0, NOW),
        )
    db_ticks.backfill_block_times(tape, "solana", [100], lambda _slot: int(BASE.timestamp()), NOW)
    tape.execute(
        "INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before,floor_ts) "
        "VALUES('VaultA','head',?,NULL,NULL)", (NOW,))

    accumulators, _bindings = bootstrap_accumulators([{
        "position_id": "p1", "capture_position_id": -1, "network_id": "solana",
        "mint": "MintA", "opened_at": BASE.isoformat(),
    }], {"solana": tape})

    assert accumulators[("solana", "VaultA")].health.current is True
