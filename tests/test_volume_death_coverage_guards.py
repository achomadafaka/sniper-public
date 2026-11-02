"""The volume-death rail must not read its own blindness as a dead market.

Reported broken by the operator on 2026-08-29, on 24 armed fires. Two defects, both the same
shape: an unknown treated as a measurement.

  * `silence = held_s` when no swap was seen since entry. 16 of the 24 had ZERO swap marks for the
    whole hold while price marks existed — we were quoting the coin and never observing swaps — so
    a 61-second-old position tripped a 60-second threshold on its first sweep. They closed at
    60-61s at 1.00x. A time-stop wearing a liquidity rail's name.
  * the `uncovered` second-witness branch FIRED. Every one of the 24 audit rows reads
    `measured_no_state_witness allowed=1`. A guard whose blocking branch never runs is not a guard.
"""
import sqlite3
import threading
import time

import pytest

from sniper.services import memecoin_volume_death as V
from sniper.services.memecoin_solana_swap_marks import (
    PoolBinding, SolanaSwapMarkCache, probe_position_activity, probe_vault_activity,
)
from sniper.services.memecoin_solana_amounts import WSOL

CHAIN, MINT = "solana", "MINT"
OPEN = "2026-08-29T12:00:00+00:00"
NOW = "2026-08-29T12:01:01+00:00"   # 61s old, against a 60s threshold
COHORT = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE memecoin_position_marks (id INTEGER PRIMARY KEY, chain_id TEXT,
      mint TEXT, sweep_ts TEXT, mid REAL, executable_exit_value REAL, liquidity REAL, mcap REAL,
      route_quote_id TEXT, source TEXT, quality TEXT)""")
    return c


def _mark(c, *, ts, source, mid):
    c.execute("INSERT INTO memecoin_position_marks (chain_id, mint, sweep_ts, mid, source) "
              "VALUES (?,?,?,?,?)", (CHAIN, MINT, ts, mid, source))
    c.commit()


def _decide(c, *, vouched=True, **kw):
    """`vouched` adds another coin's swap inside the silence: the proof the stream was delivering
    (2026-09-13, PVE 57075). These guards test the witnesses, so they assume a live stream."""
    if vouched:
        c.execute("INSERT INTO memecoin_position_marks (chain_id, mint, sweep_ts, mid, source) "
                  "VALUES (?,?,?,?,?)", (CHAIN, "OTHER", "2026-08-29T12:01:00+00:00", 1.0,
                                         V.SWAP_SOURCE))
        c.commit()
    pos = {"id": kw.pop("position_id", None), "opened_at": OPEN,
           "mint": MINT, "network_id": CHAIN,
           "quote_route_target": kw.pop("quote_route_target", None)}
    # A single token-facing exit pool that matches the entry route, unless a test says otherwise:
    # an ambiguous exit route can never certify quiet (route_unknown).
    kw.setdefault("exit_quote_evidence", {"route_plan": [{"ammKey": "PoolA", "inputMint": MINT}]})
    return V.armed_exit_decision(c, pos, now_iso=NOW, held_s=61.0, exit_s=60.0,
                                 min_opened_at=COHORT, stream_ok=True,
                                 require_onchain=kw.pop("require_onchain", False), **kw)


def test_a_pool_the_swap_lane_never_watched_does_not_fire(conn):
    """THE 16. Price marks exist, swap marks never did. Absence of observation is not silence."""
    _mark(conn, ts="2026-08-29T12:00:30+00:00", source="quote", mid=1.0)
    fire, _sil, why = _decide(conn)
    assert fire is False and why == "swap_lane_uncovered"


def test_a_covered_pool_with_no_second_witness_does_not_fire(conn):
    """THE OTHER 8. The swap lane covers this pool and saw nothing, but the reserve lane never
    watched it, so 'did the pool go quiet' has no second answer. Freeze on not-knowing."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)  # coverage, old
    fire, _sil, why = _decide(conn)
    # The 2026-09-02 split read this as `structural` — "watched, never emitted, cannot ever answer".
    # Withdrawn 2026-09-22: the swap mark replaces the state mark in the mark cache, so a missing
    # state row measures the sample and not the pool. One freeze, one label.
    assert fire is False and why == "state_uncovered_frozen"


