"""Funded Ethereum mainnet (chain 1) leg. Spec docs/superpowers/specs/2026-09-30-memecoin-eth-
mainnet-funded-design.md (Beta, 677e51f9f).

Step 0: the operator's first ETH wallets (Roveryaps, badabeepp, 0xsneakky) have Solana proofs only,
and they cannot be armed on chain 1 before it is funded. So their chain-1 addresses are proved from
their own J7 chain-1 buy receipts, for NAMED wallets, before arming. A proof never changes authority.
The receipt logic is chain-agnostic (Transfer(to = address) of the coin), so the real BSC receipts
of test_axiom_j7_transfer_verify serve, relabelled to chain 1.
"""
import sqlite3

import pytest

from scripts.research import verify_axiom_addresses as cli
from sniper.services import axiom_address_verify as av
from sniper.settings import memecoin as cfg
from test_axiom_j7_transfer_verify import FX, _conn

PUBLIC = "https://eth-reads.example"
CUSTODY_56 = "https://custody-56.example/v2/key"


@pytest.fixture(autouse=True)
def _no_protect_status_watch(monkeypatch):
    """An accepted chain-1 send starts chain1_protect.watch_status: a daemon thread polling
    protect.flashbots.net every 12 s for ~6.4 min. It ran into later tests on the same xdist worker
    and refused deploy 2eba6d095 (Gamma). This file proves the send path, not the watch
    (tests/test_chain1_protect.py owns it), so the start is recorded and no thread runs."""
    from sniper.execution import chain1_protect
    started = []
    monkeypatch.setattr(chain1_protect, "watch_status", lambda tx_hash, **kw: started.append(tx_hash))
    return started


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """FX's three wallets, their J7 buys relabelled to chain 1, NOT armed on chain 1."""
    path = tmp_path / "ledger.db"
    src = _conn()
    src.execute("UPDATE memecoin_events SET network_id='1'")
    src.executescript("""CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_user_id TEXT, handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
        paused INTEGER DEFAULT 0, real_chains TEXT);""")
    for i, uid in enumerate(FX["armed"]):
        src.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) "
                    "VALUES(?,?,1,'solana')", (uid, "handle%d" % i))
    src.commit()
    dst = sqlite3.connect(path)
    dst.executescript("\n".join(src.iterdump()))
    dst.close()
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_1", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", CUSTODY_56)      # .env loaded, chain 1 has no custody yet
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"56": (CUSTODY_56,)}, raising=False)
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append((url, method)) or FX["receipts"].get(params[0]), None))
    return {"path": str(path), "asked": asked}


def _rows(path):
    return sqlite3.connect(path).execute(
        "SELECT network_id, source_user_id, verify_status FROM axiom_verified_addresses").fetchall()


def _authority(path):
    return sqlite3.connect(path).execute(
        "SELECT source_user_id, real_approved, real_chains FROM memecoin_wallets ORDER BY id").fetchall()


def test_chain_1_is_a_j7_proof_chain_with_a_non_funded_default_host():
    assert "1" in av.EVM_CHAINS
    host = av.EVM_RPC["1"]
    # Never publicnode (funded 4663/8453/5042 pools share its per-IP budget) and never drpc.
    assert "publicnode" not in host and "drpc" not in host and host.startswith("https://")


def test_named_unarmed_wallets_are_proved_on_chain_1_and_nothing_is_armed(ledger):
    before = _authority(ledger["path"])
    cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC,
              "--wallets", "handle0,handle1," + FX["armed"][2]])
    rows = _rows(ledger["path"])
    assert {r[0] for r in rows} == {"1"}
    assert {r[1] for r in rows if r[2] == av.CONFIRMED} == set(FX["armed"])
    assert {u for u, _m in ledger["asked"]} == {PUBLIC}
    assert _authority(ledger["path"]) == before          # a proof never changes authority


def test_without_named_wallets_an_unarmed_chain_proves_nothing(ledger):
    cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC])
    assert [r for r in _rows(ledger["path"]) if r[2] == av.CONFIRMED] == []


def test_an_unknown_named_wallet_refuses_before_any_read(ledger):
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC,
                  "--wallets", "handle0,nobody"])
    assert ledger["asked"] == []


def test_wallets_needs_j7(ledger):
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--verify", "--chain", "1", "--wallets", "handle0"])


def test_chain_1_with_no_env_loaded_refuses(ledger, monkeypatch):
    for name in ("MEMECOIN_EVM_RPC_URL_56", "MEMECOIN_EVM_RPC_URL_4663", "MEMECOIN_EVM_RPC_URL_8453"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC,
                  "--wallets", "handle0"])
    assert ledger["asked"] == []


def test_once_chain_1_has_custody_its_own_domain_is_refused(ledger, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", "https://eth-reads.example/v2/custody")
    with pytest.raises(SystemExit):
        cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC,
                  "--wallets", "handle0"])
    assert ledger["asked"] == []


def test_once_chain_1_has_custody_its_own_url_is_the_env_marker(ledger, monkeypatch):
    """With MEMECOIN_EVM_RPC_URL_1 set, chain 1 is an ordinary chain: its own custody URL proves the
    env is loaded, even when no other chain's custody URL is present."""
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_56")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", "https://eth-custody.example/v2/key")
    cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "1", "--rpc-url", PUBLIC,
              "--wallets", "handle0"])
    assert {u for u, _m in ledger["asked"]} == {PUBLIC}


# ================================ step 1: chain registry and config ================================
# Addresses PROVED on chain 2026-09-30 (spec §1): non-empty eth_getCode on publicnode; factories and
# WETH derived via SwapRouter02.factory()/WETH9(), QuoterV2.factory(), V2Router02.factory()/WETH(),
# V4Quoter.poolManager(). Never copied from memory.

from sniper.execution import evm_wallet as W                      # noqa: E402
from sniper.execution.evm_v4_quote import V4                       # noqa: E402
from sniper.services import memecoin_quotes as Q                   # noqa: E402

ETH = "1"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def test_chain_1_quotes_in_weth_not_usdc():
    quote = W.CHAIN_QUOTE[ETH]
    assert quote["token"].lower() == WETH
    assert quote["decimals"] == 18 and quote["kind"] == "eth" and quote["usd_symbol"] == "ETH"
    assert W.QUOTE_SYMBOL[ETH] == "WETH"
    assert ETH in W.native_quote_chains() and not W.native_is_quote(ETH)


def test_a_150_dollar_clip_sizes_to_weth_atomic_from_the_eth_usd_read():
    from sniper.execution.evm_real_seams import usd_to_quote_atomic

    assert usd_to_quote_atomic(ETH, 150.0, price_fn=lambda *_a, **_k: 2500.0) == 6 * 10 ** 16
    assert usd_to_quote_atomic(ETH, 150.0, price_fn=lambda *_a, **_k: None) is None


def test_the_v4_deployment_is_the_one_proved_on_chain():
    assert {k: str(v).lower() for k, v in V4[ETH].items()} == {
        "pool_manager": "0x000000000004444c5dc75cb358380d2e3de08a90",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
        "universal_router": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "gt_network": "eth"}


def test_the_v3_and_v2_venues_are_the_ones_proved_on_chain():
    from sniper.settings.memecoin_v6_paper import (
        MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN, MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN)

    assert {k: v.lower() for k, v in cfg.memecoin_evm_v3_addresses(ETH).items()} == {
        "router": "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
        "quoter": "0x61ffe014ba17989e743c5f6cb21bf9697530b21e"}
    assert cfg.memecoin_evm_v2_exit_config(ETH) == {
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D", "connectors": (WETH, USDC)}
    assert MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN[ETH].lower() == "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
    assert {k: v.lower() for k, v in MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN[ETH].items()} == {
        "v3_factory": "0x1f98431c8ad98523631ae4a59f267346ea31f984",
        "v2_factory": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"}


def test_the_v3_connector_is_the_non_quote_currency():
    """dex_v3_quote skips a connector equal to the sell token: with a WETH bankroll it is USDC."""
    assert cfg.memecoin_evm_v3_connectors(ETH) == ((USDC, 500),)


def test_chain_1_is_on_the_rung_ladder_and_not_the_dexscreener_blind_list():
    assert ETH in cfg.MEMECOIN_EVM_RUNG_LADDER_CHAINS
    assert ETH not in cfg.MEMECOIN_DEX_BLIND_CHAINS


def test_the_read_pool_never_defaults_to_a_shared_funded_domain():
    """publicnode is in the funded 4663/8453/5042 pools and budgets per IP (spec decision 8)."""
    pool = cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_1": "https://eth-custody.example/v2/k"})
    assert all("publicnode" not in u for u in pool.get(ETH, ()))
    both = cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_1": "https://eth-custody.example/v2/k",
                              "MEMECOIN_EVM_RPC_POOL_URLS_1": "https://eth-extra.example"})
    assert both[ETH] == ("https://eth-custody.example/v2/k", "https://eth-extra.example")


def test_ethereum_has_an_independent_default_read_backup():
    primary = "https://eth-custody.example/v2/k"
    assert cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_1": primary})[ETH] == (
        primary, "https://rpc.mevblocker.io")
    # An explicit empty override rolls back to custody alone.
    assert ETH not in cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_1": primary,
                                        "MEMECOIN_EVM_RPC_POOL_URLS_1": ""})
    assert ETH not in cfg._evm_rpc_pool({})


def test_market_data_does_not_price_chain_1():
    from sniper.execution.geckoterminal_quote import GT_NETWORK, GT_OHLCV_NETWORK
    # DexScreener indexes Ethereum: GeckoTerminal must not price it (the RUSSELL/Base rule)
    assert ETH not in GT_NETWORK and ETH not in GT_OHLCV_NETWORK


def test_ethereum_is_funded_capable_but_not_allowlisted_by_default():
    from sniper.services import memecoin_chains as C

    import inspect

    assert next(c for c in C.CHAINS if c["key"] == "eth")["funded"] is True
    # funded is CAPABILITY; the code-default allowlist still excludes 1 (activation is the operator's)
    assert '"MEMECOIN_EVM_ALLOWLIST", "8453,4663"' in inspect.getsource(cfg)


def test_no_other_chain_moves():
    assert W.CHAIN_QUOTE["4663"] == {
        "token": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", "decimals": 18, "kind": "eth",
        "usd_symbol": "ETH"}
    assert W.CHAIN_QUOTE["8453"] == {
        "token": "0x4200000000000000000000000000000000000006", "decimals": 18, "kind": "eth",
        "usd_symbol": "ETH"}
    assert W.CHAIN_QUOTE["56"]["usd_symbol"] == "BNB"
    assert W.QUOTE_SYMBOL["4663"] == "WETH" and W.QUOTE_SYMBOL["56"] == "WBNB"
    assert cfg.memecoin_evm_v3_fee_tiers(ETH) == cfg.MEMECOIN_EVM_V3_FEE_TIERS
    assert V4["8453"]["pool_manager"] == "0x498581ff718922c3f8e6a244956af099b2652b2b"
    assert cfg.memecoin_evm_v3_addresses("8453")["router"] == "0x2626664c2603336E57B271c5C0b26F421741e481"


def test_the_chain_1_defaults_are_all_suffix_scoped():
    import inspect

    src = inspect.getsource(cfg)
    for token in ("MEMECOIN_EVM_V3_QUOTER_1\"", "MEMECOIN_EVM_V3_ROUTER_1\"", "MEMECOIN_EVM_V2_ROUTER_1\"",
                  "MEMECOIN_EVM_V2_CONNECTORS_1\"", "MEMECOIN_EVM_V3_CONNECTOR_1\""):
        assert token in src, token


