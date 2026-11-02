"""4663 (Robinhood) exit price oracle (2026-07-12). DexScreener has no pair for Robinhood-chain
tokens (100% missing_pair), so the real exit sweep gets no `mid` and the price rails (TP/derisk/stop)
could never nominate — a funded 4663 position could only leave via mirror-on-sell or the 24h
unquotable timer. Fix: when a feed price is ABSENT, `real_exit_sweep` derives an executable unit
price from the venue's exact-balance sell quote divided by remaining open quantity."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_4663(c, mint="0xRobinToken", entry=1.0, notional=10.0):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                          entry_price=entry, entry_notional=notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-12T00:00:00Z",
                          strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                          network_id="4663")
    qty = notional / entry
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=qty, notional=notional,
                  fill_source="chain", fraction=1.0, created_at="2026-07-12T00:00:00Z")
    return pid


def _open_solana(c, mint="SoLHealthy", entry=1.0, notional=10.0):
    return m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                           entry_price=entry, entry_notional=notional, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-12T00:00:00Z",
                           strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                           network_id="solana")


def test_4663_take_profit_fires_via_preview_when_unpriced(monkeypatch):
    """No DexScreener price for the 4663 mint, but the 0x sell quote shows +60% -> real_tp fires."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    calls = []
    preview_calls = {"n": 0}

    def preview_exit_fn(pos):
        preview_calls["n"] += 1
        return {"expected_usdc": 16.0}          # $16 / 10 remaining tokens = 1.60 exact unit price

    ctx = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                      {"status": "closed"})[1],
           "preview_exit_fn": preview_exit_fn}
    # feed price map returns NOTHING for the 4663 mint (simulates missing_pair)
    R.real_exit_sweep(c, {"4663": ctx}, price_fn=lambda mints: {},
                      now_fn=lambda: "2026-07-12T00:05:00Z")
    assert calls == [("0xRobinToken", "real_tp")]     # TP nominated + confirmed off the 0x quote
    assert preview_calls["n"] == 1                     # exactly one 0x quote (nominate = confirm reuse)


def test_4663_unpriced_down_position_does_not_false_stop(monkeypatch):
    """4663 stop is deliberately 0 (WETH launch tokens dip and recover). Even with a derivable
    -60% mid from the 0x quote, no stop fires — the price-fallback must not resurrect a stop the
    chain policy disabled."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)   # global stop on, but 4663 override=0
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    calls = []
    ctx = {"exit_fn": lambda conn, pos, reason, now: (calls.append((pos["mint"], reason)),
                                                      {"status": "closed"})[1],
           "preview_exit_fn": lambda pos: {"expected_usdc": 4.0}}   # -60%
    R.real_exit_sweep(c, {"4663": ctx}, price_fn=lambda mints: {},
                      now_fn=lambda: "2026-07-12T00:05:00Z")
    assert calls == []                                # no stop on 4663; position rides


def test_priced_solana_position_never_calls_preview_fallback(monkeypatch):
    """When the feed HAS a price (Solana always does), the fallback must not run — a healthy priced
    position consults the 0x quote zero times (no wasted calls, Solana path unchanged)."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    from sniper.execution.solana_quote import norm_mint
    c = _c()
    _open_solana(c, mint="SoLHealthy", entry=1.0)
    preview_calls = {"n": 0}
    ctx = {"exit_fn": lambda conn, pos, reason, now: {"status": "closed"},
           "preview_exit_fn": lambda pos: (preview_calls.__setitem__("n", preview_calls["n"] + 1),
                                           {"expected_usdc": 4.0})[1]}
    pmap = {norm_mint("SoLHealthy"): {"price": 1.1, "liquidity": 1e9}}   # +10%, healthy
    R.real_exit_sweep(c, {"solana": ctx}, price_fn=lambda mints: pmap,
                      now_fn=lambda: "2026-07-12T00:05:00Z")
    assert preview_calls["n"] == 0                     # feed price present -> no fallback, no exit


# --- the confirmed quote must reach the executor (2026-09-02) ---------------------------------
# `_confirmed_exit_preview` was handed over only for _STOP_DEBOUNCE_REASONS (b_stop, catastrophe),
# added by 2ed78097 "Fix EVM exit quote capacity". But the preview is bought for EVERY reason in
# _EXECUTABLE_PRICE_REASONS, which includes real_tp, real_trail and peak_stall. Those dropped it and
# the executor re-quoted the same bag inside its slippage ladder, so a take-profit paid for two
# route quotes and used one -- on the money path, while the position is trying to sell.

