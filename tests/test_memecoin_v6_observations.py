"""Task 3 (runtime) — point-in-time V6 observations + capture targets.

The observation adapter freezes facts and NEVER chooses an action: missing facts stay None and the
engine (not the adapter) turns them into a data_insufficient HOLD.
"""
import sqlite3
from types import SimpleNamespace

from sniper.analysis.memecoin_quote_failures import QuoteFailure as TypedQuoteFailure
from sniper.persistence import db_memecoin_v6_paper as v6db
from sniper.services.memecoin_v6_observations import (
    _pool_for_position, build_observation, v6_capture_position_id, v6_capture_positions,
)
from sniper.services.memecoin_quotes import QuoteStale, QuoteStatus, QuoteUnavailable
from sniper.services.memecoin_v6_engine import (
    V6Action, V6PositionState, fingerprint, policy_document, policy_hash, step,
)
from sniper.services.memecoin_v6_incremental_state import PoolAccumulator, PoolMeta
from sniper.services.memecoin_v6_events import V6MarketEvent
from sniper.services.memecoin_v6_position_policy import V6State

NOW = "2026-07-26T12:00:00+00:00"


def _fill(unit_price="0.80", out=9_800_000, quote_out_asset="USDC"):
    return SimpleNamespace(unit_price=unit_price, quote_out_atomic=out,
                           route_binding={"venue": "jupiter"},
                           quote_out_asset=quote_out_asset)


def _position(**over):
    base = {"position_id": "p1", "network_id": "solana", "mint": "MintX",
            "remaining_qty_atomic": 1_000_000,
            "opened_at": "2026-07-26T11:00:00+00:00", "executable_high_water": "0.95"}
    base.update(over)
    return base


def _state():
    return V6PositionState(position_id="p", remaining_qty_atomic=1_000_000, break_state=V6State())


def test_evm_position_prefers_exact_route_market_pool_over_ambiguous_tape_metadata():
    tape = sqlite3.connect(":memory:")
    position = _position(
        network_id="4663", mint="0x" + "aa" * 20,
        route_binding={"protocol": "uniswap_v3", "tokens": ["0x" + "bb" * 20,
                       "0x" + "aa" * 20], "fees": [10_000],
                       "market_pool": "0x" + "11" * 20},
    )

    assert _pool_for_position(tape, position) == "0x" + "11" * 20


def _full_facts(**over):
    facts = {"prior_low": 0.90, "net": -1.0, "arrival_rate": 0.2, "buy_share": 0.5,
             "realized_vol": 0.05, "chain_vol_p90": 0.10, "source_wallet_exited": False,
             "wallet_tier_adjustment": 0, "drawdown": 0.1, "market_quote_asset": "USDC"}
    facts.update(over)
    return lambda position, **kw: dict(facts)


def test_full_facts_make_a_scorable_observation():
    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=lambda *a, **k: _fill(), facts_fn=_full_facts())
    assert obs.observed_at == NOW and obs.network_id == "solana"
    assert obs.payload["executable_price"] == 0.80
    assert obs.payload["arrival_rate"] == 0.2 and obs.payload["prior_low"] == 0.90
    assert obs.payload["is_break"] is True         # price 0.80 < prior_low 0.90 and net < 0
    assert obs.payload["route_degraded"] is False and obs.payload["sellable"] is True
    assert obs.payload["executable_high_water"] == 0.95
    assert obs.payload["drawdown"] == 1.0 - 0.8 / 0.95
    assert obs.payload["missing_fields"] == []
    decision = step(_state(), obs)
    assert not any(r.startswith("data_insufficient") for r in decision.reason_codes)
    assert decision.action in (V6Action.DERISK, V6Action.EXIT_FULL)


def test_missing_facts_hold_data_insufficient():
    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=lambda *a, **k: _fill(), facts_fn=lambda *a, **k: {})
    assert obs.payload["prior_low"] is None and obs.payload["arrival_rate"] is None
    assert obs.payload["missing_fields"]           # adapter surfaces the gap, chooses nothing
    decision = step(_state(), obs)
    assert decision.action is V6Action.HOLD
    assert "data_insufficient:is_break" in decision.reason_codes


