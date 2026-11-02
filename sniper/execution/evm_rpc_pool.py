"""Free multi-endpoint rotation for EVM chain RPC, so one provider's rate limit is not the book's.

The funded 4663 lane ran on ONE keyless public endpoint. Measured 2026-08-13, 208 of 210 logged
`HTTP Error 429` tracebacks came from `evm_wallet._rpc` — the chain RPC, not GeckoTerminal — and a
429 inside the V4 quote surfaced as `no_route`, i.e. an entry refused as though the coin had no
pool. Adding a second endpoint costs nothing: chainid.network lists two official endpoints for
Robinhood Chain and both answered `eth_chainId = 0x1237` with a current block.

This follows the `auto_free` doctrine already used for submission: chain-owner official public
endpoints only, identity-checked before use, health-gated, quarantined on failure. It adds no key,
no subscription and no operator endpoint.

IDENTITY IS CHECKED BEFORE AN ENDPOINT IS EVER USED FOR DATA. An RPC that answers for a different
chain would return plausible-looking wrong balances and quotes on a funded path, so an endpoint
that cannot prove its chain id is never used — it is not merely deprioritised.
"""
from __future__ import annotations

import threading
import time
import logging
import re

_log = logging.getLogger(__name__)

_QUARANTINE_S = 60.0        # CAP on a quarantine; the first blip costs far less (see penalize)
# ESCALATING QUARANTINE (2026-09-03). Observed live minutes after the self-ranking deploy: alchemy
# 24 ok / 2 fail and publicnode 34 ok / 2 fail, BOTH sitting out 60s at once, every read routed to
# the endpoint that measures 3x slower and ~8 blocks stale. A single transport blip on an
# identity-proven endpoint with a long success streak does not deserve a minute; a dead one
# reaches the cap in three tries. Consecutive failures: 5s -> 15s -> 60s; any success resets.
_QUARANTINE_STEPS_S = (5.0, 15.0, 60.0)
_PENALTY_LOG_INTERVAL_S = 60.0
_IDENTITY_TTL_S = 3600.0    # re-prove chain id at most hourly

_lock = threading.Lock()
_state = {}                 # url -> {"until": float, "chain_ok": bool|None, "checked": float, ...}

# MEASURED RANKING (2026-09-03). The configured primary used to be preferred unconditionally
# while not quarantined, so a slow primary cost the 0.4s hedge on every read and a dead one cost a
# 60s quarantine cycle each time it expired. Measured 60 samples/endpoint at 1/s the same day:
#   robinhood-mainnet.g.alchemy.com   p50 116ms  p95 160ms  head lag 0 blocks
#   robinhood-rpc.publicnode.com      p50  60ms  p95  81ms  head lag 0.5 (max 2)
#   rpc.mainnet.chain.robinhood.com   p50 191ms  p95 225ms  head lag 8 (max 14)   <- 3x slower AND stale
# Latency alone would rank the stale endpoint above a fresh one for quotes and nonces, so the score
# is latency PLUS head lag priced at _LAG_BLOCK_MS per block. Hysteresis keeps the configured
# primary unless another PROVEN endpoint is materially better, and returns to it as soon as it is
# close again, so a flapping endpoint cannot flap the choice.
_EWMA_ALPHA = 0.3
_LAG_BLOCK_MS = 100.0        # ~one block time on 4663 (measured ~10 blocks/s); staleness priced in ms
_SWITCH_RATIO = 0.6          # leave the primary only for an endpoint scoring < 60% of it
_RETURN_RATIO = 1.2          # come back once the primary scores within 120% of the best
_FAIL_PENALTY_MS = 400.0     # a recent failure counts as this much latency while it decays
_SAMPLER_PERIOD_S = 15.0



class RequestNotSent(Exception):
    """A caller's own limit refused a request BEFORE dispatch (2026-09-17).

    No bytes left this process, so the refusal is not evidence about any endpoint. Never penalize,
    record, quarantine, or mark a provider blind because of it. The multihop route search raises it
    from its request-budget transport when its deadline or request count is spent.
    """


class NoEligibleReadEndpoint(RequestNotSent):
    """This read's endpoint filter excludes every member; no request was dispatched."""


def _now():
    return time.monotonic()


def _entry(url):
    return _state.setdefault(url, {"until": 0.0, "chain_ok": None, "checked": 0.0,
                                   "ewma_ms": None, "ok": 0, "fail": 0, "fail_ewma": 0.0,
                                   "head": None, "head_at": 0.0, "lag_blocks": None,
                                   "consec_fail": 0, "last_penalty": None, "last_penalty_at": 0.0,
                                   "penalties": 0, "_last_logged": 0.0,
                                   "refused": 0, "last_refusal": None})


