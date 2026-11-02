"""Desk cohort U runners ranker (pre-registration docs/audits/2026-10-21-desk-u-ranker-preregistration.md
§2-§3, amendments 1-2). Pure code: features, score, rank, SR draws. The worker
(scripts/memecoin_desk_ranker_worker.py) owns the clock, the tape reads and the files.

Every 60 s cycle ranks the in-band candidates on tape-only inputs known at the cycle start
(`rank_at`): a swap counts only when its host `received_at` <= rank_at. The top 30 are the shortlist;
a tie at the cut breaks on opportunity_id byte order. Every in-band candidate gets a row each cycle,
kept or cut; an unscorable one gets rank NULL and counts as cut.

RANKER_VERSION names the score, the band rule and every constant below. A change to any of them is
a new version and a new cohort (pre-registration, status line); it is never a knob.
"""
from __future__ import annotations

import hashlib
import json
import math

RANKER_VERSION = "u-runners-v1"
RUN_LIFT_ROWS = 300      # u-inline-v1 constant (amendment 13): the agent_run_lift looks back at most this many rows for the last regular row
INLINE_RANKER_VERSION = "u-inline-v1"   # the u-runners-v1 features and score, computed per ticket at its JUDGE claim from the as-of snapshot (no cycle, no top-N)
CYCLE_S = 60.0
TOP_N = 30
LOOKBACK_S = 60.0          # the flow window behind rank_at
HEAD_MAX_LAG_S = 10.0      # Pump trades every second; a tape head older than this is a dead tape
MAX_LIFE_S = 1800.0        # a candidate stops being ranked 30 min after its emission
BAND_USD = (30_000.0, 100_000.0)   # producer U band (MEMECOIN_DESK_UNIVERSE_BAND_{LO,HI}_USD defaults; .env unset), frozen for v1

SR_ALL_DOMAIN, SR_ALL_P = "desk-sr-u-all-v1", 0.08   # amendment 3 (PR #524): was 0.01, too few draws
SR_POST_DOMAIN, SR_POST_P = "desk-sr-u-post-v1", 0.25

# Per-candidate row state. "complete" is the only scored state; every other one is rank NULL.
COMPLETENESS = ("complete", "tape_gap", "tape_stale", "no_price", "band_unknown", "left_band")


def draw(domain: str, opportunity_id: str) -> float:
    """u = sha256(domain | opportunity_id) / 2^256, the opportunity map §13.3 separator."""
    digest = hashlib.sha256((domain + "|" + opportunity_id).encode()).digest()
    return int.from_bytes(digest, "big") / 2 ** 256


def sr_draws(opportunity_id: str) -> dict:
    """The two fixed-p selection draws. Arm membership also needs the hard filter (SR-all) and the
    shortlist (SR-post); those join at read time, the draw itself never moves."""
    u_all, u_post = draw(SR_ALL_DOMAIN, opportunity_id), draw(SR_POST_DOMAIN, opportunity_id)
    return {"u_sr_all": u_all, "u_sr_post": u_post, "sr_all_draw": u_all < SR_ALL_P,
            "sr_post_draw": u_post < SR_POST_P}


SR_RAIL_DOMAINS = {"tp_pct": "desk-sr-u-tp-v1", "sl_pct": "desk-sr-u-sl-v1", "trail_pct": "desk-sr-u-trail-v1"}


def sr_rails(opportunity_id: str, rail_bounds: dict) -> dict:
    """The SR arm's rails (pre-reg §3, amendment 1: SR has no timing draw): each rail its own
    domain-separated u, mapped uniformly onto the cohort's frozen integer menu [lo, hi], the same
    menu the JUDGE picks from. Never reuses the selection draw (that would truncate it to u < p)."""
    out = {}
    for rail, domain in SR_RAIL_DOMAINS.items():
        lo, hi = (int(v) for v in rail_bounds[rail])
        if hi < lo:
            raise ValueError("rail_bounds %s" % rail)
        out[rail] = min(hi, lo + int(draw(domain, opportunity_id) * (hi - lo + 1)))
    return out


def price(virtual_quote, virtual_token):
    """SOL per token from one trade's virtual reserves (lamports over 6-decimal atoms). None if unknown."""
    if not virtual_quote or not virtual_token:
        return None
    return (virtual_quote / 1e9) / (virtual_token / 1e6)


def band_state(*, emission_mcap_usd, emission_price, last_price, last_real_token, band=BAND_USD):
    """'inside' | 'left_band' | 'band_unknown'. The current mcap is the emission's mcap moved by the
    price ratio (SOL/USD drift inside the 30 min life is ignored). real_token 0 = the curve is done."""
    if last_real_token == 0:
        return "left_band"
    if not emission_mcap_usd or not emission_price or not last_price:
        return "band_unknown"
    mcap = emission_mcap_usd * last_price / emission_price
    return "inside" if band[0] <= mcap <= band[1] else "left_band"


def features(swaps, *, rank_at, base_price):
    """v1 inputs from the mint's swaps in (rank_at - LOOKBACK_S, rank_at], each
    (received_at, is_buy, quote_atomic, actor, price). `base_price` is the last price known at the
    window start (or the emission's, for a candidate born inside the window). A healthy tape with no
    swap in the window is a known zero, never missing."""
    lo = rank_at - LOOKBACK_S
    window = sorted((s for s in swaps if lo < s[0] <= rank_at), key=lambda s: s[0])
    buy = sum(s[2] for s in window if s[1])
    sell = sum(s[2] for s in window if not s[1])
    prices = [s[4] for s in window if s[4]]
    last = prices[-1] if prices else base_price
    return {"net_sol": round((buy - sell) / 1e9, 9),
            "buyers": len({s[3] for s in window if s[1]}),
            "n_swaps": len(window),
            "ret_ln": round(math.log(last / base_price), 9) if last and base_price else 0.0}


