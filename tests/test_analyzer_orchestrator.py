"""End-to-end: thesis + fresh + archetype -> synthesize -> immutable read. Plus the worker's
fail-loud contract — a geo-block surfaces and leaves NO read (never a false insufficient_evidence)."""
from __future__ import annotations

import os
import sqlite3
import sys

from sniper.analysis.winning_wallet_thesis import orchestrator as ORCH
from sniper.persistence import db_wallet_reads as RD
from sniper.persistence import db_wallet_analysis_queue as Q
from helpers_thesis import make_book, make_profile

BIG = 2_000_000_000  # fixed "now" well past any episode ts


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    RD.ensure_wallet_reads_tables(c)
    Q.ensure_queue_tables(c)
    return c


def _deep_book():
    rows = [dict(area="pol", entry=0.5, cost=1000, won=(1 if i < 40 else 0), slug=f"e{i}")
            for i in range(60)]
    return make_book(rows)


def _arch(structural=False):
    return {"archetype": ("paired_outcome_arb" if structural else "directional_discretionary"),
            "intent": "thesis_specialist", "structural_share_high": structural,
            "structural_share": (0.95 if structural else 0.1)}


def test_established_edge_end_to_end_appends_read():
    c = _conn()
    read = ORCH.analyze_one(
        c, "0xDEEP", thesis_profile=make_profile(p_value=0.001, obs_over_expected=1.4,
                                                 edge_labels=("politics",)),
        book=_deep_book(), archetype=_arch(), now_ts=BIG, test_index=1)
    assert read.primary_verdict == "established_directional_edge"
    assert RD.latest_read(c, "0xDEEP")["primary_verdict"] == "established_directional_edge"


def test_thin_fresh_high_conviction_routes_strategy_not_established():   # case 4
    # A thin, LARGE-conviction ($9k bets), winning wallet is NOT established (no shuffle proof). It now
    # surfaces as fresh_high_conviction and routes to strategy_candidate — the right home for the
    # "came from nowhere, bet huge" archetype (gardenshed/GRIMDRIP), instead of being buried as no_edge.
    c = _conn()
    book = make_book([dict(area="pol", entry=0.5, cost=9000, won=1, slug=f"f{i}") for i in range(2)])
    read = ORCH.analyze_one(
        c, "0xFRESH", thesis_profile=make_profile(), book=book, archetype=_arch(),
        clv_fn=lambda ep: 0.5, now_ts=BIG, test_index=1)
    assert read.primary_verdict == "fresh_high_conviction"
    assert read.operational_route == "strategy_candidate"


def test_idempotent_same_snapshot_one_read():
    c = _conn()
    kw = dict(thesis_profile=make_profile(p_value=0.001, edge_labels=("politics",)),
              book=_deep_book(), archetype=_arch(), now_ts=BIG, test_index=1)
    ORCH.analyze_one(c, "0xW", **kw)
    ORCH.analyze_one(c, "0xW", **kw)  # same data_as_of (now_ts) -> idempotent
    n = c.execute("SELECT COUNT(*) FROM wallet_analysis_reads WHERE wallet='0xw'").fetchone()[0]
    assert n == 1


def test_worker_fail_loud_on_geoblock_writes_no_read():
    c = _conn()
    Q.enqueue(c, "0xG", "manual", now=1)
    row = Q.lease(c, "w1", now=10)

    def analyze(_w):
        raise ORCH.GeoBlockedError("region blocked")

    ORCH.process_queue_row(c, row, analyze, now=10)
    qr = c.execute("SELECT last_error FROM analyzer_read_queue WHERE id=?", (row["id"],)).fetchone()
    assert "geo" in qr["last_error"].lower()
    assert RD.latest_read(c, "0xG") is None  # NOT a false insufficient_evidence read


def test_worker_completes_on_success():
    c = _conn()
    Q.enqueue(c, "0xS", "manual", now=1)
    row = Q.lease(c, "w1", now=10)

    def analyze(w):
        return ORCH.analyze_one(c, w, thesis_profile=make_profile(p_value=0.001, edge_labels=("politics",)),
                                book=_deep_book(), archetype=_arch(), now_ts=BIG, test_index=1)

    ORCH.process_queue_row(c, row, analyze, now=10)
    assert c.execute("SELECT status FROM analyzer_read_queue WHERE id=?", (row["id"],)).fetchone()["status"] == "done"
    assert RD.latest_read(c, "0xS")["primary_verdict"] == "established_directional_edge"


def test_rate_limiter_min_interval():
    rl = ORCH.RateLimiter(rps=8.0)
    assert abs(rl.min_interval - 0.125) < 1e-9


def test_ledger_raises_on_abnormal_fetch_termination(monkeypatch):
    # P1: a geo/auth refusal mid-walk must RAISE, not return truncated rows (which would be cached
    # as a full history and produce a false insufficient_evidence read).
    import pytest
    from sniper.analysis.winning_wallet_thesis import ledger as L

    def fake_fetch(*_a, **kw):
        emit = kw.get("progress_emitter")
        if emit:
            emit("fetch_terminated", {"reason": "api_refused", "status": 403, "n_rows": 0})
        return []

    monkeypatch.setattr(L.P, "build_session", lambda: None)
    monkeypatch.setattr(L.P, "fetch_user_activity", fake_fetch)
    with pytest.raises(L.LedgerFetchError) as ei:
        L._fetch_activity("0xW", max_pages=1)
    assert ei.value.status == 403 and ei.value.reason == "api_refused"


