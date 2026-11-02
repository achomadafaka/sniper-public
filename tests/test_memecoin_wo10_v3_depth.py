"""WO-10 — Uniswap-V3 pool depth: decode Mint/Burn/Collect + swap in-range liquidity, gated on a
live-verified decoder, with in-range burn severity. topic0s are keccak-self-checked (the repo rule:
decoders are live-verified, fixtures encode the bug). Fee claims (Collect) can never set depth-pull.
"""
import sqlite3

from eth_hash.auto import keccak

from sniper.analysis import exit_features as xf
from sniper.persistence import db_ticks as t
from sniper.services.memecoin_exit_brain import CUT, propose_v5
from sniper.services.memecoin_exit_materializer import _read_pool_ticks
from sniper.services.memecoin_exit_market_state import FEATURE_VERSION, compute_state
from sniper.services.exit_engine_hot import HotPositionEngine
from sniper.services.memecoin_hot_supervisor import hot_state_from_feats
from sniper.services import memecoin_tick_stream as ts
from sniper.settings import memecoin as mc


def _w(v):
    """32-byte data word (two's complement for negatives) as 64 hex chars."""
    return format(v & (2 ** 256 - 1), "064x")


def _topic(v):
    return "0x" + _w(v)


def _log(topic0, topics, data_words, *, addr="0xpool", bn=1000, li=3):
    return {"topics": [topic0, *topics], "data": "0x" + "".join(data_words),
            "address": addr, "blockNumber": hex(bn), "transactionHash": "0xabc",
            "logIndex": hex(li), "blockHash": "0xbh"}


# --- topic0 provenance: keccak self-check (derivation, not a magic constant) --------------------

def test_v3_topic0s_are_keccak_of_canonical_signatures():
    def k(sig):
        return "0x" + keccak(sig.encode()).hex()
    # anchors that MUST match the pre-existing verified constants prove the derivation is sound
    assert ts.V3_SWAP == k("Swap(address,address,int256,int256,uint160,uint128,int24)")
    assert ts.V2_SWAP == k("Swap(address,uint256,uint256,uint256,uint256,address)")
    # the WO-10 additions, derived the same way
    assert ts.V3_MINT == k("Mint(address,address,int24,int24,uint128,uint256,uint256)")
    assert ts.V3_BURN == k("Burn(address,int24,int24,uint128,uint256,uint256)")
    assert ts.V3_COLLECT == k("Collect(address,address,int24,int24,uint128,uint128)")


# --- decode ------------------------------------------------------------------------------------

def test_decode_v3_swap_carries_liquidity_tick_sqrtprice():
    log = _log(ts.V3_SWAP, [_topic(0), _topic(0)],
               [_w(-1000), _w(500), _w(2 ** 96), _w(1_000_000), _w(100)])
    r = ts.decode_evm_log("4663", log, "t")
    assert r["kind"] == "v3_swap" and r["amount0"] == "-1000" and r["amount1"] == "500"
    assert r["liquidity"] == "1000000" and r["tick"] == 100 and r["sqrt_price_x96"] == str(2 ** 96)


def test_decode_v3_burn_carries_liquidity_and_indexed_tick_range():
    log = _log(ts.V3_BURN, [_topic(0), _topic(50), _topic(150)], [_w(250_000), _w(100), _w(200)])
    r = ts.decode_evm_log("4663", log, "t")
    assert r["kind"] == "v3_burn" and r["liquidity"] == "250000"
    assert r["tick_lower"] == 50 and r["tick_upper"] == 150
    assert r["amount0"] == "-100" and r["amount1"] == "-200"           # removed -> negative into-pool


def test_decode_v3_burn_negative_tick_is_sign_extended():
    log = _log(ts.V3_BURN, [_topic(0), _topic(-887220), _topic(-887000)], [_w(9), _w(1), _w(1)])
    r = ts.decode_evm_log("4663", log, "t")
    assert r["tick_lower"] == -887220 and r["tick_upper"] == -887000


def test_decode_v3_collect_is_a_fee_claim_never_depth():
    log = _log(ts.V3_COLLECT, [_topic(0), _topic(50), _topic(150)], [_w(0), _w(7), _w(8)])
    r = ts.decode_evm_log("4663", log, "t")
    assert r["kind"] == "v3_collect" and r.get("liquidity") is None
    assert r.get("tick_lower") is None                                 # no range -> can't set depth


