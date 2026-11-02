"""Item 16 — real exit executor (DISARMED; injected seams). Sells the ENTIRE token balance
(no decimals math), prices realized PnL from the on-chain USDC delta, walks a slippage ladder on
failure, then marks a STUCK BAG (real, un-exited, alerting) — never a fake clean close. Unknown
confirmation leaves the position OPEN and does not retry (the sell may have landed)."""
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.execution import solana_exit_executor as ex


def _c(entry_notional=10.0):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=entry_notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.02, qty=500.0, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at="t")
    return c, m.get_position(c, pid)


class _Deps:
    def __init__(self, confirms, received=15.0):
        self.confirms = list(confirms)   # per-rung outcome
        self.rung = 0
        self._received = received
        self.calls = []
    def quote_fn(self, mint, tokens, slippage_pct):
        self.calls.append(("quote", slippage_pct)); return {
            "inAmount": "500", "outAmount": "100", "otherAmountThreshold": "90"}
    def usdc_fn(self):
        # post-sell balance only after a confirmed send
        return self._received if "sold" in [c[0] if isinstance(c, tuple) else c for c in self.calls] else 0.0
    def token_fn(self, mint):
        return 500.0
    def sign_fn(self, quote):
        return (b"tx", f"SIG{self.rung}")
    def send_fn(self, signed):
        self.calls.append("send")
    def confirm_fn(self, sig):
        out = self.confirms[self.rung]; self.rung += 1
        if out == "confirmed":
            self.calls.append("sold")
        return out


def _run(c, pos, d, reason="b_timecap"):
    return ex.execute_exit(c, pos, reason, "2026-07-09T01:00:00Z",
                           quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                           confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                           slippage_ladder=[3.0, 10.0, 25.0])


def test_confirmed_first_rung_closes_with_chain_delta_pnl():
    c, pos = _c(entry_notional=10.0); d = _Deps(["confirmed"], received=15.0)
    r = _run(c, pos, d)
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 0 and abs(p["realized_pnl"] - 5.0) < 1e-9    # 15 received - 10 cost
    assert p["exit_reason"] == "b_timecap"
    fill = c.execute("SELECT kind, tx_sig FROM memecoin_fills WHERE position_id=? AND kind='bracket_exit'",
                     (pos["id"],)).fetchone()
    assert fill["tx_sig"] == "SIG0"


def test_confirmed_full_exit_fill_kind_matches_reason():
    for reason, expected_kind in (
        ("mirror", "mirror_exit"),
        ("manual", "manual_close"),
        ("b_stop", "bracket_exit"),
    ):
        c, pos = _c(); d = _Deps(["confirmed"])
        assert _run(c, pos, d, reason=reason)["status"] == "closed"
        fill = c.execute(
            "SELECT kind FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
            (pos["id"],),
        ).fetchone()
        assert fill["kind"] == expected_kind


def test_ladder_widens_then_succeeds():
    c, pos = _c(); d = _Deps(["failed", "confirmed"], received=12.0)
    r = _run(c, pos, d)
    assert r["status"] == "closed"
    assert [c[1] for c in d.calls if c[0] == "quote"][:2] == [3.0, 10.0]   # widened 3% -> 10%


def test_all_rungs_fail_marks_stuck_bag_still_open():
    c, pos = _c(); d = _Deps(["failed", "failed", "failed"])
    r = _run(c, pos, d)
    assert r["status"] == "stuck"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["stuck_bag"] == 1     # real, un-exited, flagged — NOT a fake close


def test_unknown_leaves_open_and_does_not_retry():
    c, pos = _c(); d = _Deps(["timeout", "confirmed"])   # 2nd rung would succeed but must not be tried
    r = _run(c, pos, d)
    assert r["status"] == "unknown"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and (p["stuck_bag"] or 0) == 0
    assert d.calls.count("send") == 1                    # sent exactly once, no retry after unknown


def test_transient_zero_token_read_does_not_book_gone():
    """token_balance fails CLOSED to 0.0 on an RPC blip — believing it books a -100% 'gone' close
    on tokens the wallet still holds. Must bounded-re-read before the gone branch."""
    c, pos = _c(); d = _Deps(["confirmed"])
    reads = {"n": 0}
    def flaky_tokens(mint):
        reads["n"] += 1
        return 0.0 if reads["n"] == 1 else 500.0         # transient blip, then truth
    sleeps = []
    r = ex.execute_exit(c, pos, "b_timecap", "2026-07-09T01:00:00Z",
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=flaky_tokens,
                        slippage_ladder=[3.0], sleep_fn=sleeps.append)
    assert r["status"] == "closed"                       # NOT gone
    assert len(sleeps) == 1
    p = m.get_position(c, pos["id"])
    assert p["exit_reason"] == "b_timecap"               # real close, not b_timecap_gone


def test_zero_token_balance_stays_open_until_a_sale_is_proven():
    """A zero token read cannot close a full position without a sale receipt."""
    c, pos = _c(); d = _Deps(["confirmed"])

    out = ex.execute_exit(
        c, pos, "b_stop", "2026-08-31T22:09:49Z",
        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=lambda _mint: 0.0,
        slippage_ladder=[3], sleep_fn=lambda _seconds: None,
    )

    row = m.get_position(c, pos["id"])
    assert out["status"] == "stuck"
    assert "send" not in d.calls
    assert row["is_open"] == 1 and row["stuck_bag"] == 1
    assert row["realized_pnl"] is None and row["close_claim_id"] is None


