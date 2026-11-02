"""Office sheet tool (plan Task 5 as amended: sheet(candidate, *, desk_dir, policy, clock_sample, hard_filter_row, now,
store_conn, buyer_ledger, strategy_epoch)). The D readers are stubbed here and count their reads; the D blocks
themselves are pinned by tests/test_desk_d_sheet_contract.py (build_d_blocks byte-equal to the worker)."""
import copy
import json
import sqlite3

import pytest

from test_memecoin_desk_judge import databases  # noqa: F401  (fixture for the real-tape parity test)

from sniper.office import store as S
from sniper.office.tools import sheet as T

NOW = 1_800_000_000.0
PASS_AT = "2026-01-15T08:00:50+00:00"            # host 1768464050: after the market cut, before the snapshot


SNAP_AT = 1_768_464_100.0                       # host time of the candidate snapshot, after the PASS


def candidate(pool="POOL1", mint="MINT1", snap="s" * 64, at=SNAP_AT):
    return {"network_id": "solana", "mint": mint, "pool": pool, "snapshot": {"hash": snap, "emission_at": at}}


def hard(**over):
    row = {"verdict": "PASS", "ts": PASS_AT, "market_cut": {"receipt_cut": 1_768_464_000.0, "cut_slot": 900},
           "evidence": {"known_at": PASS_AT}, "source_metadata": None, "emission_candidate": candidate(snap="e" * 64)}
    row.update(over)
    return row


class Reads:
    def __init__(self, monkeypatch):
        self.n = 0
        self.price = 1.0

        def facts(c, **kw):
            self.n += 1
            return {"version": "pumpswap-facts-v3", "flow_60s": {"sell_n": 2}, "as_of": kw["as_of"],
                    "receipt_cut": kw["receipt_cut"], "cut_slot": kw["cut_slot"], "price": self.price}
        monkeypatch.setattr(T.F, "facts_for", facts)
        monkeypatch.setattr(T.K, "validate_ps_inputs", lambda p, v: True)
        monkeypatch.setattr(T.K, "validate_candidate", lambda c: c)
        monkeypatch.setattr(T.K, "safety_from_evidence", lambda c, e, **kw: {"authority_mint": "closed"})
        monkeypatch.setattr(T.DB, "build_d_blocks", lambda c, f, **kw: {"clock": {"offset_s": kw["clock"][0]}})
        monkeypatch.setattr(T.K, "render_judge_sheet", lambda c, **kw: {"ps_facts": kw["ps_facts"],
                                                                        "d_blocks": kw["d_blocks"]})
        monkeypatch.setattr(T.T, "trending_for", lambda c, **kw: {"version": "t", "as_of": kw["as_of"]})
        monkeypatch.setattr(T.T, "validate", lambda b: b)


@pytest.fixture
def env(tmp_path, monkeypatch):
    reads = Reads(monkeypatch)
    conn = S.open_store(tmp_path / "office.db")
    yield conn, reads
    conn.close()


CALL_NOW = SNAP_AT + 30


def call(conn, *, c=None, policy=None, clock=None, row=None, epoch="e1", now=CALL_NOW):
    return T.sheet(c or candidate(), desk_dir="/d", policy=policy or {"rail_bounds": {}, "hard_filter": {}, "v": 1},
                   clock_sample=clock if clock is not None else {"offset_s": 0.25, "sampled_at": CALL_NOW - 10},
                   hard_filter_row=row or hard(), now=now, store_conn=conn, buyer_ledger=[], strategy_epoch=epoch)


def freeze(conn, decision_id, r, epoch="e1"):
    return T.freeze_bundle(conn, decision_id, r["cache_key"], definition_bundle_hash=r["definition_bundle_hash"],
                           strategy_epoch=epoch)