# --- the coverage manifest (spec decision 5): funded chain-1 entries refuse until each capability is
#     evidenced, and the submission capability waits for the operator's choice (decision 6).

def test_the_pinned_manifest_has_hashed_proof_for_every_live_capability():
    import hashlib
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    manifest = cfg._eth_capability_manifest()
    assert cfg.eth_capability_reason() is None
    for capability in cfg._ETH_REQUIRED_CAPABILITIES:
        proofs = manifest["evidence"][capability]
        assert proofs
        for proof in proofs:
            path = root / proof["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == proof["sha256"]
    private = json.loads((root / manifest["evidence"]["submission"][0]["path"]).read_text())
    assert private["status"] == "confirmed" and private["private_accepted"] is True
    assert private["private_provider"] == "1:flashbots_protect_fast"
    receipt = private["receipt"]
    assert int(receipt["status"], 16) == 1
    assert receipt["transactionHash"].lower() == private["tx_hash"].lower()
    assert private["fee"]["status"] == "complete"
    assert int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16) == int(private["fee"]["total_wei"])


@pytest.mark.parametrize("change, reason", [
    ({"chain_id": 57073}, "eth_manifest_invalid"),
    ({"funded_activation": "blocked"}, "eth_activation_blocked"),
    ({"required_capabilities": {"submission": "unsupported"}}, "eth_submission_unsupported"),
])
def test_each_manifest_gap_has_its_own_reason(change, reason):
    ready = {"chain_id": 1, "funded_activation": "ready",
             "required_capabilities": {name: "supported_tested" for name in cfg._ETH_REQUIRED_CAPABILITIES}}
    manifest = dict(ready, **{k: v for k, v in change.items() if k != "required_capabilities"})
    if "required_capabilities" in change:
        manifest["required_capabilities"] = dict(ready["required_capabilities"],
                                                 **change["required_capabilities"])
    assert cfg.eth_capability_reason(manifest) == reason
    assert cfg.eth_capability_reason(ready) is None


def test_the_evm_leg_refuses_an_allowlisted_chain_1_entry_on_the_manifest(monkeypatch):
    """Allowlisting "1" alone must not trade: the manifest gate refuses first, by name."""
    from sniper.services import memecoin_evm_leg as leg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", ETH})
    monkeypatch.setattr(cfg, "eth_capability_reason", lambda *a, **k: "eth_submission_unsupported")
    reason = leg.chain_capability_refusal(ETH)
    assert reason == {"status": "rejected", "reason": "eth_submission_unsupported"}
    assert leg.chain_capability_refusal("8453") is None


def test_the_entry_refusal_runs_before_any_quote_in_evm_real_entry():
    """The helper only protects if evm_real_entry calls it before the executor: pin the order."""
    import inspect
    from sniper.services import memecoin_evm_leg as leg

    src = inspect.getsource(leg.evm_real_entry)
    assert "chain_capability_refusal(chain)" in src
    assert src.index("chain_capability_refusal(chain)") < src.index("execute_fn(")


def test_chain_1_is_not_in_the_shared_hot_exit_worker_yet():
    """Gamma M6: an L1 approve + sell waits two 12 s blocks; the hot worker is shared with
    4663/8453/56. Chain 1 joins only with its own execution thread (spec v2)."""
    from sniper.services.memecoin_evm_hot import HOT_CHAINS

    assert ETH not in HOT_CHAINS


def test_the_early_evm_dispatch_also_gates_chain_1_on_its_manifest(monkeypatch):
    """memecoin_real_leg refuses before evm_entry_fn is reached, as it does for Ink."""
    import inspect
    from sniper.services import memecoin_real_leg as rl

    src = inspect.getsource(rl.real_entry)
    assert 'route_network in ("57073", "1")' in src
    assert "cfg.chain_capability_reason(route_network)" in src
    monkeypatch.setattr(cfg, "eth_capability_reason", lambda *a, **k: "eth_gas_guard_unsupported")
    assert cfg.chain_capability_reason("1") == "eth_gas_guard_unsupported"
    assert cfg.chain_capability_reason("8453") is None



# --- Gamma M-A on spec v2 (2026-09-30): no chain-1 PAPER read may draw on the metered Infura custody
#     key (MEMECOIN_EVM_RPC_URL_1, set by the operator 11:14Z). Paper quotes and the pre-gate PoolKey
#     prewarm run for every chain-1 J7 buy (45 wallets, 100-175/day, up to ~26 calls a quote).

def test_a_chain_1_paper_quote_refuses_before_any_rpc(monkeypatch):
    from types import SimpleNamespace
    from sniper.execution import evm_wallet as _W

    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", "https://infura.invalid/v3/custody")
    calls = []
    monkeypatch.setattr(_W, "_http", lambda *a, **k: calls.append(a) or pytest.fail("RPC on chain 1"))
    with pytest.raises(Q.IdentityInvalid):
        Q.quote_entry(SimpleNamespace(mint="0x" + "ab" * 20, network_id=ETH), 20.0,
                      evm_rpc_url=cfg.evm_rpc_for_chain(ETH))
    assert calls == []


def test_the_pre_gate_poolkey_prewarm_never_queues_chain_1():
    from sniper.services import memecoin_evm_key_prewarm as kp

    kp.reset_state()
    assert kp.enqueue(ETH, "0x" + "ab" * 20, now=1.0) is False
    assert kp.enqueue("4663", "0x" + "ab" * 20, now=1.0) is True
    kp.reset_state()


# ============================ step 2: private submission branch (spec v2 §3.6) =======================
# Operator 2026-09-30: chain-1 bytes go to Flashbots Protect fast ONLY, never custody or any public
# endpoint, in EVERY submit mode, failing closed (Gamma H2: build_evm_backend always added custody as
# a sender; MEMECOIN_EVM_SUBMIT_MODE is global; the rpc mode sends raw to custody).

import json as _json                                                 # noqa: E402

from eth_account import Account                                    # noqa: E402

FLASHBOTS = "https://rpc.flashbots.net/fast"
INFURA = "https://mainnet.infura.example/v3/custody"


def _raw(chain_id, nonce=0):
    acct = Account.from_key("0x" + "11" * 32)
    signed = acct.sign_transaction({"to": "0x" + "22" * 20, "value": 1, "gas": 21000,
                                    "gasPrice": 10 ** 9, "nonce": nonce, "chainId": chain_id})
    return "0x" + signed.raw_transaction.hex().removeprefix("0x")


class _Http:
    """Records every POST url; answers eth_chainId for the custody check and accepts sends."""

    def __init__(self, chain_hex="0x1", send_ok=True):
        self.posts, self.chain_hex, self.send_ok = [], chain_hex, send_ok

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        req = _json.loads(body)
        self.posts.append((url, req["method"]))
        if req["method"] == "eth_chainId":
            return 200, {"result": self.chain_hex}, None
        if req["method"] == "eth_sendRawTransaction":
            if not self.send_ok:
                raise OSError("flashbots down")
            from sniper.execution.submission_wiring import evm_tx_identity
            return 200, {"result": evm_tx_identity(req["params"][0])}, None
        return 200, {"result": None}, None


def test_the_raw_chain_id_is_read_from_the_signed_bytes():
    from sniper.execution.submission_wiring import evm_raw_chain_id

    assert evm_raw_chain_id(_raw(1)) == 1
    assert evm_raw_chain_id(_raw(8453)) == 8453
    assert evm_raw_chain_id("0xnothex") is None and evm_raw_chain_id("") is None


@pytest.mark.parametrize("mode", ["auto_free", "pooled_rpc", "rpc"])
def test_every_submit_mode_sends_chain_1_bytes_to_flashbots_only(monkeypatch, mode):
    from sniper.execution.submission_backends import build_evm_backend
    from sniper.execution.submission_wiring import make_evm_send_fn

    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", mode)
    http = _Http()
    transport = build_evm_backend(ETH, custody_rpc_url=INFURA, http=http)
    assert transport is not None, "chain 1 never falls back to the legacy custody send"
    raw = _raw(1)
    out = make_evm_send_fn(transport, chain=ETH, action="entry", owner_kind="test", owner_id=None,
                           record=False)(raw)
    assert out and str(out).lower() == raw_identity(raw)
    sends = [u for u, m in http.posts if m == "eth_sendRawTransaction"]
    assert sends == [FLASHBOTS]


def raw_identity(raw):
    from sniper.execution.submission_wiring import evm_tx_identity
    return evm_tx_identity(raw).lower()


def test_flashbots_down_fails_closed_and_never_falls_back_to_custody(monkeypatch):
    from sniper.execution.submission_backends import build_evm_backend
    from sniper.execution.submission_wiring import make_evm_send_fn

    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "auto_free")
    http = _Http(send_ok=False)
    transport = build_evm_backend(ETH, custody_rpc_url=INFURA, http=http)
    out = make_evm_send_fn(transport, chain=ETH, action="entry", owner_kind="test", owner_id=None,
                           record=False)(_raw(1))
    assert out is None                                         # ambiguous, never accepted
    assert [u for u, m in http.posts if m == "eth_sendRawTransaction"] == [FLASHBOTS]


def test_the_legacy_send_refuses_chain_1_bytes_off_flashbots_without_any_request():
    from sniper.execution import evm_real_seams as S

    http = _Http()
    assert S.eth_send_raw(_raw(1), INFURA, http=http) is False
    assert http.posts == []
    assert "private" in (S._last_send_rejection.message or "")
    other = _Http(chain_hex="0x2105")
    assert S.eth_send_raw(_raw(8453), INFURA, http=other)            # other chains unchanged
    assert other.posts == [(INFURA, "eth_sendRawTransaction")]


def test_a_provider_off_flashbots_never_carries_chain_1_bytes():
    from sniper.execution.evm_fast_submit import EvmRpcProvider
    from sniper.execution.submission_transport import AttemptState
    from sniper.execution.submission_wiring import build_request

    http = _Http()
    raw = _raw(1)
    request = build_request(chain_id=ETH, action="entry", tx_identity=raw_identity(raw),
                            signed_payload=raw, owner_kind="test", owner_id=None, created_at=None)
    attempt = EvmRpcProvider(http, INFURA, provider_id="1:custody_rpc").submit(request)
    assert attempt.state is AttemptState.NOT_SENT and http.posts == []
    ok = EvmRpcProvider(http, FLASHBOTS, provider_id="1:flashbots_protect").submit(request)
    assert ok.state is AttemptState.ACCEPTED and http.posts == [(FLASHBOTS, "eth_sendRawTransaction")]


def test_the_executors_identical_bytes_re_offer_goes_to_flashbots(monkeypatch):
    """ctx['_send'] re-offers a pinned close's own bytes (memecoin_exit_rebroadcast). On chain 1 it
    must reach Flashbots and return the identity: a False from the custody guard would read as a
    node rejection to the re-offer caller."""
    from sniper.execution.evm_real_seams import build_real_evm_executor
    from sniper.execution.submission_backends import build_evm_backend

    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "auto_free")
    http = _Http()
    transport = build_evm_backend(ETH, custody_rpc_url=INFURA, http=http)
    ex = build_real_evm_executor(acct=Account.from_key("0x" + "11" * 32), chain_id=ETH,
                                 rpc_url=INFURA, http=http, submission_transport=transport,
                                 venue_quote_fn=lambda *a, **k: None)
    raw = _raw(1, nonce=3)
    assert str(ex["_send"](raw)).lower() == raw_identity(raw)
    assert [u for u, m in http.posts if m == "eth_sendRawTransaction"] == [FLASHBOTS]


