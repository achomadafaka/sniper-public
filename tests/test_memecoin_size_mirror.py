"""Size-aware copy-sell (2026-09-15, operator: "sell size is important").

When the copied wallet sells while we hold, the share of its holding sold since our entry decides:
at or above MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION we sell the whole position, below it the sell is
a profit-taking trim and we hold on rails. fart (0xdedrater, 2026-09-15): bought $981, sold $595 25 s
later (about 40% of the $1,480 bought); the rails held it to a -43% stop (-$75.62).
"""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

OPEN = "2026-09-15T09:43:25+00:00"


def _t(side, usd, px, at, eid=None):
    return {"side": side, "their_size_usd": usd, "their_price": px, "created_at": at,
            "fomo_event_id": eid or f"{side}-{at}"}


def share(trades, lookback_s=86400):
    return R.source_sell_share(trades, opened_at=OPEN, lookback_s=lookback_s)


def test_one_sell_of_40pct_reads_40pct():
    f, d = share([_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                  _t("sell", 44, 0.11, "2026-09-15T09:43:49Z")])
    assert f == pytest.approx(0.4)
    assert d["since_their_buy_s"] == 25.0 and d["their_pnl_pct"] == pytest.approx(10.0)


def test_sells_accumulate_since_entry():
    trades = [_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
              _t("sell", 20, 0.1, "2026-09-15T09:44:00Z"),
              _t("sell", 20, 0.1, "2026-09-15T09:45:00Z")]
    assert share(trades[:2])[0] == pytest.approx(0.2)
    assert share(trades)[0] == pytest.approx(0.4)


def test_a_rebuy_after_entry_grows_the_base():
    f, _ = share([_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                  _t("buy", 100, 0.1, "2026-09-15T09:44:00Z"),
                  _t("sell", 60, 0.1, "2026-09-15T09:45:00Z")])
    assert f == pytest.approx(0.3)


def test_a_sell_before_entry_shrinks_the_holding():
    f, _ = share([_t("buy", 100, 0.1, "2026-09-15T09:40:00Z"),
                  _t("sell", 50, 0.1, "2026-09-15T09:41:00Z"),
                  _t("sell", 25, 0.1, "2026-09-15T09:45:00Z")])
    assert f == pytest.approx(0.5)


def test_unknown_holding_is_none_not_zero():
    # a buy older than the lookback does not count, so nothing is known to be held
    f, _ = share([_t("buy", 100, 0.1, "2026-09-13T09:00:00Z"),
                  _t("sell", 50, 0.1, "2026-09-15T09:45:00Z")], lookback_s=86400)
    assert f is None
    # an unpriced trade after entry makes the share unknowable
    f, _ = share([_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                  _t("sell", 50, None, "2026-09-15T09:45:00Z")])
    assert f is None


def test_the_share_is_capped_at_one():
    f, _ = share([_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                  _t("sell", 120, 0.1, "2026-09-15T09:45:00Z")])       # 1.2x: rounding, a full exit
    assert f == 1.0


def test_an_impossible_share_is_a_unit_mismatch_not_a_size():
    """Beta 2026-09-15: 10 of 40 triggers read above 1.5x (one 899x) from inconsistent prices."""
    f, d = share([_t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                  _t("sell", 50, 0.001, "2026-09-15T09:45:00Z")])     # 50x the holding
    assert f is None and d["inconsistent"] is True and d["raw_fraction"] == pytest.approx(50.0)


# ---- runner decision -------------------------------------------------------------------------

def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, mint, network_id="solana"):
    # opened_recorded_at pinned to OPEN too: open_position() defaults it to the real wall clock,
    # which -- since the fix preferring the confirmed fill over decision start -- would read every
    # fixture event (all stamped near OPEN) as "before" a same-instant fill, never "after".
    return m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                           entry_price=0.1, entry_notional=175.0, entry_fill_source="chain",
                           entry_lag_s=1.0, their_size_usd=100.0, opened_at=OPEN,
                           opened_recorded_at=OPEN,
                           strategy="alpha_b", execution_mode="real", network_id=network_id)


