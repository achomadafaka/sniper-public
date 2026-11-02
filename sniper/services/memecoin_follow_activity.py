"""Per-follow activity coverage poll + signal-source A/B.

The global fomo /feed under-samples active traders (measured: it missed 7/18 of one
follow's large buys). This pass polls each curated follow's own /swaps and copies any
recent LARGE BUY the feed MISSED, tagged signal_source='follow_activity' (vs the feed's
'global_feed'), so paper PnL can be compared by source. These signals are LATE by design
(poll cadence, post-herd) — the experiment measures whether the feed's real-time speed
actually pays off vs copying the same traders a few minutes late.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent, _norm_network
from sniper.feeds.fomo_account import fetch_recent_swaps
from sniper.analysis.fomo_account.classify import classify_swap
from sniper.execution.solana_quote import honest_fill
from sniper.util.clock import real_utc_now_iso

import logging

logger = logging.getLogger(__name__)


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _has_feed_event_near(conn, uid, mint, network_id, ts, window_s):
    """True if a global-feed buy for (uid, network, mint) exists within window_s of ts —
    i.e. the feed already caught this trade, so it's not a genuine miss."""
    return _has_event_near(m.buy_event_times(conn, uid, mint, network_id), ts, window_s)


def _has_event_near(times, ts, window_s):
    t = _parse(ts)
    if not t:
        return True   # unparseable -> be conservative, treat as already-seen
    for other in times:
        ot = _parse(other)
        if ot and abs((ot - t).total_seconds()) <= window_s:
            return True
    return False


def _follow_alert_kind(ev, *, armed, min_usd):
    """Alert routing for a follow-activity catch from an ARMED wallet over the size floor:
    - 'fresh'  — lag within MEMECOIN_TG_MAX_LAG_S: ARMED (fast lane), alert like a
      normal front-run signal regardless of the delayed-catch toggle. Added 2026-07-16 after
      the operator never saw smol_intern's micro buys: fresh catches had NO alert path once
      the delayed-catch alert went dark.
    - 'delayed' — stale backfill; only when MEMECOIN_TG_DELAYED_CATCH_ENABLED (off by default:
      120-900s-old signals are unactionable channel noise, operator call 2026-07-16).
    - None — no alert."""
    from sniper.settings import memecoin as _cfg
    if not armed or min_usd is None:
        return None
    try:
        if float(getattr(ev, "their_size_usd", 0) or 0) < float(min_usd):
            return None
    except (TypeError, ValueError):
        return None
    # Same mcap ceiling as the front-run lane (2026-08-17, operator): no follow alert for a
    # coin with a KNOWN market cap over MEMECOIN_TG_MAX_MCAP_USD.
    if not _cfg.tg_mcap_ok(getattr(ev, "their_market_cap", None)):
        return None
    try:
        lag = float(ev.lag_s) if getattr(ev, "lag_s", None) is not None else None
    except (TypeError, ValueError):
        lag = None
    if lag is not None and 0 <= lag <= _cfg.MEMECOIN_TG_MAX_LAG_S:
        return "fresh"
    return "delayed" if _cfg.MEMECOIN_TG_DELAYED_CATCH_ENABLED else None


def _should_follow_alert(ev, *, armed, min_usd):
    """Back-compat boolean over _follow_alert_kind (any alert kind -> True)."""
    return _follow_alert_kind(ev, armed=armed, min_usd=min_usd) is not None


