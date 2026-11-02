"""The paper bracket_pass runs on its own thread (2026-09-27, Alpha GO).

maint.bracket.paper was 98% of the bracket block and 14.7% of the funded loop's wall time
(13:59-14:05Z), all paper price reads and paper exit quotes. The hold-stamp normalize inside it also
rewrites REAL rows, so it stays on the loop and the worker runs bracket_pass(normalize=False).
"""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_runner as run

SRC = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "memecoin_runner_loop.py")).read()
NOW = "2026-09-27T14:10:00Z"


class _Conn:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _fresh_worker(monkeypatch):
    monkeypatch.setattr(run, "_PAPER_BRACKET", {"thread": None, "pass_started_at": None,
                                                "last_pass_s": None, "failures": 0})


def _join():
    run._PAPER_BRACKET["thread"].join(5)
    assert not run._PAPER_BRACKET["thread"].is_alive()


def test_a_hung_paper_pass_leaves_the_loop_and_shows_in_the_status(monkeypatch):
    gate, seen, conn = threading.Event(), [], _Conn()

    def slow_pass(c, **kw):
        seen.append((c, kw))
        gate.wait(5)
    monkeypatch.setattr(run, "bracket_pass", slow_pass)
    t0 = time.monotonic()
    assert run.start_paper_bracket(now_fn=lambda: NOW, connect=lambda: conn,
                                   evm_price_for=None, supplemental_price_map={}) is True
    assert time.monotonic() - t0 < 0.5
    status = run.paper_bracket_status()
    assert status["alive"] and status["pass_started_at"] == NOW
    assert run.start_paper_bracket(now_fn=lambda: NOW, connect=lambda: conn) is False
    gate.set()
    _join()
    (c, kw), = seen
    assert c is conn and kw["normalize"] is False and kw["supplemental_price_map"] == {}
    assert conn.closed == 1 and run.paper_bracket_status()["last_pass_s"] is not None


def test_a_failed_pass_counts_and_closes_its_connection(monkeypatch):
    conn = _Conn()

    def boom(c, **kw):
        raise RuntimeError("dexscreener down")
    monkeypatch.setattr(run, "bracket_pass", boom)
    assert run.start_paper_bracket(now_fn=lambda: NOW, connect=lambda: conn) is True
    _join()
    assert run.paper_bracket_status()["failures"] == 1 and conn.closed == 1


def test_normalize_false_skips_the_stamp_rewrite_that_touches_real_rows(monkeypatch):
    from sniper.services import memecoin_exits
    calls = []
    monkeypatch.setattr(run.m, "normalize_open_position_hold_stamps",
                        lambda conn, **kw: calls.append(kw))
    monkeypatch.setattr(memecoin_exits, "recover_orphaned_paper_closes", lambda conn, now: None)
    monkeypatch.setattr(run.m, "list_open_positions", lambda conn: [])
    run.bracket_pass(object(), now_fn=lambda: NOW, normalize=False)
    assert calls == []
    run.bracket_pass(object(), now_fn=lambda: NOW)
    assert calls == [{"now_iso": NOW}]


def test_a_paper_exit_quote_from_the_worker_spends_the_paper_role(monkeypatch):
    """jupiter_role is a ContextVar, and a new thread does not inherit it. The paper quote must
    set it at the call; a quote that ran as funded would cool the funded Jupiter host on a 429."""
    from sniper.execution import solana_real_seams as seams
    from sniper.services import memecoin_copy_trader as ct
    from sniper.services import memecoin_exits, memecoin_quotes
    roles = []

    def quote_exit(position, qty_atomic, **kw):
        roles.append(seams.active_role())
        return SimpleNamespace(unit_price=0.5)
    monkeypatch.setattr(memecoin_quotes, "quote_exit", quote_exit)
    monkeypatch.setattr(ct, "_exit_token_decimals", lambda mint, net: 6)

    # conftest stubs memecoin_exits._exit_quote_fn offline; production delegates to this call.
    assert "return executable_exit_fill(mint, network_id, qty)" in open(
        memecoin_exits.__file__).read()

    def paper_pass(c, **kw):
        roles.append(("worker_default", seams.active_role()))
        roles.append(("ret", ct.executable_exit_fill("Mint1111", "solana", 10.0)))
    monkeypatch.setattr(run, "bracket_pass", paper_pass)
    with seams.jupiter_role(seams.ROLE_PAPER):     # the caller's role must not be what saves it
        assert run.start_paper_bracket(now_fn=lambda: NOW, connect=_Conn) is True
    _join()
    default = roles[0][1]
    assert default != seams.ROLE_PAPER, "the worker inherited a role; this test proves nothing"
    assert roles[1:] == [seams.ROLE_PAPER, ("ret", (0.5, "route_quote"))]


def test_the_loop_keeps_normalize_and_the_inline_path():
    assert run.paper_bracket_thread_enabled({"MEMECOIN_PAPER_BRACKET_THREAD": "0"}) is False
    assert run.paper_bracket_thread_enabled({}) is True
    start = SRC.index("if _PAPER_BRACKET_THREAD:")
    block = SRC[start:SRC.index('_ph["maint.bracket.paper"]', start)]
    threaded, inline = block.split("\n                else:\n", 1)
    assert threaded.index("m.normalize_open_position_hold_stamps(conn") < threaded.index(
        "start_paper_bracket(")
    assert "bracket_pass(conn, **_pb_kwargs)" in inline
    assert "if evm_px and _pb_started:" in SRC


def test_the_heartbeat_carries_the_paper_bracket(tmp_path):
    assert "paper_bracket=(paper_bracket_status() if _PAPER_BRACKET_THREAD else None)" in SRC
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), paper_bracket={
        "alive": True, "pass_started_at": "t", "last_pass_s": 2.1, "failures": 0, "x": 1})
    hb = json.loads(path.read_text())["paper_bracket"]
    assert hb["alive"] is True and hb["last_pass_s"] == 2.1 and "x" not in hb