# ================== step 3.1: every chain-1 swap carries a short deadline (spec v3 §3.7) ==============

def test_chain_1_swaps_carry_a_60s_deadline_and_other_chains_keep_theirs(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_SWAP_DEADLINE_S_1", raising=False)
    assert cfg.memecoin_evm_swap_deadline_s(ETH, 600) == 60
    assert cfg.memecoin_evm_swap_deadline_s("4663", 600) == 600
    assert cfg.memecoin_evm_swap_deadline_s("56", 300) == 300
    monkeypatch.setenv("MEMECOIN_EVM_SWAP_DEADLINE_S_1", "45")
    assert cfg.memecoin_evm_swap_deadline_s(ETH, 600) == 45
    monkeypatch.setenv("MEMECOIN_EVM_SWAP_DEADLINE_S_1", "0")          # malformed: never 0 or open
    assert cfg.memecoin_evm_swap_deadline_s(ETH, 600) == 60
    assert ETH in cfg.memecoin_evm_expiry_chains() and "4663" not in cfg.memecoin_evm_expiry_chains()


def test_a_chain_1_v3_swap_is_wrapped_in_multicall_with_its_deadline():
    """SwapRouter02 exactInputSingle has NO deadline; chain 1 wraps it so it expires."""
    from sniper.execution.evm_dex_quote import _with_expiry

    inner = "0x04e45aaf" + "00" * 224
    wrapped = _with_expiry(ETH, inner, now_fn=lambda: 1_000_000)
    assert wrapped.startswith("0x5ae401dc")
    assert int(wrapped[10:74], 16) == 1_000_060                 # multicall(uint256 deadline, ...)
    assert inner[2:] in wrapped
    assert _with_expiry("4663", inner, now_fn=lambda: 1_000_000) == inner   # unchanged elsewhere


def test_every_swap_deadline_site_reads_the_per_chain_deadline():
    import inspect
    from sniper.execution import (evm_dex_quote, evm_mixed_hop_router, evm_v2_exit_quote,
                                  evm_v4_multihop_tx, evm_v4_quote)

    for mod in (evm_v4_quote, evm_v4_multihop_tx, evm_mixed_hop_router, evm_v2_exit_quote,
                evm_dex_quote):
        src = inspect.getsource(mod)
        assert "memecoin_evm_swap_deadline_s(" in src, mod.__name__
    v4 = inspect.getsource(evm_v4_quote._dex_v4_quote)
    assert "int(now_fn()) + 600" not in v4
    assert "int(now_fn()) + 300" not in inspect.getsource(evm_v2_exit_quote)


# ============ Gamma on 735d89e70 (step 2) and 4d8fc9cf5 (step 3.1): one MEDIUM, four LOW ============
# MEDIUM: Flashbots-only fails closed for EXITS too. A relay outage leaves a chain-1 bag unsellable,
# so N sends in a row that the relay did not answer page RELAY-DOWN at once.

class _Transport:
    """A transport whose single (private) provider answers with the given states."""

    def __init__(self, states):
        self.states = list(states)

    def submit(self, request):
        from sniper.execution.submission_transport import (
            AttemptState, SubmissionAttempt, SubmissionResult)
        state = AttemptState(self.states.pop(0))
        attempt = SubmissionAttempt("1:flashbots_protect_fast", state, None, 0.0, None, None)
        return SubmissionResult(state, request.tx_identity, (attempt,),
                                attempt.provider_id if state is AttemptState.ACCEPTED else None)


@pytest.fixture
def relay_pages(monkeypatch):
    from sniper.execution import submission_wiring as W2
    from sniper.services import memecoin_alerts
    pages = []
    monkeypatch.setattr(memecoin_alerts, "send_alert",
                        lambda kind, text, **k: pages.append((kind, text)))
    monkeypatch.setattr(W2, "_relay_streak", {})
    return pages


def _send_n(states, chain=ETH):
    from sniper.execution.submission_wiring import make_evm_send_fn
    send = make_evm_send_fn(_Transport(states), chain=chain, action="exit", owner_kind="test",
                            owner_id=None, record=False)
    return [send(_raw(int(chain), nonce=i)) for i in range(len(states))]


def test_three_unanswered_chain_1_sends_in_a_row_page_relay_down_once(relay_pages):
    from sniper.execution.submission_wiring import RELAY_DOWN_AFTER
    assert RELAY_DOWN_AFTER == 3
    out = _send_n(["ambiguous", "not_sent", "ambiguous", "ambiguous"])
    assert out == [None, None, None, None]                   # the seam's verdicts are untouched
    assert [kind for kind, _t in relay_pages] == ["RELAY-DOWN"]
    text = relay_pages[0][1]
    assert "chain 1" in text and "exits" in text and "flashbots_protect_fast" in text


@pytest.mark.parametrize("answer", ["accepted", "explicit_rejected"])
def test_any_answer_from_the_relay_ends_the_streak(relay_pages, answer):
    _send_n(["ambiguous", "ambiguous", answer, "ambiguous", "ambiguous"])
    assert relay_pages == []


def test_the_relay_answering_again_after_a_page_says_so(relay_pages):
    _send_n(["ambiguous"] * 3 + ["accepted", "ambiguous", "ambiguous", "ambiguous"])
    assert [kind for kind, _t in relay_pages] == ["RELAY-DOWN", "RELAY-UP", "RELAY-DOWN"]


def test_a_chain_with_public_senders_never_pages_relay_down(relay_pages):
    _send_n(["ambiguous"] * 5, chain="8453")
    assert relay_pages == []


def test_a_failing_page_never_changes_the_send_verdict(monkeypatch, relay_pages):
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda *a, **k: 1 / 0)
    assert _send_n(["ambiguous"] * 3) == [None, None, None]


def test_the_byte_guard_cannot_be_switched_off_by_a_missing_decoder():
    """LOW: `rlp` was imported inside the try, so a missing dependency returned None for every
    transaction and the chain-1 guard read that as "not chain 1". Now it fails at import."""
    import inspect
    from sniper.execution import submission_wiring as W2
    assert W2.rlp.__name__ == "rlp"
    assert "import rlp" not in inspect.getsource(W2.evm_raw_chain_id)


def test_the_private_branch_clamps_its_deadlines_where_nothing_validates_them(monkeypatch):
    """LOW: `rpc` mode skips `_validate_evm`, and the private branch had a floor but no ceiling.
    The other modes refuse an out-of-range deadline before the branch is reached."""
    from sniper.execution import submission_backends as B
    from sniper.settings import memecoin_submission as C
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_DEADLINE_MS", "900000")
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "auto_free")
    with pytest.raises(C.SubmissionConfigError):
        B.build_evm_backend(ETH, custody_rpc_url=INFURA, http=_Http())
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_MODE", "rpc")
    monkeypatch.setenv("MEMECOIN_EVM_SUBMIT_TIMEOUT_MS", "900000")
    built = {}
    from sniper.execution import evm_fast_submit, submission_transport
    monkeypatch.setattr(evm_fast_submit, "EvmRpcProvider", lambda http, url, *, provider_id, timeout_s=None: (
        built.setdefault("timeout_s", timeout_s), object())[1])
    monkeypatch.setattr(submission_transport, "SubmissionTransport",
                        lambda providers, deadline_s: built.setdefault("deadline_s", deadline_s))
    monkeypatch.setattr(B, "_register_transport", lambda t: None)
    B.build_evm_backend(ETH, custody_rpc_url=INFURA, http=_Http())
    assert built == {"timeout_s": C._MAX_DEADLINE_MS / 1000.0,
                     "deadline_s": C._MAX_DEADLINE_MS / 1000.0}


@pytest.mark.parametrize("env, want", [("", 60), ("45", 45), ("300", 300), ("301", 300),
                                       ("86400", 300), ("0", 60), ("-5", 60), ("abc", 60)])
def test_the_chain_1_swap_deadline_has_an_upper_bound(monkeypatch, env, want):
    """LOW: the dead proof waits for whatever deadline was signed, so a large knob value would hold
    a reservation for that long. Host skew shifts the validity window, never the proof: the release
    compares CHAIN time with the decoded deadline."""
    monkeypatch.setenv("MEMECOIN_EVM_SWAP_DEADLINE_S_1", env)
    assert cfg.memecoin_evm_swap_deadline_s("1", 600) == want
    assert cfg.memecoin_evm_swap_deadline_s("4663", 600) == 600


# ================= step 3.2: the dead proof on chain 1 is PROTOCOL EXPIRY (spec v3 §3.7) =================
# A private transaction is invisible to custody's pending pool, so `pending == latest` proves nothing
# on chain 1. The only proof that signed swap bytes can no longer execute is their own deadline
# passing on CHAIN time. The deadline is decoded from the bound swap bytes by one allowlisted decoder
# (Gamma's design): an unknown selector or undecodable bytes give no proof, and the row is held.

DEADLINE = 1_790_770_000
MARGIN = 24


def _signed(data, *, chain_id=1, nonce=7, typed=False):
    acct = Account.from_key("0x" + "11" * 32)
    tx = {"to": "0x" + "22" * 20, "value": 0, "gas": 500000, "nonce": nonce, "chainId": chain_id,
          "data": data}
    tx.update({"maxFeePerGas": 10 ** 9, "maxPriorityFeePerGas": 10 ** 8, "type": 2} if typed
              else {"gasPrice": 10 ** 9})
    return "0x" + acct.sign_transaction(tx).raw_transaction.hex().removeprefix("0x")


def _swap_calldata(kind, deadline=DEADLINE):
    """One swap per route kind, from the FUNDED builders."""
    if kind == "v4":                                        # UniversalRouter.execute
        from sniper.execution.evm_v4_quote import _ur_execute_data
        return _ur_execute_data(bytes([0x10]), ["00" * 32], deadline)
    if kind == "v3":                                        # SwapRouter02 multicall(deadline, ...)
        from sniper.execution.evm_dex_quote import _with_expiry
        return _with_expiry(ETH, "0x04e45aaf" + "00" * 224, now_fn=lambda: deadline - 60)
    if kind == "v2":                                        # Router02 swapExactTokensFor...FOT
        from sniper.execution.evm_v2_exit_quote import _encode_swap
        return _encode_swap(10 ** 16, 1, [WETH, "0x" + "33" * 20], "0x" + "44" * 20, deadline)
    raise AssertionError(kind)


ROUTE_KINDS = ("v4", "v3", "v2")


@pytest.mark.parametrize("kind", ROUTE_KINDS)
@pytest.mark.parametrize("form", ["hex", "base64_of_hex", "typed"])
def test_the_deadline_the_builder_encoded_is_the_deadline_decoded(kind, form):
    import base64
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    raw = _signed(_swap_calldata(kind), typed=form == "typed")
    payload = base64.b64encode(raw.encode()).decode() if form == "base64_of_hex" else raw
    assert decode_signed_swap_deadline(payload) == DEADLINE


@pytest.mark.parametrize("data", [
    "0x04e45aaf" + "00" * 224,                              # bare exactInputSingle: NO deadline
    "0x2e1a7d4d" + "00" * 32,                               # WETH.withdraw
    "0x095ea7b3" + "00" * 64,                               # approve
    "0x24856bc3" + "00" * 128,                              # UR execute WITHOUT a deadline
    "0x3593564c" + "00" * 32,                               # truncated head
    "0x"])
def test_an_unknown_selector_or_a_short_head_gives_no_deadline(data):
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    assert decode_signed_swap_deadline(_signed(data)) is None