def _event(c, mint, eid, side, usd, px, at, network_id="solana"):
    ev = {"source_user_id": "u1", "source_handle": "h", "mint": mint, "ticker": "T",
          "side": side, "their_price": px, "their_market_cap": 1e5, "their_size_usd": usd,
          "is_first_buy": False, "fomo_event_id": eid, "created_at": at, "received_at": at,
          "lag_s": 0.3, "signal_source": "follow_activity", "network_id": network_id}
    m.insert_event(c, ev)

    class _Ev:
        pass
    obj = _Ev()
    obj.__dict__.update(ev)
    return obj


def _run(c, ev, monkeypatch, *, chains=("solana",)):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset(chains))
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)
    monkeypatch.setattr(R, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "check_wallet_demotion", lambda *a, **k: None)
    calls = []
    rc = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["id"], reason)),
                                                     {"status": "closed"})[1]}
    res = R.real_mirror_on_sell(c, ev, {"solana": rc, "4663": rc},
                                now_fn=lambda: "2026-09-15T09:43:50+00:00")
    return res, calls


def _audit(c, pid):
    row = c.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                    "AND reason='mirror' ORDER BY id DESC", (pid,)).fetchone()
    return row["allowed"], json.loads(row["detail"])


def test_a_40pct_sell_closes_the_position(monkeypatch):
    c = _c(); pid = _open(c, "FART")
    _event(c, "FART", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    ev = _event(c, "FART", "s1", "sell", 44, 0.11, "2026-09-15T09:43:49Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [(pid, "mirror")] and res == {"status": "closed"}
    allowed, d = _audit(c, pid)
    assert allowed == 1 and d["code"] == "size_mirror" and d["sold_fraction"] == pytest.approx(0.4)


def test_a_20pct_trim_holds_on_rails(monkeypatch):
    c = _c(); pid = _open(c, "TRIM")
    _event(c, "TRIM", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    ev = _event(c, "TRIM", "s1", "sell", 20, 0.1, "2026-09-15T09:43:49Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [] and res["status"] == "mirror_held" and res["reason"] == "size_hold"
    allowed, d = _audit(c, pid)
    assert allowed == 0 and d["sold_fraction"] == pytest.approx(0.2)


def test_the_second_trim_that_crosses_30pct_closes(monkeypatch):
    c = _c(); pid = _open(c, "STEP")
    _event(c, "STEP", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    _event(c, "STEP", "s1", "sell", 20, 0.1, "2026-09-15T09:43:40Z")
    ev = _event(c, "STEP", "s2", "sell", 15, 0.1, "2026-09-15T09:43:49Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [(pid, "mirror")]


def test_a_sell_in_the_decision_to_fill_gap_does_not_count_as_after_our_entry(monkeypatch):
    """Beta review, 2026-09-15: opened_at is decision start, before quote/sign/send/confirm;
    opened_recorded_at is the confirmed fill (same class of bug as
    finding-opened-at-is-decision-start-not-the-fill-2026-09-03). A trader sell landing between
    the two has not sold anything WE held yet -- it must fold into the pre-entry holding, not
    trigger an exit for a position whose fill had not even landed."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="GAP", ticker="T", entry_price=0.1,
        entry_notional=175.0, entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=100.0,
        opened_at="2026-09-15T09:43:25+00:00", opened_recorded_at="2026-09-15T09:43:30+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana")
    _event(c, "GAP", "b1", "buy", 100, 0.1, "2026-09-15T09:43:20Z")   # before decision start
    # lands between decision start (09:43:25) and the confirmed fill (09:43:30)
    ev = _event(c, "GAP", "s1", "sell", 50, 0.1, "2026-09-15T09:43:27Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [], "a sell before our fill closed a position we had not confirmed yet"
    assert res["reason"] == "size_hold"
    allowed, d = _audit(c, pid)
    assert allowed == 0 and d["sold_fraction"] == pytest.approx(0.0)


def test_an_unknown_holding_holds(monkeypatch):
    c = _c(); pid = _open(c, "NOBUY")
    ev = _event(c, "NOBUY", "s1", "sell", 44, 0.11, "2026-09-15T09:43:49Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [] and res["reason"] == "size_unknown"


@pytest.mark.parametrize("side, usd, px", [("buy", 900, None), ("sell", 50, None),
                                         ("buy", None, 0.1), ("sell", None, 0.1)])
def test_incomplete_pre_entry_inventory_holds(monkeypatch, side, usd, px):
    c = _c(); pid = _open(c, "PARTIAL")
    _event(c, "PARTIAL", "missing", side, usd, px, "2026-09-15T09:43:23Z")
    _event(c, "PARTIAL", "buy", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    ev = _event(c, "PARTIAL", "sell", "sell", 40, 0.1, "2026-09-15T09:43:49Z")
    result, calls = _run(c, ev, monkeypatch)
    assert calls == []
    assert result["reason"] == "size_unknown"
    allowed, detail = _audit(c, pid)
    assert not allowed and detail["sold_fraction"] is None


def test_unpriced_trade_outside_inventory_window_does_not_poison_the_share():
    fraction, _ = share([_t("buy", 900, None, "2026-09-13T09:00:00Z"),
                         _t("buy", 100, 0.1, "2026-09-15T09:43:24Z"),
                         _t("sell", 40, 0.1, "2026-09-15T09:43:49Z")])
    assert fraction == pytest.approx(0.4)


def test_the_operator_hold_still_wins(monkeypatch):
    c = _c(); pid = _open(c, "HELD")
    m.set_position_mirror_disabled(c, pid, True)
    _event(c, "HELD", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    ev = _event(c, "HELD", "s1", "sell", 100, 0.1, "2026-09-15T09:43:49Z")
    res, calls = _run(c, ev, monkeypatch)
    assert calls == [] and res["status"] == "mirror_disabled"


def test_an_unlisted_chain_keeps_the_old_path(monkeypatch):
    c = _c(); pid = _open(c, "0xrobin", network_id="4663")
    _event(c, "0xrobin", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z", network_id="4663")
    ev = _event(c, "0xrobin", "s1", "sell", 10, 0.1, "2026-09-15T09:43:49Z", network_id="4663")
    _run(c, ev, monkeypatch, chains=("solana",))
    _allowed, d = _audit(c, pid)
    assert not d["code"].startswith("size_")


def test_off_by_default(monkeypatch):
    c = _c(); pid = _open(c, "OFF")
    _event(c, "OFF", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z")
    ev = _event(c, "OFF", "s1", "sell", 44, 0.11, "2026-09-15T09:43:49Z")
    _run(c, ev, monkeypatch, chains=())
    _allowed, d = _audit(c, pid)
    assert not d["code"].startswith("size_")


def test_an_ambiguous_coin_is_refused_before_the_size_path(monkeypatch):
    """Beta review item 6: one wallet holding the coin on two chains, sell without a chain tag."""
    c = _c()
    a = _open(c, "0xsame", network_id="4663")
    b = _open(c, "0xsame", network_id="8453")
    _event(c, "0xsame", "b1", "buy", 100, 0.1, "2026-09-15T09:43:24Z", network_id="")
    ev = _event(c, "0xsame", "s1", "sell", 100, 0.1, "2026-09-15T09:43:49Z", network_id="")
    res, calls = _run(c, ev, monkeypatch, chains=("4663", "8453"))
    assert res is None and calls == []
    assert c.execute("SELECT COUNT(*) FROM memecoin_exit_trigger_audit WHERE position_id IN (?,?)",
                     (a, b)).fetchone()[0] == 0