def test_known_sheet_is_stored_once_with_corrected_cuts_and_the_labelled_host_cut(env):
    conn, reads = env
    r = call(conn)
    assert r["state"] == "known" and r["cut"]["basis"] == "corrected_utc"
    assert r["cut"]["receipt_cut"] == 1_768_464_000.0 - 0.25 and r["cut"]["host_receipt_cut"] == 1_768_464_000.0
    assert r["cut"]["host_receipt_cut"] == r["cut"]["receipt_cut"] + r["clock_sample"]["offset_s"]   # Delta's contract
    assert r["cut"]["knowledge_cut"] == SNAP_AT - 0.25 and r["clock_sample"]["corrected_utc"] == CALL_NOW - 0.25
    assert r["cut"]["knowledge_cut"] <= r["clock_sample"]["corrected_utc"]         # T4: cut <= decision frontier
    assert r["sheet_hash"] == T._sha(r["sheet"])
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_identical_requests_share_one_entry(env):
    conn, reads = env
    a, b = call(conn), call(conn)
    assert a == b and reads.n == 1 and conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


@pytest.mark.parametrize("change", [dict(c=candidate(pool="POOL2"), row=hard(emission_candidate=candidate(pool="POOL2"))), dict(policy={"rail_bounds": {}, "hard_filter": {}, "v": 2}),
                                    dict(epoch="e2"), dict(clock={"offset_s": 0.5, "sampled_at": CALL_NOW - 10}),
                                    dict(c=candidate(snap="t" * 64))])
def test_same_mint_cut_never_collides_across_pools_policies_epochs_clocks_snapshots(env, change):
    conn, reads = env
    a = call(conn)
    b = call(conn, **change)
    assert a["cache_key"] != b["cache_key"] and reads.n == 2


@pytest.mark.parametrize("clock", [{"offset_s": None, "sampled_at": None},
                                   {"offset_s": float("nan"), "sampled_at": CALL_NOW - 10},
                                   {"offset_s": True, "sampled_at": CALL_NOW - 10}, {},
                                   {"offset_s": 0.25, "sampled_at": 1.0},                       # ancient sample
                                   {"offset_s": 0.25, "sampled_at": CALL_NOW - 3601},           # past the D max age
                                   {"offset_s": 0.25, "sampled_at": CALL_NOW + 5}])             # a future sample
def test_unknown_offset_is_typed_unknown_without_read(env, clock):
    conn, reads = env
    r = call(conn, clock=clock)
    assert (r["state"], r["reason"]) == ("unknown", "clock_unknown") and reads.n == 0
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize("row", [dict(verdict="KILL"), dict(verdict="SKIP")])
def test_only_a_hard_filter_pass_gets_a_sheet(env, row):
    conn, reads = env
    r = call(conn, row=hard(**row))
    assert (r["state"], r["reason"]) == ("refused", "not_hard_filter_pass") and reads.n == 0


def test_judges_get_identical_bundle_late_update_ignored(env):
    """The sheet's knowledge time is the PASS persist time: a price row arriving between judge A and judge B cannot
    change what B reads, and one decision has exactly one bundle."""
    conn, reads = env
    a = call(conn)
    h1 = freeze(conn, "dec1", a)
    reads.price = 2.0                                                          # the tape moved on
    b = call(conn)
    assert b == a and freeze(conn, "dec1", b) == h1
    other = call(conn, epoch="e2")
    with pytest.raises(ValueError, match="bundle_conflict"):
        freeze(conn, "dec1", other, epoch="e2")
    assert conn.execute("SELECT COUNT(*) FROM decision_bundles").fetchone()[0] == 1


def test_future_backfilled_row_keeps_earlier_sheet_byte_identical(env):
    conn, reads = env
    a = call(conn)
    raw = conn.execute("SELECT payload_json FROM receipts").fetchone()[0]
    reads.price = 99.0                                                         # a backfill lands after the receipt
    assert json.dumps(call(conn), sort_keys=True) == json.dumps(a, sort_keys=True)
    assert conn.execute("SELECT payload_json FROM receipts").fetchone()[0] == raw
    assert S.verify_chain(conn, "receipts")[0]


def test_a_failed_read_is_typed_and_not_cached(env, monkeypatch):
    conn, reads = env
    monkeypatch.setattr(T.F, "facts_for", lambda c, **kw: (_ for _ in ()).throw(ValueError("tape_missing")))
    r = call(conn)
    assert r["state"] == "unknown" and r["reason"] == "sheet_unavailable:tape_missing"
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_the_context_facts_lens_is_never_exposed():
    src = (T.__file__)
    assert "memecoin_desk_context_facts" not in open(src).read().split('"""', 2)[2]



