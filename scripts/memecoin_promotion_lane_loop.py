#!/usr/bin/env python3
"""Lane A worker — evaluate the funded gates for signals the funded path never gated.

A wallet that is not armed stops at the approval check, so its signals never reach the gate stack:
the `funded_entry_shadow_v1` substage on those attempts names fifteen gate families and every one
reads "unknown". This worker replays the reproducible gates a minute later and records what the
funded path WOULD have decided, then, only for an admitted signal, models the position the funded
policy would have held.

TWO PASSES ON TWO CLOCKS. A signal is EVALUATED seconds after the decision and PRICED hours after
it, because the modelled hold runs for the policy's whole timecap. The first supervised pass
(2026-09-23) did both at once and wrote four admitted rows with no fill at all -- the exit window
was still in the future when it looked. So `run_once` evaluates and leaves `fill_basis='pending'`,
and `run_settle` prices a row once its own `horizon_target_s` has elapsed. The three states are not
interchangeable: `pending` is not due, `quoted` had a price series, `unquoted` means no first price
arrived inside the decision's policy window.

OFF THE HOT PATH, ALWAYS. It runs from cron under flock against the pinned release, reads the
funded ledger through `get_readonly_connection()`, and writes one table. It never opens a row in
`memecoin_positions`, never signs anything, and never imports a signing seam.

WHY A MINUTE MATTERS AND IS STILL HONEST. Gate inputs are decision-time quantities and this reads
them late, so every row carries `eval_lag_s` and no readout may quote a gate result without it.
The chase families need the price at the decision instant, which nobody fetched; they are computed
from the late quote, stored as evidence, and recorded `unknown_lagged` so they can never admit or
refuse. Admission here is therefore LOOSER than funded, and every read must say so.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files                          # noqa: E402

from sniper.util.state_paths import state_root, state_path       # noqa: E402

load_env_files(str(state_root()))

import sniper.settings.memecoin as cfg                                   # noqa: E402
from sniper.persistence import db_memecoin as m                          # noqa: E402
from sniper.persistence.db_core import get_connection, get_readonly_connection  # noqa: E402
from sniper.services import memecoin_promotion_lane as lane              # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("promotion_lane")

BATCH = 40


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _epoch(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def pending_attempts(conn, *, limit=BATCH):
    """Fresh not_approved attempts without a Lane A row, oldest first.

    The unique attempt ID is the progress marker. A timestamp cursor loses an evaluation failure
    before the latest success and loses rows tied at a full page's last timestamp.
    """
    # ⚠️ AN OLD SIGNAL CANNOT BE GATED HONESTLY. The gate inputs are decision-time quantities and
    # the market read is taken NOW, so an attempt from hours ago would be scored against a market
    # that has moved. The age floor keeps old history outside every pass; only fresh unrecorded
    # attempts can enter the denominator.
    max_age = float(getattr(cfg, "MEMECOIN_PROMOTION_LANE_MAX_AGE_S", 900.0) or 900.0)
    floor_iso = (_now() - dt.timedelta(seconds=max_age)).isoformat()
    has_lane = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_promotion_lane'"
    ).fetchone()
    unseen = ("AND NOT EXISTS (SELECT 1 FROM memecoin_promotion_lane l "
              "WHERE l.attempt_id=a.attempt_id)" if has_lane else "")
    return [dict(r) for r in conn.execute(
        "SELECT a.attempt_id, a.origin_event_id, a.network_id, a.mint, a.ticker, a.source_user_id, "
        "       a.source_handle, a.decision_started_at, a.source_price, a.source_market_cap, "
        "       a.chase_source_size_usd, a.classification_json, a.terminal_reason "
        "  FROM memecoin_entry_attempts a "
        " WHERE a.terminal_status='refused' AND a.terminal_reason LIKE '%not_approved%' "
        f" AND a.decision_started_at>=? {unseen} "
        " ORDER BY a.decision_started_at, a.attempt_id LIMIT ?",
        (floor_iso, int(limit)))]


def market_read(mint, network_id):
    """One DexScreener read through the funded path's own parser. None when it says nothing."""
    from sniper.execution.solana_quote import dexscreener_stats
    try:
        return dexscreener_stats(mint, network_id=network_id)
    except Exception:
        logger.exception("promotion lane: market read failed for %s/%s", network_id, mint)
        return None


