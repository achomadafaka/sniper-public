"""JUDGE-time soft facts: the bound cluster row that landed since the hard filter replaces the stored one."""
import json
from datetime import datetime, timezone

import test_memecoin_desk_contract as T
from sniper.services import memecoin_desk_soft_facts as SF

NOW = 1_800_000_000.0
DAY = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def ticket(checks=None):
    c = T.candidate()
    ev = T.evidence(c)
    if checks is not None:
        ev["checks"] = checks
    return {"candidate": c, "stages": [{"stage": "hard_filter", "inputs": {"evidence": ev}}]}


def row(t, **over):
    c = t["candidate"]
    out = {"schema": "desk-cluster/v1", **{k: c[k] for k in ("ticket_id", "network_id", "mint", "pool")},
           "snapshot_hash": c["snapshot"]["hash"], "known_at": iso(NOW - 1),
           "check": {"status": "kill", "reason": "cluster_top_funder_high"},
           "metrics": {"attempted_supply": 100, "resolved_supply": 100, "eligible": 5,
                       "top_funder_of_attempted": 0.61, "clustered_of_attempted": 0.7}}
    out.update(over)
    return out


def write(tmp, *rows, raw=()):
    d = tmp / "cluster_checks"
    d.mkdir(exist_ok=True)
    (d / (DAY + ".jsonl")).write_text("\n".join([json.dumps(r) for r in rows] + list(raw)) + "\n")


def facts(tmp, t):
    return SF.for_ticket(t, desk_dir=tmp, now=NOW)


def test_row_landing_after_the_filter_becomes_a_ready_fact(tmp_path):
    t = ticket({"market_data": {"status": "pass"}})                      # no cluster at filter time
    assert facts(tmp_path, t)["cluster"]["reason"] == "not_yet_ready"
    write(tmp_path, row(t))
    c = facts(tmp_path, t)["cluster"]
    assert (c["state"], c["check"], c["top_funder_high"], c["top_funder_pct_of_attempted"]) == ("ready", "flagged", True, 61.0)


def test_unbound_future_or_foreign_rows_never_bind(tmp_path):
    t = ticket({})
    write(tmp_path, row(t, snapshot_hash="0" * 64), row(t, known_at=iso(NOW + 5)), row(t, mint="other"),
          row(t, schema="desk-cluster/v0"), raw=["{torn"])
    c = facts(tmp_path, t)["cluster"]
    assert c["state"] == "unknown" and c["reason"] == "not_yet_ready"


def test_newest_bound_row_wins_and_holders_come_from_the_stored_evidence(tmp_path):
    t = ticket()
    write(tmp_path, row(t, check={"status": "unknown", "reason": "cluster_unknown_budget"}),
          row(t, check={"status": "pass", "reason": "cluster_clear"}))
    out = facts(tmp_path, t)
    assert out["cluster"]["check"] == "pass" and out["holders"]["top10_pct"] == 20


def test_unreadable_cluster_file_is_an_unknown_fact_not_a_raise(tmp_path):
    t = ticket({})
    d = tmp_path / "cluster_checks"
    d.mkdir()
    (d / (DAY + ".jsonl")).mkdir()                                       # reading a directory raises OSError
    c = facts(tmp_path, t)["cluster"]
    assert c["state"] == "unknown" and c["reason"] == "unreadable"


def test_stage_without_evidence_still_yields_unknown_facts(tmp_path):
    t = {"candidate": T.candidate(), "stages": [{"stage": "hard_filter", "inputs": {"evidence": None}}]}
    out = facts(tmp_path, t)
    assert out["holders"]["reason"] == "missing" and out["cluster"]["reason"] == "not_yet_ready"


REAL_METRICS = {"attempted_supply": 1581521323316586, "clustered_of_attempted": 0.3125, "clustered_supply": 494225413536433,
                "coverage": 0.9, "eligible": 7, "largest_funder_supply": 300000000000000, "resolved": 7,
                "resolved_supply": 1423369190984927, "top_funder_of_attempted": 0.5555}


def real_row(t, status, reason, metrics):
    """Shaped like the cluster worker's real output: percentages live ONLY in metrics, nothing is injected into check."""
    return row(t, check={"status": status, "reason": reason}, metrics=metrics, decision_id="d", policy_id="p", rpc_calls=3)


def test_real_shaped_row_percentages_come_from_metrics(tmp_path):
    t = ticket({})
    complete = {**REAL_METRICS, "coverage": 1.0, "resolved_supply": REAL_METRICS["attempted_supply"]}
    write(tmp_path, real_row(t, "kill", "cluster_top_funder_high", complete))
    c = facts(tmp_path, t)["cluster"]
    assert (c["check"], c["top_funder_high"], c["top_funder_pct_of_attempted"], c["clustered_pct_of_attempted"]) == ("flagged", True, 55.55, 31.25)
    write(tmp_path, real_row(t, "pass", "cluster_clear", {**complete, "top_funder_of_attempted": 0.0, "clustered_of_attempted": 0.0}))
    c = facts(tmp_path, t)["cluster"]
    assert c["check"] == "pass" and c["top_funder_pct_of_attempted"] == 0.0 and c["clustered_pct_of_attempted"] == 0.0     # a completed 0 is a known 0


def test_unknown_decision_metrics_are_placeholders_never_a_zero_percent(tmp_path):
    t = ticket({})
    placeholder = {**REAL_METRICS, "coverage": 0.0, "resolved": 0, "clustered_of_attempted": 0.0, "top_funder_of_attempted": 0.0}
    write(tmp_path, real_row(t, "unknown", "cluster_unknown_budget", placeholder))
    c = facts(tmp_path, t)["cluster"]
    assert c["state"] == "unknown" and c["top_funder_pct_of_attempted"] is None and c["clustered_pct_of_attempted"] is None


def test_bad_metrics_are_absent_not_clamped(tmp_path):
    t = ticket({})
    for bad in ({}, {"top_funder_of_attempted": 1.5, "clustered_of_attempted": -0.1}, {"top_funder_of_attempted": float("nan")},
                {"top_funder_of_attempted": "0.5"}, None):
        write(tmp_path, real_row(t, "kill", "cluster_share_high", bad))
        c = facts(tmp_path, t)["cluster"]
        assert c["check"] == "flagged" and c["top_funder_pct_of_attempted"] is None and c["clustered_pct_of_attempted"] is None


def test_partial_real_shaped_metrics_do_not_claim_exact_percentages(tmp_path):
    t=ticket({})
    write(tmp_path,real_row(t,'kill','cluster_top_funder_high',REAL_METRICS))
    c=facts(tmp_path,t)['cluster']
    assert c['check']=='flagged' and c['top_funder_high'] is True
    assert c['top_funder_pct_of_attempted'] is None and c['clustered_pct_of_attempted'] is None
