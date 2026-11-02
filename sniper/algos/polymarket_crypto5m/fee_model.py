"""Per-market taker fee resolver — empirically validated, fail-closed.

EMPIRICAL FINDING (2026-06-20): the market config (CLOB ``/markets/<condition_id>`` and Gamma) says
``feesEnabled=true, taker_base_fee=1000``, but realized ``last_trade_price.fee_rate_bps`` is ``0`` on
live trades. The sources CONFLICT — so ``taker_base_fee=1000`` must NOT be assumed to be a rate. We
capture both and decode only when they agree; otherwise fail closed. Documented taker formula:
``fee = shares · fee_rate · price · (1-price)``.

``fee_status``: ``known`` (usable) | ``fee_conflict`` | ``fee_unknown`` | ``fee_stale``. Only ``known``
permits a (future) paper entry.
"""
from __future__ import annotations

import math
from typing import Optional

DECODER_VERSION = "v0-bps"
CLOB_MARKET_URL = "https://clob.polymarket.com/markets"
DEFAULT_MAX_AGE_S = 3600
_BPS = 10000.0
MAX_SANE_FEE_RATE = 1.0   # a per-share fee rate above 100% is not a real config


def _sane_rate(r) -> bool:
    try:
        return math.isfinite(r) and 0.0 <= r <= MAX_SANE_FEE_RATE
    except TypeError:
        return False


def decode_fee(*, condition_id: str, fees_enabled, taker_base_fee, observed_fee_rate_bps=None,
               fetched_ts: Optional[int] = None, now_ts: Optional[int] = None,
               max_age_s: int = DEFAULT_MAX_AGE_S, source: str = "clob") -> dict:
    """Decode a per-market fee config. ``taker_base_fee`` / ``observed_fee_rate_bps`` are in bps.

    Decode only when the config and a realized observation AGREE (or fees are disabled, or only one
    authoritative source exists). Config-says-fee + observed-zero (the finding) -> ``fee_conflict``.
    Config-only with no realized observation to validate -> ``fee_unknown`` (cannot confirm the decode).
    """
    out = {"condition_id": condition_id, "decoder_version": DECODER_VERSION, "source": source,
           "fees_enabled": bool(fees_enabled), "taker_base_fee": taker_base_fee,
           "observed_fee_rate_bps": observed_fee_rate_bps, "fetched_ts": fetched_ts}

    if fetched_ts is not None and now_ts is not None and now_ts - fetched_ts > max_age_s:
        return {**out, "fee_rate": None, "fee_status": "fee_stale"}

    if not fees_enabled:
        return {**out, "fee_rate": 0.0, "fee_status": "known"}

    cfg_bps = float(taker_base_fee) if taker_base_fee is not None else None
    if observed_fee_rate_bps is None:
        # only the config exists; its interpretation is UNVALIDATED -> never assume -> fail closed
        return {**out, "fee_rate": None, "fee_status": "fee_unknown"}
    try:
        obs_bps = float(observed_fee_rate_bps)
    except (TypeError, ValueError):
        return {**out, "fee_rate": None, "fee_status": "fee_unknown"}
    obs_rate = obs_bps / _BPS
    if not _sane_rate(obs_rate):                                            # NaN / negative / absurd
        return {**out, "fee_rate": None, "fee_status": "fee_unknown"}

    if cfg_bps is None:
        return {**out, "fee_rate": obs_rate, "fee_status": "known"}         # only a realized observation
    if abs(obs_bps - cfg_bps) <= 1e-9:
        return {**out, "fee_rate": obs_rate, "fee_status": "known"}         # config & observed agree
    return {**out, "fee_rate": None, "fee_status": "fee_conflict"}          # disagree (e.g. 1000 vs 0)


def is_known(fee: dict) -> bool:
    return fee.get("fee_status") == "known"


