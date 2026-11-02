#!/usr/bin/env python3
"""Overnight watch: report new funded positions, and separate SYSTEM refusals from POLICY ones.

WHY THE SPLIT IS THE WHOLE POINT. `memecoin_entry_attempts.terminal_reason` mixes two populations
that want opposite responses. `not_approved` and `disarmed` are the arming governance working
correctly -- 863 and 68 in one 3-hour sample -- and paging on them is noise that trains the operator
to ignore the channel. `no quote (provider_rpc_transport: ...)`, `quote balance unavailable`, and an
`unresolved signed transaction` are the SYSTEM failing to execute a signal it wanted to take, and
every one is a missed entry or an ambiguous spend. Only the second population should ever page.

An UNKNOWN reason is reported as UNKNOWN, never silently bucketed as policy. The classifier can only
see the patterns it was given, and a refusal shape nobody anticipated is exactly the one worth
seeing -- intersecting against a hardcoded list can otherwise only ever report members of that list.

Windowing is explicit. `--since-minutes` bounds every count, and the window is printed with the
numbers, because a count without its window is not a rate and has burned this project before.

Read-only on the funded ledger. Never writes, never trades.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# One file, shared by the traceback offsets and the loss-streak latch. Each reader load-modifies-
# saves the whole dict and they run sequentially, so neither drops the other's keys.
STATE_PATH = ROOT / "data" / "memecoin_overnight_watch.state"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A refusal the OPERATOR chose. Correct behaviour; never pages.
POLICY = (
    "not_approved", "disarmed", "excluded", "one position per coin",
    "insufficient bankroll", "max concurrent", "max total exposure",
    "out_of_scope", "cooldown", "manual", "post_peak", "mcap", "premium",
    "volume", "turnover", "txns", "chase", "route capacity", "capacity",
    "safety", "honeypot", "unsellable", "tax", "liquidity", "holder",
    "cluster", "rug", "age", "band", "not in scope", "wallet",
    "min-trade floor", "re-entry", "daily cap",
    # "quiet_hours" is the operator's configured trading window refusing a signal outside it.
    # Fourth phrasing of an operator-configured rule to reach UNKNOWN and page; the classifier can
    # only report members of the list it is given, which is why the bucket exists.
    "quiet_hours",
    # FOUND BY ENUMERATION, NOT ONE AT A TIME (2026-09-23). Five phrasings had reached UNKNOWN one
    # per night, each costing a round trip to notice. Running every refusal shape the live book
    # produced in 7 days through this classifier surfaced 23 UNKNOWN shapes at once; these nine name
    # an operator-configured limit doing its job and were paging as ACTIONABLE. Counts are that
    # week's occurrences. The ~53 that remain -- route build refusals, simulation reverts,
    # broadcast rejections, `sell_sim_min_out_repeated`, `interrupted` -- are deliberately left in
    # UNKNOWN: they are faults or shapes nobody has explained, which is what the bucket is for.
    "round trip",               # 54  round trip N% over N% cap (sell back $N of $N)
    "round_trip_over_budget",   #  7  the same cap inside a route_over_budget message
    "drawdown pause",           # 34  today's realized PnL fell below the day's high
    "kill_switch",              # 28  the operator's own switch
    "chart mid",                # 26  route quote is Nx the chart mid (cap Nx)
    "entry older than",         #  9  the source-age gate
    "coin_too_young",           #  9  configured minimum age
    "price impact",             #  4  configured impact cap
    "gas reserve",              #  2  native-is-quote gas reserve floor
    # "absolute floor": `source buy $89 under absolute floor $198` is the operator's minimum
    # source-buy size doing its job. Its MCAP sibling already landed in POLICY via "mcap", so only
    # the absolute form fell to UNKNOWN and paged. Verified against the live classifier 2026-09-22.
    "absolute floor",
    # "source price" (not just "above source price"): the execution premium cap emits BOTH
    # "exact quote is +59% above source price (cap 30%)" and "route quote is 3.06x the source price
    # (cap 1.95x)". The narrower pattern caught only the first, so the multiplier form fell to
    # UNKNOWN and would have paged on a cap doing exactly its job -- it blocked a 2.64x route that
    # would have been -53%. Third phrasing of an operator-configured ceiling to land here; the
    # lesson is that one gate can emit several message shapes.
    "source price",
    # RUN-UP CAPS. Both name their own configured ceiling -- "(cap 3.0x)", "(cap 30%)" -- so they are
    # the operator's choice, not a fault. Neither contains "chase" or any other word already listed,
    # so they fell through to UNKNOWN and would have paged. The UNKNOWN bucket surfacing them is the
    # design working: a classifier can only report members of the list it was given, which is exactly
    # why an unrecognised reason must never be folded into POLICY by default.
    "above first-seen price", "past their entry",
)
# The SYSTEM failing to act on a signal it wanted. These page.
#
# EVERY pattern here must be matched on a WORD BOUNDARY. The first version matched bare substrings
# and classified `m5 organic volume $0 under floor $142942` as SYSTEM, because the dollar amount
# contains "429". A refusal the operator deliberately configured was about to page as an
# infrastructure fault -- the classifier inventing an incident out of an ordinary number. Numeric
# and short tokens are the dangerous ones; "rpc" inside a longer word is the same hazard.
SYSTEM = (
    r"^slot window expired$",
    r"\brpc\b", r"rpc_transport", r"\btransport\b", r"\btimeout\b", r"timed out",
    r"\bprovider\b", r"\bunresolved\b", r"balance unavailable",
    r"\bno[_ ]route\b", r"decimals unavailable", r"quote failed", r"infrastructure",
    r"\bunavailable\b", r"\bconnection\b", r"\bhttp[_ ]?4\d\d\b", r"rate limit",
    r"\binternal\b", r"invalid_funded_contract",
)
_SYSTEM_RE = re.compile("|".join(SYSTEM))


def _traded_chains() -> set | None:
    """Chains we actually trade, or None when that cannot be established.

    None is deliberate and is NOT an empty set: an unreadable allowlist must make the caller fail
    LOUD rather than silently reclassify real faults as policy. `_wallet_is_armed` takes the same
    direction for the same reason -- "unknown inputs return True, so a lookup failure is never
    silent"."""
    try:
        from sniper.util.envfiles import load_env_files
        load_env_files(str(ROOT))
        from sniper.settings import memecoin as cfg
        return {str(x) for x in cfg.MEMECOIN_EVM_ALLOWLIST} | {"solana"}
    except Exception:
        return None