def test_raw_reserve_witness_unfreezes_covered_quiet_pool(conn):
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    cache = SolanaSwapMarkCache()
    binding = PoolBinding(MINT, "PoolA", "CoinV", "QuoteV", WSOL)
    row = {"kind": "sol_sync", "pool": "PoolA", "block_no": 1,
           "amount0": "2000000000000", "amount1": "300000000000",
           "decimals0": 6, "decimals1": 9}
    for sec in (0, 50):
        cache.record(row, binding, now_ts=float(sec),
                     observed_at=f"2026-08-29T12:00:{sec:02d}+00:00")
    assert _decide(conn, reserve_witness_cache=cache)[::2] == (True, "measured_state_still")
    cache.record({**row, "amount0": "2100000000000"}, binding, now_ts=55.0,
                 observed_at="2026-08-29T12:00:55+00:00")
    assert _decide(conn, reserve_witness_cache=cache)[::2] == (False, "state_moving")


def test_cz_missed_vault_trade_blocks_sol_volume_death(conn):
    """CZ 65543: the swap and reserve WS both missed a real trade during the silence."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    cache = SolanaSwapMarkCache()
    binding = PoolBinding(MINT, "PoolA", "CoinV", "QuoteV", WSOL)
    row = {"kind": "sol_sync", "pool": "PoolA", "block_no": 1,
           "amount0": "2000000000000", "amount1": "300000000000",
           "decimals0": 6, "decimals1": 9}
    for sec in (0, 50):
        cache.record(row, binding, now_ts=float(sec),
                     observed_at=f"2026-08-29T12:00:{sec:02d}+00:00")
    cache.onchain_witness = lambda **_kw: "moving"
    fire, _silence, why = _decide(conn, reserve_witness_cache=cache, require_onchain=True)
    assert fire is False and why == "onchain_moving"


@pytest.mark.parametrize("local_swap", [False, True])
def test_bound_pool_can_exit_on_onchain_quiet_when_ws_coverage_is_missing(conn, local_swap):
    if local_swap:
        _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **_kw: "still"
    fire, silence, why = _decide(conn, reserve_witness_cache=cache, require_onchain=True)
    assert (fire, silence, why) == (True, 60.0, "measured_onchain_still")


def test_onchain_quiet_can_replace_a_blind_socket_but_not_confirmed_movement(conn):
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **_kw: "still"
    pos = {"id": 123, "opened_at": OPEN, "mint": MINT, "network_id": CHAIN}
    assert V.armed_exit_decision(
        conn, pos, now_iso=NOW, held_s=61.0, exit_s=60.0,
        min_opened_at=COHORT, stream_ok=False, reserve_witness_cache=cache,
        exit_quote_evidence={"route_plan": [{"ammKey": "PoolA", "inputMint": MINT}]}) == (
            True, 60.0, "measured_onchain_still")
    _mark(conn, ts="2026-08-29T12:00:50+00:00", source=V.SWAP_SOURCE, mid=1.0)
    cache.onchain_witness = lambda **_kw: "moving"
    assert _decide(conn, reserve_witness_cache=cache, require_onchain=True)[::2] == (
        False, "onchain_moving")


def _tape_file(tmp_path, monkeypatch):
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as dbt
    from sniper.services import memecoin_solana_tape as st
    path = tmp_path / "sol_tape.db"
    tc = sqlite3.connect(path)
    tc.row_factory = sqlite3.Row
    dbt.ensure_tick_tables(tc)
    dbt.ensure_block_times(tc)
    ensure_feature_tables(tc)
    st.ensure_sol_cursor(tc)
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_DB", str(path))
    return tc


def test_a_real_curve_binding_reaches_the_probe_with_distinct_curve_and_vault(tmp_path, monkeypatch):
    # PAIDCATS 65854 (2026-09-26, Codex ctb audit P0). The binding comes from the PRODUCER,
    # resolve_position_pool, on a pump curve transaction: pool = coin_vault = the token vault,
    # quote_vault = the curve PDA, quote_mint = native-sol. The exit route's key is the CURVE.
    # The old fixture used one address for both roles, which is the only shape the bug spares.
    from test_memecoin_pump_curve_tape import CURVE, MINT, VAULT, _curve_tx
    from sniper.services import memecoin_solana_tape as st
    tc = _tape_file(tmp_path, monkeypatch)
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    ledger.execute("CREATE TABLE memecoin_fills (id INTEGER, position_id INTEGER, kind TEXT, "
                   "tx_sig TEXT)")
    ledger.execute("INSERT INTO memecoin_fills VALUES (1, 7, 'entry', 'entry')")
    tx = _curve_tx()
    tx["blockTime"] = 1_700_000_000
    assert st.resolve_position_pool(tc, ledger, {"id": 7, "mint": MINT}, lambda _sig: tx) == VAULT
    assert CURVE != VAULT
    seen = []
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.probe_vault_activity",
                        lambda vault, *_args, **_kw: (seen.append(vault), "still")[1])
    assert probe_position_activity(7, MINT, 0, 30, CURVE) == "still"
    assert seen == [VAULT]                          # the token vault is what gets probed
    # Wrong or missing route identity stays unavailable, typed, with NO probe call.
    assert probe_position_activity(7, MINT, 0, 30, VAULT) == "unavailable:identity_mismatch"
    assert probe_position_activity(7, MINT, 0, 30, "OtherCurve") == "unavailable:identity_mismatch"
    assert probe_position_activity(7, MINT, 0, 30, None) == "unavailable:route_unknown"
    assert probe_position_activity(8, MINT, 0, 30, CURVE) == "unavailable:binding_missing"
    assert probe_position_activity(7, "WRONG", 0, 30, CURVE) == "unavailable:binding_missing"
    tc.execute("UPDATE sol_pool_vaults SET quote_mint='other' WHERE pool=?", (VAULT,))
    tc.commit()
    assert probe_position_activity(7, MINT, 0, 30, CURVE) == "unavailable:identity_mismatch"
    assert seen == [VAULT]
    tc.execute("UPDATE sol_pool_vaults SET quote_mint='native-sol' WHERE pool=?", (VAULT,))
    tc.commit()
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.probe_vault_activity",
                        lambda *_args, **_kw: "unavailable")
    assert probe_position_activity(7, MINT, 0, 30, CURVE) == "unavailable:provider"


def _bound_curve_position(tmp_path, monkeypatch):
    """The producer's real binding for position 7 (a pump curve), as the slot lane leaves it."""
    from test_memecoin_pump_curve_tape import CURVE, MINT as CMINT, VAULT, _curve_tx
    from sniper.services import memecoin_solana_tape as st
    tc = _tape_file(tmp_path, monkeypatch)
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    ledger.execute("CREATE TABLE memecoin_fills (id INTEGER, position_id INTEGER, kind TEXT, "
                   "tx_sig TEXT)")
    ledger.execute("INSERT INTO memecoin_fills VALUES (1, 7, 'entry', 'entry')")
    tx = _curve_tx()
    tx["blockTime"] = 1_700_000_000
    assert st.resolve_position_pool(tc, ledger, {"id": 7, "mint": CMINT}, lambda _sig: tx) == VAULT
    return tc, CURVE, CMINT, VAULT


