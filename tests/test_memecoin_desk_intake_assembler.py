"""Desk intake step 4: the intake assembler ingests an IntakeBook emission into an intake cohort, reads the mint
once, and the intake-v1 hard filter records the stage the JUDGE claims from."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import memecoin_desk_intake_assembler as IA
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_intake_book as B
from test_memecoin_desk_contract import evidence as base_evidence, policy
from test_memecoin_desk_intake_book import NOW, printed, sight

INTAKE = {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 1}


def emission(tmp_path):
    sight(tmp_path, NOW - 30)
    b = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "e.db")
    b.reload(NOW)
    [(_, body)] = printed(b, NOW)
    return json.loads(body)


def cohort(conn):
    hard = {**policy(), "intake": INTAKE}
    D.register_cohort(conn, cohort_id="intake", producer="U", prompt_version="desk-judge-v26",
                      policy={"clip_usd": 25, "venue": "pumpswap", "hard_filter": hard, "evidence_wait_s": 30},
                      created_at=datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat())
    return hard


class FakeRpc:
    timeout_s = 1.0

    def __init__(self, reply=None, err=None):
        self.reply, self.err, self.calls = reply, err, []

    def clock(self):
        return NOW + 3

    def __call__(self, method, params, deadline=None):
        self.calls.append((method, params))
        return self.reply, self.err


def test_an_intake_emission_reaches_a_hard_filter_pass_with_one_mint_read(tmp_path):
    cand = emission(tmp_path)
    conn = D.connect(tmp_path / "desk.db")
    hard = cohort(conn)
    got = desk.ingest_candidate(conn, cand, cohort_id="intake", now_iso=datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat())
    assert got["is_primary"]
    rpc = FakeRpc(reply={"context": {"slot": 99}, "value": [{"data": ["", "base64"], "owner": "x"}]})
    seen = {}

    def ps_evidence(c, **kw):
        seen.update(kw)
        e = base_evidence(c)
        e["known_at"] = datetime.fromtimestamp(kw["evaluated_at_host"], timezone.utc).isoformat()
        e["checks"]["token_safety"].update(supply_atomic="1000000000000000", decimals=6)
        return e
    verdict, reason, _ = IA.evaluate(cand, got["ticket_id"], desk_conn=conn, desk_dir=tmp_path, rpc=rpc, clip_usd=25,
                                     hard_policy=hard, now_fn=lambda: NOW + 4, evidence_fn=ps_evidence)
    assert (verdict, reason) == ("PASS", "pass")
    assert [m for m, _ in rpc.calls] == ["getMultipleAccounts"]
    assert seen["mint_account"]["context_slot"] == 99 and seen["mint_known_at"] == NOW + 3
    from sniper.services.memecoin_desk_intake_book import batch_end
    assert seen["as_of_host"] == batch_end(NOW) and seen["evaluated_at_host"] == NOW + 4   # Am36 batch end, then evaluation


def test_a_failed_mint_read_reaches_the_evidence_as_unknown_and_refuses(tmp_path):
    cand = emission(tmp_path)
    conn = D.connect(tmp_path / "desk.db")
    hard = cohort(conn)
    got = desk.ingest_candidate(conn, cand, cohort_id="intake", now_iso=datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat())

    def ps_evidence(c, **kw):
        assert kw["mint_account"] is None and kw["mint_known_at"] is None
        e = base_evidence(c)
        e["known_at"] = datetime.fromtimestamp(kw["evaluated_at_host"], timezone.utc).isoformat()
        e["checks"]["token_safety"] = {"status": "unknown", "reason": "mint_account_missing"}
        return e
    verdict, reason, _ = IA.evaluate(cand, got["ticket_id"], desk_conn=conn, desk_dir=tmp_path,
                                     rpc=FakeRpc(err="timeout"), clip_usd=25, hard_policy=hard,
                                     now_fn=lambda: NOW + 4, evidence_fn=ps_evidence)
    assert verdict == "SKIP" and reason == "unknown:token_safety:mint_account_missing"


def test_real_ps_evidence_and_real_intake_v1_filter_end_to_end(tmp_path):
    """Delta's REAL pumpswap_evidence (0ced1097f) on a live-shaped tape (no metadata.db, no create_pool row, labelled
    canonical-PDA binding) -> the REAL intake-v1 hard filter: the integration Zeta and Delta asked step 4 to own."""
    from datetime import datetime, timezone
    from sniper.services import memecoin_desk_contract as K
    from test_memecoin_desk_contract import rehash
    from test_memecoin_desk_ps_evidence import NOW as T, evidence as ps_evidence, partial_intake
    desk_dir, c = partial_intake(tmp_path, derived=True, absent=False)
    fields = c["snapshot"]["fields"]
    c["snapshot"]["missing"] = sorted(n for n, r in fields.items() if r["value"] is None or r["completeness"] != "complete")
    c = rehash(c)
    K.validate_candidate(c)
    e = ps_evidence(desk_dir, c)
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 1}}
    got = K.hard_filter(c, evidence=e, policy=hard, evaluated_at=datetime.fromtimestamp(T + .1, timezone.utc).isoformat())
    assert got["reason"] in ("pass", "kill:mcap_safety", "kill:liq_min"), (got, e["checks"])
    assert got["reason"] == "pass", (got, {k: v for k, v in e["checks"].items()})


def test_a_ticket_failing_the_free_pool_check_spends_zero_mint_reads(tmp_path):
    """Alpha 2026-10-28: 1,273 of 2,000 daily mint reads went to tickets that then failed pool_tip_unproven on the
    local tape. The free pool check runs first; a failure records its own reason and never reaches the paid read."""
    cand = emission(tmp_path)
    conn = D.connect(tmp_path / "desk.db")
    hard = cohort(conn)
    got = desk.ingest_candidate(conn, cand, cohort_id="intake", now_iso=datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat())
    rpc, reads, collected = FakeRpc(reply={"context": {"slot": 99}, "value": [{"data": ["", "base64"], "owner": "x"}]}), [], []

    def ps_evidence(c, **kw):
        assert kw["mint_account"] is None and kw["mint_known_at"] is None
        e = base_evidence(c)
        e["known_at"] = datetime.fromtimestamp(kw["evaluated_at_host"], timezone.utc).isoformat()
        return e
    verdict, reason, evidence = IA.evaluate(
        cand, got["ticket_id"], desk_conn=conn, desk_dir=tmp_path, rpc=rpc, clip_usd=25, hard_policy=hard,
        now_fn=lambda: NOW + 4, evidence_fn=ps_evidence, precheck_fn=lambda c, **kw: "pool_tip_unproven",
        on_read=lambda: reads.append(1), collect=lambda e: collected.append(1) or {})
    assert (verdict, reason) == ("SKIP", "unknown:market_data:pool_tip_unproven")
    assert rpc.calls == [] and reads == [] and collected == []                  # no paid read, no paid holder reads
    assert evidence["checks"]["token_safety"] == {"status": "unknown", "reason": "mint_read_skipped:pool_tip_unproven"}


def test_a_ticket_passing_the_free_pool_check_reads_the_mint_once_and_reports_it(tmp_path):
    cand = emission(tmp_path)
    conn = D.connect(tmp_path / "desk.db")
    hard = cohort(conn)
    got = desk.ingest_candidate(conn, cand, cohort_id="intake", now_iso=datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat())
    rpc, reads, seen = FakeRpc(reply={"context": {"slot": 99}, "value": [{"data": ["", "base64"], "owner": "x"}]}), [], {}

    def ps_evidence(c, **kw):
        e = base_evidence(c)
        e["known_at"] = datetime.fromtimestamp(kw["evaluated_at_host"], timezone.utc).isoformat()
        e["checks"]["token_safety"].update(supply_atomic="1000000000000000", decimals=6)
        return e

    def precheck(c, **kw):
        seen.update(kw)
        return None
    verdict, reason, _ = IA.evaluate(cand, got["ticket_id"], desk_conn=conn, desk_dir=tmp_path, rpc=rpc, clip_usd=25,
                                     hard_policy=hard, now_fn=lambda: NOW + 4, evidence_fn=ps_evidence,
                                     precheck_fn=precheck, on_read=lambda: reads.append(len(rpc.calls)))
    assert (verdict, reason) == ("PASS", "pass")
    assert [m for m, _ in rpc.calls] == ["getMultipleAccounts"] and reads == [0]     # reported BEFORE the call
    from sniper.services.memecoin_desk_intake_book import batch_end
    assert seen["as_of_host"] == batch_end(NOW) and seen["clip_usd"] == 25          # Am36 batch end


def test_pool_refusal_is_the_evidence_pool_check_without_the_mint(tmp_path):
    """The free check is the same pool code pumpswap_evidence runs after the mint; it needs no mint account."""
    from sniper.services import memecoin_desk_ps_evidence as PE
    from test_memecoin_desk_ps_evidence import NOW as T, partial_intake
    desk_dir, c = partial_intake(tmp_path, derived=True, absent=False)
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    kw = dict(desk_dir=desk_dir, as_of_host=T - .5, evaluated_at_host=T + .1, clip_usd=25,
              sol_usd=state["sol_usd"], sol_at=T - .5 - state["sol_age_s"])
    assert PE.pool_refusal(c, **kw) is None
    assert PE.pool_refusal(c, **{**kw, "sol_usd": None}) == "sol_usd_unknown"
    import sqlite3
    t = sqlite3.connect(desk_dir / "pumpswap" / "tape.db"); t.execute("UPDATE swaps_ps SET is_buy=0"); t.commit(); t.close()
    from test_memecoin_desk_ps_evidence import evidence as full_evidence
    refusal = PE.pool_refusal(c, **kw)
    assert refusal is not None and full_evidence(desk_dir, c)["checks"]["market_data"]["reason"] == refusal  # same code



def test_am36_market_data_reason_carries_the_cut_basis():
    from sniper.services.memecoin_desk_intake_book import batch_end
    c = {"snapshot": {"emission_at": batch_end(100.5), "fields": {"pool_state": {"value": {"reserve_known_at": 100.5}}}}}
    assert IA.cut_basis(c) == "batch_end"
    c["snapshot"]["emission_at"] = 101.2
    assert IA.cut_basis(c) == "processing" and IA.cut_basis({}) is None
