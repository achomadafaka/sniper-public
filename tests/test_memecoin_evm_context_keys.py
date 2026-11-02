"""ONE GUARD FOR A CLASS: a reader takes a key the production EVM context never sets.

Three times a hand-made context dict in a test hid a key missing from the real one: 4663 holding
sells (`rpc_url`, 2026-09-22), the chain-1 expiry proof (`proof_snapshot_fn`, 2026-09-30) and the
4663 dip watch (`venue_quote_with_miss_fn`, `quote_token`, 2026-09-30). Each reader had tests, and
each test built its own context.

This builds the PRODUCTION context (`memecoin_evm_leg.build_real_evm_context`) for every funded
EVM chain and checks two kinds of read, with the names collected from the sources, so a new read
fails here the day it is written:

- a SUBSCRIPT, `ctx["name"]` (any variable ending in ctx or context, and `rc`): the key must be in
  the context. This is the dip watch defect and the holding-sell defect.
- an OPTIONAL read through the variable `ctx`, `ctx.get("name")` or `(ctx or {}).get("name")`:
  the key must be in the context, or be declared in OPTIONAL_KEYS with the reason it may be
  absent. An undeclared optional read of a key the builder never sets is a feature that is
  silently off; that is the chain-1 `proof_snapshot_fn` defect. `.get` through any other variable
  name is NOT collected: those are mostly other dicts (alert views, the LI.FI context).
"""
import json
import re
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
FUNDED_EVM_CHAINS = ("4663", "56", "8453", "5042")
# `ctx[...]`, `rc[...]`, `context[...]`, `evm_ctx[...]`: a READ, never an assignment.
READ = re.compile(r"""\b(\w*ctx|\w*context|rc)\[["']([A-Za-z_0-9]+)["']\](?!\s*=[^=])""")
# Files whose `ctx` is another dict. A file added here must still read one, or the entry is stale.
NOT_THE_EVM_CONTEXT = {
    "sniper/services/memecoin_slot_lane.py": "the Solana funded context",
    "sniper/services/memecoin_other_holdings.py": "the Solana funded context",
    "sniper/services/memecoin_withdraw.py": "the Solana funded context",
    "sniper/services/memecoin_opportunity_live.py": "a slot context",
    "sniper/services/memecoin_desk_context_facts.py": "the desk JUDGE context-facts sheet",
}
# Variables that end in `context` and are another dict.
NOT_A_CONTEXT_VARIABLE = {"classification_context": "the manual buy's classification facts"}
# Keys read under a Solana-only branch of a file that also handles EVM contexts.
SOLANA_ONLY_KEYS = {"sol_fn": "memecoin_real_runner reads the SOL balance of a Solana context",
                    "rpc_confirm_fn": "runner_loop rent sweeps read the Solana context's confirm, behind .get"}


# `ctx.get("name")` and `(ctx or {}).get("name")`.
OPTIONAL_READ = re.compile(r"""(?:\bctx|\(ctx\s+or\s+\{\}\))\.get\(\s*["']([A-Za-z_0-9]+)["']""")
# Read with .get and absent from every EVM context ON PURPOSE.
OPTIONAL_KEYS = {
    "chain_id": "legacy injected holdings context compatibility; production uses chain",
    "block_height_fn": "Solana only: the blockhash-expiry proof",
    "confirm_proof_once_fn": "Solana only",
    "mint_read_fn": "Solana only",
    "conn": "the web process's lazily recovered context, not a chain context",
    "real_mark_fn": "the web process's lazily recovered context, not a chain context",
    "http": "an optional transport; absent means the default one",
    "recovery_only": "set only on a recovery-only context (a chain out of the allowlist)",
    "entry_size_cap_usd": "set only on the RH fast lane shared-route 4663 context: its pilot clip (N18)",
    "early_quote_balances_fn": "installed by BSC build_live from its lane snapshot, and on the RH fast lane's per-signal 4663 shared-route context copy under MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT; absent otherwise",
    "entry_signing_snapshot_fn": "installed by BSC build_live from its local snapshot; absent before lane startup and on other chains",
    "native_funding_precheck_fn": "installed by BSC build_live; absent before lane startup and on other chains",
}
# Read with .get, never set, and NOT on purpose: the feature behind it has never run on an EVM
# chain. Empty is the goal. `_send` and `_confirm_once` sat here on 2026-09-30 (the pinned-close
# re-offer); they are wired now as `reoffer_send_fn` and `confirm_once_fn`.
KNOWN_UNWIRED = {}


def _sources():
    return sorted([ROOT / "scripts" / "memecoin_runner_loop.py",
                   *(ROOT / "sniper" / "services").glob("memecoin_*.py"),
                   *(ROOT / "sniper" / "execution").glob("*.py")])


def _subscript_reads():
    """{key: [file:line, ...]} over every scanned source that handles an EVM context."""
    reads, seen_exempt = {}, set()
    for path in _sources():
        rel = str(path.relative_to(ROOT))
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for var, key in READ.findall(line):
                if rel in NOT_THE_EVM_CONTEXT or var in NOT_A_CONTEXT_VARIABLE:
                    seen_exempt.add(rel if rel in NOT_THE_EVM_CONTEXT else var)
                    continue
                reads.setdefault(key, []).append("%s:%d" % (rel, number))
    return reads, seen_exempt


def _optional_reads():
    reads = {}
    for path in _sources():
        rel = str(path.relative_to(ROOT))
        if rel in NOT_THE_EVM_CONTEXT:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for key in OPTIONAL_READ.findall(line):
                reads.setdefault(key, []).append("%s:%d" % (rel, number))
    return reads