def _label_only_decision(conn, mint, route_key, witness):
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = witness
    pos = {"id": 7, "opened_at": OPEN, "mint": mint, "network_id": CHAIN,
           "quote_route_target": "slot_lane"}         # what every slot-lane fill stores
    conn.execute("INSERT INTO memecoin_position_marks (chain_id, mint, sweep_ts, mid, source) "
                 "VALUES (?,?,?,?,?)", (CHAIN, "OTHER", "2026-08-29T12:01:00+00:00", 1.0,
                                        V.SWAP_SOURCE))
    conn.commit()
    return V.armed_exit_decision(
        conn, pos, now_iso=NOW, held_s=61.0, exit_s=60.0, min_opened_at=COHORT, stream_ok=True,
        require_onchain=True, reserve_witness_cache=cache,
        exit_quote_evidence={"route_plan": [{"ammKey": route_key, "inputMint": mint,
                                             "outputMint": WSOL}]})


def test_a_slot_lane_curve_position_reaches_the_probe_through_its_binding(conn, tmp_path,
                                                                          monkeypatch):
    # Position 65860 (2026-09-26): quote_route_target 'slot_lane' made every exit route read as
    # route_changed, so vol_death could never reach its proof. The binding names the curve.
    _tc, curve, mint, vault = _bound_curve_position(tmp_path, monkeypatch)
    seen = []
    witness = lambda **kw: (seen.append(kw["current_pool_key"]), "still")[1]
    fire, _silence, why = _label_only_decision(conn, mint, curve, witness)
    assert seen == [curve] and why != "route_changed"
    seen.clear()
    fire, _silence, why = _label_only_decision(conn, mint, "OtherCurve", witness)
    assert (fire, why, seen) == (False, "route_changed", [])   # a different curve still vetoes
    fire, _silence, why = _label_only_decision(conn, mint, vault, witness)
    assert (fire, why, seen) == (False, "route_changed", [])   # the vault is not the curve


