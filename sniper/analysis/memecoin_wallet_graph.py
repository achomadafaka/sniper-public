"""WO-6 — predictive rug: wallet-graph CONCEPT ported from the Polymarket funding graph.

The Polymarket channel (sniper/analysis/winning_wallet_thesis/funding_graph.py) resolves a wallet's
funder and detects coordinated entries. The memecoin data model differs completely, so this ports the
CONCEPT, not the code: for a held coin, resolve the DEPLOYER + LP-provider wallets from the pool's
recorded creation history, watch them, and flag when a watched wallet acts in the tape window — the
shape that precedes a rug (LP pull / dump).

Like the funding graph: pure given injected feeds, and it degrades LOUDLY — no maker data / no RPC ->
empty watch-set with an explicit data_gap, NEVER a phantom "deployer active" claim. SHADOW ONLY: it
logs a context flag; no rule may consume it without a new prereg (the shadow read is the go/no-go).

Swap makers are not creation identity and can never establish a deployer. `sig_fetch(pool)` is the
injectable creation-history source; its rows must explicitly identify the creator/deployer. Until
that feed exists, this surface reports unavailable rather than upgrading an actor proxy into fact.
"""
from __future__ import annotations


def _norm(a):
    return (str(a or "")).strip()


def resolve_watch_set(pool, pool_ticks, *, top_lps=3, sig_fetch=None, router_addrs=None):
    """Deployer + top LP-provider wallets for one pool from its recorded creation history.

    `pool_ticks`: rows with maker/block identity plus explicit creation or liquidity roles. Deployer
    comes only from a creator/deployer row. LP providers come only from explicit liquidity-add rows.
    `router_addrs` are excluded (the router is every pool's maker, not a real actor).

    `sig_fetch(pool)` (optional): a Solana genesis walk returning [{maker, block_no}] older than the
    tape reaches; merged BEFORE selection so the deployer is the true earliest, not just the earliest
    we happened to record. Absent -> tape-only (documented as a coverage limit, never a fabrication).

    Returns {deployer, lp_providers, resolvable, data_gaps}. resolvable=False + a data_gap when no
    maker exists anywhere — the caller must treat an unresolvable pool as "no signal", not "safe"."""
    excl = {_norm(a) for a in (router_addrs or set())}
    rows = [dict(r) for r in pool_ticks]
    if sig_fetch is not None:
        try:
            older = sig_fetch(pool) or []
            rows = [dict(r) for r in older] + rows          # genesis-first, then tape
        except Exception as e:  # noqa: BLE001 — surface a gap, never a silent claim
            rows.append({"_gap": f"sig_fetch failed: {e}"})
    data_gaps = [r["_gap"] for r in rows if isinstance(r, dict) and r.get("_gap")]
    withmaker = [r for r in rows if isinstance(r, dict) and _norm(r.get("maker"))
                 and _norm(r.get("maker")) not in excl and r.get("block_no") is not None]
    creators = [r for r in withmaker if str(r.get("role") or "").lower() in
                {"deployer", "pool_creator", "creator"}
                or str(r.get("kind") or "").lower() in {"create_pool", "initialize_pool"}]
    if not creators:
        return {"deployer": None, "lp_providers": [], "resolvable": False,
                "data_gaps": data_gaps + ["creation-history deployer unavailable (unresolvable)"]}
    creators.sort(key=lambda r: r["block_no"])
    deployer = _norm(creators[0]["maker"])
    # Only explicit liquidity-add actors are LP providers. Swap frequency is not LP identity.
    freq = {}
    for r in withmaker:
        mk = _norm(r["maker"])
        role = str(r.get("role") or "").lower()
        kind = str(r.get("kind") or "").lower()
        if mk != deployer and (role in {"lp", "lp_provider"}
                               or kind == "lp_add" or kind.endswith("mint")
                               or kind.endswith("add_liquidity")):
            freq[mk] = freq.get(mk, 0) + 1
    lp_providers = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:top_lps]]
    return {"deployer": deployer, "lp_providers": lp_providers, "resolvable": True,
            "data_gaps": data_gaps}


def deployer_activity(watch_set, recent_ticks):
    """Did a WATCHED wallet (deployer or an LP provider) act in the recent tape window?

    Returns {active, wallets}. active=True with the intersecting wallets when a watched maker appears;
    an unresolvable watch_set (no deployer, no LPs) is active=False with NO claim (never guess a rug)."""
    if not watch_set or not watch_set.get("resolvable"):
        return {"active": False, "wallets": []}
    watched = {watch_set.get("deployer")} | set(watch_set.get("lp_providers") or [])
    watched.discard(None)
    seen = {_norm(r.get("maker")) for r in recent_ticks if isinstance(r, dict)}
    hit = sorted(w for w in watched if w in seen)
    return {"active": bool(hit), "wallets": hit}
