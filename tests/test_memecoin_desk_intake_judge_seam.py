"""Desk intake step 4b end to end up to the JUDGE call: a real intake candidate (live-shaped tape: no metadata.db, no
create_pool row, canonical-PDA binding) -> ingest into an intake cohort -> Delta's REAL pumpswap_evidence -> the REAL
intake-v1 hard filter PASS -> the ticket is selected without a ranker -> intake_mint_of rebuilds the mint read from
the stored evidence -> Delta's REAL ps_asof intake refresh at the claim -> desk.pumpswap_unpriced_reason is None, so
run_once would spend a model call (not a preflight SKIP)."""
from datetime import datetime, timezone

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_asof as AS
from sniper.services import memecoin_desk_judge as J
from test_desk_ps_asof_intake import NOW, partial_intake
from test_memecoin_desk_contract import policy
from test_memecoin_desk_ps_evidence import evidence as ps_evidence, mint


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def test_an_intake_ticket_reaches_a_priced_judge_claim(tmp_path):
    desk_dir, c = partial_intake(tmp_path, derived=True)
    conn = D.connect(tmp_path / "desk.db")
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 1}}
    D.register_cohort(conn, cohort_id="intake", producer="U", prompt_version="desk-judge-v26",
                      policy={"clip_usd": 25, "venue": "pumpswap", "hard_filter": hard, "evidence_wait_s": 30,
                              "select_version": J.INTAKE_SELECT_VERSION, "judge_p_admit": 1.0,
                              "judge_calls_per_hour": 40}, created_at=iso(NOW - 3600))
    print_at = c["snapshot"]["emission_at"]
    got = desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(print_at + 0.1))
    assert got["is_primary"]
    # the assembler: one mint read between the print and the claim (Alpha: print <= read < claim)
    read_at, evaluated, claim = NOW, NOW + 0.1, NOW + 0.2
    st = c["snapshot"]["fields"]["pool_state"]["value"]
    e = ps_evidence(desk_dir, c, as_of_host=print_at, sol_at=print_at - 2,
                    mint_account=mint(), mint_known_at=read_at, evaluated_at_host=evaluated)
    filtered = desk.evaluate_candidate(conn, got["ticket_id"], evidence=e, policy=hard, now_iso=iso(evaluated))
    assert (filtered["verdict"], filtered["reason"]) == ("PASS", "pass"), e["checks"]
    ticket = D.get_ticket(conn, got["ticket_id"])
    picks = J._select(conn, J.ranker_gate(tmp_path), limit=10, now=datetime.fromtimestamp(claim, timezone.utc))
    assert [(t["ticket_id"], s) for t, s in picks] == [(ticket["ticket_id"], "shortlisted")]      # no ranker
    intake_mint = J.intake_mint_of(ticket)
    assert intake_mint["supply_atomic"] == "1000000000000000" and intake_mint["authority_mint"] == "closed"
    assert intake_mint["context_slot"] == 21
    refreshed, _ = AS.refresh(ticket["candidate"], desk_dir=desk_dir, now=claim, intake_mint=intake_mint)
    assert desk.pumpswap_unpriced_reason(refreshed) is None                  # the JUDGE seat would make a call
    price = desk.priced_verdict(refreshed, clip_usd=25, basis="asof")
    assert price is not None and price["mcap_usd"] > 0


