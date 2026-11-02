"""Reconcile ownership (2026-10-06): the Ethereum worker reconciles chain 1 on its own connection,
the main loop reconciles everything else. Together they must cover EVERY context key and EVERY
network with open real inventory, each exactly once; a network with inventory but no context
must stay loud (status 'unreadable') on the main loop, never go silent."""
import re
import sqlite3
from pathlib import Path

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

LOOP = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, mint, network):
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint=mint, ticker="",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real", network_id=network)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=500.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t")
    return pid


def _ctx():
    return {"token_checked_fn": lambda mint: 0.0}        # every read drifts -> every pos is a finding


def _owners(c, contexts, main_scope, worker_scope):
    """position_id -> how many sides reconciled it, wired exactly as the runner loop wires it."""
    maintenance = {k: v for k, v in contexts.items() if k != "1"}   # runner_loop.py, verbatim
    seen = {}
    for ctxs, scope in ((maintenance, main_scope), ({"1": contexts["1"]} if "1" in contexts else {},
                                                    worker_scope)):
        if scope is None:
            continue
        for f in R.real_reconcile(c, ctxs, alert_fn=lambda *a: None, **scope):
            seen[f["position_id"]] = seen.get(f["position_id"], 0) + 1
    return seen


def _book(c):
    return {net: _open(c, mint, net) for mint, net in (
        ("So1Mint", "solana"), ("0x56", "56"), ("0x4663", "4663"), ("0xeth", "1"),
        ("0xorphan", "999"))}                             # 999: open inventory, no context


CONTEXTS = {"solana": _ctx(), "56": _ctx(), "4663": _ctx(), "1": _ctx()}


def _uncovered(c, contexts, main_scope, worker_scope):
    pids = set(_book(c).values())
    seen = _owners(c, contexts, main_scope, worker_scope)
    return {p for p in pids if seen.get(p) != 1}


def test_scopes_cover_every_context_and_inventory_network_exactly_once():
    c = _c()
    main, worker = R.reconcile_scopes(CONTEXTS)
    assert _uncovered(c, CONTEXTS, main, worker) == set()


def test_without_a_chain1_context_the_main_loop_owns_everything():
    c = _c()
    contexts = {k: v for k, v in CONTEXTS.items() if k != "1"}
    main, worker = R.reconcile_scopes(contexts)
    assert worker is None
    assert _uncovered(c, contexts, main, worker) == set()


def test_the_coverage_check_fails_when_either_side_drops_a_network():
    # main narrowed to its own context keys: the context-less inventory network goes silent
    c = _c()
    _main, worker = R.reconcile_scopes(CONTEXTS)
    narrowed = {"network_ids": {k for k in CONTEXTS if k != "1"}}
    assert _uncovered(c, CONTEXTS, narrowed, worker) != set()
    # worker drops chain 1
    c = _c()
    main, _worker = R.reconcile_scopes(CONTEXTS)
    assert _uncovered(c, CONTEXTS, main, {"network_ids": set()}) != set()
    # both reconcile chain 1: counted twice, also a failure
    c = _c()
    assert _uncovered(c, CONTEXTS, {}, {"network_ids": {"1"}}) != set()


def test_runner_loop_passes_the_scopes_to_both_reconcile_calls():
    src = LOOP.read_text()
    calls = re.findall(r"real_reconcile\((.*?)\)\s*\n", src, re.S)
    assert len(calls) == 2, calls
    worker = [c for c in calls if "eth_contexts" in c]
    main = [c for c in calls if "maintenance_contexts" in c]
    assert len(worker) == len(main) == 1, calls
    assert "_reconcile_worker_scope" in worker[0] and "_reconcile_main_scope" not in worker[0], calls
    assert "_reconcile_main_scope" in main[0] and "_reconcile_worker_scope" not in main[0], calls
    assert "reconcile_scopes(contexts)" in src
