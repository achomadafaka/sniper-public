"""Codex N14 + N13 (follow-up review 2026-10-07): a confirmed EVM sell whose receipt arrives
late is booked exactly once from THAT transaction's receipt, at the intent's frozen quote-asset
price, through the production recovery queues (never by calling a reconciler directly).

N14: a confirmed partial that recovery saw late used to clear its derisk_sig and book nothing
     ($10 entry, $6 partial, $7 final close read realized -$3 instead of +$3).
N13: unpriced_full_exits() was Solana-only, so an EVM chain_confirmed_unpriced close was never
     reconciled.
"""
import os
import sqlite3

import pytest

from sniper.execution.evm_exit_executor import execute_exit_evm, execute_partial_exit_evm
from sniper.execution.exec_lock import serialize_executor
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

FROZEN_ETH_USD = 2000.0     # the price the signed intent froze
LIVE_ETH_USD = 3000.0       # a later mark recovery must never use
NOW = "2026-07-09T01:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _pos(c, entry_notional=10.0, tokens=10.0):
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint="0xtok", ticker="",
                          entry_price=1.0, entry_notional=entry_notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="8453")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=tokens, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:00Z", tx_sig="E")
    return m.get_position(c, pid)


_QUOTE = {"sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9", "transaction": {"to": "0xr"}}


def _seams(*, tokens, tx_hash, partial=False):
    seams = dict(quote_fn=((lambda mint, toks, slip, fraction: _QUOTE) if partial
                           else (lambda mint, toks, slip: _QUOTE)),
                 approve_fn=lambda *a: True, sign_fn=lambda tx: ("0xraw" + tx_hash, tx_hash, 3),
                 send_fn=lambda raw: None, confirm_fn=lambda h: "confirmed",
                 usdc_fn=lambda: 50.0, token_balance_fn=lambda mint: tokens)
    return seams


class Receipts:
    """The EVM leg's receipt_proceeds_fn contract: quote-token atomic net x price, where the price
    is the caller's frozen quote_asset_usd or else a live mark. `weth` is per-tx quote received."""
    def __init__(self, weth):
        self.weth, self.available, self.calls = dict(weth), set(), []

    def __call__(self, tx_hash, *, quote_asset_usd=None):
        self.calls.append((tx_hash, quote_asset_usd))
        if tx_hash not in self.available:
            return None
        return self.weth[tx_hash] * (quote_asset_usd or LIVE_ETH_USD)

    def at_exit(self, tx_hash):
        """The executor's own immediate read (a separate seam in production), priced at exit time."""
        return self.weth[tx_hash] * FROZEN_ETH_USD if tx_hash in self.available else None


def _ctx(receipts, confirm="confirmed"):
    lock = serialize_executor(execute_partial_exit_evm, f"test-evm-recovery-{os.getpid()}")
    return {"8453": {"confirm_once_fn": lambda h: confirm if not callable(confirm) else confirm(h),
                     "derisk_fn": lock, "receipt_proceeds_fn": receipts}}


def _partial_unknown(c, pos, receipts):
    """The real executor: the half sale confirms but its proceeds are unprovable in budget."""
    res = execute_partial_exit_evm(c, pos, 0.5, "2026-07-09T00:05:00+00:00", slippage_ladder=[3],
                                   sleep_fn=lambda _s: None, receipt_proceeds_fn=receipts.at_exit,
                                   require_receipt_proceeds=True, quote_asset_usd=FROZEN_ETH_USD,
                                   **_seams(tokens=10.0, tx_hash="0xpart", partial=True))
    assert res["status"] == "unknown"
    assert m.get_position(c, pos["id"])["derisk_sig"] == "0xpart"


