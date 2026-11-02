"""Tests for the POST60 negative diagnostic.

Every case here is a defect an audit found in a previous revision, or an invariant that decides
whether a wallet is rejected. They run against an in-memory tape, never the live database.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pump20_post60_screen.py"
_spec = importlib.util.spec_from_file_location("pump20_post60_screen", SCRIPT)
p20 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p20)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE memecoin_signal_outcomes_v2(
            outcome_id TEXT, upstream_event_id TEXT, source_user_id TEXT, handle TEXT,
            network_id TEXT, mint TEXT, signal_observed_at TEXT);
        CREATE TABLE memecoin_events(
            fomo_event_id TEXT, their_market_cap REAL, is_first_buy INTEGER);
        CREATE TABLE memecoin_horizon_marks_v2(
            outcome_id TEXT, target_s INTEGER, price REAL, status TEXT, quality TEXT,
            observed_at TEXT, actual_age_s REAL, lateness_s REAL);
        """
    )
    return c


def add_signal(c, oid, uid, handle, mint, ts, *, net="solana", mcap=1_000_000.0, first_buy=1):
    c.execute("INSERT INTO memecoin_signal_outcomes_v2 VALUES (?,?,?,?,?,?,?)",
              (oid, f"ev-{oid}", uid, handle, net, mint, ts))
    c.execute("INSERT INTO memecoin_events VALUES (?,?,?)", (f"ev-{oid}", mcap, first_buy))


def add_marks(c, oid, p60, p900, *, observed_at="2026-08-01T00:00:00+00:00",
              lateness=0.0, status="observed", quality="ok", age_ok=True):
    for target, price in ((60, p60), (900, p900)):
        if price is None:
            continue
        c.execute("INSERT INTO memecoin_horizon_marks_v2 VALUES (?,?,?,?,?,?,?,?)",
                  (oid, target, price, status, quality, observed_at,
                   target if age_ok else target - 1, lateness))


def test_wallet_identity_is_the_id_not_the_handle(conn):
    """412 wallet ids carry more than one handle; grouping by handle splits one wallet in two."""
    add_signal(conn, "a", "uid-1", "alias_one", "mintA", "2026-07-20T00:00:00+00:00")
    add_signal(conn, "b", "uid-1", "alias_two", "mintB", "2026-07-21T00:00:00+00:00")
    add_marks(conn, "a", 1.0, 1.1)
    add_marks(conn, "b", 1.0, 1.2)
    eps, n_sel = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert {e["uid"] for e in eps} == {"uid-1"}
    assert n_sel[("uid-1", "solana")] == 2, "both coins belong to one wallet"


def test_one_handle_shared_by_two_wallet_ids_stays_split(conn):
    """14 handles map to more than one wallet id; they must not merge into one score."""
    add_signal(conn, "a", "uid-1", "shared", "mintA", "2026-07-20T00:00:00+00:00")
    add_signal(conn, "b", "uid-2", "shared", "mintB", "2026-07-21T00:00:00+00:00")
    add_marks(conn, "a", 1.0, 1.1)
    add_marks(conn, "b", 1.0, 1.2)
    eps, n_sel = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert {e["uid"] for e in eps} == {"uid-1", "uid-2"}
    assert n_sel[("uid-1", "solana")] == 1 and n_sel[("uid-2", "solana")] == 1