def _deploy_intent_alarms(path, *, now_s: float, stale_s: int) -> list[str]:
    """Alarm when a deploy started stopping things and never finished. Never raises.

    A HALF-COMPLETED DEPLOY IS INDISTINGUISHABLE FROM SUCCESS, which is why this exists. The deploy
    stops the runner early and switches `current` late. Killed in between, the watchdog relaunches
    from `current` -- still the OLD release -- and the runner comes back HEALTHY on the old code.
    Every other check here passes. Measured 2026-09-02T14:56Z: a client-side 2-minute timeout cut a
    funded deploy exactly there, and the only reason anyone noticed was that a human was watching
    the symlink.

    The deploy writes this file before touching the runner and removes it on success, so its
    presence past a normal deploy's duration means the deploy did not land. A trap cannot cover
    this: bash runs no EXIT trap on SIGKILL, so the file is the only evidence that survives.

    Reports the intended SHA and what `current` actually points at, because "a deploy failed" is
    not actionable and "you are running X, you meant Y" is.
    """
    try:
        path = Path(path)
        age = now_s - path.stat().st_mtime
    except FileNotFoundError:
        return []                              # the normal state: no deploy in flight
    except Exception as exc:
        return [f"DEPLOY INTENT UNREADABLE: {type(exc).__name__}: {exc}"]
    if age <= stale_s:
        return []                              # a deploy legitimately in flight right now
    try:
        intended = path.read_text().strip() or "<empty>"
    except Exception:
        intended = "<unreadable>"
    try:
        current = (Path(path).parents[1] / ".runtime" / "memecoin" / "current").resolve().name
    except Exception:
        current = "<unknown>"
    return [f"DEPLOY INCOMPLETE: intent written {age/60:.1f}m ago and never cleared — "
            f"intended {intended[:12]}, current {current[:12]}. The runner may be healthy on the "
            f"OLD release; a half-deploy reverts silently."]


def runner_health(hb_path, *, now_s: float, stale_s: int = 120,
                  grace_s: int = 300, feed_stale_s: int = 1800,
                  deploy_intent_stale_s: int = 600) -> list[str]:
    """Alarm lines for runner/route state the refusal tables cannot see. Never raises.

    THIS EXISTS BECAUSE A ROUTE DIED FOR 26 HOURS AND THIS WATCH SAW NOTHING. LI.FI came up
    unauthenticated on 2026-09-01T07:48 (Cloudflare 403 on a request with no User-Agent) and stayed
    that way. It produced ZERO refusal rows, because a disabled route is not a refusal -- the
    direct V3 path simply carried every funded entry instead. A DB-only watch is structurally blind
    to any degradation that removes an option rather than rejecting a signal.

    Staleness comes from the file MTIME, not from the `ts` field inside it. A timestamp written by
    the runner cannot report that the runner stopped writing; the mtime is stamped by the OS, so it
    keeps counting after the process dies. Same reason a heartbeat counter needs an external clock.

    The LI.FI check is graced by UPTIME, not by a sentinel file: the authentication probe runs after
    the first heartbeat, so a fresh process is legitimately unauthenticated for a moment. Uptime is
    derived from `started_at` in the heartbeat itself, which keeps this check stateless -- it cannot
    lose its own start time across a restart, which is exactly how the 26h gap stayed invisible.
    """
    alarms: list[str] = []
    alarms.extend(_deploy_intent_alarms(Path(hb_path).parent / ".memecoin_deploy_intent",
                                        now_s=now_s, stale_s=deploy_intent_stale_s))
    try:
        import json
        path = Path(hb_path)
        age = now_s - path.stat().st_mtime
        if age > stale_s:
            alarms.append(f"RUNNER HEARTBEAT STALE: {age/60:.1f}m old (>{stale_s}s)")
            return alarms                      # a stale file's contents describe the past
        hb = json.loads(path.read_text())
    except FileNotFoundError:
        return ["RUNNER HEARTBEAT ABSENT: data/memecoin_runner.heartbeat does not exist"]
    except Exception as exc:                   # unreadable is a fault, never a pass
        return [f"RUNNER HEARTBEAT UNREADABLE: {type(exc).__name__}: {exc}"]

    if hb.get("auth_ok") is not True:
        alarms.append(f"RUNNER auth_ok={hb.get('auth_ok')!r} (expected True)")

    uptime = None
    try:
        from datetime import datetime
        started = datetime.fromisoformat(str(hb.get("started_at")))
        uptime = now_s - started.timestamp()
    except Exception:
        uptime = None                          # unknown uptime -> do not grace, report below

    # A CONNECTED FEED THAT DELIVERS NOTHING. `auth_ok` catches a rejected credential -- it caught
    # the 2026-09-02 Privy 401 seven times and paged correctly. It cannot catch a feed that
    # authenticates and then goes silent, which is a real and separately-observed failure: a
    # publicnode websocket once accepted a subscription and delivered zero payloads (2026-08-07),
    # and a subscription reply is not delivery.
    #
    # `last_payload_at` is the only field that distinguishes them. Absent means NOTHING has ever
    # arrived on this connection, which after the grace window is a fault however healthy the
    # handshake looks. Graced by uptime like the LI.FI check, for the same reason: a freshly started
    # runner legitimately has no payload yet.
    # NOT A FAULT WHILE J7 IS THE SOURCE (2026-09-22). The fomo account is banned and signals
    # arrive over the J7 relay, so `fomo_activity_ws status='disabled'` is the configured state and
    # paging on it trains the operator to ignore the channel. Check J7 delivery instead, retaining
    # the payload-age gate even when some accounts connect. Account-specific outages alert separately.
    j7 = hb.get("j7_feed") or {}
    accounts = j7.get("accounts")
    j7_connected = (any(a.get("status") == "connected" for a in accounts if isinstance(a, dict))
                    if isinstance(accounts, list) else j7.get("status") == "connected")
    ws = hb.get("fomo_activity_ws") or {}
    feed_name = "fomo_activity_ws"
    if str(ws.get("status") or "").lower() == "disabled" and j7:
        feed_name = "j7_feed"
        ws = {"status": j7.get("status"), "reconnect_count": j7.get("reconnect_count"),
              "last_payload_at": j7.get("last_event_at") if j7_connected else None}
    if ws and (uptime is None or uptime > grace_s):
        last_payload = ws.get("last_payload_at")
        if not last_payload:
            up = "unknown" if uptime is None else f"{uptime/60:.0f}m"
            alarms.append(
                f"SIGNAL FEED SILENT: {feed_name} status={ws.get('status')!r} has no delivering "
                f"connection (up {up}, {ws.get('reconnect_count')} reconnects). "
                "Check this feed's connection and authentication.")
        else:
            try:
                from datetime import datetime
                age = now_s - datetime.fromisoformat(
                    str(last_payload).replace("Z", "+00:00")).timestamp()
                if age > feed_stale_s:
                    alarms.append(
                        f"SIGNAL FEED STALE: {feed_name} last payload {age/60:.0f}m ago "
                        f"(status={ws.get('status')!r}). Check this feed's delivery.")
            except Exception:
                alarms.append(f"SIGNAL FEED {feed_name} last_payload_at unparseable: {last_payload!r}")

    lifi = hb.get("lifi") or {}
    if str(lifi.get("mode") or "") == "authenticated_funded":
        reasons = list(lifi.get("disable_reasons") or ())
        if reasons and (uptime is None or uptime > grace_s):
            up = "unknown" if uptime is None else f"{uptime/60:.0f}m"
            alarms.append(
                f"LI.FI ROUTE DISABLED: reasons={reasons} authenticated={lifi.get('authenticated')} "
                f"entry_admitted={lifi.get('entry_admitted')} (runner up {up}). "
                "Funded entries may still route direct; this route is unavailable.")
    return alarms


