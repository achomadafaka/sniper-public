"""The sell-share gate refuses funded entries, so every way it could be wrong is a money bug.

It reads COUNTS while the operator's threshold was about VOLUME. That substitution is deliberate
and sized (a count-80 cap catches ~30% of what a volume-80 cap would refuse), so the tests pin both
that it works and that it never claims to be measuring volume.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.services import memecoin_sell_share as ss

CAP = 80.0
FLOOR = 8


def _stats(buys, sells):
    return {"txns_m5_buys": buys, "txns_m5_sells": sells}


# ------------------------------------------------------------------ the reading

def test_the_share_is_sells_over_total():
    assert ss.count_sell_share(_stats(2, 8)) == (80.0, 10)
    assert ss.count_sell_share(_stats(8, 2)) == (20.0, 10)


def test_zero_transactions_is_unknown_not_zero_percent_sells():
    """A window with no trades says nothing about direction."""
    assert ss.count_sell_share(_stats(0, 0)) == (None, 0)


# ------------------------------------------------------------------ fail-open family

@pytest.mark.parametrize("stats", [
    None,
    {},
    {"txns_m5_buys": 5},                       # half the field
    {"txns_m5_sells": 5},
    {"txns_m5_buys": None, "txns_m5_sells": None},
    {"txns_m5_buys": "junk", "txns_m5_sells": 9},
    _stats(0, 0),
])
def test_every_unknown_admits(stats):
    """Chase-guard convention: an unknown tape is not a dumping tape, and refusing on missing data
    would refuse hardest exactly when the provider is down."""
    allow, _share, _total, _reason = ss.sell_share_decision(stats, cap_pct=CAP, min_txns=FLOOR)
    assert allow is True


def test_a_thin_window_is_recorded_but_never_refuses():
    """2 sells out of 2 is '100% sells' on nothing at all."""
    allow, share, total, reason = ss.sell_share_decision(_stats(0, 2), cap_pct=CAP, min_txns=FLOOR)
    assert allow is True
    assert share == 100.0 and total == 2
    assert "not evaluated" in reason and "floor 8" in reason


def test_a_zero_cap_disables_the_gate():
    allow, _s, _t, reason = ss.sell_share_decision(_stats(0, 99), cap_pct=0, min_txns=FLOOR)
    assert allow is True and "off" in reason


# ------------------------------------------------------------------ the refusal

def test_a_dumping_tape_is_refused_with_the_share_in_the_reason():
    allow, share, total, reason = ss.sell_share_decision(_stats(2, 14), cap_pct=CAP, min_txns=FLOOR)
    assert allow is False
    assert share == pytest.approx(87.5) and total == 16
    assert "88%" in reason and "cap 80%" in reason


def test_the_reason_names_COUNTS_so_nobody_reads_it_as_volume():
    """The operator's 80 was about volume. A refusal that hid the basis would let that number be
    misread as the one they chose."""
    _allow, _s, _t, reason = ss.sell_share_decision(_stats(2, 14), cap_pct=CAP, min_txns=FLOOR)
    assert "txns" in reason, "the basis must be visible in the refusal itself"
    assert "volume" not in reason.lower()


def test_a_buy_dominated_tape_passes():
    allow, share, _t, reason = ss.sell_share_decision(_stats(34, 8), cap_pct=CAP, min_txns=FLOOR)
    assert allow is True and share == pytest.approx(19.0, abs=0.1)
    assert "txns" in reason


def test_exactly_at_the_cap_passes():
    """Refuse ABOVE the cap, not at it — the operator named a ceiling, not an exclusive bound."""
    allow, share, _t, _r = ss.sell_share_decision(_stats(2, 8), cap_pct=CAP, min_txns=FLOOR)
    assert share == 80.0 and allow is True


# ------------------------------------------------------------------ volume, the evidence lane

def test_volume_share_derives_from_the_readers_existing_totals():
    """gross = P + N, net = P - N, so sells N = (gross - net)/2. No new column, no re-walk."""
    # 300 quote in (buys), 700 out (sells): gross 1000, net -400 -> 70% sells
    flow = {"gross_quote": 1000, "net_quote": -400}
    got = ss.volume_sell_share(None, pool="P", since_iso="x", flow_fn=lambda *a, **k: flow)
    assert got == pytest.approx(70.0)


def test_volume_share_matches_counts_when_every_trade_is_the_same_size():
    """The two measures only agree when size is uniform — which is why counts are a sized hole."""
    flow = {"gross_quote": 1000, "net_quote": 0}
    assert ss.volume_sell_share(None, pool="P", since_iso="x",
                                flow_fn=lambda *a, **k: flow) == pytest.approx(50.0)


def test_the_whale_sell_case_counts_and_volume_disagree_hard():
    """One 990-unit sell against ten 1-unit buys: 9% by count, 99% by volume. This is the
    pathology the cap exists for, and the count reading misses it."""
    by_count, _t = ss.count_sell_share(_stats(10, 1))
    flow = {"gross_quote": 1000, "net_quote": -980}
    by_volume = ss.volume_sell_share(None, pool="P", since_iso="x", flow_fn=lambda *a, **k: flow)
    assert by_count == pytest.approx(9.09, abs=0.1)
    assert by_volume == pytest.approx(99.0)
    assert ss.sell_share_decision(_stats(10, 1), cap_pct=CAP, min_txns=FLOOR)[0] is True, \
        "counts admit it — the documented, sized hole"


@pytest.mark.parametrize("flow", [None, {}, {"gross_quote": 0, "net_quote": 0}])
def test_volume_share_is_none_when_unreadable(flow):
    assert ss.volume_sell_share(None, pool="P", since_iso="x", flow_fn=lambda *a, **k: flow) is None


def test_volume_share_needs_a_pool_and_never_raises():
    assert ss.volume_sell_share(None, pool=None, since_iso="x") is None

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("tape gone")
    assert ss.volume_sell_share(None, pool="P", since_iso="x", flow_fn=boom) is None


def test_pool_lookup_is_none_when_absent_and_never_raises():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sol_pool_vaults (pool TEXT, mint TEXT)")
    c.execute("INSERT INTO sol_pool_vaults VALUES ('POOL1','MINT1')")
    assert ss.pool_for_mint(c, "MINT1") == "POOL1"
    assert ss.pool_for_mint(c, "GHOST") is None
    assert ss.pool_for_mint(sqlite3.connect(":memory:"), "MINT1") is None   # no table


# ------------------------------------------------------------------ isolation

def test_the_module_cannot_execute_anything():
    import pathlib
    src = pathlib.Path(ss.__file__).read_text()
    for forbidden in ("execute_fn", "exit_fn", "reserve_entry", "release_pending",
                      "wallet_exec_lock", "sendTransaction"):
        assert forbidden not in src, f"a read-only gate must not reference {forbidden}"