# --- schema: nullable columns, old rows byte-equivalent ----------------------------------------

def test_pool_ticks_migrates_nullable_and_roundtrips_v3():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(pool_ticks)")}
    for col in ("tick", "tick_lower", "tick_upper", "liquidity", "sqrt_price_x96", "block_hash"):
        assert col in cols
    # a pre-V3 row (no new fields) inserts with NULLs; a v3_burn round-trips its typed columns
    t.insert_ticks(c, [{"chain_id": "4663", "pool": "p", "kind": "v2_swap", "block_no": 1,
                        "tx_sig": "x", "log_index": 0, "amount0": "1", "amount1": "2",
                        "observed_at": "t"}])
    t.insert_ticks(c, [ts.decode_evm_log("4663",
                       _log(ts.V3_BURN, [_topic(0), _topic(50), _topic(150)],
                            [_w(250_000), _w(100), _w(200)]), "t")])
    old = c.execute("SELECT tick_lower, liquidity FROM pool_ticks WHERE kind='v2_swap'").fetchone()
    assert old["tick_lower"] is None and old["liquidity"] is None
    burn = c.execute("SELECT tick_lower, tick_upper, liquidity FROM pool_ticks "
                     "WHERE kind='v3_burn'").fetchone()
    assert burn["tick_lower"] == 50 and burn["tick_upper"] == 150 and burn["liquidity"] == "250000"


def test_materializer_reader_preserves_v3_depth_evidence():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.insert_ticks(c, [{
        "chain_id": "4663", "pool": "0xpool", "kind": "v3_burn", "block_no": 7,
        "tx_sig": "0xtx", "log_index": 2, "amount0": "-1", "amount1": "-2",
        "observed_at": "t", "tick_lower": 50, "tick_upper": 150,
        "liquidity": "250000", "sqrt_price_x96": str(2 ** 96), "block_hash": "0xbh",
    }])

    row = _read_pool_ticks(c, "4663", "0xpool")[0]

    assert row["tick_lower"] == 50 and row["tick_upper"] == 150
    assert row["liquidity"] == "250000"
    assert row["sqrt_price_x96"] == str(2 ** 96)
    assert row["block_hash"] == "0xbh"


# --- capability gated on a verified decoder ----------------------------------------------------

def test_v3_depth_unavailable_until_chain_verified():
    off = xf.profile_capability("4663", "v3", v3_depth_verified=frozenset())
    assert off.depth_trusted is False and "depth" not in off.required
    on = xf.profile_capability("4663", "v3", v3_depth_verified=frozenset({"4663"}))
    assert on.depth_trusted is True and "depth" in on.required
    # V2 is unaffected — it always has depth from Sync reserves
    assert xf.profile_capability("4663", "v2", v3_depth_verified=frozenset()).depth_trusted is True


# --- in-range burn severity --------------------------------------------------------------------

def _swap(tick, liq):
    return {"kind": "v3_swap", "tick": tick, "liquidity": str(liq)}


def _burn(lo, hi, liq):
    return {"kind": "v3_burn", "tick_lower": lo, "tick_upper": hi, "liquidity": str(liq)}


def _iso(ep):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def _v3_inputs(base_ep):
    amts = [("100", "-50"), ("-100", "60"), ("120", "-50"), ("-90", "55"), ("110", "-48"),
            ("-80", "50")]
    swaps = [{"kind": "v3_swap", "pool": "0xp", "block_no": i, "maker": "0xR",
              "amount0": a0, "amount1": a1, "liquidity": "1000000", "tick": 100}
             for i, (a0, a1) in enumerate(amts, start=1)]
    swaps.append({"kind": "v3_burn", "pool": "0xp", "block_no": 6, "maker": "0xo",
                  "amount0": "-1", "amount1": "-1", "liquidity": "250000",
                  "tick_lower": 50, "tick_upper": 150})
    block_times = {i: _iso(base_ep + i) for i in range(1, 7)}
    contract = {"chain_id": "4663", "mint": "COIN", "pool": "0xp", "pool_kind": "v3",
                "position_id": 7, "quote_side": 0, "entry_price": 1.0}
    return swaps, block_times, contract