def _live_display_mcap(mint, network_id):
    """LAST-RESORT market cap for the ALERT ONLY. Never a decision input.

    Why it is needed: a coin fresh enough that the funded lane bands it `assumed_nano` has no
    stored market cap anywhere -- the feed event carries 0, `resolve_mint_mcap` has no same-mint
    history, the classification resolved None, `entry_mcap` is NULL and no mark carries one. The
    alert then prints "mcap ~?" for a coin the provider can price perfectly well. Measured on
    OMEGA (0x8338e93a..., 4663, filled 2026-08-30T16:58): all five stored sources empty,
    DexScreener returning $52,800 the whole time.

    WHY IT IS NOT WIRED INTO THE DECISION, and this separation is the point. `entry_mcap` feeds
    `funded_policy_for_arm` -> `resolve_policy`, so it selects the position's EXIT RAILS. The
    2026-08-30 band-source pin deliberately keeps DexScreener out of that on DEX-blind chains,
    because the two mcap sources disagree beyond 10% on ~50% of 4663 tokens with tails to 9x, and
    nobody has established which is right. Display can be wrong by a factor and cost nothing;
    a band can be wrong by a factor and change when we sell. So this value reaches the operator's
    eyes and stops there.

    One provider call, only when every stored source is empty, on an alert that fires per armed
    buy (~3/hour on 4663). It goes through the shared DexScreener breaker and returns None on any
    failure -- an alert must never fail because a market cap could not be fetched.
    """
    if not mint or not network_id:
        return None
    try:
        from sniper.execution.solana_quote import dexscreener_stats
        stats = dexscreener_stats(mint, network_id=str(network_id))
    except Exception:
        logger.exception("alert display mcap lookup failed mint=%s (alert unchanged)", mint)
        return None
    try:
        value = float((stats or {}).get("market_cap") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _classified_effective_mcap(attempt):
    """The mcap the funded classifier actually resolved for this attempt, or None.

    Read-only display helper: this is the value that decided the coin band, so showing it keeps the
    alert consistent with the admission decision printed directly beneath it. Never raises — a
    malformed or absent classification just means 'unknown'."""
    import json as _json
    payload = (attempt or {}).get("classification_json")
    if not payload:
        return None
    try:
        facts = (_json.loads(payload) or {}).get("facts") or {}
        value = float(facts.get("effective_mcap"))
    except (ValueError, TypeError, AttributeError):
        return None
    return value if value > 0 else None


def _retry_pending_status(terminal_status, real_result):
    """`retrying` while the re-quote lane still owns a transient miss, else the real status.

    Only rewrites a transient miss, and only while the lane is enabled — a genuine refusal, a fill
    and a give-up all keep their own word.
    """
    status = terminal_status or "pending"
    if str(status).lower() not in {"aborted", "route_failed", "rejected", "unsafe", "failed"}:
        return status
    try:
        from sniper.settings import memecoin as _cfg
        if float(getattr(_cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0) or 0) <= 0:
            return status
        from sniper.services.memecoin_real_runner import _is_transient_entry_miss
        if _is_transient_entry_miss(real_result):
            return "retrying"
    except Exception:
        return status                # never let an alert-cosmetic read break the alert
    return status


def _follow_alert_context(conn, uid, *, armed, ev=None, real_result=None,
                          real_attempted=False, real_error=None, real_lane_enabled=True):
    """Honest wallet, coin, and exact-entry context for one follow-lane alert."""
    from sniper.settings import memecoin as _cfg
    from sniper.services.memecoin_chains import funded_chain_keys, resolve_chain
    chain = resolve_chain(getattr(ev, "network_id", None)) if ev is not None else None
    funded = chain in funded_chain_keys()
    g = m.alpha_b_copy_stats(conn, uid, chain=chain) if funded else None
    context = {
        "real_armed": bool(armed and funded),
        "authority_status": ("armed" if armed and funded else
                             "watch_only" if funded else "unfunded_chain"),
        "gate_applicable": funded,
        "chain": chain,
        "paper_pnl": g["pnl"] if g else None,
        "paper_n": g["n_closes"] if g else None,
        "gate_ok": bool(
            g and g["n_closes"] >= _cfg.MEMECOIN_REAL_PROMOTE_MIN_CLOSES
            and g["pnl"] >= _cfg.MEMECOIN_REAL_PROMOTE_MIN_PNL_USD
        ),
    }
    if ev is None:
        return context

    network_id = getattr(ev, "network_id", None)
    mint = getattr(ev, "mint", None)
    ticker = getattr(ev, "ticker", None) or None
    if not ticker and network_id is not None:
        ticker = m.resolve_mint_ticker(conn, mint, network_id=network_id)
    raw_mcap = getattr(ev, "their_market_cap", None)
    try:
        display_mcap = float(raw_mcap) if float(raw_mcap or 0) > 0 else None
    except (TypeError, ValueError):
        display_mcap = None
    if display_mcap is None and network_id is not None:
        display_mcap = m.resolve_mint_mcap(conn, mint, network_id=network_id)
    context.update({
        "display_ticker": ticker,
        "display_market_cap": display_mcap,
        "prior_buyers_60m": (
            m.prior_buyers_60m(
                conn,
                mint=mint,
                network_id=network_id,
                source_user_id=uid,
                before_iso=getattr(ev, "created_at", None),
            ) if network_id is not None else None
        ),
    })

    attempt = m.entry_attempt_for_origin(conn, getattr(ev, "fomo_event_id", None))
    if attempt is not None:
        terminal_status = attempt.get("terminal_status")
        context.update({
            # A TRANSIENT MISS IS NOT A VERDICT WHILE THE RETRY LANE STILL OWNS IT (2026-09-08).
            # `_alert_entry_result` in the runner already returns early for this case; this path
            # did not, so a fresh-pool miss paged as "entry: ABORTED" and the re-quote filled
            # seconds later with no correction. HOODLAND: aborted 14:15:41 `no_route_all_venues`,
            # FILLED 14:16:17 on retry, closed real_tp +$11.03 — and the operator was asked to fix
            # a trade that had already profited. Report the truth, which is that the decision is
            # still open, rather than a refusal we are about to overturn.
            "entry_status": _retry_pending_status(terminal_status, real_result),
            "entry_reason": (attempt.get("terminal_reason")
                             or "attempt trace incomplete"),
            "entry_code": (real_result or {}).get("status"),
            "attempt_id": attempt.get("attempt_id"),
            "pending_id": attempt.get("pending_id"),
            "tx_sig": attempt.get("tx_sig"),
            "position_id": attempt.get("position_id"),
        })
        # The CLASSIFIER resolves mcap live (DexScreener); the display fallbacks above only read
        # OUR tables. A coin seen solely via follow_activity has no non-zero reading to borrow, so
        # the alert printed "mcap ~?" for a coin the pipeline had already banded (2026-07-27:
        # banded 'micro', i.e. $50k-$250k, yet displayed as unknown). Prefer the resolved value.
        if context.get("display_market_cap") is None:
            resolved = _classified_effective_mcap(attempt)
            if resolved is not None:
                context["display_market_cap"] = resolved
    elif real_error is not None:
        detail = str(real_error).strip().replace("\n", " ")[:200]
        context.update({
            "entry_status": "failed",
            "entry_code": "hook_exception",
            "entry_reason": detail or type(real_error).__name__,
        })
    elif real_attempted:
        context.update({
            "entry_status": "not_attributed",
            "entry_reason": "attempt trace missing",
        })
    else:
        context.update({
            "entry_status": "not_attempted",
            "entry_reason": ("follow real lane disabled" if not real_lane_enabled
                             else "real hook unavailable"),
        })
    # LAST RESORT, display only, AFTER the whole entry-status chain. It sits here rather than
    # inside the `attempt is not None` branch because a coin refused before an attempt row exists
    # is exactly the one whose market cap the operator most wants to see. Placing it mid-chain
    # detaches the `elif`s from their `if` and silently rewrites every non-attempt entry status —
    # three existing tests caught precisely that.
    if context.get("display_market_cap") is None:
        context["display_market_cap"] = _live_display_mcap(mint, network_id)
    return context


def fast_lane_wallets(conn):
    """The fast-lane roster: EFFECTIVE armed wallets (real_approved AND not paused AND not
    excluded) — the same set the real leg trades. Re-read every sweep so arm/revoke applies
    live. Latency-cliff rationale (verified 2026-07-15): <60s follow catches were +$79/copy
    78% WR (honest alpha_b, n=9) vs −$14 to −$16/copy at 60-600s; a 107-wallet × 2s sweep +
    300s idle put armed catches in the losing buckets by construction."""
    # Observation roster, not authority: a wallet armed on ANY chain is worth watching fast.
    # Entry authority always names a chain (real_approved_user_ids(conn, network_id=...)).
    armed = m.real_armed_any_chain_user_ids(conn)
    return [w for w in m.list_wallets(conn, include_paused=False)
            if w["source_user_id"] in armed]


def _wallet_responses(roster, swaps_fn, receipt_fn, fetch_workers):
    def fetch(wallet):
        return swaps_fn(wallet["source_user_id"]) or []

    workers = max(1, min(int(fetch_workers or 1), len(roster) or 1))
    if workers == 1:
        for wallet in roster:
            try:
                yield wallet, fetch(wallet), receipt_fn(), None
            except Exception as exc:
                yield wallet, [], receipt_fn(), exc
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="follow-fetch") as pool:
        pending = {pool.submit(fetch, wallet): wallet for wallet in roster}
        for future in as_completed(pending):
            wallet = pending[future]
            try:
                yield wallet, future.result(), receipt_fn(), None
            except Exception as exc:
                yield wallet, [], receipt_fn(), exc


