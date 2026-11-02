"""Tests for strategies/shadow_copy_v1.py — paper-only shadow-copy plugin."""
from __future__ import annotations

import json
import sqlite3
import time
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import init_db  # noqa: E402
from sniper.strategies.base import StrategySignal  # noqa: E402
from sniper.strategies.shadow_copy_v1 import (  # noqa: E402
    PLUGIN_ID,
    ShadowCopyV1Strategy,
    _already_copied,
    _matches_market_filter,
    _open_position_count,
    _parse_params,
    ensure_shadow_copy_pending_signals_table,
    insert_pending_signal,
    mark_pending_processed,
    mark_pending_rejected,
)
from sniper.strategies.registry import get_strategy_plugin  # noqa: E402


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    # The plugin's max_concurrent gate reads from strategy_positions (created
    # by db.init_db). _already_copied reads only from shadow_copy_pending_signals,
    # so we don't need paper_trader's schema in tests.
    ensure_shadow_copy_pending_signals_table(c)
    yield c
    c.close()


def _swissmiss_grain_params(target_wallets: list[str] | None = None) -> dict:
    """Strategy params matching the cross-wallet promoted thesis.

    `target_wallets=[]` is meaningful — caller wants the empty-list path.
    Don't collapse with `or`, which converts `[]` to the default.
    """
    if target_wallets is None:
        target_wallets = [
            "0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
            "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
        ]
    return {
        "target_wallets": target_wallets,
        "market_filter": {
            "categories": ["sports"],
            "market_types": ["binary"],
            "outcome_roles": ["binary_named_outcome"],
            "entry_price_range": [0.50, 0.75],
        },
        "max_position_size_usdc": 25.0,
        "min_position_size_usdc": 5.0,
        "max_concurrent": 5,
        "min_wallet_position_size_usdc": 100.0,
        "max_observation_age_minutes": 30,
    }


def _insert_obs(
    conn,
    *,
    wallet: str = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
    condition_id: str = "0xcond1",
    asset_id: str = "asset1",
    outcome: str = "Yes",
    side: str = "BUY",
    price: float = 0.60,
    size_usdc: float = 500.0,
    market_category: str = "sports",
    market_type: str = "binary",
    outcome_role: str = "binary_named_outcome",
    title: str = "Test sports match",
    observed_ts: int | None = None,
) -> int | None:
    return insert_pending_signal(
        conn,
        wallet=wallet,
        condition_id=condition_id,
        asset_id=asset_id,
        outcome=outcome,
        side=side,
        price=price,
        size_usdc=size_usdc,
        market_category=market_category,
        market_type=market_type,
        outcome_role=outcome_role,
        title=title,
        observed_ts=observed_ts if observed_ts is not None else int(time.time()),
    )


# ---------------------------------------------------------------------------
# Schema + persistence helpers
# ---------------------------------------------------------------------------


def test_ensure_table_idempotent(conn):
    # Calling twice must not raise
    ensure_shadow_copy_pending_signals_table(conn)
    ensure_shadow_copy_pending_signals_table(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_copy_pending_signals'"
    ).fetchone()
    assert row is not None


def test_insert_pending_signal_returns_rowid(conn):
    rid = _insert_obs(conn)
    assert rid is not None
    assert rid > 0
    row = conn.execute(
        "SELECT status FROM shadow_copy_pending_signals WHERE id=?", (rid,)
    ).fetchone()
    assert row["status"] == "pending"


def test_insert_pending_signal_dedup_returns_none(conn):
    obs_ts = int(time.time())
    first = _insert_obs(conn, observed_ts=obs_ts)
    assert first is not None
    # Same (wallet, condition_id, asset_id, observed_ts, side) → dedup
    second = _insert_obs(conn, observed_ts=obs_ts)
    assert second is None


def test_mark_pending_processed_and_rejected(conn):
    rid = _insert_obs(conn)
    mark_pending_processed(conn, rid, strategy_id=42)
    row = conn.execute(
        "SELECT status, strategy_id FROM shadow_copy_pending_signals WHERE id=?",
        (rid,),
    ).fetchone()
    assert row["status"] == "processed"
    assert row["strategy_id"] == 42

    rid2 = _insert_obs(conn, condition_id="0xcond2")
    mark_pending_rejected(conn, rid2, reason="filter:something")
    row2 = conn.execute(
        "SELECT status, reject_reason FROM shadow_copy_pending_signals WHERE id=?",
        (rid2,),
    ).fetchone()
    assert row2["status"] == "rejected"
    assert "filter:" in row2["reject_reason"]


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------


