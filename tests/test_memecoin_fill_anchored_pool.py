"""The entry receipt names the pool a position actually traded (2026-09-02).

Mint-keyed resolution answers for 1 of 8 recent funded 4663 positions: a mint maps to many pools
(median 13, max 191 among ambiguous ones) and `len(rows) == 1` refuses. The fill knows what the
mint cannot — which pool the swap actually touched.

Selection rules, each established by measurement rather than assumption:

- A routed entry emits SEVERAL Swap logs (observed 3 swaps / 3 distinct pools). Taking the first,
  or the last, would pick a hop we did not end up holding.
- Narrowing candidates by `pool_meta` FAILS: it knew nothing about 6 of those 8 mints, so it
  narrowed 1 of 8. Rejected on evidence.
- V2/V3: the pool contract itself appears as a counterparty in a `Transfer` of our mint.
- V4: the log is emitted by the PoolManager, so no pool address appears in any Transfer. The pool
  id is `topics[1]`, and its currencies come from the PoolManager's Initialize event.
- Two candidate pools that both hold the mint stay AMBIGUOUS and fail closed. That happened once in
  eight and is an honest refusal, not a gap to paper over.

Measured end to end on funded entries: 7 of 8 uniquely identified against mint-keyed's 1 of 8.
"""
import pytest

from sniper.services.memecoin_fill_pool import select_traded_pool

MGR = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
TOPICS = (SWAP_V3, SWAP_V4)
MINT = "0x" + "aa" * 20
POOL_V3 = "0x" + "c2" * 20
POOL_V4 = "0x" + "b1" * 32
OTHER_V4 = "0x" + "e4" * 32
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _swap(addr, topic=SWAP_V3, pool_id=None):
    t = [topic] + ([pool_id] if pool_id else [])
    return {"address": addr, "topics": t}


def _transfer(token, frm, to):
    return {"address": token,
            "topics": [TRANSFER, "0x" + "00" * 12 + frm[2:], "0x" + "00" * 12 + to[2:]]}


def _sel(logs, key_fn=None, mint=MINT):
    return select_traded_pool(logs, mint, pool_manager=MGR, ingest_topics=TOPICS,
                              key_fn=key_fn or (lambda pid: None))


def test_a_v2_v3_pool_is_identified_by_moving_our_mint():
    logs = [_swap(POOL_V3), _transfer(MINT, POOL_V3, "0x" + "11" * 20)]
    assert _sel(logs) == (POOL_V3, 0)


def test_a_swap_pool_that_never_touched_our_mint_is_rejected():
    """A routed hop between two other tokens is a swap we made but not the pool we hold."""
    other = "0x" + "dd" * 20
    logs = [_swap(other), _swap(POOL_V3), _transfer(MINT, POOL_V3, "0x" + "11" * 20)]
    assert _sel(logs) == (POOL_V3, 1)


def test_a_v4_pool_is_identified_by_its_CURRENCIES_not_by_a_transfer():
    """V4 logs come from the PoolManager, so the pool id never appears in a Transfer. Identity has
    to come from the pool's own currencies."""
    keys = {POOL_V4: {"currency0": WETH, "currency1": MINT}}
    logs = [_swap(MGR, SWAP_V4, POOL_V4)]
    assert _sel(logs, key_fn=keys.get) == (POOL_V4, 0)


def test_a_v4_hop_whose_currencies_exclude_our_mint_is_rejected():
    keys = {POOL_V4: {"currency0": WETH, "currency1": "0x" + "99" * 20},
            OTHER_V4: {"currency0": WETH, "currency1": MINT}}
    logs = [_swap(MGR, SWAP_V4, POOL_V4), _swap(MGR, SWAP_V4, OTHER_V4)]
    assert _sel(logs, key_fn=keys.get) == (OTHER_V4, 1)


def test_two_candidate_pools_both_holding_the_mint_FAIL_CLOSED():
    """Observed once in eight. An honest ambiguity — do not invent a tie-break."""
    keys = {POOL_V4: {"currency0": WETH, "currency1": MINT},
            OTHER_V4: {"currency0": WETH, "currency1": MINT}}
    logs = [_swap(MGR, SWAP_V4, POOL_V4), _swap(MGR, SWAP_V4, OTHER_V4)]
    assert _sel(logs, key_fn=keys.get) is None


def test_no_swap_log_at_all_fails_closed():
    assert _sel([_transfer(MINT, POOL_V3, "0x" + "11" * 20)]) is None


def test_a_v4_key_lookup_that_fails_does_not_admit_the_pool():
    """An RPC miss must not become 'this pool holds our mint'. Fail closed, never fail open."""
    logs = [_swap(MGR, SWAP_V4, POOL_V4)]
    assert _sel(logs, key_fn=lambda pid: None) is None


def test_the_returned_log_index_points_at_the_selected_swap():
    """Persisted for auditability: the derivation must be re-checkable from the receipt later."""
    keys = {POOL_V4: {"currency0": WETH, "currency1": MINT}}
    logs = [_transfer(MINT, "0x" + "11" * 20, "0x" + "22" * 20), _swap(MGR, SWAP_V4, POOL_V4)]
    pool, idx = _sel(logs, key_fn=keys.get)
    assert (pool, idx) == (POOL_V4, 1)
    assert logs[idx]["topics"][1] == POOL_V4


def test_mint_matching_is_case_insensitive():
    keys = {POOL_V4: {"currency0": WETH, "currency1": MINT.upper()}}
    assert _sel([_swap(MGR, SWAP_V4, POOL_V4)], key_fn=keys.get) == (POOL_V4, 0)


def test_one_pool_appearing_twice_reports_the_FIRST_log_index():
    """A pool can be swapped through more than once in one route. The pool is unambiguous, but the
    persisted log index is the audit witness, so which occurrence is recorded must be deliberate
    and stable — otherwise re-deriving the answer later lands on a different log."""
    keys = {POOL_V4: {"currency0": WETH, "currency1": MINT}}
    logs = [_swap(MGR, SWAP_V4, POOL_V4), _transfer(MINT, "0x" + "11" * 20, "0x" + "22" * 20),
            _swap(MGR, SWAP_V4, POOL_V4)]
    assert _sel(logs, key_fn=keys.get) == (POOL_V4, 0)