def classify(reason: str, network: str | None = None, traded: set | None = None) -> str:
    """SYSTEM / POLICY / UNKNOWN for one refusal.

    `unroutable_chain` NEEDS THE CHAIN, because the same string means opposite things:
      * chain NOT in the allowlist  -> POLICY. A global feed delivers signals on chains we
        deliberately do not trade and the router declines them. Working as configured.
      * chain IN the allowlist      -> SYSTEM. We meant to trade it and could not. A real fault.

    Classifying on the reason text alone cannot separate those, and it drowns the case that matters:
    126 of ~155 SYSTEM refusals in a 3h sample were unroutable_chain on chains 56 and 1, neither of
    which is configured. If 4663 or 8453 ever emitted this, it would be buried under by-design noise.

    This is a REPEAT of a solved problem. `_wallet_is_armed` (memecoin_real_runner.py:92) exists for
    exactly this class and its docstring records the measurement: "84 of the ~173 entry alerts in 24h
    were `unroutable_chain` on wallets with no authority on that chain at all -- half the traffic,
    none of it actionable." That gate suppressed it for Telegram in August; this watch reintroduced
    it through a different door. Found by Beta, 2026-09-01.
    """
    r = (reason or "").lower()
    if not r:
        return "UNKNOWN"
    if "unroutable_chain" in r:
        if traded is None:                  # allowlist unreadable -> fail LOUD, never silent
            return "SYSTEM"
        return "SYSTEM" if str(network or "") in traded else "POLICY"
    # AN UNDERSCORE IS A WORD CHARACTER, SO `\b` NEVER FIRES INSIDE ONE OF THESE TOKENS
    # (2026-09-23). `\bprovider\b` does not match `provider_poolkey_read_failed`, `\brpc\b` does
    # not match `provider_rpc_rate_limit`, and `\bno[_ ]route\b` does not match
    # `no_route_all_venues` -- so 33 refusals a week reported UNKNOWN while being exactly the
    # SYSTEM population this file exists to separate out. The tell was already here: `rpc_transport`
    # sits in SYSTEM as a literal underscore token, a workaround for this same anchor rather than a
    # fix. Only the SYSTEM match is normalised; POLICY keeps the raw string because its patterns
    # deliberately contain underscores (`not_approved`, `quiet_hours`, `coin_too_young`).
    # THE ONE-PATH GUARD IS NOT A FAULT (2026-09-28, operator page). "unresolved signed transaction
    # on <chain>" refuses a buy because OUR OWN transaction for that chain is still in flight -- the
    # slot lane's, usually, with J7 standing down behind it. It matched `\bunresolved\b` and paged as
    # SYSTEM on every such refusal. A reservation that STAYS stuck still pages: the entry breaker
    # below pages on its duration, which is the measure its own comment argues for.
    if "unresolved signed transaction" in r:
        return "POLICY"
    if _SYSTEM_RE.search(r.replace("_", " ")):  # SYSTEM wins ties: a policy word inside an RPC
        return "SYSTEM"                         # failure ("safety read timed out") is a fault.
    # Match complete policy reasons so missing proof and malformed prices still page.
    if (r == "first_source_buy_missed" or re.fullmatch(
            r"route quote is [0-9]+(?:\.[0-9]+)?x the post-trade price "
            r"\(cap [0-9]+(?:\.[0-9]+)?x\)", r)):
        return "POLICY"
    for pat in POLICY:
        if pat in r:
            return "POLICY"
    return "UNKNOWN"


# ---------------------------------------------------------------------------------------------
# Sections added 2026-09-22 on the operator's overnight order. EVERY ONE IS FAIL-ISOLATED: the cron
# runs this file straight from the repo checkout, so a commit is live with no deploy gate between.
# A section that throws must degrade to one line and never take the refusal report down with it.

