"""Desk paper engine on a GRADUATED (PumpSwap) coin with Delta's exact pool pricing (memecoin_desk_ps_paper,
afa41efb4): ENTER -> fill at decision + 1.0 s on committed PS tape -> mark -> RISK exit at + 1.5 s -> closed ledger row.
Live-shaped tape (Delta's fixture: no metadata.db, canonical-PDA binding), decimals from the ticket's mint read,
full-clip cost, unknown = no fill / prior mark kept, all host clocks."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_paper as paper
from sniper.persistence import db_memecoin_desk as store
from test_desk_ps_asof import MINT, NOW
from test_memecoin_desk_ps_evidence import partial_intake

T0 = NOW - 2.0                        # the JUDGE response_at (host): fill moment T0 + 1.0 <= the PS cut NOW - 0.5
TOKENS, SPEND, SOLD = 619271730832, 250000000, 245445588     # Delta's exact atomic buy/sell on this tape at $25 / SOL $100


def book(tmp_path, monkeypatch, decimals=6):
    desk_dir, _ = partial_intake(tmp_path, derived=True)
    conn = sqlite3.connect(tmp_path / "desk.db")
    conn.row_factory = sqlite3.Row
    paper.ensure(conn)
    store.ensure_tables(conn)
    store.register_cohort(conn, cohort_id="intake", producer="U", prompt_version="test",
                          policy={"venue": "pumpswap", "cumulative_kill_usd": -200}, created_at=paper._iso(T0-500))
    price = 25 / (TOKENS / 10 ** (6 if decimals is None else decimals))
    meta = {"cohort_id": "intake", "verdict_price": {"ref_price_usd": price, "cap_pct": 10.0, "fee_pct": 0.3,
                                                     "impact_pct": 1.0}}
    conn.execute("INSERT INTO desk_paper_positions(ticket_id, intent_id, mint, network_id, cohort_id, version, state, "
                 "requested_at, size_usd, rails_json, desk_metadata_json, verdict_ref_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("desk:U:intake:x", "i", MINT, "solana", "intake", paper.VERSION, "pending_fill", paper._iso(T0), 25.0,
                  json.dumps({"tp_pct": 100, "sl_pct": 40, "trail_pct": None}), json.dumps(meta), price))
    conn.commit()
    monkeypatch.setattr(paper, "_mint_decimals", lambda c, tid: decimals)
    return desk_dir, conn


def cand(tid):
    return {"venue": "pumpswap"}


def beat(desk_dir, at):
    hb = json.loads((desk_dir / "universe.heartbeat").read_text())
    hb.update(ts=at, ps_committed_through=at)
    (desk_dir / "universe.heartbeat").write_text(json.dumps(hb))


def test_enter_fill_mark_exit_on_the_pool_writes_a_closed_ledger_row(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    [r] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    assert r["state"] == "open", r
    row = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    fill = json.loads(row["fill_json"])
    assert row["qty_atomic"] == str(TOKENS) and fill["venue"] == "pumpswap" and fill["decimals"] == 6
    assert fill["spend_lamports"] == SPEND and row["entry_cost_usd"] == pytest.approx(25 + paper.EXEC_COST_SOL["entry"] * 100)
    assert row["filled_at"] == paper._iso(T0 + paper.ENTRY_DELAY_S)
    assert paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0,
                      risk_exits={"desk:U:intake:x": paper.RISK_EXIT_REASON}) == [
        {"ticket_id": "desk:U:intake:x", "state": "exit_requested", "reason": paper.RISK_EXIT_REASON}]
    row = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert row["last_mark_usd"] == pytest.approx(SOLD / 1e9 * 100 / (TOKENS / 1e6))      # marked on the exact pool sell
    assert paper.tick(conn, desk_dir=desk_dir, now=NOW + 0.2, sol_usd=100.0, sol_at=T0) == []   # cut < exit moment: wait
    beat(desk_dir, NOW + 3.0)                                                       # the PS tape commits past it
    [c] = paper.tick(conn, desk_dir=desk_dir, now=NOW + 3.2, sol_usd=100.0, sol_at=T0)
    assert c["state"] == "closed"
    row = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    ex = json.loads(row["exit_json"])
    assert ex["lamports"] == SOLD and ex["venue"] == "pumpswap" and ex["exit_delay_s"] == paper.RISK_EXIT_DELAY_S
    # the exit row keeps the exact quote's proof and binding (Gamma/Alpha 2026-10-28: D exits are auditable)
    assert ex["proof"]["map_source"] == "intake_canonical_pda" and ex["proof"]["as_of_host"] == ex["at"]
    assert ex["proof"]["committed_through"] >= ex["at"] and ex["binding_kind"] == "intake_canonical_pda"
    assert row["closed_at"] == paper._iso(NOW + paper.RISK_EXIT_DELAY_S)
    assert row["pnl_usd"] == pytest.approx(SOLD / 1e9 * 100 - paper.EXEC_COST_SOL["exit"] * 100 - row["entry_cost_usd"])


def test_decimals_come_from_the_mint_read_not_1e6(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch, decimals=9)
    [r] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    row = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert r["state"] == "open" and row["entry_price_usd"] == pytest.approx(25 / (TOKENS / 1e9))


def test_unknown_decimals_or_pool_state_never_fill(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch, decimals=None)
    [r] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    assert (r["state"], r["refusal"]) == ("failed", "entry_failed:decimals_unknown")
    desk_dir2, conn2 = book(tmp_path / "b", monkeypatch)
    t = sqlite3.connect(desk_dir2 / "pumpswap" / "tape.db")
    t.execute("UPDATE swaps_ps SET cashback_fee_bps='1'")                            # unproven native fee: unknown pool
    t.commit()
    t.close()
    [r] = paper.fill_pending(conn2, desk_dir=desk_dir2, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    assert r["state"] == "failed" and r["refusal"].startswith("entry_failed:")


def test_an_unknown_sell_keeps_the_prior_mark(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    before = dict(conn.execute("SELECT last_mark_usd, last_mark_at FROM desk_paper_positions").fetchone())
    monkeypatch.setattr(paper, "_ps_sell", lambda *a, **k: (None, "pool_stale"))
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    after = dict(conn.execute("SELECT last_mark_usd, last_mark_at FROM desk_paper_positions").fetchone())
    assert after == before                                                          # unknown is never 0, never a new mark


def test_the_fill_waits_for_the_ps_cut_not_the_retired_curve_cut(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    beat(desk_dir, T0 + 0.5)                                                        # PS cut short of the fill moment
    assert paper.fill_pending(conn, desk_dir=desk_dir, now=T0 + 1.2, sol_usd=100.0, sol_at=T0, candidate_fn=cand) == []
    assert conn.execute("SELECT state FROM desk_paper_positions").fetchone()[0] == "pending_fill"


@pytest.mark.parametrize("known_fx", [True, False])
def test_proven_zero_pool_sell_marks_zero_at_cut_without_resetting_peak_or_quote_age(tmp_path,monkeypatch,known_fx):
    desk_dir,conn=book(tmp_path,monkeypatch)
    paper.fill_pending(conn,desk_dir=desk_dir,now=NOW,sol_usd=100.0,sol_at=T0,candidate_fn=cand)
    before=dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    tape=sqlite3.connect(desk_dir/"pumpswap/tape.db")
    tape.execute("UPDATE swaps_ps SET post_quote_reserve_atomic='1'")
    tape.commit();tape.close()
    assert paper._ps_sell(desk_dir,MINT,int(before["qty_atomic"]),NOW-.5,NOW)==(None,paper.PROVEN_ZERO)
    paper.tick(conn,desk_dir=desk_dir,now=NOW,sol_usd=100.0 if known_fx else None,sol_at=T0)
    after=dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert after["last_mark_usd"]==0
    assert after["last_mark_at"]==paper._iso(NOW-.5)
    assert (after["peak_price_usd"],after["peak_at"],after["last_quote_at"])==(
        before["peak_price_usd"],before["peak_at"],before["last_quote_at"])


def test_fill_uses_decimals_from_its_persisted_hard_filter_receipt(tmp_path,monkeypatch):
    from sniper.persistence import db_memecoin_desk as store
    from test_desk_ps_asof import candidate
    from test_memecoin_desk_contract import rehash
    with monkeypatch.context() as stub:
        desk_dir,conn=book(tmp_path,stub,decimals=9)
    store.ensure_tables(conn)
    c=candidate()
    c.update(ticket_id="desk:U:intake:x",opportunity_id="desk:U:intake:x")
    c["snapshot"]["fields"]["pool_state"]["value"]["base_decimals"]=9
    rehash(c)
    store.record_candidate(conn,c,cohort_id="intake",now_iso=paper._iso(T0),cooldown_s=0)
    store.record_stage(conn,ticket_id="desk:U:intake:x",stage="hard_filter",seat="code",verdict="PASS",
                       reason="test_mint_receipt",inputs={"evidence":{"checks":{"token_safety":{"status":"pass","decimals":9}}}},
                       ts=paper._iso(T0))
    [out]=paper.fill_pending(conn,desk_dir=desk_dir,now=NOW,sol_usd=100,sol_at=T0,candidate_fn=cand)
    row=dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert out["state"]=="open"
    assert json.loads(row["fill_json"])["decimals"]==9
    assert row["entry_price_usd"]==pytest.approx(25/(TOKENS/10**9))


def test_a_pending_loss_proof_retries_the_same_fill_moment_then_censors(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    real, calls = paper._fill_ps, []

    def flaky(*a, **k):
        calls.append(k["now"])
        if len(calls) == 1:                    # a loss receipt still pending attribution at the first read
            return {"state": "failed", "refusal": "entry_failed:ps_capture_unproven", "decided_at": paper._iso(T0)}
        return real(*a, **k)
    monkeypatch.setattr(paper, "_fill_ps", flaky)
    assert paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand) == []
    assert conn.execute("SELECT state FROM desk_paper_positions").fetchone()[0] == "pending_fill"
    [r] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    assert r["state"] == "open"
    assert conn.execute("SELECT filled_at FROM desk_paper_positions").fetchone()[0] == paper._iso(T0 + paper.ENTRY_DELAY_S)

    desk_dir2, conn2 = book(tmp_path / "b", monkeypatch)
    monkeypatch.setattr(paper, "_fill_ps", lambda *a, **k: {"state": "failed", "refusal": "entry_failed:ps_capture_unproven",
                                                           "decided_at": paper._iso(T0)})
    late = T0 + paper.ENTRY_DELAY_S + paper.FRONTIER_WAIT_S + 0.1
    beat(desk_dir2, late)
    [r] = paper.fill_pending(conn2, desk_dir=desk_dir2, now=late, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    assert (r["state"], r["refusal"]) == ("failed", "entry_failed:ps_capture_unproven")   # never filled blind


# desk_paper_marks (Alpha + Gamma 2026-10-28): one append-only row per mark attempt, refused ones included.
def _marks(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM desk_paper_marks ORDER BY id")]


def test_every_mark_attempt_is_logged_priced_and_refused(tmp_path, monkeypatch):
    import hashlib
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    monkeypatch.setattr(paper, "_ps_sell", lambda *a, **k: (None, "pool_stale"))
    paper.tick(conn, desk_dir=desk_dir, now=NOW + 1, sol_usd=100.0, sol_at=T0)
    priced, refused = _marks(conn)
    pos = conn.execute("SELECT id, cohort_id FROM desk_paper_positions").fetchone()
    assert (priced["position_id"], priced["cohort_id"]) == (pos["id"], pos["cohort_id"])
    assert priced["lamports"] == str(SOLD) and priced["reason"] == "pumpswap"
    assert priced["binding_kind"] == "intake_canonical_pda" and priced["tip_side"] in ("buy", "unknown")
    from sniper.services import memecoin_desk_ps_paper as PSP
    q, _ = PSP.sell(desk_dir, MINT, TOKENS, NOW - .5, evaluated_at=NOW)
    assert priced["proof_sha256"] == hashlib.sha256(json.dumps(q["proof"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert refused["lamports"] is None and refused["reason"] == "pool_stale"
    assert (refused["binding_kind"], refused["tip_side"], refused["proof_sha256"]) == ("unknown", "unknown", None)


def _old_row(conn, age_s):
    import time as _t
    conn.execute("INSERT INTO desk_paper_marks(position_id, cohort_id, at_host, at_ts, lamports, reason, binding_kind, "
                 "tip_side, proof_sha256, run_id, tick_seq) VALUES (1,'intake','x',?,NULL,'r','unknown','unknown',NULL,'old',1)",
                 (_t.time() - age_s,))
    conn.commit()


def test_the_marks_log_is_insert_only_inside_retention(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE desk_paper_marks SET lamports='1'")
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM desk_paper_marks")
    conn.rollback()
    _old_row(conn, paper.MARKS_RETENTION_S + 86400)
    assert paper.prune_marks(conn) == 1                                       # wall clock: only the 15-day-old row
    assert len(_marks(conn)) == 1


def test_the_prune_cutoff_is_forward_only_and_never_inside_retention(tmp_path, monkeypatch):
    import time as _t
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.prune_marks(conn)
    cutoff = conn.execute("SELECT cutoff_ts FROM desk_paper_marks_prune").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError):                               # lowering it
        conn.execute("UPDATE desk_paper_marks_prune SET cutoff_ts=?", (cutoff - 1,))
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError):                               # past now - 14 d would free recent rows
        conn.execute("UPDATE desk_paper_marks_prune SET cutoff_ts=?", (_t.time() - paper.MARKS_RETENTION_S + 3600,))
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM desk_paper_marks_prune")
    conn.rollback()
    with pytest.raises(sqlite3.DatabaseError):                               # prune_marks with a future 'now' too
        paper.prune_marks(conn, now=_t.time() + 86400)


def test_mark_rows_carry_a_run_id_and_a_gapless_tick_seq(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    for k in range(3):
        paper.tick(conn, desk_dir=desk_dir, now=NOW + k, sol_usd=100.0, sol_at=T0)
    rows = _marks(conn)
    assert {r["run_id"] for r in rows} == {paper.RUN_ID}
    seqs = [r["tick_seq"] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + 3))                          # one per tick, no gap inside a run


def test_a_marks_log_failure_never_breaks_the_mark(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    conn.execute("DROP TABLE desk_paper_marks"); conn.commit()
    monkeypatch.setattr(paper, "ensure", lambda c: None)                    # keep the table gone for this tick
    before = paper.MARK_LOG_ERRORS["mark_log_errors"]
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    row = dict(conn.execute("SELECT last_mark_usd FROM desk_paper_positions").fetchone())
    assert row["last_mark_usd"] == pytest.approx(SOLD / 1e9 * 100 / (TOKENS / 1e6))
    assert paper.MARK_LOG_ERRORS["mark_log_errors"] == before + 1


def test_tick_prunes_marks_past_retention_at_most_hourly(tmp_path, monkeypatch):
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    _old_row(conn, paper.MARKS_RETENTION_S + 86400)
    monkeypatch.setattr(paper, "_LAST_PRUNE", [float("-inf")])
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    assert [m["run_id"] for m in _marks(conn)] == [paper.RUN_ID]              # the old row aged out, the new one kept


def test_tick_seq_is_unique_across_concurrent_writers(monkeypatch):
    """Module globals are defects until proven (crew rule): two threads writing marks never share a tick_seq."""
    import sys, threading
    seen, lock = [], threading.Lock()

    class Conn:                                           # records the tick_seq each batch was given
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, rows):
            with lock:
                seen.extend(r[-1] for r in rows)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                           # force thread switches inside the allocation
    try:
        row, start = {"id": 1, "cohort_id": "intake"}, threading.Barrier(4)
        def writer():
            start.wait()
            for _ in range(20000):
                paper._write_marks(Conn(), [paper._mark_row(row, NOW, None, "r", {})])
        threads = [threading.Thread(target=writer) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        sys.setswitchinterval(old)
    assert len(seen) == 80000 and len(set(seen)) == 80000


def test_a_mark_row_build_failure_never_breaks_the_mark_and_leaves_a_gap(tmp_path, monkeypatch):
    """Zeta 1d5d5dd20: building the log row (iso/hash/JSON) can fail too; the mark still lands, the failure is
    counted and the lost row shows as a tick_seq gap inside the run."""
    desk_dir, conn = book(tmp_path, monkeypatch)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0, candidate_fn=cand)
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100.0, sol_at=T0)
    real, before = paper._mark_row, paper.MARK_LOG_ERRORS["mark_log_errors"]

    def fail(*a, **k):
        raise ValueError("row build failed")
    monkeypatch.setattr(paper, "_mark_row", fail)
    paper.tick(conn, desk_dir=desk_dir, now=NOW + 1, sol_usd=100.0, sol_at=T0)
    assert conn.execute("SELECT last_mark_at FROM desk_paper_positions").fetchone()[0] == paper._iso(NOW - .5)
    assert paper.MARK_LOG_ERRORS["mark_log_errors"] == before + 1
    monkeypatch.setattr(paper, "_mark_row", real)
    paper.tick(conn, desk_dir=desk_dir, now=NOW + 2, sol_usd=100.0, sol_at=T0)
    seqs = [m["tick_seq"] for m in _marks(conn)]
    assert len(seqs) == 2 and seqs[1] - seqs[0] == 2                          # the failed tick is a visible gap
