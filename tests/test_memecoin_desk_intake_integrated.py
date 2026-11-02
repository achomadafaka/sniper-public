"""ONE integrated offline run of the desk intake slice (Alpha 2026-10-27), live-shaped (no metadata.db, no create_pool
row, the pool bound by its canonical PDA): an intake row -> IntakeBook emission on the next proven BUY print ->
intake assembler with ONE mint read + Delta's REAL pumpswap_evidence -> intake-v1 PASS -> JUDGE pick without a ranker
-> intake_mint_of -> REAL ps_asof intake refresh at the claim -> the priced JUDGE sheet the model would see (model
call stubbed: the sheet is the artifact)."""
import json
import sqlite3
import sys
import pytest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import memecoin_desk_intake_assembler as IA
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_asof as AS
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_intake_book as B
from sniper.services import memecoin_desk_judge as J
from test_desk_ps_asof import MINT, NOW, POOL, A
from test_memecoin_desk_contract import policy
from test_memecoin_desk_ps_evidence import fixture, mint

SOL_CAIP = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
PRINT_AT, SLOT = NOW - 10, 18            # the fixture tape's BUY print (40 SOL quote reserve)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class MintRpc:
    timeout_s = 1.0

    def __init__(self):
        self.calls = 0

    def clock(self):
        return NOW                                        # the mint read completes after the print, before the claim

    def __call__(self, method, params, deadline=None):
        self.calls += 1
        assert method == "getMultipleAccounts" and params[0] == [MINT]
        acc = mint()
        return {"context": {"slot": acc.pop("context_slot")}, "value": [acc]}, None


@pytest.mark.parametrize("feed,filt", [("pumpfun", "active"), ("fomo", "trending24h"),
                                       ("dexscreener", "boost_latest")])