def failed_transactions(c, since_iso):
    """Entries and exits whose SUBMITTED transaction failed on chain. Gas was spent.

    Scoped to attempts that actually broadcast: a preflight refusal that never signed is already
    counted once in the refusal report, and counting it again here would make a quiet night read as
    a bad one.

    Rows are (kind, at, coin, network, tx, reason, source). THE COIN IS NEVER "None" (2026-09-28):
    slot-lane attempts carry no ticker, so the page read "entry 04:38:30 None solana ..."; the mint
    prefix stands in, and the source handle says whose buy it was.
    """
    rows = []
    try:
        for r in c.execute(
            "SELECT decision_started_at AS at, ticker, mint, source_handle, network_id, tx_sig, "
            "terminal_reason FROM memecoin_entry_attempts WHERE decision_started_at>=? "
            "AND tx_sig IS NOT NULL "
            "AND (terminal_status IN ('failed','reverted') OR terminal_reason LIKE '%revert%' "
            "     OR terminal_reason LIKE '%reverted_min_out%') ORDER BY decision_started_at",
                (since_iso,)):
            rows.append(("entry", r["at"], r["ticker"] or str(r["mint"] or "?")[:8],
                         r["network_id"], r["tx_sig"], r["terminal_reason"], r["source_handle"]))
    except Exception as exc:
        return None, f"entry side unavailable: {exc}"
    try:
        for r in c.execute(
            "SELECT closed_at AS at, ticker, network_id, close_sig AS tx, exit_reason "
            "FROM memecoin_positions WHERE execution_mode='real' AND closed_at>=? "
            "AND close_sig IS NOT NULL AND exit_reason LIKE '%fail%' ORDER BY closed_at",
                (since_iso,)):
            rows.append(("exit", r["at"], r["ticker"] or "?", r["network_id"], r["tx"],
                         r["exit_reason"], None))
    except Exception as exc:
        return rows, f"exit side unavailable: {exc}"
    return rows, None


# A MIN-OUT REVERT IS THE SLIPPAGE LIMIT DOING ITS JOB (2026-09-28). The price moved past our
# minimum output before the transaction landed; the chain refused the trade and kept only the fee.
# It is listed, never paged: "pages on any" fired on BuySlippageBelowMinBaseAmountOut at 04:38Z.
# Every other failure still pages -- a revert nobody named is exactly what the page is for.
_MIN_OUT_RE = re.compile(
    r"slippage|min[_ ]?(?:base|quote)?[_ ]?amount[_ ]?out|min[_ -]?out|too ?little ?received"
    r"|insufficient[_ ]output", re.I)


def is_min_out(row) -> bool:
    return bool(_MIN_OUT_RE.search(str(row[5] or "")))


def failed_line(row) -> str:
    kind, at, coin, net, tx, reason, source = row[:7]
    note = "  (min-out: price moved, fee only)" if is_min_out(row) else ""
    return (f"      {kind} {str(at)[11:19]} {coin} {net} {source or '-'} {str(tx)[:18]} "
            f"{str(reason)[:60]}{note}")


def failed_pages(rows) -> list[str]:
    failed = [r for r in rows or () if not is_min_out(r)]
    if not failed:
        return []
    kind, _at, coin, net, _tx, reason, _source = failed[0][:7]
    return [f"{len(failed)} failed transaction(s) with gas spent (not a min-out revert) · "
            f"{net or 'unknown chain'} {kind} {coin or '?'}: {str(reason or 'unknown failure')[:90]}"]