def test_matches_market_filter_category():
    p = _swissmiss_grain_params()
    row = {"market_category": "sports", "market_type": "binary", "outcome_role": "binary_named_outcome", "price": 0.6}
    passes, _ = _matches_market_filter(row, p["market_filter"])
    assert passes

    row_bad = dict(row, market_category="elections")
    passes, reason = _matches_market_filter(row_bad, p["market_filter"])
    assert not passes
    assert "market_category" in reason


def test_matches_market_filter_price_range():
    p = _swissmiss_grain_params()
    # Within range
    row_in = {"market_category": "sports", "market_type": "binary", "outcome_role": "binary_named_outcome", "price": 0.60}
    passes, _ = _matches_market_filter(row_in, p["market_filter"])
    assert passes

    # Below
    row_below = dict(row_in, price=0.40)
    passes, reason = _matches_market_filter(row_below, p["market_filter"])
    assert not passes
    assert "outside" in reason

    # At upper bound — exclusive
    row_hi = dict(row_in, price=0.75)
    passes, _ = _matches_market_filter(row_hi, p["market_filter"])
    assert not passes


def test_matches_market_filter_empty_filter_passes_everything():
    row = {"market_category": "anything", "market_type": "anything", "outcome_role": "anything", "price": 0.5}
    passes, _ = _matches_market_filter(row, {})
    assert passes


# ---------------------------------------------------------------------------
# discover() — full plugin flow
# ---------------------------------------------------------------------------


def _make_strategy_row(*, id=1, params=None, wallet="0xshadowcopy"):
    return {
        "id": id,
        "wallet": wallet,
        "label": "ShadowCopyTest",
        "strategy_type": PLUGIN_ID,
        "params": params or _swissmiss_grain_params(),
        "status": "active",
    }


def test_discover_passes_swissmiss_buy_in_grain(conn):
    _insert_obs(conn)
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    signals = plugin.discover(conn, strategy)
    assert len(signals) == 1
    s = signals[0]
    assert s.wallet == "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"
    assert s.condition_id == "0xcond1"
    assert s.current_price == 0.60
    assert s.metadata["market_category"] == "sports"


def test_discover_rejects_off_grain_category(conn):
    _insert_obs(conn, market_category="elections")
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []
    # Verify rejection persisted
    row = conn.execute(
        "SELECT status, reject_reason FROM shadow_copy_pending_signals"
    ).fetchone()
    assert row["status"] == "rejected"
    assert "market_category" in row["reject_reason"]


def test_discover_rejects_off_grain_price_range(conn):
    _insert_obs(conn, price=0.40)
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []


def test_discover_rejects_sell_side(conn):
    _insert_obs(conn, side="SELL")
    plugin = ShadowCopyV1Strategy()
    # SELL rows filtered before any rejection logic runs (SQL WHERE side='BUY')
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []
    # Sell row stays pending — not our concern
    row = conn.execute("SELECT status FROM shadow_copy_pending_signals").fetchone()
    assert row["status"] == "pending"


def test_discover_rejects_off_whitelist_wallet(conn):
    _insert_obs(conn, wallet="0xrandom_wallet")
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []


def test_discover_rejects_old_observation(conn):
    old_ts = int(time.time()) - 86400  # 1 day old
    _insert_obs(conn, observed_ts=old_ts)
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []
    row = conn.execute(
        "SELECT status, reject_reason FROM shadow_copy_pending_signals"
    ).fetchone()
    assert row["status"] == "rejected"
    assert "observation_too_old" in row["reject_reason"]


def test_discover_rejects_small_wallet_position(conn):
    _insert_obs(conn, size_usdc=50.0)
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []
    row = conn.execute(
        "SELECT status, reject_reason FROM shadow_copy_pending_signals"
    ).fetchone()
    assert "wallet_position_below_min" in row["reject_reason"]