# A dispatch lag above this many seconds is logged. p90 on 2026-08-23 was 0.64s across 494
# attempts, so 2s is unambiguously an outlier rather than normal jitter.
_DISPATCH_LAG_WARN_S = 2.0


def _buys_first(swaps):
    """Order a wallet's swaps so every BUY is dispatched before any sell is classified.

    The per-wallet loop is serial and the sell branch performs a DB WRITE
    (`mark_sell_classified`) plus a `sell_event_times` read before the buy branch is reached. So a
    batch that happens to lead with large sells puts measurement writes in front of entry
    submission on the latency-critical armed fast lane, and every one of those writes competes for
    the single SQLite write lock on a 7.7 GB ledger that also has a web server and a research
    daemon holding write handles.

    CLAUDE.md already states the rule for this exact class of work: "candidate-cohort DB writes run
    only after the current poll's funded hooks. Never put probe HTTP or candidate research writes
    before entry submission." The sell gauge is measurement, and its own comment says "measure
    only, never act".

    Pure reordering. Both branches are idempotent and order-independent: the sell gauge dedups on
    swap id through `mark_sell_classified`, and the buy path dedups on `fomo_event_id` through
    `insert_event`. Relative order WITHIN buys and WITHIN sells is preserved.
    """
    buys, rest = [], []
    for s in swaps:
        (buys if classify_swap(s)[0] == "buy" else rest).append(s)
    return buys + rest