def test_quote_failure_marks_route_degraded_and_holds():
    def _boom(*a, **k):
        raise QuoteUnavailable("no route", failure=TypedQuoteFailure.NO_ROUTE)

    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=_boom, facts_fn=_full_facts())
    assert obs.payload["executable_price"] is None
    assert obs.payload["route_degraded"] is True and obs.payload["sellable"] is False
    assert obs.payload["quote_status"] == QuoteStatus.ROUTE_ABSENT.value
    assert obs.payload["quote_failure_code"] == "no_route"
    decision = step(_state(), obs)
    assert decision.action is V6Action.HOLD
    by_rule = {result.rule_id: result.status.value for result in decision.rule_results}
    assert by_rule["catastrophe"] == "insufficient"
    assert by_rule["route_loss"] == "not_triggered"


def test_provider_failure_does_not_promote_an_early_break_to_full_exit():
    def _provider_down(*_args, **_kwargs):
        raise QuoteUnavailable("rate limited", failure=TypedQuoteFailure.RATE_LIMIT)

    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=_provider_down,
        facts_fn=_full_facts(
            price=0.80, prior_low=0.90, net=-1.0,
            arrival_rate=0.0, buy_share=0.0, realized_vol=0.01,
        ),
    )

    assert obs.payload["quote_status"] == QuoteStatus.PROVIDER_UNAVAILABLE.value
    assert obs.payload["route_degraded"] is False
    assert step(_state(), obs).action is V6Action.DERISK


def test_stale_quote_is_not_route_degradation():
    def _stale(*_args, **_kwargs):
        raise QuoteStale("cached quote age exceeded")

    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=_stale, facts_fn=_full_facts(),
    )

    assert obs.payload["quote_status"] == QuoteStatus.STALE.value
    assert obs.payload["route_degraded"] is False


def test_observation_does_not_freeze_the_live_quote_clock():
    def _quote(position, qty, **kwargs):
        assert "now_fn" not in kwargs
        return _fill()

    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=_quote, facts_fn=_full_facts())
    assert obs.payload["sellable"] is True


def test_observation_cutoff_includes_the_quote_observation_time():
    quote_time = "2026-07-26T12:00:02+00:00"
    seen = []
    fill = SimpleNamespace(unit_price="0.80", quote_out_atomic=9_800_000,
                           route_binding={"venue": "jupiter"}, observed_at=quote_time,
                           quote_age_s=2.0)

    def _facts(position, *, now_iso, **kwargs):
        seen.append(now_iso)
        return _full_facts()(position)

    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=lambda *a, **k: fill, facts_fn=_facts)
    assert obs.observed_at == quote_time
    assert seen == [quote_time]


def test_is_break_false_when_price_above_prior_low():
    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                            quote_fn=lambda *a, **k: _fill(unit_price="0.95"),
                            facts_fn=_full_facts(prior_low=0.90))
    assert obs.payload["is_break"] is False        # 0.95 >= 0.90


def test_no_confirmed_low_is_explicitly_no_break_when_price_and_flow_are_known():
    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.95"),
        facts_fn=_full_facts(prior_low=None, net=0.0),
    )

    assert obs.payload["is_break"] is False
    assert "is_break" not in obs.payload["missing_fields"]


def test_observation_id_is_deterministic():
    a = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                          quote_fn=lambda *a, **k: _fill(), facts_fn=_full_facts())
    b = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                          quote_fn=lambda *a, **k: _fill(), facts_fn=_full_facts())
    assert a.observation_id == b.observation_id
    assert a.input_fingerprint == b.input_fingerprint


def test_worker_wake_time_does_not_change_semantic_observation_id():
    facts = _full_facts(block_identity="sig-1:3", capture_version="sol-v2", pool="PoolA")
    a = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=None,
                          quote_fn=lambda *a, **k: _fill(), facts_fn=facts)
    b = build_observation(_position(), now_iso="2026-07-26T12:00:00.100+00:00",
                          tape_conn=None, state_conn=None,
                          quote_fn=lambda *a, **k: _fill(), facts_fn=facts)
    assert a.evidence.market == b.evidence.market
    assert a.observation_id == b.observation_id