def exit_series(conn, mint, network_id, since_ts, until_ts):
    """The 1-minute price series for this mint from `memecoin_quote_ledger`.

    ⚠️ ONE MINUTE IS COARSER THAN THE FUNDED RAIL, which sweeps in seconds. A take-profit or a stop
    crossed and retraced inside a minute is invisible here, so this UNDER-triggers both bounds and
    the modelled hold is therefore a conservative one, not a faithful replay of the rail.

    ⚠️ AN EMPTY RESULT IS A FACT ABOUT US, NOT ABOUT THE COIN. The ledger is written by the scanner
    sweep over the mints it already prices, so it covers coins we took and little else: over 895
    `not_approved` attempts in the 24 h to 2026-09-23, 178 of the 179 covered mints were mints we
    had opened a position on, against 1 of 113 we never held. Lane A admits exactly the coins we did
    not take, so this series is empty for most of them and `fill_basis='unquoted'` is the honest
    answer, never a zero and never a fill.

    ⚠️ A NULL CHAIN IS UNKNOWN, NOT A WILDCARD (Codex review R2). The ledger carries NULL on
    legacy bare-mint rows, and admitting them for every requested chain let a NULL-chain price
    enter a Robinhood series. A row that cannot say which chain it priced cannot price this one.
    """
    rows = conn.execute(
        "SELECT ts, price FROM memecoin_quote_ledger "
        " WHERE mint=? AND ts>=? AND ts<=? AND network_id=? ORDER BY ts",
        (str(mint), str(since_ts), str(until_ts), str(network_id))).fetchall()
    out = []
    for row in rows:
        stamp, price = _epoch(row[0]), row[1]
        if stamp is not None and price and price > 0:
            out.append((stamp, float(price)))
    return out


def evaluate_one(attempt, *, ro_conn, now, cfg_module=cfg):
    """One attempt -> one lane row. Returns the row dict, never raises for data reasons."""
    decision_ts = _epoch(attempt["decision_started_at"])
    evaluated = now.isoformat()
    row = {
        "attempt_id": attempt["attempt_id"], "origin_event_id": attempt.get("origin_event_id"),
        "network_id": str(attempt["network_id"]), "mint": attempt["mint"],
        "ticker": attempt.get("ticker"), "source_user_id": attempt.get("source_user_id"),
        "source_handle": attempt.get("source_handle"), "lane_version": lane.LANE_VERSION,
        "decision_started_at": attempt["decision_started_at"], "evaluated_at": evaluated,
        "eval_lag_s": (None if decision_ts is None else now.timestamp() - decision_ts),
        "created_at": evaluated,
    }
    try:
        facts = (json.loads(attempt.get("classification_json") or "{}") or {}).get("facts") or {}
    except (TypeError, ValueError):
        facts = {}
    row["episode_id"] = facts.get("episode_id")

    market = market_read(attempt["mint"], attempt["network_id"])
    if not market:
        row.update(verdict="unreadable", refused_family=None,
                   refused_reason="market read unavailable",
                   market_data_source="dexscreener")
        return row
    row["market_data_source"] = "dexscreener"

    source_size = attempt.get("chase_source_size_usd") or facts.get("source_size_usd")
    try:
        states, inputs, refused = lane.evaluate_gates(
            facts=facts, market=market, source_size_usd=source_size,
            network_id=attempt["network_id"], coin_band=facts.get("coin_band"),
            source_user_id=attempt.get("source_user_id"),
            ledger=reentry_state(ro_conn, attempt, cfg_module=cfg_module), cfg=cfg_module,
            source_mcap=source_buy_mcap(attempt, facts),
            prior_net_usd=source_prior_net(ro_conn, attempt, cfg_module=cfg_module,
                                           judged_usd=source_size))
    except lane.Unreadable as exc:
        row.update(verdict="unreadable", refused_reason=str(exc),
                   market_data_source="dexscreener")
        return row

    row["gate_states_json"] = lane.dumps(states)
    row["gate_inputs_json"] = lane.dumps(inputs)
    row["chase_lagged_json"] = lane.dumps(lane.lagged_chase(
        source_price=attempt.get("source_price"), quote_price=market.get("price"), cfg=cfg_module))
    verdict = lane.verdict_of(states, refused)
    row["verdict"] = verdict
    if refused:
        row["refused_family"], row["refused_reason"] = refused[0], str(refused[1])[:200]
    if verdict != "admitted":
        return row

    # Admitted: record the position the funded policy would have held, and PRICE IT LATER.
    # A FIXED CLIP, NOT THE FUNDED SIZER. Funded sizing is bankroll- and band-dependent and changes
    # under us; a promotion read wants one constant so a wallet's rows are comparable across weeks.
    #
    # ⚠️ THE FILL IS NOT READ HERE. This runs `eval_lag_s` after the decision -- 5 s to 840 s on the
    # first supervised pass -- while the modelled hold runs `max_hold_s` (14,400 s) beyond it, so at
    # this moment the whole exit window is still in the future. The first pass proved it: four
    # admitted rows, four empty series, and one of them (JIZZCOIN, 2026-09-23T15:37:18Z) had six
    # ledger rows minutes later. `settle_pending` prices the row once its horizon has elapsed.
    clip = float(getattr(cfg_module, "MEMECOIN_PROMOTION_LANE_CLIP_USD", 100.0) or 100.0)
    policy = frozen_policy(facts.get("coin_band"))
    max_hold = float(getattr(policy, "max_hold_s", 0) or 0) or DEFAULT_HORIZON_S
    row.update(clip_usd=clip, policy_id=getattr(policy, "policy_id", None),
               policy_json=lane.dumps(getattr(policy, "__dict__", {})),
               horizon_target_s=max_hold, price_at_signal=attempt.get("source_price"),
               fill_basis=PENDING)
    return row