@pytest.mark.parametrize("payload", [None, "", "0xnothex", "zz", "0x" + "c0"])
def test_undecodable_bytes_give_no_deadline(payload):
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    assert decode_signed_swap_deadline(payload) is None


def test_an_implausible_deadline_is_no_deadline():
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    for bad in (0, 5, 2 ** 200):
        assert decode_signed_swap_deadline(_signed(_swap_calldata("v4", deadline=bad))) is None


def test_every_rung_the_chain_1_ladder_can_return_builds_a_swap_with_a_deadline(monkeypatch):
    """Gamma LOW on 4d8fc9cf5: a rung added to chain 1 later without a deadline would be held
    forever by the "unknown selector: hold" rule. It must fail here instead."""
    from sniper.execution import evm_real_seams as S
    from sniper.services.memecoin_real_runner import EXPIRY_PROVEN_RUNGS
    rungs = []
    ladder = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=ETH, quote_token=WETH, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        pons_v2_quote_fn=None, relay_quote_fn=lambda *a, **k: None, acct=object(),
        rpc_url="https://rpc", on_rung=rungs.append)
    monkeypatch.setattr("sniper.execution.evm_v4_multihop.dex_v4_multihop_quote",
                        lambda *a, **k: None)
    ladder(WETH, "0x" + "33" * 20, 10 ** 16)
    asked = {r["rung"] for r in rungs if not r.get("skipped") and not r.get("unavailable")}
    assert asked and asked <= set(EXPIRY_PROVEN_RUNGS), asked - set(EXPIRY_PROVEN_RUNGS)
    assert set(EXPIRY_PROVEN_RUNGS.values()) <= set(ROUTE_KINDS)      # each maps to a round-trip above


def _row(**over):
    row = {"id": 9, "network_id": ETH, "mint": "0x" + "33" * 20, "nonce": 7,
           "reserved_at": "2026-09-30T12:00:00+00:00", "broadcast_at": "2026-09-30T12:00:01+00:00",
           "signed_payload_b64": _signed(_swap_calldata("v4"))}
    row.update(over)
    return row


def _snap(ts, *, nonce=7, balance=0.0, block=19_000_000):
    return {"block": block, "ts": ts, "nonce": nonce, "balance": balance}


def _entry(row, *, ts, balance=0.0, pending=7, confirmed=7, ts_for="use", snap_nonce=7):
    """`ts`, `balance` and `snap_nonce` are ONE block's view (the proof snapshot) on chain 1; on
    another chain `balance` is the plain latest read and the snapshot is never asked."""
    from sniper.services import memecoin_real_runner as RR
    expiry = str(row.get("network_id")) == ETH

    def snapshot(net, mint):
        if callable(ts):
            return ts(net)
        if callable(balance):
            return balance(net, mint)
        return None if ts is None else (ts if isinstance(ts, (dict, bool)) else
                                        _snap(ts, nonce=snap_nonce, balance=balance))
    return RR.evm_entry_never_admitted(
        row, now_iso="2026-09-30T12:30:00+00:00", confirmed_nonce=confirmed,
        pending_nonce_for=lambda net: pending,
        token_balance_for=((lambda net, mint: pytest.fail("a latest-height read in the proof"))
                           if expiry else
                           (balance if callable(balance) else (lambda net, mint: balance))),
        proof_snapshot_for=(None if ts_for is None else snapshot))


def test_a_chain_1_entry_is_released_only_after_its_own_deadline_on_chain_time():
    assert _entry(_row(), ts=DEADLINE + MARGIN) is None      # 30 min old, pending == latest: held
    reason = _entry(_row(), ts=DEADLINE + MARGIN + 1)
    assert reason and reason.startswith("evm_entry_expired")
    assert str(DEADLINE) in reason and "holds none of the coin" in reason


def _raises(*_a):
    raise OSError("rpc down")


@pytest.mark.parametrize("case", ["coin_held", "balance_unreadable", "snapshot_raises", "ts_none",
                                  "no_snapshot_reader", "no_payload", "unknown_selector",
                                  "nonce_not_ours", "bool_ts", "landed_by_that_block",
                                  "nonce_behind_at_that_block", "snapshot_missing_a_field"])
def test_every_gap_in_the_chain_1_entry_proof_holds_the_row(case):
    late = DEADLINE + 10_000
    row, kw = _row(), {"ts": late}
    if case == "coin_held":
        kw["balance"] = 12.5
    if case == "balance_unreadable":
        kw["ts"] = _snap(late, balance=None)
    if case == "snapshot_raises":
        kw["ts"] = _raises
    if case == "ts_none":
        kw["ts"] = None
    if case == "bool_ts":
        kw["ts"] = _snap(True)
    if case == "no_snapshot_reader":
        kw["ts_for"] = None
    if case == "landed_by_that_block":
        kw["snap_nonce"] = 8                                   # the nonce is spent AT the proof's block
    if case == "nonce_behind_at_that_block":
        kw["snap_nonce"] = 6
    if case == "snapshot_missing_a_field":
        kw["ts"] = {"block": 1, "ts": late, "balance": 0.0}    # no nonce: not a proof
    if case == "no_payload":
        row = _row(signed_payload_b64=None)                  # a bound row with no payload: a hold
    if case == "unknown_selector":
        row = _row(signed_payload_b64=_signed("0x04e45aaf" + "00" * 224))
    if case == "nonce_not_ours":
        kw["confirmed"] = 6
    assert _entry(row, **kw) is None


def test_pending_equal_to_latest_is_not_evidence_on_chain_1_and_a_mismatch_does_not_block():
    """Custody cannot see a private transaction, so its pending count says nothing either way."""
    assert _entry(_row(), ts=DEADLINE + MARGIN + 1, pending=9)


def test_other_chains_keep_the_pending_nonce_proof(monkeypatch):
    row = _row(network_id="4663", signed_payload_b64=None)
    assert _entry(row, ts=None, ts_for=None).startswith("evm_entry_never_admitted")
    assert _entry(row, ts=None, ts_for=None, pending=8) is None
    # and never ask for a snapshot they do not need
    assert _entry(row, ts=lambda net: pytest.fail("a snapshot read off the expiry chains"))


def _close_pos(payload="use", **over):
    import base64
    raw = _signed(_swap_calldata("v4"), nonce=7)
    intent = {"nonce": 7, "balance_snapshot": 1000.0,
              "signed_payload_b64": (base64.b64encode(raw.encode()).decode()
                                     if payload == "use" else payload)}
    pos = {"id": 5, "network_id": ETH, "mint": "0x" + "33" * 20,
           "close_submitted_at": "2026-09-30T12:00:00+00:00",
           "exit_intent_json": _json.dumps(intent)}
    pos.update(over)
    return pos


def _close(pos, *, ts, mined=7, balance=1000.0, pending=7, with_ts=True):
    """On chain 1 `ts`, `mined` and `balance` are ONE block's view; the latest-height readers must
    not be asked. On another chain they are the plain latest reads."""
    from sniper.services import memecoin_real_runner as RR
    expiry = str(pos.get("network_id")) == ETH
    latest = (lambda *a: pytest.fail("a latest-height read in the chain-1 proof")) if expiry else None
    return RR.evm_close_provably_dead(
        pos, confirmed_nonce_fn=latest or (lambda: mined),
        balance_fn=latest or (lambda mint: balance),
        pending_nonce_fn=lambda: pending, native_balance_fn=lambda: 1.0,
        now_iso="2026-09-30T12:30:00+00:00",
        proof_snapshot_fn=((lambda mint: None if ts is None else
                            _snap(ts, nonce=mined, balance=balance)) if with_ts else None))


def test_a_chain_1_close_claim_is_released_only_after_the_sells_own_deadline():
    assert _close(_close_pos(), ts=DEADLINE + MARGIN) is None
    reason = _close(_close_pos(), ts=DEADLINE + MARGIN + 1)
    assert reason and "expired" in reason and str(DEADLINE) in reason


@pytest.mark.parametrize("case", ["no_payload", "unknown_selector", "no_ts_reader", "ts_none",
                                  "bag_reduced", "nonce_behind_the_intent"])
def test_every_gap_in_the_chain_1_close_proof_holds_the_claim(case):
    late, pos, kw = DEADLINE + 10_000, _close_pos(), {}
    if case == "no_payload":
        pos = _close_pos(payload=None)
    if case == "unknown_selector":
        import base64
        pos = _close_pos(payload=base64.b64encode(
            _signed("0x04e45aaf" + "00" * 224).encode()).decode())
    if case == "no_ts_reader":
        kw["with_ts"] = False
    if case == "ts_none":
        late = None
    if case == "bag_reduced":
        kw["balance"] = 400.0
    if case == "nonce_behind_the_intent":
        kw["mined"] = 6                        # Gamma LOW: dropping `mined_nonce != nonce` survived
    assert _close(pos, ts=late, **kw) is None


def test_the_nonce_exclusive_close_release_stays_on_chain_1():
    """Proof one needs no deadline: the nonce was spent by another transaction, the bag is whole."""
    reason = _close(_close_pos(), ts=DEADLINE - 50, mined=8)
    assert reason and reason.startswith("nonce 8 spent by another transaction")


def test_other_chains_keep_the_pending_nonce_close_proof_and_never_read_a_snapshot():
    from sniper.services import memecoin_real_runner as RR
    pos = _close_pos(network_id="4663")
    assert "latest == pending == intent nonce 7" in _close(pos, ts=None, with_ts=False)
    reason = RR.evm_close_provably_dead(
        pos, confirmed_nonce_fn=lambda: 7, balance_fn=lambda mint: 1000.0,
        pending_nonce_fn=lambda: 7, native_balance_fn=lambda: 1.0,
        now_iso="2026-09-30T12:30:00+00:00",
        proof_snapshot_fn=lambda mint: pytest.fail("an extra read on a chain that does not need it"))
    assert "latest == pending" in reason
    # Gamma LOW: a mined nonce BEHIND the intent proves nothing, on every chain
    assert _close(pos, ts=None, with_ts=False, mined=6, pending=6) is None


# --- Gamma MEDIUM on 5b87e0b1e: the proof's facts must come from ONE block ---
# eth_getBlockByNumber, eth_getTransactionCount and eth_call are all hedged reads, so at "latest"
# each could be answered by a different pool member at a different height. A chain time 24 s
# fresher than the nonce and the bag released a transaction that landed in between.

BLOCK = 0x121EAC0                                            # 19,000,000


class _Chain:
    """One chain, two heights: block BLOCK (fresh) and a member that does not have it yet."""

    def __init__(self, *, ts=DEADLINE + 100, nonce=7, balance=0, stale_member=False, decimals=18):
        self.ts, self.nonce, self.balance = ts, nonce, balance
        self.stale, self.decimals, self.calls = stale_member, decimals, []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        req = _json.loads(body)
        name, params = req["method"], req["params"]
        self.calls.append((name, params[-1] if params else None))
        if name == "eth_chainId":
            return 200, {"result": "0x1"}, None
        if name == "eth_getBlockByNumber":
            return 200, {"result": {"number": hex(BLOCK), "timestamp": hex(self.ts)}}, None
        if self.stale and params[-1] == hex(BLOCK):          # this member is behind: it says so
            return 200, {"error": {"code": -32000, "message": "header not found"}}, None
        if name == "eth_getTransactionCount":
            return 200, {"result": hex(self.nonce)}, None
        if name == "eth_call" and params[0]["data"].startswith("0x313ce567"):
            return 200, {"result": "0x" + format(self.decimals, "064x")}, None
        if name == "eth_call":
            return 200, {"result": "0x" + format(self.balance, "064x")}, None
        return 200, {"result": None}, None


