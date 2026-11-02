"""Item 11b — ADVISORY Solana holder-concentration scan (rug-risk signal, NOT a gate yet).

Complements the sell-ability gate (solana_token_safety): that answers "can we exit?", this answers
"will the top holders dump on us?". Uses native RPC only (getTokenLargestAccounts + getTokenSupply)
— no new API dependency, no key.

DELIBERATELY ADVISORY / FAIL-OPEN. The honeypot lesson (2026-07-14): a fail-CLOSED rug scanner that
errors on some quirk would silently block EVERY entry and look exactly like a working gate. So this
NEVER raises and returns None on any failure; a caller must treat None as "unknown", never as "unsafe".
Promotion to a hard entry gate must wait until we've logged real concentration distributions. The
USDC supply canary below detects RPC/parse failure, but it does not certify concentration accuracy.

Caveat baked into the numbers: the largest token accounts include the LP pool, burn address, and CEX
wallets — none of which are insider dumpers — so raw top-N concentration OVER-states rug risk. It is a
first-pass signal to be calibrated, not a verdict.
"""
from __future__ import annotations

import json as _json


def _rpc(method, params, rpc_url, http):
    """Return the JSON-RPC `result`, or None on an error field. Retries ONCE on the transient
    'account index service overloaded' (-32603) that mega-token account queries hit."""
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    for _ in range(2):
        _s, j, _d = http("POST", rpc_url, body=body)
        if not isinstance(j, dict):
            return None
        err = j.get("error")
        if err:
            if err.get("code") == -32603:   # index overloaded -> transient, retry once
                continue
            return None
        return j.get("result")
    return None


def holder_concentration(mint, rpc_url, *, http=None):
    """Return a rug-risk snapshot for `mint`, or None if anything is unreadable (NEVER raises).

    {top1_pct, top5_pct, top10_pct, largest_n, ui_supply}: the share of circulating supply held by
    the single largest / top-5 / top-10 token accounts. Percentages are 0-100. `largest_n` is how
    many largest accounts the RPC returned (≤20). Callers use this as an ADVISORY flag; None means
    'could not assess', which must NOT be treated as unsafe (see module docstring)."""
    try:
        from sniper.execution.solana_wallet import _http
        http = http or _http
        supply = _rpc("getTokenSupply", [mint], rpc_url, http)
        # getTokenSupply nests the amount under result.value (not result); reading result.uiAmount
        # directly always yielded 0 -> None -> every scan 'unknown' (caught by the BONK canary).
        ui_supply = float(((supply or {}).get("value") or {}).get("uiAmount") or 0.0)
        if ui_supply <= 0:
            return None
        largest = _rpc("getTokenLargestAccounts", [mint], rpc_url, http)
        accounts = (largest or {}).get("value") or []
        amounts = []
        for a in accounts:
            try:
                ui = (a or {}).get("uiAmount")
                if ui is None:
                    ui = float((a or {}).get("amount") or 0) / (10 ** int((a or {}).get("decimals") or 0))
                amounts.append(float(ui))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if not amounts:
            return None
        amounts.sort(reverse=True)

        def pct(n):
            return round(sum(amounts[:n]) / ui_supply * 100.0, 2)

        return {"top1_pct": pct(1), "top5_pct": pct(5), "top10_pct": pct(10),
                "largest_n": len(amounts), "ui_supply": ui_supply}
    except Exception:
        return None   # advisory: unknown, never a fabricated "unsafe"


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def mechanism_ok(rpc_url, *, http=None):
    """Canary: is the scanner's RPC + parse path healthy RIGHT NOW? Probes getTokenSupply on USDC —
    a stable mint whose supply query never hits the account-index overload that mega-token
    getTokenLargestAccounts calls do. True iff it returns a positive supply. A False here means the
    SCANNER itself is unreliable (RPC down, response shape changed) — so a report can say so instead
    of pretending every token is fine (the honeypot 'gate that always fails' lesson). NEVER raises."""
    try:
        from sniper.execution.solana_wallet import _http
        http = http or _http
        supply = _rpc("getTokenSupply", [USDC_MINT], rpc_url, http)
        return float(((supply or {}).get("value") or {}).get("uiAmount") or 0.0) > 0
    except Exception:
        return False


# Concentration bands for the advisory flag — calibration placeholders, NOT tuned thresholds.
# top10 over HIGH is the "few wallets own most of it" shape; these will be re-set from logged data.
CONCENTRATION_WARN_TOP10_PCT = 80.0
CONCENTRATION_HIGH_TOP10_PCT = 90.0


def concentration_flag(snapshot):
    """Map a holder_concentration() snapshot to an advisory label: 'ok' | 'warn' | 'high' | 'unknown'
    (None snapshot -> 'unknown'). Advisory only — never returns a boolean 'block'."""
    if not snapshot:
        return "unknown"
    t10 = snapshot.get("top10_pct")
    if t10 is None:
        return "unknown"
    if t10 >= CONCENTRATION_HIGH_TOP10_PCT:
        return "high"
    if t10 >= CONCENTRATION_WARN_TOP10_PCT:
        return "warn"
    return "ok"
