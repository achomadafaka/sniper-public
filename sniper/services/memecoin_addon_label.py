"""The ADD-ON LABEL on every funded entry attempt (Alpha 2026-10-17, after Deadpool pos #73925).

A LABEL, NOT A GATE. Gamma's read of 'add-on after a missed larger first buy' since 10-01 (34
funded fills, +$57.79, ROI +1.7% vs 262 first-buy fills at +2.1%) killed the refusal; the forward
read at 30 labeled fills decides any rule. So this only records, after the attempt's own decision,
whether the copied trader had already bought this coin in the ADDON_WINDOW_S before the judged buy:

  first_buy_price, first_buy_usd, first_buy_at   his earliest earlier buy in the window
  prior_net_usd                                  his net (buys minus sells) before the judged buy
  addon_share_pct                                the judged buy's share of his net including it
  our_price_over_first_pct                       our fill (else our quote) over his first buy
  missed_first_attempt_status                    our attempt on that first buy, by its transaction

The block is ABSENT when he had no earlier buy (or the history is unreadable); a field is None
when unknown, never 0. Read at attempt finish, after the funded result (the fill path never waits
on it), with the source-buy floor's own prior-net read plus one bounded first-buy read and at most
three exact-origin attempt lookups. MEMECOIN_ADDON_LABEL=0: nothing is read or written.
Known limit: a first buy only J7's tx-less 'swap:<uuid>' report carries has no transaction, so its
attempt status reads None."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ADDON_WINDOW_S = 600.0
SUBSTAGE = "addon_context"


def knob_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_ADDON_LABEL", True))


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and value == value and value != float("inf") else None


def first_prior_buy(conn, *, source_user_id, network_id, mint, before_iso, judged_id, judged_usd,
                    window_s=ADDON_WINDOW_S):
    """The trader's earliest buy of `mint` in the `window_s` before `before_iso`, other than the
    judged trade: {price, usd, at, event_id, tx} or None. Reports of the judged transaction never
    count, nor a tx-less report within PRIOR_PAIR_S whose size pairs with the judged one."""
    from datetime import datetime, timedelta
    from sniper.persistence.db_memecoin import PRIOR_PAIR_PCT, PRIOR_PAIR_S, _prior_stamp, _prior_tx_key, source_tx_of
    end = datetime.fromisoformat(str(before_iso).replace("Z", "+00:00"))
    lo = (end - timedelta(seconds=float(window_s))).strftime("%Y-%m-%dT%H:%M:%S")
    hi = (end + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S")   # second prefix; exact below
    judged_tx = source_tx_of(judged_id)
    judged_key = _prior_tx_key(judged_tx) if judged_tx else None
    rows = []
    for fid, usd, price, created in conn.execute(
            "SELECT fomo_event_id, their_size_usd, their_price, created_at FROM memecoin_events "
            "WHERE source_user_id=? AND created_at > ? AND created_at < ? AND mint=? AND side='buy' "
            "AND COALESCE(network_id,'')=COALESCE(?, COALESCE(network_id,''))",
            (source_user_id, lo, hi, mint, network_id)):
        rows.append((fid, source_tx_of(fid), usd, price, created))
    for sig, usd, price, detected in conn.execute(
            "SELECT signature, source_size_usd, source_price, detected_at FROM onchain_wallet_detections "
            "WHERE source_user_id=? AND mint=? AND detected_at > ? AND detected_at < ? "
            "AND network_id=COALESCE(?, network_id)",
            (source_user_id, mint, lo, hi, network_id)):
        rows.append(("onchain:%s" % sig, source_tx_of(sig), usd, price, detected))
    before_t, best = end.timestamp(), None
    for fid, tx, usd, price, created in rows:
        if fid == judged_id or (judged_key and tx and _prior_tx_key(tx) == judged_key):
            continue
        try:
            at = _prior_stamp(created)
        except (TypeError, ValueError):
            continue
        if not at < before_t:
            continue
        if (tx is None and judged_usd and usd and before_t - at <= PRIOR_PAIR_S
                and abs(float(usd) - float(judged_usd)) <= PRIOR_PAIR_PCT * float(judged_usd)):
            continue                                  # a tx-less report of the judged trade itself
        if best is None or at < best["_t"]:
            best = {"_t": at, "price": _positive(price), "usd": _positive(usd), "at": created,
                    "event_id": fid, "tx": tx}
    if best is not None:
        best.pop("_t")
    return best


def _first_attempt_status(conn, first):
    """Our latest attempt on the first buy per exact origin (the event itself, and its transaction on
    the slot and on-chain lanes): {origin: 'status:reason'}, or None when we made none (or the first
    buy carries no transaction). Deadpool had two: the slot lane's failed send and the backup's refusal."""
    origins = [first.get("event_id")]
    if first.get("tx"):
        origins += ["slotlane:%s" % first["tx"], "onchain:%s" % first["tx"]]
    out = {}
    for origin in dict.fromkeys(o for o in origins if o):
        row = conn.execute(
            "SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts WHERE origin_event_id=? "
            "ORDER BY decision_started_at DESC, attempt_id DESC LIMIT 1", (str(origin),)).fetchone()
        if row is not None:
            out[origin.split(":", 1)[0]] = "%s:%s" % (row[0], str(row[1] or "")[:80])
    return out or None


def addon_block(conn, attempt, *, judged_usd, our_price, window_s=ADDON_WINDOW_S):
    """The add-on block for one attempt row, or None (no earlier buy, unreadable, or a manual entry)."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_origin import bypasses_selection
    if not attempt or bypasses_selection(attempt.get("signal_source")):
        return None
    uid, mint = attempt.get("source_user_id"), attempt.get("mint")
    before = attempt.get("source_created_at") or attempt.get("decision_started_at")
    origin = attempt.get("origin_event_id")
    if not uid or not mint or not before:
        return None
    judged_usd = _positive(judged_usd)
    prior = m.source_prior_net_buy_usd(conn, source_user_id=uid, network_id=attempt.get("network_id"),
                                       mint=mint, before_iso=before, window_s=window_s,
                                       exclude_event_id=origin, judged_usd=judged_usd)
    if prior is None:
        return None
    first = first_prior_buy(conn, source_user_id=uid, network_id=attempt.get("network_id"), mint=mint,
                            before_iso=before, judged_id=origin, judged_usd=judged_usd, window_s=window_s)
    if first is None:
        return None
    our = _positive(our_price)
    share = (round(100.0 * judged_usd / (prior + judged_usd), 1)
             if judged_usd is not None and prior + judged_usd > 0 else None)
    return {"first_buy_price": first["price"], "first_buy_usd": first["usd"], "first_buy_at": first["at"],
            "first_buy_event_id": first["event_id"], "prior_net_usd": round(float(prior), 2),
            "judged_usd": judged_usd, "addon_share_pct": share,
            "our_price_over_first_pct": (round(100.0 * (our / first["price"] - 1.0), 1)
                                         if our is not None and first["price"] is not None else None),
            "missed_first_attempt_status": _first_attempt_status(conn, first),
            "window_s": float(window_s)}