def test_a_label_only_amm_position_stays_held_as_route_unknown_entry(conn, tmp_path, monkeypatch):
    tc = _tape_file(tmp_path, monkeypatch)
    tc.execute("INSERT INTO sol_position_pools (position_id, mint, pool, venue_kind, resolved_at) "
               "VALUES (7, 'MINT', 'CoinV', 'amm', 'x')")
    tc.execute("INSERT INTO sol_pool_vaults (pool, mint, coin_vault, quote_vault, quote_mint, "
               "resolved_at) VALUES ('CoinV', 'MINT', 'CoinV', 'QuoteV', 'wsol', 'x')")
    tc.commit()
    seen = []
    witness = lambda **kw: (seen.append(1), "still")[1]
    fire, _silence, why = _label_only_decision(conn, "MINT", "SomeAmmPool", witness)
    assert (fire, why, seen) == (False, "route_unknown_entry", [])


def test_real_pool_keys_keep_the_old_route_check(conn):
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **_kw: "still"
    key = str(__import__("solders.keypair", fromlist=["Keypair"]).Keypair().pubkey())
    fire, _s, why = _decide(conn, reserve_witness_cache=cache, require_onchain=True, position_id=7,
                            quote_route_target=key,
                            exit_quote_evidence={"route_plan": [{"ammKey": "Elsewhere",
                                                                 "inputMint": MINT}]})
    assert (fire, why) == (False, "route_changed")


# THE REAL AMM BINDING SHAPE (position 65858, 2026-09-26): resolve_position_pool stores the COIN
# VAULT as the binding's pool, and the exit route's ammKey is the pool account, a different address.
AMM_MINT = "FdfMy7b1pxPCZ4LBo38D2BRSX8dhzc3srL9byrXBGLZH"
AMM_COIN_VAULT = "68wRQDTVSqRu8MjKZKtr44qeFYgH9Sb3iyegKJj24rfK"
AMM_QUOTE_VAULT = "aTUQgpFAxmpJ5QddwqTiFBkWizd1WP6MTgu5zFsfuvQ"
AMM_POOL_KEY = "7cQBNzr15ys9o5tToEd9aEfNgu4JRqjP7MoC8E17uJw7"


def test_the_real_amm_binding_shape_reaches_the_probe(tmp_path, monkeypatch):
    """1710f7b1 compared the exit ammKey with the binding's pool, which for an AMM is the coin
    vault, so every AMM position would have gone blind. A fixture that gave the pool and the ammKey
    one address hid it; this one uses 65858's real, distinct addresses."""
    tc = _tape_file(tmp_path, monkeypatch)
    tc.execute("INSERT INTO sol_position_pools (position_id, mint, pool, venue_kind, resolved_at) "
               "VALUES (65858, ?, ?, 'amm', 'x')", (AMM_MINT, AMM_COIN_VAULT))
    tc.execute("INSERT INTO sol_pool_vaults (pool, mint, coin_vault, quote_vault, quote_mint, "
               "resolved_at) VALUES (?, ?, ?, ?, ?, 'x')",
               (AMM_COIN_VAULT, AMM_MINT, AMM_COIN_VAULT, AMM_QUOTE_VAULT,
                "So11111111111111111111111111111111111111112"))
    tc.commit()
    seen = []
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.probe_vault_activity",
                        lambda vault, *_args, **_kw: (seen.append(vault), "moving")[1])
    assert probe_position_activity(65858, AMM_MINT, 0, 30, AMM_POOL_KEY) == "moving"
    assert seen == [AMM_COIN_VAULT]
    assert probe_position_activity(65858, AMM_MINT, 0, 30) == "unavailable:route_unknown"
    assert seen == [AMM_COIN_VAULT]


def test_a_typed_unavailable_reaches_the_decision_as_its_own_reason(conn):
    for proof, why in (("unavailable:identity_mismatch", "onchain_unavailable:identity_mismatch"),
                       ("unavailable:binding_missing", "onchain_unavailable:binding_missing"),
                       ("unavailable:route_unknown", "onchain_unavailable:route_unknown"),
                       ("unavailable:provider", "onchain_unavailable:provider"),
                       ("unavailable:lookup_failed", "onchain_unavailable:lookup_failed"),
                       ("unavailable", "onchain_unavailable"),
                       ("pending", "onchain_pending")):
        cache = SolanaSwapMarkCache()
        cache.onchain_witness = lambda _p=proof, **_kw: _p
        fire, _silence, got = _decide(conn, reserve_witness_cache=cache, require_onchain=True)
        assert (fire, got) == (False, why)