PENDING, QUOTED, UNQUOTED = "pending", "quoted", "unquoted"
# The first reading at or after a deadline may still close the hold. 180 s matches the observation
# tolerance the refusal-family report uses for the same question.
ENDPOINT_TOLERANCE_S = 180.0
# What a modelled PnL on this lane IS: a ratio of BUY quotes, never a realizable exit.
PNL_BASIS = "buy_mark_model"
UNPRICED_SOURCE = "unpriced_by_any"
DEFAULT_HORIZON_S = 14400.0
SETTLE_BATCH = 200


def price_series(ro_conn, row, until_iso):
    """The price path for one admitted row, and the name of the instrument that produced it.

    ⚠️ ONE SOURCE PER ROW, NEVER A UNION. The ledger carries USD per whole token and a lane sample
    carries USD per ATOMIC token, because a lane sample cannot read the mint's decimals unattended.
    Concatenating them would put two scales in one series and produce a PnL that is pure unit error.
    ⚠️ THE INSTRUMENT THAT ESTABLISHED THE FILL OWNS THE ROW (Codex review N6). Preferring the
    ledger "where it has anything" let ONE late scanner mark arrive hours after the sampler had
    already set a fill, take over the row, and change both the fill price and the outcome — the
    measuring instrument moving after the experiment started. The fill is the first bar, so the
    series whose FIRST bar is earliest is the one that established it, and a later arrival cannot
    move that. On a tie the ledger wins, because it is denser.

    A late mark from the other instrument is still diagnostic; it is simply not this row's series.
    """
    ledger = exit_series(ro_conn, row["mint"], row["network_id"], row["decision_started_at"],
                         until_iso)
    # The reader returns failures too, so the replay can stop at them; a failure has no bar.
    samples = [(_epoch(s["sampled_at"]), float(s["price"]))
               for s in m.promotion_lane_samples(ro_conn, row["attempt_id"]) if s.get("price")]
    decision_ts, until_ts = _epoch(row["decision_started_at"]), _epoch(until_iso)
    samples = [(ts, price) for ts, price in samples
               if ts is not None and decision_ts is not None and until_ts is not None
               and decision_ts <= ts <= until_ts and price > 0]
    frozen_source = row.get("exit_series_source")
    frozen_price = _f(row.get("fill_price"))
    frozen_lag = _f(row.get("fill_lag_s"), allow_zero=True)
    if frozen_source in ("memecoin_quote_ledger", SAMPLE_BASIS) and row.get("fill_price") is not None:
        if (frozen_price is None or frozen_lag is None
                or decision_ts is None or until_ts is None):
            return [], frozen_source
        fill_ts = decision_ts + frozen_lag
        if fill_ts > until_ts:
            return [], frozen_source
        selected = ledger if frozen_source == "memecoin_quote_ledger" else samples
        return [(fill_ts, frozen_price)] + [bar for bar in selected if bar[0] > fill_ts], frozen_source
    if ledger and samples:
        return ((ledger, "memecoin_quote_ledger") if ledger[0][0] <= samples[0][0]
                else (samples, SAMPLE_BASIS))
    if ledger:
        return ledger, "memecoin_quote_ledger"
    # Both instruments were asked and neither answered. Naming the ledger here would read as "the
    # ledger priced this row", which is the opposite of what happened.
    return samples, (SAMPLE_BASIS if samples else UNPRICED_SOURCE)


