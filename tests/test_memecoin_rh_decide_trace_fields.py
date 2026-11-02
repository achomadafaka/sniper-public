"""4663 fast-lane decisions name what they refused on (Alpha P1, 2026-10-14).

Grading the 78 `state_stale` buys that never reached would_send (scripts/research/rh_state_stale_grade.py)
found 59 ungradable: the pre-state the decide refused on was not stored, and the tape that held it
keeps about an hour. And `source_swap_ambiguous` never said which ambiguity it was: koy's Banksy fill
(0x24f5c579, 2026-10-14 11:21Z) refused on its receipt proof's source_post reason, unrecorded. Now a
decision row carries the pool state it judged (state_sqrt, state_liquidity) and the cause of an
ambiguous source (source_post_reason: the receipt proof's own reason, or tape_swap_shape /
tape_hit_conflict on the tape path). Trace only: no reason, gate or route changes.
"""
import sqlite3

from sniper.services import memecoin_rh_fast_decide as fast
from test_memecoin_rh_fast_decide import B, CTX, SIG, WPOOL, ZPOOL, ZUP_IN, ZUP_PRE, _paid_for, _row, _tape
from test_memecoin_rh_source_binding import CTX as BOUND_CTX, SIG as BOUND_SIG, _source_tape


def test_a_stale_refusal_records_the_pool_state_it_refused():
    tape = _tape([_row(ZPOOL, B - 50, "0xpre", ZUP_PRE["sqrt"]), _row(WPOOL, B - 1, "0xx", 1)])
    rec = fast.decide(dict(SIG, paid=_paid_for(ZUP_IN)), tape=tape, **CTX)
    assert rec["reason"] == "state_stale"
    assert rec["state_sqrt"] == ZUP_PRE["sqrt"] and rec["state_liquidity"] > 0


def test_a_receipt_proofs_own_reason_names_the_ambiguity():
    from rh_source_fill_fixture import proof_for
    tape = _source_tape([200])
    proof = proof_for(BOUND_SIG, got=200, pool=ZPOOL)
    proof["source_post"] = {"reason": "v4_key_unknown"}
    tape.learn_source_fills([proof])
    out = fast.decide(dict(BOUND_SIG, paid=_paid_for(ZUP_IN)), tape=tape, **BOUND_CTX)
    assert out["reason"] == "source_swap_ambiguous" and out["source_post_reason"] == "v4_key_unknown"


def test_the_tape_paths_ambiguities_are_named_too():
    shape = fast.decide(dict(BOUND_SIG, paid=_paid_for(ZUP_IN)), tape=_source_tape([100, -50]), **BOUND_CTX)
    assert (shape["reason"], shape["source_post_reason"]) == ("source_swap_ambiguous", "tape_swap_shape")
    tape = _source_tape([200])
    tape.ingest([dict(next(iter(tape._rows.values()))[0], amount1=-300)])
    conflict = fast.decide(dict(BOUND_SIG, paid=_paid_for(ZUP_IN)), tape=tape, **BOUND_CTX)
    assert (conflict["reason"], conflict["source_post_reason"]) == ("source_swap_ambiguous",
                                                                     "tape_hit_conflict")


def test_the_decision_row_stores_both(tmp_path):
    db = str(tmp_path / "shadow.db")
    out = dict(reason="source_swap_ambiguous", handoff_ms=1.0, decide_ms=0.1,
               source_post_reason="v4_key_unknown", state_sqrt=ZUP_PRE["sqrt"], state_liquidity=7)
    fast._insert_decision(db, dict(SIG), out, {}, None)
    row = sqlite3.connect(db).execute(
        "SELECT reason, source_post_reason, state_sqrt, state_liquidity FROM rh_fast_decisions").fetchone()
    assert row == ("source_swap_ambiguous", "v4_key_unknown", str(ZUP_PRE["sqrt"]), "7")