def cost_per_share(price: float, fee_rate: float) -> float:
    """Fee-inclusive cost of one share at ``price`` under ``fee = fee_rate · price · (1-price)``."""
    p = float(price)
    return p + float(fee_rate) * p * (1.0 - p)


# --------------------------------------------------------------------------- #
# Phase 1B Deliverable #2 — fee-source reconciliation.
#
# Precedence (per the documented Polymarket fee model): the per-market fee descriptor is canonical —
# CLOB ``getClobMarketInfo(conditionID).fd`` first, the market-channel ``new_market.fee_schedule`` as
# secondary confirmation. Gamma ``feesEnabled/feeType/takerBaseFee`` and ``last_trade_price.fee_rate_bps``
# are DIAGNOSTIC ONLY — they confirm or conflict, they never ORIGINATE a rate. A usable ``fee_rate`` comes
# only from a descriptor whose ``rate`` is a sane fraction in [0,1]; a bps-shaped or raw-``1000`` value is
# therefore auto-rejected (``_sane_rate``), which makes the "never interpret 1000 as a rate" rule
# structural rather than a special case.
#
# Vocabulary is the directive's: ``fee_status`` ∈ {known, conflict, missing, unsupported};
# ``fee_source`` ∈ {clob_market_info, websocket_fee_schedule, reconciled, unavailable}. ``known`` keeps the
# same string the legacy ``decode_fee``/``is_known`` gate uses, so ``is_known()`` and ``decision.py`` Gate 4
# work unchanged on a reconciled result.
# --------------------------------------------------------------------------- #

ROUNDING_PRECISION = 5
SUPPORTED_FEE_TYPES = {"", "taker", "flat", "taker_base", "none", "crypto_fees_v2"}
_RATE_TOL = 1e-9


def parse_ws_fee_schedule(msg) -> Optional[dict]:
    """Extract the fee descriptor from a market-channel ``new_market`` WS event. Returns ``None`` for a
    non-dict. The ``fee_schedule`` sub-object carries ``rate``/``exponent``/``taker_only``/``rebate_rate``;
    ``fees_enabled``/``taker_base_fee``/``fee_type`` may sit at the top level."""
    if not isinstance(msg, dict):
        return None
    fs = msg.get("fee_schedule") if isinstance(msg.get("fee_schedule"), dict) else {}
    return {
        "rate": fs.get("rate"),
        "exponent": fs.get("exponent"),
        "taker_only": fs.get("taker_only", fs.get("takerOnly")),
        "rebate_rate": fs.get("rebate_rate", fs.get("rebateRate")),
        "fees_enabled": msg.get("fees_enabled", msg.get("feesEnabled")),
        "taker_base_fee": msg.get("taker_base_fee", msg.get("takerBaseFee")),
        "fee_type": msg.get("fee_type", msg.get("feeType")),
    }