def settle_fields(row, *, ro_conn, now, cfg_module=cfg):
    """Price one admitted row from the ledger, now that its horizon has passed.

    Returns the settle fields, or None when the row is not due yet. `fill_basis` says which of the
    three things happened and they are NOT interchangeable: `quoted` means a price series existed,
    `unquoted` means no first price arrived inside the decision's policy window. The retired value `none`
    read as "no fill step ran" when it meant "no quote existed", which is the distinction the whole
    promotion read turns on.

    Due at `fill + horizon_target_s`, never at `decision + horizon_target_s`: see the note below.
    """
    from sniper.services import memecoin_refusal_shadow as shadow

    decision_ts = _epoch(row["decision_started_at"])
    if decision_ts is None:
        return None
    horizon = _f(row.get("horizon_target_s")) or _policy_horizon(row.get("policy_json"))

    # ⚠️ THE DEADLINE IS MEASURED FROM THE MODELLED FILL, NOT FROM THE DECISION. The fill is the
    # first quote at or after the decision and it lands later -- 44 s on the first admitted row that
    # had any tape, and a whole cron minute for a lane sample. Anchoring the horizon on the decision
    # censors every row early by exactly its own fill lag, which hides a take-profit crossed in the
    # last minutes of the policy's real hold AND reports `censored_by='policy'` for a policy that
    # had not finished. So the series is read up to NOW, the first bar sets the fill, and the row
    # stays pending until `fill + horizon`.
    everything, source = price_series(ro_conn, row, now.isoformat())
    if not everything or everything[0][0] >= decision_ts + horizon:
        if now.timestamp() < decision_ts + horizon:
            return None      # no quote yet, and the hold has not run out: still pending
        # Nothing quoted this mint inside the policy's own hold. A fill that never appeared is a
        # fill the funded path would not have taken either, so this settles rather than waiting.
        return {"settled_at": now.isoformat(), "exit_series_n": 0,
                "exit_series_source": UNPRICED_SOURCE,
                "fill_basis": UNQUOTED, "censored_by": "tape", "horizon_s": 0.0}
    fill_epoch = everything[0][0]
    if now.timestamp() < fill_epoch + horizon:
        return None
    # ⚠️ AN ENDPOINT DOES NOT HAVE TO LAND ON THE DEADLINE SECOND (Codex review R1). Dropping every
    # bar past `fill + horizon` threw away a healthy reading three seconds late and settled the row
    # `open_at_horizon` with an unknown PnL, for a timecap that had plainly been reached. The first
    # reading at or after the deadline closes the hold when it arrives inside ENDPOINT_TOLERANCE_S,
    # and the delay is reported rather than hidden. Beyond that tolerance it is a different fact
    # and the row stays censored: no gap is ever filled with an old quote.
    deadline = fill_epoch + horizon
    series = [bar for bar in everything if bar[0] <= deadline]
    endpoint_delay = None
    beyond = [bar for bar in everything if bar[0] > deadline]
    if beyond and beyond[0][0] - deadline <= ENDPOINT_TOLERANCE_S:
        series = series + [beyond[0]]
        endpoint_delay = beyond[0][0] - deadline
    elif now.timestamp() <= deadline + ENDPOINT_TOLERANCE_S:
        return None
    # The replay stops at the first quote that failed: after it the tape is unobserved, and a
    # carried price cannot supply a second confirmation.
    gap_at = (_first_failed_sample_epoch(ro_conn, row["attempt_id"], fill_epoch)
              if source == SAMPLE_BASIS else None)
    if gap_at is not None:
        series = [bar for bar in series if bar[0] < gap_at] or series[:1]
        endpoint_delay = None
    settled = {"settled_at": now.isoformat(), "exit_series_n": len(series),
               "exit_series_source": source, "fill_lag_s": fill_epoch - decision_ts,
               "endpoint_delay_s": endpoint_delay}

    clip = _f(row.get("clip_usd")) or float(
        getattr(cfg_module, "MEMECOIN_PROMOTION_LANE_CLIP_USD", 100.0) or 100.0)
    policy = _rehydrate_policy(row.get("policy_json"))
    fill_price = series[0][1]
    covered = series[-1][0] - series[0][0]
    bars = [(ts, price, None, None) for ts, price in series]
    # ⚠️ EVERY PRICE HERE IS A BUY QUOTE (Codex review N8). A lane sample asks what the same dollar
    # clip BUYS now, so a ratio of two of them is a buy-price mark, not proceeds from selling the
    # quantity the first one acquired. On an unchanged 1,000/1,000 constant-product pool with a
    # $100 clip the buy-ratio return is 0.0% while a reverse quote for the 90.909091 tokens
    # acquired returns $83.333333, or -16.67%: atomic scaling does not cancel spread,
    # amount-dependent impact, transfer tax or sell unavailability. Measuring the executable side
    # needs quantity-matched SELL quotes, which this lane does not take.
    settled.update(fill_basis=QUOTED, fill_price=fill_price, fill_impact_pct=None,
                   pnl_basis=PNL_BASIS, horizon_s=min(horizon, covered),
                   censored_by="policy" if covered >= horizon else "tape")
    exit_price, exit_reason, after = shadow.simulate_exit(
        bars, fill_epoch=series[0][0], fill_price=fill_price, policy=policy,
        sweep_s=60.0, horizon_s=settled["horizon_s"], require_new_bar=True)
    if exit_reason == "open_at_horizon" and endpoint_delay is not None:
        exit_price, exit_reason, after = series[-1][1], "max_hold", series[-1][0] - fill_epoch
    if exit_reason in ("open_at_horizon", "no_fill"):
        exit_price = None
        if gap_at is not None:
            # The policy did not finish, and the reason is OUR outage, not a quiet market.
            settled["censored_by"] = "sample_gap"
            settled["gap_after_s"] = gap_at - fill_epoch
    else:
        # An exit that fired BEFORE the tape went dark is a real observation and stands.
        settled["pnl_usd"], settled["pnl_pct"] = shadow.pnl(fill_price, exit_price, clip)
        settled["censored_by"] = None
    settled.update(exit_price=exit_price, exit_reason=exit_reason, exit_after_s=after,
                   horizons_json=shadow.dumps_horizons(
                       shadow.horizons(bars, fill_epoch=series[0][0])))
    return settled


def _f(value, *, allow_zero=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and (number >= 0 if allow_zero else number > 0) else None


def _policy_horizon(policy_json):
    """The hold this row was written under, from its own stored policy. Never a fresh read: the
    live timecap may have moved since, and a row must settle on the policy it was admitted with."""
    try:
        return _f((json.loads(policy_json or "{}") or {}).get("max_hold_s")) or DEFAULT_HORIZON_S
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_S


def _rehydrate_policy(policy_json):
    """The frozen policy object the row carries, as a namespace `simulate_exit` can read.

    The stored dict is the policy AS ADMITTED. Rebuilding it from the live band map instead would
    price an old row under today's thresholds -- the `follow-30-50-v1` trap, where a name minted at
    +30/-50 runs at +25/-35.
    """
    import types

    try:
        fields = json.loads(policy_json or "{}") or {}
    except (TypeError, ValueError):
        fields = {}
    if not fields:
        return frozen_policy(None)
    return types.SimpleNamespace(**fields)


SAMPLE_BASIS = "lane_quote"
SAMPLE_UNIT_SOL = "usd_per_atomic_token"
SAMPLE_SOURCE_SOL = "jupiter_paper"


def open_real_solana_mints(conn):
    """Mints carrying an OPEN funded Solana position right now.

    The sampler skips these. The paper Jupiter key is a different key from the funded one, but a
    lane quote on a mint we are holding is still a second reader on the same route at the same
    moment, and the exit rail's quote is the one that must not queue behind anything.
    """
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT mint FROM memecoin_positions "
            " WHERE is_open=1 AND execution_mode='real' AND network_id='solana'")}
    except Exception:
        logger.exception("promotion lane: open-position read failed; sampling nothing this pass")
        return None


