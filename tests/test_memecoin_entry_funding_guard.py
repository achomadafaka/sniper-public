"""The general-path executor refuses a reservation it cannot account for (2026-09-27, operator GO).

execute_entry measures a fill as a USDC balance delta. Native-SOL funding (cbf7680b) is executed
only by the slot lane's execute_prebuilt_entry, which proves the fill from the receipt's lamports.
A SOL-funded reservation that reached execute_entry would buy with USDC it never reserved and book
a USDC delta for it. No path does that today; this pins that one never can: the reservation is
released with a named reason before any quote, read, signature or send.
"""
import sqlite3

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m


def _pending(raw_asset=None, **funding):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", **funding)
    if raw_asset is not None:
        # reserve_pending refuses assets it does not know; a future asset lands like this.
        c.execute("UPDATE memecoin_pending_entries SET funding_asset=? WHERE id=?", (raw_asset, pid))
        c.commit()
    return c, pid


def _never(name, calls):
    def call(*args, **kwargs):
        calls.append(name)
        raise AssertionError(name + " must not run for this reservation")
    return call


@pytest.mark.parametrize("funding", [
    {"funding_asset": "sol", "funding_amount_atomic": 70_000_000, "funding_sol_usd": 140.0,
     "network_id": "solana"},
    {"raw_asset": "bonk"},
])
def test_a_reservation_not_funded_in_usdc_never_reaches_a_quote_or_a_send(funding):
    conn, pid = _pending(**funding)
    calls = []
    try:
        result = ex.execute_entry(
            conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-09-27T17:05:00Z",
            quote_fn=_never("quote", calls), reverse_quote_fn=_never("reverse", calls),
            sign_fn=_never("sign", calls), send_fn=_never("send", calls),
            confirm_fn=_never("confirm", calls), usdc_fn=_never("usdc", calls),
            token_fn=_never("token", calls), impact_cap_pct=15.0)
        row = m.pending_row(conn, pid)
    finally:
        conn.close()
    assert result["status"] == "aborted"
    assert result["reason"] == "general_path_funding_unsupported"
    assert calls == []
    assert row["status"] != "reserved" and row["resolution_reason"] == \
        "general_path_funding_unsupported"


def test_a_usdc_or_unlabelled_reservation_still_executes():
    """The guard names what it refuses; legacy rows (NULL) and 'usdc' pass through untouched."""
    from test_memecoin_entry_roundtrip_cap import _Deps
    for funding in ({}, {"funding_asset": "usdc"}):
        conn, pid = _pending(**funding)
        d = _Deps(quote_impact=0.01, usd_back=9.9)
        try:
            result = ex.execute_entry(
                conn, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
                source_user_id="u1", now_iso="2026-09-27T17:05:00Z",
                quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
                send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                token_fn=d.token_fn, impact_cap_pct=15.0)
        finally:
            conn.close()
        assert result["status"] == "filled", (funding, result)