def test_the_async_witness_passes_a_typed_cause_through():
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_a: "unavailable:identity_mismatch")
    args = dict(position_id=7, mint="M", window_start_iso="2026-09-26T23:06:00+00:00",
                now_iso="2026-09-26T23:06:30+00:00", current_pool_key="Curve")
    assert cache.onchain_witness(**args) == "pending"
    deadline = time.monotonic() + 2
    got = "pending"
    while got == "pending" and time.monotonic() < deadline:
        time.sleep(0.01)
        got = cache.onchain_witness(**args)
    assert got == "unavailable:identity_mismatch"


def test_funded_decision_passes_only_one_token_facing_route_key(conn):
    cache = SolanaSwapMarkCache()
    seen = []
    cache.onchain_witness = lambda **kw: (seen.append(kw), "still")[1]
    evidence = {"route_plan": [
        {"ammKey": "PoolA", "inputMint": MINT, "outputMint": WSOL},
        {"ammKey": "QuotePool", "inputMint": WSOL, "outputMint": "USDC"},
    ]}
    assert _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                   position_id=123, exit_quote_evidence=evidence)[0] is True
    assert seen[-1]["current_pool_key"] == "PoolA"
    evidence["route_plan"].append({"ammKey": "OtherPool", "inputMint": MINT})
    assert _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                   position_id=123, exit_quote_evidence=evidence)[::2] == (False, "route_unknown")
    assert len(seen) == 1                                  # a split route never asks the witness


def test_changed_sell_pool_holds_volume_death_even_with_quiet_entry_pool(conn):
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **_kw: "still"
    evidence = {"route_plan": [
        {"ammKey": "OtherPool", "inputMint": MINT, "outputMint": WSOL},
        {"ammKey": "QuotePool", "inputMint": WSOL, "outputMint": "USDC"},
    ]}
    # Real entries store base58 pool keys; a fictional label would take the label-only branch.
    from solders.keypair import Keypair
    entry_keys = "%s,%s" % (Keypair().pubkey(), Keypair().pubkey())
    fire, _silence, why = _decide(
        conn, reserve_witness_cache=cache, require_onchain=True, position_id=123,
        quote_route_target=entry_keys, exit_quote_evidence=evidence)
    assert (fire, why) == (False, "route_changed")


def test_a_quiet_vault_fires_after_async_proof_without_waiting_for_rpc(conn):
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    gate = threading.Event()
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_args: (gate.wait(1), "still")[1])
    binding = PoolBinding(MINT, "PoolA", "CoinV", "QuoteV", WSOL)
    row = {"kind": "sol_sync", "pool": "PoolA", "block_no": 1,
           "amount0": "2000000000000", "amount1": "300000000000",
           "decimals0": 6, "decimals1": 9}
    for sec in (0, 50):
        cache.record(row, binding, now_ts=float(sec),
                     observed_at=f"2026-08-29T12:00:{sec:02d}+00:00")
    assert _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                   position_id=123)[::2] == (
        False, "onchain_pending")
    gate.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        result = _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                         position_id=123)
        if result[0]:
            assert result[2] == "measured_state_still"
            break
        time.sleep(0.005)
    else:
        pytest.fail("quiet pool never received its independent proof")


@pytest.mark.parametrize("latest_age,head_age,expected", [
    (10, 1, "moving"),       # CZ: a missed vault transaction inside the 30-second window
    (31, 1, "still"),        # a genuinely quiet pool
    (31, 4, "unavailable"), # a lagging head cannot prove the final seconds
    (31, 60, "unavailable"), # stale RPC must not certify silence
    (None, 1, "unavailable"), # unknown block time is not zero activity
])
def test_vault_signature_probe_requires_fresh_chain_and_complete_time(
        latest_age, head_age, expected):
    now = int(time.time())
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "getSlot":
            return 450673900
        if method == "getBlockTime":
            return now - head_age
        assert method == "getSignaturesForAddress"
        return [{"signature": "real-vault-transaction", "slot": 450673899,
                 "blockTime": now - latest_age if latest_age is not None else None}]

    assert probe_vault_activity("CoinV", now - 30, now, rpc_call=rpc) == expected
    if head_age == 1:
        assert calls[-1] == ("getSignaturesForAddress", ["CoinV", {
            "commitment": "confirmed", "minContextSlot": 450673900, "limit": 100}])


