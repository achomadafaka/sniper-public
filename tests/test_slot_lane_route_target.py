"""A slot-lane fill records its entry POOL keys in quote_route_target, the shape the J7 path writes.

Every slot-lane fill stored the venue label 'slot_lane', so vol_death's route check (exit route's
token-facing ammKey must be one of the entry's keys) could never match and held every lane position
as route_changed for good (65860, 2026-09-26). The J7 path writes the Jupiter routePlan ammKeys,
comma-joined; the lane now writes its USDC -> SOL leg's ammKeys, then the coin-facing key: the
bonding-curve PDA for a curve buy, the pool for a PumpSwap buy.

The curve facts are REAL: positions 65813 and 65826 (2026-09-26, entry txs 4w8rnq62... and
51riMts1...) each passed the derived PDA as account 3 of their pump.fun buy instruction.
"""
import contextlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps
from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.persistence import db_memecoin as m

FIX = Path(__file__).parent / "fixtures"
REAL_CURVES = {   # position: (mint, the bonding curve its entry tx passed to pump.fun, account 3)
    65813: ("Gm9gqiHuLE1wTUUfFLxGXmtmPfYZ7gT6UdhS2QqNC1xn", "A5t7cp29FqHjgG3UrFJM7UFte9eVqUCsow9yKXVnp8C3"),
    65826: ("4FTu9EadVeuYrxoWN37umrNDxnUcCQexzYsSatDCV5pt", "Fm2s1SPRD4PxXzUviP8no345cfFTmrU9pDSXxo6vBJGB"),
}
LEG_QUOTE = {"routePlan": [{"swapInfo": {"ammKey": "BisonLegPool111111111111111111111111111111"}}]}


@pytest.mark.parametrize("pid", sorted(REAL_CURVES))
def test_the_curve_route_names_the_bonding_curve_the_real_entry_used(pid):
    mint, curve = REAL_CURVES[pid]
    assert pcb.curve_route_target(mint) == curve
    assert pcb.curve_route_target(mint, {"quote": LEG_QUOTE}) == \
        "BisonLegPool111111111111111111111111111111," + curve


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_the_curve_builder_publishes_its_route_target(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    row = json.loads((FIX / "memecoin" / "slot_price_limit_a47a6c14.json").read_text())[0]
    event = pcb.parse_trade_event(row["trade_event_log"])
    b = pcb.SlotLaneCurveBuilder(user=event["user"], jup_call=lambda *a, **k: None,
                                 rpc=lambda *a: None, clock=_Clock())
    b._buyback = event["fee_recipient"]
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": 175_000_000, "min_out_lamports": 1_418_853_206,
              "out_lamports": 1_418_853_206, "fetched_at": _Clock.t, "quote": LEG_QUOTE}
    buy = SimpleNamespace(mint=event["mint"], token_program=pcb.TOKEN_PROGRAM,
                          trade_event_log=row["trade_event_log"])
    snap = SimpleNamespace(blockhash="11111111111111111111111111111111", last_valid_height=1,
                           sol_usd=121.5)
    _msg, validity = b.build(buy, 175.0, snap)
    assert validity["route_target"] == pcb.curve_route_target(event["mint"], b._leg)


def test_the_pumpswap_builder_publishes_the_pool(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    frames = json.loads((FIX / "pumpswap_buy_frames.json").read_text())
    live, acc = frames["direct_buy_exact_quote_in"], frames["direct_buy_exact_quote_in"]["accounts"]
    event = ps.parse_buy_event(live["buy_event_log"])
    facts = ps.parse_pool_account(frames["pool_account_b64"])
    facts.update(base_token_program=acc[11][0], quote_token_program=acc[12][0], base_decimals=6)
    b = ps.SlotLanePumpSwapBuilder(user=acc[1][0], jup_call=lambda *a, **k: None,
                                   rpc=lambda *a: None, clock=_Clock())
    b._buyback = acc[24][0]
    b.pools._pools[event["pool"]] = facts
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(5.0),
              "min_out_lamports": 30_000_000, "out_lamports": 30_400_000, "fetched_at": _Clock.t,
              "quote": LEG_QUOTE}
    snap = SimpleNamespace(blockhash="11111111111111111111111111111111", last_valid_height=1,
                           sol_usd=175.0)
    _msg, validity = b.build(SimpleNamespace(pool=event["pool"], trade_event_log=live["buy_event_log"],
                                             mint=None), 5.0, snap)
    assert validity["route_target"] == "BisonLegPool111111111111111111111111111111," + event["pool"]


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


@pytest.mark.parametrize("validity, stored", [
    ({"route_target": "Leg1,A5t7cp29FqHjgG3UrFJM7UFte9eVqUCsow9yKXVnp8C3"},
     "Leg1,A5t7cp29FqHjgG3UrFJM7UFte9eVqUCsow9yKXVnp8C3"),
    (None, "slot_lane"),                     # no builder route: the venue label, as before
])
def test_the_fill_stores_the_builders_route_target(conn, validity, stored):
    from sniper.execution.solana_real_seams import ConfirmState
    pid = m.reserve_pending(conn, mint="Gm9gqiHuLE1wTUUfFLxGXmtmPfYZ7gT6UdhS2QqNC1xn",
                            source_user_id="notdecu", strategy="alpha_b", notional=170.5,
                            reserved_at="2026-09-26T21:33:39+00:00", network_id="solana",
                            origin_event_id="slotlane:sig", signal_source="slot_lane",
                            source_price=1e-5)
    result = execute_prebuilt_entry(
        conn, pending=m.pending_row(conn, pid), signed_b64="AA==", sig="SigRoute",
        validity=validity, send_fn=lambda _b64: True,
        confirm_fn=lambda _sig: ConfirmState("confirmed", landing_slot=450796648),
        receipt_fill_fn=lambda *_: (170.5, 15_000_000.0, 6),
        now_iso="2026-09-26T21:33:40+00:00", sleep_fn=lambda _s: None)
    assert result["status"] == "filled"
    pos = conn.execute("SELECT quote_route_target FROM memecoin_positions WHERE id=?",
                       (result["position_id"],)).fetchone()[0]
    fill = conn.execute("SELECT quote_route_target FROM memecoin_fills WHERE id=?",
                        (result["fill_id"],)).fetchone()[0]
    assert pos == fill == stored
