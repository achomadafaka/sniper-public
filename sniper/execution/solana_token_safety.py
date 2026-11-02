"""Item 15 — pre-entry Solana token authority and transfer-fee gate.

The gate reads the mint with getAccountInfo (jsonParsed):
  - freeze authority must be null (a live freeze authority can freeze your token account = trapped),
  - mint authority must be null (a live mint authority = infinite-mint rug),
  - a Token-2022 fee must be immutable and within the funded limit,
  - a Token-2022 permanent delegate must be absent,
  - a Token-2022 transfer hook under the Meteora DBC pool authority must be absent (launchpad-gated).
Fail-CLOSED: any unreadable/malformed response => not safe => don't enter. RPC http is injectable.
"""
from __future__ import annotations

import json as _json

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PARSER_VERSION = "token-authority-v1"
# The Meteora DBC pool_authority PDA (seeds [b"pool_authority"], program dbcij3LW...). The test
# derives it from the seeds, so a typo here fails CI instead of silently matching nothing.
DBC_POOL_AUTHORITY = "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"


# WHY A READ FAILED IS NOT A VERDICT ON THE TOKEN (MCAT, 2026-09-23T21:27:24Z, attempt 760c3fe9).
# One `except Exception` around the read AND the parse turned four unrelated events into the single
# reason "mint account unreadable (fail-closed)", and MCAT was refused on it while being clean by
# every configured rule: no mint authority, no freeze authority, no permanent delegate, a 300 bps
# fee against a 300 bps limit, and a live fee authority that the settings admit. Its safety stage
# took 23.7 ms against a measured Solana baseline of p50 47.3 ms over 309 attempts, which is a call
# that came back fast with nothing rather than one that waited.
#
# NONE OF THESE ADMITS ANYTHING. All four still refuse. Three of them are the NODE's state and not
# the coin's, so they are re-askable by the existing retry lane, which runs every gate again.
READ_FAILED = "mint read failed (rpc) — fail-closed"
READ_REJECTED = "mint read rejected by node (fail-closed)"
ACCOUNT_NOT_VISIBLE = "mint account not visible yet (fail-closed)"
ACCOUNT_UNPARSEABLE = "mint account unparseable (fail-closed)"


def _mint_account(mint, rpc_url, http):
    """The raw account `value`. Raises as before; `read_mint_account` is the typed seam."""
    from sniper.execution.solana_wallet import _http
    http = http or _http
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]})
    _s, j, _d = http("POST", rpc_url, body=body)
    return j["result"]["value"]


def read_mint_account(mint, rpc_url, http):
    """(value, failure_reason). `failure_reason` is None only when an account came back.

    READ AT `confirmed` (2026-09-26, attempt 92dcc8ef). With no commitment the node answers at its
    FINALIZED default, ~32 slots (~9 s) behind, so a pump mint created seconds before the slot-lane
    fallback read as null and the lane's only attempt refused. Authority flags from a confirmed
    block are what the swap itself will execute against.

    ⚠️ A NULL ACCOUNT IS ALWAYS OUR NODE BEING BEHIND, never a mint that does not exist. This runs
    only because we are copying a BUY in that coin, so the mint provably exists on chain or the
    source's own transaction could not have executed. That is what makes it transient without
    needing the mint's age: the copy premise answers the question an age threshold would guess at.
    """
    from sniper.execution.solana_wallet import _http
    http = http or _http
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]})
    try:
        _s, j, _d = http("POST", rpc_url, body=body)
    except Exception:
        return None, READ_FAILED          # transport, timeout, non-2xx: urlopen raises all three
    if not isinstance(j, dict) or j.get("error") is not None:
        return None, READ_REJECTED        # a 200 carrying a JSON-RPC error is where a 429 lands
    result = j.get("result")
    if not isinstance(result, dict) or "value" not in result:
        return None, READ_REJECTED
    value = result.get("value")
    if value is None:
        return None, ACCOUNT_NOT_VISIBLE
    return value, None


def _transfer_fee_config(info, owner):
    """Return the conservative Token-2022 fee state, or None when the extension is absent."""
    if owner != TOKEN_2022_PROGRAM:
        return None
    for ext in (info.get("extensions") or []):
        if ext.get("extension") != "transferFeeConfig":
            continue
        state = ext.get("state") or {}
        try:
            older = int(state["olderTransferFee"]["transferFeeBasisPoints"])
            newer = int(state["newerTransferFee"]["transferFeeBasisPoints"])
        except (KeyError, TypeError, ValueError):
            return {"valid": False, "bps": None, "authority": None}
        valid = (0 <= older <= 10_000 and 0 <= newer <= 10_000
                 and "transferFeeConfigAuthority" in state)
        return {
            "valid": valid,
            "bps": max(older, newer) if valid else None,
            "authority": state.get("transferFeeConfigAuthority"),
        }
    return None


def _transfer_fee_bps(info, owner):
    """Return the conservative fee, None for no extension, or an unsafe sentinel."""
    config = _transfer_fee_config(info, owner)
    if config is None:
        return None
    return config["bps"] if config["valid"] else 10_001


def _permanent_delegate(info, owner):
    """The Token-2022 permanent-delegate address (can move any holder's tokens), or None."""
    if owner != TOKEN_2022_PROGRAM:
        return None
    for ext in (info.get("extensions") or []):
        if ext.get("extension") == "permanentDelegate":
            return (ext.get("state") or {}).get("delegate")
    return None


