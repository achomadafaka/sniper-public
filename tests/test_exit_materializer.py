"""Phase 2 Task 8 — materialize pass drives the PRODUCTION db_ticks + compute_state (not stubs).

Seeds a real ticks DB (pool_ticks, pool_meta, block_times) and a minimal ledger with open real
positions, then asserts the pass writes exactly one honest snapshot per position through the real
reader (`open_real_positions`) and the real `compute_state`.
"""
import json
import sqlite3
from datetime import datetime, timezone
import pytest

from sniper.persistence import db_ticks as t
from sniper.analysis import tick_features as tf
from sniper.services.memecoin_exit_materializer import _read_pool_ticks, materialize_open_positions


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def _ticks_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    tf.ensure_feature_tables(c)
    return c


def _ledger_with_positions(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # minimal memecoin_positions — only the columns the materializer reads (SELECT * reader)
    c.execute("""CREATE TABLE memecoin_positions (
        id INTEGER PRIMARY KEY, is_open INTEGER, execution_mode TEXT, network_id TEXT, mint TEXT,
        quote_route_target TEXT, entry_price REAL, executable_peak_price REAL, funded_contract_version INTEGER)""")
    c.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


def _seed_v2_pool(tc, chain_id, pool, base_ep):
    # 6 alternating swaps across blocks 1..6 + a Sync; pool_meta quote_side=0; block_times for each
    rows = []
    for i in range(1, 7):
        rows.append({"chain_id": chain_id, "pool": pool, "kind": "v2_swap", "block_no": i,
                     "tx_sig": f"0x{i:064x}", "log_index": 0, "maker": "0xR",
                     "amount0": str((100 + i) if i % 2 else -(90 + i)),
                     "amount1": str(-(40 + i) if i % 2 else (50 + i)), "observed_at": _iso(base_ep + i)})
    rows.append({"chain_id": chain_id, "pool": pool, "kind": "v2_sync", "block_no": 6,
                 "tx_sig": "0xsync", "log_index": 1, "maker": None,
                 "amount0": "1000000", "amount1": "2000000", "observed_at": _iso(base_ep + 6)})
    t.insert_ticks(tc, rows)
    tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
           "token0_decimals, token1_decimals, resolved_at) VALUES (?,?,?,?,?,?,?,?)",
           (chain_id, pool, "0xquote", "0xbase", 0, 18, 18, _iso(base_ep)))
    t.backfill_block_times(tc, chain_id, list(range(1, 7)),
                           lambda b: base_ep + b, _iso(base_ep))
    tc.commit()


def test_open_v2_position_materializes_one_candidate_capable_snapshot():
    base = 1_700_000_000
    tc = _ticks_db()
    _seed_v2_pool(tc, "4663", "0xpool", base)
    ledger = _ledger_with_positions([
        (7, 1, "real", "4663", "0xbase", "ROUTER-IGNORED", 1.8, 2.4, None),   # mint joins to the pool base side; route target is ignored now
    ])
    n = materialize_open_positions(ledger, tc, base + 7)
    assert n == 1
    from sniper.persistence import db_exit_state as xs
    snap = xs.latest_state(tc, 7)
    assert snap["state"] == "trending"
    assert snap["executable_price"] is None and snap["nominal_price"] is not None
    # idempotent: a second pass at the same tick writes nothing new
    assert materialize_open_positions(ledger, tc, base + 7) == 0


def test_pull_reader_preserves_transaction_identity_for_lp_pairing():
    base = 1_700_000_000
    tc = _ticks_db()
    _seed_v2_pool(tc, "4663", "0xpool", base)
    rows = _read_pool_ticks(tc, "4663", "0xpool")
    assert rows and all(row.get("tx_sig") for row in rows)
    assert all(row.get("log_index") is not None for row in rows)


def test_solana_open_writes_source_untrusted_snapshot():
    base = 1_700_000_000
    tc = _ticks_db()
    ledger = _ledger_with_positions([
        (9, 1, "real", "solana", "SOLCOIN", None, None, None, None),
    ])
    n = materialize_open_positions(ledger, tc, base + 7)
    assert n == 1
    from sniper.persistence import db_exit_state as xs
    assert xs.latest_state(tc, 9)["state"] == "source_untrusted"


def test_one_bad_position_never_denies_the_others():
    base = 1_700_000_000
    tc = _ticks_db()
    _seed_v2_pool(tc, "4663", "0xpool", base)
    ledger = _ledger_with_positions([
        (7, 1, "real", "4663", "COIN", "0xpool", 1.8, 2.4, None),
        (8, 1, "real", "solana", "SOLCOIN", None, None, None, None),
    ])
    assert materialize_open_positions(ledger, tc, base + 7) == 2   # both snapshotted


