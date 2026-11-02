"""Item 9 — EVM exit executor (S3-EVM). Mirrors the Solana exit: sell the ENTIRE token balance, price
realized PnL from the on-chain USDC delta (never the quote), walk a slippage ladder (3->10->25) then
mark a STUCK BAG. EVM-specific: an exact-amount approve() of the TOKEN before the sell. unknown/timeout
leaves the position open and STOPS (no double-sell)."""
import sqlite3

import pytest

from sniper.execution.evm_exit_executor import execute_exit_evm
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _pos(c, mint="0xtok", entry_notional=10.0):
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint=mint, ticker="",
                          entry_price=1.0, entry_notional=entry_notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="8453")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:00Z", tx_sig="E")
    return m.get_position(c, pid)


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]; it["i"] += 1
        return v
    return f


_QUOTE = {"sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
          "transaction": {"to": "0xr"}, "issues": {"allowance": {"spender": "0xspender"}}}


# The default approval is PIPELINED (a dict, as 4663 and 56 return): it signs the quote at once.
# A waited approval (True: chains 1, 8453, 5042) re-quotes the rung first; that path is covered by
# test_memecoin_evm_exit_requote_after_waited_approval.py (BUNKER 72163, 2026-10-08).
_PIPELINED = {"pipelined": True, "nonce": 2, "tx_hash": "0xappr"}


def _seams(*, tokens=10.0, quote=_QUOTE, approve=_PIPELINED, sign=("0xraw", "0xhash", 3), confirm="confirmed",
           usdc=(50.0, 65.0), send=None):
    calls = {"approve": [], "send": 0}

    def approve_fn(token, spender, amount):
        calls["approve"].append((token, spender, amount)); return approve

    def send_fn(raw):
        calls["send"] += 1
        return send

    return dict(
        quote_fn=lambda mint, toks, slip: quote,
        approve_fn=approve_fn,
        sign_fn=lambda tx: sign, send_fn=send_fn, confirm_fn=lambda h: confirm,
        usdc_fn=_seq(list(usdc)), token_balance_fn=lambda mint: tokens,
    ), calls


def test_confirmed_close_prices_from_usdc_delta_and_approves_token():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, calls = _seams()
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "closed"
    assert abs(r["realized"] - (65.0 - 50.0 - 10.0)) < 1e-9        # received 15 - entry 10 = +5
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 0 and abs(p["realized_pnl"] - 5.0) < 1e-9
    assert calls["approve"] == [("0xtok", "0xspender", 10)]         # exact-amount TOKEN approval
    fill = c.execute("SELECT tx_sig, kind FROM memecoin_fills WHERE position_id=? AND kind='mirror_exit'",
                     (pos["id"],)).fetchone()
    assert fill["tx_sig"] == "0xhash"


def test_existing_token_allowance_skips_redundant_approval():
    c = _c(); pos = _pos(c)
    seams, calls = _seams(approve=False)
    result = execute_exit_evm(
        c, pos, "manual", "2026-07-09T00:05:00Z", slippage_ladder=[3],
        allowance_fn=lambda _token, _spender: 10, **seams,
    )
    assert result["status"] == "closed"
    assert calls["approve"] == []


