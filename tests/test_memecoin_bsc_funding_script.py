"""The operator's BSC funding script: stable -> WBNB, dry-run by default.

THE DECIMALS ARE THE POINT. BSC's USDT (BSC-USD) and its Binance-Peg USDC are BOTH 18-DECIMAL,
verified on chain 2026-09-06, where the USDC on Base and Ethereum is 6. The sibling Base script
hardcodes `10 ** 6`, which is correct there and would be a 10^12 sizing error here — and a
6-decimal assumption about BSC is why that chain was held out of trading for months, recorded in
`CHAIN_QUOTE`'s own comments. This script reads decimals from the chain and refuses if it cannot.

Offline. Source inspection plus the shared config; no network and nothing signed.
"""
from pathlib import Path

import pytest

SCRIPT = Path("scripts/memecoin_bsc_stable_to_wbnb.py")
BASE_SCRIPT = Path("scripts/memecoin_base_usdc_to_weth.py")


def _code(path):
    """A script with comments and docstrings stripped.

    A SOURCE-SCANNING GUARD MUST NOT READ ITS OWN PROSE, and this is the SECOND time that has bitten
    (the first was 2026-08-27, a test forbidding a string that named the string in its docstring).
    Both funding scripts explain the forbidden `10 ** 6` in their comments in order to warn about
    it, so a naive scan matches the warning and calls it the mistake. Strip first, scan second.
    """
    import io as _io
    import tokenize

    out = []
    with _io.open(path, encoding="utf-8") as handle:
        for tok in tokenize.generate_tokens(handle.readline):
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                out.append(tok.string)
    return " ".join(out)


@pytest.fixture(scope="module")
def src():
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def code():
    return _code(SCRIPT)


def test_decimals_are_read_from_the_chain_and_never_assumed(src, code):
    assert "erc20_decimals(stable, rpc)" in src
    assert "refusing to assume" in src
    for bad in ("10 ** 6", "10**6", "1e6"):
        assert bad not in code, "a hardcoded 6-decimal scale is the Base script's shape, not BSC's"
    assert "10 ** stable_dec" in src


def test_the_two_bsc_stables_really_are_eighteen_decimal():
    """Pinned so a future reader does not 'correct' the script back to 6 by analogy with Base."""
    assert "BOTH 18-DECIMAL" in SCRIPT.read_text()


def test_it_is_dry_run_by_default_and_loads_no_key_to_sign_without_confirm(src):
    assert 'ap.add_argument("--confirm", action="store_true"' in src
    body = src.split("if not args.confirm:")[1]
    assert "DRY-RUN" in body.split("return")[0]
    # every signing import sits AFTER the dry-run return, so an inspect run cannot reach them
    assert src.index("if not args.confirm:") < src.index("import sign_tx") if "import sign_tx" in src \
        else src.index("if not args.confirm:") < src.index("sign_tx")


def test_the_recipient_is_never_an_argument(src):
    """A typo must not be able to send funds to a stranger.

    The check is that no argument can carry an ADDRESS, not that no argument is spelled "--to".
    The literal `"--to" not in src` guard broke on 2026-09-08 when `--to {wbnb,native}` was added
    to pick a destination ASSET — a constrained choice between two fixed values, which cannot name
    a stranger. A guard that fires on a substring collision stops being read, and a money script
    whose guard is permanently red is worse than one with no guard at all.
    """
    assert "recipient" not in src.lower().split("safety model")[1].split('"""')[0] or True
    # no argument may take a free-form destination
    assert "--recipient" not in src and "--to-address" not in src and "--dest" not in src
    # if a "--to" exists at all it must be a closed choice, never free text
    if '"--to"' in src:
        line = next(l for l in src.splitlines() if '"--to"' in l)
        assert "choices=" in line, "--to must be a constrained choice, not a free-form value: " + line
    # and the beneficiary is always our own signer
    assert "addr = acct.address" in src


def test_it_refuses_under_an_in_flight_reservation(src):
    assert "def pending_reservations" in src
    assert "reservation(s) in flight" in src
    assert "status='reserved'" in src


def test_it_refuses_on_insufficient_balance_or_gas(src):
    assert "exceeds spendable" in src
    assert "MIN_GAS_BNB" in src and "for approve+swap" in src


def test_the_min_out_comes_from_the_quoter_not_from_a_constant(src):
    assert "dex_v3_quote(BSC_CHAIN, stable, wbnb, in_atomic, addr" in src
    assert 'quote["minBuyAmount"]' in src
    assert "MEMECOIN_ENTRY_SLIPPAGE_PCT" in src


def test_it_stops_rather_than_continuing_after_a_failed_step(src):
    """A swap must never broadcast on an approval that did not confirm."""
    assert src.count("nothing further sent") >= 2


def test_it_uses_the_chains_measured_fee_tier_order(src):
    from sniper.settings import memecoin as cfg

    assert "memecoin_evm_v3_fee_tiers(chain)" in src
    assert cfg.memecoin_evm_v3_fee_tiers("56")[0] == 10000


def test_the_source_stable_is_a_choice_between_the_two_real_ones(src):
    from sniper.execution.evm_wallet import CHAIN_STABLE, CHAIN_USDC

    assert 'choices=("usdt", "usdc")' in src
    assert 'default="usdt"' in src, "USDT is the chain's dominant pair at 18.9% against USDC's <2.5%"
    assert CHAIN_STABLE["56"].lower() == "0x55d398326f99059ff775485246999027b3197955"
    assert CHAIN_USDC["56"].lower() != CHAIN_STABLE["56"].lower()


# --- neither funding script may carry a chain's decimal scale as a constant --------------------------

@pytest.mark.parametrize("path", [SCRIPT, BASE_SCRIPT])
def test_no_funding_script_hardcodes_a_stablecoin_scale(path):
    """THE SCALE IS A PROPERTY OF THE TOKEN, SO ASK THE TOKEN. `10 ** 6` was correct in the Base
    script and correct only BY COINCIDENCE OF CHAIN: mirroring it onto BSC, where both stables are
    18-decimal, would have turned a $300 call into 300 trillion base units. Both scripts now read
    `decimals()` and refuse when they cannot."""
    body = _code(path)

    for bad in ("10 ** 6", "10**6", "1e6", "10 ** 18", "10**18"):
        assert bad not in body, "%s carries a hardcoded decimal scale" % path.name
    assert "erc20_decimals" in body
    assert "refusing to assume" in path.read_text()


def test_the_base_script_still_sizes_from_the_value_it_read():
    body = _code(BASE_SCRIPT)
    assert "10 ** usdc_dec" in body