def test_compute_state_v3_depth_gated_on_verified_chain(monkeypatch):
    from sniper.services.memecoin_exit_market_state import compute_state
    import sniper.settings.memecoin as mc
    now = 1_700_000_100
    swaps, bt, contract = _v3_inputs(1_700_000_000)

    # default (unverified): V3 depth is untrusted -> the burn does NOT become an authoritative pull
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset())
    off = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    assert off.provenance["depth"].quality == "untrusted"
    assert not off.candidate_capable(("depth",))          # a v3_burn can't CUT until verified

    # verified: V3 depth trusts the v3_swap in-range liquidity + the v3_burn severity
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset({"4663"}))
    on = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    assert on.depth_pull_flag is True
    assert on.liquidity_removed_fraction == 0.25          # 250000 / 1000000, in-range
    assert on.candidate_capable(("depth",))


def test_compute_state_carries_latest_decoded_swap_identity(monkeypatch):
    now = 1_700_000_100
    swaps, bt, contract = _v3_inputs(1_700_000_000)
    for row in swaps:
        row["tx_sig"] = f"0xtx{row['block_no']}"
        row["log_index"] = 0
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset({"4663"}))

    state = compute_state(swaps, bt, contract, cfg=None, now_ts=now)

    assert state.latest_event_id == "4663|0xp|0xtx6|0"


def test_v3_severity_in_range_out_of_range_and_collect():
    # current tick 100, in-range liquidity 1,000,000
    base = [_swap(100, 1_000_000)]
    assert xf.v3_largest_lp_removal_fraction(base + [_burn(50, 150, 250_000)]) == 0.25
    # out-of-range burn does not change executable depth -> discounted to 0
    assert xf.v3_largest_lp_removal_fraction(base + [_burn(200, 300, 999_999)]) == 0.0
    # a Collect (fee claim) never contributes -> no burns -> None
    assert xf.v3_largest_lp_removal_fraction(
        base + [{"kind": "v3_collect", "amount0": "5"}]) is None
    # missing the current tick -> unmeasurable -> None (caller treats as catastrophic)
    assert xf.v3_largest_lp_removal_fraction([_burn(50, 150, 1)]) is None


def test_v3_severity_replays_events_chronologically_and_updates_active_liquidity():
    rows = [
        {**_swap(100, 1_000), "block_ts": 1.0, "block_no": 1, "log_index": 0},
        {**{"kind": "v3_mint", "tick_lower": 50, "tick_upper": 150,
            "liquidity": "200"}, "block_ts": 2.0, "block_no": 2, "log_index": 0},
        {**_burn(50, 150, 300), "block_ts": 3.0, "block_no": 3, "log_index": 0},
        {**_burn(200, 300, 900), "block_ts": 4.0, "block_no": 4, "log_index": 0},
    ]

    # The materializer supplies newest-first rows. Severity is 300 / (1000 + 200), not
    # 300 / the latest swap snapshot and not affected by the out-of-range burn.
    assert xf.v3_largest_lp_removal_fraction(list(reversed(rows))) == 0.25


def test_v3_severity_uses_block_number_when_event_timestamps_are_equal():
    rows = [
        {**_swap(100, 1_000), "block_ts": 1.0, "block_no": 1, "log_index": 0},
        {"kind": "v3_mint", "tick_lower": 50, "tick_upper": 150,
         "liquidity": "200", "block_ts": 1.0, "block_no": 2, "log_index": 0},
        {**_burn(50, 150, 300), "block_ts": 1.0, "block_no": 3, "log_index": 0},
    ]
    assert xf.v3_largest_lp_removal_fraction(list(reversed(rows))) == 0.25


def test_verified_out_of_range_v3_burn_is_not_a_depth_pull_or_cut(monkeypatch):
    now = 1_700_000_100
    swaps, bt, contract = _v3_inputs(1_700_000_000)
    burn = swaps[-1]
    burn["tick_lower"], burn["tick_upper"] = 200, 300
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset({"4663"}))

    state = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    decision = propose_v5(contract, state, cfg=mc)

    assert state.depth_pull_flag is False
    assert state.liquidity_removed_fraction == 0.0
    assert decision.action != CUT