def record(url, latency_ms, ok):
    """Passive measurement from every real call `_rpc` makes. No extra requests."""
    with _lock:
        e = _entry(url)
        if ok:
            e["ok"] += 1
            e["consec_fail"] = 0
            e["ewma_ms"] = (float(latency_ms) if e["ewma_ms"] is None
                            else (1 - _EWMA_ALPHA) * e["ewma_ms"] + _EWMA_ALPHA * float(latency_ms))
            e["fail_ewma"] = (1 - _EWMA_ALPHA) * e["fail_ewma"]
        else:
            e["fail"] += 1
            e["fail_ewma"] = (1 - _EWMA_ALPHA) * e["fail_ewma"] + _EWMA_ALPHA


_REFUSAL_TTL_S = 3600.0      # a structural refusal is remembered this long
_REFUSALS_TO_EXCLUDE = 1     # one structural refusal of a method -> that member sits out that
                             # METHOD for _REFUSAL_TTL_S. A plan cap or an archive-token wall is
                             # deterministic, and once the pool ranks the member that DID answer
                             # ahead, the refusing one is not asked again for a while -- a
                             # two-strike rule could never trip. A request malformed on OUR side
                             # is refused by every member alike, so `capable` empties and the
                             # primary is asked: the failure stays honest, never silent.


def note_refusal(url, code="", method=""):
    """A healthy endpoint declined a request (4xx with a JSON-RPC error). Not a failure, not a
    penalty -- counted, and remembered PER METHOD so the rotation stops paying a round trip to
    learn the same permanent no.

    Measured 2026-09-03 on the exact `_pool_key` query (address + topics, 100k-block page):
    Alchemy free tier -> 400 -32600 "Under the Free tier plan ... 10 block range" at ANY width;
    publicnode -> 403 -32602 "Archive requests require a personal token"; only
    rpc.mainnet.chain.robinhood.com serves it. These endpoints are fine for eth_call, receipts,
    balances and nonces. Capability is per method, so exclusion is per method."""
    with _lock:
        e = _entry(url)
        e["refused"] += 1
        e["last_refusal"] = str(code or "?")
        if method:
            m = e.setdefault("_refused_methods", {}).setdefault(str(method), {"n": 0, "at": 0.0})
            m["n"] += 1
            m["at"] = _now()


def refuses(url, method, ttl_s=None):
    """True when `url` has refused `method` at least _REFUSALS_TO_EXCLUDE times within the TTL."""
    ttl = _REFUSAL_TTL_S if ttl_s is None else float(ttl_s)
    if ttl <= 0:
        return False
    with _lock:
        m = _entry(url).get("_refused_methods", {}).get(str(method))
        if not m:
            return False
        if _now() - m["at"] > ttl:
            return False
        return m["n"] >= _REFUSALS_TO_EXCLUDE


_FULL_RANGE_LIMIT_RE = re.compile(
    r"(?:limited to a ([\d,]+) range|range [\d,]+ exceeds limit of ([\d,]+))", re.IGNORECASE)


def is_log_range_refusal(answer):
    """A named block-range limit applies to this range, not to every log read."""
    error = answer.get("error") if isinstance(answer, dict) else None
    return isinstance(error, dict) and bool(
        _FULL_RANGE_LIMIT_RE.search(str(error.get("message") or "")))


def is_full_range_refusal(answer):
    """A structural getLogs range refusal, never a transient provider-busy error."""
    error = answer.get("error") if isinstance(answer, dict) else None
    return (isinstance(error, dict) and (
        error.get("code") in {-32600, -32602, -32614}
        or bool(_FULL_RANGE_LIMIT_RE.search(str(error.get("message") or "")))))


def record_heads(heads):
    """`heads`: {url: block_number or None}. Lag is measured against the best head in the SAME
    sample, so it is a comparison between endpoints, never against a clock."""
    best = max((h for h in heads.values() if h is not None), default=None)
    with _lock:
        for url, h in heads.items():
            e = _entry(url)
            if h is None:
                e["lag_blocks"] = None
                continue
            lag = best - int(h)
            # SMOOTHED. Heads are sampled sequentially, so a single sample carries a few blocks of
            # ordering noise; at 200ms per block one noisy sample could out-vote 1s of latency and
            # flip the choice for a cycle. First sample initialises, later ones decay in.
            prev = e["lag_blocks"]
            e["head"], e["head_at"] = int(h), _now()
            e["lag_blocks"] = (lag if prev is None
                               else (1 - _EWMA_ALPHA) * prev + _EWMA_ALPHA * lag)