def test_empty_vault_history_is_unknown_and_boundary_second_is_activity():
    now = int(time.time())

    def rpc(method, _params):
        if method == "getSlot":
            return 100
        if method == "getBlockTime":
            return now
        return []

    assert probe_vault_activity("CoinV", now - 30, now, rpc_call=rpc) == "unavailable"

    def boundary_rpc(method, params):
        result = rpc(method, params)
        if method == "getSignaturesForAddress":
            return [{"signature": "boundary", "slot": 99, "blockTime": now - 30}]
        return result

    # Block time has one-second resolution: the trade may be 0.4s into this second.
    assert probe_vault_activity("CoinV", now - 29.6, now,
                                rpc_call=boundary_rpc) == "moving"


def test_second_rpc_trade_vetoes_first_rpc_quiet(monkeypatch):
    from sniper.services import memecoin_solana_blocktime as blocktime
    from sniper.settings import memecoin as settings

    now = int(time.time())
    monkeypatch.setenv("MEMECOIN_VOLDEATH_SOLANA_PROOF_RPC_URL", "rpc-A")
    monkeypatch.setenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "rpc-B")
    monkeypatch.setenv("MEMECOIN_SOL_H1_ACTIVITY_RPC_URL", "")
    monkeypatch.setattr(settings, "solana_read_rpc", lambda: "public")
    seen = []

    def rpc(url, method, _params, *, timeout):
        seen.append((url, method))
        if method == "getSlot":
            return 100
        if method == "getBlockTime":
            return now
        return [{"signature": url, "slot": 99,
                 "blockTime": now - (31 if url == "rpc-A" else 10)}]

    monkeypatch.setattr(blocktime, "_rpc_call", rpc)
    assert probe_vault_activity("CoinV", now - 30, now) == "moving"
    assert {url for url, method in seen if method == "getSignaturesForAddress"} == {
        "rpc-A", "rpc-B"}


def test_slow_signature_read_cannot_certify_a_stale_head(monkeypatch):
    clock = [1000]
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.time.time",
                        lambda: clock[0])

    def rpc(method, _params):
        if method == "getSlot":
            return 100
        if method == "getBlockTime":
            return 1000
        clock[0] += 4
        return [{"signature": "old", "slot": 99, "blockTime": 960}]

    assert probe_vault_activity("CoinV", 970, 1000, rpc_call=rpc) == "unavailable"


def test_failed_second_rpc_cannot_age_out_first_quiet_proof(monkeypatch):
    from sniper.services import memecoin_solana_blocktime as blocktime
    from sniper.settings import memecoin as settings

    clock = [1000.0]
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.time.time",
                        lambda: clock[0])
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.time.monotonic",
                        lambda: clock[0])
    monkeypatch.setenv("MEMECOIN_VOLDEATH_SOLANA_PROOF_RPC_URL", "rpc-A")
    monkeypatch.setenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "rpc-B")
    monkeypatch.setattr(settings, "solana_read_rpc", lambda: "public")

    def rpc(url, method, _params, *, timeout):
        if url == "rpc-B":
            clock[0] += 4
            return None
        if method == "getSlot":
            return 100
        if method == "getBlockTime":
            return 1000
        return [{"signature": "old", "slot": 99, "blockTime": 960}]

    monkeypatch.setattr(blocktime, "_rpc_call", rpc)
    assert probe_vault_activity("CoinV", 970, 1000) == "unavailable"


def test_completed_probe_age_and_requested_window_control_cache(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("sniper.services.memecoin_solana_swap_marks.time.monotonic",
                        lambda: clock[0])
    gate = threading.Event()
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_args: (gate.wait(1), "still")[1])
    args = {"position_id": 123, "mint": MINT,
            "window_start_iso": "2026-08-29T12:00:30+00:00", "now_iso": NOW}
    assert cache.onchain_witness(**args) == "pending"
    clock[0] += 4.0  # queue plus RPC exceeded the old three-second submission TTL
    gate.set()
    deadline = time.time() + 1.0
    while not next(iter(cache._onchain_futures.values()))[1].done() and time.time() < deadline:
        time.sleep(0.005)
    assert cache.onchain_witness(**args) == "still"
    # A longer silence window needs a new proof; the earlier result covered only 30s.
    assert cache.onchain_witness(**{**args,
                                    "window_start_iso": "2026-08-29T12:00:00+00:00"}) == "pending"