def test_take_profit_hands_its_confirmed_quote_to_the_executor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    quote = {"expected_usdc": 16.0}
    seen = {}

    def exit_fn(conn, pos, reason, now):
        seen["reason"] = reason
        seen["preview"] = pos.get("_confirmed_exit_preview")
        return {"status": "closed"}

    R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn,
                                   "preview_exit_fn": lambda pos: quote}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")

    assert seen["reason"] == "real_tp"
    # the SAME object the take-profit was confirmed against, not a re-fetch
    assert seen["preview"] is quote


def test_the_handoff_is_scoped_to_the_call_and_does_not_leak(monkeypatch):
    """It is popped in a `finally`, so a position dict never carries a stale quote to a later tick."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    captured = {}

    def exit_fn(conn, pos, reason, now):
        captured["pos"] = pos
        assert pos.get("_confirmed_exit_preview") is not None
        raise RuntimeError("executor blew up mid-exit")

    try:
        R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn,
                                       "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                          price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    except RuntimeError:
        pass
    assert "_confirmed_exit_preview" not in captured["pos"]


def test_the_preview_quote_is_timed_and_charged_to_the_exit(monkeypatch):
    """The stage map could not see the quote that DECIDES the exit.

    The executor's clock opens when execute_exit_evm is entered, but the preview is bought earlier
    in the sweep. So `total_ms` is executor-entry-to-closed, not decision-to-closed. `preview_ms`
    is accumulated on the position so the seam can report the honest number.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(R.time, "monotonic", lambda: clock["t"])

    def slow_preview(pos):
        clock["t"] += 1.25                       # a 1250ms route quote
        return {"expected_usdc": 16.0}

    seen = {}

    def exit_fn(conn, pos, reason, now):
        seen["preview_ms"] = pos.get("_exit_preview_ms")
        return {"status": "closed"}

    R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn, "preview_exit_fn": slow_preview}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")

    assert seen["preview_ms"] == pytest.approx(1250.0, abs=1.0)


def test_preview_timing_does_not_leak_to_a_later_tick(monkeypatch):
    """Cleared with _confirmed_exit_preview, so the next exit is not charged for this one."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    captured = {}

    def exit_fn(conn, pos, reason, now):
        captured["pos"] = pos
        return {"status": "closed"}

    R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn,
                                   "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    assert "_exit_preview_ms" not in captured["pos"]


def test_a_position_that_buys_its_own_preview_is_labelled_own(monkeypatch):
    """`preview_ms=0` must never be ambiguous between free and unmeasured."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)
    seen = {}

    def exit_fn(conn, pos, reason, now):
        seen["src"] = pos.get("_exit_preview_src")
        seen["ms"] = pos.get("_exit_preview_ms")
        return {"status": "closed"}

    R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn,
                                   "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    assert seen["src"] == "own"
    assert seen["ms"] is not None