def test_worker_geo_block_real_path_writes_no_read(monkeypatch):
    # P2: exercise analyze_wallet_live's real classification (not a pre-raised error). A
    # LedgerFetchError(403) from the fetch must become GeoBlockedError -> queue failed, NO read.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import analyze_wallets_worker as W
    from sniper.analysis.winning_wallet_thesis import cli as CLI
    from sniper.analysis.winning_wallet_thesis.ledger import LedgerFetchError

    def boom(*_a, **_k):
        raise LedgerFetchError("api_refused", 403)

    monkeypatch.setattr(CLI, "extract_profile_and_book", boom)
    c = _conn()
    Q.enqueue(c, "0xGEO", "manual", now=1)
    row = Q.lease(c, "w1", now=10)
    W.ORCH.process_queue_row(
        c, row, lambda w: W.analyze_wallet_live(c, w, now_ts=BIG, test_index=1), now=10)
    qr = c.execute("SELECT last_error FROM analyzer_read_queue WHERE id=?", (row["id"],)).fetchone()
    assert "geo" in qr["last_error"].lower()
    assert RD.latest_read(c, "0xGEO") is None


def test_worker_skips_soft_deleted_wallet():
    # blacklist: a soft-deleted wallet drains from the queue without analysis and writes no read
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import analyze_wallets_worker as W
    from sniper.persistence import db_wallet_flags as F
    c = _conn()
    F.ensure_wallet_flags_tables(c)
    F.soft_delete(c, "0xDEAD", now=1)
    assert W.analyze_wallet_live(c, "0xDEAD", now_ts=BIG, test_index=1) is None
    assert RD.latest_read(c, "0xDEAD") is None


def test_established_requires_shuffle_significance_not_fresh_evalue():
    # REAL-DATA BUG (favorite-scraper 0x20ad75 + a wallet with oe=0.62 yet 'established'): over huge n
    # a marginal calibration excess compounds the fresh e-value astronomically. The fresh e-process is
    # for THIN-history surprise only — a DEEP wallet must be established by the SHUFFLE test, not a big
    # e-value. So a deep wallet with shuffle p=0.085 + e_value=9e45 is NOT established.
    from helpers_thesis import fresh_view
    book = make_book([dict(area="crypto", entry=0.5, cost=1000, won=1, slug=f"e{i}") for i in range(160)])
    huge_e = fresh_view(e_value=9e45, alt_explanation=None, n_independent_events=200, obs_over_expected=1.22)
    not_sig = ORCH.build_thesis_view(make_profile(p_value=0.085, obs_over_expected=1.22), book, huge_e)
    assert not_sig.established_edge_supported is False         # e-value alone must NOT establish
    # the SHUFFLE test is what establishes a deep wallet
    sig = ORCH.build_thesis_view(make_profile(p_value=0.004, obs_over_expected=1.5), book, huge_e)
    assert sig.established_edge_supported is True
    # public_truth_stale still disqualifies even a shuffle-significant wallet
    stale = fresh_view(e_value=9e45, alt_explanation="public_truth_stale", n_independent_events=200)
    assert ORCH.build_thesis_view(make_profile(p_value=0.004), book, stale).established_edge_supported is False


def test_clean_labels_dedupes_and_drops_noise():
    # readability: cluster labels carry case-dupes + non-domain noise (hour buckets, 'held','Games')
    out = ORCH._clean_labels(["sports", "Sports", "Games", "held", "underdog", "21h", "MLB"])
    assert out == ["sports", "underdog", "MLB"]
    assert ORCH._clean_labels(["held", "crypto", "Crypto"]) == ["crypto"]


def test_incidental_stale_bet_does_not_nuke_clean_established_wallet():
    from sniper.analysis.winning_wallet_thesis import fresh_anomaly as FA
    rows = [dict(area="sports", entry=0.5, cost=1000, won=1, slug=f"clean{i}",
                 first_buy_ts=100, resolution_ts=1000)
            for i in range(79)]
    rows.append(dict(area="sports", entry=0.5, cost=5000, won=1, slug="stale-small-share",
                     first_buy_ts=2000, resolution_ts=1000))
    book = make_book(rows)
    fresh = FA.assess_fresh_anomaly(book)
    assert fresh.alt_explanation != "public_truth_stale"
    # A 79/79-win record is shuffle-overwhelming in reality (p~0); establish it via the SHUFFLE test
    # (not a big e-value). The point stands: an incidental small stale bet must not nuke it.
    tv = ORCH.build_thesis_view(make_profile(p_value=0.004, obs_over_expected=1.5), book, fresh)
    assert tv.established_edge_supported is True


def test_edge_domains_only_use_domain_axes():
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster
    prof = make_profile(p_value=0.001, obs_over_expected=1.4)
    prof.edge_clusters = [
        EdgeCluster("hour", "21h", {}, "hour", 20, 10, 10.0, 5.0),
        EdgeCluster("mech", "held", {}, "mechanism", 20, 10, 10.0, 5.0),
        EdgeCluster("tag", "NBA", {}, "tag", 20, 10, 10.0, 5.0),
    ]
    fresh = ORCH.assess_fresh_anomaly(_deep_book())
    tv = ORCH.build_thesis_view(prof, _deep_book(), fresh)
    assert tv.edge_domains == ["NBA"]