def test_repeated_market_state_gets_a_new_id_after_an_intervening_transition():
    facts = _full_facts(block_identity="sig-A:3", capture_version="sol-v2", pool="PoolA")
    first = build_observation(
        _position(observation_sequence=1), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(), facts_fn=facts,
    )
    repeated_later = build_observation(
        _position(observation_sequence=3), now_iso="2026-07-26T12:00:02+00:00",
        tape_conn=None, state_conn=None, quote_fn=lambda *a, **k: _fill(), facts_fn=facts,
    )

    assert first.evidence == repeated_later.evidence
    assert first.observation_id != repeated_later.observation_id


def test_same_unconsumed_sequence_keeps_identical_wakes_idempotent():
    facts = _full_facts(block_identity="sig-A:3", capture_version="sol-v2", pool="PoolA")
    first = build_observation(
        _position(observation_sequence=1), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(), facts_fn=facts,
    )
    retry = build_observation(
        _position(observation_sequence=1), now_iso="2026-07-26T12:00:00.100+00:00",
        tape_conn=None, state_conn=None, quote_fn=lambda *a, **k: _fill(), facts_fn=facts,
    )

    assert first.observation_id == retry.observation_id


def test_identical_healthy_quote_refresh_is_not_new_semantic_evidence():
    def quote_at(at):
        return SimpleNamespace(
            unit_price="0.80", quote_out_atomic=9_800_000,
            quote_out_asset="USDC", quote_provider="jupiter",
            route_binding={"venue": "jupiter", "amm_keys": ["PoolA"]},
            quote_fingerprint="provider-payload-changes-with-each-call",
            observed_at=at, quote_age_s=0.0,
        )

    first = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: quote_at(NOW), facts_fn=_full_facts())
    refreshed = build_observation(
        _position(), now_iso="2026-07-26T12:00:05+00:00", tape_conn=None,
        state_conn=None,
        quote_fn=lambda *a, **k: quote_at("2026-07-26T12:00:05+00:00"),
        facts_fn=_full_facts())

    assert first.evidence.quote == refreshed.evidence.quote


def test_distinct_catastrophe_quote_checks_confirm_identical_economics():
    def quote_at(at):
        return SimpleNamespace(
            unit_price="0.10", quote_out_atomic=100_000,
            quote_out_asset="USDC", quote_provider="jupiter",
            route_binding={"venue": "jupiter", "amm_keys": ["PoolA"]},
            quote_fingerprint="same-economics", observed_at=at, quote_age_s=0.0,
        )

    position = _position(entry_unit_price="1.0")
    facts = _full_facts(
        prior_low=0.05, net=0.0, arrival_rate=0.0, buy_share=0.5,
        market_quote_asset="USDC")
    first = build_observation(
        position, now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: quote_at(NOW), facts_fn=facts)
    replay = build_observation(
        position, now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: quote_at(NOW), facts_fn=facts)
    second = build_observation(
        position, now_iso="2026-07-26T12:00:05+00:00", tape_conn=None,
        state_conn=None,
        quote_fn=lambda *a, **k: quote_at("2026-07-26T12:00:05+00:00"), facts_fn=facts)

    assert first.evidence.quote == replay.evidence.quote
    assert first.evidence.quote != second.evidence.quote
    state = V6PositionState(
        position_id="p1", remaining_qty_atomic=1_000_000,
        entry_unit_price="1.0", entry_quote_asset="USDC", break_state=V6State())
    pending = step(state, first)
    confirmed = step(pending.state_if_filled, second)
    assert pending.action is V6Action.HOLD
    assert confirmed.action is V6Action.EXIT_FULL
    assert "catastrophe" in confirmed.reason_codes


def test_identical_quote_becomes_semantic_when_time_cap_crosses():
    position = _position(opened_at="2026-07-26T00:00:00+00:00")
    before = build_observation(
        position, now_iso="2026-07-26T11:59:59+00:00", tape_conn=None,
        state_conn=None, quote_fn=lambda *a, **k: _fill(), facts_fn=_full_facts())
    expired = build_observation(
        position, now_iso="2026-07-26T12:00:00+00:00", tape_conn=None,
        state_conn=None, quote_fn=lambda *a, **k: _fill(), facts_fn=_full_facts())

    assert before.evidence.quote != expired.evidence.quote