def sample_one(row, *, now, cfg_module=cfg):
    """One exact quote for one admitted mint. Returns the sample dict, or None for a non-Solana row.

    ⚠️ THE PRICE IS USD PER ATOMIC TOKEN, NOT PER TOKEN. Converting needs the mint's decimals, which
    is a Solana RPC read this worker may not make unattended, so the scale is left alone and named
    in `price_unit`. Ratios within this source are comparable buy marks, not executable sell PnL;
    the settle pass must never concatenate these with `memecoin_quote_ledger` prices.

    A provider failure is recorded WITH ITS REASON and a NULL price. A quote that did not happen is
    not a price of zero.

    ⚠️ `sampled_at` IS WHEN THE ANSWER CAME BACK, `scheduled_at` IS WHEN THE PASS ASKED. They used
    to be one number taken from the pass clock BEFORE the request, and the requests are sequential,
    so every row after the first in a pass was stamped early by the sum of the preceding round
    trips — with its own `rtt_ms` recorded right beside the wrong stamp. A failure gets an
    observation stamp too: the request happened and we saw it fail.
    """
    import time

    from sniper.services import memecoin_quotes as quotes

    clip = _f(row.get("clip_usd")) or float(
        getattr(cfg_module, "MEMECOIN_PROMOTION_LANE_CLIP_USD", 100.0) or 100.0)
    sample = {"attempt_id": row["attempt_id"], "network_id": str(row["network_id"]),
              "mint": row["mint"], "scheduled_at": now.isoformat(), "quote_usd": clip,
              "price_basis": SAMPLE_BASIS, "price_unit": SAMPLE_UNIT_SOL,
              "source": SAMPLE_SOURCE_SOL}
    started = time.monotonic()
    try:
        payload, reason, _host = quotes.jupiter_entry_quote_detailed(row["mint"], clip)
    except Exception as exc:
        sample.update(sampled_at=_now().isoformat(),
                      reason="exception: %s" % type(exc).__name__,
                      rtt_ms=(time.monotonic() - started) * 1000.0)
        return sample
    sample["sampled_at"] = _now().isoformat()
    sample["rtt_ms"] = (time.monotonic() - started) * 1000.0
    out_atomic = None
    if isinstance(payload, dict):
        try:
            out_atomic = int(payload.get("outAmount"))
        except (TypeError, ValueError):
            out_atomic = None
    if not out_atomic or out_atomic <= 0:
        sample["reason"] = str(reason or "no out amount")
        return sample
    sample.update(out_atomic=str(out_atomic), price=clip / float(out_atomic), reason=None)
    return sample


def _first_failed_sample_epoch(ro_conn, attempt_id, after_epoch):
    """When this row's tape first went dark at or after `after_epoch`, or None.

    ⚠️ A REPLAY MUST NOT CARRY A PRICE ACROSS AN OUTAGE (Codex review N5). `bar_at_or_before`
    returns the newest bar at or before each sweep, so a quote that failed leaves the previous
    healthy price standing and a two-sweep stop confirms twice on ONE observation. The failure
    rows are already written with their reason and a NULL price; the replay stops at the first of
    them and the row is censored there, because a distinct confirmation needs a distinct quote.
    """
    for sample in m.promotion_lane_samples(ro_conn, attempt_id):
        stamp = _epoch(sample["sampled_at"])
        if stamp is None or stamp < after_epoch or sample.get("price"):
            continue
        return stamp
    return None