def _snapshot(chain):
    from sniper.execution.evm_real_seams import eth_proof_snapshot
    return eth_proof_snapshot("0x" + "11" * 20, "0x" + "33" * 20, INFURA, http=chain)


def test_the_proof_snapshot_reads_the_nonce_and_the_balance_at_the_block_it_took_the_time_from():
    chain = _Chain(nonce=7, balance=5 * 10 ** 18)
    assert _snapshot(chain) == {"block": BLOCK, "ts": DEADLINE + 100, "nonce": 7, "balance": 5.0}
    at = {name: tag for name, tag in chain.calls if name != "eth_chainId"}
    assert at["eth_getBlockByNumber"] is False               # ["latest", False]
    assert at["eth_getTransactionCount"] == hex(BLOCK)
    balance_calls = [tag for name, tag in chain.calls if name == "eth_call"]
    assert hex(BLOCK) in balance_calls and "latest" not in [
        tag for name, tag in chain.calls if name == "eth_getTransactionCount"]
    assert [name for name, _t in chain.calls].count("eth_getBlockByNumber") == 1


def test_a_member_that_does_not_have_the_block_gives_no_snapshot():
    assert _snapshot(_Chain(stale_member=True)) is None


@pytest.mark.parametrize("broken", ["down", "no_timestamp", "no_decimals"])
def test_any_failed_read_gives_no_snapshot(broken):
    chain = _Chain()
    if broken == "down":
        assert _snapshot(lambda *a, **k: (_ for _ in ()).throw(OSError("down"))) is None
        return
    real = chain.__call__

    def http(method, url, headers=None, body=None, timeout=None):
        req = _json.loads(body)
        if broken == "no_timestamp" and req["method"] == "eth_getBlockByNumber":
            return 200, {"result": {"number": hex(BLOCK)}}, None
        if broken == "no_decimals" and req["method"] == "eth_call" and (
                req["params"][0]["data"].startswith("0x313ce567")):
            return 200, {"result": "0x"}, None
        return real(method, url, headers=headers, body=body, timeout=timeout)
    assert _snapshot(http) is None


def test_a_sell_that_landed_by_the_proofs_block_is_never_called_expired():
    """The case Gamma named. The sell landed: at the proof's block the nonce is spent and the bag
    is gone. A fresher chain time next to a staler nonce used to release it as expired."""
    assert _close(_close_pos(), ts=DEADLINE + 10_000, mined=8, balance=0.0) is None
    assert _entry(_row(), ts=DEADLINE + 10_000, snap_nonce=8, balance=12.5) is None


def test_the_context_exports_the_snapshot_and_the_recovery_paths_receive_it(monkeypatch):
    import inspect
    from pathlib import Path
    from sniper.execution.evm_real_seams import build_real_evm_executor
    from sniper.services import memecoin_real_runner as RR
    chain = _Chain(nonce=3, balance=0)
    ex = build_real_evm_executor(acct=Account.from_key("0x" + "11" * 32), chain_id=ETH,
                                 rpc_url=INFURA, http=chain, venue_quote_fn=lambda *a, **k: None)
    snap = ex["proof_snapshot_fn"]("0x" + "33" * 20)
    assert (snap["block"], snap["ts"], snap["nonce"], snap["balance"]) == (
        BLOCK, DEADLINE + 100, 3, 0.0)
    assert "latest_block_ts_fn" not in ex                    # one reader, not two
    assert RR.make_proof_snapshot_for({ETH: {"proof_snapshot_fn": lambda mint: mint}})(
        ETH, "0xabc") == "0xabc"
    assert RR.make_proof_snapshot_for({})(ETH, "0xabc") is None
    loop = (Path(RR.__file__).resolve().parents[2] / "scripts/memecoin_runner_loop.py").read_text()
    assert "proof_snapshot_for=_proof_snapshot_for" in loop
    assert 'proof_snapshot_fn=(ctx or {}).get("proof_snapshot_fn")' in inspect.getsource(RR)
    assert "proof_snapshot_for=proof_snapshot_for" in inspect.getsource(RR.recover_stale_reservations)


# --- Gamma LOW on 5b87e0b1e and 5d3fb9c1c: four surviving mutants ---

def test_an_unknown_selector_whose_first_word_looks_like_a_deadline_gives_no_deadline():
    """The earlier cases were saved by the range check, not by the allowlist."""
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    data = "0xdeadbeef" + format(DEADLINE, "064x") + format(DEADLINE, "064x") * 4
    assert decode_signed_swap_deadline(_signed(data)) is None


@pytest.mark.parametrize("env, want", [("", 24), ("30", 30), ("12", 12), ("5", 12), ("1", 12),
                                       ("0", 24), ("junk", 24)])
def test_the_expiry_margin_is_never_under_one_block(monkeypatch, env, want):
    monkeypatch.setenv("MEMECOIN_EVM_EXPIRY_MARGIN_S_1", env)
    assert cfg.memecoin_evm_expiry_margin_s(ETH) == want


def test_a_persist_gate_rejection_neither_extends_nor_ends_the_relay_streak(relay_pages):
    from sniper.execution.submission_transport import AttemptState, SubmissionResult
    from sniper.execution.submission_wiring import _note_private_relay, _relay_streak
    _send_n(["ambiguous", "ambiguous"])
    synthetic = SubmissionResult(AttemptState.EXPLICIT_REJECTED, "0xid", (), None)   # no provider asked
    _note_private_relay(ETH, synthetic)
    assert _relay_streak[ETH] == [2, False] and relay_pages == []
    _send_n(["ambiguous"])
    assert [kind for kind, _t in relay_pages] == ["RELAY-DOWN"]


# ===== the funded chain-1 parameters (operator 2026-09-30 through Alpha; code defaults, no .env) =====
# clip $20, round-trip cost cap 10%, gas ceiling 5 gwei, canary stop at -25% of the deployed clip
# from the 5th close. None of them arms anything: the pinned manifest still refuses every entry.

def test_the_chain_1_parameters_are_code_defaults_and_no_other_chain_moves(monkeypatch):
    for name in ("MEMECOIN_EVM_FIXED_CLIP_USD_1", "MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1",
                 "MEMECOIN_EVM_MAX_GAS_GWEI_1", "MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE_1",
                 "MEMECOIN_EVM_CANARY_STOP_RETURN_PCT_1"):
        monkeypatch.delenv(name, raising=False)
    assert cfg.evm_fixed_clip_usd_for(ETH) == 20.0
    assert cfg.evm_roundtrip_cost_cap_pct_for(ETH) == 10.0
    assert cfg.evm_max_gas_gwei_for(ETH) == 5.0
    assert cfg.evm_canary_stop_for(ETH) == (5, -25.0)
    for other in ("4663", "8453", "56", "5042", "57073"):
        assert cfg.evm_fixed_clip_usd_for(other) == 0.0                     # off: its own sizing mode
        assert cfg.evm_roundtrip_cost_cap_pct_for(other) == cfg.MEMECOIN_MAX_ROUNDTRIP_COST_PCT
        assert cfg.evm_max_gas_gwei_for(other) == 0.0                       # off
        assert cfg.evm_canary_stop_for(other) is None


@pytest.mark.parametrize("name, value, read, want", [
    ("MEMECOIN_EVM_FIXED_CLIP_USD_1", "35", "evm_fixed_clip_usd_for", 35.0),
    ("MEMECOIN_EVM_FIXED_CLIP_USD_1", "0", "evm_fixed_clip_usd_for", 0.0),          # 0 turns it off
    ("MEMECOIN_EVM_FIXED_CLIP_USD_1", "abc", "evm_fixed_clip_usd_for", 20.0),       # malformed: default
    ("MEMECOIN_EVM_FIXED_CLIP_USD_1", "-5", "evm_fixed_clip_usd_for", 20.0),
    ("MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1", "7.5", "evm_roundtrip_cost_cap_pct_for", 7.5),
    ("MEMECOIN_MAX_ROUNDTRIP_COST_PCT_1", "junk", "evm_roundtrip_cost_cap_pct_for", 10.0),
    ("MEMECOIN_EVM_MAX_GAS_GWEI_1", "3", "evm_max_gas_gwei_for", 3.0),
    ("MEMECOIN_EVM_MAX_GAS_GWEI_1", "0", "evm_max_gas_gwei_for", 0.0),              # 0 is off
    ("MEMECOIN_EVM_MAX_GAS_GWEI_1", "nan", "evm_max_gas_gwei_for", 5.0)])
def test_each_chain_1_parameter_has_a_suffix_scoped_override(monkeypatch, name, value, read, want):
    monkeypatch.setenv(name, value)
    assert getattr(cfg, read)(ETH) == want


def test_the_canary_stop_override_and_off_switch(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE_1", "8")
    monkeypatch.setenv("MEMECOIN_EVM_CANARY_STOP_RETURN_PCT_1", "-20")
    assert cfg.evm_canary_stop_for(ETH) == (8, -20.0)
    monkeypatch.setenv("MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE_1", "0")                # off
    assert cfg.evm_canary_stop_for(ETH) is None
    monkeypatch.setenv("MEMECOIN_EVM_CANARY_STOP_FROM_CLOSE_1", "5")
    monkeypatch.setenv("MEMECOIN_EVM_CANARY_STOP_RETURN_PCT_1", "15")               # a gain is no stop
    assert cfg.evm_canary_stop_for(ETH) == (5, -25.0)


def _ledger():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _closed(c, n, *, pnl, notional=20.0, network=ETH, source="follow_activity", mint=None):
    from sniper.persistence import db_memecoin as m
    for i in range(n):
        pid = m.open_position(
            c, source_user_id="u", source_handle="h", mint=mint or "0x%040x" % (1000 + i + len(
                c.execute("SELECT id FROM memecoin_positions").fetchall())),
            ticker="T", entry_price=1.0, entry_notional=notional, entry_fill_source="chain",
            entry_lag_s=1.0, their_size_usd=0.0, opened_at="2026-09-30T00:00:00Z",
            strategy="alpha_b", execution_mode="real", network_id=network, signal_source=source)
        m.close_position(c, pid, "2026-09-30T00:05:00Z", realized_pnl=pnl)


def _eth_ctx(c, *, weth_usd=300.0, **kw):
    from sniper.services import memecoin_evm_risk as ER
    return ER.build_evm_risk_context(c, usdc_balance=weth_usd, native_balance=1.0, day="2026-09-30",
                                     network_id=ETH, **kw)


@pytest.mark.parametrize("balance", [25.0, 300.0, 50_000.0])
def test_the_chain_1_clip_is_a_fixed_20_dollars_whatever_the_equity(monkeypatch, balance):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    ctx = _eth_ctx(_ledger(), weth_usd=balance)
    assert ctx.trade_size_usd == 20.0 and ctx.base_trade_size_usd == 20.0


def test_a_chain_1_balance_under_the_clip_refuses_and_never_shrinks_the_clip(monkeypatch):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    ctx = _eth_ctx(_ledger(), weth_usd=12.0)
    assert ctx.trade_size_usd == 20.0
    decision = ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd)
    assert not decision.allowed and "bankroll" in decision.reason.lower()