@pytest.mark.parametrize("retry", [False, True])
def test_execution_quote_refusal_prevents_the_next_funded_write(retry):
    c = _c(); pos = _pos(c)
    seams, calls = _seams(confirm="failed")
    checks = []
    signed = []
    seams["sign_fn"] = lambda tx: signed.append(tx) or ("0xraw", "0xhash", 3)

    def check(fresh, quote, tokens):
        checks.append(quote)
        return (retry and len(checks) == 1), {"code": "executable_threshold_not_met"}

    result = execute_exit_evm(
        c, pos, "b_stop", "2026-07-09T00:05:00Z", slippage_ladder=[10, 20],
        quote_check_fn=check, sleep_fn=lambda _: None, **seams,
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "executable_threshold_not_met"
    assert len(checks) == 1 + int(retry)
    assert len(signed) == calls["send"] == len(calls["approve"]) == int(retry)
    row = m.get_position(c, pos["id"])
    assert row["is_open"] and row["close_claim_id"] is None


def test_partial_stop_check_uses_the_sold_quantity_before_approval():
    c = _c(); pos = _pos(c)
    seams, calls = _partial_seams()
    seen = []
    result = execute_partial_exit_evm(
        c, pos, 0.5, "t0", slippage_ladder=[10],
        quote_check_fn=lambda fresh, quote, qty: (
            seen.append(qty) or False, {"code": "executable_threshold_not_met"}),
        **seams,
    )
    assert result["status"] == "deferred"
    assert seen == [5.0]
    assert calls["send"] == 0 and calls["approve"] == []
    assert m.get_position(c, pos["id"])["derisk_claim_id"] is None


def test_a_crashed_execution_quote_check_does_not_hold_a_confirmed_stop():
    """2026-09-11 (Gamma D2): before, a crash refused the stop, released the claim, returned
    `deferred` and paged nothing, so a real stop could ride to the -80% catastrophe rail. Only a
    computed value above the stop may hold it; a crash is "unknown" and the sweep's verdict stands."""
    c = _c(); pos = _pos(c)
    seams, calls = _seams()
    def check(*_args):
        raise ValueError("unreadable quote")
    result = execute_exit_evm(
        c, pos, "b_stop", "t0", slippage_ladder=[10], quote_check_fn=check, **seams)
    assert result.get("status") != "deferred"
    assert calls["send"] >= 1


def test_matching_prepared_stop_quote_skips_a_second_route_search():
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()
    quote_calls = []
    seams["quote_fn"] = lambda *_args: quote_calls.append(_args) or pytest.fail(
        "a fresh matching balance must use the prepared transaction-capable quote")
    result = execute_exit_evm(
        c, pos, "b_stop", "2026-09-01T10:06:10+00:00", slippage_ladder=[3],
        prepared_preview={"amount_atomic": 10, "quote": _QUOTE, "quote_evidence": _AT_3}, **seams,
    )

    assert result["status"] == "closed"
    assert quote_calls == []


_AT_3 = {"slippage_bps": 300}


@pytest.mark.parametrize("evidence", [{"slippage_bps": 2500}, {}, None])
def test_a_prepared_quote_at_another_slippage_is_never_signed(evidence):
    """Codex P1 (2026-09-13): the preview quoted at the LAST rung (25%) and rung 0 (3%) reused its
    transaction, so the first sell carried a 25% minimum-output floor. Calldata cannot be
    tightened afterwards: a different or unknown slippage must re-quote at the rung's own."""
    c = _c(); pos = _pos(c)
    seams, _calls = _seams(tokens=10.0)
    quote_calls = []
    seams["quote_fn"] = lambda *args: quote_calls.append(args) or _QUOTE
    execute_exit_evm(
        c, pos, "b_stop", "2026-09-01T10:06:10+00:00", slippage_ladder=[3, 10, 25],
        prepared_preview={"amount_atomic": 10, "quote": _QUOTE, "quote_evidence": evidence},
        **seams,
    )
    assert quote_calls and quote_calls[0][2] == 3


def test_a_raw_prepared_amount_matches_the_human_balance_through_decimals():
    """2026-09-11 (Gamma D7): the live preview stores the RAW balance (10 * 10**18) and the
    executor reads a HUMAN one (10.0), so the old integer compare never matched and every EVM
    exit paid a second full venue quote at rung 0."""
    c = _c(); pos = _pos(c)
    seams, _calls = _seams(tokens=10.0)
    seams["quote_fn"] = lambda *_args: pytest.fail("the confirmed preview must be reused")
    result = execute_exit_evm(
        c, pos, "b_stop", "2026-09-01T10:06:10+00:00", slippage_ladder=[3],
        prepared_preview={"amount_atomic": 10 * 10 ** 18, "quote": _QUOTE, "quote_evidence": _AT_3},
        token_decimals_fn=lambda mint: 18, **seams,
    )
    assert result["status"] == "closed"


def test_a_raw_prepared_amount_for_a_different_balance_is_not_reused():
    c = _c(); pos = _pos(c)
    seams, _calls = _seams(tokens=10.0)
    quote_calls = []
    seams["quote_fn"] = lambda *args: quote_calls.append(args) or _QUOTE
    execute_exit_evm(
        c, pos, "b_stop", "2026-09-01T10:06:10+00:00", slippage_ladder=[3],
        prepared_preview={"amount_atomic": 9 * 10 ** 18, "quote": _QUOTE, "quote_evidence": _AT_3},
        token_decimals_fn=lambda mint: 18, **seams,
    )
    assert len(quote_calls) == 1


def test_stale_prepared_stop_quote_falls_back_to_a_new_route_search():
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()
    quote_calls = []
    seams["quote_fn"] = lambda *args: quote_calls.append(args) or _QUOTE
    result = execute_exit_evm(
        c, pos, "b_stop", "2026-09-01T10:06:10+00:00", slippage_ladder=[3],
        prepared_preview={"amount_atomic": 9, "quote": _QUOTE, "quote_evidence": _AT_3}, **seams,
    )

    assert result["status"] == "closed"
    assert len(quote_calls) == 1


def test_confirmed_full_exit_fill_kind_matches_reason():
    for reason, expected_kind in (
        ("mirror", "mirror_exit"),
        ("manual", "manual_close"),
        ("b_stop", "bracket_exit"),
    ):
        c = _c(); pos = _pos(c)
        seams, _ = _seams()
        assert execute_exit_evm(c, pos, reason, "2026-07-09T00:05:00Z",
                                slippage_ladder=[3], **seams)["status"] == "closed"
        fill = c.execute(
            "SELECT kind FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
            (pos["id"],),
        ).fetchone()
        assert fill["kind"] == expected_kind


def test_zero_token_balance_stays_open_until_a_sale_is_proven():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, calls = _seams(tokens=0.0)
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25],
                         sleep_fn=lambda s: None, **seams)
    row = m.get_position(c, pos["id"])
    assert r["status"] == "stuck" and calls["send"] == 0
    assert row["is_open"] == 1 and row["stuck_bag"] == 1
    assert row["closed_at"] is None and row["realized_pnl"] is None


def test_lagged_zero_balance_read_does_not_false_close_gone():
    # one bad RPC read (erc20_balance fails closed to 0.0) must NOT book a -100% 'gone' close
    # while the tokens still sit in the wallet — bounded re-read first (Solana's pattern).
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, calls = _seams()
    reads = iter([0.0, 0.0, 10.0])                                  # two bad reads, then truth
    seams["token_balance_fn"] = lambda mint: next(reads, 10.0)
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25],
                         sleep_fn=lambda s: None, **seams)
    assert r["status"] == "closed"                                  # sold the real balance
    assert calls["send"] == 1


