"""CONFIG-GATED PAPER AS A TRIAL FEASIBILITY SCREEN (Alpha and Gamma, 2026-10-02).

funded_clone clones funded's EXIT rails, never its admission gates, so paper books signals funded
refuses (on 56, 229 of 299 closes were over the $250k ceiling). As a SCORE gated paper predicted funded
worse than ungated (Gamma, 27 wallets: Spearman +0.27/+0.31 against +0.44), so the score stays
ungated. Gated paper answers feasibility: would funded have entered enough of the wallet's signals for
a trial to reach 10 closes in 14 days? bluntz_capital had 0 gated closes and two trials that could not
fill. N (Gamma): 40 on solana, 12 on 4663, config-gated closes in the trailing 14 days, censored
included; no N on 56 or 8453.

Rule-5 map (2026-10-02, before the edit):
1. Money path: indirect. It decides whether the candidate list PRINTS a trial command; the operator
   runs it. No runtime code changes.
2. Speed: none on any fill path; a research script.
3. Concurrency: read-only ledger connection; settings are read, never written.
4. Absent/zero/unknown: config only for every wallet (funded's own decision exists only for armed
   wallets and would not transfer to candidates). Today's config is applied through the funded
   functions themselves (mcap_entry_decision, source_buy_family_reason, funded's prior-net read). An
   unresolvable mcap is refused, as MEMECOIN_SOLANA_MCAP_UNRESOLVED=refuse does. A chain without an N
   is never feasible.
5. Failures: an unreadable prior-net read judges the single buy, exactly as funded does.
6. Units: USD; chain ids canonical; the per-wallet keys are `chain:uid` lower-case; the 14-day window
   is on closed_at.
7. Sample: funded_clone closes since PAPER_SINCE; the volume and turnover floors are not applied (the
   paper row holds no volume reading) and today's config stands for every entry.
8. Rollback: revert the commit; the score never changed.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))

import wallet_follow_candidates as W  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402

SINCE = "2026-09-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def gates(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {"56:boss": 500_000.0})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_MCAP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.008)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 0.0)


def _ledger(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, source_user_id TEXT, network_id TEXT,
            mint TEXT, strategy TEXT, execution_mode TEXT, is_open INTEGER, opened_at TEXT, closed_at TEXT,
            realized_pnl REAL, entry_notional REAL, entry_mcap REAL, their_size_usd REAL,
            origin_event_id TEXT);
        CREATE TABLE memecoin_events (fomo_event_id TEXT UNIQUE, created_at TEXT, their_market_cap REAL,
            real_decision TEXT, real_decision_reason TEXT);
    """)
    for i, r in enumerate(rows):
        ev = "ev%d" % i
        c.execute("INSERT INTO memecoin_positions (source_user_id, network_id, mint, strategy, execution_mode,"
                  " is_open, opened_at, closed_at, realized_pnl, entry_notional, entry_mcap, their_size_usd,"
                  " origin_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (r.get("uid", "w"), r.get("chain", "56"), "m%d" % i, "funded_clone", "paper", 0,
                   "2026-09-20T00:00:00+00:00", "2026-09-25T00:00:00+00:00", r.get("pnl", 10.0), 100.0, r.get("mcap"),
                   r.get("their", 5_000.0), ev))
        if r.get("decision") is not None or r.get("event", True):
            c.execute("INSERT INTO memecoin_events VALUES (?,?,?,?,?)",
                      (ev, "2026-09-20T00:00:00Z", r.get("their_mcap", 0.0), r.get("decision"), r.get("reason")))
    return c


RECENT = "2026-09-18T00:00:00+00:00"


def _g(rows, **kw):
    return W.gated_paper(_ledger(rows), SINCE, RECENT, **kw)


def test_a_coin_over_the_ceiling_is_gated_out_and_an_in_band_one_counts():
    g = _g([{"mcap": 900_000.0, "pnl": -50.0}, {"mcap": 120_000.0, "pnl": 10.0}])[("w", "56")]
    v = W.gated_view([g])
    assert (v["gated_n"], v["gated_ret"], v["feasible_n"]) == (1, 0.10, 1)
    assert g["out"] == {"mcap:over_ceiling": 1}


def test_a_per_wallet_override_admits_its_own_wallet_only():
    g = _g([{"uid": "boss", "mcap": 400_000.0}, {"uid": "w", "mcap": 400_000.0}])
    assert W.gated_view([g[("boss", "56")]])["gated_n"] == 1 and W.gated_view([g[("w", "56")]])["gated_n"] == 0


def test_funded_s_own_decision_is_ignored_config_only_for_everyone():
    """A recorded fill over today's ceiling is gated out; a recorded volume refusal is not applied."""
    rows = [{"mcap": 900_000.0, "decision": "filled"},
            {"mcap": 100_000.0, "decision": "rejected", "reason": "h1 organic volume $2191 under floor $3000"}]
    g = _g(rows)[("w", "56")]
    assert W.gated_view([g])["gated_n"] == 1 and g["out"] == {"mcap:over_ceiling": 1}


def test_an_unresolvable_mcap_is_refused():
    assert _g([{"mcap": None, "their_mcap": 0.0}])[("w", "56")]["out"] == {"mcap:mcap_unresolved": 1}


def test_censored_closes_count_toward_feasibility_and_their_share_is_shown():
    rows = [{"mcap": 100_000.0, "pnl": None}, {"mcap": 100_000.0, "pnl": 20.0}, {"mcap": 900_000.0, "pnl": None}]
    v = W.gated_view([_g(rows)[("w", "56")]])
    assert (v["gated_n"], v["feasible_n"], v["gated_ret"]) == (2, 2, 0.20)
    assert v["gated_cens_share"] == 0.5 and abs(v["paper_cens_share"] - 2 / 3) < 1e-9


def test_only_closes_inside_the_14_days_count_toward_feasibility():
    c = _ledger([{"mcap": 100_000.0}, {"mcap": 100_000.0}])
    c.execute("UPDATE memecoin_positions SET closed_at = CASE id WHEN 1 THEN '2026-09-10T00:00:00+00:00' "
              "ELSE '2026-09-25T00:00:00+00:00' END")
    v = W.gated_view([W.gated_paper(c, SINCE, RECENT)[("w", "56")]])
    assert (v["gated_n"], v["feasible_n"]) == (2, 1)


def test_the_source_buy_ratio_floor_counts_the_earlier_net_like_funded():
    """$500 into a $100k coin is 0.5% < 0.8%: refused alone, admitted with $400 bought earlier."""
    row = {"mcap": 100_000.0, "their": 500.0}
    assert list(_g([row])[("w", "56")]["out"]) == ["source:source buy $500 (0.500%) under mcap floor 0.800% ($800)"]
    assert not _g([row], prior_net_fn=lambda r, window_s, judged: 400.0)[("w", "56")]["out"]


def test_the_absolute_source_buy_floor_applies():
    assert list(_g([{"mcap": 10_000.0, "their": 150.0}])[("w", "56")]["out"]) == [
        "source:source buy $150 under absolute floor $200"]


def test_feasibility_uses_gamma_s_n_per_chain_and_none_elsewhere():
    assert W.feasible({"chain": "solana", "feasible_n": 40}) and not W.feasible({"chain": "solana", "feasible_n": 39})
    assert W.feasible({"chain": "4663", "feasible_n": 12}) and not W.feasible({"chain": "4663", "feasible_n": 11})
    assert not W.feasible({"chain": "56", "feasible_n": 500}) and not W.feasible({"chain": "8453", "feasible_n": 500})
