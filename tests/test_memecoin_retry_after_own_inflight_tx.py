"""A buy refused behind our own in-flight transaction is re-asked once that transaction resolves.

2026-09-28: three slot-lane buys reverted on min-out (404becc7, 0ae8a899, 9627edc1) while the J7
copy of the same source buy, 0.3 s apart, had been refused "unresolved signed transaction on
solana" by the leg's chain-wide guard. The slot transaction failed about 0.9 s later and no lane
re-asked. The refusal is now a retry-lane transient with its own short delay. The re-ask meets the
same guard, so it can never sign while the blocker is unresolved; a filled blocker on the same coin
ends it with "one position per coin".
"""
import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_leg as RL
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_risk import duplicate_refusal, risk_refusal_result
from test_memecoin_entry_retry_lane import _ev

NOW = "2026-09-28T05:02:40+00:00"
GUARD = "unresolved signed transaction on 4663"


@pytest.fixture(autouse=True)
def _lane(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _blocker(c):
    """Our own signed, unresolved buy on the chain (the slot lane's transaction)."""
    pid = m.reserve_pending(c, mint="0xslotcoin", source_user_id="u1", strategy="alpha_b",
                            notional=30.0, reserved_at=NOW, network_id="4663")
    m.set_pending_sig(c, pid, "0xslothash", NOW)
    return pid


def _hook(executed):
    """The real leg up to its guard; past the guard, a stub executor records that it was reached."""
    def hook(conn, ev):
        return RL.real_entry(
            conn, ev, day="2026-09-28", usdc_balance=300.0, sol_balance=0.0, now_iso=NOW,
            trade_size=30.0, token_safety_fn=lambda *a, **k: (True, {}),
            execute_fn=lambda *a, **k: {}, evm_allowlist={"4663"}, resolve_fn=lambda _m: "4663",
            evm_entry_fn=lambda *a, **k: executed.append(ev.fomo_event_id) or {"status": "filled"})
    return hook


def _refused_and_queued(c, executed, *, mint="0xslotcoin"):
    ev = replace(_ev(fid="j7-1", mint=mint), network_id="4663")
    first = _hook(executed)(c, ev)
    assert first == {"status": "rejected", "reason": GUARD}
    assert R._maybe_enqueue_retry(c, ev, first, NOW), "the guard refusal must queue a re-ask"
    return ev


def _due(c, at):
    return m.due_entry_retries(c, at, window_s=60, max_attempts=12)


@pytest.mark.parametrize("mint", ["0xslotcoin", "0xothercoin"])
def test_a_blocker_still_in_flight_waits_locally_or_rechecks_the_chain_fence(mint):
    c, executed = _ledger(), []
    _blocker(c)
    _refused_and_queued(c, executed, mint=mint)
    before = dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone())
    assert not _due(c, "2026-09-28T05:02:40.900000+00:00") and _due(c, "2026-09-28T05:02:41+00:00")
    R.run_entry_retry_lane(c, _hook(executed), "2026-09-28T05:02:41+00:00")
    assert executed == [], "a re-ask signed beside an unresolved transaction"
    if mint == "0xslotcoin":
        assert dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone()) == before
    else:
        assert not _due(c, "2026-09-28T05:02:41.900000+00:00") and _due(c, "2026-09-28T05:02:42+00:00")


def test_a_blocker_that_failed_lets_the_reask_through():
    c, executed = _ledger(), []
    pid = _blocker(c)
    _refused_and_queued(c, executed)
    m.release_pending(c, pid, "chain_error:BuySlippageBelowMinBaseAmountOut")
    out = R.run_entry_retry_lane(c, _hook(executed), "2026-09-28T05:02:41+00:00")
    assert executed == ["j7-1"] and out["filled"] == 1
    assert not _due(c, "2026-09-28T05:03:00+00:00"), "a filled re-ask stays queued"


def test_a_blocker_that_filled_the_same_coin_ends_the_reask():
    c, executed = _ledger(), []
    pid = _blocker(c)
    ev = _refused_and_queued(c, executed)
    m.open_position(c, source_user_id="u1", source_handle="owner", mint=ev.mint, ticker="SHI",
                    entry_price=ev.their_price, entry_notional=30.0, entry_fill_source="chain",
                    entry_lag_s=1.0, their_size_usd=1000.0, opened_at=NOW, strategy="alpha_b",
                    execution_mode="real", network_id="4663")
    assert m.confirm_pending(c, pid)
    one_per_coin = risk_refusal_result(duplicate_refusal({"kind": "position", "id": 1}))
    assert one_per_coin["reason"] == "one position per coin: open funded position"
    R.run_entry_retry_lane(c, lambda conn, e: one_per_coin, "2026-09-28T05:02:41+00:00")
    assert not _due(c, "2026-09-28T05:03:00+00:00"), "a coin we now hold is re-asked again"
    assert ev.fomo_event_id == "j7-1"


def test_the_knob_at_zero_keeps_the_refusal_one_shot(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 0.0)
    assert not R._is_transient_entry_miss({"status": "rejected", "reason": GUARD})


def test_the_prefix_is_the_legs_own_words():
    import inspect
    assert '"unresolved signed transaction on %s"' in inspect.getsource(RL.real_entry)
    assert R._UNRESOLVED_SIGNED_PREFIX == "unresolved signed transaction on "