def test_hot_features_include_v3_burn_severity():
    engine = HotPositionEngine(
        7, {"chain_id": "4663", "pool": "0xp", "trail_pct": 30.0}, quote_side=0,
        state_fn=lambda feats, now_ts: None)
    assert engine.add_tick({**_swap(100, 1_000), "block_ts": 1.0, "block_no": 1,
                            "tx_sig": "0xswap", "log_index": 0})
    assert engine.add_tick({**_burn(50, 150, 250), "block_ts": 2.0, "block_no": 2,
                            "tx_sig": "0xburn", "log_index": 0})

    features = engine.features(now_ts=2.0)

    assert features["depth_pull_flag"] is True
    assert features["liquidity_removed_fraction"] == 0.25
    assert features["latest_depth_ts"] == 2.0

    state = hot_state_from_feats(
        features, now_ts=2.0, tape_complete=True, stale_s=180.0, min_points=5,
        mint="COIN", pool="0xp", position_id=7, chain_id="4663", pool_kind="v3",
        v3_depth_verified={"4663"})
    decision = propose_v5({"trail_pct": 30.0}, state, cfg=mc)
    assert state.candidate_capable(("depth",))
    assert decision.action == CUT


def test_hot_v3_depth_stays_untrusted_until_chain_decoder_is_verified():
    engine = HotPositionEngine(
        7, {"chain_id": "4663", "pool": "0xp", "trail_pct": 30.0}, quote_side=0,
        state_fn=lambda feats, now_ts: None)
    engine.add_tick({**_swap(100, 1_000), "block_ts": 1.0, "block_no": 1,
                     "tx_sig": "0xswap", "log_index": 0})
    engine.add_tick({**_burn(50, 150, 900), "block_ts": 2.0, "block_no": 2,
                     "tx_sig": "0xburn", "log_index": 0})
    state = hot_state_from_feats(
        engine.features(now_ts=2.0), now_ts=2.0, tape_complete=True, stale_s=180.0,
        min_points=5, mint="COIN", pool="0xp", position_id=7, chain_id="4663",
        pool_kind="v3", v3_depth_verified=frozenset())

    assert not state.candidate_capable(("depth",))
    assert propose_v5({"trail_pct": 30.0}, state, cfg=mc).action != CUT


def test_fresh_v3_burn_has_fresh_depth_even_when_last_swap_is_old(monkeypatch):
    now = 1_700_001_000
    swaps, bt, contract = _v3_inputs(now - 400)
    burn = swaps[-1]
    burn["block_no"] = 100
    bt[100] = _iso(now - 1)
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset({"4663"}))

    state = compute_state(swaps, bt, contract, cfg=None, now_ts=now)

    assert state.depth_pull_flag is True
    assert state.provenance["depth"].freshness_s == 1.0
    assert state.candidate_capable(("depth",))


def test_verified_catastrophic_v3_burn_cuts_during_structure_warmup(monkeypatch):
    now = 1_700_001_000
    rows = [
        {**_swap(100, 1_000), "pool": "0xp", "block_no": 1, "tx_sig": "0xswap",
         "log_index": 0, "amount0": "100", "amount1": "-50"},
        {**_burn(50, 150, 900), "pool": "0xp", "block_no": 2, "tx_sig": "0xburn",
         "log_index": 0, "amount0": "-1", "amount1": "-1"},
    ]
    block_times = {1: _iso(now - 2), 2: _iso(now - 1)}
    contract = {"chain_id": "4663", "mint": "COIN", "pool": "0xp", "pool_kind": "v3",
                "position_id": 7, "quote_side": 0, "entry_price": 1.0}
    monkeypatch.setattr(mc, "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", frozenset({"4663"}))

    state = compute_state(rows, block_times, contract, cfg=None, now_ts=now)
    decision = propose_v5(contract, state, cfg=mc)

    assert state.state.value == "warmup"
    assert state.depth_pull_flag is True and state.liquidity_removed_fraction == 0.9
    assert state.candidate_capable(("depth",))
    assert decision.action == CUT


def test_wo10_feature_contract_is_versioned():
    assert FEATURE_VERSION == "v4"