def test_route_change_invalidates_completed_quiet_proof():
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_args: "still")
    args = {"position_id": 123, "mint": MINT, "current_pool_key": "CurveA",
            "window_start_iso": "2026-08-29T12:00:30+00:00", "now_iso": NOW}
    assert cache.onchain_witness(**args) == "pending"
    deadline = time.time() + 1.0
    while not next(iter(cache._onchain_futures.values()))[1].done() and time.time() < deadline:
        time.sleep(0.005)
    assert cache.onchain_witness(**args) == "still"
    assert cache.onchain_witness(**{**args, "current_pool_key": "PoolB"}) == "pending"
    assert cache.onchain_witness(**args) == "pending"  # switching back also needs new proof


def test_probe_refuses_a_partial_or_misordered_signature_page():
    now = int(time.time())

    def rpc(method, _params):
        if method == "getSlot":
            return 100
        if method == "getBlockTime":
            return now
        return [{"signature": "old", "slot": 90, "blockTime": now - 31},
                {"signature": "new", "slot": 95, "blockTime": now - 10}]

    assert probe_vault_activity("CoinV", now - 30, now, rpc_call=rpc) != "still"


def test_a_moving_pool_does_not_fire(conn):
    """Position 44813: peaked 4.45x and was sold for +6.3% while the reserve tape swung 45%."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i, mid in enumerate((0.0035, 0.0050, 0.0037, 0.0049)):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10), source=V.STATE_SOURCE, mid=mid)
    fire, _sil, why = _decide(conn)
    assert fire is False and why == "state_moving"


def test_a_genuinely_dead_covered_pool_STILL_FIRES(conn):
    """The rail is not disarmed. Covered pool, no swap since entry, reserves flat: this is the
    state the rail exists for and it must still fire, or the fix is just a switch-off."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i in range(4):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10), source=V.STATE_SOURCE, mid=0.004)
    fire, _sil, why = _decide(conn)
    assert fire is True and why == "measured_state_still"