# ---- Zeta HOLD 9a810d361

def test_a_tampered_cached_receipt_is_typed_corrupt_never_returned(env):
    conn, reads = env
    r = call(conn)
    rec = json.loads(conn.execute("SELECT payload_json FROM receipts").fetchone()[0])
    rec["sheet"]["ps_facts"]["price"] = 999                                    # body changed, sheet_hash kept
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='receipts'").fetchall():
        conn.execute("DROP TRIGGER " + name)                                   # an attacker or a bug got past the triggers
    conn.execute("UPDATE receipts SET payload_json=?", (S.canonical_json(rec).decode(),))
    conn.commit()
    out = call(conn)
    assert (out["state"], out["reason"]) == ("unknown", "receipt_corrupt") and reads.n == 1     # no hiding re-read


def test_freeze_bundle_never_trusts_a_caller_sheet(env):
    conn, reads = env
    with pytest.raises(ValueError, match="bundle_receipt_missing"):
        T.freeze_bundle(conn, "d1", "f" * 64, definition_bundle_hash="x", strategy_epoch="e1")
    r = call(conn)
    with pytest.raises(ValueError, match="bundle_receipt_corrupt"):          # wrong bundle/epoch binding
        T.freeze_bundle(conn, "d1", r["cache_key"], definition_bundle_hash="x", strategy_epoch="e1")
    h = freeze(conn, "d1", r)
    b = json.loads(conn.execute("SELECT payload_json FROM decision_bundles").fetchone()[0])
    assert b["receipt_row_hash"] == r["row_hash"] and freeze(conn, "d1", r) == h


@pytest.mark.parametrize("change,why", [
    (dict(c=candidate(at=1_768_463_000.0)), "market_cut_after_sheet"),          # market cut after the snapshot
    (dict(row=hard(ts="2026-01-15T08:01:41+00:00")), "snapshot_before_pass"),          # PASS after the snapshot
    (dict(now=SNAP_AT - 1, clock={"offset_s": 0.25, "sampled_at": SNAP_AT - 20}), "snapshot_before_pass_or_future"),
])
def test_causal_cuts_or_refused(env, change, why):
    conn, reads = env
    r = call(conn, **change)
    assert r["state"] in ("refused", "unknown") and why.split("_or_")[0] in r["reason"] and reads.n == 0


def test_the_office_contract_names_its_intentional_diffs():
    assert "ps_trending" in T.OFFICE_SHEET_CONTRACT["lenses"]
    assert any("desk-context-v1" in d for d in T.OFFICE_SHEET_CONTRACT["intentional_diffs_from_the_desk_judge_sheet"])


