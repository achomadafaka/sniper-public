"""A proven storage layout can be reused; a prior simulated amount cannot.

Permit2 (refined 2026-10-17, Alpha after Gamma's (d); supersedes the 2026-10-10 wording "never invent
an allowance slot/value to satisfy a larger simulation"): the point was never to widen WITHOUT a read.
The live allowance decides every Permit2 override, warm exactly as cold:
- an amount word inside the allowance last read for this coin: balance-only, no provider read;
- a larger word: exactly one live allowance read, then balance-only if it now covers, else BOTH
  slots (the cold path's own override);
- no Permit2 override ever comes from a cached or persisted proof alone: those are slot layouts.
The 10-10 behaviour reused a balance-only override for a larger sell; the chain's allowance no longer
covered it, transferFrom reverted, and a sellable coin was refused.
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from eth_utils import keccak
import pytest

from sniper.execution import evm_token_safety as safety


MINT = "0x" + "31" * 20
TAKER = "0x" + "22" * 20
SMART_ROUTER = "0x13f4ea83d0bd40e75c8222255bc855a974568dd4"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"


def _mapping_key(address, slot):
    address_word = bytes.fromhex(address[2:]).rjust(32, b"\0")
    slot_word = (slot.to_bytes(32, "big") if isinstance(slot, int)
                 else bytes.fromhex(slot[2:]))
    return "0x" + keccak(address_word + slot_word).hex()


def _bound_overrides(spender, *, live_allowance=None, mint=MINT, slot_proof_load=None):
    balance_slot = _mapping_key(TAKER, 0)
    allowance_slot = _mapping_key(spender, _mapping_key(TAKER, 1))
    reads = []

    def http(method, url, *, headers=None, body=None, timeout=None):
        assert method == "POST" and url == "https://rpc.invalid"
        request = json.loads(body)
        assert request["method"] == "eth_call"
        tx, _block, *rest = request["params"]
        assert tx["to"] == mint
        diff = ((rest[0] if rest else {}).get(mint) or {}).get("stateDiff") or {}
        # A call with no state override reads the chain itself: the live allowance read.
        reads.append(tx["data"][:10] + ("" if rest else ":live"))
        if tx["data"].startswith("0x70a08231"):
            result = diff.get(balance_slot, "0x0")
        elif tx["data"].startswith("0xdd62ed3e"):
            # An override of the allowance slot is what allowance() returns (as on chain); any other
            # read answers the chain's own allowance.
            result = (diff[allowance_slot] if allowance_slot in diff
                      else hex(live_allowance) if live_allowance is not None else "0x0")
        else:
            pytest.fail("Unexpected provider operation in slot proof")
        return 200, {"jsonrpc": "2.0", "id": request["id"], "result": result}, None

    def never_quote(*args, **kwargs):
        pytest.fail("Building overrides must not start a venue quote")

    fn = safety.build_evm_token_safety(
        chain_id="56", rpc_url="https://rpc.invalid", taker=TAKER,
        http=http, quote_fn=never_quote, slot_proof_load=slot_proof_load,
    ).fund_overrides
    return fn, reads, balance_slot, allowance_slot


@pytest.mark.parametrize(
    "spender,unlimited_allowance",
    [(SMART_ROUTER, False), (PERMIT2, True)],
    ids=["ordinary-spender", "permit2-unlimited-balance-only"],
)
def test_cached_slot_proof_rebuilds_each_amount_without_reads_or_prior_mutation(
        spender, unlimited_allowance):
    bound, reads, balance_slot, allowance_slot = _bound_overrides(
        spender, live_allowance=2 ** 256 - 1 if unlimited_allowance else None,
    )
    small, large = 10 ** 18, 6 * 10 ** 24
    first = bound(MINT, spender, small)
    assert first is not None
    expected_slots = ({balance_slot} if unlimited_allowance
                      else {balance_slot, allowance_slot})
    assert set(first[MINT]["stateDiff"]) == expected_slots
    assert all(int(word, 16) == small + 10 ** 24
               for word in first[MINT]["stateDiff"].values())
    # Chain 56 leads with the OZ-Upgradeable layout (51/52, _CHAIN_SLOT_LEADS): this slot-0 token pays
    # one extra lead probe per mapping it scans (balance, and allowance when it is not unlimited).
    assert len(reads) == (3 if unlimited_allowance else 5)
    original = deepcopy(first)
    cold_reads = len(reads)

    second = bound(MINT, spender, large)
    assert len(reads) == cold_reads, "Known slots must not be rediscovered"
    assert first == original, "A new request must not mutate the prior simulation"
    assert second is not None
    assert set(second[MINT]["stateDiff"]) == expected_slots
    assert all(int(word, 16) == large + 10 ** 24
               for word in second[MINT]["stateDiff"].values()), (
        "A cached layout returned the prior small amount for a larger simulation"
    )
    assert second is not first


@pytest.mark.parametrize(
    "amount,expected_word",
    [(2 ** 256 - 1, 2 ** 256 - 1), (2 ** 256 - 6, 2 ** 256 - 1),
     (0, None), (-1, None), (None, None), ("invalid", None), (2 ** 256, None)],
    ids=["uint256-max", "near-uint256-max", "zero", "negative", "absent",
         "malformed", "overflow"],
)
@pytest.mark.parametrize("spender", [SMART_ROUTER, PERMIT2], ids=["ordinary", "permit2"])
def test_warm_overrides_validate_amount_before_using_proven_layout(spender, amount, expected_word):
    bound, reads, _balance_slot, _allowance_slot = _bound_overrides(
        spender, live_allowance=2 ** 256 - 1 if spender == PERMIT2 else None,
    )
    first = bound(MINT, spender, 10 ** 18)
    original = deepcopy(first)
    cold_reads = len(reads)

    second = bound(MINT, spender, amount)
    assert len(reads) == cold_reads
    assert first == original
    if expected_word is None:
        assert second is None, "Invalid amount must not return prior valid overrides"
    else:
        assert second is not None
        assert all(int(word, 16) == expected_word
                   for word in second[MINT]["stateDiff"].values())
        assert second is not first


def _live(reads, start=0):
    return [r for r in reads[start:] if r.endswith(":live")]


def test_a_finite_permit2_allowance_is_read_live_before_any_allowance_override():
    """The refined rule (module docstring): inside the allowance last read, balance-only and no read;
    above it, exactly one live allowance read and only then both slots, on EVERY such call (the
    second larger call reads again: no override from the cache alone)."""
    bound, reads, balance_slot, allowance_slot = _bound_overrides(PERMIT2, live_allowance=2 * 10 ** 24)
    first = bound(MINT, PERMIT2, 10 ** 18)
    assert set(first[MINT]["stateDiff"]) == {balance_slot}
    assert len(_live(reads)) == 1
    original = deepcopy(first)
    mark = len(reads)
    inside = bound(MINT, PERMIT2, 10 ** 18 // 2)
    assert len(reads) == mark and set(inside[MINT]["stateDiff"]) == {balance_slot}
    for call in range(2):
        mark = len(reads)
        larger = bound(MINT, PERMIT2, 6 * 10 ** 24)
        assert len(_live(reads, mark)) == 1
        if call:
            assert reads[mark:] == ["0xdd62ed3e:live"], "layouts are cached; only the live read repeats"
        assert set(larger[MINT]["stateDiff"]) == {balance_slot, allowance_slot}
        assert all(int(w, 16) == 7 * 10 ** 24 for w in larger[MINT]["stateDiff"].values())
    assert first == original
    mark = len(reads)
    again = bound(MINT, PERMIT2, 10 ** 18)
    assert len(reads) == mark and set(again[MINT]["stateDiff"]) == {balance_slot}


def test_a_persisted_permit2_proof_seeds_layouts_and_the_live_read_still_decides():
    balance_slot = _mapping_key(TAKER, 0)
    allowance_slot = _mapping_key(PERMIT2, _mapping_key(TAKER, 1))
    bound, reads, _b, _a = _bound_overrides(PERMIT2, live_allowance=2 * 10 ** 24,
                                            slot_proof_load=lambda key: (balance_slot, allowance_slot))
    larger = bound(MINT, PERMIT2, 6 * 10 ** 24)
    assert reads == ["0xdd62ed3e:live"]                                   # no scan; never the proof alone
    assert set(larger[MINT]["stateDiff"]) == {balance_slot, allowance_slot}
    covered = bound(MINT, PERMIT2, 10 ** 18)
    assert set(covered[MINT]["stateDiff"]) == {balance_slot}


@pytest.mark.parametrize("spender", [SMART_ROUTER, PERMIT2], ids=["ordinary", "permit2"])
def test_caller_mutation_cannot_poison_cached_token_or_native_state(spender):
    bound, reads, balance_slot, allowance_slot = _bound_overrides(
        spender, live_allowance=2 ** 256 - 1 if spender == PERMIT2 else None,
    )
    first = bound(MINT, spender, 10 ** 18)
    pristine = deepcopy(first)
    cold_reads = len(reads)

    first[MINT]["stateDiff"][balance_slot] = "0x" + "00" * 32
    first[MINT]["stateDiff"].pop(allowance_slot, None)
    first[MINT]["stateDiff"]["0x" + "ff" * 32] = "0x" + "ff" * 32
    first[TAKER]["balance"] = "0x0"
    first[TAKER]["caller_owned"] = True
    mutated = deepcopy(first)

    second = bound(MINT, spender, 10 ** 18)
    assert len(reads) == cold_reads
    assert second == pristine
    assert first == mutated, "Rebuilding must not repair or reuse a caller-owned object"
    assert second[MINT]["stateDiff"] is not first[MINT]["stateDiff"]
    assert second[TAKER] is not first[TAKER]


@pytest.mark.parametrize("spender", [SMART_ROUTER, PERMIT2], ids=["ordinary", "permit2"])
def test_concurrent_warm_requests_keep_independent_amounts_without_reads(spender):
    bound, reads, _balance_slot, _allowance_slot = _bound_overrides(
        spender, live_allowance=2 ** 256 - 1 if spender == PERMIT2 else None,
    )
    first = bound(MINT, spender, 10 ** 18)
    original = deepcopy(first)
    cold_reads = len(reads)
    rendezvous = Barrier(2)

    def request(amount):
        rendezvous.wait(timeout=1)
        result = bound(MINT, spender, amount)
        rendezvous.wait(timeout=1)
        return amount, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, amount) for amount in (2 * 10 ** 18, 6 * 10 ** 24)]
        completed = [future.result(timeout=2) for future in futures]

    assert len(reads) == cold_reads
    assert first == original
    for amount, result in completed:
        assert result is not None
        assert all(int(word, 16) == amount + 10 ** 24
                   for word in result[MINT]["stateDiff"].values())
    assert completed[0][1][MINT]["stateDiff"] is not completed[1][1][MINT]["stateDiff"]
    assert completed[0][1][TAKER] is not completed[1][1][TAKER]


@pytest.mark.parametrize("spender", [SMART_ROUTER, PERMIT2], ids=["ordinary", "permit2"])
def test_equivalent_mixed_case_mint_and_spender_reuse_proven_slots(spender):
    mint = "0xabcd" + "31" * 18
    bound, reads, balance_slot, allowance_slot = _bound_overrides(
        spender, mint=mint, live_allowance=2 ** 256 - 1 if spender == PERMIT2 else None,
    )
    first = bound(mint, spender, 10 ** 18)
    original = deepcopy(first)
    cold_reads = len(reads)

    second = bound("0x" + mint[2:].upper(), "0x" + spender[2:].upper(), 6 * 10 ** 24)
    assert len(reads) == cold_reads
    assert first == original
    assert second is not None
    accounts = {address.lower(): state for address, state in second.items()}
    expected_slots = ({balance_slot} if spender == PERMIT2
                      else {balance_slot, allowance_slot})
    assert set(accounts[mint]["stateDiff"]) == expected_slots
    assert all(int(word, 16) == 7 * 10 ** 24
               for word in accounts[mint]["stateDiff"].values())