def _close(c, pid, receipts, tokens=5.0):
    return execute_exit_evm(c, m.get_position(c, pid), "mirror", "2026-07-09T00:30:00+00:00",
                            slippage_ladder=[3], sleep_fn=lambda _s: None,
                            receipt_proceeds_fn=receipts.at_exit, require_receipt_proceeds=True,
                            quote_asset_usd=FROZEN_ETH_USD,
                            **_seams(tokens=tokens, tx_hash="0xclose"))


def _derisk_fills(c, pid):
    return [tuple(r) for r in c.execute(
        "SELECT price, qty, notional, fill_source, tx_sig FROM memecoin_fills "
        "WHERE position_id=? AND kind='derisk'", (pid,))]


# ---- N14 ----

def test_codex_n14_late_partial_after_final_close_books_plus_three_exactly_once(monkeypatch):
    _alerts(monkeypatch)
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003, "0xclose": 0.0035})   # $6 and $7 at the frozen $2000
    _partial_unknown(c, pos, receipts)

    R.recover_unverified_derisks(c, _ctx(receipts), NOW)       # confirmed, receipt still missing
    row = m.get_position(c, pid)
    assert row["derisk_sig"] is None and row["derisked_at"] is not None
    assert _derisk_fills(c, pid) == [(None, 5.0, None, "chain_confirmed_unpriced", "0xpart")]
    assert m.derisk_proceeds(c, pid) is None                   # unknown, never $0

    receipts.available.add("0xclose")
    r = _close(c, pid, receipts)                               # the unpriced partial never blocks it
    assert r["status"] == "closed" and r["realized"] is None and r["received"] == pytest.approx(7.0)
    assert m.get_position(c, pid)["realized_pnl"] is None      # never the understated -$3

    receipts.available.add("0xpart")
    for _ in range(3):                                         # repeat passes book once
        R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    assert m.get_position(c, pid)["realized_pnl"] == pytest.approx(3.0)
    assert _derisk_fills(c, pid) == [(pytest.approx(1.2), 5.0, pytest.approx(6.0),
                                      "chain_confirmed_receipt_reconciled", "0xpart")]
    assert ("0xpart", FROZEN_ETH_USD) in receipts.calls
    assert all(q == FROZEN_ETH_USD for tx, q in receipts.calls if tx == "0xpart")


def test_a_late_partial_with_its_receipt_ready_books_priced_and_counts_at_close():
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003, "0xclose": 0.0035})
    _partial_unknown(c, pos, receipts)
    receipts.available.update({"0xpart", "0xclose"})
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    assert m.derisk_proceeds(c, pid) == pytest.approx(6.0)
    assert m.get_position(c, pid)["is_open"] == 1
    assert _close(c, pid, receipts)["status"] == "closed"
    assert m.get_position(c, pid)["realized_pnl"] == pytest.approx(3.0)


def test_a_confirmed_partial_never_rearms_on_a_later_not_found():
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003})
    _partial_unknown(c, pos, receipts)
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    stamp = m.get_position(c, pid)["derisked_at"]
    late = "2026-07-09T05:00:00+00:00"
    for _ in range(5):
        R.recover_unverified_derisks(c, _ctx(receipts, confirm="not_found"), late)
    assert m.get_position(c, pid)["derisked_at"] == stamp     # the rail never re-arms a sale that landed
    assert m.unverified_derisks(c) == []


def test_an_intent_without_its_frozen_quote_price_stays_unpriced_never_a_live_mark():
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003})
    res = execute_partial_exit_evm(c, pos, 0.5, "2026-07-09T00:05:00+00:00", slippage_ladder=[3],
                                   sleep_fn=lambda _s: None, receipt_proceeds_fn=receipts.at_exit,
                                   require_receipt_proceeds=True, quote_asset_usd=None,
                                   **_seams(tokens=10.0, tx_hash="0xpart", partial=True))
    assert res["status"] == "unknown"
    receipts.available.add("0xpart")
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    assert _derisk_fills(c, pid) == [(None, 5.0, None, "chain_confirmed_unpriced", "0xpart")]