def test_swap_lane_coverage_is_unbounded_in_time(conn):
    """Coverage is a property of the POOL, not of the hold. A pool last seen trading days ago is
    still a pool we watch; scoping this to the hold would re-create the bug it fixes."""
    assert V.swap_lane_covers(conn, mint=MINT, chain_id=CHAIN) is False
    _mark(conn, ts="2026-07-01T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    assert V.swap_lane_covers(conn, mint=MINT, chain_id=CHAIN) is True


def test_the_rail_still_freezes_on_a_blind_stream(conn):
    """Pre-existing behaviour, pinned so a later edit cannot quietly drop it."""
    pos = {"opened_at": OPEN, "mint": MINT, "network_id": CHAIN}
    fire, _sil, why = V.armed_exit_decision(conn, pos, now_iso=NOW, held_s=61.0, exit_s=60.0,
                                            min_opened_at=COHORT, stream_ok=False,
                                            require_onchain=False)
    assert fire is False and why == "blind_frozen"


# --- the stream must be proven alive during the silence (2026-09-13, PVE 57075) ---

def test_a_silence_only_the_coin_itself_vouches_for_does_not_fire(conn):
    """PVE: the stream watched one mint, stopped delivering, and its own last swap kept the
    chain-level health check green while the coin traded $21.9K that minute."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i in range(4):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10), source=V.STATE_SOURCE, mid=0.004)
    fire, _sil, why = _decide(conn, vouched=False)
    assert fire is False and why == "stream_unvouched"


def test_flat_exact_quotes_may_vouch_for_an_unproven_stream(conn):
    """A genuinely dead coin alone on the stream still exits when the sellable price is flat."""
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i in range(4):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10), source=V.STATE_SOURCE, mid=0.004)
    V._EXACT_TAPE.clear()
    for ts in ("12:00:03", "12:00:30", "12:00:58"):
        V.record_exact_quote(None, quoted_at="2026-08-29T%s+00:00" % ts, price=0.004)
    try:
        fire, _sil, why = _decide(conn, vouched=False)
    finally:
        V._EXACT_TAPE.clear()
    assert fire is True and why == "measured_state_still"


def test_moving_exact_quotes_veto_even_a_still_reserve_read(conn):
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i in range(4):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10), source=V.STATE_SOURCE, mid=0.004)
    V._EXACT_TAPE.clear()
    for ts, px in (("12:00:03", 0.0040), ("12:00:30", 0.0046), ("12:00:58", 0.0041)):
        V.record_exact_quote(None, quoted_at="2026-08-29T%s+00:00" % ts, price=px)
    try:
        fire, _sil, why = _decide(conn)
    finally:
        V._EXACT_TAPE.clear()
    assert fire is False and why == "exact_quote_moving"


@pytest.mark.parametrize("stamps", [("12:00:03", "12:00:58"), ("12:00:40", "12:00:58")])
def test_sparse_exact_quotes_still_prove_movement(conn, stamps):
    _mark(conn, ts="2026-08-20T00:00:00+00:00", source=V.SWAP_SOURCE, mid=1.0)
    for i in range(4):
        _mark(conn, ts="2026-08-29T12:00:%02d+00:00" % (10 + i * 10),
              source=V.STATE_SOURCE, mid=0.004)
    V._EXACT_TAPE.clear()
    for stamp, price in zip(stamps, (0.004, 0.006)):
        V.record_exact_quote(None, quoted_at="2026-08-29T%s+00:00" % stamp, price=price)
    try:
        fire, _, why = _decide(conn)
    finally:
        V._EXACT_TAPE.clear()
    assert not fire and why == "exact_quote_moving"


@pytest.mark.parametrize("plan", [
    None, [],
    [{"ammKey": AMM_POOL_KEY, "inputMint": MINT},                                     # split sell
     {"ammKey": "8SrzuBHchAuBmfyWyi9wA38jSSCyJE5jfq7EckXaR3ah", "inputMint": MINT}],
    [{"ammKey": AMM_POOL_KEY, "inputMint": MINT}, {"inputMint": MINT, "outputMint": WSOL}],  # no pool
])
def test_an_ambiguous_exit_route_cannot_certify_a_quiet_entry_pool(conn, plan):
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **kw: "still"
    fire, _silence, why = _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                                  exit_quote_evidence={"route_plan": plan})
    assert (fire, why) == (False, "route_unknown")
    assert "route_unknown" in V.POST_THRESHOLD_REASONS and "route_unknown" in V.HELD_WITNESS_REASONS


def test_a_raydium_shaped_leg_names_its_pool_as_pool_id(conn):
    """The Raydium fallback quote names a leg's pool `poolId`, not `ammKey`: 492 of 45,747 funded
    Solana exit quotes (2026-09-26). That is still one known pool and must not read as unknown."""
    cache = SolanaSwapMarkCache()
    seen = []
    cache.onchain_witness = lambda **kw: (seen.append(kw), "still")[1]
    # The real Raydium leg shape (route series id 84273, 2026-09-15), on this test's mint.
    plan = [{"feeAmount": "10069668555", "feeMint": MINT, "inputMint": MINT, "outputMint": WSOL,
             "poolId": "7u8ugKxunXmWp2qsxUGwQo1noZm8o1WadkkBW3hFDyHF"}]
    fire, _silence, why = _decide(conn, reserve_witness_cache=cache, require_onchain=True,
                                  exit_quote_evidence={"route_plan": plan})
    assert (fire, why) == (True, "measured_onchain_still")
    assert seen[-1]["current_pool_key"] == "7u8ugKxunXmWp2qsxUGwQo1noZm8o1WadkkBW3hFDyHF"


def test_the_real_amm_entry_route_matches_its_exit_pool_and_asks_the_witness(conn):
    """65858's stored entry route (leg ammKey, pool ammKey) and an exit route through the same pool:
    no route_changed, and the witness is asked with the pool key."""
    cache = SolanaSwapMarkCache()
    seen = []
    cache.onchain_witness = lambda **kw: (seen.append(kw), "still")[1]
    fire, _silence, why = _decide(
        conn, reserve_witness_cache=cache, require_onchain=True,
        quote_route_target="8FnX3xo2yYw3EUE6w3nQA4GfXGS9wpK6oj3veJpbFzLo," + AMM_POOL_KEY,
        exit_quote_evidence={"route_plan": [{"ammKey": AMM_POOL_KEY, "inputMint": MINT,
                                             "outputMint": WSOL}]})
    assert (fire, why) == (True, "measured_onchain_still")
    assert seen[-1]["current_pool_key"] == AMM_POOL_KEY
