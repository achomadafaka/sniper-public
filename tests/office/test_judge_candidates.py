"""Judge frames carry verified near-pass sheet receipts (mint, chart, flow, holders, price, narrative lens)."""
import json

from sniper.office import runtime as R, store as S
from sniper.office.tools import sheet as SH


def _receipt(conn, *, at, mint="M1", sheet=None, epoch="e1"):
    request = {"network_id": "solana", "mint": mint, "pool": "P1", "feed": "near_pass"}
    body = sheet or {"mint": mint, "verdict_price": 0.0001, "chart": {"high_1h": 2}, "ps_facts": {"flow_60s": {"sell_n": 3}},
                     "d_blocks": {"holders": {"top10_pct": 31}}, "safety_proof": {"mint": mint}}
    lenses = {"ps_trending": {"narrative_refs": ["x:1"]}}
    rec = {"kind": "sheet", "state": "known", "contract": SH.OFFICE_SHEET_CONTRACT["version"], "cache_key": SH._sha(request),
           "request": request, "definition_bundle_hash": "b" * 64, "strategy_epoch": epoch,
           "cut": {"basis": "corrected_utc"}, "clock_sample": {"corrected_utc": at, "offset_s": 0.0, "sampled_at": at},
           "sheet": body, "sheet_hash": SH._sha(body), "lenses": lenses, "lenses_hash": SH._sha(lenses)}
    return S.append(conn, "receipts", rec)


def test_judge_frame_carries_candidate_fact_paths(tmp_path):
    conn = S.open_store(tmp_path / "office.db")
    row = _receipt(conn, at=1000.0)
    out = R.judge_candidates(conn, cut=1100.0)
    assert out["state"] == "known" and out["omitted_n"] == 0 and len(out["items"]) == 1
    item = out["items"][0]
    assert item["receipt_row_hash"] == row and item["mint"] == "M1"
    s = item["sheet"]
    assert s["mint"] == "M1" and s["verdict_price"] and s["chart"] and s["ps_facts"]["flow_60s"] and s["d_blocks"]["holders"]
    assert item["lenses"]["ps_trending"]["narrative_refs"] == ["x:1"]


def test_only_causal_fresh_intact_sheets(tmp_path):
    conn = S.open_store(tmp_path / "office.db")
    _receipt(conn, at=2000.0, mint="FUTURE")
    _receipt(conn, at=1100.0 - R.JUDGE_SHEET_MAX_AGE_S - 1, mint="STALE")
    assert R.judge_candidates(conn, cut=1100.0) == {"state": "known", "items": [], "omitted_n": 0}


def test_budget_omits_and_counts(tmp_path):
    conn = S.open_store(tmp_path / "office.db")
    for i in range(3):
        _receipt(conn, at=1000.0 + i, mint="M%d" % i, sheet={"mint": "M%d" % i, "pad": "x" * (R.JUDGE_SHEET_BUDGET // 2)})
    out = R.judge_candidates(conn, cut=1100.0)
    assert [i["mint"] for i in out["items"]] == ["M2"] and out["omitted_n"] == 2
    assert len(json.dumps(out)) < R.JUDGE_SHEET_BUDGET


def test_eval_cohort_filter_keeps_only_the_frozen_cohort(tmp_path):
    conn = S.open_store(tmp_path / "office.db")
    _receipt(conn, at=1000.0)                                           # strategy_epoch e1
    assert R.judge_candidates(conn, cut=1100.0, cohort_id="e1")["items"]
    v28 = "desk-cohort:U:c7b5728fa973a49fd82269d60d0ebed64e62c405c1d7848cbcfc405c480b770f"
    core = "desk-cohort:U:b92bbebfa7466c7eb764a25f744533b41effde95d11de6b4a6e69661147a1fe7"
    _receipt(conn, at=1001.0, mint="M2", epoch=v28)
    out = R.judge_candidates(conn, cut=1100.0, cohort_id=core)
    assert out["items"] == [] and out["other_cohort_n"] == 2 and out["cohort_id"] == core     # the c7b5728f receipt refused


def test_sheet_tool_refuses_a_ticket_from_another_cohort(tmp_path):
    import sqlite3
    desk = tmp_path / "desk.db"; c = sqlite3.connect(desk)
    c.execute("CREATE TABLE desk_ticket_heads(ticket_id TEXT PRIMARY KEY, cohort_id TEXT)")
    c.executemany("INSERT INTO desk_ticket_heads VALUES(?,?)", [("t-core", "core"), ("t-v28", "c7b5728f")]); c.commit(); c.close()
    assert R.ticket_cohort_refusal(desk, "t-core", "core") is None
    assert R.ticket_cohort_refusal(desk, "t-v28", "core") == "eval_cohort_mismatch"
    assert R.ticket_cohort_refusal(desk, "t-none", "core") == "ticket_unknown"
