"""A standing quote-token allowance for the pinned chain-56 routers (2026-10-07, operator YES).

Spec docs/superpowers/specs/2026-10-07-bsc-standing-allowance-design.md. A 56 entry approved its
router for the exact WBNB amount every time (evm.approval p50 186 / p90 505 ms since 10-01). Each
pinned router now keeps an allowance worth MEMECOIN_EVM_STANDING_ALLOWANCE_CAP_USD, and this book
counts it: V moves only when we approve or spend, and is never read from chain. An entry V covers
skips its approve; any other runs today's exact approve.

EVERY RULE ERRS LOW. A short count only falls back to today's approve; an over-count is a swap that
reverts on transferFrom. So: a fill never adds back, an exact approve zeroes the count and taints
an in-flight top-up, a top-up that did not confirm never raises it, and a standing entry that did
not fill makes it unknown. The book is process-wide, under one lock.
"""
from __future__ import annotations

import itertools
import math
import threading
from fractions import Fraction

# The routers 56 fills pulled through (09-25..10-07, n=62): Pancake V2 Router 33, Smart Router 13,
# V3 SwapRouter 8, KyberSwap 4. The flap portal is paid in native BNB; Permit2 and the Relay proxy
# were never used on 56. Any other spender keeps the exact approve.
PINNED_SPENDERS = {"56": frozenset({
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",   # PancakeSwap V2 Router
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",   # PancakeSwap Smart Router (mixed_v3_v2)
    "0x1b81d678ffb9c0263b24a97847620c99d213eb14",   # PancakeSwap V3 SwapRouter (v3, v3_bridge)
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",   # KyberSwap router
}),
    # BASE (operator yes 2026-10-15 via Alpha, same terms as 56). Permit2 first: the WETH spender
    # of every V4/Universal Router entry (7 of 12 real Base fills since 09-15); the UR itself takes
    # an in-transaction PERMIT2_PERMIT signature and is never an ERC-20 spender. Every other Base
    # spender keeps today's exact approve until it is proven the same way (see PIN_PROOFS).
    "8453": frozenset({
        "0x000000000022d473030f116ddee9f6b43ac78ba3",   # Permit2
    }),
    # ROBINHOOD CHAIN (operator "3 yes sure" 2026-10-21 via Alpha, brain 205ef146; same terms):
    # Permit2 is the WETH spender of 4663 V4 in-swap-unwrap entries, 6 of the 10 entries since
    # 10-18 that paid an exact approve (176-277 ms each). KyberSwap and the Pons router keep it.
    "4663": frozenset({
        "0x000000000022d473030f116ddee9f6b43ac78ba3",   # Permit2
    })}

# ON-CHAIN PROOF OF EACH NON-56 PIN, recorded when it was pinned (rerun the script to re-prove):
# scripts/research/prove_standing_spender_8453.py against https://mainnet.base.org.
PIN_PROOFS = {
    ("8453", "0x000000000022d473030f116ddee9f6b43ac78ba3"): {
        "proven_at": "2026-10-15", "code_bytes": 9152,
        "code_hash": "0xa67739abc3ede9dbdc0491636c67d6a14ac07fab9030c3f509b1eb7b11dff8ed",
        # DOMAIN_SEPARATOR() == keccak(EIP712Domain typehash, keccak("Permit2"), 8453, address)
        "domain_separator": "0x3b6f35e4fce979ef8eac3bcdc8c3fc38fe7911bb0c69c8fe72bf1fd1a17e6f07",
    },
    # 2026-10-21, the 4663 research dRPC host (never the funded pool). The code hash differs from
    # Base's: Permit2 bakes its chain id and address into immutables; the domain is what proves it.
    ("4663", "0x000000000022d473030f116ddee9f6b43ac78ba3"): {
        "proven_at": "2026-10-21", "code_bytes": 9152,
        "code_hash": "0x5208783f52488f7d3493e5e38311ab707c1d75457fe472a19b0b4d57d66a7fca",
        "domain_separator": "0x448684463b1f7965c1ec7c249cee11520df24c07242efc2b20f6e54c85614fad",
    },
}

_LOCK = threading.Lock()
_BOOKS = {}
_SEQ = itertools.count(1)
_COUNTS = {"standing": 0, "fallback": 0, "unfilled_unknown": 0,
           "topups_confirmed": 0, "topups_tainted": 0, "topups_failed": 0, "seeded": 0}


def reset_state():
    """Test hook. Production never calls this."""
    with _LOCK:
        _BOOKS.clear()
        for name in _COUNTS:
            _COUNTS[name] = 0


def pinned(chain, spender):
    return str(spender or "").lower() in PINNED_SPENDERS.get(str(chain), ())


def book_key(chain, wallet, token, spender):
    return (str(chain), str(wallet).lower(), str(token).lower(), str(spender).lower())


def _book(key):
    return _BOOKS.setdefault(key, {"v": None, "open": {}, "topup": None, "clip": None, "usd": None,
                                   "gen": 0})