def _alerts(monkeypatch):
    from sniper.services import memecoin_alerts
    sent = []
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: sent.append((kind, text)))
    return sent


@pytest.mark.parametrize("damage", ["missing", "other_tx", "bad_fraction", "no_balance"])
def test_a_missing_or_invalid_intent_books_a_quantity_unknown_debt_never_priced(damage, monkeypatch):
    import json
    sent = _alerts(monkeypatch)
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003})
    _partial_unknown(c, pos, receipts)
    claim = m.get_position(c, pid)["derisk_claim_id"]
    if damage == "missing":
        c.execute("DELETE FROM memecoin_exit_intents WHERE claim_id=?", (claim,))
    else:
        raw = json.loads(c.execute("SELECT intent_json FROM memecoin_exit_intents WHERE claim_id=?",
                                   (claim,)).fetchone()[0])
        raw.update({"other_tx": {"tx_identity": "0xelse"}, "bad_fraction": {"fraction": 1.5},
                    "no_balance": {"balance_snapshot": None}}[damage])
        c.execute("UPDATE memecoin_exit_intents SET intent_json=? WHERE claim_id=?",
                  (json.dumps(raw), claim))
    c.commit()
    receipts.available.add("0xpart")
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    row = m.get_position(c, pid)
    assert row["derisk_sig"] is None and row["derisked_at"] is not None
    assert _derisk_fills(c, pid) == [(None, None, None, "chain_confirmed_unpriced", "0xpart")]
    assert m.derisk_proceeds(c, pid) is None
    assert any(kind == "DERISK-LANDED" and "operator" in text for kind, text in sent)
    # a priced final close books realized unknown, never a number short by the partial
    receipts.weth["0xclose"] = 0.0035
    receipts.available.add("0xclose")
    r = _close(c, pid, receipts)
    assert r["status"] == "closed" and r["realized"] is None
    assert m.get_position(c, pid)["realized_pnl"] is None
    assert not any("$+0.00" in text or "$0.00" in text for _k, text in sent)


def test_a_close_booked_before_the_partial_existed_gains_the_partial_once():
    """A legacy closed row still carrying a pending derisk sig (pos 9003 shape): its realized was
    booked without the partial; once the partial is priced, realized grows by it exactly once."""
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003, "0xclose": 0.0035})
    _partial_unknown(c, pos, receipts)
    claim = m.get_position(c, pid)["derisk_claim_id"]
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=-3.0, "
              "exit_reason='mirror' WHERE id=?", ("2026-07-09T00:30:00+00:00", pid))
    m.insert_fill(c, position_id=pid, kind="mirror_exit", price=1.4, qty=5.0, notional=7.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:30:00+00:00",
                  tx_sig="0xclose")
    c.commit()
    receipts.available.add("0xpart")
    for _ in range(3):
        R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    row = m.get_position(c, pid)
    assert row["realized_pnl"] == pytest.approx(3.0) and row["derisk_claim_id"] is None
    assert claim is not None


@pytest.mark.parametrize("order", ["derisk_first", "close_first"])
def test_an_unpriced_partial_and_an_unpriced_close_settle_to_the_same_realized(order):
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003, "0xclose": 0.0035})
    _partial_unknown(c, pos, receipts)
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)       # unpriced partial
    r = _close(c, pid, receipts)
    assert r["status"] == "closed" and r["realized"] is None   # unpriced close
    receipts.available.update({"0xpart", "0xclose"})
    passes = ([R.recover_unverified_derisks, R.recover_unverified_closes] if order == "derisk_first"
              else [R.recover_unverified_closes, R.recover_unverified_derisks])
    for _ in range(2):
        for run in passes:
            run(c, _ctx(receipts), NOW)
    assert m.get_position(c, pid)["realized_pnl"] == pytest.approx(3.0)


# ---- N13 ----