def _production_context(monkeypatch, tmp_path, chain):
    from sniper.execution.evm_wallet import generate_keypair_file
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://rpc-" + chain)
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        return 200, {"result": hex(int(chain)) if request["method"] == "eth_chainId" else "0x"}, None
    ctx = leg.build_real_evm_context(chain, http=http)
    assert ctx is not None and ctx["chain"] == chain
    return ctx


@pytest.mark.parametrize('answer,detail', [
    ({'error': {'code': -32603, 'message': 'service temporarily unavailable'}}, '-32603'),
    ({'error': {'code': 3, 'message': 'execution reverted', 'data': '0xdeadbeef'}}, '0xdeadbeef'),
    ({'error': {'code': 3, 'message': 'https://rpc.example/private-key?token=secret'}}, 'rpc.example'),
    ({}, 'eth_estimateGas result is absent'),
])
def test_production_gas_check_preserves_rpc_failure_without_credentials(
        monkeypatch, tmp_path, answer, detail):
    from sniper.execution import evm_real_seams as seams
    ctx = _production_context(monkeypatch, tmp_path, '56')
    calls = []
    monkeypatch.setattr(seams, '_rpc', lambda *a, **k: calls.append(a) or answer)
    with pytest.raises(RuntimeError) as failure:
        ctx['lifi_local']['estimate_gas_fn']({'to': '0x' + '11' * 20})
    assert detail in str(failure.value)
    assert 'private-key' not in str(failure.value) and 'secret' not in str(failure.value)
    assert len(calls) == 1 and calls[0][1] == 'eth_estimateGas'


def test_production_gas_check_keeps_successful_estimate(monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams as seams
    ctx = _production_context(monkeypatch, tmp_path, '56')
    monkeypatch.setattr(seams, '_rpc', lambda *a, **k: {'result': '0xf10ef'})
    assert ctx['lifi_local']['estimate_gas_fn']({'to': '0x' + '11' * 20}) == 987375


@pytest.mark.parametrize("chain", FUNDED_EVM_CHAINS)
def test_the_production_evm_context_carries_every_key_a_reader_subscripts(monkeypatch, tmp_path, chain):
    reads, _exempt = _subscript_reads()
    ctx = _production_context(monkeypatch, tmp_path, chain)
    missing = {key: where[:3] for key, where in sorted(reads.items())
               if key not in ctx and key not in SOLANA_ONLY_KEYS}
    assert not missing, "chain %s context lacks keys that are read with ctx[...]: %s" % (chain, missing)


@pytest.mark.parametrize("chain", FUNDED_EVM_CHAINS)
def test_every_optional_read_is_set_by_the_builder_or_declared_optional(monkeypatch, tmp_path, chain):
    reads = _optional_reads()
    ctx = _production_context(monkeypatch, tmp_path, chain)
    declared = set(OPTIONAL_KEYS) | set(KNOWN_UNWIRED)
    unset = {key: where[:3] for key, where in sorted(reads.items())
             if key not in ctx and key not in declared}
    assert not unset, ("chain %s: read with ctx.get(...), never set by build_real_evm_context and "
                       "not declared optional, so the code behind it never runs: %s" % (chain, unset))
    # A declared key that is now set, or no longer read, is a stale declaration.
    assert not [key for key in declared if key in ctx], "declared absent but the builder sets it"
    assert declared <= set(reads), sorted(declared - set(reads))


def test_the_scan_sees_the_known_readers_and_no_exemption_is_stale():
    reads, seen_exempt = _subscript_reads()
    # The three defects of this class, by their reader.
    assert any("memecoin_dip_retry.py" in w for w in reads["venue_quote_with_miss_fn"])
    assert any("memecoin_dip_retry.py" in w for w in reads["quote_token"])
    assert any("holding_routes.py" in w for w in reads["rpc_url"])
    exempt = set(NOT_THE_EVM_CONTEXT) | set(NOT_A_CONTEXT_VARIABLE)
    # A file exempted for an optional `ctx.get(...)` read only is still a live exemption.
    seen_exempt |= {rel for rel in NOT_THE_EVM_CONTEXT
                    if OPTIONAL_READ.search((ROOT / rel).read_text())}
    assert seen_exempt == exempt, "a stale exemption: %s" % sorted(exempt - seen_exempt)
    assert all(key in reads for key in SOLANA_ONLY_KEYS), SOLANA_ONLY_KEYS


def test_an_assignment_is_not_collected_and_an_equality_is():
    assert READ.findall('ctx["real_buy_fn"] = make()') == []
    assert READ.findall('context["recovery_snapshot"]=x') == []
    assert READ.findall('if ctx["chain"] == "1":') == [("ctx", "chain")]
    assert READ.findall("fn = evm_ctx['exit_fn']") == [("evm_ctx", "exit_fn")]
    assert READ.findall('ctx.get("optional")') == []
    assert READ.findall('pk = rc["pubkey"]') == [("rc", "pubkey")]
    assert OPTIONAL_READ.findall('fn = (ctx or {}).get("proof_snapshot_fn")') == ["proof_snapshot_fn"]
    assert OPTIONAL_READ.findall('x = ctx.get("http"); y = view_context.get("ticker")') == ["http"]


def test_production_base_gas_context_shares_nonce_claim_and_exact_release(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "8453")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    ctx = _production_context(monkeypatch, tmp_path, "8453")
    gas = ctx["gas_topup"]
    assert gas["claim_nonce_fn"](5) == 5
    assert gas["claim_nonce_fn"](5) == 6
    assert gas["release_nonce_fn"](5) is False
    assert gas["release_nonce_fn"](6) is True
    assert gas["claim_nonce_fn"](5) == 6