def test_discover_empty_target_wallets_returns_empty(conn):
    _insert_obs(conn)
    plugin = ShadowCopyV1Strategy()
    params = _swissmiss_grain_params(target_wallets=[])
    signals = plugin.discover(conn, _make_strategy_row(params=params))
    assert signals == []


def test_discover_dedup_already_copied(conn):
    # First pass: emit + mark processed
    rid_first = _insert_obs(conn, observed_ts=int(time.time()) - 60)
    mark_pending_processed(conn, rid_first, strategy_id=1)
    # Second pass: insert a new pending row for the SAME (wallet, condition, asset)
    # at a slightly later observed_ts — should be rejected as already_copied.
    rid_second = _insert_obs(conn, observed_ts=int(time.time()))
    assert rid_second is not None
    plugin = ShadowCopyV1Strategy()
    signals = plugin.discover(conn, _make_strategy_row())
    assert signals == []
    row = conn.execute(
        "SELECT status, reject_reason FROM shadow_copy_pending_signals WHERE id=?",
        (rid_second,),
    ).fetchone()
    assert row["status"] == "rejected"
    assert "already_copied" in row["reject_reason"]


# ---------------------------------------------------------------------------
# enter() decisions
# ---------------------------------------------------------------------------


def test_enter_returns_paper_decision_with_bounded_size(conn):
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    signal = StrategySignal(
        wallet="0xtest",
        condition_id="0xcond",
        outcome="YES",
        title="x",
        current_price=0.60,
        position_value=500.0,
        analyzed_ts=int(time.time()),
        metadata={"pending_id": 1},
    )
    decision = plugin.enter(conn, strategy, signal, [])
    assert decision.should_enter is True
    assert decision.execution_mode == "paper"
    assert decision.size_usdc <= 25.0
    assert decision.size_usdc >= 5.0


def test_enter_rejects_when_max_concurrent_reached(conn):
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    strategy["params"]["max_concurrent"] = 1
    # Pre-insert one open strategy_position for this strategy_id
    conn.execute(
        """
        INSERT INTO strategy_positions
          (wallet, condition_id, outcome, strategy_id, state, opened_ts, updated_ts)
        VALUES ('0xa', '0xc', 'YES', '1', 'open', ?, ?)
        """,
        (int(time.time()), int(time.time())),
    )
    conn.commit()
    signal = StrategySignal(
        wallet="0xtest", condition_id="0xother", outcome="YES",
        title="x", current_price=0.6, position_value=200.0,
        analyzed_ts=int(time.time()),
    )
    decision = plugin.enter(conn, strategy, signal, [])
    assert decision.should_enter is False
    assert "max_concurrent" in decision.reason


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_plugin_is_registered():
    plugin = get_strategy_plugin(PLUGIN_ID)
    assert plugin is not None
    assert isinstance(plugin, ShadowCopyV1Strategy)


# ---------------------------------------------------------------------------
# Parse params
# ---------------------------------------------------------------------------


def test_parse_params_merges_defaults():
    raw = {"target_wallets": ["0xa"]}
    merged = _parse_params({"params": raw})
    assert merged["target_wallets"] == ["0xa"]
    assert merged["max_position_size_usdc"] == 25.0  # default


def test_parse_params_accepts_json_string():
    raw_json = json.dumps({"target_wallets": ["0xa"], "max_concurrent": 10})
    merged = _parse_params({"params": raw_json})
    assert merged["target_wallets"] == ["0xa"]
    assert merged["max_concurrent"] == 10


def test_parse_params_market_filter_deep_merges():
    raw = {"market_filter": {"categories": ["finance"]}}
    merged = _parse_params({"params": raw})
    assert merged["market_filter"]["categories"] == ["finance"]
    # Other fields fall back to default (empty)
    assert "market_types" in merged["market_filter"]


# ---------------------------------------------------------------------------
# exit_strategy()
# ---------------------------------------------------------------------------


def test_exit_returns_resolution_win_at_threshold():
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    position = {"current_price": 0.99}
    decision = plugin.exit(None, strategy, position, None)
    assert decision.should_exit is True
    assert decision.status == "resolution_win"


def test_exit_returns_resolution_loss_at_threshold():
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    position = {"current_price": 0.01}
    decision = plugin.exit(None, strategy, position, None)
    assert decision.should_exit is True
    assert decision.status == "resolution_loss"