def sample_pending(ro_conn, conn, *, now, cfg_module=cfg):
    """One exact quote per admitted mint still inside its horizon. Returns the pass counters.

    ⚠️ THE CAP MAKES `sampled` A FUNCTION OF OUR OWN QUEUE. Any coverage share read off this table
    must carry `capped` beside it, or it describes how many rows fitted under the cap rather than
    how many coins could be priced.

    ⚠️ THE BUDGET IS CHARGED BEFORE THE REQUEST, NOT AFTER A SUCCESS (Codex review N4). Counting
    only priced samples meant an outage evaded the cap entirely: 25 pending rows under a cap of 2
    issued 25 requests and recorded `capped=0`, because a rate-limited answer never incremented
    the counter it was supposed to be bounded by. `attempted` is the number this cap now governs;
    `sampled` and `errors` split it by outcome.
    """
    limit = int(getattr(cfg_module, "MEMECOIN_PROMOTION_LANE_SAMPLE_MAX", 20) or 20)
    attempted = sampled = skipped = capped = errors = skipped_held = 0
    held = open_real_solana_mints(ro_conn)
    if held is None:          # the read failed; a sample while blind could land on a held mint
        return {"attempted": 0, "sampled": 0, "skipped": 0, "capped": 0, "errors": 1,
                "skipped_held": 0}
    # ⚠️ A ROW WITH NO PRICE YET GOES FIRST, ALWAYS. The fill IS the first priced sample, and a
    # horizon runs for hours, so oldest-first alone lets a backlog of long-lived rows hold the whole
    # cap and starve every newly admitted one -- which would not lose a price, it would move the
    # fill hours past the decision and quietly change what the row measures.
    #
    # THE ORDER IS THE SELECTOR'S NOW (Codex review N7). Re-sorting here could only reorder rows
    # that had already survived the selector's LIMIT, so a new row outside the oldest 200 never
    # reached this sort at all; and the old key counted any sample row, so a row whose every quote
    # had failed looked served.
    candidates = m.promotion_lane_rows_open_for_sampling(ro_conn, network_id="solana")
    for row in candidates:
        decision_ts = _epoch(row["decision_started_at"])
        horizon = _f(row.get("horizon_target_s")) or _policy_horizon(row.get("policy_json"))
        if decision_ts is None:
            continue
        # ⚠️ THE HOLD RUNS FROM THE FILL, THE FIRST PRICE IN THE SELECTED SERIES (Codex review
        # R1). Stopping at `decision + horizon` while settlement waits for `fill + horizon` cost
        # the row exactly its own fill lag of tape, every time — a whole cron minute for a lane
        # sample — and settlement then reported it open at a horizon it had not been given the
        # evidence to reach. The extra read below only happens for rows already past the
        # decision-based bound, so the common case costs nothing.
        if now.timestamp() >= decision_ts + horizon:
            series, _source = price_series(ro_conn, row, now.isoformat())
            fill_ts = series[0][0] if series else None
            if (fill_ts is None or fill_ts >= decision_ts + horizon
                    or now.timestamp() > fill_ts + horizon + ENDPOINT_TOLERANCE_S):
                continue      # past endpoint tolerance: settle's business
        if str(row["network_id"]) != "solana":
            skipped += 1      # 4663/8453 are priced from the pool tape, as now
            continue
        if row["mint"] in held:
            skipped_held += 1
            continue
        if attempted >= limit:
            capped += 1
            continue
        attempted += 1
        sample = sample_one(row, now=now, cfg_module=cfg_module)
        if sample is None:
            attempted -= 1    # nothing was asked of the provider
            continue
        m.record_promotion_lane_sample(conn, sample)
        if sample.get("price"):
            sampled += 1
            series, source = price_series(conn, row, sample["sampled_at"])
            if series:
                m.claim_promotion_lane_fill(
                    conn, row["attempt_id"], source=source, price=series[0][1],
                    lag_s=series[0][0] - decision_ts)
        else:
            errors += 1
    return {"attempted": attempted, "sampled": sampled, "skipped": skipped, "capped": capped,
            "errors": errors, "skipped_held": skipped_held}


def settle_pending(ro_conn, conn, *, now, limit=SETTLE_BATCH):
    """Price every admitted row whose horizon has elapsed. Returns (settled, not_due, skipped)."""
    settled = not_due = skipped = 0
    for row in m.promotion_lane_rows_unsettled(ro_conn, limit=limit):
        try:
            fields = settle_fields(row, ro_conn=ro_conn, now=now)
        except Exception:
            logger.exception("promotion lane: settle read failed for %s", row["attempt_id"])
            skipped += 1
            continue
        if fields is None:
            not_due += 1
            decision_ts = _epoch(row["decision_started_at"])
            series, source = price_series(ro_conn, row, now.isoformat())
            if series and decision_ts is not None:
                m.claim_promotion_lane_fill(
                    conn, row["attempt_id"], source=source, price=series[0][1],
                    lag_s=series[0][0] - decision_ts)
            continue
        settled += 1 if m.settle_promotion_lane_row(conn, row["attempt_id"], fields) else 0
    return settled, not_due, skipped


def source_buy_mcap(attempt, facts):
    """The mcap the funded source-buy leg judges against: the CLASSIFICATION's own, then the
    source's reported cap. Never the late market read -- that is a different number taken at a
    different time, and using it here would compare the source's buy to a cap that did not exist
    when they made it. Absent stays absent, which routes to the unknown-mcap floor exactly as
    funded does.
    """
    for candidate in (facts.get("effective_mcap"), attempt.get("source_market_cap")):
        value = _f(candidate)
        if value is not None:
            return value
    return None


