"""Standing-allowance top-ups start only in the funded runner (2026-10-15).

Every web start signed its own top-ups on the funded wallet. Base WETH->Permit2: the runner at
11:23:11Z and 11:27:11Z (8 s after its start), the web at 11:23:31Z and 11:27:31Z (7 s after its
start); 56 WBNB the same for all four pinned spenders. The web's book is empty at boot, so it topped
up on every restart, and the last writer set the chain allowance: at 11:23 the runner's Base book
counted 211707420345083095 while the chain held 211705627558991773. build_real_evm_context starts
the thread only on an explicit opt-in, and only scripts/memecoin_runner_loop.py passes it."""
import ast
from pathlib import Path

import pytest

from sniper.execution import evm_real_seams
from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg
from test_memecoin_evm_leg import _chain_identity_http

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def started(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    path = str(tmp_path / "evm.json")
    generate_keypair_file(path)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", path)
    monkeypatch.setenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", "56,8453")
    calls = []
    original = evm_real_seams.build_real_evm_executor

    def build(**kwargs):
        ex = original(**kwargs)
        assert ex.get("standing_start_fn"), "the seam must still offer the start"
        return {**ex, "standing_start_fn": lambda: calls.append(kwargs["chain_id"])}
    monkeypatch.setattr(evm_real_seams, "build_real_evm_executor", build)
    for chain in ("56", "8453"):
        monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
        monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://rpc")
    return calls


@pytest.mark.parametrize("chain", ["56", "8453"])
def test_a_context_built_without_the_opt_in_starts_no_top_ups(started, chain):
    assert leg.build_real_evm_context(chain, http=_chain_identity_http(chain)) is not None
    assert started == []


@pytest.mark.parametrize("chain", ["56", "8453"])
def test_the_runner_opt_in_starts_them(started, chain):
    ctx = leg.build_real_evm_context(chain, http=_chain_identity_http(chain), start_standing=True)
    assert ctx is not None and started == [chain]


def test_the_web_build_starts_no_standing_thread(started, monkeypatch):
    # The web's own call, sniper/web/server.py: build_all_contexts(own_sidecar=False, conn=conn).
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56", "8453"})
    monkeypatch.setattr(runner, "build_real_context", lambda **_kw: None)
    seen = {}
    real = leg.build_real_evm_context

    def build(chain, **kw):
        seen[chain] = kw.get("start_standing", False)
        return real(chain, http=_chain_identity_http(chain), **{k: v for k, v in kw.items() if k != "http"})
    monkeypatch.setattr(leg, "build_real_evm_context", build)
    contexts = runner.build_all_contexts(own_sidecar=False, conn=None)
    assert set(contexts) == {"56", "8453"} and started == [] and seen == {"56": False, "8453": False}
    runner.build_all_contexts(own_sidecar=False, conn=None, start_standing=True)
    assert sorted(started) == ["56", "8453"]


def _calls(path, name):
    tree = ast.parse((ROOT / path).read_text())
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) == name]


def _opt_in(call):
    return any(kw.arg == "start_standing" and isinstance(kw.value, ast.Constant)
               and kw.value.value is True for kw in call.keywords)


def test_only_the_runner_loop_opts_in():
    loop = _calls("scripts/memecoin_runner_loop.py", "build_all_contexts")
    assert len(loop) == 1 and _opt_in(loop[0]), "the funded runner must keep its top-ups"
    for path, name in {"sniper/web/server.py": "build_all_contexts",
                       "sniper/services/memecoin_real_runner.py": "build_all_contexts",  # web rebuild
                       "scripts/memecoin_lifi_canary.py": "build_real_evm_context"}.items():
        calls = _calls(path, name)
        assert calls and not any(_opt_in(call) for call in calls), path
    # The deploy verify builds its contexts inside a `python -c` string.
    verify = (ROOT / "scripts/memecoin_runtime_verify.py").read_text()
    assert "build_all_contexts(" in verify and "start_standing" not in verify