def lag_ms(url):
    """Measured staleness of `url`, priced in milliseconds by the same block cost `score` uses.
    None when nothing has been sampled yet."""
    with _lock:
        lag = _entry(url)["lag_blocks"]
    return None if lag is None else float(lag) * _LAG_BLOCK_MS


def score(url):
    """Lower is better. None when nothing has been measured yet."""
    with _lock:
        e = _entry(url)
        if e["ewma_ms"] is None:
            return None
        lag = e["lag_blocks"] or 0
        return e["ewma_ms"] + lag * _LAG_BLOCK_MS + e["fail_ewma"] * _FAIL_PENALTY_MS


def snapshot(urls):
    """Heartbeat view. HOSTS ONLY -- a custody url may carry an API key."""
    out = []
    seen = {}
    with _lock:
        for url in urls:
            e = _entry(url)
            host = url.split("/")[2] if "//" in url else url
            seen[host] = seen.get(host, 0) + 1
            if seen[host] > 1:                 # two keys on one host: "host#2", never the path
                host = f"{host}#{seen[host]}"
            out.append({
                "host": host,
                "ewma_ms": None if e["ewma_ms"] is None else round(e["ewma_ms"], 1),
                "ok": e["ok"], "fail": e["fail"],
                "lag_blocks": e["lag_blocks"],
                "quarantined_s": max(0.0, round(e["until"] - _now(), 1)),
                "identity": e["chain_ok"],
                "last_penalty": e["last_penalty"],
                "last_penalty_age_s": (None if not e["last_penalty_at"]
                                       else round(_now() - e["last_penalty_at"], 1)),
                "penalties": e["penalties"],
                "refused": e["refused"], "last_refusal": e["last_refusal"],
                "refused_methods": sorted(k for k, v in e.get("_refused_methods", {}).items()
                                          if v["n"] >= _REFUSALS_TO_EXCLUDE
                                          and _now() - v["at"] <= _REFUSAL_TTL_S),
            })
    return out


def start_head_sampler(urls, head_fn, period_s=None, verify_fn=None):
    """One eth_blockNumber per endpoint per period (3 requests / 15s for a 3-endpoint pool), so lag
    and latency are known for endpoints the hot path is not currently using. `head_fn(url)` returns
    the block number or raises; its own timeout bounds the thread. Daemon; never raises out."""
    period = float(period_s or _SAMPLER_PERIOD_S)

    from concurrent.futures import ThreadPoolExecutor

    def _one(url):
        # Prove identity once (TTL-bounded) so a fast secondary can be RANKED before the primary
        # has ever failed; `choose` never uses an unproven secondary for data.
        if verify_fn is not None and identity_state(url) is None:
            try:
                mark_identity(url, bool(verify_fn(url)))
            except Exception:
                pass
        t = _now()
        try:
            h = int(head_fn(url))
            record(url, (_now() - t) * 1000.0, True)
            return url, h
        except Exception:
            record(url, (_now() - t) * 1000.0, False)
            return url, None

    def _run():
        # CONCURRENT, NOT SEQUENTIAL. This chain mines ~10 blocks/s, so sampling members one after
        # another made whichever was asked FIRST look 3-5 blocks stale by construction -- the
        # primary, every time. All heads are taken in the same instant and lag is the spread.
        ex = ThreadPoolExecutor(max_workers=max(1, len(list(urls))), thread_name_prefix="evm-head")
        while True:
            try:
                heads = dict(ex.map(_one, list(urls)))
                record_heads(heads)
            except Exception:
                pass
            time.sleep(period)

    th = threading.Thread(target=_run, name="evm-rpc-head-sampler", daemon=True)
    th.start()
    return th


def pool_for(url, cfg=None):
    """Every endpoint that may serve `url`'s chain, primary first. Just [url] when none configured."""
    from sniper.settings import memecoin as _cfg
    cfg = cfg or _cfg
    for pools in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {},
                  getattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {}) or {}):
        for _chain, urls in pools.items():
            if url in urls:
                return list(urls)
    return [url]


