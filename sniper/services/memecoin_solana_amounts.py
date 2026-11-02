"""Solana swap amounts from token-balance deltas — program-agnostic (unlock step 2, brief §5).

The Solana arrival lane records slot + signature only, so the exit brain has no flow and is forced
to legacy. Decoding each AMM's instruction layout (Raydium / Pump AMM / Meteora / whatever launches
next week) is a treadmill. `getTransaction` already reports `meta.preTokenBalances` and
`meta.postTokenBalances` for every account the transaction touched, so the swap's true size and
direction fall out of the BALANCE DELTAS regardless of which program moved them.

Direction convention matches the EVM tape exactly: **positive = INTO the pool**. The pool is found
STRUCTURALLY, not by program ID: a pool vault authority is the only owner holding BOTH the coin and
a quote asset in the same transaction, so its balance deltas ARE the pool deltas. (Verified against
live mainnet transactions 2026-07-24 — an earlier trader-centric version assumed the fee payer owned
the token accounts, which is false: real swaps route through PDAs and relayer bots, and the fee payer
typically owns none of them.)

STRICTLY READ-ONLY, on `solana_read_rpc()`. Never `MEMECOIN_SOLANA_RPC_URL`: the funded keypair
exists on this host and that variable is what arms the real Solana leg.

Fail-closed: anything ambiguous (no trader delta, missing balances, unparseable amounts) returns
None. A swap we cannot size honestly is not a swap we record.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memecoin")

# canonical Solana quote assets
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_MINTS = (WSOL, USDC)
NATIVE_SOL = "native-sol"
# Official Pump program address (pump-fun/pump-public-docs, PUMP_PROGRAM_README.md).
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _amount(entry):
    try:
        return int((entry.get("uiTokenAmount") or {}).get("amount"))
    except (TypeError, ValueError, AttributeError):
        return None


def _fee_payer(tx):
    """The signer — account 0 of the message, in either encoding."""
    try:
        keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    except AttributeError:
        return None
    if not keys:
        return None
    k = keys[0]
    return k.get("pubkey") if isinstance(k, dict) else k


def token_deltas(tx):
    """{(mint, owner): signed_delta} across every token account the tx touched.

    post - pre, treating an account absent from `pre` as having started at zero (a token account
    opened inside the transaction — extremely common on Solana).
    """
    meta = (tx or {}).get("meta") or {}
    pre, post = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
    bal = {}
    for entry, sign in ((pre, -1), (post, +1)):
        for e in entry:
            amt = _amount(e)
            if amt is None:
                continue
            key = (e.get("mint"), e.get("owner"))
            bal[key] = bal.get(key, 0) + sign * amt
    return bal


def swap_amounts(tx, coin_mint, *, quote_mints=QUOTE_MINTS):
    """Signed (coin_into_pool, quote_into_pool) atomic amounts, or None.

    Positive = INTO the pool, matching the EVM tape. The pool authority is identified structurally
    as the only owner holding BOTH the coin and a quote asset, so its deltas are used directly — no
    AMM layout, no program ID, no trader inference. Fail-closed on 0 pools (not a swap of this coin)
    and on >1 (a multi-hop route touching several pools of the same coin is ambiguous, and guessing
    which leg is 'the' market would fabricate flow).
    """
    if not coin_mint:
        return None
    deltas = token_deltas(tx)
    if not deltas:
        return None
    owners_with_coin = {o for (m, o) in deltas if m == coin_mint and o}
    pools = [o for o in owners_with_coin
             if any((q, o) in deltas for q in quote_mints)]
    if len(pools) != 1:
        return None
    pool = pools[0]
    coin_d = deltas.get((coin_mint, pool), 0)
    quote_d = sum(deltas.get((q, pool), 0) for q in quote_mints)
    if coin_d == 0 or quote_d == 0:
        return None
    if (coin_d > 0) == (quote_d > 0):
        return None                       # both legs same direction: LP op, not directional flow
    return (coin_d, quote_d)              # already pool-side; no negation


def swap_row(tx, coin_mint, *, slot, signature, chain_id="solana", observed_at=None,
             quote_mints=QUOTE_MINTS):
    """A pool_ticks-shaped row with real amounts, or None. `amount0`=coin, `amount1`=quote, so a
    caller uses quote_side=1 — the same shape the EVM tape and compute_state already consume."""
    amts = swap_amounts(tx, coin_mint, quote_mints=quote_mints)
    if amts is None:
        return None
    coin_in, quote_in = amts
    return {"chain_id": chain_id, "pool": coin_mint, "kind": "sol_swap", "block_no": slot,
            "tx_sig": signature, "log_index": 0, "maker": _fee_payer(tx),
            "amount0": str(coin_in), "amount1": str(quote_in), "observed_at": observed_at}


# VENUE PROGRAMS (2026-10-21, LP rail): the AMM/launchpad programs a pool can belong to. Aggregators
# (Jupiter, OKX, DFlow) also list a pool's vaults on their outer instruction, so only these count.
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CLMM_PROGRAM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
METEORA_DLMM_PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_DAMM_V2_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
PUMP_CURVE_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
KNOWN_VENUE_PROGRAMS = frozenset({
    PUMPSWAP_PROGRAM, RAYDIUM_CPMM_PROGRAM, RAYDIUM_AMM_V4_PROGRAM, RAYDIUM_CLMM_PROGRAM,
    RAYDIUM_LAUNCHLAB_PROGRAM, METEORA_DLMM_PROGRAM, METEORA_DAMM_V2_PROGRAM, METEORA_DBC_PROGRAM,
    PUMP_CURVE_PROGRAM})


def venue_program(tx, coin_vault, quote_vault):
    """The ONE known venue program whose instruction (outer or inner) names both pool vaults in
    this jsonParsed transaction, else None (unknown, or ambiguous). Pure, never raises."""
    try:
        message = ((tx or {}).get("transaction") or {}).get("message") or {}
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (message.get("accountKeys") or [])]
        instructions = list(message.get("instructions") or [])
        for inner in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
            instructions.extend((inner or {}).get("instructions") or [])
        found = set()
        for ix in instructions:
            program = ix.get("programId")
            if program is None and isinstance(ix.get("programIdIndex"), int):
                program = keys[ix["programIdIndex"]] if ix["programIdIndex"] < len(keys) else None
            accounts = [keys[a] if isinstance(a, int) and a < len(keys) else a
                        for a in (ix.get("accounts") or [])]
            if program in KNOWN_VENUE_PROGRAMS and coin_vault in accounts and quote_vault in accounts:
                found.add(program)
        return next(iter(found)) if len(found) == 1 else None
    except Exception:
        return None


def account_states(tx):
    """{token_account_address: (mint, owner, delta, post)} keyed by the ACCOUNT ADDRESS.

    The vault-keyed lane. (mint, owner) keying merges every vault an owner holds of one mint,
    which is exactly what makes multi-hop routes ambiguous — and live verification 2026-07-24
    showed our OWN wallet resolves as a pool candidate on our own fills (we hold both coin and
    quote in the fill tx). Keying by address is exact: one physical vault, one entry.
    """
    try:
        keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    except AttributeError:
        return {}
    addrs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    meta = (tx or {}).get("meta") or {}
    pre = {}
    for e in meta.get("preTokenBalances") or []:
        amt = _amount(e)
        if amt is not None and e.get("accountIndex") is not None and e["accountIndex"] < len(addrs):
            pre[addrs[e["accountIndex"]]] = amt
    out = {}
    for e in meta.get("postTokenBalances") or []:
        amt = _amount(e)
        idx = e.get("accountIndex")
        if amt is None or idx is None or idx >= len(addrs):
            continue
        addr = addrs[idx]
        out[addr] = (e.get("mint"), e.get("owner"), amt - pre.get(addr, 0), amt)
    return out


def _account_keys(tx):
    try:
        keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")) or []
    except AttributeError:
        return []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in keys]


def _invokes_program(tx, program_id):
    """True, False, or None when the transaction carries no evidence either way.

    NONE IS NOT FALSE (2026-09-08). This read two sources — the parsed instructions, then the log
    lines — and both fell through `or []` to an empty sequence, so `any([])` returned False and a
    transaction that had never been fetched claimed the program was NOT invoked. A failed read and
    a successful read proving absence were the same answer. That is the shape that hid the Solana
    revert-name extractor for two weeks (`_program_error_name` fed logs the caller never sent) and
    the token-account lookup that reinstated the MOX exit bug on any RPC hiccup.

    Here it costs pump-curve rows in the tape rather than a trade, so the callers still decline to
    emit on None — but they decline knowing they had no evidence, instead of recording a negative
    they never observed.
    """
    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    instructions = message.get("instructions") or []
    keys = _account_keys(tx)
    for ins in instructions:
        if not isinstance(ins, dict):
            continue
        pid = ins.get("programId")
        if pid is None and ins.get("programIdIndex") is not None:
            try:
                pid = keys[int(ins["programIdIndex"])]
            except (IndexError, TypeError, ValueError):
                pid = None
        if pid == program_id:
            return True
    logs = (((tx or {}).get("meta") or {}).get("logMessages")) or []
    if any(str(line).startswith(f"Program {program_id} invoke") for line in logs):
        return True
    if not instructions and not logs:
        return None                     # neither source was present: we did not look, we cannot say
    return False


def _native_state(tx, address):
    keys = _account_keys(tx)
    try:
        idx = keys.index(address)
    except ValueError:
        return None
    meta = (tx or {}).get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return None
    try:
        before, after = int(pre[idx]), int(post[idx])
    except (TypeError, ValueError):
        return None
    return after - before, after


def resolve_pump_curve_accounts(tx, coin_mint, *, exclude_owner=None):
    """Resolve one native-SOL Pump bonding curve from a confirmed transaction.

    The program, token-vault movement, and curve PDA lamport movement must agree.
    `exclude_owner` removes the fee payer from the curve candidates.
    """
    if not coin_mint:
        return None
    if _invokes_program(tx, PUMP_PROGRAM_ID) is not True:
        return None                     # False (proven absent) and None (no evidence) both decline
    candidates = []
    for vault, (mint, owner, coin_delta, coin_post) in account_states(tx).items():
        if mint != coin_mint or not owner or coin_post <= 0 or coin_delta == 0:
            continue
        if exclude_owner is not None and str(owner) == str(exclude_owner):
            continue
        native = _native_state(tx, owner)
        if native is None:
            continue
        native_delta, native_post = native
        if native_post <= 0 or native_delta == 0:
            continue
        if (coin_delta > 0) == (native_delta > 0):
            continue
        candidates.append((owner, vault, NATIVE_SOL, owner))
    return candidates[0] if len(candidates) == 1 else None


def pump_curve_rows(tx, coin_mint, coin_vault, curve_owner, *, slot, signature,
                    observed_at=None):
    """Transaction-grounded native-SOL curve rows for one already-resolved Pump curve."""
    if _invokes_program(tx, PUMP_PROGRAM_ID) is not True:
        return []                       # False (proven absent) and None (no evidence) both decline
    coin = account_states(tx).get(coin_vault)
    native = _native_state(tx, curve_owner)
    if not coin or coin[0] != coin_mint or coin[1] != curve_owner or native is None:
        return []
    _mint, _owner, coin_delta, coin_post = coin
    native_delta, native_post = native
    if not coin_delta or not native_delta or (coin_delta > 0) == (native_delta > 0):
        return []
    base = {"chain_id": "solana", "pool": coin_vault, "block_no": slot,
            "tx_sig": signature, "maker": _fee_payer(tx), "observed_at": observed_at,
            "venue_kind": "pump_curve"}
    return [
        {**base, "kind": "sol_curve_sync", "log_index": 1,
         "amount0": str(coin_post), "amount1": str(native_post)},
        {**base, "kind": "sol_curve_swap", "log_index": 0,
         "amount0": str(coin_delta), "amount1": str(native_delta)},
    ]


def resolve_pool_accounts(tx, coin_mint, *, exclude_owner=None, required_owner=None,
                          quote_mints=QUOTE_MINTS):
    """(owner, coin_vault, quote_mint, quote_vault), exact from token-account history."""
    if not coin_mint:
        return None
    states = account_states(tx)
    owners = {}
    posts = {}
    for addr, (mint, owner, _d, post) in states.items():
        if owner and owner != exclude_owner:
            owners.setdefault(owner, {})[mint] = addr
            posts[(owner, mint)] = post
    cands = [(o, mints) for o, mints in owners.items()
             if coin_mint in mints and any(q in mints for q in quote_mints)]
    if required_owner is not None:
        cands = [(owner, mints) for owner, mints in cands
                 if str(owner) == str(required_owner)]
    # A real pool holds inventory AFTER a swap; a router's pass-through ATAs end at zero. Apply
    # this even to a single candidate: one zero-balance router is still not a pool.
    cands = [(o, mints) for o, mints in cands
             if (posts.get((o, coin_mint)) or 0) > 0
             and any((posts.get((o, q)) or 0) > 0 for q in quote_mints if q in mints)]
    if len(cands) != 1:
        return None
    owner, mints = cands[0]
    qmint = next((q for q in quote_mints if q in mints and (posts.get((owner, q)) or 0) > 0),
                 next(q for q in quote_mints if q in mints))
    return owner, mints[coin_mint], qmint, mints[qmint]


def resolve_pool_accounts_dynamic(tx, coin_mint, *, exclude_owner=None, required_owner=None,
                                  quote_ok_fn=None, quote_mints=QUOTE_MINTS):
    """The standard WSOL/USDC resolution, then a DYNAMIC-quote pass for exotic pairings.

    2026-08-14, PEECOIN: the launchpad meta now pairs memecoins against tokenized assets — its
    CLMM pool quotes in GLDx (tokenized gold, ~$401, $350K liquidity) — so the coin could never
    bind and its display fell back to aggregator mids that mispriced a close by 54%. The dynamic
    pass accepts a non-standard quote ONLY when exactly one non-payer owner holds the coin plus
    exactly ONE other positive-balance mint, AND `quote_ok_fn(mint)` confirms that mint is
    independently USD-priceable with real liquidity. No validator -> no dynamic pass; junk/junk
    pairings fail the validator and stay unbound. Fail-closed everywhere."""
    resolved = resolve_pool_accounts(
        tx, coin_mint, exclude_owner=exclude_owner, required_owner=required_owner,
        quote_mints=quote_mints)
    if resolved or quote_ok_fn is None:
        return resolved
    states = account_states(tx)
    owners = {}
    posts = {}
    for addr, (mint, owner, _d, post) in states.items():
        if owner and owner != exclude_owner:
            owners.setdefault(owner, {})[mint] = addr
            posts[(owner, mint)] = post
    cands = []
    for owner, mints in owners.items():
        if required_owner is not None and str(owner) != str(required_owner):
            continue
        if coin_mint not in mints or (posts.get((owner, coin_mint)) or 0) <= 0:
            continue
        others = [m for m in mints if m != coin_mint and (posts.get((owner, m)) or 0) > 0]
        if len(others) == 1:
            cands.append((owner, mints, others[0]))
    if len(cands) != 1:
        return None
    owner, mints, qmint = cands[0]
    try:
        if not quote_ok_fn(qmint):
            return None
    except Exception:
        return None
    return owner, mints[coin_mint], qmint, mints[qmint]


def resolve_pool(tx, coin_mint, *, exclude_owner=None, quote_mints=QUOTE_MINTS):
    """(pool_owner, coin_vault_address, quote_mint) from one tx of this coin, or None.

    Structural rule as `swap_amounts`, PLUS `exclude_owner` (our funded wallet): on our own fill
    the trader holds both the coin and a quote asset, so without the exclusion the resolver sees
    two candidates and fails closed on every one of our own entries (found live 2026-07-24).
    Still fail-closed on 0 or >1 remaining candidates.
    """
    resolved = resolve_pool_accounts(
        tx, coin_mint, exclude_owner=exclude_owner, quote_mints=quote_mints)
    return resolved[:3] if resolved else None


def vault_rows(tx, coin_mint, coin_vault, *, slot, signature, observed_at=None,
               quote_mints=QUOTE_MINTS, quote_vault=None):
    """pool_ticks rows for the KNOWN pool (keyed by its coin-vault address) from one tx.

    Exact — no multi-hop ambiguity: the coin leg is the named vault's own delta; the quote leg is
    its owner's quote vault in the same tx. Emits (verified live 2026-07-24, sign rule confirmed
    against instruction logs — Buy/Sell for opposite-sign, RemoveLiquidityByRange2 for same-sign):

    - `sol_swap`  — opposite-sign legs (a trade). amount0=coin, amount1=quote, positive = INTO pool.
    - `sol_mint` / `sol_burn` — SAME-sign legs (LP add / remove). `sol_burn` is the direct
      liquidity-pull signal, the Solana analog of `v2_burn`.
    - `sol_sync`  — ALWAYS when the vault is touched: the pool's POST balances (reserves), the
      Solana analog of `v2_sync`. One-sided events (single-sided CLMM liquidity, fee ops) emit
      only this — the reserve level still moves, but no directional flow is fabricated.

    Returns [] when this tx does not touch the vault. `pool` on every row = the coin-vault
    address (the enumeration key), never the mint — one coin can have many pools.

    `quote_vault` is the pool's BOUND quote-vault address from `sol_pool_vaults`. When given it is
    used exclusively and without a mint filter; when absent the owner+mint match is used, which is
    what every caller did before 2026-09-23.
    """
    states = account_states(tx)
    coin = states.get(coin_vault)
    if not coin or coin[0] != coin_mint:
        return []
    _mint, owner, coin_d, coin_post = coin
    quote_d, quote_post = 0, None
    if quote_vault:
        # THE BOUND ADDRESS WINS, AND IT IS EXCLUSIVE (2026-09-23).
        #
        # The owner+mint match below assumes one authority holds both sides. A pool whose quote
        # vault sits under a DIFFERENT program-derived address therefore found no quote leg
        # whichever mint was passed. Measured on data/memecoin_sol_tape.db: 60 of 60 pools still
        # emitting null-quote syncs after the 2026-09-22 quote-mint fix DID carry a bound
        # `quote_vault` in `sol_pool_vaults` — the pool's own SOL side, stored and unused. The worst,
        # 2QrpqAEsi7wcrynfpoDbFNZDrDGiMC5bQJEU2H1r9mxk, is the pool this module's pull-lane caller
        # names as having held zero ticks for its entire life.
        #
        # NO MINT FILTER: the binding already decided which asset quotes this pool, so re-checking
        # it against `quote_mints` would reintroduce the very defect the address fixes.
        #
        # NO FALLBACK when the bound vault is absent from the transaction. That means the quote side
        # did not move, which is a known zero for the swap legs and an UNKNOWN reserve — falling
        # back to an owner match here would adopt an unrelated vault as this pool's quote side.
        bound = states.get(quote_vault)
        if bound:
            quote_d, quote_post = bound[2], bound[3]
    else:
        for _addr, (mint, own, d, post) in states.items():
            if own == owner and mint in quote_mints:
                quote_d += d
                quote_post = post if quote_post is None else quote_post + post
    base = {"chain_id": "solana", "pool": coin_vault, "block_no": slot, "tx_sig": signature,
            "maker": _fee_payer(tx), "observed_at": observed_at}
    rows = [{**base, "kind": "sol_sync", "log_index": 1,
             "amount0": str(coin_post),
             "amount1": str(quote_post) if quote_post is not None else None}]
    if coin_d and quote_d:
        if (coin_d > 0) == (quote_d > 0):
            kind = "sol_mint" if coin_d > 0 else "sol_burn"
        else:
            kind = "sol_swap"
        rows.append({**base, "kind": kind, "log_index": 0,
                     "amount0": str(coin_d), "amount1": str(quote_d)})
    return rows


def get_transaction_fn(url=None, *, call=None, commitment=None):
    """Build `signature -> tx json | None` on the READ-ONLY endpoint. `call` injected for tests.

    `commitment=None` sends no commitment key, so the RPC applies its own default of `finalized`.
    That default is ~32 slots (~13 s) behind a fill, which is correct for a caller that must not
    act on a reorg-able transaction and far too slow for one that only needs to READ a landed
    transaction. Pass `confirmed` for the second case; see the funded pool-bind call site.
    """
    if call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call
        from sniper.settings.memecoin import solana_read_rpc
        url = url or solana_read_rpc()
        if not url:
            return None

        def call(method, params):
            return _rpc_call(url, method, params, timeout=15)

    opts = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}
    if commitment:
        opts["commitment"] = str(commitment)

    def get_transaction(signature):
        try:
            return call("getTransaction", [signature, dict(opts)])
        except Exception:
            logger.exception("solana getTransaction failed sig=%s", signature)
            return None

    return get_transaction
