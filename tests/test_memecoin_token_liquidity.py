"""Per-token liquidity store (2026-07-18): the runner captures GT's reserve_in_usd (from the pool
discovery it already makes) and persists it here; the web display reads it because GT is otherwise
unreachable from the web (the runner saturates the per-IP rate limit).
"""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_token_liquidity_store_roundtrip_upsert_and_staleness():
    c = _c()
    m.set_token_liquidity(c, "4663", "0xabc", 125000.0, "2026-07-18T10:00:00+00:00")
    assert m.get_token_liquidity(c, "4663", "0xabc", now_iso="2026-07-18T10:05:00+00:00") == 125000.0
    # upsert replaces
    m.set_token_liquidity(c, "4663", "0xabc", 99000.0, "2026-07-18T10:10:00+00:00")
    assert m.get_token_liquidity(c, "4663", "0xabc", now_iso="2026-07-18T10:11:00+00:00") == 99000.0
    # older than max_age -> dropped
    assert m.get_token_liquidity(c, "4663", "0xabc",
                                 now_iso="2026-07-18T13:00:00+00:00", max_age_s=7200) is None
    # missing / zero / negative -> None, never a phantom $0
    assert m.get_token_liquidity(c, "4663", "0xmissing") is None
    m.set_token_liquidity(c, "4663", "0xz", 0.0, "2026-07-18T10:00:00+00:00")
    assert m.get_token_liquidity(c, "4663", "0xz") is None


def test_real_exit_sweep_persists_4663_liquidity(monkeypatch):
    # the runner-write: after the exit hooks, capture the deepest pool's reserve (from the discovery it
    # already makes) and persist it for the display.
    import sniper.execution.evm_v4_quote as V4
    monkeypatch.setattr(V4, "_gt_pools_for_mint", lambda *a, **k: [])           # warm (no-op here)
    monkeypatch.setattr(V4, "top_pool_reserve_usd", lambda chain, mint, **k: 87000.0)
    c = _c()
    m.open_position(c, source_user_id="u1", source_handle="h", mint="0xrobin", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="4663")
    rc = {"exit_fn": lambda *a, **k: {"status": "open"},
          "preview_exit_fn": lambda pos: {"expected_usdc": 9.0}}   # synthetic mid -10% -> no exit
    R.real_exit_sweep(c, {"4663": rc}, price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert m.get_token_liquidity(c, "4663", "0xrobin", now_iso="2026-07-09T00:06:00Z") == 87000.0


def test_real_exit_sweep_skips_non_4663_liquidity(monkeypatch):
    import sniper.execution.evm_v4_quote as V4
    called = []
    monkeypatch.setattr(V4, "_gt_pools_for_mint", lambda chain, mint, **k: called.append(mint) or [])
    monkeypatch.setattr(V4, "top_pool_reserve_usd", lambda *a, **k: 50000.0)
    c = _c()
    m.open_position(c, source_user_id="u1", source_handle="h", mint="SolMint", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="solana")
    R.real_exit_sweep(c, {"solana": {"exit_fn": lambda *a, **k: {"status": "open"}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert called == []                                            # only 4663 goes through the GT capture
