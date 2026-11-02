"""Replay fidelity regressions (Codex review 2026-07-24).

The replay is the only place a v5 verdict comes from, so a defect here is indistinguishable from
an engine result. Both bugs below made replay disagree with production on the same tape.
"""
import os
import sqlite3
import sys
import inspect
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import scripts.research.memecoin_v5_blind_replay as r        # noqa: E402
from datetime import datetime, timezone


def test_replay_module_has_no_tick_tape_write_path():
    source = inspect.getsource(r)
    assert "connect_ticks(" not in source
    assert "backfill_block_times" not in source


def _ticks_db(rows, pool="0xpool", mint="0xbase"):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE pool_ticks (chain_id TEXT, pool TEXT, kind TEXT, block_no INTEGER, "
              "tx_sig TEXT, log_index INTEGER, maker TEXT, amount0 TEXT, amount1 TEXT, observed_at TEXT)")
    c.execute("CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT, "
              "quote_side INTEGER, token0_decimals INTEGER, token1_decimals INTEGER, resolved_at TEXT)")
    c.execute("CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts INTEGER, fetched_at TEXT)")
    c.execute("INSERT INTO pool_meta VALUES ('4663',?,?,?,0,18,18,'x')", (pool, "0xquote", mint))
    for b, ts, a0, a1, kind in rows:
        c.execute("INSERT INTO pool_ticks VALUES ('4663',?,?,?,?,0,'0xm',?,?,'x')",
                  (pool, kind, b, f"0x{b:064x}", a0, a1))
        c.execute("INSERT INTO block_times VALUES ('4663',?,?,'x')", (b, ts))
    c.commit()
    return c


def test_replay_keeps_every_block_in_a_shared_second():
    """4663 makes ~10 blocks/s and block_times are second-resolution, so the old {ts: px} map kept
    ONE block per second and threw the other ~9 away — replay ran on a per-second tape."""
    base = 1_700_000_000
    rows = [(100 + i, base + (i // 10), "100", "-50", "v2_swap") for i in range(30)]
    rows.append((130, base + 3, "1000000", "2000000", "v2_sync"))
    tc = _ticks_db(rows)
    path = r.exec_price_path_per_block(
        [dict(x) | {"block_ts": base + ((x["block_no"] - 100) // 10)}
         for x in tc.execute("SELECT * FROM pool_ticks WHERE kind='v2_swap' ORDER BY block_no DESC")],
        quote_side=0)
    blocks = [b for _ts, _px, _n, b in path]
    assert len(blocks) == 30, "distinct blocks sharing a second were merged"
    assert blocks == sorted(blocks), "blocks must stay chronological inside a shared timestamp"


def test_replay_derisk_is_one_shot_like_production():
    """propose_v5 honours derisked_at, but replay never stamped it — so replay halved the bag on
    every sweep while production halves it once. Same tape, different answer."""
    from sniper.services.memecoin_exit_brain import DERISK, propose_v5
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
    fams = ("price", "flow", "vol", "depth")
    prov = {f: Provenance("pool_ticks", quality="trusted") for f in fams}
    st = MarketState(chain_id="4663", mint="C", pool="p", position_id=1, engine_version="v",
                     feature_version="v1", state=StateLabel.TRENDING, required_families=fams,
                     provenance=prov, lower_high_state=True, realized_vol=0.30)
    contract = {"trail_pct": 20.0}
    stamped = None
    fired = 0
    for _sweep in range(5):                      # five production sweeps over one position
        d = propose_v5({**contract, "derisked_at": stamped}, st)
        if d.action == DERISK:
            fired += 1
            stamped = 1_700_000_000              # what the ledger stamps
    assert fired == 1, f"DERISK fired {fired}x across sweeps; the ledger guard makes it one-shot"


def test_replay_v5_uses_production_selector_and_decision_fraction(monkeypatch):
    """Catches a nominal v5 replay still calling propose_v3 or accounting every partial as 0.5."""
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel

    base = 1_700_000_000
    rows = [(100 + i, datetime.fromtimestamp(base + i * 30, tz=timezone.utc).isoformat(),
             "100", "-50", "v2_swap") for i in range(12)]
    rows.append((99, datetime.fromtimestamp(base - 1, tz=timezone.utc).isoformat(),
                 "1000000", "500000", "v2_sync"))
    tc = _ticks_db(rows)
    fams = ("price", "flow", "vol", "depth")
    prov = {f: Provenance("pool_ticks", quality="trusted") for f in fams}

    def weakening(*_args, **_kwargs):
        return MarketState(
            chain_id="4663", mint="0xbase", pool="0xpool", position_id=1,
            engine_version="v", feature_version="v1", state=StateLabel.WEAKENING,
            required_families=fams, provenance=prov, lower_high_state=True,
            realized_vol=0.60, net_flow_quote="-1")

    monkeypatch.setattr(r, "compute_state", weakening)
    pos = {
        "id": 1, "mint": "0xbase", "strategy": "control",
        "opened_at": datetime.fromtimestamp(base, tz=timezone.utc).isoformat(),
        "entry_price": 2.0, "entry_notional": 100.0, "realized_pnl": 0.0,
        "entry_mcap": 100_000.0,
    }
    out = r.replay_position(tc, None, pos, fetch_headers=False, engine_version="v5")
    assert out["engine_version"] == "exit-brain-v5"
    assert "v5_pnl_h0" in out and "v3_pnl_h0" not in out
    assert out["partial_fractions"] == [pytest.approx(7 / 12)]


def test_replay_take_profit_obeys_the_executor_one_shot_claim(monkeypatch):
    """The funded adapter sends both partial actions through one CAS claim. Replay must not sell
    repeated TAKE_PROFIT decisions after the first claim has settled."""
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel

    base = 1_700_000_000
    rows = [(100 + i, datetime.fromtimestamp(base + i * 30, tz=timezone.utc).isoformat(),
             "100", "-50", "v2_swap") for i in range(12)]
    rows.append((99, datetime.fromtimestamp(base - 1, tz=timezone.utc).isoformat(),
                 "1000000", "500000", "v2_sync"))
    tc = _ticks_db(rows)
    fams = ("price", "flow", "vol", "depth")
    prov = {f: Provenance("pool_ticks", quality="trusted") for f in fams}

    def climax(*_args, **_kwargs):
        return MarketState(
            chain_id="4663", mint="0xbase", pool="0xpool", position_id=1,
            engine_version="v", feature_version="v1", state=StateLabel.TRENDING,
            required_families=fams, provenance=prov, higher_low_state=True,
            arrival_accel=2.0, realized_vol=0.60, net_flow_quote="1")

    monkeypatch.setattr(r, "compute_state", climax)
    pos = {
        "id": 1, "mint": "0xbase", "strategy": "control",
        "opened_at": datetime.fromtimestamp(base, tz=timezone.utc).isoformat(),
        "entry_price": 2.0, "entry_notional": 100.0, "realized_pnl": 0.0,
        "entry_mcap": 100_000.0,
    }
    out = r.replay_position(tc, None, pos, fetch_headers=False, engine_version="v5")
    assert out["partial_fractions"] == [pytest.approx(7 / 12)]


def test_replay_uses_the_position_frozen_trail_and_band(monkeypatch):
    policy = type("P", (), {
        "policy_id": "funded-mid-v1", "coin_band": "mid", "trail_pct": 35.0,
    })()
    monkeypatch.setattr(r, "frozen_policy_from_position", lambda _pos: policy)
    assert r._policy_fields({"entry_mcap": 100_000.0}) == (35.0, "mid")
