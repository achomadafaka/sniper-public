"""Route-agnostic Relay purchase proof for the on-chain lane, on 4663, 56 and 8453 (2026-09-28).

Measured on real J7-proved buys of axiom_confirmed wallets, the watcher decoded 5 of 25 on 4663 and
0 of 30 on 56. Every one was the same Relay Permit2 envelope (proxy, adapter, router cleanup). The
relay proof also required a Kyber `Swapped` event, which 13/25 and 19/30 do not carry because the
adapter routed elsewhere, and it ran on 4663 only. On 56 and 8453 the Relay proxy and router carry
the SAME pinned code hashes as on 4663 (read 2026-09-28); the Kyber router on 56 does not.

`any_route=True` (the watcher) drops ONLY the Kyber requirement. Every envelope check stays: the
Permit2 arguments, the payment legs payer -> router -> adapter, the mint legs adapter -> router ->
wallet, the FundsMovement pair, and the wallet as the mint's only net gainer. The default mode, used
by the address verifier, is unchanged.

FEE AND TAX GAINERS (2026-09-28, Alpha GO). 13/25 real 4663 buys and 12/30 real 56 buys also pay the
mint to a second party FROM THE POOL (a V4 hook fee, a token tax). A gainer other than the wallet is
allowed only when every unit it received came from the pool side: the parties upstream of the swap
target in the mint flow, or a pass-through they paid (a Base V4 hook forwards its fee on), never the
router or the swap target. The wallet stays the only gainer from the relay chain.

Known miss, kept on purpose until 2026-09-28: a swap step aimed at a contract other than the adapter
(0x Settler, a direct Kyber call) moves the tokens through other executors. SUPERSEDED by operator
order: those are now proven from the wallet's own delta and a pass-through router, under proof
version relay-envelope-other-target-v1 (tests/test_relay_other_target.py). All frames below are real receipts (tests/fixtures/relay_purchases_2026-09-28.json.gz);
mutated ones are marked.
"""
import copy
import gzip
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode

from sniper.execution.evm_relay_quote import RELAY_ROUTER
from sniper.services import memecoin_evm_onchain_watch as M