def test_exit_holds_in_middle():
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    position = {"current_price": 0.50, "opened_at_ts": int(time.time()) - 86400}
    decision = plugin.exit(None, strategy, position, None)
    assert decision.should_exit is False


def test_exit_at_max_hold_days():
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    # Override max_hold_days to small value for the test
    strategy["params"]["max_hold_days"] = 0.01  # ~15 minutes
    very_old = int(time.time()) - 86400
    position = {"current_price": 0.50, "opened_at_ts": very_old}
    decision = plugin.exit(None, strategy, position, None)
    assert decision.should_exit is True
    assert decision.status == "early_exit"


# ---------------------------------------------------------------------------
# Seam-bug regressions (2026-05-29) — ABC alignment, dedup, opened_ts, poller
# ---------------------------------------------------------------------------


def test_enter_signature_matches_base_abc():
    """B1: enter() must match StrategyPlugin.enter so the runner call site
    enter(conn, strategy, signal, open_positions) doesn't TypeError."""
    import inspect
    from sniper.strategies.base import StrategyPlugin
    abc_params = list(inspect.signature(StrategyPlugin.enter).parameters)
    plugin_params = list(inspect.signature(ShadowCopyV1Strategy.enter).parameters)
    assert plugin_params == abc_params, (plugin_params, abc_params)


def test_exit_signature_matches_base_abc():
    """exit() must match StrategyPlugin.exit (conn, strategy, position, state)."""
    import inspect
    from sniper.strategies.base import StrategyPlugin
    abc_params = list(inspect.signature(StrategyPlugin.exit).parameters)
    plugin_params = list(inspect.signature(ShadowCopyV1Strategy.exit).parameters)
    assert plugin_params == abc_params, (plugin_params, abc_params)


def test_enter_marks_pending_processed_so_next_discover_dedups(conn):
    """B2: enter() on a real signal marks the source pending row processed, so a
    later re-buy of the same (wallet, condition, asset) is deduped on discover."""
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()  # id=1
    rid = _insert_obs(conn, observed_ts=int(time.time()) - 60)
    sigs = plugin.discover(conn, strategy)
    assert len(sigs) == 1 and sigs[0].metadata["pending_id"] == rid

    decision = plugin.enter(conn, strategy, sigs[0], [])
    assert decision.should_enter is True
    row = conn.execute(
        "SELECT status, strategy_id FROM shadow_copy_pending_signals WHERE id=?", (rid,)
    ).fetchone()
    assert row["status"] == "processed"
    assert row["strategy_id"] == 1

    # A fresh observation for the same market is now deduped.
    _insert_obs(conn, observed_ts=int(time.time()))
    assert plugin.discover(conn, strategy) == []


def test_exit_max_hold_uses_opened_ts():
    """B4: max_hold gate reads the real strategy_positions column `opened_ts`."""
    plugin = ShadowCopyV1Strategy()
    strategy = _make_strategy_row()
    strategy["params"]["max_hold_days"] = 0.01
    position = {"current_price": 0.50, "opened_ts": int(time.time()) - 86400}
    decision = plugin.exit(None, strategy, position, None)
    assert decision.should_exit is True
    assert decision.status == "early_exit"


def test_poller_calls_fetch_user_activity_with_valid_signature(monkeypatch):
    """B3: _fetch_wallet_activity must call fetch_user_activity(session, wallet,
    start_ts=...) — the real signature — not kwargs that match no overload."""
    import scripts.poll_shadow_copy_targets as mod
    import sniper.markets.polymarket_api as pmapi
    captured = {}

    def fake_fetch(session, wallet, *args, **kwargs):
        captured["wallet"] = wallet
        captured["start_ts"] = kwargs.get("start_ts")
        return [{"timestamp": 2_000_000_000, "conditionId": "0xc", "asset": "1"}]

    monkeypatch.setattr(pmapi, "fetch_user_activity", fake_fetch)
    out = mod._fetch_wallet_activity("0x" + "a" * 40, 1_900_000_000)
    assert captured["wallet"] == "0x" + "a" * 40
    assert captured["start_ts"] == 1_900_000_000
    assert isinstance(out, list)