def test_a_batched_preview_is_labelled_batch_and_not_charged_as_zero(monkeypatch):
    """The pre-pass quotes every position concurrently, so its cost is UNATTRIBUTABLE, not zero.

    Reporting 0 would make every batched exit look cheaper than one that bought its own, and the
    bias runs one direction, so a median over mixed rows would drift low.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    pid = _open_4663(c, entry=1.0, notional=10.0)
    seen = {}

    def exit_fn(conn, pos, reason, now):
        seen["src"] = pos.get("_exit_preview_src")
        seen["ms"] = pos.get("_exit_preview_ms")
        return {"status": "closed"}

    called = {"n": 0}

    def never(pos):                                  # must NOT be consulted; the batch supplied it
        called["n"] += 1
        return {"expected_usdc": 99.0}

    R.real_exit_sweep(c, {"4663": {"exit_fn": exit_fn, "preview_exit_fn": never}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z",
                      preview_by_position={pid: {"expected_usdc": 16.0}})
    assert seen["src"] == "batch"
    assert seen["ms"] is None                        # withheld, never rendered as 0
    assert called["n"] == 0


# --- stuck-bag backoff wiring (2026-09-02) ----------------------------------------------------
# Position 46888 ran 65 failing ladders in 31 minutes, re-fired every 10-16s by three rails with no
# memory that the last one failed. The decision layer lives in memecoin_stuck_backoff; these cover
# that the sweep actually consults it, and that it can never strand a position.

def _stuck_history(c, pid, n, *, at="2026-07-12T00:04:59+00:00"):
    for _ in range(n):
        m.record_exit_execution(c, position_id=pid, network_id="4663", exit_reason="real_tp",
                                attempted_at=at, status="stuck")


def test_a_rail_is_suppressed_while_the_bag_is_backing_off(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    pid = _open_4663(c, entry=1.0, notional=10.0)
    _stuck_history(c, pid, 3)                       # 3 failures 1s ago -> wait 120s
    calls = []
    R.real_exit_sweep(c, {"4663": {"exit_fn": lambda *a: calls.append(a) or {"status": "closed"},
                                   "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    assert calls == [], "a bag mid-backoff must not re-enter the executor"


def test_manual_always_pierces_the_backoff(monkeypatch):
    """The operator hand-closed 4 of 7 stuck positions. Blocking the human is the one unacceptable
    outcome — a waste fix must never become a stranded-money bug."""
    c = _c()
    pid = _open_4663(c, entry=1.0, notional=10.0)
    _stuck_history(c, pid, 20)                      # deep into the backoff
    from sniper.services.memecoin_stuck_backoff import stuck_backoff
    blocked, _ = stuck_backoff(c, pid, "4663", exit_reason="manual",
                               now_iso="2026-07-12T00:05:00+00:00")
    assert blocked is False


def test_a_backoff_failure_allows_the_exit(monkeypatch):
    """If the guard itself raises, the position must still be able to leave."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    import sniper.services.memecoin_stuck_backoff as sb
    monkeypatch.setattr(sb, "stuck_backoff",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    c = _c()
    pid = _open_4663(c, entry=1.0, notional=10.0)
    _stuck_history(c, pid, 5)
    calls = []
    R.real_exit_sweep(c, {"4663": {"exit_fn": lambda *a: calls.append(a) or {"status": "closed"},
                                   "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    assert len(calls) == 1, "a raising backoff must never suppress an exit"


def test_a_first_attempt_is_never_delayed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    c = _c()
    _open_4663(c, entry=1.0, notional=10.0)         # no history at all
    calls = []
    R.real_exit_sweep(c, {"4663": {"exit_fn": lambda *a: calls.append(a) or {"status": "closed"},
                                   "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-07-12T00:05:00Z")
    assert len(calls) == 1


# --- the stage map has to be emitted at a level the runner actually shows (2026-09-02) ----------
# The runner configures no logging level, so the root logger sits at WARNING and every
# `logger.info` on the funded path is DISCARDED. Proven on the first funded exit after the map
# shipped: position 48336 closed 15:14:28Z with a `memecoin_exit_execution_attempts` row
# (total_ms=6907), the WARNING-level `vol_death:` line for that same exit is in the log, and
# `exit stage map` appears zero times. An instrument nobody can read is not an instrument.

def _stage_map_log_level():
    """The logger method used for the `exit stage map` line, read from the AST.

    Matched on the FORMAT STRING of the call, not on source text, so this test cannot match its
    own comments — a source-scanning guard that reads its own prose reports itself.
    """
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "sniper" / "execution" / "evm_real_seams.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Name) and target.id == "logger"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if str(node.args[0].value).startswith("exit stage map pos="):
            return node.func.attr
    return None


def test_the_exit_stage_map_is_emitted_above_info():
    """INFO is dropped by this process, so an INFO stage map is a map nobody ever sees."""
    level = _stage_map_log_level()
    assert level is not None, "the exit stage map log line is gone"
    assert level != "info", (
        "the exit stage map is logged at INFO; the runner sets no level so the root logger is "
        "WARNING and the line is discarded — this is the defect that hid it for a full day")
    assert level in {"warning", "error", "critical"}


def test_the_runner_still_configures_no_logging_level():
    """The premise. If someone later calls basicConfig(level=INFO) this test should be revisited
    rather than silently keeping the map at WARNING for a reason that stopped being true."""
    import logging
    assert logging.getLogger("sniper.execution.evm_real_seams").getEffectiveLevel() >= logging.WARNING
