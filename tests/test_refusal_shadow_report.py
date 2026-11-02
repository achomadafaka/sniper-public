"""The modelled side of the refusal shadow report counts coins, like the real side.

The real side deduplicates to distinct funded positions. The modelled side counted every
refusal row, so several refusals on one coin — which share one price path and resolve to one
later position — inflated `n`, `sum$` and the win rate against a real total that counted the
position once. The two columns then sat side by side in different units.

Offline: dicts in, captured stdout out. No database, no provider.
"""
from __future__ import annotations

import pytest

from scripts.research import refusal_shadow_report as report


def _refusal(second, *, mint="M", pnl=None, sampled=1, position=None, real_pnl=None,
             real_open=False):
    return {
        "family": "chase", "network_id": "4663", "mint": mint,
        "decision_started_at": f"2026-09-23T12:00:{second:02d}+00:00",
        "pnl_usd": pnl, "sampled": sampled,
        "real_position_id": position, "real_pnl": real_pnl, "real_open": real_open,
    }


def _row_of(capsys):
    out = capsys.readouterr().out
    return next(line for line in out.splitlines() if line.strip().startswith("chase")), out


def test_three_refusals_on_one_coin_are_one_campaign(capsys):
    """One coin, one price path, one later position: the modelled side must count it once."""
    rows = [_refusal(s, pnl=5.0, position=7, real_pnl=10.0) for s in (10, 20, 30)]
    report.table(rows, title="LATER HELD", with_real=True)
    row, out = _row_of(capsys)
    fields = row.split()

    assert "coins" in out and "refusals" in out, "both units must be visible"
    assert fields[2] == "1", "one coin"
    assert fields[3] == "3", "three refusals, reported as their own column"
    assert fields[-1] == "10.00", "the position's close counts once"
    assert fields[-2] == "1", "one distinct real position"


def test_the_modelled_sum_is_the_first_refusal_not_every_refusal(capsys):
    """The table's question is the FIRST refusal, so that is the row the campaign carries."""
    rows = [_refusal(10, pnl=5.0, position=7, real_pnl=10.0),
            _refusal(20, pnl=99.0, position=7, real_pnl=10.0)]
    report.table(rows, title="LATER HELD", with_real=True)
    row, _ = _row_of(capsys)

    assert "5.00" in row, "the earliest refusal's modelled PnL is the campaign's"
    assert "99.00" not in row, "a later refusal on the same coin is not a second observation"


def test_two_coins_stay_two_campaigns(capsys):
    rows = [_refusal(10, mint="A", pnl=5.0, position=7, real_pnl=10.0),
            _refusal(10, mint="B", pnl=5.0, position=8, real_pnl=-4.0)]
    report.table(rows, title="LATER HELD", with_real=True)
    row, _ = _row_of(capsys)
    fields = row.split()

    assert fields[2] == "2" and fields[3] == "2"
    assert fields[-1] == "6.00", "two distinct positions still sum"


def test_a_campaign_whose_first_refusal_was_never_sampled_is_not_priced(capsys):
    """Taking the earliest refusal can leave a campaign unpriced, and that is the honest answer."""
    rows = [_refusal(10, pnl=None, sampled=0), _refusal(20, pnl=5.0, sampled=1)]
    report.table(rows, title="NEVER HELD")
    row, _ = _row_of(capsys)
    fields = row.split()

    assert fields[2] == "1" and fields[3] == "2"
    assert fields[4] == "0", "the first refusal carried no price, so the campaign is unpriced"
    assert fields[6] == "1", "and it is counted as our own coverage gap, not as a flat outcome"