def outcomes(c, since_iso, day_iso):
    """Closes, wins, losses and realized PnL by chain, for the window AND a fixed 24 h."""
    def tally(bound):
        by = {}
        for r in c.execute(
            "SELECT network_id AS net, realized_pnl AS pnl FROM memecoin_positions "
            "WHERE execution_mode='real' AND closed_at>=? AND realized_pnl IS NOT NULL",
                (bound,)):
            b = by.setdefault(r["net"] or "?", {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
            b["n"] += 1
            b["pnl"] += float(r["pnl"])
            if float(r["pnl"]) > 0:
                b["w"] += 1
            elif float(r["pnl"]) < 0:
                b["l"] += 1
        return by

    def streaks(bound):
        """Consecutive losses ending NOW, per chain. A run broken by a win is not a streak."""
        run = {}
        for r in c.execute(
            "SELECT network_id AS net, realized_pnl AS pnl FROM memecoin_positions "
            "WHERE execution_mode='real' AND closed_at>=? AND realized_pnl IS NOT NULL "
            "ORDER BY closed_at", (bound,)):
            net = r["net"] or "?"
            run[net] = run.get(net, 0) + 1 if float(r["pnl"]) < 0 else 0
        return run
    return tally(since_iso), tally(day_iso), streaks(day_iso)


def loss_streak_starts(c, bound):
    """The `closed_at` of the FIRST loss in each chain's CURRENT run of losses.

    This is the streak's identity. Latching on length alone would silence a streak that broke and
    rebuilt to the same length -- three losses, a win, three more -- which is a new event the
    operator should hear about, not a repeat. A win clears the chain's start, so the next run gets
    a new one.
    """
    start, run = {}, {}
    for r in c.execute(
        "SELECT network_id AS net, realized_pnl AS pnl, closed_at FROM memecoin_positions "
        "WHERE execution_mode='real' AND closed_at>=? AND realized_pnl IS NOT NULL "
        "ORDER BY closed_at", (bound,)):
        net = r["net"] or "?"
        if float(r["pnl"]) < 0:
            if not run.get(net):
                start[net] = str(r["closed_at"])
            run[net] = run.get(net, 0) + 1
        else:
            run[net] = 0
            start.pop(net, None)
    return start


def _load_state(state_path) -> dict:
    import json as _json
    try:
        if os.path.exists(state_path):
            return _json.load(open(state_path)) or {}
    except Exception:
        pass
    return {}


def _save_state(state_path, state) -> None:
    import json as _json
    try:
        with open(state_path, "w") as fh:
            _json.dump(state, fh)
    except Exception:
        pass                          # a page we cannot latch is still a page worth printing


def pnl24_pages(net24, state_path, *, floor=-150.0, step=50.0) -> list[str]:
    """Page when the 24h net CROSSES the floor, then again for each further `step` down.

    "24h net below -$150" paged on every 15-minute run while the net stayed there (2026-09-28): one
    fact, a page per run. Back at or above the floor clears the latch, so a later crossing is new.
    """
    state = _load_state(state_path)
    latch = state.get("pnl24") or {}
    pages = []
    if net24 >= floor:
        latch = {}
    elif latch.get("paged") is None:
        pages.append(f"24h net ${net24:+.2f} below -${abs(floor):.0f}")
        latch = {"paged": net24}
    elif net24 <= float(latch["paged"]) - step:
        pages.append(f"24h net ${net24:+.2f} below -${abs(floor):.0f} (down "
                     f"${float(latch['paged']) - net24:.2f} since the last page)")
        latch = {"paged": net24}
    state["pnl24"] = latch
    _save_state(state_path, state)
    return pages


def streak_pages(run, starts, state_path, *, threshold=3):
    """Page when a loss streak FORMS or GROWS -- never once per run while it merely persists.

    Written because "3 consecutive losses on solana" paged on every 15-minute run from 23:30Z,
    which is a dozen copies of one fact and teaches the operator to ignore the channel. A page the
    reader learns to skip is worse than no page.

    The latch key is the streak's START timestamp, not its length, so a broken-and-rebuilt streak
    of the same length is correctly a NEW page. A chain that drops below `threshold` leaves the
    state entirely, so its next streak is new by construction.
    """
    import json as _json
    state = {}
    try:
        if os.path.exists(state_path):
            state = _json.load(open(state_path)) or {}
    except Exception:
        state = {}
    seen = state.get("loss_streaks") or {}
    pages, keep = [], {}
    for net, n in sorted(run.items()):
        if n < threshold:
            continue
        start = starts.get(net)
        keep[net] = {"start": start, "len": n}
        prev = seen.get(net) or {}
        if prev.get("start") != start or n > int(prev.get("len") or 0):
            pages.append(f"{n} consecutive losses on {net}")
    state["loss_streaks"] = keep
    try:
        # Shared with `new_tracebacks`, which load-modify-saves the same dict. Both run
        # sequentially in main(), so neither drops the other's keys.
        with open(state_path, "w") as fh:
            _json.dump(state, fh)
    except Exception:
        pass                          # a page we cannot latch is still a page worth printing
    return pages


def observer_health(vol_db, log_path, *, now_s, window_s):
    """Per-chain feed age, window covered, resets, and the last pricer / V3 resolver lines."""
    import json as _json
    import time as _t
    out = {}
    try:
        hb = _json.load(open(ROOT / "data" / "memecoin_runner.heartbeat"))
        for chain, v in (hb.get("onchain_volume_feed") or {}).items():
            out[chain] = {"feed_age_s": v.get("feed_age_s"), "covered_s": v.get("window_age_s"),
                          "resets_24h": v.get("resets_24h"), "available": v.get("available")}
    except Exception as exc:
        return {}, f"heartbeat unreadable: {exc}", None, None
    priced = resolved = None
    try:
        if os.path.exists(log_path):
            with open(log_path, "rb") as fh:                 # tail without reading the whole file
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 400_000))
                tail = fh.read().decode("utf-8", "replace").splitlines()
            import re as _re
            for line in reversed(tail):
                if priced is None and "hop prices:" in line:
                    # The line carries a dict of skip reasons after the count; only the COUNT and
                    # the timestamp are actionable, and dumping the dict buries them.
                    m = _re.search(r"hop prices:\s*(\d+)\s*priced", line)
                    stamp = line[:19]
                    priced = f"{m.group(1)} priced at {stamp}" if m else line.strip()[:80]
                if resolved is None and "v3 pools resolved" in line:
                    m = _re.search(r"v3 pools resolved[:\s]*(\d+)", line)
                    resolved = f"{m.group(1)} resolved at {line[:19]}" if m else line.strip()[:80]
                if priced and resolved:
                    break
    except Exception as exc:
        return out, f"log tail unavailable: {exc}", priced, resolved
    return out, None, priced, resolved


def unresolved_pools(vol_db):
    """Pools that have TICKS but no pool_meta row, split by address shape.

    A 42-character pool is an EVM address (a real V2/V3 pair); a 66-character one is a V4 pool id.
    They share the `v3_swap` kind, so a resolver that does not split them spends every batch trying
    to resolve V4 ids as contracts and never resolves a single real pair (Alpha, 2026-09-22).
    Counting them separately is what makes that visible.
    """
    try:
        c = sqlite3.connect(f"file:{vol_db}?mode=ro", uri=True, timeout=10)
        try:
            rows = c.execute(
                "SELECT LENGTH(t.pool) AS len, COUNT(DISTINCT t.pool) AS n FROM pool_ticks t "
                "LEFT JOIN pool_meta m ON m.chain_id=t.chain_id AND m.pool=t.pool "
                "WHERE t.chain_id='4663' AND m.pool IS NULL GROUP BY 1").fetchall()
            return {int(r[0]): int(r[1]) for r in rows}, None
        finally:
            c.close()
    except Exception as exc:
        return {}, str(exc)


_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')


def _frame(text):
    m = _FRAME_RE.search(text)
    return f"{os.path.basename(m.group(1))}:{m.group(2)} {m.group(3)}" if m else text.strip()