def test_lagged_received_read_rereads_before_booking():
    # a confirmed sell with a laggy USDC read must re-read before booking realized=-entry
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams(usdc=(50.0, 50.0, 65.0))                      # pre=50, lag: 50, re-read: 65
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25],
                         sleep_fn=lambda s: None, **seams)
    assert r["status"] == "closed"
    assert abs(r["realized"] - 5.0) < 1e-9                          # 15 received - 10 entry


def test_confirmed_full_exit_uses_exact_receipt_proceeds_after_balance_lag():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams(usdc=(50.0, 50.0))
    r = execute_exit_evm(
        c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
        sleep_fn=lambda _s: None, receipt_proceeds_fn=lambda _hash: 15.0, **seams,
    )
    assert r["status"] == "closed" and r["received"] == 15.0
    assert m.get_position(c, pos["id"])["realized_pnl"] == 5.0


def test_exact_receipt_overrides_a_larger_whole_wallet_delta():
    c = _c(); pos = _pos(c, entry_notional=41.83)
    seams, _ = _seams(usdc=(0.0, 330.370697614186))
    r = execute_exit_evm(
        c, pos, "real_tp", "2026-08-31T20:41:28Z", slippage_ladder=[10],
        receipt_proceeds_fn=lambda _hash: 51.574448330221,
        sleep_fn=lambda _seconds: None, **seams,
    )
    assert r["received"] == pytest.approx(51.574448330221)
    assert r["realized"] == pytest.approx(9.744448330221)


def test_confirmed_full_exit_uses_receipt_after_balance_read_error():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams()
    reads = {"n": 0}

    def balance():
        reads["n"] += 1
        if reads["n"] == 1:
            return 50.0
        raise TimeoutError("RPC timeout")

    seams["usdc_fn"] = balance
    r = execute_exit_evm(
        c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
        sleep_fn=lambda _s: None, receipt_proceeds_fn=lambda _hash: 15.0, **seams,
    )
    assert r["status"] == "closed" and r["received"] == 15.0


def test_confirmed_full_exit_keeps_claim_when_proceeds_stay_unknown():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams(usdc=(50.0, 50.0))
    r = execute_exit_evm(
        c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
        sleep_fn=lambda _s: None, receipt_proceeds_fn=lambda _hash: None, **seams,
    )
    row = m.get_position(c, pos["id"])
    assert r["status"] == "unknown"
    assert row["is_open"] == 1 and row["close_claim_id"] and row["close_sig"] == "0xhash"
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
        (pos["id"],),
    ).fetchone()[0] == 0


def test_ladder_exhausts_to_stuck_bag():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=None)                                   # no route at any rung
    r = execute_exit_evm(c, pos, "b_stop", "t", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "stuck"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["stuck_bag"] == 1               # stays open + flagged


def test_route_capacity_uses_a_half_exit_before_marking_the_bag_stuck():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=None)
    calls = []

    def partial_exit(fraction):
        calls.append(fraction)
        return {"status": "derisked", "received": 4.0}

    result = execute_exit_evm(
        c, pos, "b_stop", "t", slippage_ladder=[3, 10], **seams,
        quote_miss_fn=lambda _slip: {"code": "route_capacity"},
        partial_exit_fn=partial_exit,
    )

    assert result == {
        "status": "derisked", "received": 4.0,
        "fallback_reason": "route_capacity", "fallback_fraction": 0.5,
    }
    assert calls == [0.5]
    assert m.get_position(c, pos["id"])["stuck_bag"] == 0


def test_non_capacity_quote_failure_does_not_use_a_partial_exit():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=None)

    result = execute_exit_evm(
        c, pos, "b_stop", "t", slippage_ladder=[3], **seams,
        quote_miss_fn=lambda _slip: {"code": "no_route"},
        partial_exit_fn=lambda _fraction: pytest.fail("no-route must not sell a partial bag"),
    )

    assert result == {"status": "stuck", "failure_type": "no_route"}
    assert m.get_position(c, pos["id"])["stuck_bag"] == 1


