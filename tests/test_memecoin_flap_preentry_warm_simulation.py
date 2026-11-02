"""Real Flap quote factories with read-RPC boundary fakes; no signing or network."""
from concurrent.futures import ThreadPoolExecutor

from eth_utils import keccak
import pytest

from sniper.execution import evm_flap_curve as flap
from sniper.execution import evm_read_rpc, evm_token_safety
from sniper.execution.evm_wallet import SEL_ALLOWANCE, SEL_BALANCE_OF

MINT = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
OTHER_TAKER = "0x" + "ab" * 20
PORTAL = flap.FLAP_PORTAL["56"]
OTHER_PORTAL = "0x" + "cd" * 20
RPC = "https://bsc.example"
AMOUNT = 10_000 * 10**18
PAYOUT = 59_874_447_230_422


def _word(value):
    if isinstance(value, str):
        return bytes.fromhex(value[2:].rjust(64, "0"))
    return value.to_bytes(32, "big")


def _slots(taker, spender):
    return (
        "0x" + keccak(_word(taker) + _word(51)).hex(),
        "0x" + keccak(_word(spender) + keccak(_word(taker) + _word(52))).hex(),
    )


class Reads:
    def __init__(self):
        self.calls = []
        self.real_payout = None
        self.override_payout = PAYOUT
        self.state = [1, 304288643025442896, 52272443043027083493254115, 6109695396, 5,
                      6140000000000000000, 107036752000000000000000000,
                      6797205657280000000000000000, 800000000000000000000000000,
                      flap._NATIVE, 0, 0, 100, 0, 0]

    def eth_call_raw(self, to, data, rpc_url, *, http=None, overrides=None,
                     from_addr=None, value_wei=None, raise_on_error=False):
        assert rpc_url == RPC
        selector = data[2:10]
        if selector == flap._SEL_GET_TOKEN_V6:
            self.calls.append(("state",))
            return "0x" + "".join(_word(value).hex() for value in self.state)
        if selector in (SEL_BALANCE_OF, SEL_ALLOWANCE):
            taker = "0x" + data[10:74][-40:]
            spender = "0x" + data[74:138][-40:] if selector == SEL_ALLOWANCE else PORTAL
            slot = _slots(taker, spender)[selector == SEL_ALLOWANCE]
            self.calls.append(("probe", taker, spender))
            diff = ((overrides or {}).get(to) or {}).get("stateDiff", {})
            return diff.get(slot, "0x" + "0" * 64)
        assert selector == flap._SEL_SWAP_EXACT_INPUT
        assert int(data[10:74], 16) == 32
        amount = int(data[202:266], 16)
        kind = "override" if overrides is not None else "real"
        self.calls.append((kind, amount, overrides))
        if overrides is not None:
            assert set(overrides[MINT]["stateDiff"]) == set(_slots(from_addr, to))
            assert all(int(v, 16) == amount + 10**24
                       for v in overrides[MINT]["stateDiff"].values())
            payout = self.override_payout
            if callable(payout):
                payout = payout(amount)
        else:
            payout = self.real_payout
        return None if payout is None else "0x" + _word(payout).hex()


@pytest.fixture
def reads(monkeypatch):
    source = Reads()
    monkeypatch.setattr(flap, "_sell_override_slot_cache", {})
    monkeypatch.setattr(evm_read_rpc, "eth_call_raw", source.eth_call_raw)
    monkeypatch.setattr(evm_token_safety, "_slot_probe_alternates", lambda url: ())
    return source


def _warm(reads, *, taker=TAKER, spender=PORTAL):
    assert flap.discover_sell_overrides(MINT, spender, AMOUNT, taker=taker, rpc_url=RPC)
    assert len(reads.calls) == 2
    reads.calls.clear()


def _quote(**kwargs):
    return flap.dex_flap_curve_exit_quote(
        "56", MINT, flap._NATIVE, AMOUNT, TAKER, rpc_url=RPC, slippage_bps=100, **kwargs)


def _simulations(reads):
    return [call[0] for call in reads.calls if call[0] in ("override", "real")]


def test_warm_token_safety_uses_one_exact_full_clip_simulation(reads):
    _warm(reads)
    check = flap.wrap_token_safety_fn(
        "56", RPC, taker=TAKER, fallback_fn=lambda *a, **k: pytest.fail("not Flap"))
    assert check(MINT, bought_amount_atomic=AMOUNT) == (
        True, "flap_curve_exit_quotable_at_clip")
    assert _simulations(reads) == ["override"]
    assert [c[0] for c in reads.calls] == ["state", "override"]


def test_warm_preentry_uses_exact_override_without_real_attempt(reads):
    _warm(reads)
    quote = _quote(pre_entry=True)
    assert _simulations(reads) == ["override"]
    assert quote["buyAmount"] == str(PAYOUT)
    assert quote["minBuyAmount"] == "59275702758117"
    assert quote["route"]["quote_basis"] == "allowance_override"
    assert quote["transaction"]["value"] == "0x0"


