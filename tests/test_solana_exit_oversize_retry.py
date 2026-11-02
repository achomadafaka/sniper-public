"""A sell whose signed payload cannot fit a Solana packet is re-quoted with a capped account count
before the write-ahead (2026-09-13: {CAT} 56745 refused at 1,446 / 1,290 bytes; POCHACCO 56510
at 1,296). Buys already did this; sells burned a rung on the send seam's refusal."""
import base64

import sniper.execution.solana_exit_executor as X


def test_an_oversized_payload_is_detected_and_a_fitting_one_is_not():
    big = base64.b64encode(b"x" * 1300).decode()
    small = base64.b64encode(b"x" * 900).decode()
    assert X._oversized(big) is True and X._oversized(small) is False


def test_the_quote_is_capped_only_after_an_oversize(monkeypatch):
    calls = []

    def quote_fn(mint, tokens, slip, max_accounts=None):
        calls.append(max_accounts)
        return {"ok": True}
    X._quote(quote_fn, "M", 1.0, 3.0, None)
    X._quote(quote_fn, "M", 1.0, 10.0, 48)
    assert calls == [None, 48]


def test_a_legacy_quote_fn_without_the_cap_is_called_as_before():
    seen = []

    def legacy(mint, tokens, slip):
        seen.append((mint, tokens, slip))
        return {"ok": True}
    assert X._quote(legacy, "M", 2.0, 25.0, 48) == {"ok": True}
    assert seen == [("M", 2.0, 25.0)]


def test_both_exit_loops_drop_an_oversized_payload_before_the_write_ahead():
    import inspect
    for fn, write in ((X.execute_exit, "mark_position_close_submitted"),
                      (X.execute_partial_exit, "signed_tx, sig = signed[:2]")):
        src = inspect.getsource(fn)
        i = src.index("if cap_accounts is None and _oversized(signed_tx)")
        assert "cap_accounts = _exit_retry_accounts()" in src[i:i + 200]
        assert src.index("_quote(quote_fn, mint, tokens, slip, cap_accounts") < i
    full = inspect.getsource(X.execute_exit)
    assert full.index("_oversized(signed_tx)") < full.index("mark_position_close_submitted(")


def test_an_oversized_sell_is_rebuilt_capped_and_sent_once():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.02, qty=500.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t")
    pos = m.get_position(c, pid)
    state = {"sold": False, "quotes": [], "sent": 0}

    def quote_fn(mint, tokens, slip, max_accounts=None):
        state["quotes"].append(max_accounts)
        return {"inAmount": "500", "outAmount": "100", "otherAmountThreshold": "90",
                "_capped": bool(max_accounts)}

    def sign_fn(quote):
        size = 900 if quote.get("_capped") else 1446
        return (base64.b64encode(b"x" * size).decode(), "SIG%d" % len(state["quotes"]))

    def send_fn(signed):
        state["sent"] += 1

    def confirm_fn(sig):
        state["sold"] = True
        return "confirmed"
    old = cfg.MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS
    cfg.MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS = 48
    try:
        r = X.execute_exit(c, pos, "real_tp", "2026-09-13T06:58:38+00:00",
                           quote_fn=quote_fn, sign_fn=sign_fn, send_fn=send_fn, confirm_fn=confirm_fn,
                           usdc_fn=lambda: 15.0 if state["sold"] else 0.0,
                           token_balance_fn=lambda mint: 500.0, slippage_ladder=[3.0, 10.0, 25.0])
    finally:
        cfg.MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS = old
    assert r["status"] == "closed"
    assert state["quotes"] == [None, 48], "the second rung is capped"
    assert state["sent"] == 1, "the oversized payload never reached the send seam"