def _traceback_at(lines, i):
    """(summary, kind) for the Traceback that starts at lines[i].

    THE EXCEPTION, NOT THE FIRST FRAME (2026-09-28). The page printed the line after "Traceback",
    which is the outermost `File ...` frame -- 60 identical rows naming a release path and no error.
    The summary is the exception line, then the outermost and innermost frames by file:line."""
    frames, exc = [], None
    for line in lines[i + 1:i + 60]:
        if line.startswith("  "):
            if line.lstrip().startswith("File"):
                frames.append(_frame(line))
            continue
        if line.strip() and not re.match(r"^\d{4}-\d{2}-\d{2}T", line):
            exc = line.strip()
        break
    where = (frames[0] if len(frames) == 1 else f"{frames[0]} -> {frames[-1]}") if frames else "?"
    exc_text = exc[:120] if exc else "(exception line not captured)"
    kind = "|".join([(exc or "?").split(":")[0],
                     frames[0].split(" ")[0] if frames else "?",
                     frames[-1].split(" ")[0] if frames else "?"])
    return f"{exc_text} -- {where}", kind


def traceback_rows(found) -> list[str]:
    """One row per distinct (log, traceback), with its count, most frequent first."""
    counts = {}
    for name, summary, _kind in found:
        counts[(name, summary)] = counts.get((name, summary), 0) + 1
    return [f"{n:>7}x {name}: {summary}"
            for (name, summary), n in sorted(counts.items(), key=lambda kv: -kv[1])]


def traceback_pages(found, state_path, *, now_s, window_s=86400) -> list[str]:
    """Page on a traceback KIND not paged in the last `window_s`, never on its repeats.

    The runner logs a handled provider failure with its stack (tickstream getLogs, "tick skipped"):
    2686 of them sat in the log on 2026-09-28 and every 15-minute run paged "60 new traceback(s)".
    A new kind -- a new exception or a new place -- still pages at once."""
    state = _load_state(state_path)
    seen = state.get("tb_kinds") or {}
    new = {}
    for _name, summary, kind in found:
        last = seen.get(kind)
        if kind not in new and (last is None or now_s - float(last) > window_s):
            new[kind] = summary.split(" -- ")[0]
    for kind in new:
        seen[kind] = now_s
    state["tb_kinds"] = seen
    _save_state(state_path, state)
    if not new:
        return []
    names = list(dict.fromkeys(new.values()))      # one kind per place; name each exception once
    more = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
    return [f"{len(new)} new traceback kind(s): " + "; ".join(names[:3]) + more]