def _learn(book, amount, token_usd):
    book["clip"] = int(amount)
    if token_usd:
        book["usd"] = float(token_usd)


def value(key):
    with _LOCK:
        return _book(key)["v"]


def reserve(key, amount, *, token_usd=None):
    """A reservation id when the count covers `amount` (and it is spent), else None."""
    amount = int(amount)
    with _LOCK:
        book = _book(key)
        _learn(book, amount, token_usd)
        if amount <= 0 or book["v"] is None or book["v"] < amount:
            _COUNTS["fallback"] += 1
            return None
        book["v"] -= amount
        book["gen"] += 1
        rid = next(_SEQ)
        book["open"][rid] = amount
        if book["topup"] is not None:
            book["topup"]["post"] += amount   # signs after the top-up's nonce: mines after it
        _COUNTS["standing"] += 1
        return rid


def exact_approved(key, amount, *, token_usd=None):
    """Today's exact approve ran: it OVERWRITES the allowance, whatever the order with a top-up."""
    with _LOCK:
        book = _book(key)
        _learn(book, amount, token_usd)
        book["v"] = 0
        book["gen"] += 1
        if book["topup"] is not None:
            book["topup"]["tainted"] = True


def settle(key, rid, filled):
    with _LOCK:
        book = _book(key)
        book["open"].pop(rid, None)
        book["gen"] += 1
        if not filled:
            book["v"] = None              # reverted, aborted or unknown: re-learn from a top-up
            _COUNTS["unfilled_unknown"] += 1


def topup_due(key):
    with _LOCK:
        book = _book(key)
        return (book["topup"] is None and bool(book["usd"])
                and (book["v"] is None or bool(book["clip"]) and book["v"] < 2 * book["clip"]))


def bootstrap_price(key, token_usd):
    """Seed startup pricing only; an unknown allowance stays unknown until confirmation."""
    if type(token_usd) not in (int, float) or not math.isfinite(token_usd) or token_usd <= 0:
        return False
    with _LOCK:
        book = _book(key)
        if book["v"] is None and book["usd"] is None:
            book["usd"] = float(token_usd)
    return True


def seed_generation(key):
    """Taken BEFORE the on-chain allowance read: seed_allowance applies only if nothing touched the
    book in between (an exact approve, a reservation, a settle or a top-up)."""
    with _LOCK:
        return _book(key)["gen"]


def seed_allowance(key, allowance_atomic, cap_atomic_value, generation):
    """STARTUP SEED (Gamma, 2026-10-21): the books live in memory, so every restart re-sent one cap
    approve per pinned spender. Seed an UNKNOWN count from the on-chain allowance only when it is at
    or above the cap; below it the count stays unknown and today's single top-up runs."""
    if (type(allowance_atomic) is not int or allowance_atomic < 0 or not cap_atomic_value
            or allowance_atomic < int(cap_atomic_value)):
        return False
    with _LOCK:
        book = _book(key)
        if (book["v"] is not None or book["topup"] is not None or book["open"]
                or book["gen"] != generation):
            return False
        book["v"] = int(allowance_atomic)
        _COUNTS["seeded"] += 1
        return True


def last_price(key):
    with _LOCK:
        return _book(key)["usd"]


def topup_begin(key):
    """Before the top-up claims its nonce: every reservation open now may still pull after it.
    False when one is already in flight (Gamma LOW: a second top-up thread after a context rebuild
    would replace it and drop the reservations between the two, an over-count)."""
    with _LOCK:
        book = _book(key)
        if book["topup"] is not None:
            return False
        book["topup"] = {"post": sum(book["open"].values()), "tainted": False}
        book["gen"] += 1
        return True


def topup_end(key, outcome, *, cap_atomic=None):
    with _LOCK:
        book = _book(key)
        topup, book["topup"] = book["topup"], None
        if outcome != "confirmed" or topup is None or cap_atomic is None:
            _COUNTS["topups_failed"] += 1           # never raises the count
            return "failed"
        if topup["tainted"]:
            book["v"] = 0
            _COUNTS["topups_tainted"] += 1
            return "tainted"
        book["v"] = max(0, int(cap_atomic) - topup["post"])
        _COUNTS["topups_confirmed"] += 1
        return "confirmed"


def cap_atomic(cap_usd, token_usd, decimals):
    """floor(cap_usd / token_usd x 10^decimals), or None without a positive price."""
    try:
        if not token_usd or float(token_usd) <= 0:
            return None
        return int(Fraction(str(cap_usd)) / Fraction(str(float(token_usd))) * 10 ** int(decimals))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def snapshot():
    """Heartbeat projection: the count per (chain, spender) and the top-up outcomes."""
    with _LOCK:
        spenders = {"%s:%s" % (key[0], key[3]): {
            "v_atomic": None if book["v"] is None else str(book["v"]),
            "open": len(book["open"]), "topup_in_flight": book["topup"] is not None}
            for key, book in _BOOKS.items()}
        return {"spenders": spenders, **_COUNTS}
