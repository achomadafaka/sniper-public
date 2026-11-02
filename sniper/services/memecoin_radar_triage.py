"""Coin Radar Stage 3: LLM triage.

Over the top-N radar rows, build a compact fact sheet and ask an LLM for a promising / watch / skip
verdict per coin. NEVER auto-trades — verdicts are drafts for the operator to confirm. The LLM call is
injectable (unit-tested without a network); the default uses an OpenAI-compatible endpoint IF a key is
configured (XAI_API_KEY -> Grok, else OPENAI_API_KEY), otherwise it no-ops and verdicts stay empty until
a key is set. There is no hardcoded "promising" formula: this is a measured, logged draft signal, to be
compared against outcomes before it is trusted.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

VERDICTS = {"promising", "watch", "skip"}

SYSTEM = (
    "You are a memecoin scout. For each coin you are given market stats and how many wallets we "
    "INDEPENDENTLY rate as sharp bought it ('armed_buyers', with some handles). Output a terse verdict "
    "per coin: 'promising', 'watch', or 'skip', plus a reason of <=12 words. Favor coins with real "
    "liquidity AND volume AND multiple sharp buyers; be skeptical of thin liquidity, tiny mcap with no "
    "sharp buyers, or zero armed_buyers. This is a research draft, not a trade order. Respond with "
    'STRICT JSON only: {"verdicts":[{"network_id":"<network_id>","mint":"<mint>",'
    '"verdict":"<v>","reason":"<r>"}]}.'
)


def build_fact_sheet(conn, row):
    """Compact, LLM-ready facts for one coin, including the sharp (armed) buyer handles."""
    from sniper.services.memecoin_universe import armed_buyers_for
    handles = armed_buyers_for(conn, row.get("network_id"), row.get("mint"))
    age_s = row.get("age_s")
    return {
        "mint": row.get("mint"), "network_id": row.get("network_id"), "ticker": row.get("ticker"),
        "market_cap": row.get("market_cap"), "liquidity": row.get("liquidity"),
        "volume_24h": row.get("volume_24h"), "buyers": row.get("buyers"),
        "armed_buyers": row.get("armed_buyers"), "armed_handles": handles[:12],
        "age_hours": round(age_s / 3600.0, 1) if age_s is not None else None,
        "source": row.get("source"),
    }


class TriageUnconfigured(Exception):
    """No LLM key is set. Distinct from a call that was made and failed."""


class TriageProviderError(Exception):
    """The provider was called and did not return a usable answer."""


def _openai_compatible_llm(sheets):
    """Call an OpenAI-compatible chat endpoint when configured; return [{mint, verdict, reason}].

    Raises `TriageUnconfigured` when no key is set and `TriageProviderError` when a call
    was actually attempted and failed. Collapsing both into `[]` made a 401 or a malformed
    response indistinguishable from "nobody configured this", so a broken provider reported
    itself as merely unconfigured and nobody was paged.
    """
    import urllib.request
    xai, openai = os.getenv("XAI_API_KEY"), os.getenv("OPENAI_API_KEY")
    if xai:
        base, key = "https://api.x.ai/v1/chat/completions", xai
        model = os.getenv("MEMECOIN_TRIAGE_MODEL", "grok-2-latest")
    elif openai:
        base, key = "https://api.openai.com/v1/chat/completions", openai
        model = os.getenv("MEMECOIN_TRIAGE_MODEL", "gpt-4o-mini")
    else:
        raise TriageUnconfigured("no XAI_API_KEY or OPENAI_API_KEY configured")
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps({"coins": sheets})}],
    }).encode()
    try:
        req = urllib.request.Request(
            base, data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.load(r)
        content = data["choices"][0]["message"]["content"]
        return (json.loads(content).get("verdicts")) or []
    except Exception as exc:
        raise TriageProviderError(f"{model}: {type(exc).__name__}: {exc}") from exc


def triage(conn, rows, *, llm_fn=None, top_n=15, now_iso=None):
    """Triage the top-N rows: build fact sheets -> llm_fn -> validated verdicts -> persist. Returns
    {verdicts, n, llm}. Only known mints and valid verdicts are kept; nothing auto-trades."""
    from sniper.persistence import db_memecoin as m
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    llm_fn = llm_fn or _openai_compatible_llm
    sheets = [build_fact_sheet(conn, r) for r in (rows or [])[:int(max(1, top_n))]]
    if not sheets:
        return {"verdicts": [], "n": 0, "llm": "no-rows"}
    failure = None
    try:
        raw = llm_fn(sheets) or []
    except TriageUnconfigured as exc:
        return {"verdicts": [], "n": 0, "llm": "unconfigured", "error": str(exc)}
    except Exception as exc:
        # A provider that was called and failed is a fault, not an absent feature.
        raw, failure = [], f"{type(exc).__name__}: {exc}"
    by_key = {(str(s["network_id"]), s["mint"]): s for s in sheets}
    keys_by_mint = {}
    for key in by_key:
        keys_by_mint.setdefault(key[1], []).append(key)
    validated = {}
    for v in raw:
        if not isinstance(v, dict):
            continue
        mint = v.get("mint")
        verdict_raw = v.get("verdict")
        if not isinstance(mint, str) or not isinstance(verdict_raw, str):
            continue
        verdict = verdict_raw.lower().strip()
        network_id = v.get("network_id")
        if network_id not in (None, ""):
            key = (str(network_id), mint)
        else:
            matches = keys_by_mint.get(mint, [])
            key = matches[0] if len(matches) == 1 else None
        if key not in by_key or verdict not in VERDICTS:
            continue
        reason = v.get("reason")
        if not isinstance(reason, str):
            try:
                reason = json.dumps(reason, ensure_ascii=True, sort_keys=True)
            except (TypeError, ValueError):
                reason = str(reason)
        validated[key] = {"network_id": key[0], "mint": mint, "verdict": verdict,
                          "reason": reason[:200], "model": "llm-triage-v1"}
    verdicts = list(validated.values())
    m.upsert_radar_triage(conn, verdicts, now_iso)
    if failure is not None:
        return {"verdicts": [], "n": 0, "llm": "provider_error", "error": failure}
    llm_state = "ok" if verdicts else ("empty" if raw else "no_verdicts")
    return {"verdicts": verdicts, "n": len(verdicts), "llm": llm_state}
