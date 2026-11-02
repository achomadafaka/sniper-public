"""EVM gas top-up: unwrap WETH to native, never swap.

The Solana sibling is unavailable on EVM in every parameter, so 4663 has run on hand-funded gas
since it was armed. Measured 2026-08-30: 0.0000925 ETH per funded round trip against a 0.000350
refusal floor -- about 19 trades of runway. These tests pin the fail-safe DIRECTIONS, because every
one of them can be inverted while still "working".
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.execution.exec_lock import wallet_exec_lock
from sniper.services import memecoin_evm_gas_topup as G
import scripts.memecoin_runner_loop as runner_loop

E = 10 ** 18
BASE = dict(low_water_wei=int(0.0020 * E), target_wei=int(0.0030 * E),
            min_weth_reserve_wei=int(0.010 * E), daily_cap_wei=0,
            spent_today_wei=0)
RUN_BASE = {key: value for key, value in BASE.items() if key != "spent_today_wei"}


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def plan(**over):
    kw = dict(BASE); kw.update(over)
    return G.plan_gas_topup(**kw)


def test_tops_up_the_gap_to_target_not_the_whole_balance():
    p = plan(native_balance_wei=int(0.0002 * E), weth_balance_wei=int(0.05 * E))
    assert p["act"] is True and p["state"] == "topup"
    assert p["amount_wei"] == int(0.0030 * E) - int(0.0002 * E)


def test_above_low_water_does_nothing():
    assert plan(native_balance_wei=int(0.0021 * E), weth_balance_wei=int(0.05 * E))["act"] is False


def test_UNREADABLE_NATIVE_IS_NOT_LOW():
    """THE LOAD-BEARING DIRECTION. A transient RPC failure must never trigger a spend. Invert this
    and every RPC hiccup unwraps bankroll. The Solana sibling returns `sol_unreadable` for the same
    reason, and a transient read cost us a $497 entry through the premium gate on 2026-08-30."""
    p = plan(native_balance_wei=None, weth_balance_wei=int(0.05 * E))
    assert p["act"] is False and p["state"] == "native_unreadable"


def test_unreadable_weth_is_not_spare():
    p = plan(native_balance_wei=int(0.0001 * E), weth_balance_wei=None)
    assert p["act"] is False and p["state"] == "weth_unreadable"


def test_reserve_protects_the_trading_bankroll():
    """WETH IS the bankroll. Unwrapping through the reserve converts the last of it into gas for
    trades it can no longer fund."""
    p = plan(native_balance_wei=int(0.0001 * E), weth_balance_wei=int(0.010 * E))
    assert p["act"] is False and p["state"] == "weth_below_reserve"


def test_partial_topup_is_REFUSED_not_clamped():
    """A partial top-up that leaves native under the floor spends a transaction to stay refused.
    Clamping to the available spare is the tempting bug."""
    p = plan(native_balance_wei=int(0.0001 * E),
             weth_balance_wei=int(0.010 * E) + int(0.0005 * E))
    assert p["act"] is False and p["state"] == "insufficient_weth_for_full_topup"


def test_daily_cap_blocks():
    p = plan(native_balance_wei=int(0.0001 * E), weth_balance_wei=int(0.05 * E),
             daily_cap_wei=int(0.004 * E), spent_today_wei=int(0.0095 * E))
    assert p["act"] is False and p["state"] == "daily_cap_reached"


def test_target_at_or_below_low_water_is_refused_as_config():
    """Topping up INTO the refusing state would re-fire every pass forever."""
    p = plan(native_balance_wei=int(0.0001 * E), weth_balance_wei=int(0.05 * E),
             target_wei=int(0.0005 * E))
    assert p["act"] is False and p["state"] == "target_not_above_low_water"


def test_zero_low_water_means_off_never_always():
    p = plan(native_balance_wei=0, weth_balance_wei=int(0.05 * E), low_water_wei=0)
    assert p["act"] is False and p["state"] == "disabled"


def test_zero_daily_cap_means_unlimited_topup():
    p = plan(native_balance_wei=int(0.0001 * E), weth_balance_wei=int(0.05 * E),
             daily_cap_wei=0)
    assert p["act"] is True and p["amount_wei"] == int(0.0030 * E) - int(0.0001 * E)


def test_encode_withdraw_matches_the_selector_the_executor_already_knows():
    """0x2e1a7d4d is matched at evm_swap_executor.py:389 and used by the buy path's pre-transactions.
    A different selector here would be a new calldata shape on the funded key."""
    data = G.encode_withdraw(int(0.002 * E))
    assert data.startswith("0x2e1a7d4d")
    assert len(data) == 10 + 64
    assert int(data[10:], 16) == int(0.002 * E)


def test_encode_withdraw_rejects_negative():
    with pytest.raises(ValueError):
        G.encode_withdraw(-1)


def test_submitted_evm_topup_counts_against_the_cap_and_blocks_the_next_pass():
    """A crash after signing must keep the native amount out of the trading bankroll."""
    conn = _conn()
    row_id = m.record_evm_gas_topup_submission(
        conn, now_iso="2026-08-31T12:00:00+00:00", day="2026-08-31", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xabc", nonce=12,
    )

    assert row_id
    assert m.evm_gas_topup_spent_today(conn, "4663", "2026-08-31") == 1_500_000_000_000_000
    assert m.has_unresolved_evm_gas_topup(conn, "4663") is True

    m.finish_evm_gas_topup(conn, row_id=row_id, status="confirmed")
    assert m.has_unresolved_evm_gas_topup(conn, "4663") is False


@pytest.mark.parametrize("status", ["not_sent", "failed"])
def test_unsent_or_failed_evm_topup_does_not_consume_the_daily_cap(status):
    conn = _conn()
    row_id = m.record_evm_gas_topup_submission(
        conn, now_iso="2026-08-31T12:00:00+00:00", day="2026-08-31", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xabc", nonce=12,
    )
    assert m.finish_evm_gas_topup(conn, row_id=row_id, status=status) is True

    assert m.evm_gas_topup_spent_today(conn, "4663", "2026-08-31") == 0


def _seams(**over):
    calls = {"sent": [], "signed": []}
    kw = dict(
        chain_id="4663", address="0xabc", weth_token="0xWETH", rpc_url="http://rpc",
        native_balance_fn=lambda: int(0.0001 * E),
        weth_balance_fn=lambda: int(0.05 * E),
        nonce_fn=lambda: 7, gas_price_fn=lambda: 1_000_000_000,
        sign_fn=lambda tx: (calls["signed"].append(tx), ("0xsigned", "0xhash"))[1],
        send_fn=lambda raw: (calls["sent"].append(raw), "0xhash")[1],
        confirm_fn=lambda h: True,
        **BASE)
    kw.update(over)
    return kw, calls


def test_end_to_end_builds_an_unwrap_with_no_value_and_the_right_chain():
    kw, calls = _seams()
    out = G.evm_gas_topup_once(**kw)
    assert out["state"] == "topup" and out["status"] == "confirmed"
    tx = calls["signed"][0]
    assert tx["to"] == "0xWETH"           # unwrap targets the WETH contract, never a router
    assert tx["value"] == 0               # withdrawing native, not sending it
    assert tx["chainId"] == 4663
    assert tx["data"].startswith("0x2e1a7d4d")


def test_a_refusal_never_signs_anything():
    kw, calls = _seams(native_balance_fn=lambda: None)
    out = G.evm_gas_topup_once(**kw)
    assert out["act"] is False and out["state"] == "native_unreadable"
    assert calls["signed"] == [] and calls["sent"] == []


def test_send_without_a_hash_is_AMBIGUOUS_not_failed():
    """A missing hash means the unwrap may or may not have landed. Recording it as failed invites a
    retry, and a second unwrap on top of a landed first one is a double spend."""
    kw, _ = _seams(send_fn=lambda raw: None)
    out = G.evm_gas_topup_once(**kw)
    assert out["status"] == "submitted_unknown"
    assert out["state"] == "send_unconfirmed"


def test_the_signed_hash_is_journaled_before_the_send():
    order = []
    kw, calls = _seams(
        record_submission_fn=lambda plan: order.append(("record", plan["tx_hash"])),
        finish_fn=lambda plan: order.append(("finish", plan["status"])),
        send_fn=lambda raw: (order.append(("send", raw)), calls["sent"].append(raw), "0xhash")[2],
    )
    out = G.evm_gas_topup_once(**kw)
    assert out["status"] == "confirmed"
    assert order == [("record", "0xhash"), ("send", "0xsigned"), ("finish", "confirmed")]


def test_a_mismatched_send_hash_stays_unresolved():
    kw, calls = _seams(send_fn=lambda raw: (calls["sent"].append(raw), "0xother")[1])
    out = G.evm_gas_topup_once(**kw)
    assert out["status"] == "submitted_unknown"
    assert out["state"] == "send_hash_mismatch"
    assert calls["sent"] == ["0xsigned"]


def test_journal_failure_prevents_the_broadcast():
    def boom(_plan):
        raise RuntimeError("ledger down")
    kw, calls = _seams(record_submission_fn=boom)
    out = G.evm_gas_topup_once(**kw)
    assert out["state"] == "journal_failed"
    assert calls["sent"] == []


def _context(calls, *, native=int(0.0005 * E), confirm_once=lambda _h: "confirmed"):
    return {
        "chain": "4663",
        "address": "0xabc",
        "gas_topup": {
            "weth_token": "0xWETH",
            "rpc_url": "https://rpc",
            "native_balance_raw_fn": lambda: native,
            "weth_balance_raw_fn": lambda: int(0.05 * E),
            "nonce_fn": lambda: 12,
            "gas_price_fn": lambda: 1_000_000_000,
            "sign_fn": lambda tx: (calls["signed"].append(tx), ("0xsigned", "0xhash"))[1],
            "send_fn": lambda raw: (calls["sent"].append(raw), "0xhash")[1],
            "confirm_once_fn": confirm_once,
        },
    }


def test_runner_pass_records_one_confirmed_unwrap_at_the_target(tmp_path):
    """Deleting the runner call must fail this test. The planner alone cannot refill gas."""
    conn = _conn()
    calls = {"signed": [], "sent": []}
    out = G.run_evm_gas_topup_pass(
        conn, context=_context(calls), now_iso="2026-08-31T12:00:00+00:00",
        lock_dir=str(tmp_path), **RUN_BASE,
    )

    assert out["status"] == "confirmed"
    assert calls["sent"] == ["0xsigned"]
    rows = conn.execute(
        "SELECT native_amount_atomic, status FROM memecoin_evm_gas_topups"
    ).fetchall()
    assert [(row["native_amount_atomic"], row["status"]) for row in rows] == [
        ("2500000000000000", "confirmed")
    ]


def test_runner_pass_skips_when_a_prior_signed_unwrap_has_no_receipt(tmp_path):
    conn = _conn()
    m.record_evm_gas_topup_submission(
        conn, now_iso="2026-08-31T12:00:00+00:00", day="2026-08-31", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xprior", nonce=11,
    )
    calls = {"signed": [], "sent": []}
    out = G.run_evm_gas_topup_pass(
        conn, context=_context(calls, confirm_once=lambda _h: "unknown"),
        now_iso="2026-08-31T12:05:00+00:00", lock_dir=str(tmp_path), **RUN_BASE,
    )

    assert out["state"] == "unresolved"
    assert calls == {"signed": [], "sent": []}


def test_runner_pass_never_waits_for_an_entry_or_exit(tmp_path):
    conn = _conn()
    calls = {"signed": [], "sent": []}
    with wallet_exec_lock("evm-4663-0xabc", lock_dir=str(tmp_path), blocking=False) as held:
        assert held
        out = G.run_evm_gas_topup_pass(
            conn, context=_context(calls), now_iso="2026-08-31T12:00:00+00:00",
            lock_dir=str(tmp_path), **RUN_BASE,
        )

    assert out["state"] == "wallet_busy"
    assert calls == {"signed": [], "sent": []}


def test_loop_adapter_calls_only_the_robinhood_topup_with_the_approved_bounds():
    calls = {}

    def topup(conn, **kwargs):
        calls["conn"] = conn
        calls.update(kwargs)
        return {"state": "above_low_water"}

    contexts = {"4663": {"address": "0xabc", "chain": "4663"}, "8453": {}}
    result = runner_loop.run_evm_gas_topup_pass(
        object(), contexts=contexts, now_iso="2026-08-31T12:00:00+00:00", topup_fn=topup,
    )

    assert result == {"state": "above_low_water"}
    assert calls["context"] is contexts["4663"]
    assert calls["low_water_wei"] == 2_000_000_000_000_000
    assert calls["target_wei"] == 3_000_000_000_000_000
    assert calls["min_weth_reserve_wei"] == 10_000_000_000_000_000
    assert calls["daily_cap_wei"] == 0


# --- the reserve must yield while a position is trapped (2026-09-02) ----------------------------
#
# Live on 4663 this evening: native fell to 0.00051674 ETH against a 0.002 low-water mark and the
# top-up logged `weth_below_reserve` EIGHTY-THREE TIMES, once every 15s, while three funded bags sat
# un-exitable. The refusal is correct for entries and exactly wrong for exits — with a position
# open the gas is the only way the money comes OUT, and the lock is self-tightening because a
# reverted sell still burns gas (RIZZLER's burned 739,626).

from sniper.services.memecoin_evm_gas_topup import plan_gas_topup as _plan

_LOW, _TARGET, _RESERVE = 2_000_000_000_000_000, 3_000_000_000_000_000, 10_000_000_000_000_000
_NATIVE_STUCK = 516_737_446_218_251          # the balance measured during the live deadlock


def _p(**over):
    kw = dict(native_balance_wei=_NATIVE_STUCK, weth_balance_wei=_RESERVE // 2,
              low_water_wei=_LOW, target_wei=_TARGET, min_weth_reserve_wei=_RESERVE,
              daily_cap_wei=0, spent_today_wei=0, open_position_count=0)
    kw.update(over)
    return _plan(**kw)


def test_a_flat_book_keeps_the_reserve_absolute():
    """Nothing is trapped, so the original reasoning holds in full: do not convert the last of the
    bankroll into gas for trades it can no longer fund."""
    out = _p(open_position_count=0)
    assert out["act"] is False and out["state"] == "weth_below_reserve"


def test_an_open_position_releases_the_reserve():
    out = _p(open_position_count=1)
    assert out["act"] is True and out["state"] == "reserve_released_for_exit"
    assert out["amount_wei"] == _TARGET - _NATIVE_STUCK, "release exactly what a normal top-up would"


def test_the_release_never_exceeds_what_a_HEALTHY_topup_would_take():
    """Bound 2. It is a conditional release, not an override — it can never drain more than the
    ordinary path."""
    out = _p(open_position_count=5)
    assert out["amount_wei"] == _TARGET - _NATIVE_STUCK
    assert out["amount_wei"] < _RESERVE


def test_the_release_never_exceeds_the_WETH_actually_held():
    """Bound 3."""
    tiny = 1_000
    out = _p(open_position_count=1, weth_balance_wei=tiny)
    assert out["act"] is True and out["amount_wei"] == tiny


def test_zero_weth_cannot_release_anything():
    out = _p(open_position_count=1, weth_balance_wei=0)
    assert out["act"] is False and out["state"] == "weth_below_reserve"


def test_gas_above_the_low_water_mark_never_releases():
    """Bound 1 is not the only gate — the function still refuses before reaching the reserve at
    all when there is nothing to fix."""
    out = _p(open_position_count=3, native_balance_wei=_LOW + 1)
    assert out["act"] is False and out["state"] == "above_low_water"


def test_an_unreadable_balance_still_outranks_the_release():
    for k in ("native_balance_wei", "weth_balance_wei"):
        out = _p(open_position_count=1, **{k: None})
        assert out["act"] is False, f"{k}=None must never release"


def test_ample_weth_takes_the_ORDINARY_path_not_the_release():
    """The release must not shadow the healthy case — with WETH above the reserve this is a
    normal top-up and must be labelled as one."""
    out = _p(open_position_count=1, weth_balance_wei=_RESERVE * 3)
    assert out["act"] is True and out["state"] == "topup"


def _dead_ctx(calls, *, confirmed, pending, native=int(0.0005 * E)):
    ctx = _context(calls, native=native, confirm_once=lambda _h: "not_found")
    ctx["gas_topup"]["confirmed_nonce_fn"] = lambda: confirmed
    ctx["gas_topup"]["pending_nonce_fn"] = lambda: pending
    return ctx


def test_a_topup_the_chain_can_never_mine_is_released(tmp_path):
    """Row 42 (chain 4663, 2026-09-04) sat `submitted_unknown` at nonce 1432 while the account
    stood at 1435. Only `confirmed` and `failed` cleared this branch, so it blocked EVERY gas
    top-up on the chain. Gas is what an exit spends."""
    conn = _conn()
    m.record_evm_gas_topup_submission(
        conn, now_iso="2026-09-04T14:19:26+00:00", day="2026-09-04", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xdead", nonce=1432,
    )
    calls = {"signed": [], "sent": []}
    out = G.run_evm_gas_topup_pass(
        conn, context=_dead_ctx(calls, confirmed=1435, pending=1435),
        now_iso="2026-09-04T16:30:00+00:00", lock_dir=str(tmp_path), **RUN_BASE,
    )
    assert out["state"] == "recovered_never_admitted"
    assert m.has_unresolved_evm_gas_topup(conn, "4663") is False


def test_a_topup_still_at_its_own_nonce_stays_unresolved(tmp_path):
    # The account has NOT moved past it, so the transaction may still mine. Releasing here would
    # let a second top-up race a live one.
    conn = _conn()
    m.record_evm_gas_topup_submission(
        conn, now_iso="2026-09-04T14:19:26+00:00", day="2026-09-04", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xmaybe", nonce=1432,
    )
    calls = {"signed": [], "sent": []}
    out = G.run_evm_gas_topup_pass(
        conn, context=_dead_ctx(calls, confirmed=1432, pending=1433),
        now_iso="2026-09-04T16:30:00+00:00", lock_dir=str(tmp_path), **RUN_BASE,
    )
    assert out["state"] == "unresolved"
    assert calls == {"signed": [], "sent": []}


def test_a_missing_nonce_seam_proves_nothing_and_stays_unresolved(tmp_path):
    # Fail-closed: no proof available means the previous behaviour, not a release.
    conn = _conn()
    m.record_evm_gas_topup_submission(
        conn, now_iso="2026-09-04T14:19:26+00:00", day="2026-09-04", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xnoproof", nonce=1432,
    )
    calls = {"signed": [], "sent": []}
    out = G.run_evm_gas_topup_pass(
        conn, context=_context(calls, confirm_once=lambda _h: "not_found"),
        now_iso="2026-09-04T16:30:00+00:00", lock_dir=str(tmp_path), **RUN_BASE,
    )
    assert out["state"] == "unresolved"
    assert m.has_unresolved_evm_gas_topup(conn, "4663") is True


def test_an_unreadable_receipt_still_stays_unresolved(tmp_path):
    # `unknown` is a FAILED READ. It is not a node saying "no such transaction".
    conn = _conn()
    m.record_evm_gas_topup_submission(
        conn, now_iso="2026-09-04T14:19:26+00:00", day="2026-09-04", chain_id="4663",
        native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
        tx_hash="0xunread", nonce=1432,
    )
    calls = {"signed": [], "sent": []}
    ctx = _dead_ctx(calls, confirmed=1435, pending=1435)
    ctx["gas_topup"]["confirm_once_fn"] = lambda _h: "unknown"
    out = G.run_evm_gas_topup_pass(
        conn, context=ctx, now_iso="2026-09-04T16:30:00+00:00",
        lock_dir=str(tmp_path), **RUN_BASE,
    )
    assert out["state"] == "unresolved"
    assert m.has_unresolved_evm_gas_topup(conn, "4663") is True