def test_the_regime_scale_still_shrinks_a_fixed_clip_and_a_manual_size_still_wins(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    assert _eth_ctx(_ledger(), risk_scale=0.5).trade_size_usd == 10.0
    assert _eth_ctx(_ledger(), size_override=7.0).trade_size_usd == 7.0


def test_other_chains_keep_their_sizing_mode(monkeypatch):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _ledger()
    with_knob = ER.build_evm_risk_context(c, usdc_balance=300.0, native_balance=1.0,
                                          day="2026-09-30", network_id="4663")
    monkeypatch.setattr(cfg, "evm_fixed_clip_usd_for", lambda chain: 0.0)     # the knob gone
    without = ER.build_evm_risk_context(c, usdc_balance=300.0, native_balance=1.0,
                                        day="2026-09-30", network_id="4663")
    assert with_knob.trade_size_usd == without.trade_size_usd != 20.0


@pytest.mark.parametrize("closes, pnl, stopped", [
    (4, -20.0, False),        # four total losses: not checked before the 5th close
    (5, -5.0, False),         # exactly -25% is not below it
    (5, -5.01, True),
    (5, -20.0, True),
    (9, 1.0, False)])
def test_the_canary_stop_is_checked_from_the_5th_close_on_the_cumulative_gross_return(
        monkeypatch, closes, pnl, stopped):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _ledger()
    _closed(c, closes, pnl=pnl)
    ctx = _eth_ctx(c)
    decision = ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd)
    assert (not decision.allowed) is stopped
    if stopped:
        assert decision.reason.startswith("chain 1 canary stop")
        assert "%d closes" % closes in decision.reason and "-25" in decision.reason


def test_the_canary_stop_counts_only_this_chains_automatic_real_closes(monkeypatch):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _ledger()
    _closed(c, 6, pnl=-20.0, network="4663")                 # another chain's losses
    _closed(c, 6, pnl=-20.0, source="manual")                # the operator's own buys
    _closed(c, 3, pnl=-20.0)                                 # only these count: 3 closes
    ctx = _eth_ctx(c)
    assert ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd).allowed
    assert ER.build_evm_risk_context(
        c, usdc_balance=300.0, native_balance=1.0, day="2026-09-30",
        network_id="4663").canary_stop_reason is None         # no canary on 4663


def test_a_close_with_unknown_pnl_is_not_a_zero_in_the_canary(monkeypatch):
    from sniper.persistence import db_memecoin as m
    c = _ledger()
    _closed(c, 5, pnl=-6.0)                                  # -30% on five closes: stopped
    assert m.evm_canary_book(c, ETH) == (5, -30.0, 100.0)
    c.execute("UPDATE memecoin_positions SET realized_pnl=NULL WHERE id=1")
    assert m.evm_canary_book(c, ETH) == (4, -24.0, 80.0)      # unknown is left out, never 0


def test_a_manual_chain_1_entry_is_not_refused_by_the_canary_stop(monkeypatch):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _ledger()
    _closed(c, 5, pnl=-20.0)
    ctx = _eth_ctx(c, enforce_concurrency=False)
    assert ctx.canary_stop_reason is None
    assert ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd).allowed


def test_an_operator_override_passes_a_tripped_canary_stop(monkeypatch):
    """Gamma LOW on b3257df0b: dropping `and not ctx.override_risk_caps` survived. Here the stop IS
    tripped in the context, and only the override lets the entry through."""
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _ledger()
    _closed(c, 5, pnl=-20.0)
    tripped = _eth_ctx(c)
    assert tripped.canary_stop_reason and "while it stays below" in tripped.canary_stop_reason
    assert not ER.evm_entry_allowed(tripped, mint="0x" + "33" * 20,
                                    trade_size=tripped.trade_size_usd).allowed
    override = _eth_ctx(c, override_risk_caps=True)
    assert override.canary_stop_reason                       # still tripped in the context
    assert ER.evm_entry_allowed(override, mint="0x" + "33" * 20,
                                trade_size=override.trade_size_usd).allowed


def test_the_canary_stop_lifts_by_itself_when_the_book_recovers(monkeypatch):
    """There is no latch: the stop re-reads the lifetime book. The refusal text must not promise
    an operator action that does not exist."""
    from sniper.services import memecoin_evm_risk as ER
    c = _ledger()
    _closed(c, 5, pnl=-6.0)                                  # -30%
    assert ER.canary_stop_reason(c, ETH)
    assert "operator lifts" not in ER.canary_stop_reason(c, ETH)
    _closed(c, 1, pnl=20.0)                                  # a good close: -10/120 = -8.3%
    assert ER.canary_stop_reason(c, ETH) is None


# ======================= step 5: gas on chain 1 (spec §4.3, Alpha's rulings 2026-09-30) =======================
# 1. The gas reserve natives of chain 1 are NOT invented: entries refuse while they are unset.
# 2. The gas guard: an entry refuses on an unreadable gas price, above the gwei ceiling, while the
#    round-trip gas units are unmeasured, and when the round trip's gas costs more than the cap.
# 3. The tip knob, the L1 fee reader, and the gas top-up scoped by chain.

GAS_NATIVES = ("MEMECOIN_EVM_GAS_ENTRY_NATIVE_1", "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_1",
               "MEMECOIN_EVM_GAS_EXIT_NATIVE_1")