def normalize_bool(v):
    """Coerce a JSON bool/int/string to a real bool (so the string "false" is not truthy). Unknown -> None."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "t"):
        return True
    if s in ("false", "0", "no", "f", ""):
        return False
    return None


def _descriptor(payload) -> dict:
    """Normalize a fee payload (CLOB ``/markets`` json with a nested ``fd``/``fee_schedule``, a flat config,
    or an already-parsed WS descriptor) to a common field set. CLOB docs use the short ``fd`` form
    ``{r, e, to}`` — those short keys are read ONLY from the nested descriptor (never the top-level payload,
    to avoid collisions). Missing -> ``None``; booleans normalized."""
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("fd") or payload.get("fee_schedule") or payload.get("feeSchedule") or {}
    if not isinstance(nested, dict):
        nested = {}

    def pick(*keys, nested_only=()):
        for src in (payload, nested):
            for k in keys:
                v = src.get(k)
                if v is not None:
                    return v
        for k in nested_only:
            v = nested.get(k)
            if v is not None:
                return v
        return None

    return {
        "rate": pick("rate", nested_only=("r",)),
        "exponent": pick("exponent", nested_only=("e",)),
        "taker_only": normalize_bool(pick("taker_only", "takerOnly", nested_only=("to",))),
        "rebate_rate": pick("rebate_rate", "rebateRate"),
        "fees_enabled": normalize_bool(pick("fees_enabled", "feesEnabled")),
        "taker_base_fee": pick("taker_base_fee", "takerBaseFee"),
        "fee_type": pick("fee_type", "feeType"),
    }


def _sane_or_none(r) -> Optional[float]:
    try:
        rr = float(r)
    except (TypeError, ValueError):
        return None
    return rr if _sane_rate(rr) else None


def _to_rate(bps) -> Optional[float]:
    try:
        r = float(bps) / _BPS
    except (TypeError, ValueError):
        return None
    return r if math.isfinite(r) else None       # NaN/Inf observation can't confirm anything


def _exponent_ok(descriptor) -> bool:
    """We model only ``fee = rate·p·(1-p)`` (exponent 1). A descriptor with a different exponent is an
    unmodeled fee scheme -> must not be decoded as 'known'."""
    exp = descriptor.get("exponent")
    if exp is None:
        return True
    try:
        return abs(float(exp) - 1.0) <= 1e-9
    except (TypeError, ValueError):
        return False


def reconcile_fee_sources(*, condition_id: str, clob=None, ws_fee_schedule=None, gamma=None,
                          observed_fee_rate_bps=None, fetched_ts: Optional[int] = None,
                          now_ts: Optional[int] = None, max_age_s: int = DEFAULT_MAX_AGE_S) -> dict:
    """Reconcile every fee source into one fail-closed result. See module note for precedence."""
    raw = {"clob": clob, "ws": ws_fee_schedule, "gamma": gamma,
           "last_trade_fee_rate_bps": observed_fee_rate_bps}
    base = {"condition_id": condition_id, "decoder_version": DECODER_VERSION,
            "rounding_precision": ROUNDING_PRECISION, "raw_source_payloads": raw,
            "fetched_ts": fetched_ts, "fee_exponent": None, "taker_only": None}

    def result(status, source, rate, **extra):
        return {**base, "fee_status": status, "fee_source": source, "fee_rate": rate, **extra}

    # 0. staleness — a too-old config can't be trusted.
    if fetched_ts is not None and now_ts is not None and now_ts - fetched_ts > max_age_s:
        return result("missing", "unavailable", None, caveat="stale")

    clob_d = _descriptor(clob)
    ws_d = _descriptor(ws_fee_schedule)

    # 1. explicit fees-disabled from a primary source -> genuine no-fee (no fee model to be unsupported).
    for d, src in ((clob_d, "clob_market_info"), (ws_d, "websocket_fee_schedule")):
        if d.get("fees_enabled") is False:
            return result("known", src, 0.0)

    # 2. unsupported fee model from a primary source (we only model flat/taker fees, exponent 1).
    for d, src in ((clob_d, "clob_market_info"), (ws_d, "websocket_fee_schedule")):
        ft = d.get("fee_type")
        if ft is not None and str(ft).lower() not in SUPPORTED_FEE_TYPES:
            return result("unsupported", src, None)
        if not _exponent_ok(d):
            return result("unsupported", src, None)

    # 3. descriptor rate — canonical CLOB, then WS.
    clob_rate = _sane_or_none(clob_d.get("rate"))
    ws_rate = _sane_or_none(ws_d.get("rate"))
    if clob_rate is not None:
        src = "clob_market_info"
        if ws_rate is not None:
            if abs(clob_rate - ws_rate) > _RATE_TOL:
                return result("conflict", "reconciled", None)
            src = "reconciled"
        if observed_fee_rate_bps is not None:
            obs_rate = _to_rate(observed_fee_rate_bps)
            if obs_rate is None or abs(obs_rate - clob_rate) > _RATE_TOL:
                return result("conflict", src, None)
        return result("known", src, clob_rate,
                      fee_exponent=clob_d.get("exponent"), taker_only=clob_d.get("taker_only"))
    if ws_rate is not None:
        if observed_fee_rate_bps is not None:
            obs_rate = _to_rate(observed_fee_rate_bps)
            if obs_rate is None or abs(obs_rate - ws_rate) > _RATE_TOL:
                return result("conflict", "websocket_fee_schedule", None)
        return result("known", "websocket_fee_schedule", ws_rate,
                      fee_exponent=ws_d.get("exponent"), taker_only=ws_d.get("taker_only"))

    # 3. no descriptor rate -> flat-config + realized observation (fail-closed).
    if clob is None and ws_fee_schedule is None:
        return result("missing", "unavailable", None)
    primary_d = clob_d if clob is not None else ws_d      # read flat fields from whichever source exists
    src = "clob_market_info" if clob is not None else "websocket_fee_schedule"
    try:
        flat_val = (float(primary_d.get("taker_base_fee"))
                    if primary_d.get("taker_base_fee") is not None else None)
    except (TypeError, ValueError):
        flat_val = None
    if flat_val == 0.0 or primary_d.get("fees_enabled") is False:
        return result("known", src, 0.0)                      # genuine no-fee
    if observed_fee_rate_bps is None:
        return result("missing", src, None)                   # fee-enabled but params unavailable (1000, no trades)
    obs_rate = _to_rate(observed_fee_rate_bps)
    if obs_rate == 0.0 and (flat_val is None or flat_val > 0):
        return result("conflict", src, None)                  # the documented 1000-vs-realized-0 finding
    return result("missing", src, None)                       # realized>0 but no descriptor -> never originate a rate


def taker_fee(*, shares: float, fee_rate: float, price: float) -> float:
    """``fee = shares · fee_rate · price · (1-price)``, rounded to 5 decimal places (documented)."""
    p = float(price)
    return round(float(shares) * float(fee_rate) * p * (1.0 - p), ROUNDING_PRECISION)


def parse_clob_market_fee(raw: dict, *, condition_id: str, observed_fee_rate_bps=None,
                          fetched_ts=None, now_ts=None, fees_enabled=None) -> dict:
    """Decode from a CLOB ``/markets/<condition_id>`` payload. ``taker_base_fee`` is the fee signal —
    NOT ``enable_order_book`` (a distinct field). A missing ``taker_base_fee`` means we can't decode ->
    fail closed (``fee_unknown``); ``0`` is a genuine no-fee (``known 0``); ``>0`` needs a realized
    observation to validate. Pass an explicit ``fees_enabled`` (e.g. Gamma ``feesEnabled``) to override.
    """
    tbf = raw.get("taker_base_fee")
    if fees_enabled is None:
        # unknown config (tbf None) is treated as enabled-but-unvalidated -> fee_unknown downstream
        fees_enabled = (tbf is None) or (float(tbf) > 0)
    return decode_fee(condition_id=condition_id, fees_enabled=fees_enabled, taker_base_fee=tbf,
                      observed_fee_rate_bps=observed_fee_rate_bps, fetched_ts=fetched_ts,
                      now_ts=now_ts, source="clob")


def fetch_market_fee(fetch, condition_id: str, *, observed_fee_rate_bps=None, fetched_ts=None,
                     now_ts=None) -> dict:
    """Thin network resolve: ``fetch(url)->json`` injected. Captures raw + decodes (fail-closed)."""
    raw = fetch(f"{CLOB_MARKET_URL}/{condition_id}")
    fee = parse_clob_market_fee(raw or {}, condition_id=condition_id,
                                observed_fee_rate_bps=observed_fee_rate_bps, fetched_ts=fetched_ts,
                                now_ts=now_ts)
    fee["raw_payload"] = raw
    return fee


# --------------------------------------------------------------------------- #
# Phase 1C — canonical fee synthesis (GPT-Pro statuses) + published-curve reconciliation.
#
# canonical_rate / ws_rate are DECODED fractional rates from a descriptor with DOCUMENTED semantics
# (None if absent). A raw base_fee/taker_base_fee=1000 is NOT a decoded rate -> canonical None. The
# realized last_trade fee_rate_bps stays diagnostic (a CONFLICT CHECK), never authoritative. Live finding
# 2026-06-20: the descriptor exists in GAMMA feeSchedule (rate=0.07, exponent=1), not CLOB `.fd`; with the
# capture-lane realized fee_rate_bps=0 that synthesizes to `conflict` (blocked) — a present-but-invalid
# realized value (NaN / unparseable) also fails closed to conflict. See finding-polymarket-crypto5m-fee-conflict.
# --------------------------------------------------------------------------- #

SYNTHESIS_PRICES = (0.10, 0.25, 0.50, 0.75, 0.90)


_REALIZED_TOL = 1e-4   # descriptor rate vs realized rate (bps/1e4)


def synthesize_fee_status(*, fees_enabled=None, canonical_rate=None, ws_rate=None,
                          observed_fee_rate_bps=None) -> dict:
    """GATE-COMPATIBLE fee status (``fee_status`` ∈ {known, conflict, missing} so ``is_known``/decision
    Gate 4 keep working) PLUS a ``confidence`` (reconciled / canonical_only) + caveat. canonical_rate/
    ws_rate are DECODED fractional rates from a documented descriptor (None if absent — a bare
    base_fee=1000 is NOT a decoded rate). The realized ``observed_fee_rate_bps`` is a CONFLICT CHECK, not
    authoritative: a documented 0.07 descriptor vs realized 0 -> conflict (block), never silently accepted."""
    if fees_enabled is False:
        return {"fee_status": "known", "fee_rate": 0.0, "fee_source": "fees_disabled",
                "confidence": "disabled"}
    # a realized observation that is PRESENT but unparseable / non-finite (NaN/Inf) can't confirm a
    # descriptor -> fail closed (treated as a conflicting observation below), never silently "known".
    obs_present = observed_fee_rate_bps is not None
    obs_rate = _to_rate(observed_fee_rate_bps) if obs_present else None
    if canonical_rate is not None:
        c = float(canonical_rate)
        if ws_rate is not None and abs(c - float(ws_rate)) > _RATE_TOL:
            return {"fee_status": "conflict", "fee_rate": None, "fee_source": "descriptor_vs_ws"}
        if obs_present and (obs_rate is None or abs(c - obs_rate) > _REALIZED_TOL):
            return {"fee_status": "conflict", "fee_rate": None, "fee_source": "descriptor_vs_realized",
                    "caveat": f"descriptor {c} vs realized {obs_rate}"}
        out = {"fee_status": "known", "fee_rate": c, "fee_source": "descriptor",
               "confidence": ("reconciled" if ws_rate is not None else "canonical_only")}
        if ws_rate is None:
            out["caveat"] = "ws_unavailable"
        return out
    if ws_rate is not None:
        if obs_present and (obs_rate is None or abs(float(ws_rate) - obs_rate) > _REALIZED_TOL):
            return {"fee_status": "conflict", "fee_rate": None, "fee_source": "ws_vs_realized"}
        return {"fee_status": "known", "fee_rate": float(ws_rate), "fee_source": "ws_fee_schedule",
                "confidence": "canonical_only", "caveat": "clob_descriptor_unavailable"}
    return {"fee_status": "missing", "fee_rate": None, "fee_source": "unavailable",
            "caveat": "no_decodable_canonical_descriptor"}


def fee_per_share_table(fee_rate, prices=SYNTHESIS_PRICES) -> dict:
    """Per-share taker fee at each price under ``fee = rate·p·(1-p)`` (5-dp) — to reconcile a decoded rate
    against the published fee table."""
    return {p: round(float(fee_rate) * p * (1.0 - p), ROUNDING_PRECISION) for p in prices}