def source_event_anchor(conn, attempt):
    """(iso, basis) for the SOURCE EVENT this attempt copied, or (None, "unknown").

    ⚠️ THE ANCHOR IS THE EVENT, NOT OUR DECISION (Codex review R4). Funded reads the cumulative
    source buys before `ev.created_at`; this lane read them before its own `decision_started_at`,
    which is `eval_lag_s` later. A second buy landing in between then counted as PRIOR evidence
    here and did not exist for funded, so the same evaluator admitted what funded refused.
    Sharing a function does not make different input populations equivalent.

    Two exact sources, in order, and neither is the decision time:
      attempt_row   the attempt's own `source_created_at`, written when the signal arrived
      origin_event  `memecoin_events.created_at` for this attempt's `origin_event_id`

    A same-wallet, same-coin event near the decision may be a different buy. Missing exact
    identity remains unknown rather than borrowing that event's time.
    """
    stamp = attempt.get("source_created_at")
    if stamp:
        return str(stamp), "attempt_row"
    origin = attempt.get("origin_event_id")
    try:
        if origin:
            row = conn.execute(
                "SELECT created_at FROM memecoin_events WHERE fomo_event_id=? LIMIT 1",
                (str(origin),)).fetchone()
            if row and row[0]:
                return str(row[0]), "origin_event"
    except Exception:
        logger.exception("promotion lane: source anchor read failed for %s",
                         attempt.get("attempt_id"))
    return None, "unknown"


def source_prior_net(conn, attempt, *, cfg_module=cfg, judged_usd=None):
    """The trader's earlier net into this coin BEFORE THE SOURCE EVENT, the funded cumulative leg.

    An unreadable history returns None and the family judges the single buy, which is what the
    funded path does. An anchor we cannot establish returns None for the same reason: falling back
    to the decision time would quietly restore the defect this fixes.
    """
    window = float(getattr(cfg_module, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0) or 0)
    if window <= 0:
        return None
    anchor, _basis = source_event_anchor(conn, attempt)
    if not anchor:
        return None
    try:
        return m.source_prior_net_buy_usd(
            conn, source_user_id=attempt.get("source_user_id"),
            network_id=attempt.get("network_id"), mint=attempt.get("mint"),
            before_iso=anchor, window_s=window,
            exclude_event_id=attempt.get("origin_event_id"), judged_usd=judged_usd)
    except Exception:
        logger.exception("promotion lane: prior-net read failed for %s", attempt["attempt_id"])
        return None


def frozen_policy(coin_band):
    """The funded follow policy for this band, with ITS OWN default identity.

    No versioned policy id is named here: the rename map in `memecoin_funded_policy` is the one
    place that may pin one, and a copy of the string out here is how two contents end up under one
    identity. `tests/test_memecoin_timecap_lift.py` enforces that.
    """
    from sniper.services.memecoin_funded_policy import _follow3050_policy
    return _follow3050_policy(coin_band=str(coin_band or "out_of_scope"))