def test_codex_n13_an_evm_unpriced_close_is_reconciled_by_the_production_queue_once():
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xclose": 0.0075})                   # $15 at the frozen $2000
    r = _close(c, pid, receipts, tokens=10.0)
    assert r["status"] == "closed" and r["realized"] is None
    assert len(m.unpriced_full_exits(c)) == 1
    R.recover_unverified_closes(c, _ctx(receipts), NOW)        # receipt still unavailable
    assert m.get_position(c, pid)["realized_pnl"] is None
    receipts.available.add("0xclose")
    for _ in range(3):
        R.recover_unverified_closes(c, _ctx(receipts), NOW)
    assert m.get_position(c, pid)["realized_pnl"] == pytest.approx(5.0)
    fills = c.execute("SELECT price, notional, fill_source FROM memecoin_fills WHERE position_id=? "
                      "AND kind='mirror_exit'", (pid,)).fetchall()
    assert [tuple(f) for f in fills] == [(pytest.approx(1.5), pytest.approx(15.0),
                                          "chain_confirmed_receipt_reconciled")]
    assert all(q == FROZEN_ETH_USD for tx, q in receipts.calls if tx == "0xclose")
    assert m.unpriced_full_exits(c) == []


def test_an_evm_close_whose_intent_lost_its_frozen_price_stays_unpriced():
    import json
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xclose": 0.0075})
    _close(c, pid, receipts, tokens=10.0)
    for raw, claim in c.execute("SELECT intent_json, claim_id FROM memecoin_exit_intents "
                                "WHERE position_id=? AND action='close'", (pid,)).fetchall():
        d = json.loads(raw); d["quote_asset_usd"] = None
        c.execute("UPDATE memecoin_exit_intents SET intent_json=? WHERE claim_id=?", (json.dumps(d), claim))
    c.execute("UPDATE memecoin_positions SET exit_intent_json=NULL WHERE id=?", (pid,))
    c.commit()
    receipts.available.add("0xclose")
    R.recover_unverified_closes(c, _ctx(receipts), NOW)
    assert m.get_position(c, pid)["realized_pnl"] is None


def test_the_pricing_write_waits_for_the_wallet_lock():
    """Without the executor's lock a close could book realized NULL after the partial is priced
    and leave nothing to revisit it; recovery must not price outside that lock."""
    c = _c(); pos = _pos(c); pid = pos["id"]
    receipts = Receipts({"0xpart": 0.003})
    _partial_unknown(c, pos, receipts)
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    receipts.available.add("0xpart")
    no_lock = {"8453": {"confirm_once_fn": lambda h: "confirmed", "receipt_proceeds_fn": receipts}}
    R.recover_unverified_derisks(c, no_lock, NOW)
    assert _derisk_fills(c, pid)[0][0] is None
    R.recover_unverified_derisks(c, _ctx(receipts), NOW)
    assert m.derisk_proceeds(c, pid) == pytest.approx(6.0)


def test_a_paper_orphan_with_an_unpriced_partial_finalizes_unknown_never_raises():
    """Gamma LOW-1: the paper orphan sweep summed derisk_proceeds directly; an unpriced partial
    (None) would raise and stop the sweep. It now books realized unknown, like every close path."""
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _c(); pos = _pos(c); pid = pos["id"]
    c.execute("UPDATE memecoin_positions SET execution_mode='paper' WHERE id=?", (pid,)); c.commit()
    m.insert_fill(c, position_id=pid, kind="derisk", price=None, qty=5.0, notional=None,
                  fill_source="chain_confirmed_unpriced", fraction=0.5,
                  created_at="2026-07-09T00:05:00+00:00", tx_sig="0xpart")
    assert m.claim_close(c, pid) is True
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.4, qty=5.0, notional=7.0,
                  fill_source="route_quote", fraction=1.0, created_at="2026-07-09T00:30:00+00:00")
    assert recover_orphaned_paper_closes(c, NOW) == 1
    row = m.get_position(c, pid)
    assert row["is_open"] == 0 and row["realized_pnl"] is None