def test_cold_preentry_retains_real_then_discovery_then_override(reads):
    quote = _quote(pre_entry=True)
    assert quote["buyAmount"] == str(PAYOUT)
    assert [c[0] for c in reads.calls] == ["state", "real", "probe", "probe", "override"]


def test_default_exit_keeps_real_allowance_first_even_with_warm_proof(reads):
    _warm(reads)
    reads.real_payout = PAYOUT * 2
    quote = _quote()
    assert _simulations(reads) == ["real"]
    assert quote["buyAmount"] == str(PAYOUT * 2)
    assert quote["route"]["quote_basis"] == "real_allowance"


@pytest.mark.parametrize("override", [None, PAYOUT * 2])
def test_warm_override_failure_or_model_refusal_keeps_real_allowance_exemption(reads, override):
    _warm(reads)
    reads.override_payout = override
    reads.real_payout = PAYOUT * 3
    quote = _quote(pre_entry=True)
    assert _simulations(reads) == ["override", "real"]
    assert quote["buyAmount"] == str(PAYOUT * 3)
    assert quote["route"]["quote_basis"] == "real_allowance"


@pytest.mark.parametrize("real", [None, 0])
def test_model_refusal_without_positive_real_simulation_stays_refused(reads, real):
    _warm(reads)
    reads.override_payout = PAYOUT * 2
    reads.real_payout = real
    assert _quote(pre_entry=True) is None
    assert _simulations(reads) == ["override", "real"]


@pytest.mark.parametrize("changed", ["taker", "spender"])
def test_hash_proof_does_not_cross_custody_or_spender(reads, changed):
    _warm(reads)
    taker = OTHER_TAKER if changed == "taker" else TAKER
    spender = OTHER_PORTAL if changed == "spender" else PORTAL
    overrides = flap.discover_sell_overrides(MINT, spender, AMOUNT, taker=taker, rpc_url=RPC)
    assert set(overrides[MINT]["stateDiff"]) == set(_slots(taker, spender))
    assert len(reads.calls) == 2


@pytest.mark.parametrize("changed", ["taker", "spender"])
def test_preentry_wrong_identity_proof_stays_cold(reads, changed):
    _warm(reads, taker=OTHER_TAKER if changed == "taker" else TAKER,
          spender=OTHER_PORTAL if changed == "spender" else PORTAL)
    assert _quote(pre_entry=True)["buyAmount"] == str(PAYOUT)
    assert [c[0] for c in reads.calls] == ["state", "real", "probe", "probe", "override"]


def test_case_normalized_identity_reuses_proof_without_rpc(reads):
    _warm(reads)
    overrides = flap.discover_sell_overrides(
        MINT.upper(), PORTAL.upper(), AMOUNT, taker=TAKER.upper(), rpc_url=RPC)
    assert set(overrides[MINT]["stateDiff"]) == set(_slots(TAKER, PORTAL))
    assert reads.calls == []


def test_warm_amounts_build_independent_override_maps_under_concurrency(reads):
    _warm(reads)
    amounts = [AMOUNT, AMOUNT * 10]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda amount: flap.discover_sell_overrides(
            MINT, PORTAL, amount, taker=TAKER, rpc_url=RPC), amounts))
    assert reads.calls == []
    for result, amount in zip(results, amounts):
        assert {int(v, 16) for v in result[MINT]["stateDiff"].values()} == {amount + 10**24}
    results[0][MINT]["stateDiff"].clear()
    assert len(results[1][MINT]["stateDiff"]) == 2
    assert len(flap.discover_sell_overrides(
        MINT, PORTAL, AMOUNT, taker=TAKER, rpc_url=RPC)[MINT]["stateDiff"]) == 2


def test_custom_discovery_keeps_real_first_without_receiving_preentry_keyword(reads):
    _warm(reads)
    calls = []

    def custom(mint, spender, amount, *, taker, rpc_url, http=None):
        calls.append(amount)
        return flap.discover_sell_overrides(
            mint, spender, amount, taker=taker, rpc_url=rpc_url, http=http)

    assert _quote(pre_entry=True, overrides_fn=custom)["buyAmount"] == str(PAYOUT)
    assert calls == [AMOUNT]
    assert _simulations(reads) == ["real", "override"]


def test_warm_slot_proof_does_not_cache_curve_fee_or_mutable_state(reads):
    _warm(reads)
    assert _quote(pre_entry=True)["buyAmount"] == str(PAYOUT)
    reads.state[12] = 300
    reads.calls.clear()
    assert _quote(pre_entry=True) is None
    assert reads.calls[0] == ("state",)
    assert _simulations(reads) == ["override", "real"]


def test_converted_quote_does_not_compare_native_output_to_quote_units(reads, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED", True)
    _warm(reads)
    reads.state[9] = "0x" + "12" * 20
    reads.state[10] = 1
    reads.override_payout = PAYOUT * 2
    quote = _quote(pre_entry=True)
    assert quote["buyAmount"] == str(PAYOUT * 2)
    assert _simulations(reads) == ["override"]


def test_actual_exit_unquotable_override_never_gets_a_third_simulation(reads):
    _warm(reads)
    reads.override_payout = None
    assert _quote() is None
    assert _simulations(reads) == ["real", "override"]