def reentry_state(conn, attempt, *, cfg_module=cfg):
    """The funded re-entry families, read from the funded path's OWN inputs AS OF the decision.

    ⚠️ THESE ARE COIN RULES, NOT WALLET RULES. The first version of this asked whether every close
    THIS WALLET had on this mint had lost, which is a different question from the one funded asks
    and gives a different answer: funded reads the LATEST real close for the COIN identity
    `(network_id, mint)`, whoever's signal opened it. A +$10 close followed by a -$5 close blocks
    funded and did not block the shadow.

    Every input comes from the funded readers with `before_iso` set to the decision stamp, so a
    position that closed after the decision cannot inform it. Re-implementing the rules here is
    what produced the divergence the 2026-09-23 review found, so nothing is re-implemented:
      * cooldown      -> `m.recently_closed_real_coin_ids`
      * losing        -> `m.losing_real_coin_ids`, only when MEMECOIN_REAL_BLOCK_LOSING_REENTRY
      * winner cap    -> `m.last_real_close_for_coin` + `m.real_campaigns_today_for_coin`, and ONLY
                         when MEMECOIN_REENTRY_WINNER_UNBLOCKED, which is what gates that whole
                         branch at memecoin_real_runner.py:2795.

    A family the live configuration cannot fire is PASS, not UNKNOWN: an inert gate is a gate that
    admitted, and calling it unknown would let a readout count configuration as missing evidence.
    """
    stamp, mint = attempt["decision_started_at"], attempt["mint"]
    network_id = attempt.get("network_id")
    if not stamp or not mint:
        return {}
    coin = m.coin_identity(network_id, mint)
    state = {}
    try:
        cooldown_s = float(getattr(cfg_module, "MEMECOIN_REAL_REENTRY_COOLDOWN_S", 0) or 0)
        cooled = m.recently_closed_real_coin_ids(conn, stamp, cooldown_s, before_iso=stamp)
        state["cooldown_blocked"] = coin in cooled
        state["cooldown_blocked_reason"] = (
            "re-entry cooldown: this coin closed real inside %.0fs" % cooldown_s)
    except Exception:
        logger.exception("promotion lane: cooldown read failed for %s", attempt["attempt_id"])

    try:
        if getattr(cfg_module, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", False):
            losers = m.losing_real_coin_ids(
                conn, since_iso=cfg_module.losing_reentry_since_iso(stamp), before_iso=stamp)
            state["losing_blocked"] = coin in losers
        else:
            state["losing_blocked"] = False
        state["losing_blocked_reason"] = "losing re-entry: this coin's last real close lost"
    except Exception:
        logger.exception("promotion lane: losing read failed for %s", attempt["attempt_id"])

    try:
        if not getattr(cfg_module, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False):
            # The whole branch is unreachable while winners stay cooled, so it cannot refuse.
            # Recorded as INERT rather than only as a pass: `_reason` keys are kept out of
            # `gate_inputs_json` and are read only when a family blocks, so a plain pass would be
            # indistinguishable from a gate that ran and admitted.
            state["winner_cap_blocked"] = False
            state["winner_cap_inert"] = "MEMECOIN_REENTRY_WINNER_UNBLOCKED=0"
        else:
            last = m.last_real_close_for_coin(conn, network_id, mint, before_iso=stamp)
            cap = int(getattr(cfg_module, "MEMECOIN_REENTRY_WINNER_DAILY_CAP", 0) or 0)
            if not (last and last.get("won")) or cap <= 0:
                state["winner_cap_blocked"] = False
            else:
                today = m.real_campaigns_today_for_coin(conn, network_id, mint, stamp,
                                                        before_iso=stamp)
                state["winner_cap_blocked"] = today >= cap
                state["winner_cap_blocked_reason"] = (
                    "winner re-entry daily cap: %d real campaign(s) on this coin today (cap %d)"
                    % (today, cap))
    except Exception:
        logger.exception("promotion lane: winner-cap read failed for %s", attempt["attempt_id"])

    state["coin_identity"] = "%s:%s" % coin
    return state


def run_once():
    if not getattr(cfg, "MEMECOIN_PROMOTION_LANE_ENABLED", False):
        logger.info("MEMECOIN_PROMOTION_LANE_ENABLED is off; nothing to do")
        return 0
    ro = get_readonly_connection(str(state_path("sniper_tracker.db")))
    ro.row_factory = sqlite3.Row
    written = admitted = errors = 0
    with contextlib.closing(ro):
        attempts = pending_attempts(ro)
        if not attempts:
            logger.info("promotion lane: no fresh unrecorded not_approved attempts")
            return 0
        rows = []
        for attempt in attempts:
            try:
                rows.append(evaluate_one(attempt, ro_conn=ro, now=_now()))
            except Exception:
                logger.exception("promotion lane: evaluation failed for %s", attempt["attempt_id"])
                errors += 1
    conn = get_connection(str(state_path("sniper_tracker.db")))
    try:
        m.ensure_promotion_lane_table(conn)
        for row in rows:
            written += 1 if m.record_promotion_lane_row(conn, row) else 0
            admitted += 1 if row.get("verdict") == "admitted" else 0
    finally:
        conn.close()
    logger.info("promotion lane: evaluated=%d written=%d admitted=%d errors=%d",
                len(attempts), written, admitted, errors)
    return written


def run_sample():
    """The sampler pass: one exact quote per admitted mint still inside its horizon."""
    if not getattr(cfg, "MEMECOIN_PROMOTION_LANE_ENABLED", False):
        logger.info("MEMECOIN_PROMOTION_LANE_ENABLED is off; nothing to sample")
        return 0
    ro = get_readonly_connection(str(state_path("sniper_tracker.db")))
    ro.row_factory = sqlite3.Row
    conn = get_connection(str(state_path("sniper_tracker.db")))
    try:
        m.ensure_promotion_lane_table(conn)
        m.ensure_promotion_lane_samples_table(conn)
        with contextlib.closing(ro):
            counts = sample_pending(ro, conn, now=_now())
    finally:
        conn.close()
    logger.info("promotion lane sample: attempted=%(attempted)d sampled=%(sampled)d "
                "errors=%(errors)d skipped=%(skipped)d capped=%(capped)d "
                "skipped_held=%(skipped_held)d", counts)
    return counts["sampled"]


def run_settle():
    """The second pass: price the admitted rows whose horizon has elapsed.

    Separate from the evaluation pass because the two run on different clocks -- a row is evaluated
    seconds after the decision and priced hours after it -- and because a settle must keep working
    when there is nothing new to evaluate.
    """
    if not getattr(cfg, "MEMECOIN_PROMOTION_LANE_ENABLED", False):
        logger.info("MEMECOIN_PROMOTION_LANE_ENABLED is off; nothing to settle")
        return 0
    ro = get_readonly_connection(str(state_path("sniper_tracker.db")))
    ro.row_factory = sqlite3.Row
    conn = get_connection(str(state_path("sniper_tracker.db")))
    try:
        m.ensure_promotion_lane_table(conn)
        m.ensure_promotion_lane_samples_table(conn)
        with contextlib.closing(ro):
            settled, not_due, skipped = settle_pending(ro, conn, now=_now())
    finally:
        conn.close()
    logger.info("promotion lane settle: settled=%d not_due=%d skipped=%d", settled, not_due, skipped)
    return settled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one pass, for cron")
    parser.add_argument("--settle-only", action="store_true",
                        help="price due rows and evaluate nothing")
    parser.add_argument("--sample", action="store_true",
                        help="quote admitted mints still inside their horizon, and nothing else")
    args = parser.parse_args()
    if args.sample:
        return run_sample()
    if not args.settle_only:
        run_once()
    run_sample()
    run_settle()


if __name__ == "__main__":
    main()
