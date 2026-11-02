"""What the coin did in the 60 seconds AFTER a funded take-profit exit, at ONE-SECOND cadence.

NOT `memecoin_post_tp_followup`, which already exists and is a DIFFERENT instrument: that one
samples four fixed horizons (60/300/900/1800s) into the funded ledger to ask whether a laddered
take-profit is worth building. This one samples every second for sixty into a research database to
ask whether a CONFIRMATION WINDOW is. Different cadence, different horizon, different question,
different store — and they must stay separately switchable, because running both is twice the
research quota for two answers only one of which anyone is waiting on.

I created this file at the other one's path first and `cat >` overwrote it, which broke the live
runner's import until it was restored. The names were close enough to collide and the instruments
are not the same; hence the longer name.

Operator hypothesis, 2026-08-29: would a TP CONFIRMATION window — on touch, ride while the price
holds at or above the target, sell when it stops — beat the instant take-profit? The deciding
number does not exist. We exit sub-second, so the funded ledger holds zero observations of the
next minute, and the question has no data on either side of it.

MEASUREMENT ONLY, AND STRUCTURALLY SO. This runs in its own process, reads the funded ledger
READ-ONLY, and learns about an exit from the durable record the runner already wrote. It cannot
delay an exit because it is not on the exit path at all — the same property that made the refusal
capture safe, and a stronger guarantee than a fire-and-forget call inside the executor, which
could still raise or block in the caller's thread. Nothing here reads back into a decision; the
V6 post-exit doctrine applies verbatim.

THE QUOTE IS THE POSITION'S OWN BAG. `exit_qty_atomic` is the exact quantity the take-profit sold,
recovered from the fill whose `tx_sig` IS the position's `close_sig`. Quoting that quantity back
answers "what would the bag we sold have been worth" in route space, at our real clip. A fixed
USD notional would answer a different and easier question, because impact is size-dependent and
the whole point of the confirmation window is whether a REAL exit could have got more.

AGE IS MEASURED FROM THE EXIT, NEVER FROM WHEN WE NOTICED IT. A poller detects a close seconds
after it happens; anchoring on capture start would relabel a late first look as t=0 and move every
observation earlier than it occurred. Anchored on `closed_at`, a series that begins at 2.4s says
so, and `detection_lag_s` records exactly how much of the early window was lost. The 3-second
window candidate is the one this bites hardest, so the honest floor matters more than the average.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

HORIZON_S = 60.0
CADENCE_S = 1.0
# The take-profit family. `real_trail` and `derisk` also sell into strength but under a different
# rule, so pooling them would answer a question nobody asked; they can be added deliberately.
TP_REASONS = ("real_tp",)
COMPLETE, GAPPED, TRUNCATED = "complete", "gapped", "truncated_worker"
# The capture lane's vocabulary, carried VERBATIM (prereg §3). `no_route` and `provider_error` are
# different facts and must never be merged: an unroutable coin is a real market state that ends a
# window, a provider error is our own blindness. Collapsing them would file our outage as the
# coin dying, in the direction that favours the hypothesis.
OK, NO_ROUTE, PROVIDER_ERROR = "ok", "no_route", "provider_error"


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def new_tp_exits(ledger, *, since_iso, limit=20):
    """Funded take-profit closes newer than the watermark, with the bag each one sold.

    The exit fill is matched on `tx_sig = close_sig` rather than on kind or recency. A position can
    carry a de-risk and a partial before its final exit, and the study is about the quantity the
    TAKE-PROFIT sold — the transaction identity is the only thing that names it exactly.
    """
    placeholders = ",".join("?" * len(TP_REASONS))
    rows = ledger.execute(
        "SELECT p.id, p.mint, p.network_id, p.closed_at, p.close_sig, p.close_exit_reason, "
        "       f.qty, f.notional, f.price "
        "FROM memecoin_positions p "
        "LEFT JOIN memecoin_fills f ON f.position_id = p.id AND f.tx_sig = p.close_sig "
        "WHERE p.execution_mode='real' AND p.closed_at > ? "
        "  AND p.close_exit_reason IN (%s) AND p.close_sig IS NOT NULL "
        "ORDER BY p.closed_at LIMIT ?" % placeholders,
        (since_iso, *TP_REASONS, int(limit))).fetchall()
    out = []
    for r in rows:
        if not r["qty"] or float(r["qty"]) <= 0:
            # No recoverable bag means no route-real quote. Skipped rather than quoted at a made-up
            # size: a series priced on the wrong quantity is worse than a missing one, because it
            # looks like evidence.
            logger.info("post-tp: position %s has no exit-fill qty for %s; skipped",
                        r["id"], r["close_sig"])
            continue
        out.append(dict(r))
    return out


def schedule(exit_row, *, decimals, now_iso, followup_id=None):
    """Freeze what the capture needs before any network call. Returns None if unusable."""
    exited = _parse(exit_row["closed_at"])
    started = _parse(now_iso)
    if exited is None or started is None or decimals is None:
        return None
    lag = (started - exited).total_seconds()
    if lag >= HORIZON_S:
        # The whole window is already behind us. Capturing the tail would produce a series whose
        # first observation is past every candidate window, which answers nothing and still spends
        # quotes.
        logger.info("post-tp: position %s detected %.1fs after exit, past the %.0fs horizon",
                    exit_row["id"], lag, HORIZON_S)
        return None
    return {
        "followup_id": followup_id or uuid.uuid4().hex,
        "position_id": exit_row["id"],
        "close_sig": exit_row["close_sig"],
        "network_id": exit_row["network_id"],
        "mint": exit_row["mint"],
        "exit_reason": exit_row["close_exit_reason"],
        "exited_at": exit_row["closed_at"],
        "exit_qty_atomic": str(int(round(float(exit_row["qty"]) * (10 ** int(decimals))))),
        # The decimals travel WITH the plan. Deriving the unit price with a hardcoded 1e6 put
        # quote_price 1000x off exit_price on nine-decimal tokens — a systematic false negative
        # shaped exactly like the confirm-window hypothesis failing (Gamma audit, 2026-08-29).
        "token_decimals": int(decimals),
        "exit_value_usd": exit_row["notional"],
        "exit_price": exit_row["price"],
        "detection_lag_s": round(lag, 3),
        "_exited": exited,
    }


def due_ages(plan, *, now):
    """The next whole-second age to observe, or None. One observation per cadence step.

    Steps are computed from the EXIT, so a capture that starts late begins at the step it is
    genuinely at rather than at zero — two captures with different detection lags then share an
    age axis and can be pooled.
    """
    elapsed = (now - plan["_exited"]).total_seconds()
    if elapsed > HORIZON_S:
        return None
    step = int(elapsed // CADENCE_S)
    return step if step >= plan.get("_last_step", -1) + 1 else None


def observation(plan, *, quote, now_iso, now, failure=None):
    """One immutable row from one quote. `quote_value_usd` is the whole bag, not a unit price."""
    out_usd = (int(quote.get("outAmount") or 0) / 1e6) if quote else None
    qty_atomic = int(plan["exit_qty_atomic"])
    return {
        **{k: plan[k] for k in ("followup_id", "position_id", "close_sig", "network_id", "mint",
                                "exit_reason", "exited_at", "exit_qty_atomic", "exit_value_usd",
                                "exit_price", "detection_lag_s", "token_decimals")},
        "observed_at": now_iso,
        "age_s": round((now - plan["_exited"]).total_seconds(), 3),
        # The unit price is derived from the bag so both spaces are recoverable from one row, and
        # neither has to be recomputed from a stored percentage later.
        "quote_price": (out_usd / (qty_atomic / (10 ** int(plan["token_decimals"]))))
                       if (out_usd and qty_atomic and plan.get("token_decimals") is not None)
                       else None,
        "quote_value_usd": out_usd,
        # THREE STATES, NOT TWO. A raised provider call used to land here as `no_route`, which
        # says the coin cannot be sold when what happened is that we could not ask.
        "quality": (PROVIDER_ERROR if failure else (OK if out_usd else NO_ROUTE)),
        "provider": (quote or {}).get("_provider"),
        "latency_ms": (quote or {}).get("_latency_ms"),
    }


def terminal(plan, ages, *, outcome=None, qualities=()):
    """Coverage for one finished follow-up. `ages` are the observed ages, in order.

    `censored` is computed HERE and stored, so the censored rate is a query rather than a scan of
    every observation. The prereg requires it reported beside every result: `no_route` correlates
    with dying coins, so dropping censored windows biases TOWARD continuation — the direction that
    favours the hypothesis, which makes silence about the rate a thumb on the scale.
    """
    expected = int(HORIZON_S // CADENCE_S)
    gaps = [b - a for a, b in zip(ages, ages[1:])] if len(ages) > 1 else []
    if outcome is None:
        # A gap wider than two cadence steps is a hole, not jitter. Named so a reader can exclude
        # it rather than average across it.
        outcome = GAPPED if (gaps and max(gaps) > CADENCE_S * 2.5) else COMPLETE
    return {
        **{k: plan[k] for k in ("followup_id", "position_id", "close_sig", "network_id", "mint",
                                "exit_reason", "exited_at", "exit_qty_atomic", "exit_value_usd",
                                "exit_price", "detection_lag_s")},
        "censored": 1 if any(q != OK for q in qualities) else 0,
        "horizon_s": HORIZON_S, "cadence_s": CADENCE_S, "expected_obs": expected,
        "n_obs": len(ages),
        "first_age_s": round(ages[0], 3) if ages else None,
        "max_gap_s": round(max(gaps), 3) if gaps else None,
        "outcome": outcome,
    }
