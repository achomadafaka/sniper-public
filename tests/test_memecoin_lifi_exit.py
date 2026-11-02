import sqlite3
from types import SimpleNamespace

from eth_account import Account

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution.evm_exit_executor import execute_exit_evm, execute_partial_exit_evm
from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.execution.exit_preview import PreviewResult
from sniper.execution.lifi_executor import execute_lifi_exit
from sniper.persistence import db_memecoin as m


NOW = "2026-08-30T20:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn, *, dependent=True):
    position_id = m.open_position(
        conn, source_user_id="wallet", source_handle="wallet", mint="0xtoken", ticker="T",
        entry_price=1, entry_notional=10, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=10, opened_at=NOW, strategy="control", execution_mode="real",
        network_id="4663", lifi_dependent=dependent,
    )
    return m.get_position(conn, position_id)


def _seams(quote_fn):
    return {
        "quote_fn": quote_fn,
        "approve_fn": lambda *_args: True,
        "sign_fn": lambda *_args: ("0xraw", "0xhash", 1),
        "send_fn": lambda *_args: True,
        "confirm_fn": lambda *_args: "confirmed",
        "usdc_fn": iter([10, 20]).__next__,
        "token_balance_fn": lambda _mint: 10,
    }


def test_full_exit_uses_lifi_only_after_exact_direct_no_route():
    conn = _conn()
    position = _position(conn)
    calls = []

    result = execute_exit_evm(
        conn, position, "manual", NOW,
        slippage_ladder=[3, 10],
        quote_miss_fn=lambda _slip: {"code": "no_route"},
        lifi_quote_fn=lambda **kwargs: calls.append(kwargs) or {
            "status": "unknown", "reason": "lifi_submitted_unknown",
        },
        **_seams(lambda *_args: None),
    )

    assert result["status"] == "unknown"
    assert len(calls) == 1
    assert calls[0]["claim_id"]
    assert calls[0]["fraction"] == 1.0


def test_direct_success_and_provider_fault_cause_zero_lifi_calls():
    quote = {
        "sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
        "transaction": {"to": "0xrouter"},
    }
    conn = _conn()
    calls = []
    result = execute_exit_evm(
        conn, _position(conn, dependent=False), "manual", NOW, slippage_ladder=[3],
        quote_miss_fn=lambda _slip: {"code": "no_route"},
        lifi_quote_fn=lambda **kwargs: calls.append(kwargs),
        **_seams(lambda *_args: quote),
    )
    assert result["status"] == "closed"
    assert calls == []

    conn = _conn()
    result = execute_exit_evm(
        conn, _position(conn), "manual", NOW, slippage_ladder=[3],
        quote_miss_fn=lambda _slip: {"code": "provider_rate_limit"},
        lifi_quote_fn=lambda **kwargs: calls.append(kwargs),
        **_seams(lambda *_args: None),
    )
    assert result["status"] == "deferred"
    assert calls == []
    assert not m.get_position(conn, 1)["stuck_bag"]


def test_lifi_dependent_terminal_close_uses_atomic_finalizer():
    conn = _conn()
    position = _position(conn)
    calls = []
    quote = {
        "sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
        "transaction": {"to": "0xrouter"},
    }

    result = execute_exit_evm(
        conn, position, "manual", NOW, slippage_ladder=[3],
        terminal_finalize_fn=lambda kwargs: calls.append(kwargs) or m.book_full_exit_and_finalize(
            conn, **kwargs,
        ),
        **_seams(lambda *_args: quote),
    )

    assert result["status"] == "closed"
    assert len(calls) == 1
    assert m.get_position(conn, position["id"])["is_open"] == 0


def test_partial_lifi_exit_keeps_the_position_and_slot():
    conn = _conn()
    position = _position(conn)
    result = execute_partial_exit_evm(
        conn, position, 0.5, NOW, slippage_ladder=[3],
        quote_miss_fn=lambda _slip: {"code": "no_route"},
        lifi_quote_fn=lambda **_kwargs: {"status": "derisked", "received": 5},
        **_seams(lambda *_args: None),
    )

    assert result["status"] == "derisked"
    assert m.get_position(conn, position["id"])["is_open"] == 1
    assert m.get_position(conn, position["id"])["lifi_dependent"] == 1


def test_lifi_exit_advances_only_typed_slippage_failures():
    calls = []
    results = iter([
        SimpleNamespace(ok=True, failure_type=None),
        SimpleNamespace(ok=True, failure_type=None),
    ])

    result = execute_lifi_exit(
        slippage_ladder=[3, 10, 25],
        quote_fn=lambda rung: calls.append(rung) or next(results),
        execute_fn=lambda _quote, rung: {
            "status": "failed", "failure_type": "minimum_output"
        } if rung == 3 else {"status": "closed"},
        freshness_fn=lambda _quote: True,
        cleanup_fn=lambda _reason: None,
    )
    assert result["status"] == "closed"
    assert calls == [3, 10]

    for failure in ("no_route", "timeout", "rate_limited", "provider_error"):
        calls.clear()
        result = execute_lifi_exit(
            slippage_ladder=[3, 10, 25],
            quote_fn=lambda rung, failure=failure: calls.append(rung) or SimpleNamespace(
                ok=False, failure_type=failure,
            ),
            execute_fn=lambda *_args: None,
            freshness_fn=lambda _quote: True,
            cleanup_fn=lambda _reason: None,
        )
        assert result["failure_type"] == failure
        assert calls == [3]


def test_stale_lifi_exit_starts_cleanup_without_another_quote():
    calls = []
    cleanup = []
    result = execute_lifi_exit(
        slippage_ladder=[3, 10],
        quote_fn=lambda rung: calls.append(rung) or SimpleNamespace(ok=True, failure_type=None),
        execute_fn=lambda *_args: None,
        freshness_fn=lambda _quote: False,
        cleanup_fn=cleanup.append,
    )

    assert result["reason"] == "stale_quote"
    assert calls == [3]
    assert cleanup == ["stale_quote"]


def test_preview_uses_lifi_only_after_direct_no_route(monkeypatch):
    from sniper.execution import evm_wallet
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_args, **_kwargs: 123)
    calls = []
    expected = PreviewResult.failed(QuoteFailure.INSUFFICIENT_LIQUIDITY)
    executor = build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        venue_quote_fn=lambda *_args, **_kwargs: None,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
        lifi_preview_fn=lambda **kwargs: calls.append(kwargs) or expected,
    )

    result = executor["preview_exit_fn"]({"mint": "0xtoken", "lifi_dependent": 1})

    assert result is expected
    assert len(calls) == 1
    assert calls[0]["amount_atomic"] == 123
    assert calls[0]["direct_miss"]["code"] == "no_route"


def test_preview_provider_fault_does_not_call_lifi(monkeypatch):
    from sniper.execution import evm_wallet

    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_args, **_kwargs: 123)
    calls = []
    executor = build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        venue_quote_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
        lifi_preview_fn=lambda **kwargs: calls.append(kwargs),
    )

    result = executor["preview_exit_fn"]({"mint": "0xtoken", "lifi_dependent": 1})

    assert result.failure == QuoteFailure.PROVIDER_TIMEOUT
    assert calls == []