@pytest.fixture(autouse=True)
def gas_natives(monkeypatch):
    """The three chain-1 reserve amounts, as the fork rehearsal will set them. Autouse for the
    whole file: a test about the UNSET state empties them itself."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"1": 0.0004})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"1": 0.0001})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"1": 0.0004})


def test_chain_1_names_its_unset_gas_reserve_amounts(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"1": 0.0001})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {})
    assert cfg.evm_gas_natives_unset(ETH) == (GAS_NATIVES[0], GAS_NATIVES[2])
    for other in ("4663", "8453", "56", "5042", "57073"):
        assert cfg.evm_gas_natives_unset(other) == ()         # they keep the global amounts


def test_a_chain_1_entry_refuses_while_its_gas_reserve_is_unset_even_a_manual_one(monkeypatch):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    for name in ("ENTRY", "APPROVAL", "EXIT"):
        monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_%s_NATIVE_BY_CHAIN" % name, {})
    for kw in ({}, {"enforce_concurrency": False}, {"override_risk_caps": True}):
        ctx = _eth_ctx(_ledger(), **kw)
        decision = ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd)
        assert not decision.allowed and decision.reason.startswith("chain 1 gas reserve is not set")
        assert "MEMECOIN_EVM_GAS_ENTRY_NATIVE_1" in decision.reason


def test_with_its_gas_reserve_set_chain_1_is_judged_on_the_balance(monkeypatch, gas_natives):
    from sniper.services import memecoin_evm_risk as ER
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    ctx = _eth_ctx(_ledger())
    assert ctx.gas_entry_native == 0.0004 and ctx.gas_reserve_unset == ()
    assert ER.evm_entry_allowed(ctx, mint="0x" + "33" * 20, trade_size=ctx.trade_size_usd).allowed


@pytest.mark.parametrize("env, want", [("", None), ("0", None), ("-5", None), ("abc", None),
                                       ("450000", 450000), ("450000.9", 450000)])
def test_the_round_trip_gas_units_are_unset_until_measured(monkeypatch, env, want):
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", env)
    assert cfg.evm_roundtrip_gas_units_for(ETH) == want
    assert cfg.evm_roundtrip_gas_units_for("4663") is None


GWEI = 10 ** 9


def _guard(monkeypatch, *, gas_gwei=0.5, units="450000", clip=20.0, eth_usd=2500.0, chain=ETH):
    from sniper.services.memecoin_evm_risk import evm_gas_guard_refusal
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", units)
    price = None if gas_gwei is None else int(gas_gwei * GWEI)
    # A plain V4 route: the class that falls back to the flat knob these cases set (2026-10-07).
    return evm_gas_guard_refusal(chain, gas_price_wei=price, clip_usd=clip, native_usd=eth_usd,
                                 route={"protocol": "uniswap_v4"})


def test_a_round_trip_inside_the_cost_cap_passes(monkeypatch):
    # 450,000 units x 0.5 gwei x $2,500 = $0.5625 = 2.81% of a $20 clip
    assert _guard(monkeypatch) is None


def test_a_round_trip_over_the_cost_cap_is_refused_with_its_numbers(monkeypatch):
    # 450,000 x 2 gwei x $2,500 = $2.25 = 11.25% of $20 (cap 10%)
    reason = _guard(monkeypatch, gas_gwei=2.0)
    assert reason == ("gas guard: round trip gas $2.25 is 11.2% of the $20.00 clip (cap 10.0%; "
                      "450000 units at 2.000 gwei)")
    assert _guard(monkeypatch, gas_gwei=1.77) is None                   # 9.96%: inside


def test_the_gwei_ceiling_is_a_spike_guard_checked_before_the_cost(monkeypatch):
    reason = _guard(monkeypatch, gas_gwei=5.01, units="1000")           # the cost alone would pass
    assert reason == "gas guard: gas 5.010 gwei is over the 5.0 gwei entry ceiling"
    assert _guard(monkeypatch, gas_gwei=5.0, units="1000") is None


@pytest.mark.parametrize("kw, text", [
    ({"gas_gwei": None}, "gas price unreadable"),
    ({"gas_gwei": 0.0}, "gas price unreadable"),
    ({"units": ""}, "round-trip gas units are not set (MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1_V4 or "
                    "MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1)"),
    ({"eth_usd": None}, "native USD price unknown"),
    ({"eth_usd": 0.0}, "native USD price unknown"),
    ({"clip": 0.0}, "clip unknown")])
def test_every_unknown_in_the_gas_guard_refuses_never_passes(monkeypatch, kw, text):
    reason = _guard(monkeypatch, **kw)
    assert reason and reason.startswith("gas guard: ") and text in reason


def test_a_chain_without_gas_parameters_has_no_gas_guard(monkeypatch):
    assert _guard(monkeypatch, chain="4663", gas_gwei=None, units="") is None
    assert cfg.evm_gas_guard_chains() == frozenset({ETH})


def test_the_executor_asks_the_gas_guard_before_the_first_wallet_mutation():
    import inspect
    from sniper.execution import evm_swap_executor as X2
    src = inspect.getsource(X2.execute_entry_evm)
    at = src.index("gas_guard_fn(usd_amount)")
    assert src.index("safety_gate_fn()") < at
    for mutation in ("approve_fn(", "sign_fn(", "send_fn("):
        assert at < src.index(mutation), mutation


def test_a_gas_guard_refusal_releases_the_reservation_and_touches_nothing():
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    c = _c()
    pid = _reserve(c, network_id=ETH)
    seams, calls = _seams(quote=_QUOTE)
    asked = []
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id=ETH, usd_amount=20.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-30T00:00:01Z",
        gas_guard_fn=lambda usd: asked.append(usd) or "gas guard: gas 9.000 gwei is over the 5.0 "
                                                        "gwei entry ceiling", **seams)
    assert asked == [20.0]
    assert (result["status"], result["reason"]) == (
        "aborted", "gas guard: gas 9.000 gwei is over the 5.0 gwei entry ceiling")
    assert calls["approve"] == [] and calls["sign"] == 0 and calls["send"] == 0
    row = m.pending_row(c, pid)
    assert row["status"] != "reserved" and row["tx_sig"] is None


def test_a_passing_gas_guard_changes_nothing_in_the_entry():
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    c = _c()
    pid = _reserve(c, network_id=ETH)
    seams, calls = _seams(quote=_QUOTE)
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id=ETH, usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-09-30T00:00:01Z", gas_guard_fn=lambda usd: None,
        **seams)
    assert result["status"] == "filled" and calls["send"] == 1


class _GasHttp(_Http):
    def __init__(self, gas_wei):
        super().__init__()
        self.gas_wei = gas_wei

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        req = _json.loads(body)
        if req["method"] == "eth_gasPrice":
            self.posts.append((url, req["method"]))
            return 200, {"result": hex(self.gas_wei)}, None
        return super().__call__(method, url, headers=headers, body=body, timeout=timeout)


def _executor(chain, http, **kw):
    from sniper.execution.evm_real_seams import build_real_evm_executor
    return build_real_evm_executor(acct=Account.from_key("0x" + "11" * 32), chain_id=chain,
                                   rpc_url=INFURA, http=http,
                                   venue_quote_fn=lambda *a, **k: None, **kw)


def test_the_chain_1_context_wires_the_guard_and_its_ceiling_reads_the_expected_price(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", "450000")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.3")
    http = _GasHttp(int(6 * GWEI))
    ex = _executor(ETH, http)
    reason = ex["gas_guard_fn"](20.0, native_usd=2500.0, route={"protocol": "uniswap_v4"})
    assert reason and "entry ceiling" in reason and "gwei is over the 5.0" in reason
    calm = _executor(ETH, _GasHttp(int(0.25 * GWEI)))
    assert calm["gas_guard_fn"](20.0, native_usd=2500.0, route={"protocol": "uniswap_v4"}) is None
    assert _executor("4663", _GasHttp(int(6 * GWEI))).get("gas_guard_fn") is None


# BUNKER 93e8a712 (2026-10-08): refused "round trip gas $5.86 is 11.7% of the $50.00 clip (cap 10.0%;
# 638162 units at 3.642 gwei)" -- the guard priced the SIGNING price (raw x 1.5 buffer + tip). A type-2
# transaction pays base + tip; that is ~2.52 gwei here, $4.05, 8.1%. Native ~$2,521, as the refusal implies.
BUNKER_RAW, BUNKER_TIP_GWEI, BUNKER_NATIVE = int(2.2457 * GWEI), "0.2735", 2521.0
HOOKED = {"protocol": "uniswap_v4", "hooked": True}


def _bunker(monkeypatch, type2_chains):
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", type2_chains)
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", BUNKER_TIP_GWEI)
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1_V4_HOOKED", "638162")
    monkeypatch.setattr(cfg, "memecoin_chain1_fee_headroom", lambda: False)
    ex = _executor(ETH, _GasHttp(BUNKER_RAW))
    facts = {}
    return ex["gas_guard_fn"](50.0, native_usd=BUNKER_NATIVE, facts=facts, route=HOOKED), facts


def test_bunker_passes_at_its_expected_type2_cost_and_the_trace_keeps_the_signed_price(monkeypatch):
    reason, facts = _bunker(monkeypatch, "1")
    assert reason is None
    assert facts["gwei"] == pytest.approx(2.2457 + 0.2735, abs=1e-6)          # base + tip
    assert facts["signed_gwei"] == pytest.approx(2.2457 * 1.5 + 0.2735, abs=1e-6)
    assert facts["gas_price_basis"] == "expected_type2"
    assert facts["share_pct"] == pytest.approx(8.11, abs=0.02) and facts["units"] == 638162


def test_a_legacy_chain_1_keeps_the_signed_price_and_refuses_bunker(monkeypatch):
    """MEMECOIN_EVM_TYPE2_CHAINS="" signs legacy, which pays the whole signed gas price."""
    reason, facts = _bunker(monkeypatch, "")
    assert reason and "11.7% of the $50.00 clip" in reason
    assert facts["gwei"] == facts["signed_gwei"] and facts["gas_price_basis"] == "signed_legacy"


def test_the_tip_is_added_to_the_buffered_gas_price_on_chain_1_only(monkeypatch):
    base = int(0.25 * GWEI)
    monkeypatch.delenv("MEMECOIN_EVM_TIP_GWEI_1", raising=False)
    plain = _executor(ETH, _GasHttp(base))["gas_price_fn"]()
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.3")
    tipped = _executor(ETH, _GasHttp(base))["gas_price_fn"]()
    assert tipped - plain == int(0.3 * GWEI) and cfg.evm_tip_gwei_for(ETH) == 0.3
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_4663", "0.3")            # only chain 1 has a default home
    assert cfg.evm_tip_gwei_for("4663") == 0.3
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "-1")
    assert cfg.evm_tip_gwei_for(ETH) == 0.0


# --- the L1 fee reader: gasUsed x effectiveGasPrice, nothing else, from the canonical receipt ---

def test_the_l1_fee_is_gas_used_times_effective_gas_price():
    from sniper.execution import evm_op_fees as F
    fee = F.l1_tx_fee({"gasUsed": hex(210_000), "effectiveGasPrice": hex(int(0.4 * GWEI)),
                       "blockNumber": hex(19_000_000)})
    total = 210_000 * int(0.4 * GWEI)
    assert fee == {"l2_execution_wei": str(total), "l1_data_wei": None, "operator_wei": None,
                   "total_wei": str(total), "status": "complete"}
    assert ETH in F.L1_FEE_CHAINS and not (F.L1_FEE_CHAINS & F.OP_STACK_FEE_CHAINS)


@pytest.mark.parametrize("receipt", [{}, {"gasUsed": "0x5208"}, {"effectiveGasPrice": "0x1"},
                                     {"gasUsed": "junk", "effectiveGasPrice": "0x1"}])
def test_a_receipt_without_both_fields_is_unknown_never_zero(receipt):
    from sniper.execution import evm_op_fees as F
    fee = F.l1_tx_fee(receipt)
    assert fee["total_wei"] is None and fee["status"] == "unknown:execution"


def test_the_chain_1_context_carries_the_fee_reader_and_the_leg_reconciles_with_it(monkeypatch):
    from sniper.execution import evm_op_fees as F
    seen = []
    monkeypatch.setattr(F, "read_l1_tx_fee", lambda tx_hash, rpc_url, http=None: (
        seen.append((tx_hash, rpc_url)) or {"status": "complete", "total_wei": "1"}))
    ex = _executor(ETH, _Http())
    assert ex["op_fee_fn"]("0xabc") == {"status": "complete", "total_wei": "1"}
    assert seen == [("0xabc", INFURA)]
    assert _executor("4663", _Http()).get("op_fee_fn") is None


# --- the gas top-up, scoped by chain (Alpha ruling 2) ---

def test_the_gas_top_up_runs_on_the_named_chains_and_chain_1_needs_its_own_levels(monkeypatch):
    assert cfg.memecoin_evm_gas_topup_chains() == ("4663",)
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663, 1")
    assert cfg.memecoin_evm_gas_topup_chains() == ("4663", "1")
    for name in ("LOW", "TARGET", "MIN_WETH_RESERVE"):
        monkeypatch.delenv("MEMECOIN_EVM_GAS_TOPUP_%s_NATIVE_1" % name, raising=False)
    assert cfg.evm_gas_topup_levels_for(ETH) is None             # absent is not zero, not 4663's
    assert cfg.evm_gas_topup_levels_for("4663") == {
        "low_wei": cfg.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI, "target_wei": cfg.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI,
        "min_weth_reserve_wei": cfg.MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI,
        "daily_cap_wei": cfg.MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI}
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE_1", "0.01")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE_1", "0.02")
    assert cfg.evm_gas_topup_levels_for(ETH) is None             # one of three still missing
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_1", "0.03")
    assert cfg.evm_gas_topup_levels_for(ETH) == {
        "low_wei": 10 ** 16, "target_wei": 2 * 10 ** 16, "min_weth_reserve_wei": 3 * 10 ** 16,
        "daily_cap_wei": 0}
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE_1", "0.005")    # target under low
    assert cfg.evm_gas_topup_levels_for(ETH) is None


def _loop():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"
    spec = importlib.util.spec_from_file_location("memecoin_runner_loop_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_loop_tops_up_each_named_chain_with_its_own_levels_and_skips_an_unset_one(monkeypatch):
    loop = _loop()
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663,1")
    for name in ("LOW", "TARGET", "MIN_WETH_RESERVE"):
        monkeypatch.delenv("MEMECOIN_EVM_GAS_TOPUP_%s_NATIVE_1" % name, raising=False)
    calls = []

    def topup(conn, *, context, now_iso, **levels):
        calls.append((context["chain"], levels))
        return {"state": "above_low_water", "chain_id": context["chain"]}
    contexts = {"4663": {"chain": "4663"}, ETH: {"chain": ETH}}
    out = loop.run_evm_gas_topup_pass(None, contexts=contexts, now_iso="2026-09-30T00:00:00Z",
                                      topup_fn=topup)
    assert [c for c, _l in calls] == ["4663"]                     # chain 1: levels unset, no pass
    assert out["state"] == "1:levels_unset"
    assert out["chains"][ETH] == {"state": "levels_unset", "chain_id": ETH}
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE_1", "0.01")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE_1", "0.02")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_1", "0.03")
    calls.clear()
    loop.run_evm_gas_topup_pass(None, contexts=contexts, now_iso="2026-09-30T00:00:00Z",
                                topup_fn=topup)
    by_chain = dict(calls)
    assert sorted(by_chain) == [ETH, "4663"]
    assert by_chain[ETH]["low_water_wei"] == 10 ** 16
    assert by_chain[ETH]["hold_under_reservation"] is True
    assert "hold_under_reservation" not in by_chain["4663"]      # 4663 is exactly as before


def test_the_top_up_never_touches_a_balance_under_an_in_flight_reservation():
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_evm_gas_topup as T
    c = _ledger()
    m.reserve_pending(c, mint="0x" + "44" * 20, source_user_id="u", strategy="alpha_b",
                      notional=20.0, reserved_at="2026-09-30T00:00:00Z", network_id=ETH)
    touched = []
    fail = lambda *a, **k: touched.append(1) or pytest.fail("the top-up read or sent under a reservation")  # noqa: E731
    seams = {name: fail for name in ("native_balance_raw_fn", "weth_balance_raw_fn", "nonce_fn",
                                     "gas_price_fn", "sign_fn", "send_fn", "confirm_once_fn")}
    seams.update(weth_token=WETH, rpc_url=INFURA)
    context = {"chain": ETH, "address": "0x" + "11" * 20, "gas_topup": seams}
    levels = dict(low_water_wei=10 ** 16, target_wei=2 * 10 ** 16, min_weth_reserve_wei=0,
                  daily_cap_wei=0)
    out = T.run_evm_gas_topup_pass(c, context=context, now_iso="2026-09-30T00:00:05Z",
                                   hold_under_reservation=True, **levels)
    assert out == {"state": "reservation_in_flight", "chain_id": ETH, "reservations": 1}
    assert touched == []


# --- Gamma LOW on 808b0bc37 ---

def test_the_gas_guard_is_wired_with_the_entrys_own_eth_usd_never_a_constant(monkeypatch):
    """`native_usd=1.0` instead of the entry's quote-token USD survived every test, and then the
    10% cost cap could never bind. The executor's guard is called here as the entry calls it."""
    from sniper.execution import evm_swap_executor as X2
    from sniper.persistence import db_memecoin as m
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", "450000")
    monkeypatch.delenv("MEMECOIN_EVM_TIP_GWEI_1", raising=False)
    captured = {}

    def fake_entry(conn, **kw):
        captured.update(kw)
        return {"status": "aborted", "reason": "test stop"}
    monkeypatch.setattr(X2, "execute_entry_evm", fake_entry)
    c = _ledger()
    pid = m.reserve_pending(c, mint="0x" + "33" * 20, source_user_id="u", strategy="alpha_b",
                            notional=20.0, reserved_at="2026-09-30T00:00:00Z", network_id=ETH)
    ex = _executor(ETH, _GasHttp(int(2 * GWEI)), price_fn=lambda: 2500.0, usdc_fn=lambda: 100.0,
                   token_fn=lambda mint: 0.0)
    ex["execute_fn"](c, pid, "0x" + "33" * 20, 20.0, "2026-09-30T00:00:01Z")
    assert captured["sell_token_usd"] == 2500.0
    reason = captured["gas_guard_fn"](20.0, route={"protocol": "uniswap_v4"})
    # 450,000 units x the price the entry signs with x $2,500: over 10% of $20 only at ETH's price
    assert reason and reason.startswith("gas guard: round trip gas $") and "of the $20.00 clip" in reason
    dollars = float(reason.split("$")[1].split(" ")[0])
    assert dollars == pytest.approx(450_000 * ex["gas_price_fn"]() / 1e18 * 2500.0, abs=0.01), reason
    assert dollars > 2.0                                     # at $1 per ETH it would be $0.001


