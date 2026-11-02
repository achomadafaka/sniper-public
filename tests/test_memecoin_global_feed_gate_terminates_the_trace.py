"""The global-feed refusal must TERMINATE its attempt trace, like every other gate does.

2026-09-08: 33 attempt rows (11% of the global-feed lane, against <=0.5% on every earlier day)
carried `terminal_status IS NULL` — a decision was started and never recorded a verdict. Every one
was `delivery_lane='global_feed'`; the websocket and rest lanes on the same three chains were 0%.

Cause: the 2026-09-06 "the global feed is a telescope, not a trading signal" gate returns from
`real_entry_hook` before `identity` is bound and before the local `trace is None` setup, and it
was the ONLY return in that function that never called `_finish_trace_for_result` — the other
thirteen all do. `_hook_raw` cannot cover it either: it sets `handed_off = True` before calling
the hook precisely because the hook owns the trace from that point on.

Exposure jumped that day because chain 56 became routable at 11:00. BSC global-feed events had
been dying earlier at `unroutable_chain`, which DOES write a terminal; once 56 entered `contexts`
they reached this gate instead. First orphan 11:31.

No money moves either way. The damage is to the attempt tape, which is the evidence base every
entry study counts — an orphaned refusal is invisible to "why did we not buy this", and it was
this gap that made the funded null-rate read 6.9% and look like a runner fault.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg

NOW = "2026-07-15T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)


class _Trace:
    """Stands in for the caller's trace. Records the one call that was missing."""

    def __init__(self):
        self.finished = []

    def finish(self, terminal_status, terminal_reason, **fields):
        self.finished.append((terminal_status, terminal_reason))
        return True


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "alice")
    m.set_wallet_real_approved(conn, "u1", True, chains=["solana"], force=True)
    return conn


def _event(source="global_feed"):
    return SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="So1Mint", ticker="ABC",
        network_id="solana", their_price=0.25, their_market_cap=100_000.0,
        their_size_usd=5_000.0, is_first_buy=True, fomo_event_id="event-1",
        created_at="2026-07-15T11:59:59+00:00", received_at="legacy-host-clock",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc", lag_s=0.1, signal_source=source,
    )


def _run(conn, ev, trace):
    return runner.real_entry_hook(
        conn, ev, {"solana": {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
                              "token_safety_fn": lambda mint, **kw: (True, "ok"),
                              "execute_fn": lambda *a, **k: {"status": "unknown"},
                              "kill_path": None}},
        now_fn=lambda: NOW, trade_size=10.0, max_age_s=120, trace=trace)


def test_the_global_feed_refusal_writes_a_terminal_status(monkeypatch):
    """THE REGRESSION: this returned a refusal and left the row's verdict NULL forever."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", False, raising=False)
    trace = _Trace()
    res = _run(_conn(), _event(), trace)
    assert res["status"] == "rejected"
    assert res["reason"] == "global_feed entries disabled"
    assert trace.finished, "the refusal orphaned its trace"
    status, reason = trace.finished[0]
    assert status == "refused", status
    assert reason == "global_feed entries disabled", reason


def test_it_does_not_crash_when_there_is_no_trace(monkeypatch):
    """The hook is called directly (not via `_hook_raw`) in several paths; trace stays optional."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", False, raising=False)
    assert _run(_conn(), _event(), None)["reason"] == "global_feed entries disabled"


def test_the_gate_still_refuses_only_the_global_feed(monkeypatch):
    """Terminating the trace must not widen or narrow what the gate actually refuses."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", False, raising=False)
    trace = _Trace()
    res = _run(_conn(), _event(source="follow_activity"), trace)
    assert (res or {}).get("reason") != "global_feed entries disabled"


def test_the_switch_on_still_lets_the_global_feed_through(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", True, raising=False)
    res = _run(_conn(), _event(), _Trace())
    assert (res or {}).get("reason") != "global_feed entries disabled"


def test_every_refusal_return_in_the_hook_terminates_its_trace():
    """The invariant, checked at the source: this gate was the only one of fourteen that did not,
    and nothing failed when it was added. Guard the class, not just the instance."""
    import ast
    import pathlib
    src = (pathlib.Path(runner.__file__)).read_text()
    lines = src.splitlines()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "real_entry_hook")
    orphans = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
            continue
        if not any(isinstance(k, ast.Constant) and k.value == "status"
                   for k in node.value.keys):
            continue
        # the finish call sits in the handful of lines that build and record the refusal
        window = "\n".join(lines[max(0, node.lineno - 13):node.lineno])
        if "_finish_trace_for_result" not in window:
            orphans.append((node.lineno, lines[node.lineno - 1].strip()[:90]))
    assert not orphans, "returns that leave the attempt row's verdict NULL: %s" % orphans