def _launchpad_hook_program(info, owner):
    """The transfer-hook program of a mint whose hook the Meteora DBC launchpad controls, or None.

    LAUNCHPAD-GATED COINS CANNOT BE BOUGHT BY ANY ROUTE OF OURS (2026-09-27). ZrueWB1Y, 74cmFdL5 and
    2iCpCQRK passed this gate as 'ok' and died at the quote (Jupiter TOKEN_NOT_TRADABLE, Raydium
    ROUTE_NOT_FOUND): their hook refuses any trade the launchpad did not co-sign ("FOMO_REQUIRED").
    Census over every Solana attempt mint 09-13..09-27 (1,873): 16 carry a transferHook extension
    and none was ever funded. 11 have a NULL program, which is a no-op hook (xStocks and others), so
    the extension alone is not the key. All 5 non-null programs sit under the DBC pool authority.
    A non-null hook under any other authority still goes to the quote, as before."""
    if owner != TOKEN_2022_PROGRAM:
        return None
    for ext in (info.get("extensions") or []):
        if ext.get("extension") != "transferHook":
            continue
        state = ext.get("state")
        if not isinstance(state, dict):
            return None
        program = state.get("programId")
        return program if program and state.get("authority") == DBC_POOL_AUTHORITY else None
    return None


def _has_transfer_fee(info, owner):
    bps = _transfer_fee_bps(info, owner)
    return bps is not None and bps > 0


def parse_token_authority(val):
    """WO-14 OBSERVATION-ONLY parse of a getAccountInfo `value` into the token-authority fields we
    diff over a hold. Reuses the entry-gate's Token-2022 handling (no second parser). Returns typed
    fields plus explicit `known` (False when the response was unreadable/unparseable). NEVER a
    decision — no rule/executor consumes this."""
    info = (((val or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    owner = (val or {}).get("owner")
    return {
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
        "supply": info.get("supply"),
        "token_program": owner,
        "is_token_2022": owner == TOKEN_2022_PROGRAM,
        "transfer_fee_bps": _transfer_fee_bps(info, owner),
        "permanent_delegate": _permanent_delegate(info, owner),
        "known": bool(info) and owner is not None,
    }


def token_safety_ok(mint, rpc_url, *, max_transfer_fee_bps, http=None,
                    allow_live_fee_authority=None):
    """Return the authority and immutable-fee gate result. All checks fail closed."""
    if allow_live_fee_authority is None:
        from sniper.settings import memecoin as _cfg
        allow_live_fee_authority = bool(getattr(
            _cfg, "MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY", False))
    val, failure = read_mint_account(mint, rpc_url, http)
    return token_safety_verdict(val, failure, max_transfer_fee_bps=max_transfer_fee_bps,
                                allow_live_fee_authority=allow_live_fee_authority)


def token_safety_verdict(val, failure, *, max_transfer_fee_bps, allow_live_fee_authority):
    """The gate's verdict on an ALREADY-READ mint account (value, failure_reason), the pair
    `read_mint_account` returns. Pure, so a read taken earlier in the same attempt (the band's, when
    MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain) is judged exactly as a fresh one would be."""
    if failure is not None:
        return False, failure
    try:
        info = val["data"]["parsed"]["info"]
        owner = val.get("owner")
    except Exception:
        return False, ACCOUNT_UNPARSEABLE
    if not isinstance(info, dict):
        # An account we CAN read but cannot understand is the one case here that is about the
        # token, so it stays terminal and out of the transient set.
        return False, ACCOUNT_UNPARSEABLE
    hook = _launchpad_hook_program(info, owner)
    if hook is not None:
        # NO ROUTE OF OURS CAN TRADE IT (Gamma 2026-10-08; operator (a') via Alpha): Jupiter answers
        # TOKEN_NOT_TRADABLE for these mints both ways, and no builder of ours builds DBC
        # Swap2WithTransferHook. Admitting one only turned this terminal refusal into a retried "no
        # quote" on the funded seam. (The old "requires the platform co-sign" was wrong for some hooks:
        # one-signer trades pass on 9eQyvzp3.) Terminal: absent from the retry lane's transient set.
        return False, f"launchpad-gated: transfer hook {hook[:8]} not tradable on our routes (Meteora DBC)"
    if info.get("freezeAuthority") is not None:
        return False, "live freeze authority (account can be frozen)"
    if info.get("mintAuthority") is not None:
        return False, "live mint authority (infinite-mint rug risk)"
    fee = _transfer_fee_config(info, owner)
    if _permanent_delegate(info, owner) is not None:
        return False, "live token-2022 permanent delegate"
    if fee is not None and not fee["valid"]:
        return False, "token-2022 transfer-fee schedule unreadable (fail-closed)"
    if fee is not None and fee["authority"] is not None and not allow_live_fee_authority:
        return False, "live token-2022 transfer-fee authority"
    if fee is not None and fee["bps"] > max_transfer_fee_bps:
        return False, (f"token-2022 transfer fee {fee['bps']} bps over "
                       f"{max_transfer_fee_bps} bps limit")
    if fee is not None and fee["bps"] > 0:
        # SAY WHICH ONE IT WAS. A live authority admitted on the epoch argument and an immutable
        # fee are different risks, and the ledger has to be able to tell them apart afterwards.
        immutable = "true" if fee["authority"] is None else "false"
        reason = (f"ok transfer_fee_bps={fee['bps']} "
                  f"fee_limit_bps={max_transfer_fee_bps} "
                  f"fee_authority_immutable={immutable}")
    else:
        reason = "ok"
    # (A Token-2022 capability refusal used to sit here. It was WRONG AT THIS LAYER: 54 of the 60
    # most recent funded Solana fills are Token-2022 mints, and every one of them routed and filled
    # through Jupiter, which handles the program correctly. Only the RAYDIUM fallback build cannot,
    # so the refusal now lives there — `solana_real_seams.raydium_swap_tx` — where it declines one
    # rung instead of the whole coin. Removed 2026-09-09 before it could cost meaningful flow; it
    # had fired once.)
    return True, reason