def dispatch_trading_activity(conn, payload, size_usd, *, real_hook, now_fn=None,
                              fill_fn=honest_fill, stats_fn=None):
    """Send one Fomo WebSocket item through the existing funded follow path."""
    from sniper.feeds.fomo_account import activity_payload_to_swap

    swap = activity_payload_to_swap(payload)
    if swap is None or real_hook is None:
        return {"status": "ignored"}
    uid = swap["userId"]
    roster = {w["source_user_id"]: w for w in fast_lane_wallets(conn)}
    event_id = "act-" + str(swap["id"])
    prior = conn.execute(
        "SELECT action FROM memecoin_events WHERE fomo_event_id=?", (event_id,)
    ).fetchone()
    if prior and prior["action"] != "paper_failed":
        return {"status": "duplicate"}

    kind, mint, usd, qty = classify_swap(swap)
    if kind == "buy":
        wallet = roster.get(uid)
        if wallet is None:
            return {"status": "ignored"}
        follow_activity_pass(
            conn,
            None,
            size_usd,
            real_hook=real_hook,
            now_fn=now_fn,
            real_now_fn=now_fn,
            fill_fn=fill_fn,
            stats_fn=stats_fn,
            swaps_fn=lambda _uid: [swap],
            sleep_fn=None,
            wallets=[wallet],
            reconcile_holders=False,
            delivery_lane="websocket",
        )
        row = conn.execute(
            "SELECT action FROM memecoin_events WHERE fomo_event_id=?", (event_id,)
        ).fetchone()
        return {"status": "dispatched" if row else "ignored"}

    if kind != "sell" or uid not in (set(roster) | m.open_position_user_ids(conn, mode="real")):
        return {"status": "ignored"}
    observed_at = (now_fn or real_utc_now_iso)()
    created_at = swap["createdAt"]
    created = _parse(created_at)
    observed = _parse(observed_at)
    lag_s = (observed - created).total_seconds() if created and observed else -1.0
    ev = CopyEvent(
        source_user_id=uid,
        source_handle=swap.get("userHandle") or roster.get(uid, {}).get("handle") or "",
        mint=mint,
        ticker=swap.get("ticker") or "",
        side="sell",
        their_price=(usd / qty if qty else 0.0),
        their_market_cap=float(swap.get("marketCap") or 0),
        their_size_usd=usd,
        is_first_buy=False,
        fomo_event_id=event_id,
        created_at=created_at,
        received_at=observed_at,
        lag_s=lag_s,
        signal_source="follow_activity",
        delivery_lane="websocket",
        network_id=_norm_network(swap.get("networkId")),
        received_at_normalized=observed_at,
        clock_domain="observed_real_utc",
    )
    if not m.insert_event(conn, ev.__dict__):
        return {"status": "duplicate"}
    result = real_hook(conn, ev) or {}
    m.set_event_action(conn, event_id, result.get("status") or "ignored")
    return {"status": "dispatched", "real": result}