def test_unknown_leaves_open_no_double_sell():
    c = _c(); pos = _pos(c)
    seams, calls = _seams(confirm="unknown")
    r = execute_exit_evm(c, pos, "mirror", "t", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "unknown" and calls["send"] == 1        # sent once, not retried
    assert m.get_position(c, pos["id"])["is_open"] == 1


def test_failed_rung_widens_then_confirms():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    # first rung confirm 'failed', second 'confirmed'
    confirms = _seq(["failed", "confirmed"])
    seams, _ = _seams(usdc=(50.0, 50.0, 65.0))
    seams["confirm_fn"] = lambda h: confirms()
    r = execute_exit_evm(c, pos, "mirror", "t", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "closed"


def test_definite_send_rejection_widens_without_confirming_rejected_hash():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    sends = _seq([False, "0xaccepted"])
    confirms = {"n": 0}
    seams, calls = _seams(usdc=(50.0, 50.0, 65.0))
    seams["send_fn"] = lambda _raw: calls.__setitem__("send", calls["send"] + 1) or sends()

    def confirm(_hash):
        confirms["n"] += 1
        return "confirmed"

    seams["confirm_fn"] = confirm
    r = execute_exit_evm(c, pos, "mirror", "t", slippage_ladder=[3, 10], **seams)
    assert r["status"] == "closed"
    assert calls["send"] == 2 and confirms["n"] == 1


def test_approve_failure_widens_rung():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(approve=False)                               # approve fails every rung -> stuck
    r = execute_exit_evm(c, pos, "mirror", "t", slippage_ladder=[3, 10], **seams)
    assert r["status"] == "stuck"


def test_no_approval_when_allowance_ok():
    c = _c(); pos = _pos(c, entry_notional=10.0)
    q = {"sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
         "transaction": {"to": "0xr"}, "issues": {"allowance": None}}
    seams, calls = _seams(quote=q)
    r = execute_exit_evm(c, pos, "mirror", "t", slippage_ladder=[3], **seams)
    assert r["status"] == "closed" and calls["approve"] == []


# --- realized PnL must include prior de-risk proceeds (Codex-flagged, 2026-07-11; unfixed until now)
#
# The Solana sibling books `received + derisk_proceeds - entry_notional`, and says why in its own
# comment: "Without the derisk term a de-risked winner books as a loss." The EVM twin booked only
# `received - entry_notional`, silently dropping every partial-sell leg. It stayed dormant because
# MEMECOIN_REAL_DERISK_PCT=0 and the EVM context wires no derisk_fn — but the EVM leg went live on
# 2026-07-14 (the honeypot false-positive was fixed), so this is now a loaded gun, not a footnote.

def test_realized_includes_prior_derisk_proceeds():
    """De-risked winner: sold half for $8, final leg returns $7 on a $10 entry.
    True realized = 8 + 7 - 10 = +$5. The bug books 7 - 10 = -$3 — a winner recorded as a loss."""
    c = _c()
    pos = _pos(c, entry_notional=10.0)
    m.insert_fill(c, position_id=pos["id"], kind="derisk", price=1.6, qty=5.0, notional=8.0,
                  fill_source="chain", fraction=0.5, created_at="2026-07-09T01:00:00Z", tx_sig="D")
    seams, _ = _seams(tokens=5.0, usdc=(50.0, 57.0))          # final leg returns $7

    res = execute_exit_evm(c, pos, "b_tp", "2026-07-09T02:00:00Z", **seams)

    assert res["status"] == "closed"
    assert abs(res["realized"] - 5.0) < 1e-9, (
        "realized must be derisk(8) + received(7) - entry(10) = +5, got %r" % res["realized"])
    row = m.get_position(c, pos["id"])
    assert abs(row["realized_pnl"] - 5.0) < 1e-9


def test_zero_token_balance_with_prior_derisk_stays_unrealized():
    """A zero read cannot close a full position without a sale receipt."""
    c = _c()
    pos = _pos(c, entry_notional=10.0)
    m.insert_fill(c, position_id=pos["id"], kind="derisk", price=1.6, qty=5.0, notional=8.0,
                  fill_source="chain", fraction=0.5, created_at="2026-07-09T01:00:00Z", tx_sig="D")
    seams, _ = _seams(tokens=0.0)

    res = execute_exit_evm(c, pos, "b_stop", "2026-07-09T02:00:00Z", **seams)

    assert res["status"] == "stuck"
    row = m.get_position(c, pos["id"])
    assert row["is_open"] == 1 and row["stuck_bag"] == 1
    assert row["realized_pnl"] is None


# --- double-submit guard (HIGH-2): the runner sweep + web manual close can target the same position.
# The `position` dict is a stale snapshot; a re-read of is_open before broadcasting, plus honoring
# claim_close's atomic result before finalizing, prevents a second real sell + a duplicate ledger row.

def test_already_closed_position_aborts_without_selling():
    """A concurrent actor already claimed+closed the position. The executor must re-read is_open,
    see 0, and abort BEFORE broadcasting any sell (no send, no fill, no re-finalize)."""
    c = _c(); pos = _pos(c, entry_notional=10.0)
    assert m.claim_close(c, pos["id"]) is True          # someone else wins the claim first
    m.finalize_close(c, pos["id"], "2026-07-09T00:04:00Z", 3.0, exit_reason="mirror")
    seams, calls = _seams()
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "already_closed"
    assert calls["send"] == 0                            # never broadcast a sell for gone tokens
    fills = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='mirror_exit'",
                      (pos["id"],)).fetchone()[0]
    assert fills == 0                                    # no duplicate exit fill
    row = m.get_position(c, pos["id"])
    assert abs(row["realized_pnl"] - 3.0) < 1e-9         # winner's realized untouched


def test_lost_close_claim_race_refuses_before_approval_or_broadcast(monkeypatch):
    """An EVM close-claim loser performs no approval, signature, or sell."""
    c = _c(); pos = _pos(c, entry_notional=10.0)
    monkeypatch.setattr(m, "claim_position_close", lambda *_a, **_k: None)
    seams, calls = _seams()
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3, 10, 25], **seams)
    assert r["status"] == "already_closing"
    assert calls["approve"] == [] and calls["send"] == 0
    fills = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='mirror_exit'",
                      (pos["id"],)).fetchone()[0]
    assert fills == 0                                    # loser inserts no fill


# ── partial exit / derisk (2026-07-17 — previously the EVM leg had NO derisk executor) ──