FIX = json.load(gzip.open(Path(__file__).parent / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
BUYS = FIX["buys"]
ENVELOPE = [b for b in BUYS if b["shape"] in ("kyber", "no_kyber", "fee_gainer")]
FEE = [b for b in BUYS if b["shape"] == "fee_gainer"]


def _id(b):
    return "%s-%s-%s" % (b["chain"], b["shape"], b["handle"])


def _proof(b, **kw):
    return M.relayer_purchase_from_receipt(b["receipt"], b["tx"], b["wallet"], chain=b["chain"],
                                           contracts_verified=True, **kw)


def _to_wallet(b):
    """The router -> wallet mint Transfer (the leg the proof's output rests on)."""
    for log in b["receipt"]["logs"]:
        t = log["topics"]
        if (log["address"].lower() == b["mint"] and len(t) == 3 and t[0] == M.TRANSFER_TOPIC
                and t[2][-40:] == b["wallet"][2:]):
            return log
    raise AssertionError("no transfer to the wallet")


@pytest.mark.parametrize("b", ENVELOPE, ids=_id)
def test_every_relay_envelope_buy_decodes_whatever_the_route(b):
    proof = _proof(b, any_route=True)
    assert proof is not None
    assert proof["proof_version"] == M.RELAY_ENVELOPE_PROOF_VERSION
    assert proof["mint"] == b["mint"] and proof["beneficiary"] == b["wallet"]
    assert proof["output_atomic"] == int(_to_wallet(b)["data"], 16)


@pytest.mark.parametrize("b", [b for b in ENVELOPE if b["shape"] != "kyber"], ids=_id)
def test_the_default_mode_is_unchanged_for_the_address_verifier(b):
    assert _proof(b) is None


def test_the_default_mode_still_proves_a_kyber_route_on_4663():
    b = next(b for b in BUYS if b["chain"] == "4663" and b["shape"] == "kyber")
    assert _proof(b)["proof_version"] == M.RELAY_KYBER_PROOF_VERSION


@pytest.mark.parametrize("b", [b for b in BUYS if b["shape"] == "other_target"], ids=_id)
def test_the_former_named_misses_now_prove_as_other_target(b):
    assert _proof(b, any_route=True)["proof_version"] == M.RELAY_OTHER_TARGET_PROOF_VERSION


def test_a_chain_outside_the_verified_set_is_refused():
    # Chain 1 joined the set on 2026-10-17 (MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS); Ink is outside it.
    b = next(b for b in ENVELOPE if b["chain"] == "56")
    assert M.relayer_purchase_from_receipt(b["receipt"], b["tx"], b["wallet"], chain="57073",
                                           contracts_verified=True, any_route=True) is None


# --- negative controls ------------------------------------------------------------------------

@pytest.mark.parametrize("b", ENVELOPE, ids=_id)
def test_a_third_party_receiving_the_mint_is_not_the_wallets_buy(b):
    """MUTATED real receipt: the router -> wallet leg is redirected to a third party."""
    b = copy.deepcopy(b)
    log = _to_wallet(b)
    log["topics"][2] = "0x" + "0" * 24 + "ee" * 20
    assert _proof(b, any_route=True) is None


@pytest.mark.parametrize("b", ENVELOPE, ids=_id)
def test_the_wallet_as_payer_is_not_a_buy(b):
    """MUTATED real transaction: the Permit2 payer is re-encoded as the wallet itself."""
    b = copy.deepcopy(b)
    raw = bytes.fromhex(b["tx"]["input"][10:])
    values = list(decode(M.PERMIT2_TYPES, raw))
    values[0] = b["wallet"]
    b["tx"]["input"] = b["tx"]["input"][:10] + encode(M.PERMIT2_TYPES, values).hex()
    assert _proof(b, any_route=True) is None


@pytest.mark.parametrize("s", FIX["sells"], ids=lambda s: s["chain"])
def test_a_real_sell_is_not_a_buy(s):
    assert M.relayer_purchase_from_receipt(s["receipt"], s["tx"], s["wallet"], chain=s["chain"],
                                           contracts_verified=True, any_route=True) is None


def _mint_logs(b):
    return [log for log in b["receipt"]["logs"]
            if log["address"].lower() == b["mint"] and len(log["topics"]) == 3
            and log["topics"][0] == M.TRANSFER_TOPIC]


def _fee_log(b):
    """The pool -> fee/tax recipient leg: the mint Transfer to a net gainer that is not the wallet."""
    net = {}
    for log in _mint_logs(b):
        a = int(log["data"], 16)
        net[log["topics"][2][-40:]] = net.get(log["topics"][2][-40:], 0) + a
        net[log["topics"][1][-40:]] = net.get(log["topics"][1][-40:], 0) - a
    fee = [k for k, v in net.items() if v > 0 and k != b["wallet"][2:]]
    assert len(fee) == 1
    return next(log for log in _mint_logs(b) if log["topics"][2][-40:] == fee[0])


def _topic(address):
    return "0x" + "0" * 24 + address[2:].lower()


@pytest.mark.parametrize("b", FEE, ids=_id)
def test_a_pool_paid_fee_gainer_is_allowed(b):
    """REAL frame: two net gainers of the mint, the second paid by the pool."""
    assert _proof(b, any_route=True)["output_atomic"] == int(_to_wallet(b)["data"], 16)


@pytest.mark.parametrize("b", FEE, ids=_id)
@pytest.mark.parametrize("sender", ["0x" + "dd" * 20, RELAY_ROUTER, M.RELAY_ADAPTER_4663])
def test_a_second_gainer_paid_from_outside_the_pool_side_is_none(b, sender):
    """MUTATED real receipt: the fee leg's sender is an unrelated address, the router, or the
    adapter. Only the pool side may pay anyone but the wallet."""
    b = copy.deepcopy(b)
    _fee_log(b)["topics"][1] = _topic(sender)
    assert _proof(b, any_route=True) is None


@pytest.mark.parametrize("b", ENVELOPE, ids=_id)
def test_a_third_party_on_the_adapter_to_router_leg_is_none(b):
    """MUTATED real receipt: the adapter -> router mint leg is redirected to a third party."""
    b = copy.deepcopy(b)
    leg = next(log for log in _mint_logs(b) if log["topics"][1][-40:] == M.RELAY_ADAPTER_4663[2:]
               and log["topics"][2][-40:] == RELAY_ROUTER[2:])
    leg["topics"][2] = _topic("0x" + "ee" * 20)
    assert _proof(b, any_route=True) is None


# --- the pinned code check, on 56 and 8453 ----------------------------------------------------

def _reader(b, chain_id, code):
    def read(method, params):
        if method == "eth_chainId":
            return hex(chain_id)
        if method == "eth_getCode":
            return code.get(params[0], "0xdeadbeef")
        if method == "eth_getTransactionByHash":
            return b["tx"]
        if method == "eth_getTransactionReceipt":
            return b["receipt"]
        raise AssertionError(method)
    return read


@pytest.mark.parametrize("b", [b for b in ENVELOPE if b["chain"] in ("56", "8453")], ids=_id)
def test_envelope_proofs_pass_the_pinned_relay_code_and_skip_kyber(b):
    """The proxy and router code are the chain's real bytes; any other address answers junk, so a
    Kyber check would fail. The envelope proof never asks for it."""
    proof = M.read_relayer_purchase(b["receipt"], b["tx"], b["wallet"], chain=b["chain"],
                                    read_fn=_reader(b, int(b["chain"]), FIX["code_" + b["chain"]]),
                                    any_route=True)
    assert proof is not None and proof["code_basis"] == "receipt_block"


def test_56_relay_code_that_differs_is_refused():
    b = next(b for b in ENVELOPE if b["chain"] == "56")
    code = dict(FIX["code_56"])
    first = next(iter(code))
    code[first] = code[first][:-2] + "00"
    evidence = {}
    assert M.read_relayer_purchase(b["receipt"], b["tx"], b["wallet"], chain="56",
                                   read_fn=_reader(b, 56, code), any_route=True,
                                   evidence=evidence) is None
    assert evidence["reason"] == "contract_code_mismatch"


# --- through the watcher ----------------------------------------------------------------------

@pytest.mark.parametrize("b", [b for b in ENVELOPE if b["chain"] in ("56", "8453")], ids=_id)
def test_the_watcher_turns_a_real_relay_buy_into_a_detection(b, monkeypatch):
    import scripts.research.onchain_evm_wallet_watch as w
    w._configure(b["chain"])
    try:
        log = _to_wallet(b)
        watched = {b["wallet"]: ("uid", b["handle"])}
        payment = decode(M.PERMIT2_TYPES, bytes.fromhex(b["tx"]["input"][10:]))[1][0][0][0].lower()
        row = w.push_detection("http://unused", log, watched, decimals_cache={b["mint"]: 18, payment: 18},
                               quote_usd=600.0, now="2026-09-28T06:30:00+00:00",
                               read_fn=_reader(b, int(b["chain"]), FIX["code_" + b["chain"]]),
                               payment_price_fn=lambda token: 1.0 if token == payment else None)
    finally:
        w._configure("4663")
    assert row is not None
    assert row["buy"]["mint"] == b["mint"] and row["handle"] == b["handle"]
    assert row["buy"]["source_size_usd"] > 0


@pytest.mark.parametrize("chain,token,pair", [
    ("56", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "USDCUSD"),
    ("8453", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDCUSD"),
    ("4663", "0x5fc5360d0400a0fd4f2af552add042d716f1d168", "USDGUSD")])
def test_the_payment_token_is_priced_from_its_own_ticker(monkeypatch, chain, token, pair):
    import scripts.research.onchain_evm_wallet_watch as w
    from sniper.execution import evm_wallet
    asked = []

    def http(method, url, timeout=None):
        asked.append(url)
        return 200, {"error": [], "result": {pair: {"b": ["0.9998"]}}}, None
    monkeypatch.setattr(evm_wallet, "_http", http)
    monkeypatch.setattr(w, "_PAYMENT_PRICES", {})
    w._configure(chain)
    try:
        assert w._payment_usd_price(token, 600.0) == pytest.approx(0.9998)
        assert w._payment_usd_price("0x" + "12" * 20, 600.0) is None
    finally:
        w._configure("4663")
    assert asked == ["https://api.kraken.com/0/public/Ticker?pair=" + pair]