def test_new_block_identity_changes_market_evidence_and_observation_id():
    a = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(),
        facts_fn=_full_facts(block_identity="sig-1:3", capture_version="sol-v2", pool="PoolA"),
    )
    b = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(),
        facts_fn=_full_facts(block_identity="sig-1:4", capture_version="sol-v2", pool="PoolA"),
    )
    assert a.evidence.market != b.evidence.market
    assert a.observation_id != b.observation_id


def test_same_mint_positions_get_distinct_observation_ids():
    a = build_observation(_position(position_id="p1"), now_iso=NOW, tape_conn=None,
                          state_conn=None, quote_fn=lambda *a, **k: _fill(),
                          facts_fn=_full_facts())
    b = build_observation(_position(position_id="p2"), now_iso=NOW, tape_conn=None,
                          state_conn=None, quote_fn=lambda *a, **k: _fill(),
                          facts_fn=_full_facts())
    assert a.observation_id != b.observation_id


# ---- default facts reader: point-in-time market-state -----------------------------------------

def _state_db(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE memecoin_exit_market_state (
        id INTEGER PRIMARY KEY, position_id INTEGER, chain_id TEXT, mint TEXT, pool TEXT,
        engine_version TEXT, feature_version TEXT, computed_at TEXT, source_event_time TEXT,
        freshness_s REAL, state TEXT, nominal_price REAL, executable_price REAL,
        executable_peak REAL, drawdown REAL, drawdown_velocity REAL, realized_vol REAL,
        net_flow_quote TEXT, arrival_rate REAL, arrival_accel REAL, reserve_quote REAL,
        state_json TEXT)""")
    return c


def _insert_state(c, *, mint, computed_at, arrival_rate, net, prior_low):
    import json
    c.execute("INSERT INTO memecoin_exit_market_state (position_id, chain_id, mint, "
              "engine_version, feature_version, computed_at, arrival_rate, realized_vol, "
              "drawdown, net_flow_quote, state_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (1, "solana", mint, "v6", "f1", computed_at, arrival_rate, 0.05, 0.1, str(net),
               json.dumps({"prior_low": prior_low, "buy_share": 0.5, "chain_vol_p90": 0.1})))
    c.commit()


def test_default_facts_reads_point_in_time_and_ignores_future_rows(tmp_path):
    p = str(tmp_path / "state.sqlite")
    c = _state_db(p)
    _insert_state(c, mint="MintX", computed_at="2026-07-26T11:30:00+00:00",
                  arrival_rate=0.33, net=-2.0, prior_low=0.88)         # past (used)
    _insert_state(c, mint="MintX", computed_at="2026-07-26T12:30:00+00:00",
                  arrival_rate=0.99, net=5.0, prior_low=0.50)          # future (ignored)
    obs = build_observation(_position(), now_iso=NOW, tape_conn=None, state_conn=c,
                            quote_fn=lambda *a, **k: _fill())
    assert obs.payload["arrival_rate"] == 0.33     # past row, not the future 0.99
    assert obs.payload["prior_low"] == 0.88
    assert obs.payload["net"] == -2.0


def _evm_tape_with_structure():
    from datetime import datetime, timedelta
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_ticks.ensure_tick_tables(conn)
    db_ticks.ensure_block_times(conn)
    ensure_feature_tables(conn)
    mint = "0x" + "ab" * 20
    pool = "0x" + "cd" * 20
    conn.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at,"
        "token0_decimals,token1_decimals) VALUES(?,?,?,?,?,?,?,?)",
        ("4663", pool, "0x" + "ef" * 20, mint, 0, NOW, 18, 18))
    prices = (2.0, 1.5, 1.0, 1.2, 1.4, 1.3, 1.5, 1.6)
    rows = []
    block_times = {}
    start = datetime.fromisoformat(NOW) - timedelta(seconds=10)
    for i, price in enumerate(prices, 1):
        sign = 1 if i % 2 else -1
        rows.append({"chain_id": "4663", "pool": pool, "kind": "v2_swap", "block_no": i,
                     "tx_sig": f"0xtx{i}", "log_index": 0, "maker": "0xrouter",
                     "amount0": str(sign * int(price * 100)), "amount1": str(-sign * 100),
                     "observed_at": NOW})
        block_times[i] = int((start + timedelta(seconds=i)).timestamp())
    rows.append({"chain_id": "4663", "pool": pool, "kind": "v2_sync", "block_no": 8,
                 "tx_sig": "0xsync", "log_index": 1, "maker": None,
                 "amount0": "1000000", "amount1": "2000000", "observed_at": NOW})
    db_ticks.insert_ticks(conn, rows)
    db_ticks.backfill_block_times(conn, "4663", block_times,
                                  lambda block: block_times[block], NOW)
    return conn, mint


def test_runtime_facts_are_computed_from_v6_tape_without_a_funded_state_row():
    tape, mint = _evm_tape_with_structure()
    position = _position(position_id="v6-e1", network_id="4663", mint=mint,
                         chain_vol_p90=0.2, source_wallet_exited=False,
                         wallet_tier_adjustment=0)
    obs = build_observation(position, now_iso=NOW, tape_conn={"4663": tape}, state_conn=None,
                            quote_fn=lambda *a, **k: _fill(unit_price="0.80"))
    assert obs.payload["prior_low"] == 1.3          # latest causally confirmed trough
    assert obs.payload["price"] == 1.6          # structural tape units, not executable quote 0.80
    assert obs.payload["executable_price"] == 0.8
    assert obs.payload["buy_share"] == 0.5
    assert obs.payload["chain_vol_p90"] == 0.2
    assert obs.payload["capture_version"] == "v6-direct-v1/exit-v4"
    assert obs.payload["missing_fields"] == []


def test_executable_usdc_price_never_substitutes_for_structural_pool_units():
    facts = _full_facts(price=None, prior_low=1.0, net=-1.0, market_quote_asset="WSOL")
    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.005"), facts_fn=facts)
    assert obs.payload["executable_price"] == 0.005
    assert obs.payload["price"] is None
    assert obs.payload["is_break"] is None
    assert obs.payload["quote_units_comparable"] is False


def test_executable_price_does_not_substitute_when_market_quote_asset_is_unknown():
    facts = _full_facts(price=None, prior_low=0.006, net=-1.0, market_quote_asset=None)
    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.005"), facts_fn=facts)
    assert obs.payload["price"] is None
    assert obs.payload["is_break"] is None
    assert obs.payload["quote_units_comparable"] is None


def test_executable_price_does_not_substitute_when_route_quote_asset_is_unknown():
    facts = _full_facts(price=None, prior_low=0.006, net=-1.0, market_quote_asset="USDC")
    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.005", quote_out_asset=None),
        facts_fn=facts)
    assert obs.payload["price"] is None
    assert obs.payload["is_break"] is None
    assert obs.payload["quote_units_comparable"] is None


def test_executable_usdc_price_can_substitute_for_usdc_mint_pool_units():
    facts = _full_facts(
        price=None, prior_low=0.006, net=-1.0,
        market_quote_asset="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    )
    obs = build_observation(
        _position(), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.005"), facts_fn=facts)
    assert obs.payload["price"] == 0.005
    assert obs.payload["is_break"] is True
    assert obs.payload["quote_units_comparable"] is True


def test_executable_weth_price_can_substitute_for_4663_weth_address_pool_units():
    facts = _full_facts(
        price=None, prior_low=0.006, net=-1.0,
        market_quote_asset="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    )
    obs = build_observation(
        _position(network_id="4663"), now_iso=NOW, tape_conn=None, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.005", quote_out_asset="WETH"),
        facts_fn=facts)
    assert obs.payload["price"] == 0.005
    assert obs.payload["is_break"] is True


def test_direct_tape_observation_excludes_events_after_its_cutoff():
    from datetime import datetime, timedelta
    from sniper.persistence import db_ticks

    tape, mint = _evm_tape_with_structure()
    future = datetime.fromisoformat(NOW) + timedelta(minutes=1)
    db_ticks.insert_ticks(tape, [{
        "chain_id": "4663", "pool": "0x" + "cd" * 20, "kind": "v2_swap",
        "block_no": 99, "tx_sig": "0xfuture", "log_index": 0, "maker": "0xrouter",
        "amount0": "9900", "amount1": "-100", "observed_at": future.isoformat(),
    }])
    db_ticks.backfill_block_times(tape, "4663", {99: int(future.timestamp())},
                                  lambda block: int(future.timestamp()), NOW)
    position = _position(position_id="v6-e1", network_id="4663", mint=mint,
                         chain_vol_p90=0.2, source_wallet_exited=False,
                         wallet_tier_adjustment=0)

    obs = build_observation(position, now_iso=NOW, tape_conn={"4663": tape}, state_conn=None,
                            quote_fn=lambda *a, **k: _fill(unit_price="0.80"))

    assert obs.payload["price"] == 1.6
    assert obs.payload["block_identity"] != "4663:0x" + "cd" * 20 + ":0xfuture:0"


def test_solana_runtime_facts_use_the_v6_capture_namespace_and_own_tape():
    from datetime import datetime, timedelta
    from sniper.persistence import db_ticks
    from sniper.services.memecoin_solana_tape import ensure_sol_cursor

    tape = db_ticks.connect_sol_tape(":memory:")
    ensure_sol_cursor(tape)
    mint, vault, position_id = "MintSol", "VaultSol", "v6-sol-entry"
    capture_id = v6_capture_position_id(position_id)
    tape.execute(
        "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at,"
        "token0_decimals,token1_decimals) VALUES('solana',?,?,?,?,?,?,?)",
        (vault, mint, "So11111111111111111111111111111111111111112", 1, NOW, 9, 9),
    )
    tape.execute(
        "INSERT INTO sol_position_pools(position_id,mint,pool,venue_kind,resolved_at) "
        "VALUES(?,?,?,?,?)", (capture_id, mint, vault, "amm", NOW),
    )
    tape.execute(
        "INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before,floor_ts) "
        "VALUES(?,?,?,NULL,NULL)", (vault, "sig-8", NOW),
    )
    prices = (2.0, 1.5, 1.0, 1.2, 1.4, 1.3, 1.5, 1.6)
    start = datetime.fromisoformat(NOW) - timedelta(seconds=10)
    rows, times = [], {}
    for i, price in enumerate(prices, 1):
        sign = 1 if i % 2 else -1
        rows.append({
            "chain_id": "solana", "pool": vault, "kind": "sol_swap", "block_no": i,
            "tx_sig": f"sig-{i}", "log_index": 0, "maker": None,
            "amount0": str(sign * 100), "amount1": str(-sign * int(price * 100)),
            "observed_at": NOW,
        })
        rows.append({
            "chain_id": "solana", "pool": vault, "kind": "sol_sync", "block_no": i,
            "tx_sig": f"sig-{i}", "log_index": 1, "maker": None,
            "amount0": "1000000", "amount1": "2000000", "observed_at": NOW,
        })
        times[i] = int((start + timedelta(seconds=i)).timestamp())
    db_ticks.insert_ticks(tape, rows)
    db_ticks.backfill_block_times(tape, "solana", times,
                                  lambda block: times[block], NOW)

    position = _position(
        position_id=position_id, capture_position_id=capture_id, network_id="solana", mint=mint,
        chain_vol_p90=0.2, source_wallet_exited=False, wallet_tier_adjustment=0,
    )
    obs = build_observation(
        position, now_iso=NOW, tape_conn={"solana": tape}, state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.80"),
    )

    assert obs.payload["pool"] == vault
    assert obs.payload["capture_status"] == "trending"
    assert obs.payload["tape_complete"] is True
    assert obs.payload["price"] == 1.6
    assert obs.payload["prior_low"] == 1.3
    assert obs.payload["missing_fields"] == []


def test_pool_facts_fast_path_never_reads_the_tape():
    meta = PoolMeta(network_id="4663", pool="0xpool", mint="0xmint",
                    pool_kind="v2", quote_side=0, chain_vol_p90=0.9)
    acc = PoolAccumulator(meta)
    for i, (a0, a1) in enumerate(
            (("100", "-50"), ("-100", "60"), ("120", "-50"),
             ("-90", "55"), ("110", "-48"), ("-80", "50")), start=1):
        event_at = f"2026-07-26T11:59:{49 + i:02d}+00:00"
        acc.apply(V6MarketEvent(
            network_id="4663", pool="0xpool", block_height=i,
            block_hash=f"0xb{i}", tx_id=f"0xt{i}", event_index=0,
            source_event_at=event_at, received_at=event_at, kind="v2_swap",
            payload={"maker": "0xr", "amount0": a0, "amount1": a1},
        ))
    sync_at = "2026-07-26T11:59:55+00:00"
    acc.apply(V6MarketEvent(
        network_id="4663", pool="0xpool", block_height=6, block_hash="0xb6",
        tx_id="0xsync", event_index=1, source_event_at=sync_at, received_at=sync_at,
        kind="v2_sync", payload={"amount0": "1000000", "amount1": "2000000"},
    ))

    class ExplodingTape:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("incremental fast path must not query tape")

    position = _position(position_id="fast-p1", network_id="4663", mint="0xmint",
                         chain_vol_p90=0.2, source_wallet_exited=False)
    obs = build_observation(
        position, now_iso=NOW, tape_conn=ExplodingTape(), state_conn=None,
        quote_fn=lambda *a, **k: _fill(unit_price="0.80"), pool_facts=acc.snapshot(NOW),
    )
    assert obs.payload["capture_version"].startswith("v6-incremental-")
    assert obs.payload["block_identity"] is not None
    assert obs.payload["price"] is not None
    assert obs.payload["chain_vol_p90"] == 0.2


# ---- capture targets --------------------------------------------------------------------------

def _valid_entry(entry_id, network, mint, token_qty=1_000_000):
    doc = policy_document()
    qp = {"in_atomic": 25_000_000, "out_atomic": token_qty}
    return {
        "entry_id": entry_id, "origin_event_id": f"evt-{entry_id}", "network_id": network,
        "mint": mint, "ticker": "PEP", "source_wallet_id": "w", "source_wallet_handle": "@w",
        "signal_source": "fomo", "notional_usd_micros": 25_000_000, "entry_price": "0.000042",
        "quote_in_asset": "USDC", "quote_in_atomic": 25_000_000, "quote_in_decimals": 6,
        "quote_out_asset": mint, "quote_out_atomic": token_qty, "quote_out_decimals": 6,
        "token_qty_atomic": token_qty, "route_target": "raydium", "pool_or_vault": "poolX",
        "quote_id": "q", "quote_provider": "jupiter", "quote_age_s": 1.0,
        "entry_mcap": None, "entry_liquidity": None,
        "signal_ts": NOW, "quote_ts": NOW, "fill_ts": NOW,
        "policy_hash": policy_hash(), "policy_document": doc,
        "quote_payload": qp, "quote_fingerprint": fingerprint(qp),
        "route_binding": {"venue": "jupiter"}, "costs": {"slippage_bps": 300},
        "net_pnl_computable": True,
    }


def test_v6_capture_positions_returns_open_solana_only(tmp_path):
    conn = v6db.connect(str(tmp_path / "v6.sqlite"))
    solana_entry = _valid_entry("s1", "solana", "MintSol")
    solana_entry["route_binding"] = {
        "venue": "jupiter", "route_plan": [{"swapInfo": {
            "ammKey": "TokenPool", "inputMint": "WSOL", "outputMint": "MintSol",
        }}],
    }
    v6db.record_entry_fill(conn, solana_entry)
    v6db.record_entry_fill(conn, _valid_entry("e1", "4663", "0x" + "ab" * 20))
    targets = v6_capture_positions(conn, "solana")
    assert [t["mint"] for t in targets] == ["MintSol"]
    assert isinstance(targets[0]["id"], int) and targets[0]["id"] < 0
    assert targets[0]["entry_id"] == "s1" and targets[0]["opened_at"] == NOW
    assert targets[0]["network_id"] == "solana"
    assert targets[0]["route_binding"]["route_plan"][0]["swapInfo"]["ammKey"] == "TokenPool"
