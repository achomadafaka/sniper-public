"""Wallet Analyzer API: job concurrency/dedup semantics and read-endpoint delegation.

Threads are stubbed so jobs stay in their initial state — we drive _JOBS directly to assert the
queue/cap/staleness logic rather than actually running the live extractor."""
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from sniper.routes import api_wallet_analyzer as M

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40


@pytest.fixture
def client(monkeypatch):
    M._JOBS.clear()

    class _NoThread:                     # never actually run the extractor; job keeps its queued state
        def __init__(self, *a, **k): pass
        def start(self): pass

    monkeypatch.setattr(M.threading, "Thread", _NoThread)
    app = Flask(__name__)
    app.register_blueprint(M.create_api_wallet_analyzer_blueprint())
    c = app.test_client()
    yield c
    M._JOBS.clear()


def _analyze(client, wallet, refresh=False):
    return client.post("/api/wallet-analyzer/analyze", json={"wallet": wallet, "refresh": refresh})


def test_report_attaches_plain_narrative(client, monkeypatch):
    # /report attaches the plain-English narrative (verdict + top patterns); no removed follow link.
    w0 = "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee"
    monkeypatch.setattr(M, "_load_full_report", lambda w: {
        "wallet": w, "headline": "x",
        "capital_risk": {"roi_on_peak_deployed_pct": 9.0},
        "shuffle_baseline": {"obs_over_expected": 2.5},
        "edge_clusters": [{"label": "CBB", "pnl": 50000, "n_positions": 100, "edge_pp": 4, "is_loss_center": 0}],
        "loss_centers": [], "data_gaps": []})
    monkeypatch.setattr(M, "_wallet_profile", lambda w: {})
    n = client.get(f"/api/wallet-analyzer/report?wallet={w0}").get_json()["narrative"]
    assert n["state"] == "profitable_provable" and n["verdict_line"]
    assert n["top_patterns"][0]["label"] == "CBB"
    assert ("forward" + "_profiles") not in n


def test_live_positions_drops_resolved_redeemable(client, monkeypatch):
    # REAL-DATA (gardenshed France/Bosnia at $0): Polymarket /positions returns resolved-but-unredeemed
    # bets (redeemable=True, curPrice=0) mixed with live ones. The live tab must drop them (same as the
    # poller) so a wallet's board shows only genuinely-open copyable bets, not a wall of $0 resolved losses.
    W = "0x" + "a" * 40
    rows = [
        {"conditionId": "0xlive", "outcomeIndex": 0, "title": "US win", "curPrice": 0.4,
         "size": 100, "redeemable": False},
        {"conditionId": "0xdead", "outcomeIndex": 0, "title": "France win", "curPrice": 0.0,
         "size": 100, "redeemable": True},
    ]
    monkeypatch.setattr(M.P, "fetch_user_positions",
                        lambda *a, **k: (rows, SimpleNamespace(reason="partial_page")))
    monkeypatch.setattr(M, "_known_position_keys", lambda w: set())
    d = client.get(f"/api/wallet-analyzer/live-positions?wallet={W}").get_json()
    assert {p["condition_id"] for p in d["positions"]} == {"0xlive"}
    assert d["count"] == 1 and d["resolved_hidden"] == 1


def test_invalid_wallet_rejected(client):
    assert _analyze(client, "not-a-wallet").status_code == 400
    assert client.get("/api/wallet-analyzer/positions?wallet=nope").status_code == 400
    assert client.get("/api/wallet-analyzer/report?wallet=nope").status_code == 400


def test_active_same_wallet_is_deduped(client):
    j1 = _analyze(client, W1).get_json()
    j2 = _analyze(client, W1).get_json()
    assert j1["job_id"] == j2["job_id"]            # reused the in-flight job, did not double-run
    assert sum(1 for j in M._JOBS.values() if j["wallet"] == W1) == 1


def test_stale_same_wallet_starts_a_new_job(client):
    j1 = _analyze(client, W1).get_json()
    # age it past the stale window — a stuck job must NOT block re-analysis forever
    M._JOBS[j1["job_id"]]["started"] = time.time() - (M._JOB_STALE_S + 10)
    j2 = _analyze(client, W1).get_json()
    assert j2["job_id"] != j1["job_id"]


def test_global_inflight_cap_returns_429(client):
    started = []
    for i in range(M._MAX_INFLIGHT):
        w = "0x" + f"{i:040x}"
        assert _analyze(client, w).status_code == 200
        started.append(w)
    r = _analyze(client, W2)
    assert r.status_code == 429 and r.get_json()["inflight"] == M._MAX_INFLIGHT


