"""Being out-ranked is not being degraded.

Both funded EVM chains paged EVM-RPC-DEGRADED at 2026-09-08T11:58Z. Every pool member on both was
healthy: zero failures, zero quarantine, verified identity, no penalties. What had happened is that
the pool ranked by measured latency and lag and picked the better endpoint —

    4663  primary rpc.mainnet.chain.robinhood.com  157.1 ms / 5.6 blocks behind
          chosen  quiknode                          54.7 ms / 0 blocks
    56    primary bsc-dataseed.bnbchain.org        249.8 ms
          chosen  bsc-rpc.publicnode.com            62.8 ms

— which is the pool doing its job. The old test was `chosen != configured`, and its docstring called
that "the whole signal". It was not. The page even ended "this is FYI", which is the tell: a red
alert nobody is expected to act on trains the operator to ignore the channel that also carries real
outages. Same failure as the funding-script guard that sat permanently red on a substring collision.

These tests drive the real `_evm_primary_unhealthy` extracted from the runner, so they check the
shipped predicate rather than a copy of its logic.
"""
import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()


def _load_predicate(snapshot_rows):
    """Compile the real `_evm_primary_unhealthy` against a fake pool snapshot."""
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_evm_primary_unhealthy")
    ns = {"_erp": type("P", (), {"snapshot": staticmethod(lambda urls: snapshot_rows)})()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<runner>", "exec"), ns)
    return ns["_evm_primary_unhealthy"]


HEALTHY = {"host": "primary.example", "ewma_ms": 157.1, "lag_blocks": 5.6, "ok": 28, "fail": 0,
           "quarantined_s": 0.0, "identity": True, "last_penalty": None,
           "last_penalty_age_s": None, "penalties": 0, "refused": 0, "refused_methods": []}
PRI = "https://primary.example/rpc"


def test_a_healthy_primary_that_is_merely_slower_never_pages():
    """THE REGRESSION. This is exactly the 2026-09-08 state on both chains."""
    assert _load_predicate([dict(HEALTHY)])(PRI, [PRI]) is None


def test_a_quarantined_primary_pages():
    row = dict(HEALTHY, quarantined_s=42.0)
    assert "quarantin" in _load_predicate([row])(PRI, [PRI])


def test_a_wrong_chain_primary_pages():
    """Reading a funded quote from the wrong chain is worse than being slow."""
    row = dict(HEALTHY, identity=False)
    assert "identity" in _load_predicate([row])(PRI, [PRI])


def test_a_primary_that_refuses_methods_pages():
    row = dict(HEALTHY, refused_methods=["eth_getLogs"])
    assert "eth_getLogs" in _load_predicate([row])(PRI, [PRI])


def test_full_history_log_limit_does_not_make_current_reads_unhealthy():
    row = dict(HEALTHY, refused=1, last_refusal="-32602",
               refused_methods=["eth_getLogs:full_range"])
    assert _load_predicate([row])(PRI, [PRI]) is None


def test_full_history_limit_does_not_hide_another_refused_method():
    row = dict(HEALTHY, refused_methods=["eth_getLogs:full_range", "eth_call"])
    why = _load_predicate([row])(PRI, [PRI])
    assert why == "refuses eth_call"


def test_full_history_limit_does_not_hide_a_recent_provider_failure():
    row = dict(HEALTHY, refused_methods=["eth_getLogs:full_range"],
               last_penalty="provider_http_failure", last_penalty_age_s=20.0)
    assert "penalised" in _load_predicate([row])(PRI, [PRI])


def test_a_recent_penalty_pages_but_an_old_one_does_not():
    recent = dict(HEALTHY, last_penalty="hedged_read_failed", last_penalty_age_s=30.0)
    assert "penalised" in _load_predicate([recent])(PRI, [PRI])
    old = dict(HEALTHY, last_penalty="hedged_read_failed", last_penalty_age_s=9000.0)
    assert _load_predicate([old])(PRI, [PRI]) is None, "a stale penalty is not a current outage"


def test_a_primary_missing_from_the_pool_pages():
    assert "absent" in _load_predicate([dict(HEALTHY, host="someone.else")])(PRI, [PRI])


@pytest.mark.parametrize("boom", [RuntimeError("no pool"), ValueError("bad")])
def test_an_unreadable_snapshot_never_pages_on_a_guess(boom):
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_evm_primary_unhealthy")

    def _raise(_urls):
        raise boom
    ns = {"_erp": type("P", (), {"snapshot": staticmethod(_raise)})()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<runner>", "exec"), ns)
    assert ns["_evm_primary_unhealthy"](PRI, [PRI]) is None


def test_the_watcher_consults_the_predicate_rather_than_bare_deselection():
    watch = next(ast.get_source_segment(SRC, n) for n in ast.walk(ast.parse(SRC))
                 if isinstance(n, ast.FunctionDef) and n.name == "_evm_pool_watch")
    assert "_evm_primary_unhealthy" in watch
    assert "this is FYI" not in watch, "an alert nobody should act on must not be sent at all"
