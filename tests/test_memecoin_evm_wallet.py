"""Item 5 — EVM custody (S4-EVM). secp256k1 keypair gen/load (0600, outside repo) + per-chain
balance reads over plain JSON-RPC: native gas (ETH-on-Base / BNB) + ERC-20 (USDC + token) via
eth_call. Separate wallet from Solana; wallet=bankroll per wallet. Injected http -> no live endpoint.
Fail-closed: any malformed response -> 0.0 (never a phantom balance that could authorize a trade)."""
import json
import os
import stat

from sniper.execution import evm_wallet as W


def _fake_http(responses):
    """responses: list of result-hex strings (or dicts) returned in order for each JSON-RPC call."""
    calls = {"i": 0, "bodies": []}

    def http(method, url, headers=None, body=None, timeout=10):
        calls["bodies"].append(json.loads(body))
        r = responses[min(calls["i"], len(responses) - 1)]; calls["i"] += 1
        return 200, ({"result": r} if isinstance(r, str) else r), None
    return http, calls


def test_keypair_roundtrip_and_perms(tmp_path):
    p = str(tmp_path / "evm_kp.json")
    acct = W.generate_keypair_file(p)
    assert W.address_str(acct).startswith("0x") and len(W.address_str(acct)) == 42
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600          # secret not world-readable
    loaded = W.load_keypair(p)
    assert W.address_str(loaded) == W.address_str(acct)        # same address after reload
    assert loaded.key == acct.key


def test_native_balance_parses_wei():
    http, _ = _fake_http(["0x" + format(2 * 10**18, "x")])     # 2 ETH/BNB in wei
    assert abs(W.native_balance("0xabc", "https://rpc", http=http) - 2.0) < 1e-9


def test_native_balance_fail_closed():
    http, _ = _fake_http([{"error": {"code": -32000}}])
    assert W.native_balance("0xabc", "https://rpc", http=http) == 0.0


def test_native_balance_or_none_success_first_try():
    http, calls = _fake_http(["0x" + format(3 * 10**18, "x")])
    v = W.native_balance_or_none("0xabc", "https://rpc", http=http, backoff_s=0)
    assert abs(v - 3.0) < 1e-9
    assert calls["i"] == 1                                     # no retry when the read worked


def test_native_balance_or_none_retries_then_succeeds():
    # first read fails (RPC error payload), retry succeeds -> value, 2 calls
    http, calls = _fake_http([{"error": {"code": -32000}}, "0x" + format(10**18, "x")])
    v = W.native_balance_or_none("0xabc", "https://rpc", http=http, backoff_s=0)
    assert abs(v - 1.0) < 1e-9
    assert calls["i"] == 2


def test_native_balance_or_none_double_failure_returns_none():
    # a persistent read failure is UNKNOWN (None), never a phantom 0.0 balance
    http, calls = _fake_http([{"error": {"code": -32000}}])
    assert W.native_balance_or_none("0xabc", "https://rpc", http=http, backoff_s=0) is None
    assert calls["i"] == 2                                     # exactly one retry, then give up


_OWNER = "0x" + "ab" * 20     # valid 20-byte hex address


def test_erc20_decimals_and_balance():
    # first call: decimals() -> 6 ; second: balanceOf() -> 1_500_000 (=1.5 units at 6 decimals)
    http, calls = _fake_http(["0x" + format(6, "x"), "0x" + format(1_500_000, "x")])
    bal = W.erc20_balance(_OWNER, "0xtoken", "https://rpc", http=http)
    assert abs(bal - 1.5) < 1e-9
    # balanceOf selector + the owner address left-padded to 32 bytes in the second call's data
    data = calls["bodies"][1]["params"][0]["data"]
    assert data.startswith("0x70a08231")
    assert data.endswith(_OWNER[2:].rjust(64, "0"))


def test_erc20_balance_with_decimals_returns_existing_decimals_without_extra_rpc():
    http, calls = _fake_http(["0x" + format(8, "x"), "0x" + format(150_000_000, "x")])
    assert W.erc20_balance_with_decimals(_OWNER, "0xtoken", "https://rpc", http=http) == (1.5, 8)
    assert calls["i"] == 2


def test_erc20_balance_fail_closed_on_bad_decimals():
    http, _ = _fake_http([{"result": "0x"}, "0x" + format(1_000_000, "x")])
    assert W.erc20_balance(_OWNER, "0xtoken", "https://rpc", http=http) == 0.0


def test_usdc_balance_uses_chain_usdc_address():
    # The decimals shortcut applies only while USDC IS the chain's quote token. Base moved to WETH
    # on 2026-09-06 and Ethereum on 2026-09-30, so both ask the token itself; Arc (5042), whose
    # quote token IS its USDC, keeps the shortcut this asserts.
    http, calls = _fake_http(["0x" + format(10_000_000, "x")])  # 10 USDC
    bal = W.usdc_balance("0xowner", "5042", "https://rpc", http=http)
    assert abs(bal - 10.0) < 1e-9
    to_addr = calls["bodies"][0]["params"][0]["to"].lower()
    assert to_addr == W.CHAIN_USDC["5042"].lower()
    assert calls["bodies"][0]["params"][0]["data"].startswith("0x" + W.SEL_BALANCE_OF)
    assert calls["i"] == 1


def test_usdc_balance_unknown_chain_fail_closed():
    http, _ = _fake_http(["0x" + format(6, "x"), "0x" + format(10_000_000, "x")])
    assert W.usdc_balance("0xowner", "99999", "https://rpc", http=http) == 0.0


def test_arc_quote_token_is_the_real_usdc_address_not_a_decoy():
    # Arc mainnet (Circle, chain_id 5042, 2026-09-16): native gas IS the USDC settlement asset, at
    # a fixed precompile-style address, 6 decimals. Three 18-decimal "USDC"-branded tokens are
    # already live on Arc (arcUSDC, dUSDC, steakUSDC) — a lookup that matched by symbol/name instead
    # of this exact address would route funded money into one of them.
    real = "0x3600000000000000000000000000000000000000"
    decoys = {
        "0x8E357432CC12ff425c36432F312968aEb16112AF",   # arcUSDC
        "0x6bdfE1165D5165808d02dE05969c9a19e9b7cf30",    # dUSDC
        "0xbeef0016cb2Fd5C352ea7CA08a9f54739DFa7298",    # steakUSDC
    }
    quote = W.CHAIN_QUOTE["5042"]
    assert quote["token"].lower() == real.lower()
    assert quote["decimals"] == 6
    assert quote["kind"] == "usd"
    for decoy in decoys:
        assert quote["token"].lower() != decoy.lower()


def test_native_is_quote_true_only_for_arc():
    assert W.native_is_quote("5042") is True
    for chain in ("8453", "56", "1", "4663", "solana", "99999", "", None):
        assert W.native_is_quote(chain) is False
