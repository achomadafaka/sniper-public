"""How old a Solana coin was when we entered it, derived from the chain.

WHY. `entry_token_age_s` comes from DexScreener's `pairCreatedAt`
(memecoin_real_runner._funded_market_stats -> facts["pair_age_s"] -> the reservation -> the
position). A LaunchLab curve is not indexed before it graduates, so there is no pair, no
pairCreatedAt and no age. Measured on funded Solana closes since 2026-09-01:

    LaunchLab      n=32   with age   2   ( 6.2%)
    Raydium CPMM   n=104  with age  97   (93.3%)
    DAMM v2        n=16   with age  16   (100%)
    DBC            n=13   with age   7   (53.8%)
    other          n=177  with age 164   (92.7%)
    unbound pools  n=13   with age   0   (0%)

THE CLASSIFIER IS `sol_pool_vaults.program` in data/memecoin_sol_tape.db, taking each mint's most
frequent non-null program; a mint with no bound pool is "unbound". Re-run it to reproduce the table
above. The counts are as of 2026-09-22 BEFORE the mint_first_slot backfill: that backfill added 2
ages, both in the LaunchLab bucket, so it now reads 4 of 33 there, and two later closes take the
total from 355 to 357.

THE NUMBERS ARE MEASURED, THE VENUE NAMES ARE NOT. `program` holds the vault OWNER, not the venue's
program id -- the published ids for LaunchLab, Raydium CPMM, DAMM v2 and DBC all match zero rows in
this table. So the partition and every count above are exact and reproducible, while the label on
each bucket is an unverified hand mapping from an opaque address. Treat a bucket NAME as a
hypothesis. The addresses themselves are the durable key: LaunchLab is WLHv2UAZ..., Raydium CPMM is
GpMZbSM2..., DAMM v2 is HLnpSz9h..., DBC is FhVo3mqL...

POST-ENTRY, NEVER ON THE HOT PATH. Age at entry is a HISTORICAL fact — the entry time minus the
mint's creation time — so it can be derived after the fill at no cost to the funded critical path,
and the same derivation backfills rows we already hold. The 2026-07-15 entry-speed spec forbids
adding an unmeasured RPC to the entry path, and a copy-only runner has no age gate to feed anyway.
If an age gate is ever justified, reading it at decision time is a separate build.

THE TWO SOURCES ARE NEVER MIXED. `dexscreener_pair` is the venue's own pair timestamp;
`mint_first_slot` is an inference from the oldest signature on the mint account. A derived value
never overwrites an indexed one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memecoin")

SOURCE_PAIR = "dexscreener_pair"
SOURCE_MINT_FIRST_SLOT = "mint_first_slot"

# A bound, not a tuning knob: an old coin can carry tens of thousands of signatures and walking
# them all to date a position we already closed is not worth the reads. Unreachable bottom =
# no answer, which is the honest outcome — never a guessed age.
MAX_PAGES = 6
PAGE_LIMIT = 1000


def mint_first_block_time(mint, *, get_sigs_fn, max_pages=MAX_PAGES, page_limit=PAGE_LIMIT):
    """The blockTime of the OLDEST signature on `mint`, or None.

    Walks backwards with `before=` until a page comes back short, which is the bottom. Returns
    None when the bottom is not reached inside `max_pages`: a partial walk gives the oldest
    signature WE SAW, which is an upper bound on the creation time and would understate the coin's
    age. An understated age is worse than none — it would make an old coin look fresh.
    """
    before = None
    oldest = None
    for _page in range(int(max_pages)):
        try:
            params = {"limit": int(page_limit)}
            if before:
                params["before"] = before
            sigs = get_sigs_fn(str(mint), params) or []
        except Exception:
            logger.exception("token age: signature page failed mint=%s", mint)
            return None
        if not sigs:
            break
        for entry in sigs:
            ts = (entry or {}).get("blockTime")
            if ts:
                oldest = int(ts) if oldest is None else min(oldest, int(ts))
        before = (sigs[-1] or {}).get("signature")
        if len(sigs) < int(page_limit):
            return oldest                      # short page = we reached the bottom
        if not before:
            return None
    return None                                # bottom not reached: no honest answer


def age_seconds(opened_at, first_block_time):
    """Seconds between the coin's first on-chain activity and our entry, or None.

    A negative result means the entry predates the mint's oldest signature we found, which is
    impossible and therefore evidence the walk was wrong. It returns None rather than clamping to
    zero, because a zero would read as "brand new" — the most consequential value this field has.
    """
    if first_block_time is None:
        return None
    from datetime import datetime
    try:
        opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        if opened.tzinfo is None or opened.utcoffset() is None:
            return None
        age = opened.timestamp() - float(first_block_time)
    except (TypeError, ValueError, OverflowError):
        return None
    if age < 0:
        return None
    return age