def test_cap_ignores_stale_jobs(client):
    # fill the cap, then age all of them — a new wallet should be admitted again
    for i in range(M._MAX_INFLIGHT):
        _analyze(client, "0x" + f"{i:040x}")
    for j in M._JOBS.values():
        j["started"] = time.time() - (M._JOB_STALE_S + 10)
    assert _analyze(client, W2).status_code == 200


def test_read_endpoints_delegate_to_db_store(client, monkeypatch):
    from sniper.analysis.winning_wallet_thesis import db_store
    monkeypatch.setattr(db_store, "positions_for", lambda w: [{"sentinel": "pos", "w": w}])
    monkeypatch.setattr(db_store, "slices_for", lambda w, axis=None: [{"sentinel": "sli", "axis": axis}])
    monkeypatch.setattr(db_store, "activity_for_position", lambda w, c, o, e: [{"sentinel": "act", "c": c, "o": o, "e": e}])
    monkeypatch.setattr(db_store, "analysis_state", lambda w: {"wallet": w.lower(), "run_id": "r1", "max_activity_ts": 123})

    p = client.get(f"/api/wallet-analyzer/positions?wallet={W1}").get_json()
    assert p["positions"] == [{"sentinel": "pos", "w": W1}]

    s = client.get(f"/api/wallet-analyzer/slices?wallet={W1}&axis=tag").get_json()
    assert s["slices"] == [{"sentinel": "sli", "axis": "tag"}]

    a = client.get(f"/api/wallet-analyzer/position-activity?wallet={W1}&condition=condX&outcome=1&episode=2").get_json()
    assert a["activity"] == [{"sentinel": "act", "c": "condX", "o": 1, "e": 2}]

    st = client.get(f"/api/wallet-analyzer/analysis-state?wallet={W1}").get_json()
    assert st["state"] == {"wallet": W1.lower(), "run_id": "r1", "max_activity_ts": 123}


def test_position_activity_requires_integers(client):
    bad = client.get(f"/api/wallet-analyzer/position-activity?wallet={W1}&condition=c&outcome=x&episode=2")
    assert bad.status_code == 400


def test_run_analysis_uses_incremental_refresh_for_reanalysis(monkeypatch):
    calls = {}

    def fake_run_extractor(wallet, **kwargs):
        calls["wallet"] = wallet
        calls["kwargs"] = kwargs
        return SimpleNamespace(run_id="run1", headline="headline")

    from sniper.analysis.winning_wallet_thesis import cli, store
    monkeypatch.setattr(cli, "run_extractor", fake_run_extractor)
    monkeypatch.setattr(store, "persist_profile", lambda _profile: "profile.json")

    M._JOBS.clear()
    M._JOBS["jid"] = {"wallet": W1, "status": "queued", "started": time.time(),
                      "finished": None, "run_id": None, "headline": None, "error": None}
    M._run_analysis("jid", W1, refresh=True)

    assert calls["wallet"] == W1
    assert calls["kwargs"]["refresh"] is True
    assert calls["kwargs"]["incremental"] is True
    assert calls["kwargs"]["persist_store"] is True
    assert M._JOBS["jid"]["status"] == "done"