def test_confirmed_sell_with_lagging_usdc_read_books_real_received():
    """A lagging post-sell USDC read of 0 would book realized=-entry_notional on money that DID
    arrive. Must bounded-re-read before finalizing."""
    c, pos = _c()                                        # entry_notional 10.0
    state = {"sold": False, "post_reads": 0}
    def usdc_fn():
        if not state["sold"]:
            return 0.0
        state["post_reads"] += 1
        return 0.0 if state["post_reads"] == 1 else 15.0
    def confirm_fn(sig):
        state["sold"] = True
        return "confirmed"
    sleeps = []
    r = ex.execute_exit(c, pos, "b_timecap", "2026-07-09T01:00:00Z",
                            quote_fn=lambda m_, t, s: {
                                "inAmount": "500", "outAmount": "100",
                                "otherAmountThreshold": "90"},
                        sign_fn=lambda q: (b"tx", "SIG"), send_fn=lambda s: None,
                        confirm_fn=confirm_fn, usdc_fn=usdc_fn,
                        token_balance_fn=lambda m_: 500.0,
                        slippage_ladder=[3.0], sleep_fn=sleeps.append)
    assert r["status"] == "closed"
    assert abs(r["received"] - 15.0) < 1e-9
    assert abs(r["realized"] - 5.0) < 1e-9               # 15 received - 10 entry, NOT -10
    assert len(sleeps) == 1


def test_confirmed_sell_uses_receipt_when_balance_delta_stays_zero():
    c, pos = _c()
    d = _Deps(["confirmed"], received=0.0)
    r = ex.execute_exit(
        c, pos, "manual", "2026-07-09T01:00:00Z",
        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
        receipt_proceeds_fn=lambda sig: 15.0,
        slippage_ladder=[3.0], sleep_fn=lambda _seconds: None,
    )
    assert r["status"] == "closed"
    assert r["received"] == 15.0
    assert m.get_position(c, pos["id"])["realized_pnl"] == 5.0


# --- double-submit guard (2026-07-15 review, HIGH-2): the exit must not sell a position another
# actor already closed. send_fn happens before claim_close, and the web manual close doesn't claim
# at all, so the runner sweep + web button can both sell -> the 2nd reverts + books a phantom loss
# and two finalize_close race last-writer-wins over a real profit. With execution serialized (0.1),
# the loser re-reads is_open and must ABORT WITHOUT SELLING.

def test_already_closed_position_aborts_without_selling():
    c, pos = _c(entry_notional=10.0)
    m.claim_close(c, pos["id"])                    # a concurrent actor already closed it
    d = _Deps(["confirmed"], received=15.0)
    r = _run(c, pos, d)
    assert r["status"] == "already_closed"
    assert "send" not in d.calls, "must NOT broadcast a sell for an already-closed position"
    # the prior close's PnL must be intact (no phantom overwrite)
    row = m.get_position(c, pos["id"])
    assert row["is_open"] == 0


def test_lost_close_claim_race_refuses_before_broadcast(monkeypatch):
    """A close-claim loser never signs or broadcasts; ownership precedes the network."""
    c, pos = _c(entry_notional=10.0)
    monkeypatch.setattr(m, "claim_position_close", lambda *_a, **_k: None)
    d = _Deps(["confirmed"], received=15.0)
    r = ex.execute_exit(c, pos, "b_timecap", "2026-07-09T01:00:00Z",
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                        slippage_ladder=[3.0])
    assert r["status"] == "already_closing"
    fills = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='mirror_exit'").fetchone()[0]
    assert fills == 0, "the losing executor must not insert an exit fill"
    assert "send" not in d.calls


# --- checked USDC reads (2026-08-17, pos#38107): usdc_fn collapses a failed RPC read to 0.0, and a
# zero PRE on a funded wallet books the ENTIRE balance as proceeds (JEW booked +$49.48 on a sell
# that returned $10.87 — verified on-chain). The executor takes usdc_checked_fn, which RAISES.

def test_failed_pre_read_aborts_before_signing():
    c, pos = _c(); d = _Deps(["confirmed"])
    def broken_checked():
        raise RuntimeError("rpc read failed")
    sends = []
    r = ex.execute_exit(c, pos, "manual", "2026-07-09T01:00:00Z",
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn,
                        send_fn=lambda s: sends.append(s),
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                        usdc_checked_fn=broken_checked, token_balance_fn=d.token_fn,
                        slippage_ladder=[3.0])
    assert r["status"] == "balance_unreadable"
    assert sends == []                                   # aborted BEFORE broadcast
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and (p["stuck_bag"] or 0) == 0
    # the claim was released: a later attempt with a working read closes normally
    d2 = _Deps(["confirmed"], received=15.0)
    assert _run(c, m.get_position(c, pos["id"]), d2, reason="manual")["status"] == "closed"


def test_unreadable_post_read_goes_unknown_never_books():
    """Confirmed sell + persistently unreadable balance: never finalize with garbage — durable
    submitted_unknown, reconciliation settles from chain evidence."""
    c, pos = _c()
    state = {"sold": False}
    def checked():
        if state["sold"]:
            raise RuntimeError("rpc read failed")        # pre ok, every post-read fails
        return 42.0
    def confirm_fn(sig):
        state["sold"] = True
        return "confirmed"
    sleeps = []
    r = ex.execute_exit(c, pos, "manual", "2026-07-09T01:00:00Z",
                        quote_fn=lambda m_, t, s: {
                            "inAmount": "500", "outAmount": "100",
                            "otherAmountThreshold": "90"},
                        sign_fn=lambda q: (b"tx", "SIG"), send_fn=lambda s: None,
                        confirm_fn=confirm_fn, usdc_fn=lambda: 0.0,
                        usdc_checked_fn=checked, token_balance_fn=lambda m_: 500.0,
                        slippage_ladder=[3.0], sleep_fn=sleeps.append)
    assert r["status"] == "unknown" and r["sig"] == "SIG"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1                             # NOT closed on a fabricated number
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                     (pos["id"],)).fetchone()[0] == 0