def new_tracebacks(log_paths, state_path):
    """Each NEW Traceback since the last run as (log, summary, kind). Byte offsets in `state_path`."""
    import json as _json
    state = {}
    try:
        if os.path.exists(state_path):
            state = _json.load(open(state_path)) or {}
    except Exception:
        state = {}
    found, errors = [], []
    for path in log_paths:
        try:
            if not os.path.exists(path):
                continue
            size = os.path.getsize(path)
            start = int(state.get(path) or 0)
            if start > size:                                 # rotated: read from the new start
                start = 0
            with open(path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read().decode("utf-8", "replace")
            state[path] = size
            lines = chunk.splitlines()
            for i, line in enumerate(lines):
                if "Traceback (most recent call last)" in line:
                    summary, kind = _traceback_at(lines, i)
                    found.append((os.path.basename(path), summary, kind))
        except Exception as exc:
            errors.append(f"{os.path.basename(path)}: {exc}")
    try:
        with open(state_path, "w") as fh:
            _json.dump(state, fh)
    except Exception as exc:
        errors.append(f"state write failed: {exc}")
    return found, errors


def page_title(triggers, *, since_minutes) -> str:
    """Only triggered faults go to Telegram; the full report stays in the cron log."""
    if not triggers:
        return ""
    lines = [f"🟠 Memecoin watch · last {since_minutes}m"]
    for trigger in triggers[:3]:
        line = " ".join(str(trigger).split())
        line = re.sub(r"^\d+ new traceback kind\(s\): ", "New error: ", line)
        line = line.replace("urllib.error.HTTPError: HTTP Error ", "HTTP ")
        line = re.sub(r"^(\d+) failed transaction\(s\) with gas spent \(not a min-out revert\)",
                      r"Failed transactions: \1; gas spent", line)
        line = re.sub(r"^(\d+) SYSTEM refusal\(s\)", r"System refusals: \1", line)
        line = re.sub(r"^(\d+) UNKNOWN refusal\(s\)", r"Unexplained refusals: \1", line)
        lines.append("• " + (line if len(line) <= 180 else line[:179] + "…"))
    if len(triggers) > 3:
        lines.append(f"• +{len(triggers) - 3} more alerts")
    lines.append("Details: logs/memecoin_overnight_watch.log")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-minutes", type=int, default=30)
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--stuck-minutes", type=int, default=15,
                    help="page when a chain has been entry-blocked this long")
    ap.add_argument("--page", action="store_true",
                    help="send Telegram on an entry breaker, or SYSTEM/UNKNOWN refusals")
    args = ap.parse_args()

    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    # THE WINDOW BOUND MUST BE ISO-T, NOT datetime()'s SPACE FORM. SQLite renders
    # datetime('now','-40 minutes') as "2026-09-01 23:16:43" while every timestamp in these tables is
    # stored ISO-8601 with a "T". Since ' ' < 'T', a space-separated bound compares LESS THAN every
    # same-day row and the window silently matches EVERYTHING.
    #
    # This shipped. The watch reported an identical "21 opened / 13 opened / 34 fills" for 20m, 40m,
    # 60m and 180m windows -- lifetime-to-date counts wearing a window label -- and it was relayed to
    # the operator as "the lane is filling". The truth at that moment was ZERO positions opened in
    # three hours. A monitor that cannot distinguish one minute from three hours is worse than none,
    # because it reads as reassurance.
    #
    # strftime('%Y-%m-%dT%H:%M:%S', ...) puts the T back. Proven by construction: with this bound a
    # -1 minute and a -9999 minute window return different counts; with the old one they did not.
    win = f"-{int(args.since_minutes)} minutes"
    BOUND = "strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?))"

    opened = c.execute(
        "SELECT network_id, COUNT(*) n, ROUND(SUM(entry_notional),2) usd FROM memecoin_positions "
        f"WHERE execution_mode='real' AND opened_at >= {BOUND} GROUP BY 1", (win,)
    ).fetchall()
    closed = c.execute(
        "SELECT network_id, COUNT(*) n, ROUND(SUM(realized_pnl),2) pnl FROM memecoin_positions "
        f"WHERE execution_mode='real' AND is_open=0 AND closed_at >= {BOUND} GROUP BY 1",
        (win,)
    ).fetchall()
    attempts = c.execute(
        "SELECT network_id, terminal_reason, COUNT(*) n FROM memecoin_entry_attempts "
        f"WHERE observed_at >= {BOUND} GROUP BY 1,2", (win,)
    ).fetchall()
    open_now = c.execute(
        "SELECT COUNT(*) n FROM memecoin_positions WHERE is_open=1 AND execution_mode='real'"
    ).fetchone()["n"]

    traded = _traded_chains()
    buckets: dict[str, dict[str, int]] = {"SYSTEM": {}, "POLICY": {}, "UNKNOWN": {}}
    filled = 0
    for r in attempts:
        reason = r["terminal_reason"]
        if reason is None or str(reason).lower() == "filled":
            filled += r["n"]
            continue
        key = f"{r['network_id']}: {str(reason)[:70]}"
        bucket = classify(str(reason), r["network_id"], traded)
        buckets[bucket][key] = buckets[bucket].get(key, 0) + r["n"]

    # A STUCK SIGNED RESERVATION IS A CHAIN-WIDE ENTRY BREAKER, and its refusals understate it
    # badly. `unresolved_signed_pending_count(conn, network) > 0` refuses EVERY entry on that chain,
    # so ONE stuck row prints one refusal per signal that arrives while it sits -- 14 refusals on
    # 2026-09-01 were a single $103.63 reservation blocking 4663 for 71 minutes. Counting refusals
    # therefore measures signal arrival rate, not severity.
    #
    # Worse, release is not time-bounded. The row frees when the ACCOUNT NONCE advances past it, and
    # the nonce advances when another transaction confirms -- but entries are exactly what the gate
    # suppresses. Exits and top-ups still advance it, so it is not a deadlock, but recovery time is
    # set by unrelated activity and a quiet chain with nothing to exit has nothing to free it with.
    #
    # So page on DURATION of the stuck row, not on the refusal count. Age comes from the ledger
    # rather than a sentinel file: a stateless check cannot lose its own start time across restarts,
    # and this ran unnoticed for 71 minutes precisely because nothing was watching for the state.
    #
    # GROUP BY THE GATE'S OWN KEY, NEVER THE RAW COLUMN. `unresolved_signed_pending_count` groups by
    # `coin_network_sql(...)`, which maps '1399811149' to 'solana' and, for an EMPTY network_id,
    # infers 'evm-unknown' from a 0x mint and 'solana' otherwise. This table holds 30 rows with a
    # NULL network_id, every one a solana mint. Grouping by the raw column instead would have made
    # the alarm and the gate key on different things, with two consequences -- and the second is the
    # one that matters:
    #   * the gate blocks SOLANA while the alarm pages about a chain named `None`, which nobody can
    #     act on at 4am; and
    #   * if a NULL-network row and a 'solana' row stick together, the gate sees ONE blocked chain
    #     starting at the EARLIER of the two, while a raw-column GROUP BY splits them and reports
    #     solana's age as the min of only the labelled rows. The alarm would fire LATE -- which is
    #     precisely the failure it exists to prevent.
    # The prefixed form is required: the bare one is ambiguous once a second table carrying
    # `network_id`/`mint` is in scope, and SQLite then refuses the whole statement.
    from sniper.persistence.db_memecoin import coin_network_sql
    net_sql = coin_network_sql("memecoin_pending_entries.")
    stuck = c.execute(
        f"SELECT {net_sql} AS net, COUNT(*) n, MIN(reserved_at) since FROM memecoin_pending_entries "
        "WHERE status='reserved' AND (tx_sig IS NOT NULL OR cleanup_tx_sig IS NOT NULL "
        "      OR signed_payload_sha256 IS NOT NULL OR broadcast_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE p.is_open=1 "
        "                AND p.execution_mode='real' AND p.attempt_id=memecoin_pending_entries.attempt_id "
        "                AND memecoin_pending_entries.attempt_id IS NOT NULL) "
        "GROUP BY 1"
    ).fetchall()
    blocked = []
    for r in stuck:
        age_row = c.execute("SELECT CAST((julianday('now')-julianday(?))*1440 AS INT) m",
                            (r["since"],)).fetchone()
        mins = age_row["m"] if age_row and age_row["m"] is not None else 0
        blocked.append((r["net"], r["n"], mins, r["since"]))

    import time as _time
    health = runner_health(ROOT / "data" / "memecoin_runner.heartbeat", now_s=_time.time())

    out = [f"memecoin overnight watch — last {args.since_minutes}m (window stated; counts are not rates)"]
    out.append(f"  open funded now: {open_now}   fills in window: {filled}")
    for line in health:
        out.append(f"  *** {line}")
    for net, n, mins, since in blocked:
        flag = "  *** ENTRY BREAKER ***" if mins >= args.stuck_minutes else ""
        out.append(f"  ENTRIES BLOCKED on {net}: {n} unresolved signed reservation(s), "
                   f"oldest {mins}m (since {str(since)[:19]}){flag}")
    for label, rows in (("OPENED", opened), ("CLOSED", closed)):
        for r in rows:
            extra = f" ${r['usd']}" if label == "OPENED" else f" pnl ${r['pnl']}"
            out.append(f"  {label} {r['network_id']}: {r['n']}{extra}")
    for name in ("SYSTEM", "UNKNOWN", "POLICY"):
        b = buckets[name]
        total = sum(b.values())
        out.append(f"  {name} refusals: {total}")
        if name != "POLICY":                       # POLICY is expected; do not enumerate it
            for k, v in sorted(b.items(), key=lambda kv: -kv[1])[:8]:
                out.append(f"      {v:>5}  {k}")

    # --- sections added 2026-09-22. Each is fail-isolated: the cron runs this file from the repo
    # checkout, so a commit is live with no deploy gate, and a throwing section must never take the
    # refusal report with it. Each prints its own window and the rule that would page.
    pages = []
    # THE SAME ISO-T BOUND THE REST OF THIS FILE PROVES. Resolved through SQLite so the new
    # sections cannot reintroduce the space-separator bug documented above.
    _b = lambda mod: c.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?))", (mod,)).fetchone()[0]
    since_iso = _b(win)
    day_iso = _b("-1440 minutes")
    now_s = _time.time()
    try:
        failed, warn = failed_transactions(c, since_iso)
        out.append(f"  FAILED TRANSACTIONS (last {args.since_minutes}m; pages on any but a "
                   f"min-out revert): {len(failed or [])}")
        for row in (failed or [])[:10]:
            out.append(failed_line(row))
        if warn:
            out.append(f"      [partial] {warn}")
        pages.extend(failed_pages(failed))
    except Exception as exc:
        out.append(f"  FAILED TRANSACTIONS unavailable: {exc}")

    try:
        win, day, run = outcomes(c, since_iso, day_iso)
        net24 = sum(v["pnl"] for v in day.values())
        out.append(f"  OUTCOMES (last {args.since_minutes}m | 24h net ${net24:+.2f}; pages when "
                   f"24h net first drops below -$150 and each further -$50, and when a 3-loss "
                   f"streak on a chain forms or grows)")
        for label, tab in (("window", win), ("24h", day)):
            for net, v in sorted(tab.items()):
                wr = (100.0 * v["w"] / v["n"]) if v["n"] else 0.0
                out.append(f"      {label:>6} {net:<7} closes {v['n']:>3}  W{v['w']}/L{v['l']}  "
                           f"win {wr:4.0f}%  ${v['pnl']:+.2f}")
        pages.extend(pnl24_pages(net24, str(STATE_PATH)))
        # LATCHED: a persisting streak is reported in the table above every run, but it only PAGES
        # when it forms, grows, or starts again after a win.
        pages.extend(streak_pages(run, loss_streak_starts(c, day_iso), str(STATE_PATH)))
    except Exception as exc:
        out.append(f"  OUTCOMES unavailable: {exc}")

    try:
        feeds, warn, priced, resolved = observer_health(
            ROOT / "data" / "memecoin_onchain_volume.db",
            str(ROOT / "logs" / "memecoin_onchain_volume.log"),
            now_s=now_s, window_s=args.since_minutes * 60)
        out.append("  OBSERVER (pages on feed dark >120s)")
        for chain, v in sorted(feeds.items()):
            age = v.get("feed_age_s")
            out.append(f"      {chain:<7} feed_age {age if age is None else round(age,1)}s  "
                       f"covered {round(v.get('covered_s') or 0,1)}s  resets24h {v.get('resets_24h')}")
            if age is not None and age > 120:
                pages.append(f"{chain} volume feed dark {age:.0f}s")
        if priced:
            out.append(f"      pricer: {priced}")
        else:
            out.append("      pricer: no 'hop prices:' line in the log tail")
        if resolved:
            out.append(f"      v3:     {resolved}")
        else:
            out.append("      v3:     no 'v3 pools resolved' line in the log tail")
        unres, uerr = unresolved_pools(ROOT / "data" / "memecoin_onchain_volume.db")
        if uerr:
            out.append(f"      unresolved pools: unavailable: {uerr}")
        else:
            pairs = unres.get(42, 0)
            v4ids = unres.get(66, 0)
            out.append(f"      unresolved: {pairs} V2/V3 pairs (42-char) and {v4ids} V4 ids "
                       f"(66-char) have ticks but no pool_meta")
        if warn:
            out.append(f"      [partial] {warn}")
    except Exception as exc:
        out.append(f"  OBSERVER unavailable: {exc}")

    try:
        logs = [str(ROOT / "logs" / "memecoin_runner.log"),
                str(ROOT / "logs" / "memecoin_onchain_volume.log"),
                str(ROOT / "logs" / "memecoin_sol_tape.log")]
        tbs, errs = new_tracebacks(logs, str(STATE_PATH))
        rows = traceback_rows(tbs)
        out.append(f"  NEW TRACEBACKS since last run: {len(tbs)} in {len(rows)} kind(s) "
                   f"(pages on a kind not seen in 24h)")
        for row in rows[:8]:
            out.append(row)
        for e in errs:
            out.append(f"      [partial] {e}")
        pages.extend(traceback_pages(tbs, str(STATE_PATH), now_s=now_s))
    except Exception as exc:
        out.append(f"  TRACEBACKS unavailable: {exc}")

    breaker = [b for b in blocked if b[2] >= args.stuck_minutes]
    # A blocked chain receiving no signals produces ZERO refusals, so the breaker must be
    # able to page on its own. Gating it behind a refusal count would go silent in exactly
    # the quiet stretch where recovery takes longest.
    # EVERY TRIGGER IS NAMED (2026-09-28). The page was titled "ACTIONABLE refusals" and listed
    # only `pages`, so a page fired by a SYSTEM refusal or a runner alarm never said why.
    triggers = []
    for name in ("SYSTEM", "UNKNOWN"):
        n = sum(buckets[name].values())
        if n:
            top = max(buckets[name], key=buckets[name].get)
            triggers.append(f"{n} {name} refusal(s) · {top}")
    triggers += [f"entries blocked on {net} {mins}m" for net, _n, mins, _s in breaker]
    triggers += [line[:80] for line in health]
    triggers += pages
    actionable = len(triggers)
    if triggers:
        out.append("  PAGING BECAUSE: " + "; ".join(triggers))
    # Keep the diagnostic report in the local log; Telegram carries only the triggered faults.
    text = "\n".join(out)
    print(text)
    if args.page and actionable:
        try:
            from sniper.util.envfiles import load_env_files
            load_env_files(str(ROOT))
            from sniper.services.memecoin_telegram import send_telegram
            send_telegram(page_title(triggers, since_minutes=args.since_minutes))
        except Exception as e:                     # a page failure must never mask the report
            print(f"  [warn] telegram page failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