def record_addon_label(trace, result, *, judged_usd=None):
    """Mark the add-on block on the trace before it finishes. Never raises, never changes the
    result. `judged_usd` defaults to the trace's shadow context (the runner's source_size_usd)."""
    if trace is None or not knob_on():
        return None
    try:
        cur = trace.conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (trace.attempt_id,))
        row = cur.fetchone()
        attempt = None if row is None else dict(zip([d[0] for d in cur.description], row))
        if judged_usd is None:
            judged_usd = (trace.shadow_context() or {}).get("source_size_usd")
        our = (result or {}).get("fill_price") or (result or {}).get("quote_price")
        block = addon_block(trace.conn, attempt, judged_usd=judged_usd, our_price=our)
        if block is not None:
            trace.mark_substage(SUBSTAGE, **block)
        return block
    except Exception:
        logger.exception("memecoin add-on label failed (funded result unchanged)")
        return None


def card_words(block):
    """'add-on 8% of his $334 net, +23% over his first buy', or None. Unknown parts are left out."""
    if not isinstance(block, dict):
        return None
    share, prior, judged = block.get("addon_share_pct"), block.get("prior_net_usd"), block.get("judged_usd")
    parts = []
    if share is not None and prior is not None:
        parts.append("add-on %.0f%% of his $%.0f net" % (share, float(prior) + float(judged or 0)))
    else:
        parts.append("add-on after his first buy")
    over = block.get("our_price_over_first_pct")
    if over is not None:
        parts.append("%+.0f%% over his first buy" % over)
    return ", ".join(parts)