from sniper.execution.evm_exit_executor import execute_partial_exit_evm


def _partial_seams(**kw):
    seams, calls = _seams(**kw)
    full = seams.pop("quote_fn")
    seams["quote_fn"] = lambda mint, toks, slip, fraction: full(mint, toks, slip)
    return seams, calls


def test_partial_confirmed_books_derisk_fill_stays_open():
    c = _c(); pos = _pos(c)
    seams, calls = _partial_seams(usdc=(50.0, 60.0))          # $10 received on the partial
    res = execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                                   slippage_ladder=[0.03], **seams)
    assert res["status"] == "derisked" and abs(res["received"] - 10.0) < 1e-9
    row = m.get_position(c, pos["id"])
    assert row["is_open"] == 1 and row["derisked_at"] and row["derisk_sig"] is None
    assert m.derisk_proceeds(c, pos["id"]) == 10.0
    f = c.execute("SELECT kind, qty, fraction FROM memecoin_fills WHERE position_id=? "
                  "AND kind='derisk'", (pos["id"],)).fetchone()
    assert f["fraction"] == 0.6 and abs(f["qty"] - 6.0) < 1e-9   # 0.6 × 10 tokens


def test_partial_confirmed_uses_exact_receipt_proceeds_after_balance_lag():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(usdc=(50.0, 50.0))
    res = execute_partial_exit_evm(
        c, pos, 0.6, "t0", sleep_fn=lambda _s: None,
        slippage_ladder=[0.03], receipt_proceeds_fn=lambda _hash: 10.0, **seams,
    )
    assert res["status"] == "derisked" and res["received"] == 10.0
    assert m.derisk_proceeds(c, pos["id"]) == 10.0


def test_partial_unknown_persists_hash_books_nothing():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(confirm="pending")
    res = execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                                   slippage_ladder=[0.03], **seams)
    assert res["status"] == "unknown" and res["sig"] == "0xhash"
    row = m.get_position(c, pos["id"])
    assert row["derisked_at"] and row["derisk_sig"] == "0xhash"
    assert m.derisk_proceeds(c, pos["id"]) == 0.0


def test_partial_crash_after_broadcast_persists_hash_before_send():
    c = _c(); pos = _pos(c)
    seams, calls = _partial_seams()
    seams["confirm_fn"] = lambda _hash: (_ for _ in ()).throw(RuntimeError("post-send crash"))

    with pytest.raises(RuntimeError, match="post-send crash"):
        execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                                 slippage_ladder=[0.03], **seams)

    row = m.get_position(c, pos["id"])
    assert calls["send"] == 1
    assert row["derisked_at"] == "t0"
    assert row["derisk_sig"] == "0xhash"
    assert row["derisk_pending_at"] is None

    seams2, calls2 = _partial_seams()
    retry = execute_partial_exit_evm(c, row, 0.6, "t1", sleep_fn=lambda s: None,
                                     slippage_ladder=[0.03], **seams2)
    assert retry["status"] == "already_derisked"
    assert calls2["send"] == 0


def test_partial_failed_widens_then_exhausts_no_stuck_bag():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(confirm="failed")
    res = execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                                   slippage_ladder=[0.03, 0.10], **seams)
    assert res["status"] == "derisk_failed" and res["reason"] == "ladder exhausted"
    row = m.get_position(c, pos["id"])
    assert row["is_open"] == 1 and not row["derisked_at"] and not row["stuck_bag"]


def test_partial_fraction_bounds_and_closed_guard():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams()
    assert execute_partial_exit_evm(c, pos, 1.0, "t0", sleep_fn=lambda s: None,
                                    slippage_ladder=[0.03], **seams)["status"] == "derisk_failed"
    m.claim_close(c, pos["id"])
    assert execute_partial_exit_evm(c, pos, 0.5, "t0", sleep_fn=lambda s: None,
                                    slippage_ladder=[0.03], **seams)["status"] == "already_closed"


def test_evm_context_exposes_derisk_fn_when_executor_provides():
    # the context previously DROPPED derisk_fn even if present — regression-lock the wiring
    import inspect
    from sniper.services import memecoin_evm_leg as leg
    src = inspect.getsource(leg.build_real_evm_context)
    assert '"derisk_fn": ex.get("derisk_fn")' in src


# --- exit floor sanity at the signing boundary (UNIFROG 27097, 2026-08-05) ------

def test_implausible_floor_refuses_to_sign_and_never_sends():
    """A quote taken inside a corrupted-tape window is internally consistent and still
    authorises selling the bag for nothing. Refusing costs one sweep; signing cost 27097
    a ~$6 bag for $0.02."""
    c = _c(); pos = _pos(c)
    seams, calls = _seams()
    out = execute_exit_evm(
        c, pos, "catastrophe", "2026-07-09T00:01:00Z", **seams,
        floor_usd_fn=lambda intent, quote: 0.02,               # corrupted quote's floor
        reference_value_usd_fn=lambda position, tokens: 6.14,  # trusted bag value
    )
    assert calls["send"] == 0                                  # nothing was broadcast
    assert m.get_position(c, pos["id"])["is_open"] == 1         # position survives to retry
    assert out["status"] != "closed"