def score(f: dict) -> float:
    """u-runners-v1: net buying, breadth of buyers and the window's price move, each bounded so no
    single term runs away: asinh(net SOL) + 0.5 ln(1 + buyers) + ln(p_now / p_window_start)."""
    return round(math.asinh(f["net_sol"]) + 0.5 * math.log1p(f["buyers"]) + f["ret_ln"], 9)


def inputs_hash(f) -> str | None:
    if f is None:
        return None
    body = json.dumps({"ranker_version": RANKER_VERSION, "features": f}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def rank_cycle(cands):
    """cands: [{opportunity_id, completeness, features (None unless complete)}]. Returns one row per
    candidate: scored ones ranked 1..N by (-score, opportunity_id bytes), the rest rank NULL; the
    shortlist is rank <= TOP_N."""
    scored, rows = [], []
    for c in cands:
        if c["completeness"] == "complete":
            scored.append((-score(c["features"]), c["opportunity_id"].encode(), c))
        else:
            rows.append({**_row(c, None, None), "shortlisted": False})
    for i, (neg, _key, c) in enumerate(sorted(scored, key=lambda t: (t[0], t[1])), start=1):
        rows.append({**_row(c, i, -neg), "shortlisted": i <= TOP_N})
    return rows


def _row(c, rank, value):
    if c["completeness"] not in COMPLETENESS:
        raise ValueError("bad completeness %r" % c["completeness"])
    f = c.get("features") if rank is not None else None
    return {"opportunity_id": c["opportunity_id"], "mint": c.get("mint"), "rank": rank, "score": value,
            "inputs_hash": inputs_hash(f), "features": f, "completeness": c["completeness"]}


# a cap above the band on a live curve: real in Mayhem mode (the agent lifts virtual SOL ~505x its reported quote) or unexplained;
# never called a decode anomaly (pre-registration amendment 12)
ABOVE_LIVE_CURVE = "band_above_live_curve"
ABOVE_LIVE_CURVE_TEXT = "agent_inflated_or_unexplained"


def inline_view(candidate, rank, *, band=BAND_USD):
    """The claim-time view of ONE ticket (pure; pre-registration amendment 11 items 3-4). `candidate` is the as-of
    envelope, `rank` the as-of builder's `meta["rank"]` ({"features", "price_now", "real_token"}).
    band: 'inside' | 'below' | 'curve_complete' | 'unknown'. real_token 0 is the completed curve, whatever the cap reads (an
    off-curve coin is never chased). A live-curve cap above the band is an
    agent-lifted or unexplained state: 'unknown' with anomaly band_above_live_curve, never a side. An absent cap or unknown real_token is 'unknown'.
    score: the u-runners-v1 score of the features, or None when absent (unknown, never 0). completeness: the amendment-3 enum."""
    row = candidate["snapshot"]["fields"].get("mcap_usd") or {}
    mcap = row.get("value") if row.get("completeness") == "complete" else None
    known = isinstance(mcap, (int, float)) and not isinstance(mcap, bool) and mcap > 0
    real = (rank or {}).get("real_token")
    anomaly = None                       # reason a live-curve cap above the band is band_unknown (amendment 12)
    if isinstance(real, int) and not isinstance(real, bool) and real == 0:
        state = "curve_complete"
    elif not known or not isinstance(real, int) or isinstance(real, bool) or real < 0:
        state = "unknown"
    elif mcap < band[0]:
        state = "below"
    elif mcap > band[1]:
        state, anomaly = "unknown", ABOVE_LIVE_CURVE
    else:
        state = "inside"
    feats = (rank or {}).get("features")
    value = score(feats) if feats else None
    return {"ranker_version": INLINE_RANKER_VERSION, "score_version": RANKER_VERSION, "band": state, "anomaly": anomaly,
            "band_usd": list(band), "mcap_usd": mcap if known else None, "real_token": real,
            "trigger_actor": (rank or {}).get("trigger_actor"), "trigger_is_agent": (rank or {}).get("trigger_is_agent"),
            "agent_run_lift": (rank or {}).get("agent_run_lift"), "agent_rows_60s": (rank or {}).get("agent_rows_60s"),
            "price_sol": (rank or {}).get("price_now"), "features": feats, "score": value, "inputs_hash": inputs_hash(feats),
            "completeness": "band_unknown" if state == "unknown" else ("complete" if value is not None else "no_price")}


def cycle_start(now: float) -> float:
    return math.floor(now / CYCLE_S) * CYCLE_S


def demo():
    a = {"net_sol": 1.0, "buyers": 3, "n_swaps": 5, "ret_ln": 0.1}
    rows = rank_cycle([{"opportunity_id": "desk:U:b", "completeness": "complete", "features": a},
                       {"opportunity_id": "desk:U:a", "completeness": "complete", "features": a},
                       {"opportunity_id": "desk:U:c", "completeness": "tape_gap", "features": None}])
    by = {r["opportunity_id"]: r for r in rows}
    assert (by["desk:U:a"]["rank"], by["desk:U:b"]["rank"], by["desk:U:c"]["rank"]) == (1, 2, None)
    assert 0 <= draw(SR_ALL_DOMAIN, "x") < 1 and draw(SR_ALL_DOMAIN, "x") != draw(SR_POST_DOMAIN, "x")


if __name__ == "__main__":
    demo()