def test_one_intake_listing_reaches_a_priced_judge_sheet(tmp_path, feed, filt):
    desk_dir, _ = fixture(tmp_path)                       # Delta's live-shaped PS tape + fresh heartbeat
    tape = sqlite3.connect(desk_dir / "pumpswap" / "tape.db")
    [(SIG,)] = tape.execute("SELECT signature FROM swaps_ps WHERE slot=?", (SLOT,)).fetchall()   # the print itself
    tape.execute("DELETE FROM pools")                     # no create_pool row (410 of 415 live graduated coins)
    tape.execute("UPDATE swaps_ps SET map_status='unmapped', map_known_at=NULL, mint=NULL, quote_mint=NULL")
    tape.commit()
    tape.close()
    meta_db = desk_dir / "pumpswap" / "metadata.db"
    if meta_db.exists():
        meta_db.unlink()                                  # metadata.db does not exist live
    # 1. An enrolled feed lists the coin (host clock, offset 0)
    out = I.open_day(desk_dir / "intake", feed, PRINT_AT - 1)
    I.record_poll(out, feed, filt, PRINT_AT - 1, [{"network_id": "solana", "chain_raw": SOL_CAIP, "mint": MINT,
                                                            "venue": "pumpswap", "raw": {"complete": True}}], offset_s=0.0)
    out.close()
    # 2. the universe worker's IntakeBook emits on the next proven BUY print (bound by the canonical PDA, no metadata)
    book = B.IntakeBook(intake_dir=desk_dir / "intake", db_path=tmp_path / "emitter.db", feeds=(feed,))
    book.reload(PRINT_AT - 0.5)
    assert book.pools == {POOL: MINT}
    binding = {"pool": POOL, "mint": MINT, "quote_mint": K.WSOL_MINT, "known_at": PRINT_AT, "slot": SLOT,
               "canonical": True, "map_source": "intake_canonical_pda"}
    event = {"kind": "buy", "pool": POOL, "post_base_reserve_atomic": "100000000000000",
             "post_quote_reserve_atomic": "40000000000", "lp_fee_bps": 20, "protocol_fee_bps": 5, "creator_fee_bps": 5,
             "cashback_fee_bps": 0, "virtual_quote_atomic": "0", "virtual_quote_source": A.VQ_SOURCE}
    [(oid, body)] = book.on_print(event=event, mapping=binding, metadata=None, slot=SLOT, signature=SIG, ordinal=0,
                                  tx_index=None, now=PRINT_AT, received_at=PRINT_AT, sol_usd=100, sol_usd_at=PRINT_AT - 1)
    book.committed(oid)
    cand = json.loads(body)
    assert cand["snapshot"]["fields"]["pool_state"]["completeness"] == "partial"
    # 3. the intake assembler: ingest, ONE mint read, Delta's real evidence, the intake-v1 hard filter
    conn = D.connect(tmp_path / "desk.db")
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000],
                                  "liq_min_quote_sol": 40, "feeds": [feed]}}
    D.register_cohort(conn, cohort_id="intake", producer="U", prompt_version="desk-judge-v26",
                      policy={"clip_usd": 25, "venue": "pumpswap", "hard_filter": hard, "evidence_wait_s": 30,
                              "select_version": J.INTAKE_SELECT_VERSION, "judge_p_admit": 1.0,
                              "judge_calls_per_hour": 40, "judge_order": J.INTAKE_ORDER, "judge_defer_max_s": 120,
                              "rail_bounds": {"sl_pct": [10, 40], "tp_pct": [15, 150], "trail_pct": [8, 30]}},
                      created_at=iso(NOW - 3600))
    got = desk.ingest_candidate(conn, cand, cohort_id="intake", now_iso=iso(PRINT_AT + 0.1))
    assert got["is_primary"] and got["ticket_id"] == oid
    rpc = MintRpc()
    verdict, reason, evidence = IA.evaluate(cand, oid, desk_conn=conn, desk_dir=desk_dir, rpc=rpc, clip_usd=25,
                                            hard_policy=hard, now_fn=lambda: NOW + 0.1)
    assert (verdict, reason) == ("PASS", "pass"), evidence["checks"]
    assert rpc.calls == 1 and evidence["checks"]["token_safety"]["mint_context_slot"] == 21
    # 4. the JUDGE seat: picked without a ranker, the mint read rebuilt from the stored evidence, the as-of refresh
    claim = NOW + 0.2
    [(ticket, state)] = J._select(conn, J.ranker_gate(tmp_path), limit=10, now=datetime.fromtimestamp(claim, timezone.utc))
    assert state == "shortlisted" and ticket["ticket_id"] == oid
    refreshed, _ = AS.refresh(ticket["candidate"], desk_dir=desk_dir, now=claim, intake_mint=J.intake_mint_of(ticket))
    assert desk.pumpswap_unpriced_reason(refreshed) is None
    # 5. the sheet the model would see (the model call itself is stubbed out of this offline test)
    sheet = K.render_judge_sheet(refreshed)
    assert sheet["trigger"] == "intake" and sheet["venue"] == "pumpswap"
    assert sheet["intake"] == {"feed": feed, "filter": filt, "provider_rank": 1}
    assert sheet["supply_basis"] == "supply_at_read" and sheet["verdict_price"]["mcap_usd"] > 0
    assert sheet["liq_usd"] == 40 * 100.0 and sheet["curve_pct"] is None
    # 6. the REAL run_once spends exactly one model call on it, with that sheet (model + recorder stubbed; Zeta da8b1fc49)
    import contextlib
    seen = []

    def call_model(system, user, **kw):
        seen.append(json.loads(user))
        return {"text": '{"v":"S"}'}
    import pytest
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(J, "_record", lambda *a, **k: {"recorded": True})
        J.run_once(conn, lambda: contextlib.nullcontext(), call_model=call_model, system_prompt="desk-judge-v26",
                   worker="w", now_fn=lambda: datetime.fromtimestamp(claim, timezone.utc), calls_per_hour=100,
                   shortlist_fn=J.ranker_gate(tmp_path), admit_fn=lambda t: True,
                   asof_fn=lambda c, ts, intake_mint=None: AS.refresh(c, desk_dir=desk_dir, now=ts, intake_mint=intake_mint))
    finally:
        mp.undo()
    assert len(seen) == 1, seen
    assert seen[0]["trigger"] == "intake" and seen[0]["intake"] == {"feed": feed, "filter": filt, "provider_rank": 1}
    assert seen[0]["supply_basis"] == "supply_at_read" and seen[0]["verdict_price"]["mcap_usd"] > 0