def test_chain_1_quotes_in_wrapped_native_which_the_gas_guard_relies_on(monkeypatch):
    """The guard prices gas with the entry's quote-token USD. That is ETH/USD only while chain 1's
    quote token is WETH. A later change of the quote token must fail loudly, not misprice gas."""
    from sniper.execution import evm_wallet as W2
    assert W2.CHAIN_QUOTE[ETH]["kind"] == "eth" and W2.CHAIN_QUOTE[ETH]["token"].lower() == WETH
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", "450000")
    monkeypatch.setitem(W2.CHAIN_QUOTE, ETH, {"token": USDC, "decimals": 6, "kind": "usd"})
    ex = _executor(ETH, _GasHttp(int(0.25 * GWEI)))
    reason = ex["gas_guard_fn"](20.0, native_usd=1.0)
    assert reason == "gas guard: chain 1 quote token is not wrapped native, so the native price is unknown"


def test_topup_results_identify_only_named_chains(monkeypatch):
    loop = _loop()
    calls = []
    def topup(conn, *, context, now_iso, **levels):
        calls.append(context["chain"])
        return {"state": "above_low_water", "chain_id": context["chain"]}
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "1")
    out = loop.run_evm_gas_topup_pass(None, contexts={"4663": {"chain": "4663"}},
                                    now_iso="2026-09-30T00:00:00Z", topup_fn=topup)
    assert calls == [] and out == {"state": "context_unavailable", "chain_id": "1"}
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663")
    out = loop.run_evm_gas_topup_pass(None, contexts={"4663": {"chain": "4663"}},
                                    now_iso="2026-09-30T00:00:00Z", topup_fn=topup)
    assert calls == ["4663"] and out == {"state": "above_low_water", "chain_id": "4663"}



# --- found by step 7: the snapshot's writer and reader never met ---

def test_the_production_chain_1_context_hands_the_recovery_its_proof_snapshot(monkeypatch, tmp_path):
    """`build_real_evm_executor` builds `proof_snapshot_fn` and the runner reads it from the
    CONTEXT (`make_proof_snapshot_for`, `recover_unverified_closes`), but `build_real_evm_context`
    dropped the key. On chain 1 every expiry proof would have had no snapshot: an expired entry
    and an expired sell held forever. The test above built its own context dict and missed it."""
    from sniper.execution.evm_wallet import generate_keypair_file
    from sniper.services import memecoin_evm_leg as leg
    from sniper.services import memecoin_real_runner as RR
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_1", INFURA)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_1", "direct")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    ctx = leg.build_real_evm_context(ETH, http=_Chain(nonce=3, balance=0))
    assert ctx is not None and ctx["chain"] == ETH
    snap = RR.make_proof_snapshot_for({ETH: ctx})(ETH, "0x" + "33" * 20)
    assert snap == {"block": BLOCK, "ts": DEADLINE + 100, "nonce": 3, "balance": 0.0}


# --- found by step 7: mainnet's Universal Router takes the UPSTREAM four-field ExactInputParams ---
# The path form (hooked pools) was written for 4663, whose router has a fifth field, an empty
# `bytes` between the path and the amounts. Mainnet's router (0x66a9893c) read our amountIn from
# the slot that held the bytes offset (0x1a0 = 416 wei) and our amountIn as the minimum: every
# hooked buy reverted V4TooLittleReceived(8e15, ~4.8e8). eth_call on mainnet state, 2026-09-30:
# 6 of 6 hooked pools over 5 hooks revert on five fields and execute on four.

_V4_HOOKED_KEY = {"currency0": "0x" + "00" * 20, "currency1": "0x" + "5e" * 20, "fee": 0,
                  "tick_spacing": 200, "hooks": "0x" + "b8" * 20}
_FIVE_FIELDS = "(address,(address,uint24,int24,address,bytes)[],bytes,uint128,uint128)"
_FOUR_FIELDS = "(address,(address,uint24,int24,address,bytes)[],uint128,uint128)"


def _path_params(chain, **kw):
    from eth_abi import decode
    import sniper.execution.evm_v4_quote as Q
    packed = Q._v4_swap_input_path(_V4_HOOKED_KEY, True, 8 * 10 ** 15, 123, chain=chain, **kw)
    actions, params = decode(["bytes", "bytes[]"], bytes.fromhex(packed))
    assert actions == bytes([0x07, 0x0B, 0x0E])
    return params


def test_the_chain_1_path_form_is_byte_equal_to_the_upstream_four_field_struct():
    from eth_abi import encode
    key = _V4_HOOKED_KEY
    hop = [(key["currency1"], 0, 200, key["hooks"], b"")]
    params = _path_params(ETH, payer_is_user=False, take_to="0x" + "7a" * 20)
    assert params[0] == encode([_FOUR_FIELDS], [(key["currency0"], hop, 8 * 10 ** 15, 123)])
    for chain in ("4663", "5042", "56"):                        # Base already uses four fields
        other = _path_params(chain, payer_is_user=False, take_to="0x" + "7a" * 20)
        assert other[0] == encode([_FIVE_FIELDS], [(key["currency0"], hop, b"", 8 * 10 ** 15, 123)])
        assert other[1:] == params[1:]                          # SETTLE and TAKE do not differ


def test_the_path_form_cannot_be_encoded_without_naming_its_chain():
    import sniper.execution.evm_v4_quote as Q
    with pytest.raises(TypeError):
        Q._v4_swap_input_path(_V4_HOOKED_KEY, True, 1, 1, payer_is_user=False, take_to="0x" + "7a" * 20)


def test_a_chain_1_hooked_buy_carries_the_four_field_struct(monkeypatch):
    from eth_abi import decode
    import sniper.execution.evm_v4_quote as Q
    weth, taker = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0x" + "7a" * 20
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 9 * 10 ** 21, "pool_id": "0x" + "ff" * 32, "pool_key": _V4_HOOKED_KEY,
        "zero_for_one": True, "quote_currency": _V4_HOOKED_KEY["currency0"]})
    quote = Q.dex_v4_quote(ETH, weth, _V4_HOOKED_KEY["currency1"], 8 * 10 ** 15, taker,
                           rpc_url=INFURA, now_fn=lambda: 1_800_000_000)
    _commands, inputs, _deadline = decode(["bytes", "bytes[]", "uint256"],
                                          bytes.fromhex(quote["transaction"]["data"][10:]))
    _actions, params = decode(["bytes", "bytes[]"], inputs[-1])
    (currency_in, _path, amount_in, minimum), = decode([_FOUR_FIELDS], params[0])
    assert (int(currency_in, 16), amount_in) == (0, 8 * 10 ** 15) and 0 < minimum < 9 * 10 ** 21


# --- the gas guard's numbers on the trace (Alpha 2026-10-07: a PASS recorded nothing) -----------

def _facts(monkeypatch, **kw):
    from sniper.services.memecoin_evm_risk import evm_gas_guard_refusal
    gas_gwei, units = kw.pop("gas_gwei", 0.5), kw.pop("units", "450000")
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", units)
    facts = {}
    reason = evm_gas_guard_refusal(ETH, gas_price_wei=None if gas_gwei is None else int(gas_gwei * GWEI),
                                   clip_usd=kw.pop("clip", 20.0), native_usd=kw.pop("eth_usd", 2500.0),
                                   facts=facts, route={"protocol": "uniswap_v4"})
    return reason, facts


def test_a_passing_gas_guard_fills_its_numbers(monkeypatch):
    reason, facts = _facts(monkeypatch)
    assert reason is None
    assert facts["units"] == 450000 and facts["gwei"] == 0.5 and facts["cap_pct"] == 10.0
    assert facts["cost_usd"] == pytest.approx(0.5625) and facts["share_pct"] == pytest.approx(2.8125)
    assert facts["clip_usd"] == 20.0 and facts["native_usd"] == 2500.0 and facts["ceiling_gwei"] == 5.0


def test_a_refusal_fills_what_it_computed_and_leaves_the_unknown_out(monkeypatch):
    reason, facts = _facts(monkeypatch, gas_gwei=2.0)
    assert reason.startswith("gas guard: round trip gas $2.25") and facts["share_pct"] == pytest.approx(11.25)
    reason, facts = _facts(monkeypatch, gas_gwei=5.01, units="1000")
    assert "entry ceiling" in reason and facts["gwei"] == pytest.approx(5.01) and "units" not in facts
    reason, facts = _facts(monkeypatch, gas_gwei=None)
    assert "unreadable" in reason and facts == {}
    reason, facts = _facts(monkeypatch, units="")
    assert "units are not set" in reason and "units" not in facts and "gwei" in facts
    reason, facts = _facts(monkeypatch, eth_usd=None)
    assert "native USD" in reason and facts["units"] == 450000 and "native_usd" not in facts
    assert "cost_usd" not in facts and "share_pct" not in facts


@pytest.mark.parametrize("verdict", [None, "gas guard: gas 9.000 gwei is over the 5.0 gwei entry ceiling"])
def test_a_tagged_guard_puts_its_numbers_on_the_gas_guard_substage(verdict):
    from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    c = _c()
    pid = _reserve(c, network_id=ETH)
    seams, _calls = _seams(quote=_QUOTE)

    def guard(usd, facts=None):
        facts.update(units=622791, gwei=0.4, cost_usd=0.62, share_pct=3.1, cap_pct=10.0)
        return verdict
    guard.fills_facts = True
    trace = _TraceRecorder()
    execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id=ETH, usd_amount=20.0, strategy="alpha_b",
                      source_user_id="trader1", now_iso="2026-09-30T00:00:01Z", gas_guard_fn=guard,
                      trace=trace, **seams)
    (meta,) = [m for n, m in trace.substages if n == "evm.gas_guard"]
    assert meta["outcome"] == ("refused" if verdict else "passed") and meta["units"] == 622791
    assert meta["share_pct"] == 3.1 and meta["cap_pct"] == 10.0


def test_the_seams_per_entry_guard_forwards_the_facts_and_is_tagged(monkeypatch):
    import inspect
    from sniper.execution import evm_real_seams as seams
    monkeypatch.setenv("MEMECOIN_EVM_ROUNDTRIP_GAS_UNITS_1", "450000")
    ex = _executor(ETH, _GasHttp(int(0.25 * GWEI)))
    facts = {}
    assert ex["gas_guard_fn"](20.0, native_usd=2500.0, facts=facts, route={"protocol": "uniswap_v4"}) is None and facts["units"] == 450000
    assert "fills_facts = True" in inspect.getsource(seams)