def test_plausible_floor_signs_normally():
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()
    out = execute_exit_evm(
        c, pos, "catastrophe", "2026-07-09T00:01:00Z", **seams,
        floor_usd_fn=lambda intent, quote: 9.0,
        reference_value_usd_fn=lambda position, tokens: 10.0,
    )
    assert out["status"] == "closed"


def test_floor_check_is_inert_when_the_seams_are_absent():
    """Unwired callers must behave exactly as before — the guard is opt-in."""
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()
    assert execute_exit_evm(c, pos, "catastrophe",
                            "2026-07-09T00:01:00Z", **seams)["status"] == "closed"


def test_floor_seam_failure_allows_the_exit():
    """If the reference cannot be computed we must not strand the position."""
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()

    def boom(*_a, **_k):
        raise RuntimeError("price source down")

    out = execute_exit_evm(
        c, pos, "catastrophe", "2026-07-09T00:01:00Z", **seams,
        floor_usd_fn=boom, reference_value_usd_fn=lambda position, tokens: 6.14)
    assert out["status"] == "closed"


# --- quote-vs-fill on the EVM exit leg ---------------------------------------------------------
# `execution_slippage_pct` was NULL on every exit. The EVM leg settles in the chain's quote token
# (WETH on 4663), so `buyAmount` is quote-token atomic and needs a USD price -- which is why this
# was deferred when the only option looked like a live ETH/USD read on the exit critical path.
# It is not: `quote_asset_usd` is the SAME snapshot `_execution_balance_usd_fn` used to price
# `received`, and the seam already passes it in. No new network read.

_WETH = 10 ** 18


def _exit_quote(expected_weth):
    return {**_QUOTE, "buyAmount": str(int(expected_weth * _WETH))}


def _fill(c, pid, kind="mirror_exit"):
    return c.execute("SELECT quote_price, execution_slippage_pct, price FROM memecoin_fills "
                     "WHERE position_id=? AND kind=?", (pid, kind)).fetchone()


def test_exit_short_of_its_quote_records_negative_slippage():
    # quoted 0.005 WETH at $3000 = $15.00 for 10 tokens -> 1.50/token
    # received $14.25 -> 1.425/token -> -5.0%
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=_exit_quote(0.005), usdc=(50.0, 64.25))
    assert execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                            quote_asset_usd=3000.0, quote_asset_decimals=18,
                            **seams)["status"] == "closed"
    f = _fill(c, pos["id"])
    assert abs(f["quote_price"] - 1.50) < 1e-9
    assert abs(f["execution_slippage_pct"] - (-5.0)) < 1e-9


def test_the_percentage_does_not_move_with_the_price_snapshot():
    """The load-bearing property, and the reason a slightly stale snapshot is acceptable.

    The same quote-token price multiplies BOTH the quoted output and the realized `received`, so it
    divides out of fill/quote. Here the snapshot moves +10% and the wallet delta moves with it, as
    it does in production where the balance reader uses that very price. The percentage must not
    budge; only the absolute quote_price may.
    """
    out = []
    for px, usdc in ((3000.0, (50.0, 64.25)), (3300.0, (55.0, 70.675))):
        c = _c(); pos = _pos(c)
        seams, _ = _seams(quote=_exit_quote(0.005), usdc=usdc)
        execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                         quote_asset_usd=px, quote_asset_decimals=18, **seams)
        out.append(_fill(c, pos["id"]))
    assert abs(out[0]["execution_slippage_pct"] - out[1]["execution_slippage_pct"]) < 1e-9
    assert abs(out[1]["quote_price"] / out[0]["quote_price"] - 1.1) < 1e-9   # absolute DOES move


def test_no_quote_asset_price_leaves_unknown_not_zero():
    # a 0.0 here would read as "this exit executed exactly at its quote" and bias every aggregate.
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=_exit_quote(0.005), usdc=(50.0, 64.25))
    execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                     quote_asset_usd=None, quote_asset_decimals=18, **seams)
    f = _fill(c, pos["id"])
    assert f["quote_price"] is None and f["execution_slippage_pct"] is None


def test_missing_decimals_leaves_unknown():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=_exit_quote(0.005), usdc=(50.0, 64.25))
    execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                     quote_asset_usd=3000.0, quote_asset_decimals=None, **seams)
    assert _fill(c, pos["id"])["quote_price"] is None


def test_unparseable_buy_amount_does_not_raise_on_a_confirmed_sell():
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote={**_QUOTE, "buyAmount": "not-a-number"}, usdc=(50.0, 64.25))
    assert execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                            quote_asset_usd=3000.0, quote_asset_decimals=18,
                            **seams)["status"] == "closed"
    assert _fill(c, pos["id"])["execution_slippage_pct"] is None


def test_partial_exit_prices_its_quote_against_the_SOLD_quantity():
    """The de-risk leg divides by the part sold, not the whole bag.

    `_partial_quote` sizes the quote to the fraction, so `buyAmount` covers the sold amount. Using
    the bag denominator understates the quote by 1/fraction and can invert the sign. Caught by
    mutating: the first version of this suite exercised only the full exit, so swapping sold_qty
    for tokens on the partial passed clean.
    """
    # 10 tokens held, 60% sold = 6.0; quoted 0.003 WETH at $3000 = $9.00 -> 1.50/token
    # received $8.55 -> 1.425/token -> -5.0%
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(quote=_exit_quote(0.003), usdc=(50.0, 58.55))
    res = execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                                   slippage_ladder=[0.03], quote_asset_usd=3000.0,
                                   quote_asset_decimals=18, **seams)
    assert res["status"] == "derisked"
    f = _fill(c, pos["id"], kind="derisk")
    assert abs(f["quote_price"] - 1.50) < 1e-9
    assert abs(f["execution_slippage_pct"] - (-5.0)) < 1e-9