def test_live_positions_marks_new_since_analyzer(client, monkeypatch):
    from sniper.analysis.winning_wallet_thesis import db_store
    # known-cond: analyzer saw it OPEN (unresolved + still holding) -> a live holding is the SAME
    # position (known). reentry-cond: analyzer saw it RESOLVED -> a live holding is a RE-ENTRY (new).
    monkeypatch.setattr(db_store, "positions_for", lambda w: [
        {"condition_id": "known-cond", "outcome_index": 0, "resolved": 0,
         "buy_shares": 10.0, "sell_shares": 0.0, "split_shares": 0.0, "merge_shares": 0.0},
        {"condition_id": "reentry-cond", "outcome_index": 0, "resolved": 1,
         "buy_shares": 8.0, "sell_shares": 8.0, "split_shares": 0.0, "merge_shares": 0.0},
    ])

    def fake_fetch(_session, wallet, endpoint):
        assert wallet == W1
        assert endpoint == "positions"
        return [
            {
                "conditionId": "known-cond",
                "outcomeIndex": 0,
                "outcome": "YES",
                "title": "Known market",
                "asset": "tok-known",
                "size": "10",
                "avgPrice": "0.40",
                "curPrice": "0.60",
                "currentValue": "6",
                "cashPnl": "2",
            },
            {
                "conditionId": "new-cond",
                "outcomeIndex": 1,
                "outcome": "NO",
                "title": "Fresh market",
                "asset": "tok-new",
                "size": "5",
                "avgPrice": "0.20",
                "curPrice": "0.30",
                "currentValue": "1.5",
                "cashPnl": "0.5",
            },
            {
                "conditionId": "reentry-cond",
                "outcomeIndex": 0,
                "outcome": "YES",
                "title": "Re-bought after analysis",
                "asset": "tok-re",
                "size": "3",
                "avgPrice": "0.50",
                "curPrice": "0.55",
                "currentValue": "1.65",
                "cashPnl": "0.15",
            },
        ], SimpleNamespace(reason="partial_page")

    monkeypatch.setattr(M, "P", SimpleNamespace(fetch_user_positions=fake_fetch), raising=False)

    r = client.get(f"/api/wallet-analyzer/live-positions?wallet={W1}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["count"] == 3
    assert data["known_count"] == 1                  # only the still-open position is "known"
    assert data["new_count"] == 2                    # brand-new + the re-entry
    by_cond = {p["condition_id"]: p for p in data["positions"]}
    assert by_cond["known-cond"]["known_to_analyzer"] is True
    assert by_cond["known-cond"]["is_new_since_analysis"] is False
    assert by_cond["new-cond"]["known_to_analyzer"] is False
    # re-entry: analyzer recorded this market as resolved/closed, so a live holding reads as NEW
    assert by_cond["reentry-cond"]["known_to_analyzer"] is False
    assert by_cond["reentry-cond"]["is_new_since_analysis"] is True
    assert by_cond["new-cond"]["is_new_since_analysis"] is True
    assert by_cond["new-cond"]["current_value"] == 1.5
    assert by_cond["new-cond"]["cash_pnl"] == 0.5


def test_live_positions_fetch_error_is_502(client, monkeypatch):
    monkeypatch.setattr(M, "P", SimpleNamespace(
        fetch_user_positions=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("pm down"))
    ), raising=False)

    r = client.get(f"/api/wallet-analyzer/live-positions?wallet={W1}")
    assert r.status_code == 502
    assert "pm down" in r.get_json()["error"]


def test_live_positions_api_refusal_at_first_page_is_502(client, monkeypatch):
    monkeypatch.setattr(M, "P", SimpleNamespace(
        fetch_user_positions=lambda *_a, **_kw: ([], SimpleNamespace(reason="api_refused", status=429, offset=0))
    ), raising=False)

    r = client.get(f"/api/wallet-analyzer/live-positions?wallet={W1}")
    assert r.status_code == 502
    body = r.get_json()
    assert body["error"] == "live position fetch refused"
    assert body["status"] == 429


def test_enrich_flags_adds_read_fields_for_card():
    # The Analyzer card needs the read's plain-English headline + evidence (esp. the fresh-account
    # caveat) — _enrich_flags must surface them so the card can explain "what + how well".
    import sqlite3
    from sniper.persistence import db_wallet_reads as RD
    from sniper.analysis.winning_wallet_thesis.synthesis import WalletRead
    W = "0x" + "a" * 40
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    RD.ensure_wallet_reads_tables(c)
    RD.append_read(c, W, WalletRead(
        primary_verdict="fresh_high_surprise", operational_route="legacy_route_label",
        headline="Fresh account with unusually concentrated, high-surprise results — validation required.",
        reasons=[], warnings=["WATCH ONLY — high false-positive shape; validation required"],
        edge_location={"domains": ["crypto", "Crypto"], "loss_centers": [], "entry_shape": {}},
        evidence_profile={"history_depth": "thin", "n_independent_events": 4,
                          "fresh": {"ex_post_level": "strong"}, "recent_anomaly_level": "none"},
        provenance={"analyzer_version": 1, "rule_version": 1, "data_as_of": 1, "source_snapshot_hash": "h"}),
        now_ts=1)
    reports = [{"wallet": W}]
    M._enrich_flags(c, reports)
    r = reports[0]
    assert r["read_verdict"] == "fresh_high_surprise"
    assert "validation required" in (r["read_headline"] or "").lower()
    assert r["read_route"] == "strategy_candidate"
    assert r["read_history_depth"] == "thin" and r["read_n_events"] == 4
    assert r["read_domains"] == ["crypto", "Crypto"] and r["read_warnings"]