def chain_of(url, cfg=None):
    """The chain id whose pool contains `url`, or None. The expected identity comes from CONFIG,
    never from asking another endpoint — probing the primary to validate a secondary recurses, and
    a quarantined primary would make it unbounded."""
    from sniper.settings import memecoin as _cfg
    cfg = cfg or _cfg
    for pools in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {},
                  getattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {}) or {}):
        for chain, urls in pools.items():
            if url in urls:
                return str(chain)
    return None


def penalize(url, reason="", quarantine_s=None):
    """Sit this endpoint out on 429/5xx/transport or a provider-busy JSON-RPC body.
    Chain-level errors such as execution reverts do not quarantine the endpoint.

    THE REASON IS RECORDED AND LOGGED. Until 2026-09-03 this stored only an expiry, so a quarantined
    endpoint in the heartbeat could not be explained by anything in the log. An explicit
    `quarantine_s` (tests, callers) is honoured as given; otherwise the step escalates with
    consecutive failures and any recorded success resets it."""
    with _lock:
        e = _entry(url)
        e["consec_fail"] += 1
        step = (float(quarantine_s) if quarantine_s is not None
                else _QUARANTINE_STEPS_S[min(e["consec_fail"], len(_QUARANTINE_STEPS_S)) - 1])
        e["until"] = _now() + step
        e["last_penalty"], e["last_penalty_at"], e["penalties"] = str(reason or "?"), _now(), e["penalties"] + 1
        should_log = (_now() - e["_last_logged"]) >= _PENALTY_LOG_INTERVAL_S
        if should_log:
            e["_last_logged"] = _now()
        consec, total = e["consec_fail"], e["penalties"]
    if should_log:
        _log.warning("evm rpc pool: quarantined %s for %.0fs (%s; consecutive=%d, total=%d)",
                     url.split("/")[2] if "//" in url else url, step, reason or "?", consec, total)


def available(url):
    with _lock:
        return _entry(url)["until"] <= _now()


def mark_identity(url, ok):
    with _lock:
        e = _entry(url)
        e["chain_ok"], e["checked"] = bool(ok), _now()


def identity_state(url):
    """True proven, False disproven, None unknown/expired -> the caller must prove it."""
    with _lock:
        e = _entry(url)
        if e["chain_ok"] and (_now() - e["checked"]) > _IDENTITY_TTL_S:
            return None
        return e["chain_ok"]


def choose(url, *, verify_fn=None, candidates=None):
    """A healthy, identity-proven endpoint for `url`'s chain, or `url` unchanged.

    Ethereum preserves configured order; other chains retain measured ranking with hysteresis.
    Returning `url` when every candidate is quarantined is deliberate — the
    caller then fails on a real request rather than silently doing nothing, and its own failure
    handling (fail-closed, blind-reason reporting) still applies.
    """
    candidates = list(candidates) if candidates else pool_for(url)
    if len(candidates) == 1:
        return candidates[0]
    # RANK BY MEASUREMENT, WITH HYSTERESIS. Configured order is the tie-break and the default
    # before anything is measured; a proven, available secondary is tried before the primary only
    # when its score beats the primary's by _SWITCH_RATIO (or the primary has no score and it does).
    custody_primary = chain_of(url) == "1"
    lead = (candidates[0] if custody_primary else
            url if url in candidates else candidates[0])
    primary_score = score(lead)
    def _rank(cand):
        sc = score(cand)
        if cand == lead:
            return (0, 0.0)                # primary wins ties and unmeasured states
        if sc is None or identity_state(cand) is not True or not available(cand):
            return (2, float("inf"))       # unproven / quarantined / unmeasured: after the primary
        if custody_primary:
            return (1, sc)                 # Ethereum backups serve failures, not latency races
        if primary_score is None or sc < primary_score * _SWITCH_RATIO:
            return (-1, sc)                # materially better than the primary: ahead of it
        return (1, sc)
    candidates = sorted(candidates, key=_rank)
    for cand in candidates:
        if not available(cand):
            continue
        state = identity_state(cand)
        if state is False:
            continue                       # proven wrong chain: never usable
        if state is None:
            if verify_fn is None:
                if cand == lead:
                    return cand            # the configured primary is trusted without a probe
                continue                   # an unproven secondary is not used for data
            try:
                ok = bool(verify_fn(cand))
            except RequestNotSent:
                raise                      # never sent: not evidence about this endpoint
            except Exception:
                penalize(cand, "identity_probe_failed")
                continue
            mark_identity(cand, ok)
            if not ok:
                continue
        return cand
    return lead


def reset_for_tests():
    with _lock:
        _state.clear()