def test_partial_exit_without_a_price_stays_unknown():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(quote=_exit_quote(0.003), usdc=(50.0, 58.55))
    execute_partial_exit_evm(c, pos, 0.6, "t0", sleep_fn=lambda s: None,
                             slippage_ladder=[0.03], quote_asset_usd=None,
                             quote_asset_decimals=18, **seams)
    f = _fill(c, pos["id"], kind="derisk")
    assert f["quote_price"] is None and f["execution_slippage_pct"] is None


def test_a_refused_route_capacity_partial_keeps_the_bag_and_the_route():
    """A b_stop whose full-size route fails for capacity falls back to a partial sell. When the
    partial's execution quote refuses the stop, the exit is `deferred`: the stop was premature, and
    nothing about the route failed. So the bag is not marked stuck, the route is not forgotten, and
    no "stuck" page goes out. Without the propagation the refusal fell through to all three."""
    c = _c(); pos = _pos(c)
    route = '{"protocol": "uniswap_v4"}'
    c.execute("UPDATE memecoin_positions SET evm_route_json=? WHERE id=?", (route, pos["id"]))
    c.commit()
    seams, calls = _seams(quote=None)

    result = execute_exit_evm(
        c, pos, "b_stop", "t", slippage_ladder=[3, 10], **seams,
        quote_miss_fn=lambda _slip: {"code": "route_capacity"},
        partial_exit_fn=lambda _fraction: {"status": "deferred",
                                           "reason": "executable_threshold_not_met"},
    )

    assert result == {"status": "deferred", "reason": "executable_threshold_not_met"}
    row = m.get_position(c, pos["id"])
    assert row["stuck_bag"] == 0
    assert row["evm_route_json"] == route
    assert calls["send"] == 0


# ── a quote the intent cannot bind is named, never a silent skip (2026-09-23, RATBRAIN 64081) ──

def test_a_quote_without_a_minimum_output_is_a_named_stuck_reason():
    """The Kyber sell quote carried no `minBuyAmount`; `quote_binding_complete` was False at every
    tier, nothing was signed, and the bag was marked stuck with `failure_type` None. The skip now
    names itself so the next one is attributable."""
    c = _c(); pos = _pos(c)
    unbound = {k: v for k, v in _QUOTE.items() if k != "minBuyAmount"}
    seams, calls = _seams(quote=unbound)
    r = execute_exit_evm(c, pos, "b_stop", "2026-07-09T00:05:00Z", slippage_ladder=[10, 20, 30],
                         sleep_fn=lambda s: None, **seams)
    assert r["status"] == "stuck" and r.get("failure_type") == "quote_binding_incomplete"
    assert calls["send"] == 0 and calls["approve"] == []
    assert m.get_position(c, pos["id"])["stuck_bag"] == 1


def test_a_quote_without_a_transaction_is_a_named_stuck_reason():
    c = _c(); pos = _pos(c)
    seams, calls = _seams(quote={k: v for k, v in _QUOTE.items() if k != "transaction"})
    r = execute_exit_evm(c, pos, "b_stop", "2026-07-09T00:05:00Z", slippage_ladder=[10, 20],
                         sleep_fn=lambda s: None, **seams)
    assert r["status"] == "stuck" and r.get("failure_type") == "quote_without_transaction"
    assert calls["send"] == 0


def test_an_exhausted_ladder_logs_the_tiers_the_venues_and_the_last_failure(caplog):
    import logging
    c = _c(); pos = _pos(c)
    seams, _ = _seams(quote=None)
    rungs = [{"rung": "v4", "hit": False}, {"rung": "kyber", "hit": False, "blind_reason": "timeout"}]
    with caplog.at_level(logging.WARNING, logger="memecoin"):
        r = execute_exit_evm(c, pos, "b_stop", "2026-07-09T00:05:00Z", slippage_ladder=[10, 20],
                             sleep_fn=lambda s: None,
                             quote_miss_fn=lambda slip: {"code": "provider_timeout", "rungs": rungs},
                             **seams)
    assert r["status"] == "stuck" and r["failure_type"] == "provider_timeout"
    text = caplog.text
    assert "rungs=v4!,kyber!" in text
    assert "sell ladder exhausted" in text and "tiers=[10, 20]" in text