def follow_activity_pass(conn, auth, size_usd, *, real_hook=None, now_fn=None, real_now_fn=None,
                         fill_fn=honest_fill, stats_fn=None, swaps_fn=None, sleep_fn=None,
                         alert_fn=None, min_usd=None, max_age_s=None, dedup_window_s=None,
                         wallets=None, exclude_uids=None, sleep_s=None, fetch_workers=1,
                         reconcile_holders=True, delivery_lane="rest"):
    """Poll each non-paused curated follow's recent swaps; copy feed-missed large buys as
    signal_source='follow_activity'. Returns {checked, copied, skipped_seen, skipped_stale}.

    All position hold-stamps are on CORRECTED real-UTC (now_fn default = real_utc_now_iso) so
    opened_at matches the real-UTC exit sweep and web manual stamps (no cross-clock hold). The
    staleness age (real_now_fn, also real-UTC) diffs against fomo's real-UTC `createdAt` — diffing
    the host clock (~+7h fast) made every swap look 7h stale, so the arm copied nothing since
    launch. Both clocks default to real-UTC; they stay separate params only so tests can pin them."""
    from sniper.settings.memecoin import (
        MEMECOIN_FOLLOW_ACTIVITY_ENABLED, MEMECOIN_FOLLOW_ACTIVITY_MIN_USD,
        MEMECOIN_FOLLOW_ACTIVITY_MAX_AGE_S, MEMECOIN_FOLLOW_ACTIVITY_DEDUP_S,
        MEMECOIN_ANALYZER_SLEEP_S, MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED,
        MEMECOIN_FUNDED_ONLY)
    if not MEMECOIN_FOLLOW_ACTIVITY_ENABLED:
        return {"checked": 0, "copied": 0, "skipped_seen": 0, "skipped_stale": 0,
                "sells_seen": 0, "sells_missed": 0}
    # Operator safety gate: with the delayed-catch real leg disabled, suppress the real hook on
    # BOTH lanes (fast + slow both route through here) while keeping paper copy, alerts, and sell
    # coverage. The fast-lane latency-cliff edge is pre-registered for judgement at >=30 closed
    # armed follow copies before real money rides it. Global-feed real entries are on their own path.
    real_lane_enabled = bool(MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED)
    if not real_lane_enabled:
        real_hook = None
    now_fn = now_fn or real_utc_now_iso
    real_now_fn = real_now_fn or real_utc_now_iso
    swaps_fn = swaps_fn or (lambda uid: fetch_recent_swaps(auth, uid))
    sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
    min_usd = MEMECOIN_FOLLOW_ACTIVITY_MIN_USD if min_usd is None else min_usd
    max_age_s = MEMECOIN_FOLLOW_ACTIVITY_MAX_AGE_S if max_age_s is None else max_age_s
    dedup_window_s = MEMECOIN_FOLLOW_ACTIVITY_DEDUP_S if dedup_window_s is None else dedup_window_s

    # Keep one sweep clock for stable age comparisons. Receipt provenance is stamped separately,
    # immediately after each wallet response arrives.
    sweep_iso = real_now_fn()
    parsed_sweep = _parse(sweep_iso)
    checked = copied = skipped_seen = skipped_stale = 0
    sells_seen = sells_missed = 0

    # Two-lane rosters: `wallets` = explicit roster (fast lane: armed only, ~62s sweep);
    # `exclude_uids` = slow lane skipping wallets the fast lane already covers (WAF budget —
    # correctness never depends on this: insert_event dedups by swap id, so lane overlap can
    # never double-open). Defaults keep the single-lane behavior byte-identical.
    roster = wallets if wallets is not None else m.list_wallets(conn, include_paused=False)
    if MEMECOIN_FUNDED_ONLY:
        armed = m.real_armed_any_chain_user_ids(conn)
        roster = [w for w in roster if w["source_user_id"] in armed]
    if exclude_uids:
        roster = [w for w in roster if w["source_user_id"] not in exclude_uids]
    for w, swaps, observed_iso, fetch_error in _wallet_responses(
            roster, swaps_fn, real_now_fn, fetch_workers):
        uid = w["source_user_id"]
        if fetch_error is not None:
            try:
                from sniper.services.memecoin_holder_evidence import invalidate_wallet_evidence
                invalidate_wallet_evidence(conn, uid, real_now_fn(), "follow_poll_failed")
            except Exception:
                logger.exception("holder evidence poll-failure invalidation failed uid=%s", uid)
            # This worker sleeps and performs the next wallet's HTTP request after the failure.
            # Do not carry the evidence write transaction across either wait.
            conn.commit()
            continue
        if _parse(observed_iso) is None:
            observed_iso = datetime.now(timezone.utc).isoformat()
        now = _parse(observed_iso) or parsed_sweep or datetime.now(timezone.utc)
        checked += 1
        for s in _buys_first(swaps):
            kind, mint, usd, qty = classify_swap(s)
            network_id = _norm_network(s.get("networkId") or s.get("network_id"))
            # SELL coverage gauge (measure only, never act): mirror exits depend on the global
            # feed catching the trader's sell. Classify each large sell EXACTLY ONCE, and only in
            # a stable window [dedup_window, max_age] old — old enough that the feed had its fair
            # chance (so 'missed' won't later flip to 'seen'), young enough to still be relevant —
            # then persist so fomo's re-delivered newest-<=100 swaps don't double-count it.
            if kind == "sell" and mint and usd >= min_usd:
                st = s.get("createdAt")
                t = _parse(st)
                s_age = (now - t).total_seconds() if t else None
                if s_age is None or s_age < dedup_window_s or s_age > max_age_s:
                    continue
                seen = _has_event_near(
                    m.sell_event_times(conn, uid, mint, network_id), st, dedup_window_s
                )
                sid = s.get("id") or f"{uid}-{mint}-{st}"
                if m.mark_sell_classified(conn, sid, uid, mint, seen, now_fn()):
                    if seen:
                        sells_seen += 1
                    else:
                        sells_missed += 1
                continue
            if kind != "buy" or not mint or usd < min_usd:
                continue
            ts = s.get("createdAt")
            t = _parse(ts)
            age = (now - t).total_seconds() if t else None
            if age is None or age > max_age_s or age < 0:
                skipped_stale += 1
                continue
            event_id = "act-" + str(s.get("id") or f"{uid}-{mint}-{ts}")
            prior = conn.execute(
                "SELECT action FROM memecoin_events WHERE fomo_event_id=?", (event_id,)
            ).fetchone()
            paper_retry = bool(prior and prior["action"] == "paper_failed")
            if (not paper_retry
                    and _has_feed_event_near(conn, uid, mint, network_id, ts, dedup_window_s)):
                skipped_seen += 1
                continue
            # The activity endpoint returns mint/qty/usd/ts and NOTHING else — no symbol, no mcap.
            # Writing blanks made every downstream reader guess: memecoin_events rows from this lane
            # carried ticker='' and mcap=0.0 forever, so resolve_mint_*() had nothing to borrow for
            # a coin seen ONLY here and alerts printed "? — ? mcap". Fill from our own tables when a
            # feed event already priced this coin. DB-only on purpose: this is the latency-critical
            # armed fast lane (<60s is the +EV band), so no network call goes here — the funded
            # classifier still resolves live downstream for the admission decision.
            act_ticker = (s.get("ticker")
                          or m.resolve_mint_ticker(conn, mint, network_id=network_id) or "")
            try:
                act_mcap = float(s.get("marketCap") or 0)
            except (TypeError, ValueError):
                act_mcap = 0.0
            if act_mcap <= 0:
                act_mcap = m.resolve_mint_mcap(conn, mint, network_id=network_id) or 0.0
            ev = CopyEvent(
                source_user_id=uid, source_handle=w.get("handle") or "", mint=mint,
                ticker=act_ticker, side="buy", their_price=(usd / qty if qty else 0.0),
                their_market_cap=act_mcap, their_size_usd=usd, is_first_buy=False,
                fomo_event_id=event_id,
                created_at=ts, received_at=now_fn(), lag_s=(age if age is not None else -1.0),
                signal_source="follow_activity",
                delivery_lane=delivery_lane,
                network_id=network_id,
                received_at_normalized=observed_iso,
                clock_domain="observed_real_utc")
            inserted = m.insert_event(conn, ev.__dict__)
            if not inserted:
                if not paper_retry:
                    continue
            # DISPATCH LAG. Everything between the wallet response and this point is DB-only by
            # design (see the comment above), so a large gap means the database blocked, not that
            # the lane did work. It is invisible otherwise: `observed_at` on the attempt row is the
            # response receipt and `decision_started_at` is the hook entry, so the interval shows up
            # as a mystery in the entry trace with nothing naming it.
            #
            # Live 2026-08-23: position 44596 (KIRK) sat 11.22s here. That is 1 of 494 attempts on
            # the day (p50 0.01s, p90 0.64s) and nothing was logged during it, so the cause is still
            # unknown. This line makes the next one self-explaining instead.
            _lag_s = None
            try:
                _obs = _parse(observed_iso)
                if _obs is not None:
                    _lag_s = (_parse(now_fn()) - _obs).total_seconds()
            except Exception:
                _lag_s = None
            if _lag_s is not None and _lag_s >= _DISPATCH_LAG_WARN_S:
                logger.warning(
                    "follow_activity dispatch lag %.2fs (db-only path) wallet=%s coin=%s "
                    "network=%s swaps_in_batch=%d", _lag_s, uid, mint, network_id, len(swaps))

            # The funded hook is the money path. Optional paper work must not delay it.
            rres = None
            real_error = None
            real_attempted = real_hook is not None and not paper_retry
            if real_attempted:
                try:
                    rres = real_hook(conn, ev)
                    st = (rres or {}).get("status")
                    if st in ("disarmed", "rejected", "aborted", "route_failed", "unsafe",
                              "skipped_chase"):
                        logger.warning("real entry REFUSED (follow_activity) status=%s reason=%s "
                                       "wallet=%s coin=%s", st, (rres or {}).get("reason"),
                                       getattr(ev, "source_handle", "?"),
                                       getattr(ev, "ticker", None) or ev.mint[:12])
                except Exception as exc:
                    real_error = exc
                    logger.exception("memecoin real hook failed (follow_activity) for %s",
                                     ev.fomo_event_id)
            # fan out to ALL arms (not control-only) so the strategy A/B isn't confounded with
            # the signal_source A/B — every arm sees the same follow_activity entries. Apply the
            # SAME conviction sizing the global-feed path uses, else enabling sizing would
            # confound bet size with signal source.
            from sniper.settings.memecoin import MEMECOIN_ARMS
            from sniper.services.memecoin_copy_trader import arm_base_size, copy_event_to_arms
            ev_size = arm_base_size(MEMECOIN_ARMS, ev.their_size_usd, size_usd)
            # OFF THIS THREAD when a fan-out is installed (memecoin_paper_fanout, 2026-09-27).
            from sniper.services.memecoin_paper_fanout import submit_or_none as _paper_submit
            _queued = _paper_submit(conn, ev, ev_size, arms=MEMECOIN_ARMS, fill_fn=fill_fn,
                                    stats_fn=stats_fn, delete_on_failure=False,
                                    action_rule="follow")
            if _queued is None:
                try:
                    outcomes = copy_event_to_arms(
                        conn, ev, ev_size, now_fn(), arms=MEMECOIN_ARMS,
                        fill_fn=fill_fn, stats_fn=stats_fn,
                        funded_quote=m.funded_quote_for_origin(
                            conn, getattr(ev, 'fomo_event_id', None)),
                        funded_clip=m.funded_clip_for_origin(
                            conn, getattr(ev, 'fomo_event_id', None)))
                except Exception:
                    outcomes = {}
                    m.set_event_action(conn, ev.fomo_event_id, "paper_failed")
                    logger.exception("paper fan-out failed after funded hook for %s",
                                     ev.fomo_event_id)
                else:
                    vals = list(outcomes.values())
                    m.set_event_action(
                        conn, ev.fomo_event_id,
                        "opened" if "opened" in vals else (vals[0] if vals else "ignored"))
                    if "opened" in vals:
                        copied += 1
            # RESULT-AWARE FOLLOW ALERT. The funded hook always runs first. Production delivery is
            # queued, so Telegram can neither delay this attempt nor starve the next wallet poll.
            # NOT recorded in memecoin_alert_latency — that instrument is global_feed front-run only.
            try:
                from sniper.settings import memecoin as _tgcfg
                from sniper.settings.memecoin import MEMECOIN_TG_ALERTS
                from sniper.services.memecoin_chains import canonical_chain_id
                armed = canonical_chain_id(getattr(ev, "network_id", None)) in set(
                    m.wallet_armed_chains(conn, uid))
                # Same floor the front-run lane uses: max(flat floor, share of market cap).
                kind = _follow_alert_kind(
                    ev, armed=armed,
                    min_usd=_tgcfg.tg_min_buy_usd(getattr(ev, "their_market_cap", None)))
                # A J7 SHADOW decision spends nothing; the fast lane's attempt carries the card.
                # Neither does a J7 buy refused because only the native lane may fund the chain
                # (fast-lane-only): it would page a reason that is not the coin's (RARI, 10-06).
                if (MEMECOIN_TG_ALERTS and kind and not paper_retry
                        and not (rres or {}).get("j7_shadow")
                        and (rres or {}).get("reason") != "native_fast_lane_unavailable"):
                    from sniper.services.memecoin_telegram import (
                        build_alert_message, build_follow_activity_alert, enqueue_telegram)
                    fctx = _follow_alert_context(
                        conn,
                        uid,
                        armed=armed,
                        ev=ev,
                        real_result=rres,
                        real_attempted=real_attempted,
                        real_error=real_error,
                        real_lane_enabled=real_lane_enabled,
                    )
                    msg = (build_alert_message(ev, context=fctx) if kind == "fresh"
                           else build_follow_activity_alert(ev, context=fctx))
                    if alert_fn is not None:
                        alert_fn(msg)
                    else:
                        enqueue_telegram(
                            msg, kind=f"follow_activity:{kind}",
                            source_id=getattr(ev, "fomo_event_id", None))
            except Exception:
                logger.exception("follow-activity alert failed (copy path unaffected)")
        # Local observation work runs only after every buy in this response reached the funded
        # hook. It consumes the response already in memory and adds no request or entry latency.
        if reconcile_holders:
            try:
                from sniper.services.memecoin_holder_evidence import reconcile_follow_poll
                reconcile_follow_poll(conn, uid, swaps, observed_iso)
            except Exception:
                logger.exception("holder evidence follow reconciliation failed uid=%s", uid)
        # One wallet is the durable unit. The paced follow lanes wait between wallets; leaving
        # reconciliation open here pins the main WAL for the full 17-wallet sweep.
        conn.commit()
        if sleep_fn and int(fetch_workers or 1) <= 1:
            # The paced lane waits between wallet requests. The fast lane uses bounded workers.
            sleep_fn(MEMECOIN_ANALYZER_SLEEP_S if sleep_s is None else sleep_s)
    return {"checked": checked, "copied": copied,
            "skipped_seen": skipped_seen, "skipped_stale": skipped_stale,
            "sells_seen": sells_seen, "sells_missed": sells_missed}
