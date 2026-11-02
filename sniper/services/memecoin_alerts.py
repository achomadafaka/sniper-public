"""Item 20 — real-money operational alerts. Arming without these is flying blind.

Thin formatting + wiring over the bounded Telegram dispatcher. Every default send is queued
without blocking the trade/exit loop; queue saturation fails soft. ``send_fn`` is injectable for
tests. These fire for real-money events regardless of the front-run latency instrument toggle
(``MEMECOIN_TG_ALERTS``) — that flag gates the latency experiment, not operational safety alerts.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading as _threading

from sniper.settings import memecoin as cfg

logger = logging.getLogger(__name__)


def _default_send(text, *, kind, source_id=None, on_delivered=None):
    from sniper.services.memecoin_telegram import enqueue_telegram
    if on_delivered is None:
        return enqueue_telegram(text, kind=f"operational:{kind}", source_id=source_id), 0.0
    return enqueue_telegram(text, kind=f"operational:{kind}", source_id=source_id,
                            on_result=lambda ok: ok and on_delivered()), 0.0


def send_alert(kind, text, *, send_fn=None, icon="🔴", source_id=None, on_delivered=None):
    """`on_delivered()` runs once the page is DELIVERED: on the dispatcher's thread for the default
    send (queue acceptance is not delivery, Codex F11), right here for an injected send_fn whose
    answer is the delivery itself. Never for a page that failed."""
    message = f"{icon} memecoin {kind} | {text}"
    try:
        if send_fn is None:
            return _default_send(message, kind=kind, source_id=source_id, on_delivered=on_delivered)
        result = send_fn(message)
        if on_delivered is not None and _page_sent(result):
            on_delivered()
        return result
    except Exception:
        # KEEP the swallow. handle_auth_page is called at memecoin_runner_loop.py:1681 OUTSIDE
        # every try, so this is the only thing between a Telegram failure and an exception
        # escaping the main `while True`. Letting it raise would turn a Telegram hiccup into a
        # runner death during an auth outage -- precisely when the Telegram path is most stressed
        # and the runner is most needed, because exits run throughout by design.
        # But do not swallow SILENTLY: if enqueue_telegram raises, the dispatcher never runs, so
        # _persist_delivery_audit never fires either and the failure leaves no trace anywhere.
        logger.exception("alert %s could not be sent", kind)
        return (False, 0.0)


def _sig8(sig):
    return (sig or "")[:8]


def _known_name(mint):
    """The coin's name if this process already resolved it (memecoin_coin_names, no I/O: these
    alerts format on the trading thread)."""
    try:
        from sniper.services.memecoin_coin_names import cached_for_mint
        return cached_for_mint(mint)
    except Exception:
        return None


def _coin(mint):
    """'NAME mint' when the name is known, else the bare mint."""
    name = _known_name(mint)
    return f"{name} {mint}" if name else mint


def _who(mint, handle=None, ticker=None):
    """'PEPE (zinceth) mint…' when known — the phone-readable form — else the bare mint."""
    ticker = ticker or _known_name(mint)
    label = f"{ticker or '?'} ({handle or '?'}) {(mint or '')[:12]}…" if (handle or ticker) else mint
    return label


def _money(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(value) or value < 0:
        return "unknown"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:.0f}"


def _entry_tx_url(sig, network_id):
    from sniper.services.memecoin_links import operator_links
    return operator_links(network_id, tx_sig=sig).get("transaction")


def _entry_price(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"{value:.6g}" if math.isfinite(value) and value > 0 else "?"


def alert_entry(mint, fill_price, notional, sig, *, handle=None, ticker=None,
                source_user_id=None, source_size_usd=None, source_price=None,
                entry_lag_s=None, policy=None, network_id=None, source_id=None,
                send_fn=None):
    source = handle or source_user_id
    lines = [f"{_who(mint, source, ticker)} ${notional:.2f} @ {_entry_price(fill_price)}"]
    if source:
        line = f"source: {source}"
        if source_size_usd is not None or source_price is not None:
            line += f" bought {_money(source_size_usd)} @ {_entry_price(source_price)}"
        lines.append(line)
    try:
        lag = float(entry_lag_s)
    except (TypeError, ValueError):
        lag = None
    if lag is not None and math.isfinite(lag) and lag >= 0:
        lines.append(f"feed lag: {lag:.1f}s")
    try:
        drift = (float(fill_price) / float(source_price) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        drift = None
    fill_line = f"our fill: ${notional:.2f} @ {_entry_price(fill_price)}"
    if drift is not None and math.isfinite(drift):
        fill_line += f" ({drift:+.1f}%)"
    lines.append(fill_line)
    if policy or network_id:
        lines.append(f"policy: {policy or '?'} | chain: {network_id or '?'}")
    if tx_url := _entry_tx_url(sig, network_id):
        lines.append(f"tx: {tx_url}")
    else:
        lines.append(f"sig: {_sig8(sig)}")
    return send_alert("REAL-FILLED", "\n".join(lines), send_fn=send_fn, source_id=source_id)


def _emit(kind, text, *, send_fn=None):
    """Routine POLICY DECLINES go quiet; FAULT-shaped refusals always send (2026-08-06, operator).

    The split is 'did policy say no' vs 'is something broken'. A band-floor/ceiling/consensus/stale
    refusal is the system working — it is a decision, and at ~21/day it was the bulk of the phone
    noise. Token-safety, route, slippage, network-drop and disarm refusals are NOT in the silent
    set and never should be: those mean a sell route is missing, a sell simulation would not verify,
    or the runner is disarmed — conditions the operator has to act on. Silencing is config-only
    (MEMECOIN_ALERT_SILENT_REFUSALS=""), changes no funded decision, and the refusal is still
    written to memecoin_entry_attempts.terminal_reason either way, so the audit trail is intact."""
    if kind in cfg.MEMECOIN_ALERT_SILENT_REFUSALS:
        return (False, 0.0)
    return send_alert(kind, text, send_fn=send_fn)


def alert_entry_result(mint, result, *, ceiling=None, handle=None, ticker=None, send_fn=None):
    """Name every non-transaction real-entry outcome that the operator must distinguish."""
    result = result or {}
    status = result.get("status")
    who = _who(mint, handle, ticker)
    reason = result.get("reason") or status or "unknown"
    if status == "skipped_mcap":
        # honest reason routing (2026-07-17): the consensus gate reuses this status, and the
        # generic "> ceiling" template rendered a $1.8M solo refusal as "market cap $1.8M >
        # $2.0M ceiling" — a false statement to the operator. Say what actually refused it.
        if "consensus" in str(reason).lower():
            return _emit("CONSENSUS-SKIP", f"{who} {reason}", send_fn=send_fn)
        mcap = result.get("mcap")
        mcap_text = _money(mcap)
        detail = (f"market cap {mcap_text} > {_money(ceiling)} ceiling"
                  if mcap_text != "unknown" and ceiling else "market cap unresolved")
        if str(reason) == "assumed_micro" or "unresolved" in str(reason).lower():
            detail = "market cap unresolved"
        return _emit("MCAP-SKIP", f"{who} {detail}", send_fn=send_fn)
    if status == "aborted":
        kind = "SLIPPAGE-REFUSAL" if any(
            word in str(reason).lower() for word in ("slippage", "impact")
        ) else "ENTRY-REFUSAL"
        return _emit(kind, f"{who} {reason}", send_fn=send_fn)
    if status == "dropped_network":
        return _emit(
            "NETWORK-DROP", f"{who} network={result.get('network') or '?'} not enabled",
            send_fn=send_fn,
        )
    if status == "evm_unarmed":
        return _emit(
            "NETWORK-DROP", f"{who} {result.get('route') or 'EVM route'} has no funded real context",
            send_fn=send_fn,
        )
    if status == "route_failed":
        return _emit("ENTRY-ROUTE-REFUSAL", f"{who} {reason}", send_fn=send_fn)
    if status == "unsafe":
        return _emit("TOKEN-SAFETY-REFUSAL", f"{who} {reason}", send_fn=send_fn)
    if status == "rejected":
        return _emit("RISK-REFUSAL", f"{who} {reason}", send_fn=send_fn)
    if status == "stale":
        return _emit("STALE-SKIP", f"{who} {reason}", send_fn=send_fn)
    if status == "disarmed":
        detail = (result.get("reason") or "real entries disabled or kill switch active")
        return _emit("ENTRY-DISARMED", f"{who} {detail}", send_fn=send_fn)
    return (False, 0.0)


def alert_exit(mint, realized, reason, sig, *, handle=None, ticker=None, entry_notional=None,
               send_fn=None):
    pct = (f" ({realized / entry_notional * 100.0:+.1f}%)"
           if (entry_notional or 0) > 0 else "")
    return send_alert("EXIT", f"{_who(mint, handle, ticker)} {reason} realized=${realized:+.2f}{pct} "
                              f"sig={_sig8(sig)}", send_fn=send_fn)


def alert_stuck_bag(mint, position_id, *, reason=None, send_fn=None):
    detail = f"exit failed ({reason})" if reason else "sell ladder exhausted"
    return send_alert("STUCK-BAG", f"{_coin(mint)} pos#{position_id} {detail} — un-exited, retrying",
                      send_fn=send_fn)


def alert_exit_balance_zero(mint, position_id, *, handle=None, ticker=None, send_fn=None):
    return send_alert("EXIT-DEFERRED", f"{_who(mint, handle, ticker)} pos#{position_id} "
                      "token balance is zero — no sale proven; position stays open for reconciliation",
                      send_fn=send_fn)


def alert_lifi_disable_transitions(previous, current, *, send_fn=None):
    """Send one alert for each LI.FI disable transition."""
    before = frozenset(str(value) for value in (previous or ()))
    after = frozenset(str(value) for value in (current or ()))
    for reason in sorted(after - before):
        send_alert("LIFI-DISABLED", reason, send_fn=send_fn)
    for reason in sorted(before - after):
        send_alert("LIFI-RECOVERED", reason, send_fn=send_fn, icon="🟢")
    return after


def alert_lifi_startup_transition(previous, current, *, baseline_ready, send_fn=None):
    """Suppress transient LI.FI startup states until one recovery attempt completes."""
    if previous is None:
        if not baseline_ready:
            return None
        return alert_lifi_disable_transitions((), current, send_fn=send_fn)
    return alert_lifi_disable_transitions(previous, current, send_fn=send_fn)


def alert_quote_infra(mint, position_id, failure, streak, *, send_fn=None):
    """Compatibility wrapper for callers outside the funded sweep."""
    return send_alert(
        "QUOTE-INFRA",
        f"{_coin(mint)} pos#{position_id} exact-exit provider failure={failure} streak={streak}; "
        "token unsellable counters unchanged",
        send_fn=send_fn,
    )


def alert_rails_blind(mint, position_id, streak, nominal_pnl_pct, *, send_fn=None):
    """A funded position whose LOSS rails have been reading the nominal mid for `streak` sweeps.

    Fires once at the streak threshold, not every sweep: a single missed exact quote is routine
    provider noise and belongs in the log, but a sustained blind window means the stop and
    catastrophe rails cannot see what the bag would fetch. That is a fault, and per the funded
    alerting rule faults are loud while routine policy declines stay off the phone.
    """
    pnl = "unknown" if nominal_pnl_pct is None else f"{float(nominal_pnl_pct):+.1f}%"
    return send_alert(
        "RAILS-BLIND",
        f"{_coin(mint)} pos#{position_id} exact exit price unresolved for {streak} sweeps; "
        f"stop/catastrophe are reading the nominal mid ({pnl}) and cannot see true proceeds",
        send_fn=send_fn,
    )


def quote_infra_episode_events(changes):
    """Reduce per-position persisted streak changes into provider-scoped alert transitions."""
    groups = {}
    for raw in changes:
        provider = str(raw.get("provider") or "unknown")
        groups.setdefault(provider, []).append(raw)
    events = []
    for provider in sorted(groups):
        rows = groups[provider]
        failures = [row for row in rows if int(row.get("after") or 0) > int(row.get("before") or 0)]
        recoveries = [row for row in rows
                      if int(row.get("before") or 0) > 0 and int(row.get("after") or 0) == 0]
        if failures:
            milestones = sorted({int(row["after"]) for row in failures
                                 if int(row["after"]) in {1, 5, 20}})
            if milestones:
                failure = next((row.get("failure") for row in failures if row.get("failure")),
                               "provider_failure")
                boundaries = sorted({str(row["boundary"]) for row in failures
                                     if row.get("boundary")})
                events.append({
                    "kind": "outage", "provider": provider, "failure": str(failure),
                    "affected": len(failures),
                    "max_streak": max(int(row["after"]) for row in failures),
                    "milestone": max(milestones),
                    **({"boundary": boundaries[0] if len(boundaries) == 1 else "mixed"}
                       if boundaries else {}),
                })
            continue
        if recoveries:
            events.append({
                "kind": "recovery", "provider": provider, "affected": len(recoveries),
                "max_streak": max(int(row["before"]) for row in recoveries),
            })
    return tuple(events)


# Flap throttle for the INFRA/RECOVERED pair (2026-08-14). Brand-new coins Jupiter has not indexed
# yet blip the exact-quote provider for one tick at a time, and each blip paged TWICE — 257 of 380
# Telegram sends in one hour were this pair, and Telegram's own rate limit queued every REAL alert
# behind them (avg 16s, worst 95s). One page per provider per window; the milestone-5/20
# escalations still page immediately because a REAL outage advances past milestone 1. Deviation
# from the 1/5/20 spec is deliberate and scoped to milestone 1 + its recovery.
_QUOTE_INFRA_ALERT_WINDOW_S = 300.0
_quote_infra_state = {}     # provider -> {"last_m1": monotonic, "paged": bool}


def _quote_infra_throttled(event):
    import time as _t
    provider = event.get("provider")
    st = _quote_infra_state.setdefault(provider, {"last_m1": None, "paged": False})
    if event.get("kind") == "recovery":
        # A paged episode ALWAYS gets its recovery — never strand an operator on a red alert.
        # Only a fully-suppressed flap suppresses its own recovery.
        paged, st["paged"] = st["paged"], False
        return not paged
    milestone = event.get("milestone")
    if milestone in (1, "1"):
        # Milestone 1 never pages (operator decision 2026-08-14, second pass): a one-tick blip is
        # noise by definition — every runner restart and every unindexed-coin quote produces one.
        # A REAL outage reaches milestone 5 within seconds and pages there; the streak evidence
        # stays in the log either way.
        return True
    st["paged"] = True          # milestone 5/20 page; recovery follows only a paged episode
    return False


def alert_quote_infra_episode(event, *, send_fn=None):
    if _quote_infra_throttled(event):
        return None
    if event.get("kind") == "recovery":
        return send_alert(
            "QUOTE-INFRA-RECOVERED",
            f"provider={event['provider']} recovered affected={event['affected']} "
            f"prior_max_streak={event['max_streak']}; token unsellable counters unchanged",
            send_fn=send_fn, icon="🟢",
        )
    boundary = f"boundary={event['boundary']} " if event.get("boundary") else ""
    return send_alert(
        "QUOTE-INFRA",
        f"provider={event['provider']} {boundary}"
        f"failure={event['failure']} affected={event['affected']} "
        f"max_streak={event['max_streak']} milestone={event['milestone']}; "
        "token unsellable counters unchanged",
        send_fn=send_fn,
    )


# A REJECTED KEY IS NOT A RATE LIMIT, AND IT HAS TO PAGE ON THE FIRST ONE (2026-09-24). The
# operator's new Jupiter key was rejected at 16:01Z; every funded exit quote then fell back to the
# keyless host, whose limit is 0.5 RPS / 30 per minute against the key's 1 RPS / 60, and 119 exact
# exit quotes were lost to rate limiting over 2 h 28 min. The 401 was logged 131 times and paged
# nobody. MILESTONE 1 PAGES because keyed access failed, even if transient. A 403 can be a
# permission/firewall denial with a valid key; it does not establish invalid credentials.
_JUP_AUTH_MILESTONES = (1, 5, 20)
_jup_auth_lock = _threading.Lock()
_jup_auth_state = {}        # (role, host) -> streak, paged, status of last page


def note_jupiter_auth(host, *, ok, status=401, role=None, fallback_host=None, fallback_limit=None,
                      send_fn=None):
    """One keyed-host outcome. Pages at a milestone, once per episode, and on recovery.

    ⚠️ CALLED FROM SEVERAL THREADS -- the main poll loop, the fastlane worker and the entry
    ladder's own pool all quote Jupiter. The streak dict is therefore shared mutable state and
    takes a lock, which `_quote_infra_state` above does NOT (it is driven from one sweep). The lock
    is held around the dict update ONLY and released before the enqueue, so no send can ever be
    made while holding it.

    Never raises into the quote path: the caller is a failover branch that has already decided to
    abandon this host, and an alert must not be able to change that decision.
    """
    key = (str(role or "unknown"), str(host or "unknown"))
    try:
        with _jup_auth_lock:
            state = _jup_auth_state.setdefault(key, {"streak": 0, "paged": False})
            if ok:
                # Recovery is owed only to an episode that actually paged; a rejection that never
                # reached a milestone cannot strand the operator on a red alert.
                streak, paged = state["streak"], state["paged"]
                state["streak"], state["paged"] = 0, False
                event = ("recovery", streak, state.get("status", 401)) if paged else None
            else:
                state["streak"] += 1
                streak = state["streak"]
                if streak in _JUP_AUTH_MILESTONES:
                    state["paged"] = True
                    state["status"] = status
                    event = ("outage", streak, status)
                else:
                    event = None
    except Exception:
        logger.exception("jupiter auth alert state failed for %s", key)
        return None
    if event is None:
        return None
    kind, streak, status = event
    category = "ACCESS" if status == 403 else "KEY"
    if kind == "recovery":
        return send_alert(
            f"JUPITER-{category}-RECOVERED",
            f"role={key[0]} host={key[1]} keyed request succeeded after {streak} rejections",
            send_fn=send_fn, icon="🟢",
        )
    fallback = ""
    if fallback_host:
        fallback = (f" attempting fallback to {fallback_host}"
                    + (f" ({fallback_limit})" if fallback_limit else ""))
    return send_alert(
        "JUPITER-ACCESS-DENIED" if status == 403 else "JUPITER-KEY-REJECTED",
        f"role={key[0]} host={key[1]} HTTP {status} streak={streak};{fallback or ' no fallback named'}"
        + (" — access denied; key may be valid; check permissions, firewall or gateway"
           if status == 403 else " — authentication rejected; verify the configured key"),
        send_fn=send_fn,
    )


# A DARK J7 ACCOUNT PAGES; AN EXPIRING TOKEN PAGES BEFORE IT GOES DARK (2026-09-24). Account 1's
# JWT expired at 14:43:25Z, its socket died at the 14:49:02Z restart, the log collected 1,184
# 'Invalid token' connect_errors, and 54 armed wallets were dark for ~5 h. Nothing paged. Both
# states below are shared across the runner's loop and are updated under one lock, released before
# any enqueue, the same discipline as `note_jupiter_auth`.
_J7_DOWN_AFTER_S = 300.0
_J7_EXPIRY_MILESTONES_S = (24 * 3600.0, 2 * 3600.0, 0.0)     # 24 h, 2 h, and expired
# PAGED ONCE, NOT ONCE PER PROCESS (2026-10-16). The milestones lived in this process only, and a
# runner started inside a window pages the one it is past: ~20 deploys/restarts on 10-15/16 each
# re-sent the 24 h page, 52 J7-TOKEN-EXPIRING pages in 30 h, and the real 2 h and EXPIRED pages were
# lost among them. All three milestones are kept per (account, exp) in the state root and never
# re-page after delivery; an outage still has its independent DOWN/recovery alerts.
_J7_EXPIRY_PAGED_FILE = ("data", "j7_token_expiry_paged.json")
_J7_EXPIRY_PERSISTED = frozenset({24 * 3600.0, 2 * 3600.0, 0.0})
_j7_lock = _threading.Lock()
_j7_account_state = {}       # account_index -> {"down_since": monotonic|None, "paged": bool}
_j7_expiry_state = {}        # "index:exp" (or "index:unknown") -> set of milestones already paged
_j7_expiry_saved = {}        # "index:exp" -> milestones whose page was SENT (the file)
_j7_expiry_loaded = False    # the file is read once per process, on the first call


def _expiry_key(index, exp):
    return "%d:%s" % (int(index), "unknown" if exp is None else "%.0f" % float(exp))


def _load_expiry_paged():
    """{key: persisted milestones} written by earlier runner processes; {} when the file is absent
    or unreadable, which is the old behaviour (the milestone already past pages once)."""
    from sniper.util.state_paths import state_path
    try:
        raw = json.loads(state_path(*_J7_EXPIRY_PAGED_FILE).read_text())
        return {str(k): {float(m) for m in v} & _J7_EXPIRY_PERSISTED for k, v in raw.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("j7 token expiry state unreadable; milestones page as if none were sent")
        return {}


def _save_expiry_paged(snapshot):
    """Write the persisted milestones (atomic replace). A failed write is logged; the page went."""
    from sniper.util.state_paths import state_path
    try:
        path = state_path(*_J7_EXPIRY_PAGED_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({k: sorted(v) for k, v in snapshot.items()}, sort_keys=True))
        os.replace(tmp, path)
    except Exception:
        logger.exception("j7 token expiry state could not be written")


def _j7_accounts(status):
    """[(index, account_status_dict)] for either feed shape: several accounts, or one."""
    if not isinstance(status, dict):
        return []
    accounts = status.get("accounts")
    if isinstance(accounts, list):
        return [(i, a) for i, a in enumerate(accounts, 1) if isinstance(a, dict)]
    return [(1, status)]


def note_j7_accounts(status, *, now_mono, send_fn=None):
    """Page once when an account has not been `connected` for > 5 min, and once on its recovery.

    Any status other than `connected` counts as down -- `starting`, `reconnecting`,
    `connect_error`, `connect_failed`, `auth_error` -- because each of them delivers no signal. A
    flap that reconnects inside the window never pages. The page names the account index, the
    last error string (already redacted of the token by the feed) and the LAST KNOWN tracked
    wallet count, which is cached from before the outage and says so.
    """
    events = []
    try:
        with _j7_lock:
            for index, account in _j7_accounts(status):
                st = _j7_account_state.setdefault(index, {"down_since": None, "paged": False})
                if account.get("status") == "connected":
                    if st["paged"]:
                        events.append(("recovered", index, account, now_mono - st["down_since"]))
                    st["down_since"], st["paged"] = None, False
                    continue
                if st["down_since"] is None:
                    st["down_since"] = now_mono
                elif not st["paged"] and now_mono - st["down_since"] > _J7_DOWN_AFTER_S:
                    st["paged"] = True
                    events.append(("down", index, account, now_mono - st["down_since"]))
    except Exception:
        logger.exception("j7 account alert state failed")
        return []
    sent = []
    for kind, index, account, span in events:
        fomo, pump = account.get("tracked_count"), account.get("pump_tracked_count")
        wallets = "fomo=%s pump=%s (last known)" % (fomo, pump)
        if kind == "down":
            sent.append(send_alert(
                "J7-ACCOUNT-DOWN",
                f"account {index} not connected for {int(span)}s status={account.get('status')} "
                f"last_error={account.get('last_error') or 'none recorded'} tracked {wallets} "
                "-- its wallets deliver no signals until it reconnects",
                send_fn=send_fn))
        else:
            sent.append(send_alert(
                "J7-ACCOUNT-RECOVERED",
                f"account {index} connected again after {int(span)}s tracked {wallets}",
                send_fn=send_fn, icon="🟢"))
    return sent


def note_j7_token_expiry(expiries, *, now_epoch, send_fn=None, auto_refresh_accounts=()):
    """Page at 24 h and at 2 h before each configured token's JWT `exp`, and once when it passes.

    `expiries` is [(account_index, exp_epoch_or_None)] from the feed, which decodes the token
    locally and hands over the NUMBER only; no part of a token reaches this function or a page. An
    unreadable `exp` pages once as "expiry unknown". A runner started inside a window pages the
    milestone it is already past once, then the later ones on time. Automatic accounts renew
    an hour early; warn only if the access deadline still reaches the final 30 minutes.
    """
    global _j7_expiry_loaded
    events = []
    automatic = set(auto_refresh_accounts)
    try:
        with _j7_lock:
            if not _j7_expiry_loaded:
                _j7_expiry_loaded = True
                for key, paged in _load_expiry_paged().items():
                    _j7_expiry_saved.setdefault(key, set()).update(paged)
                    _j7_expiry_state.setdefault(key, set()).update(paged)
            for index, exp in expiries or ():
                if index in automatic and exp is not None and float(exp) - float(now_epoch) > 1800:
                    continue
                key = _expiry_key(index, exp)
                done = _j7_expiry_state.setdefault(key, set())
                if exp is None:
                    if "unknown" not in done:
                        done.add("unknown")
                        events.append((index, None, None, key, set()))
                    continue
                left = float(exp) - float(now_epoch)
                due = [m for m in _J7_EXPIRY_MILESTONES_S if left <= m and m not in done]
                if due:
                    done.update(m for m in _J7_EXPIRY_MILESTONES_S if left <= m)
                    # One page for the tightest milestone; persist all passed ones once SENT.
                    events.append((index, exp, left, key, {m for m in _J7_EXPIRY_PERSISTED if left <= m}))
    except Exception:
        logger.exception("j7 token expiry alert state failed")
        return []
    def recorder(key, marks):
        """Persist `marks` once their page is delivered (dispatcher thread for the real send)."""
        def record():
            snapshot = None
            with _j7_lock:
                if marks and not marks <= _j7_expiry_saved.get(key, set()):
                    _j7_expiry_saved.setdefault(key, set()).update(marks)
                    snapshot = {k: set(v) for k, v in _j7_expiry_saved.items() if v}
            if snapshot is not None:
                _save_expiry_paged(snapshot)
        return record if marks else None

    sent = []
    import datetime as _dt
    for index, exp, left, key, marks in events:
        if exp is None:
            sent.append(send_alert(
                "J7-TOKEN-EXPIRING",
                f"account {index} token expiry unknown (exp claim unreadable) -- check .j7.env",
                send_fn=send_fn))
            continue
        at = _dt.datetime.fromtimestamp(float(exp), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        when = ("EXPIRED" if left <= 0 else "expires in %.1f h" % (left / 3600.0))
        action = ("automatic renewal has not extended the session -- restore the J7 login"
                  if index in automatic else
                  "replace it in .j7.env and restart before the socket goes dark")
        sent.append(send_alert(
            "J7-TOKEN-EXPIRING",
            f"account {index} token {when} (exp {at}) -- {action}",
            send_fn=send_fn, on_delivered=recorder(key, marks)))
    # ONLY A DELIVERED PAGE IS RECORDED (Gamma LOW-1; Codex F11): a failed enqueue or a failed
    # delivery leaves the milestone off the file, so the next runner pages it again.
    return sent


def _page_sent(result):
    """send_alert's answer: (ok, latency) from the Telegram path, or what a test's send_fn returned."""
    return bool(result[0]) if isinstance(result, tuple) and result else bool(result)


def alert_daily_loss(day, pnl, *, send_fn=None):
    return send_alert("DAILY-LOSS", f"{day} realized ${pnl:.2f} — breaker tripped, entries halted (exits run)",
                      send_fn=send_fn)


def alert_drift(mint, position_id, *, send_fn=None):
    return send_alert("RECON-DRIFT", f"{_coin(mint)} pos#{position_id} — position ledger != on-chain balance",
                      send_fn=send_fn)


def alert_gas_low(sol_balance, required, *, send_fn=None):
    return send_alert("GAS-LOW", f"SOL {sol_balance:.4f} < required {required:.4f} — entries will block",
                      send_fn=send_fn)


def _weth_text(weth):
    return "WETH %.6f" % weth if weth is not None else "WETH unread"


def alert_evm_gas_low(chain, have, need, weth, *, send_fn=None):
    return send_alert("GAS-LOW", f"chain {chain}: native {have:.6f} < {need:.6f} needed for one "
                      f"entry + full-book exit, {_weth_text(weth)} — funded entries on {chain} "
                      "refuse until native gas is funded", send_fn=send_fn)


def alert_evm_gas_ok(chain, have, need, weth, *, send_fn=None):
    return send_alert("GAS-OK", f"chain {chain}: native {have:.6f} >= {need:.6f}, "
                      f"{_weth_text(weth)} — funded entries on {chain} can pass the gas gate again",
                      send_fn=send_fn, icon="🟢")


def alert_evm_gas_pass_held(chain, held_min, state, *, send_fn=None):
    return send_alert("GAS-PASS-HELD", f"chain {chain}: the gas pass has done nothing for "
                      f"{held_min:.0f} min ({state}) — an unsettled funded reservation holds it; "
                      "native is neither topped up nor wrapped back until it settles",
                      send_fn=send_fn, icon="🟠")


def alert_exit_anchor_bound(position, chain, row, *, send_fn=None):
    """The anchor-checked exit held a low sell quote for its whole bound and then sold it
    (spec 2026-10-01 §3.2 step 4): the operator sees every sale the hold could not save."""
    anchor_value = float(row.get("anchor_price") or 0.0) * float(row.get("qty") or 0.0)
    return send_alert(
        "EXIT-ANCHOR-BOUND",
        f"{position.get('ticker') or position.get('mint')} chain {chain} pos#{position.get('id')}: "
        f"sold at ${float(row.get('value_usd') or 0.0):.2f} against an anchor of ${anchor_value:.2f} "
        f"after holding {float(row.get('held_s') or 0.0):.1f}s ({row.get('exit_reason')}, "
        f"route {row.get('route') or '?'})",
        send_fn=send_fn, icon="🟠")


def alert_kill_switch(*, send_fn=None):
    return send_alert("KILL-SWITCH", "tripped — entries halted (exits still run)", send_fn=send_fn)


def alert_config_gap(chains, *, send_fn=None):
    values = ",".join(str(chain) for chain in chains)
    return send_alert(
        "CONFIG-GAP",
        f"funded trading enabled but allowlisted EVM chain(s) have no built context: {values}",
        send_fn=send_fn,
    )


def alert_new_network(network_id, ticker, mint, *, send_fn=None):
    """fomo emitted its first event on a network we've never seen — a venue-support decision is
    now live (e.g. Robinhood Chain 4663 was awaited 2026-07-10; entries stay dropped until the
    operator allowlists + funds it)."""
    return send_alert("NEW NETWORK",
                      f"first feed event on network={network_id} ({ticker or '?'} {mint[:12]}…) — "
                      "evaluate venue support", send_fn=send_fn)


def alert_auth_dead(*, down_s=None, send_fn=None):
    """`down_s` is the elapsed outage, present on every page after the first. Without it the
    operator cannot tell a 5-minute blip from the 121-minute 2026-09-02 outage."""
    down = "" if not down_s or down_s < 60 else f" — down {int(down_s // 60)}m"
    return send_alert("AUTH-DEAD", f"fomo auth dead{down} — feed blind; brackets still fire; "
                                   f"replace the token", send_fn=send_fn)


def alert_derisk(mint, received, sig, *, handle=None, ticker=None, send_fn=None):
    return send_alert("DERISKED", f"{_who(mint, handle, ticker)} recovered ${received:.2f} "
                                  f"sig={_sig8(sig)} (riding the rest)", send_fn=send_fn)


def alert_derisk_unknown(mint, sig, *, handle=None, ticker=None, send_fn=None):
    return send_alert("DERISK-UNKNOWN", f"{_who(mint, handle, ticker)} partial-sell confirm "
                                        f"unknown sig={_sig8(sig)} — auto-verifying; rail "
                                        f"re-arms if it provably failed", send_fn=send_fn)


def alert_close_unknown(mint, sig, *, handle=None, ticker=None, send_fn=None):
    return send_alert("CLOSE-UNKNOWN", f"{_who(mint, handle, ticker)} full sell confirmation or "
                                       f"proceeds unresolved sig={_sig8(sig)} — auto-verifying; "
                                       f"close claim stays held until recovery settles it", send_fn=send_fn)


def alert_derisk_rearmed(mint, reason, *, handle=None, ticker=None, send_fn=None):
    return send_alert("DERISK-REARMED", f"{_who(mint, handle, ticker)} partial sell never "
                                        f"landed ({reason}) — rail re-armed", send_fn=send_fn)


def alert_close_rearmed(mint, reason, *, handle=None, ticker=None, send_fn=None):
    return send_alert("CLOSE-REARMED", f"{_who(mint, handle, ticker)} full sell never landed "
                                       f"({reason}) — claim released, exit re-armed", send_fn=send_fn)


def alert_close_claim_held(mint, position_id, *, handle=None, ticker=None, send_fn=None):
    """A signed close nobody can settle. The position cannot be exited by the rails OR the operator
    while this holds, and before 2026-08-14 it made no sound at all."""
    return send_alert("CLOSE-STRANDED", f"{_who(mint, handle, ticker)} pos#{position_id} has a "
                                        f"signed close that will not settle — exit is BLOCKED; "
                                        f"reconcile the signature on chain", send_fn=send_fn)


def alert_derisk_landed_late(mint, sig, *, handle=None, ticker=None, operator=False, send_fn=None):
    tail = ("its sold quantity is unknown: operator must reconcile it; realized stays unknown"
            if operator else "booked unpriced; recovery prices it from its receipt, realized "
            "unknown until then")
    return send_alert("DERISK-LANDED", f"{_who(mint, handle, ticker)} unknown partial sell "
                                       f"DID land sig={_sig8(sig)} — {tail}", send_fn=send_fn)


def alert_failed_tx(mint, sig, reason=None, *, send_fn=None):
    """A funded tx landed and reverted. SAY WHY IN THE PAGE.

    2026-09-08: the operator forwarded this alert and asked what it was, and answering it cost a
    mainnet getTransaction — the page carried a mint and eight characters of signature. The runner
    already knew: the executor puts the chain's own words in `res["reason"]`, which for that
    transaction read "exceeds desired slippage limit" — a designed, atomic, no-funds-moved refusal.

    Without it, "the venue refused our price" and "the swap ran out of funds" are the same red
    alert, and only one of them is worth waking up for. `reason` stays optional so an older seam
    that returns a bare "failed" still pages; losing the alert is far worse than losing the why.
    """
    detail = str(reason or "").strip()
    detail = detail.split("chain_error:", 1)[1].strip() if "chain_error:" in detail else detail
    tail = f" — {detail}" if detail else ""
    return send_alert("FAILED-TX", f"{_coin(mint)} sig={sig or 'Unknown'} — transaction failed{tail}",
                      send_fn=send_fn)


def alert_operator_action(action, detail, *, send_fn=None):
    """An OPERATOR changed funded state. Every other alert in this module reports what the machine
    did; nothing reported what the human did, so arming a wallet, revoking one, closing a position
    by hand or moving a TP/SL left no trace outside the audit tables.

    That matters most when several people or sessions touch one funded book: a position that
    vanishes is either a rail that fired (alerted) or a hand on the button (silent until now).

    Best-effort like every alert here — a notification must never fail the action it describes.
    """
    return send_alert("OPERATOR", f"{action} | {detail}", send_fn=send_fn, icon="🟡")


def alert_real_demote(handle, realized_total, consecutive_losses, *, send_fn=None):
    """Pre-registered guardrail fired: the wallet is auto-revoked from the real allowlist
    (paper copy + watch alerts continue). Re-approval is a manual operator action."""
    return send_alert("REAL-DEMOTE",
                      f"{handle} auto-revoked from real allowlist — realized ${realized_total:+.2f}, "
                      f"{consecutive_losses} straight losses. Re-approve manually if you disagree.",
                      send_fn=send_fn)


# EXIT COVERAGE THAT NEVER COMES BACK (2026-09-26, Codex ctb audit P0 item 5). PAIDCATS 65854 sat
# on onchain_pending / onchain_unavailable for four minutes against a 30 s vol_death threshold and
# nothing paged; 65860 held on a false route_changed every minute. These verdicts mean the rail
# TRIED its on-chain proof and could not answer, so a long run of them is lost exit coverage, not
# a hold on evidence. The by-design holds (state_uncovered_frozen and friends) are excluded, or
# every uncovered position would page.
VOLDEATH_BLIND_REASONS = frozenset({
    "onchain_pending", "onchain_unavailable",
    "onchain_unavailable:binding_missing", "onchain_unavailable:route_unknown",
    "onchain_unavailable:identity_mismatch", "onchain_unavailable:provider",
    "onchain_unavailable:lookup_failed", "route_unknown_entry",
})
_vd_blind_lock = _threading.Lock()
_vd_blind_state = {}        # position_id -> {"since", "seen", "paged"} (monotonic seconds)


def note_voldeath_coverage(position_id, why, *, mint=None, handle=None, ticker=None,
                           now_mono=None, page_after_s=None, send_fn=None):
    """One vol_death verdict for a funded position. Pages ONCE when blind verdicts run for
    `page_after_s` (MEMECOIN_VOLDEATH_BLIND_PAGE_S; 0 = off), and once on recovery for an episode
    that paged. Alert only: it cannot fire or hold an exit, and never raises.

    Called by the fast lane and the slow sweep, so the state takes a lock, released before the
    enqueue (no send is ever made while holding it). A restart forgets episodes: one still blind
    afterwards pages once more. Returns 'blind', 'recovered' or None."""
    import time as _time
    try:
        if page_after_s is None:
            from sniper.settings import memecoin as _cfg
            page_after_s = getattr(_cfg, "MEMECOIN_VOLDEATH_BLIND_PAGE_S", 0.0)
        page_after_s = float(page_after_s or 0.0)
        if not math.isfinite(page_after_s) or page_after_s <= 0:
            return None
        now = _time.monotonic() if now_mono is None else float(now_mono)
        blind = str(why) in VOLDEATH_BLIND_REASONS
        event = None
        with _vd_blind_lock:
            for pid in [p for p, s in _vd_blind_state.items() if now - s["seen"] > 3600.0]:
                _vd_blind_state.pop(pid, None)
            state = _vd_blind_state.get(position_id)
            if blind:
                if state is None:
                    state = {"since": now, "seen": now, "paged": False}
                    _vd_blind_state[position_id] = state
                state["seen"] = now
                if not state["paged"] and now - state["since"] >= page_after_s:
                    state["paged"] = True
                    event = ("blind", now - state["since"])
            elif state is not None:
                _vd_blind_state.pop(position_id, None)
                if state["paged"]:
                    event = ("recovered", now - state["since"])
        if event is None:
            return None
        who = _who(mint, handle, ticker)
        if event[0] == "blind":
            send_alert("VOLDEATH-BLIND",
                       "pos %s %s: vol_death cannot prove silence for %.0fs (%s); other exit "
                       "rails still run" % (position_id, who, event[1], why), send_fn=send_fn)
        else:
            send_alert("VOLDEATH-BLIND-RECOVERED",
                       "pos %s %s: vol_death has evidence again after %.0fs (%s)"
                       % (position_id, who, event[1], why), send_fn=send_fn, icon="🟢")
        return event[0]
    except Exception:
        logger.exception("vol_death coverage note failed pos=%s", position_id)
        return None