@pytest.mark.parametrize('refresh', ['valid', 'missing', 'raises', 'amount', 'spender', 'balance', 'transaction'])
def test_reverted_exit_refreshes_winner_with_safe_full_ladder_fallback(refresh):
    c = _c(); pos = _pos(c)
    seams, calls = _seams()
    ladder, requotes, signed = [], [], []
    fresh = {**_QUOTE, 'transaction': {'to': '0xr', 'data': 'fresh'}}

    def requote(slippage_bps=None):
        requotes.append(slippage_bps)
        if refresh == 'raises':
            raise RuntimeError('provider unavailable')
        if refresh == 'missing':
            return None
        if refresh == 'transaction':
            return {**fresh, 'transaction': None}
        if refresh == 'amount':
            return {**fresh, 'sellAmount': '9'}
        if refresh == 'spender':
            return {**fresh, 'issues': {'allowance': {'spender': 'other'}}}
        return fresh

    def quote(mint, tokens, slip):
        ladder.append(slip)
        return {**_QUOTE, '_requote': requote}

    seams['quote_fn'] = quote
    seams['confirm_fn'] = _seq(['failed', 'confirmed'])
    seams['sign_fn'] = lambda tx: signed.append(tx) or ('raw'+str(len(signed)), 'hash'+str(len(signed)), len(signed))
    if refresh == 'balance':
        seams['token_balance_fn'] = _seq([10.0, 10.0, 11.0, 11.0])
    result = execute_exit_evm(c, pos, 'manual', '2026-07-09T00:05:00Z',
                             slippage_ladder=[3, 10], receipt_proceeds_fn=lambda h: 15.0, **seams)
    assert result['status'] == 'closed' and calls['send'] == 2
    assert ladder == ([3] if refresh == 'valid' else [3, 10])
    assert requotes == ([] if refresh == 'balance' else [1000])
    if refresh == 'valid':
        assert signed[1]['data'] == 'fresh'


# ---- Alpha ruling 2026-10-06 (exit proceeds): receipt only; unproven proceeds close UNPRICED ----

def _alerts(monkeypatch):
    from sniper.services import memecoin_alerts
    sent = []
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: sent.append((kind, text)))
    return sent


def test_a_confirmed_sell_with_unproven_proceeds_closes_unpriced_never_open_never_zero(monkeypatch):
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams(usdc=(50.0, 65.0))           # the old wallet window would say +15: not used
    sleeps, sent = [], _alerts(monkeypatch)
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                         sleep_fn=sleeps.append, receipt_proceeds_fn=lambda _h: None,
                         require_receipt_proceeds=True, **seams)
    assert r["status"] == "closed" and r["realized"] is None and r["proceeds_source"] == "unknown"
    row = m.get_position(c, pos["id"])
    assert row["is_open"] == 0 and row["realized_pnl"] is None and row["close_sig"] == "0xhash"
    fill = c.execute("SELECT price, qty, notional, fill_source FROM memecoin_fills WHERE "
                     "position_id=? AND kind='mirror_exit'", (pos["id"],)).fetchone()
    assert tuple(fill) == (None, 10.0, None, "chain_confirmed_unpriced")
    assert sleeps == [1.0, 1.0, 1.0]                               # the receipt read again first
    assert [k for k, _t in sent] == ["EXIT-UNPRICED"]              # exactly one alert
    # the later reconcile books it once, never a second fill
    assert m.reconcile_unpriced_full_exit(c, position_id=pos["id"], sig="0xhash", price=1.5,
                                          qty=10.0, notional=15.0, fill_source="chain_confirmed_receipt_reconciled")
    assert m.get_position(c, pos["id"])["realized_pnl"] == pytest.approx(5.0)
    assert not m.reconcile_unpriced_full_exit(c, position_id=pos["id"], sig="0xhash", price=1.5,
                                              qty=10.0, notional=15.0, fill_source="x")
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                     (pos["id"],)).fetchone()[0] == 1


def test_a_lagging_receipt_is_read_again_before_the_sell_closes_unpriced(monkeypatch):
    c = _c(); pos = _pos(c, entry_notional=10.0)
    seams, _ = _seams()
    answers, sleeps = iter([None, None, 15.0]), []
    r = execute_exit_evm(c, pos, "mirror", "2026-07-09T00:05:00Z", slippage_ladder=[3],
                         sleep_fn=sleeps.append, receipt_proceeds_fn=lambda _h: next(answers),
                         require_receipt_proceeds=True, **seams)
    assert r["status"] == "closed" and r["received"] == 15.0 and sleeps == [1.0, 1.0]
    assert m.get_position(c, pos["id"])["realized_pnl"] == pytest.approx(5.0)


def test_a_derisk_with_unproven_proceeds_books_no_wallet_window():
    c = _c(); pos = _pos(c)
    seams, _ = _partial_seams(usdc=(50.0, 65.0))
    r = execute_partial_exit_evm(c, pos, 0.5, "2026-07-09T00:05:00Z", slippage_ladder=[3],
                                 sleep_fn=lambda _s: None, receipt_proceeds_fn=lambda _h: None,
                                 require_receipt_proceeds=True, **seams)
    assert r["status"] == "unknown"
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                     (pos["id"],)).fetchone()[0] == 0


def test_an_unpriced_close_never_alerts_a_zero_realized(monkeypatch):
    from sniper.services import memecoin_alerts, memecoin_real_runner as rr
    from sniper.services import memecoin_telegram
    monkeypatch.setattr(memecoin_telegram, "notification_lifecycle_active", lambda: False)
    seen = []
    monkeypatch.setattr(memecoin_alerts, "alert_exit", lambda *a, **k: seen.append(a))
    rr._alert_exit_result("0xm", 1, "mirror", {"status": "closed", "realized": None,
                                                 "proceeds_source": "unknown", "tx_hash": "0xh"})
    assert seen == []                     # EXIT-UNPRICED already said it; never "$+0.00"
    rr._alert_exit_result("0xm", 1, "mirror", {"status": "closed", "realized": 5.0, "tx_hash": "0xh"})
    assert len(seen) == 1