def test_first_signal_without_marks_is_dropped_not_substituted(conn):
    """The bug that mattered: a later signal must never stand in for an unmarked first signal.

    Marks are lost precisely when a coin dies, so substituting the survivor selects on the outcome.
    """
    add_signal(conn, "first", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00")
    add_signal(conn, "later", "uid-1", "w", "mintA", "2026-07-20T06:00:00+00:00")
    add_marks(conn, "later", 1.0, 5.0)          # only the LATER signal has marks
    eps, n_sel = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert eps == [], "the episode is the first signal; without its marks it is dropped"
    assert n_sel[("uid-1", "solana")] == 1, "still counted as selected, for completeness"


def test_earliest_signal_defines_the_episode_when_it_has_marks(conn):
    add_signal(conn, "first", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00")
    add_signal(conn, "later", "uid-1", "w", "mintA", "2026-07-20T06:00:00+00:00")
    add_marks(conn, "first", 1.0, 1.1)
    add_marks(conn, "later", 1.0, 9.9)
    eps, _ = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert len(eps) == 1
    assert eps[0]["ret"] == pytest.approx(10.0), "used the first signal, not the flattering one"


def test_mark_observed_after_as_of_is_excluded(conn):
    """A historical rerun must not consume marks from its own future."""
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00")
    add_marks(conn, "a", 1.0, 1.5, observed_at="2026-08-05T00:00:00+00:00")
    assert p20.load_episodes(conn, "2026-07-11", "2026-08-01")[0] == []
    assert len(p20.load_episodes(conn, "2026-07-11", "2026-09-01")[0]) == 1


def test_null_lateness_is_rejected_not_read_as_zero(conn):
    """Unknown freshness is not perfect freshness."""
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00")
    add_marks(conn, "a", 1.0, 1.5, lateness=None)
    assert p20.load_episodes(conn, "2026-07-11", "2026-09-01")[0] == []


def test_stale_mark_beyond_the_freshness_cap_is_dropped(conn):
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00")
    add_marks(conn, "a", 1.0, 1.5, lateness=p20.LATE_TARGET_S + 1)
    assert p20.load_episodes(conn, "2026-07-11", "2026-09-01")[0] == []


def test_chain_identity_folds_4663_and_robin_once(conn):
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00", net="4663")
    add_signal(conn, "b", "uid-1", "w", "mintB", "2026-07-21T00:00:00+00:00", net="robin")
    add_marks(conn, "a", 1.0, 1.1)
    add_marks(conn, "b", 1.0, 1.2)
    eps, n_sel = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert {e["net"] for e in eps} == {"robin"}
    assert n_sel[("uid-1", "robin")] == 2


def _result(**kw):
    """A wallet that IS rejectable: complete, Holm-significant, and clearly negative. Each test
    below breaks exactly one of the three conditions."""
    base = {"completeness": 1.0, "holm_sig": True, "p_source": "bootstrap",
            "abs20": -9.0, "abs20_ci90": [-16.0, -2.0], "pump20": None}
    base.update(kw)
    return base


def test_low_completeness_withholds_rejection(conn):
    """Marks vanish when a coin dies, so the survivors skew healthy. Judging a wallet on a
    minority of its coins is biased toward NOT rejecting, so we refuse to judge at all."""
    assert p20.is_rejected(_result()) is True
    assert p20.is_rejected(_result(completeness=0.5)) is False


def test_all_three_conditions_are_required(conn):
    """completeness AND Holm significance AND a negative effect. These are ANDed, not ORed —
    an earlier revision let a bare CI bound reject on its own, with no multiplicity control."""
    assert p20.is_rejected(_result()) is True
    assert p20.is_rejected(_result(completeness=0.5)) is False          # incomplete
    assert p20.is_rejected(_result(holm_sig=False)) is False            # uncorrected
    assert p20.is_rejected(_result(abs20=1.0, abs20_ci90=[-2.0, 6.0])) is False   # not negative
    # failing the conditions is never evidence of quality — the caller keeps it on watch
    assert p20.is_rejected(_result(abs20=0.5, abs20_ci90=[-1.0, 6.0])) is False


def test_holm_stops_at_the_first_failure():
    """Step-down: the cascade halts at the first p-value that misses its own threshold, and
    everything after it fails too — even a p-value that would clear its threshold in isolation.

    Family of 3, alpha 0.05 -> thresholds 0.0167, 0.025, 0.05 in sorted order.
    """
    res = [{"p_perm": 0.04}, {"p_perm": 0.02}, {"p_perm": 0.03}]   # deliberately unsorted input
    p20.holm(res, alpha=0.05)
    by_p = {r["p_perm"]: r["holm_sig"] for r in res}
    assert by_p[0.02] is False    # smallest, but 0.02 > 0.05/3 -> cascade stops immediately
    assert by_p[0.03] is False
    assert by_p[0.04] is False    # 0.04 <= 0.05/1 would pass alone; the halt blocks it
    assert all(r["holm_family_size"] == 3 for r in res)


def test_holm_rejects_down_the_sorted_cascade():
    """Sorting happens inside holm(); input order must not change the outcome."""
    res = [{"p_perm": 0.40}, {"p_perm": 0.0166}, {"p_perm": 0.001}]
    p20.holm(res, alpha=0.05)
    by_p = {r["p_perm"]: r["holm_sig"] for r in res}
    assert by_p[0.001] is True     # 0.001  <= 0.05/3
    assert by_p[0.0166] is True    # 0.0166 <= 0.05/2
    assert by_p[0.40] is False     # 0.40   >  0.05/1


def test_holm_family_counts_every_tested_wallet():
    res = [{"p_perm": 0.001}, {"p_perm": 0.002}, {"p_perm": None}]
    p20.holm(res, alpha=0.05)
    assert [r["holm_family_size"] for r in res] == [2, 2, 2], "untested wallets are not a family"


def test_controls_exclude_the_same_wallet_and_the_same_coin():
    eps = [{"uid": "uid-1", "net": "solana", "mint": "target", "mcap": 1e6,
            "ts": "2026-07-20T00:00:00+00:00", "ret": 5.0}]
    # same wallet on other coins, plus the same coin under another wallet: none may be a control
    eps += [{"uid": "uid-1", "net": "solana", "mint": f"self{i}", "mcap": 1e6,
             "ts": "2026-07-20T00:05:00+00:00", "ret": 1.0} for i in range(12)]
    eps += [{"uid": "uid-2", "net": "solana", "mint": "target", "mcap": 1e6,
             "ts": "2026-07-20T00:05:00+00:00", "ret": 1.0}]
    p20.build_controls(eps)
    assert eps[0]["control"] is None, "no eligible control coin exists"


def test_controls_require_the_minimum_count_and_respect_the_mcap_band():
    def coin(i, mcap):
        return {"uid": f"other-{i}", "net": "solana", "mint": f"m{i}", "mcap": mcap,
                "ts": "2026-07-20T00:05:00+00:00", "ret": 2.0}
    target = {"uid": "uid-1", "net": "solana", "mint": "target", "mcap": 1e6,
              "ts": "2026-07-20T00:00:00+00:00", "ret": 5.0}
    # 9 in-band controls is one short of MIN_CONTROLS
    p20.build_controls([target] + [coin(i, 1e6) for i in range(p20.MIN_CONTROLS - 1)])
    assert target["control"] is None
    # 10 in-band controls qualify
    p20.build_controls([target] + [coin(i, 1e6) for i in range(p20.MIN_CONTROLS)])
    assert target["control"] is not None and len(target["control"]) == p20.MIN_CONTROLS
    # 10 controls far outside the market-cap band do not
    p20.build_controls([target] + [coin(i, 1e9) for i in range(p20.MIN_CONTROLS)])
    assert target["control"] is None


def test_screen_keys_wallets_by_id_and_reports_completeness():
    rng = np.random.default_rng(0)
    eps = [{"uid": "uid-1", "handle": "w", "net": "solana", "mint": f"m{i}",
            "ts": f"2026-07-2{i % 10}T00:00:00+00:00", "ret": -20.0, "control": None}
           for i in range(8)]
    res = p20.screen(eps, "solana", {("uid-1", "solana"): 16}, 8, 200, 200, rng)
    assert len(res) == 1
    assert res[0]["uid"] == "uid-1"
    assert res[0]["completeness"] == pytest.approx(0.5)
    assert p20.is_rejected({**res[0], "holm_sig": False}) is False, "50% completeness withholds"


def test_add_on_buys_are_excluded_by_default(conn):
    """An add-on is conditioned on the first buy already working, so it is not a fresh
    observation of the wallet's judgement. 25,837 of 52,789 outcomes are not first buys."""
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00", first_buy=1)
    add_signal(conn, "b", "uid-1", "w", "mintB", "2026-07-21T00:00:00+00:00", first_buy=0)
    add_marks(conn, "a", 1.0, 1.1)
    add_marks(conn, "b", 1.0, 9.9)
    eps, n_sel = p20.load_episodes(conn, "2026-07-11", "2026-09-01")
    assert [e["mint"] for e in eps] == ["mintA"]
    assert n_sel[("uid-1", "solana")] == 1, "the add-on is not even counted as selected"
    # the opt-out exists but produces a DIFFERENT statistic
    eps_all, _ = p20.load_episodes(conn, "2026-07-11", "2026-09-01", first_buy_only=False)
    assert {e["mint"] for e in eps_all} == {"mintA", "mintB"}


def test_null_is_first_buy_is_treated_as_not_first(conn):
    add_signal(conn, "a", "uid-1", "w", "mintA", "2026-07-20T00:00:00+00:00", first_buy=None)
    add_marks(conn, "a", 1.0, 1.1)
    assert p20.load_episodes(conn, "2026-07-11", "2026-09-01")[0] == []


def test_holm_covers_bootstrap_and_permutation_in_one_family():
    """One test per wallet, one family. A matched wallet uses the permutation p; an unmatched one
    falls back to the bootstrap p. The choice is made by data availability, never by which p is
    smaller — picking the better of the two would be max-of-k dressed as a correction."""
    res = [{"p_perm": 0.30, "p_boot": 0.0001},      # matched -> MUST use the permutation p
           {"p_perm": None, "p_boot": 0.001},       # unmatched -> bootstrap
           {"p_perm": None, "p_boot": 0.90}]
    p20.holm(res, alpha=0.05)
    assert res[0]["p_source"] == "permutation" and res[0]["p_value"] == 0.30
    assert res[1]["p_source"] == "bootstrap" and res[1]["p_value"] == 0.001
    assert all(r["holm_family_size"] == 3 for r in res), "unmatched wallets are in the family too"
    assert res[1]["holm_sig"] is True      # 0.001 <= 0.05/3
    assert res[0]["holm_sig"] is False     # its own p is 0.30, not the flattering 0.0001


def test_rejection_requires_holm_not_a_bare_ci_bound():
    """The defect that mattered most: a 90% CI upper bound below zero is an uncorrected 5%
    one-sided test per wallet. Across ~142 wallet-chains that is ~7 expected false rejections,
    and it produced 16 of 17 published rejections."""
    negative_ci = {"completeness": 1.0, "abs20": -14.9, "abs20_ci90": [-27.5, -4.2],
                   "pump20": None, "p_source": "bootstrap"}
    assert p20.is_rejected({**negative_ci, "holm_sig": False}) is False
    assert p20.is_rejected({**negative_ci, "holm_sig": True}) is True


def test_permutation_backed_rejection_uses_the_pump20_effect_floor():
    r = {"completeness": 1.0, "holm_sig": True, "p_source": "permutation",
         "abs20": -20.0, "abs20_ci90": [-30.0, -10.0]}
    assert p20.is_rejected({**r, "pump20": -5.0}) is True
    assert p20.is_rejected({**r, "pump20": -1.0}) is False, "significant but too small to matter"
