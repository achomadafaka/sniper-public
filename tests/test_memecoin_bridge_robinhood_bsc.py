"""Pure-parsing guards for the Robinhood(4663) -> BSC USDC bridge script (2026-09-06). These
functions turn untrusted Relay API JSON into real transactions, so the value/hex parsing and the
origin-chain guard are safety-critical. No network — pure logic only.

Robinhood holds no dollar-stable of its own (0 USDC/USDG on the live custody wallet, verified
2026-09-06 against a public 4663 RPC) — only native ETH and WETH, CHAIN_QUOTE["4663"]'s own
bankroll token. So this script bridges FROM WETH and asks Relay for an EXACT destination amount of
USDC on BSC (tradeType=EXACT_OUTPUT), the one script in this family that does not already know its
origin amount before quoting."""
import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "memecoin_bridge_robinhood_bsc.py")
_spec = importlib.util.spec_from_file_location("memecoin_bridge_robinhood_bsc", _p)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def test_to_int_handles_dec_hex_none():
    assert F._to_int("0") == 0
    assert F._to_int("50000000") == 50_000_000
    assert F._to_int("0x1237") == 4663               # Robinhood chain id, hex string
    assert F._to_int(None, 7) == 7
    assert F._to_int(42) == 42


def test_steps_flatten_and_value_parse():
    # lowercase `to` (as Relay returns for the deposit router) must be accepted and checksummed
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    relay = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
    quote = {"steps": [
        {"id": "approve", "items": [{"data": {"chainId": 4663, "to": weth,
                                              "data": "0x095ea7b3ff", "value": "0"}}]},
        {"id": "deposit", "items": [{"data": {"chainId": 4663, "to": relay,
                                              "data": "0xf9e4bab4aa", "value": "0"}}]},
    ]}
    from eth_utils import to_checksum_address
    txs = F._steps_as_txs(quote)
    assert [t["label"] for t in txs] == ["approve", "deposit"]
    assert txs[0]["to"] == F.ROBINHOOD_WETH             # checksummed
    assert txs[1]["to"] == to_checksum_address(relay)   # lowercase -> checksummed


def test_rejects_non_robinhood_origin_chain():
    """A step that would execute on the WRONG chain (e.g. BSC itself) must raise, never silently
    sign on the destination chain."""
    quote = {"steps": [{"id": "deposit", "items": [{"data": {"chainId": 56, "to": "0xX",
                                                             "data": "0x", "value": "0"}}]}]}
    try:
        F._steps_as_txs(quote)
        assert False, "expected RuntimeError on non-Robinhood origin chain"
    except RuntimeError as e:
        assert "chainId" in str(e)


def test_constants_are_the_verified_addresses():
    from sniper.execution.evm_wallet import CHAIN_QUOTE, CHAIN_USDC
    assert F.ROBINHOOD_CHAIN == 4663 and F.BSC_CHAIN == 56
    # WETH is 4663's own bankroll token (CHAIN_QUOTE), never a second, competing source.
    assert F.ROBINHOOD_WETH.lower() == CHAIN_QUOTE["4663"]["token"].lower()
    # USDC is BSC's verified Binance-Peg address (CHAIN_USDC), never a second, competing source.
    assert F.BSC_USDC.lower() == CHAIN_USDC["56"].lower()


def test_the_quote_is_exact_output_not_exact_input():
    """The whole reason this script differs from its siblings: Robinhood has no stable to name an
    exact INPUT amount of, so it must ask Relay for an exact OUTPUT on BSC instead."""
    src = open(_p).read()
    assert '"tradeType": "EXACT_OUTPUT"' in src
    assert '"tradeType": "EXACT_INPUT"' not in src


def test_script_never_hardcodes_a_stablecoin_decimal_scale():
    """THE SCALE IS A PROPERTY OF THE TOKEN, SO ASK THE TOKEN. BSC's USDC is 18-decimal where
    Base/Ethereum USDC is 6 — the exact trap that held BSC out of trading for months
    (memecoin_bsc_stable_to_wbnb.py, 2026-09-06). This script must read decimals() live and refuse
    rather than guess."""
    import tokenize
    import io
    with open(_p) as handle:
        code = tokenize.generate_tokens(handle.readline)
        body = " ".join(tok.string for tok in code
                        if tok.type not in (tokenize.COMMENT, tokenize.STRING))
    for bad in ("10 ** 6", "10**6", "1e6", "10 ** 18", "10**18"):
        assert bad not in body, "the script carries a hardcoded decimal scale"
    assert "erc20_decimals" in body
    assert "refusing to assume" in open(_p).read()


def test_refuses_when_the_quoted_weth_input_exceeds_the_wallet_balance():
    """EXACT_OUTPUT means the origin amount is not known until Relay answers — the balance check
    against it must run AFTER the quote, not before."""
    src = open(_p).read()
    quote_idx = src.index("relay_quote_exact_output(")
    balance_check_idx = src.index("required_weth > robin_weth")
    assert quote_idx < balance_check_idx, (
        "the WETH-sufficiency check must come after the quote, since EXACT_OUTPUT "
        "does not reveal the required input until Relay answers")