@pytest.mark.parametrize("offset", [0.0, 100.0])
def test_real_tape_sheet_matches_the_worker_sheet_byte_for_byte(databases, tmp_path, monkeypatch, offset):
    """Zeta HOLD 9a810d361: one end-to-end sheet through sheet() on a real tape + hard-filter fixture (real facts_for,
    validators, D blocks and renderer), equal to the desk worker's frozen ps_inputs sheet for the same candidate,
    PASS row and clock sample."""
    import copy as _copy
    from scripts import memecoin_desk_judge_worker as W
    from sniper.persistence import db_memecoin_desk as D
    import sqlite3
    from sniper.services import memecoin_desk_judge as J
    from test_desk_ps_inputs_integration import block, refresh
    from test_desk_ps_model_facts import NOW as TNOW
    from test_desk_v28_judge_inputs import ready
    tape = tmp_path / "tape"
    ticket, _ = ready(databases, tape)
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], TNOW + 1)
    safety = block(t, c, meta, TNOW + 1, tmp=tape)["safety"]
    monkeypatch.setattr(J.contract, "safety_from_evidence", lambda *a, **kw: _copy.deepcopy(safety))
    real = D.get_cohort

    def cohort(conn, cid):
        row = real(conn, cid)
        row["policy"] = {k: v for k, v in row["policy"].items() if k not in ("judge_provider", "judge_effort")}
        row["policy"].update(ps_inputs="pumpswap-facts-v3", judge_model="claude-opus-5-5", **J.contract.d_components())
        row["prompt_version"] = "desk-judge-v29"
        return row
    monkeypatch.setattr(W.store, "get_cohort", cohort)
    monkeypatch.setattr(W, "read_clock_sample", lambda path, *, now: (offset, TNOW + 1))
    import sniper.persistence.db_core as dbc
    import sniper.persistence.db_memecoin as dbm
    monkeypatch.setattr(dbc, "get_readonly_connection", lambda *a, **k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(dbm, "desk_buyer_ledger", lambda conn: [])          # no default/production DB read
    out = W.make_ps_inputs_fn(databases[0], tape, lambda _: [])(t, c, meta, TNOW + 1)
    worker_sheet = J.contract.render_judge_sheet(c, **out)
    stage = next(s for s in t["stages"] if s["stage"] == "hard_filter")
    pol = cohort(databases[0], t["cohort_id"])["policy"]
    source = W.store.get_intake_source_receipt(databases[0], t["opportunity_id"], cohort_id=t["cohort_id"],
                                               hard_policy=pol["hard_filter"], candidate=t["candidate"], as_of=TNOW + 1)
    row = {"verdict": "PASS", "ts": stage["ts"], "evidence": stage["inputs"]["evidence"],
           "market_cut": {"receipt_cut": meta["receipt_cut"], "cut_slot": meta["cut_slot"]},
           "source_metadata": source.get("source_metadata"), "emission_candidate": t["candidate"]}
    ledger = []                                                                # the same injected ledger for both paths
    store = S.open_store(tmp_path / "office.db")
    r = T.sheet(c, desk_dir=tape, policy=pol,
                clock_sample={"offset_s": offset, "sampled_at": TNOW + 1}, hard_filter_row=row, now=TNOW + 1,
                store_conn=store, buyer_ledger=ledger, strategy_epoch="e1")
    # Heat is the one deliberate difference: the Office has no read budget for it (Alpha, Gamma 2026-10-30).
    assert r["sheet"]["heat"]["reason"] == "read_budget" and r["read_budget_s"]["heat"] == 0
    worker_sheet = _copy.deepcopy(worker_sheet)
    for holder in (worker_sheet, worker_sheet["d_blocks"]):
        holder["heat"] = r["sheet"]["heat"]
    worker_sheet["price_basis"] = r["sheet"]["price_basis"]
    worker_sheet["safety_evidence"] = r["sheet"]["safety_evidence"]    # second deliberate difference: reported evidence on Office sheets only (operator 2026-10-31)
    assert r["state"] == "known", r
    dump = lambda d: json.dumps(d, sort_keys=True, separators=(",", ":"), default=repr).encode()
    def diff(a, b, path=""):
        if isinstance(a, dict) and isinstance(b, dict):
            return [d for k in sorted(set(a) | set(b)) for d in diff(a.get(k), b.get(k), path + "." + k)]
        return [] if a == b else [(path, a, b)]
    added = ("required", "missing_required", "optional_unknown")           # the Office frame's declared-required block
    assert r["sheet"] == T.with_required(worker_sheet)
    body = {k: v for k, v in r["sheet"].items() if k not in added}
    assert diff(body, worker_sheet) == [] and dump(body) == dump(worker_sheet)



def test_a_refreshed_candidate_for_another_pool_than_the_passed_one_is_refused(env):
    conn, reads = env
    r = call(conn, c=candidate(pool="POOL2"))
    assert r["state"] == "refused" and "emission_candidate_mismatch" in r["reason"] and reads.n == 0



# ---- Zeta HOLD 3670eddca

def test_an_earlier_pass_with_the_same_snapshot_is_another_request(env):
    """The PASS ceiling, judged snapshot, evidence and source proof are in the key: an earlier PASS never gets a sheet
    built for a later one."""
    conn, reads = env
    late = call(conn)
    early = call(conn, row=hard(ts="2026-01-15T08:00:40+00:00"))
    assert early["cache_key"] != late["cache_key"] and early["cut"]["pass_cut"] == 1768464040.0 - 0.25
    assert reads.n == 2
    for change in (dict(evidence={"known_at": PASS_AT, "x": 1}), dict(source_metadata={"m": 1}),
                   dict(emission_candidate=candidate(snap="f" * 64))):
        assert call(conn, row=hard(**change))["cache_key"] != late["cache_key"]


def test_a_hit_must_match_the_full_request(env):
    conn, reads = env
    r = call(conn)
    rec = json.loads(conn.execute("SELECT payload_json FROM receipts").fetchone()[0])
    assert rec["request"]["cut"]["pass_cut"] == r["cut"]["pass_cut"] and T._sha(rec["request"]) == r["cache_key"]


def test_the_decision_frontier_is_the_call_time_not_the_offset_sample(env):
    conn, reads = env
    r = call(conn, clock={"offset_s": 0.25, "sampled_at": SNAP_AT - 1})        # a valid sample 1 s before the snapshot
    assert r["state"] == "known" and r["cut"]["knowledge_cut"] <= r["clock_sample"]["corrected_utc"]
    assert r["clock_sample"]["sampled_at"] == SNAP_AT - 1


@pytest.mark.parametrize("raw", ["not json", "[1,2]"])
def test_a_malformed_cached_row_is_typed_corrupt(env, raw):
    conn, reads = env
    r = call(conn)                                    # the store's CHECK already refuses such rows; _verify still types them
    hit = {"payload_json": raw, "prev_hash": "", "row_hash": "x"}
    assert T._verify(hit, r["cache_key"], r["definition_bundle_hash"], "e1")["reason"] == "receipt_corrupt"


@pytest.mark.parametrize("column,value", [("row_hash", "f" * 64), ("prev_hash", "e" * 64),
                                          ("payload_json", None)])
def test_a_tampered_existing_bundle_is_corrupt_never_returned(env, column, value):
    """Zeta HOLD dbd6bc386: the idempotent retry re-verifies the stored bundle row before returning its hash."""
    conn, reads = env
    r = call(conn)
    freeze(conn, "d1", r)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='decision_bundles'").fetchall():
        conn.execute("DROP TRIGGER " + name)
    if value is None:                                  # same JSON object, non-canonical spacing: digest no longer matches
        raw = conn.execute("SELECT payload_json FROM decision_bundles").fetchone()[0]
        value = json.dumps(json.loads(raw), indent=1)
    conn.execute("UPDATE decision_bundles SET %s=?" % column, (value,))
    conn.commit()
    with pytest.raises(ValueError, match="bundle_corrupt"):
        freeze(conn, "d1", r)


def test_the_sheet_declares_which_facts_are_required_and_types_the_curve_era_ones_optional(env):
    """GPT Judge WAITed missing_input on curve-era fields a graduated PumpSwap sheet can never carry (live 2026-10-29)."""
    conn, _ = env
    s = call(conn)["sheet"]
    assert s["required"] == list(T.REQUIRED) and "chart" in s["required"]
    assert {k for k in T.CURVE_ERA if s.get(k) is None} <= set(s["optional_unknown"]) and not set(T.CURVE_ERA) & set(s["required"])
    assert s["missing_required"] == sorted(k for k in s["required"] if s.get(k) is None)
    assert not set(s["missing_required"]) & set(T.CURVE_ERA)


def test_a_sheet_reports_safety_evidence_and_passes_its_fields_to_the_renderer(env, monkeypatch):
    conn, _ = env
    r = call(conn)
    block = r["sheet"]["safety_evidence"]
    assert r["state"] == "known" and block["cluster"]["store_decision"]["reason"] == "cluster_store_unreadable" and block["lp"]["reason"] == "lp_reader_off"   # no store, no reader: still a sheet, labelled
    seen = {}
    render = T.K.render_judge_sheet
    monkeypatch.setattr(T.K, "render_judge_sheet", lambda c, **kw: (seen.update(kw["safety"]), render(c, **kw))[1])
    monkeypatch.setattr(T.SE, "build", lambda **kw: (True, False, {"version": "x"}))
    assert call(conn, epoch="e2")["sheet"]["safety_evidence"] == {"version": "x"} and seen["cluster_hit"] is True and seen["lp_locked_or_burned"] is False