def test_solana_position_on_an_incomplete_vault_is_not_candidate_capable():
    """Codex 2026-07-24: the Solana branch was never exercised, so a missing import there would
    only have surfaced in production (it did — the import was absent and every test still passed).

    Note what this can and cannot prove today: `profile_capability('solana', ...)` is still
    unconditionally `forced_legacy`, so a Solana position lands on SOURCE_UNTRUSTED before the
    completeness gate is ever consulted. The gate is therefore defense-in-depth for the moment
    Solana becomes actionable; the load-bearing test of the gate itself runs on 4663 in
    tests/test_memecoin_solana_tape.py::test_incomplete_vault_is_not_candidate_capable."""
    from sniper.services import memecoin_solana_tape as st
    base = 1_700_000_000
    tc = _ticks_db()
    st.ensure_sol_cursor(tc)
    # the vault is the pool id on solana; token0 = mint
    tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
               "token0_decimals, token1_decimals, resolved_at) "
               "VALUES ('solana','VAULT','MINT','QMINT',1,9,9,?)", (_iso(base),))
    tc.execute("INSERT INTO sol_sig_cursor (vault, last_sig, updated_at, frontier_before) "
               "VALUES ('VAULT','',?,'s0500')", (_iso(base),))   # known hole below the head
    tc.commit()
    ledger = _ledger_with_positions([
        (9, 1, "real", "solana", "MINT", "ROUTER-IGNORED", 1.0, 1.2, None),
    ])
    assert st.vault_tape_incomplete(tc, "VAULT") is True     # the wiring reads the frontier
    assert materialize_open_positions(ledger, tc, base + 7) == 1
    from sniper.persistence import db_exit_state as xs
    snap = xs.latest_state(tc, 9)
    # not candidate-capable either way; today SOURCE_UNTRUSTED gets there first
    assert snap["state"] in ("source_untrusted", "data_insufficient")


def test_materializer_emits_shadow_context_from_solana_own_db():
    base = 1_700_000_000
    evm_tc = _ticks_db()
    sol_tc = _ticks_db()
    xc = sqlite3.connect(":memory:"); xc.row_factory = sqlite3.Row
    from sniper.persistence import db_exit_state as xs
    xs.ensure_exit_state_tables(xc)
    for n in range(5):
        pool = "VAULT" if n == 0 else f"OTHER{n}"
        mint = "MINT" if n == 0 else f"MINT{n}"
        sol_tc.execute(
            "INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,token0_decimals,"
            "token1_decimals,resolved_at) VALUES('solana',?,?,?,?,9,9,?)",
            (pool, mint, "QUOTE", 1, _iso(base)))
        t.insert_ticks(sol_tc, [{"chain_id": "solana", "pool": pool, "kind": "sol_swap",
                                 "block_no": n + 1, "tx_sig": f"sig{n}", "log_index": 0,
                                 "maker": "ACTOR", "amount0": "-100", "amount1": "200",
                                 "observed_at": _iso(base + n + 1)}])
        t.backfill_block_times(sol_tc, "solana", [n + 1], lambda _b, n=n: base + n + 1,
                               _iso(base))
    sol_tc.commit()
    ledger = _ledger_with_positions([
        (9, 1, "real", "solana", "MINT", "IGNORED", 1.0, 1.2, None),
    ])
    assert materialize_open_positions(ledger, evm_tc, base + 7, sol_tc=sol_tc, xc=xc) == 1
    row = xc.execute(
        "SELECT state_id, episode_context_json, regime_json, deployer_activity_json, v5_shadow_json "
        "FROM memecoin_exit_candidate_decisions WHERE position_id=9").fetchone()
    assert row is not None and row["state_id"] is not None
    assert json.loads(row["episode_context_json"])["n_price_bars"] == 1
    assert json.loads(row["regime_json"])["active_pools"] == 5
    assert json.loads(row["deployer_activity_json"])["available"] is False
    # v5_shadow retired (v5-only 2026-07-25): the column is preserved but written null.
    assert row["v5_shadow_json"] is None


def test_materializer_builds_complete_v5_decision_contract(monkeypatch):
    from sniper.services.memecoin_exit_materializer import _contract
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    policy = type("P", (), {
        "policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano", "trail_pct": 0.0,
    })()
    monkeypatch.setattr(
        "sniper.services.memecoin_funded_policy.frozen_policy_from_position",
        lambda _pos: policy,
    )
    contract = _contract(
        {"id": 4, "network_id": "solana", "mint": "M", "opened_at": _iso(1000),
         "exact_quote_misses": 3},
        "POOL", "sol", 1, now_ts=1600,
    )
    assert contract["held_s"] == 600
    assert contract["exact_quote_misses"] == 3
    assert contract["trail_pct"] > 0
    assert contract["coin_band"] == "nano"


def test_materializer_does_not_relabel_an_invalid_frozen_engine_as_v5(monkeypatch):
    """Invalid immutable metadata must skip the position, not emit plausible v5 evidence."""
    from sniper.services.memecoin_exit_materializer import _contract

    def invalid(_pos):
        raise ValueError("engine funded contract decision version is invalid")

    monkeypatch.setattr(
        "sniper.services.memecoin_funded_policy.frozen_policy_from_position", invalid)
    with pytest.raises(ValueError, match="decision version is invalid"):
        _contract({"id": 4, "network_id": "solana", "mint": "M"}, "POOL", "sol", 1)
