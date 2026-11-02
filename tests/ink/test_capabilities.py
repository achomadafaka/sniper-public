from scripts.research.ink_capability_probe import READ_METHODS, _read, probe, validate_chain_reply
import json
from pathlib import Path
from urllib.error import HTTPError


def test_wrong_chain_is_not_a_usable_provider():
    result = validate_chain_reply({"jsonrpc": "2.0", "id": 1, "result": hex(8453)})
    assert result == {"capability": "wrong_chain", "usable": False}


def test_rpc_error_is_not_empty_success():
    result = validate_chain_reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})
    assert result == {"capability": "rpc_error", "usable": False}


def test_http_provider_refusal_keeps_status_not_empty_success():
    def rpc(_method, _params):
        raise HTTPError("https://public.invalid", 403, "forbidden", {}, None)
    result = _read(rpc, "eth_chainId", [])
    assert result["capability"] == "http_403"
    assert result["result"] is None


def test_probe_uses_only_read_methods_and_refuses_wrong_chain():
    calls = []

    def rpc(method, params):
        calls.append(method)
        return {"jsonrpc": "2.0", "result": "0x2105"}

    report = probe(rpc, url="https://example.invalid")
    assert calls == ["eth_chainId"]
    assert set(READ_METHODS) == {
        "eth_chainId", "eth_getBlockByNumber", "eth_getCode", "eth_call",
        "eth_getTransactionReceipt", "eth_getLogs", "eth_getStorageAt",
        "debug_traceTransaction",
    }
    assert report["provider"]["usable"] is False
    assert report["provider"]["capability"] == "wrong_chain"


def test_probe_records_code_and_proxy_implementation_without_certifying_route():
    calls = []
    implementation = "0x" + "a" * 40

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_chainId":
            return {"result": hex(57073)}
        if method == "eth_getBlockByNumber":
            return {"result": {"number": "0x100", "hash": "0x" + "f" * 64}}
        if method == "eth_getCode":
            return {"result": "0x6000" if params[0] != implementation else "0x6001"}
        if method == "eth_getStorageAt":
            return {"result": "0x" + "0" * 24 + implementation[2:]}
        return {"result": "0x1"}

    report = probe(rpc, url="https://example.invalid")
    row = report["contracts"]["v3_router"]
    assert row["code_sha256"] is not None
    assert row["proxy_implementation"] == implementation
    assert row["implementation_code_sha256"] is not None
    assert any(method == "eth_getStorageAt" for method, _ in calls)
    assert all(params[-1] == "0x100" for method, params in calls
               if method in {"eth_getCode", "eth_getStorageAt"})


def test_manifest_exposes_unverified_route_classes():
    path = Path("docs/superpowers/research/2026-09-29-ink-coverage-manifest.json")
    manifest = json.loads(path.read_text())
    assert manifest["chain_id"] == 57073
    assert manifest["funded_activation"] == "blocked"
    assert {row["route_class"] for row in manifest["venues"]} >= {
        "uniswap_v3", "uniswap_v4_hookless", "uniswap_v2", "velodrome", "unknown_launchpads",
    }
    for row in manifest["venues"]:
        assert row["state"] in {"supported_tested", "policy_disabled", "unsupported"}
        assert row["entry_proof"] and row["exit_proof"] and row["simulation_proof"]
        assert row["covered_blocks"] is not None
        if row["state"] == "unsupported":
            assert row["unsupported_reason"]


def test_v3_code_fingerprints_match_both_recorded_providers():
    root = Path("docs/superpowers/research/2026-09-29-ink-evidence")
    manifest = json.loads(Path(
        "docs/superpowers/research/2026-09-29-ink-coverage-manifest.json").read_text())
    v3 = next(row for row in manifest["venues"] if row["route_class"] == "uniswap_v3")
    for provider in ("gelato", "quicknode"):
        report = json.loads((root / f"{provider}-code-provenance.json").read_text())
        assert report["provider"]["usable"] is True
        assert int(report["state_block"]["number"], 16) == v3["code_observed_blocks"][provider]
        for name in ("factory", "router", "quoter"):
            assert report["contracts"][f"v3_{name}"]["code_sha256"] == (
                v3["observed_code_sha256"][name])
