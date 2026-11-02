"""Read-only decision projection for the legacy multi-arm memecoin paper engine."""
from __future__ import annotations

import json
import math
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_policy import paper_policy_is_valid


CHAIN_IDS = {
    "all": (),
    "solana": ("solana",),
    "base": ("8453", "base"),
    "robin": ("4663", "robin"),
    "bsc": ("56", "bsc"),
    "eth": ("1", "eth", "ethereum"),
}
MARK_STALE_S = 120.0

# Exactly the columns this module reads. `SELECT p.*` pulled all 91 and cost 1550ms of the
# 1610ms report on the live ledger (22.8k paper rows) — almost all of it in row->dict
# materialisation, not the scan. Projecting to these 24 measured 366ms.
_PROJECT_COLS = (
    "id", "origin_event_id", "paper_cohort_id", "paper_cohort_json", "assigned_policy_json",
    "experiment_status", "strategy", "network_id", "mint", "ticker", "source_user_id",
    "source_handle", "is_open", "opened_at", "closed_at", "entry_price", "entry_notional",
    "entry_fill_source", "last_quote_price", "last_quote_ts", "realized_pnl", "exit_reason",
    "signal_source", "time_to_peak_s",
)
# Non-executable fill sources come from the persistence layer so the censor here and the orphan
# recovery in memecoin_exits can never drift apart. Excluding only `noquote` (the pre-2026-08-03
# behaviour) rendered four 4663 STONKBROKER arms as known -$2.24 outcomes on a chain where no
# executable exit quote has ever existed.
_NON_EXEC_SQL = "('" + "','".join(m.NON_EXECUTABLE_FILL_SOURCES) + "')"

# Row keys the API exposes. Anything added here must also be projected, or it silently
# serialises as null.
_VIEW_COLS = (
    "id", "origin_event_id", "paper_cohort_id", "experiment_status", "strategy",
    "network_id", "mint", "ticker", "source_user_id", "source_handle", "is_open",
    "opened_at", "closed_at", "entry_price", "entry_notional", "entry_fill_source",
    "last_quote_price", "last_quote_ts", "realized_pnl", "exit_reason", "signal_source",
)
assert set(_VIEW_COLS) <= set(_PROJECT_COLS), "row view exposes an unprojected column"


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def _project_rows(conn, chain):
    entry_marks = ",".join("?" for _ in m.ENTRY_KINDS)
    exit_marks = ",".join("?" for _ in m.EXIT_KINDS)
    chain_ids = CHAIN_IDS[chain]
    chain_sql = ""
    params = [
        *m.ENTRY_KINDS, *m.EXIT_KINDS, *m.EXIT_KINDS,
        *m.EXIT_KINDS, *m.EXIT_KINDS,
    ]
    if chain_ids:
        chain_sql = f" AND LOWER(COALESCE(p.network_id,'')) IN ({','.join('?' for _ in chain_ids)})"
        params.extend(chain_ids)
    # Corrections retain the original fills for audit. Exclude only the named rejected fill.
    corrected = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='memecoin_paper_pnl_corrections'",
    ).fetchone()
    correction_join = ("LEFT JOIN memecoin_paper_pnl_corrections correction "
                       "ON correction.position_id=p.id AND correction.booked_exit_fill_id=f.id "
                       "AND correction.reason='implausible_exit_quote'") if corrected else ""
    observed_filter = "AND correction.position_id IS NULL" if corrected else ""
    fill_price = ("CASE WHEN correction.position_id IS NOT NULL THEN p.entry_price ELSE f.price END"
                  if corrected else "f.price")
    rows = conn.execute(
        f"""
        SELECT {','.join('p.' + column for column in _PROJECT_COLS)},
               COALESCE(SUM(CASE WHEN f.kind IN ({entry_marks}) THEN f.qty ELSE 0 END), 0)
                   AS report_entry_qty,
               COALESCE(SUM(CASE WHEN f.kind IN ({exit_marks}) THEN f.qty ELSE 0 END), 0)
                   AS report_exit_qty,
               COALESCE(SUM(CASE WHEN f.kind IN ({exit_marks}) THEN ({fill_price}) * f.qty ELSE 0 END), 0)
                   AS report_exit_proceeds,
               COALESCE(SUM(CASE WHEN f.kind IN ({exit_marks})
                                 AND f.fill_source NOT IN {_NON_EXEC_SQL} {observed_filter}
                                 THEN f.qty ELSE 0 END), 0) AS report_observed_exit_qty,
               COALESCE(SUM(CASE WHEN f.kind IN ({exit_marks})
                                 AND f.fill_source NOT IN {_NON_EXEC_SQL} {observed_filter}
                                 THEN f.price * f.qty ELSE 0 END), 0)
                   AS report_observed_exit_proceeds,
               COALESCE(SUM(CASE WHEN f.fill_source='fomo' THEN 1 ELSE 0 END), 0)
                   AS report_fomo_fills
        FROM memecoin_positions p
        LEFT JOIN memecoin_fills f ON f.position_id=p.id
        {correction_join}
        WHERE p.execution_mode='paper'{chain_sql}
        GROUP BY p.id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _add_outcome(row, now):
    entry = float(row.get("entry_price") or 0.0)
    entered = float(row.get("report_entry_qty") or 0.0)
    missing_entry_fill = entered <= 0
    exited = float(row.get("report_exit_qty") or 0.0)
    remaining = max(0.0, entered - exited)
    booked = float(row.get("report_exit_proceeds") or 0.0) - entry * exited
    observed_exited = float(row.get("report_observed_exit_qty") or 0.0)
    observed_remaining = max(0.0, entered - observed_exited)
    observed_booked = (
        float(row.get("report_observed_exit_proceeds") or 0.0) - entry * observed_exited
    )
    reason = str(row.get("exit_reason") or "")
    # A CLOSED position that exited entirely on non-executable legs was never observed at a price
    # we could transact, whatever its reason string says. This is the load-bearing test: it catches
    # `unquotable` closes carrying a stale `cleanup` mark (which DO have a last_quote_price, so the
    # reason-string test below misses them) and orphan recoveries whose only booked leg was
    # `noquote`. Reason strings are a label; the fills are the evidence.
    nonexecutable_exit = (
        not row.get("is_open") and exited > 0 and observed_exited <= 0
    )
    synthetic_noquote = reason.endswith("_noquote") or nonexecutable_exit or (
        reason == "unquotable" and not row.get("last_quote_price")
    )
    if missing_entry_fill:
        outcome = conservative = None
        state = "invalid_missing_entry_fill"
    elif not row.get("is_open") and exited <= 0:
        # A CLOSED position with no exit fill at all is the orphan-close malformation
        # (`recover_orphaned_paper_closes` exists for it): `realized_pnl` is stored with nothing
        # backing it. Previously this fell through and was scored as a measured outcome — the
        # 2026-08-03 audit found exactly that booking phantom $0.00 closes. Omitted, not written
        # off: with no exit leg we cannot claim the stake was lost either. (2026-08-04 review:
        # made fail-closed for funded evidence, and mirrored in `measurable_outcome_sql`.)
        outcome = conservative = None
        state = "invalid_missing_exit_fill"
    elif not row.get("is_open") and synthetic_noquote:
        outcome = None
        conservative = observed_booked - entry * observed_remaining
        state = "censored_noquote"
    elif not row.get("is_open"):
        outcome = row.get("realized_pnl")
        outcome = float(outcome) if outcome is not None else None
        conservative = outcome
        state = "closed" if outcome is not None else "censored"
    else:
        mark = row.get("last_quote_price")
        mark = float(mark) if mark is not None and float(mark) > 0 else None
        outcome = booked + (mark - entry) * remaining if mark is not None and entry > 0 else None
        conservative = outcome if outcome is not None else booked - entry * remaining
        state = "marked_open" if outcome is not None else "unpriced_open"
    quote_at = _parse(row.get("last_quote_ts"))
    quote_age_s = max(0.0, (now - quote_at).total_seconds()) if quote_at else None
    row.update(
        report_pnl=outcome,
        report_conservative_pnl=conservative,
        report_booked_pnl=booked,
        report_open_qty=remaining,
        report_outcome_state=state,
        report_quote_age_s=quote_age_s,
        report_mark_stale=(quote_age_s is not None and quote_age_s > MARK_STALE_S),
        report_missing_entry_fill=missing_entry_fill,
        report_synthetic_noquote=synthetic_noquote,
        # `_price_censored` rebuilds a priced bound as conservative + price * this quantity, so the
        # two bounds can never be built from different quantities.
        report_observed_open_qty=observed_remaining,
    )
    return row


ROUTE_SEAM = "exit_decision"    # ONE seam. Pooling seams prices different quantities at
                                # different moments, which is the mixture the design spec forbids.


def _price_censored(conn, rows, now):
    """Put a LABELLED mark-space bound on the `*_noquote` closes §3 censors, BESIDE the censor.

    WHY THIS IS NOT A FABRICATED OUTCOME. `report_pnl` stays None on every one of these rows and the
    primary cohort mean never sees this number, so "an unpriceable event is no evidence" still holds.
    What changes is that the censored band gets a second labelled end. The conservative end already
    written by `_add_outcome` writes the remaining bag off in full; this end values it at the last
    price we actually saw. Neither is the outcome; the outcome is unobserved.

    THE PRICE IS A MID, AND IT IS BIASED HIGH ON EXACTLY THIS POPULATION. The writer is
    `db_memecoin.update_position_last_quote`, called from `memecoin_exits` with a DexScreener/GT
    `mid` -- its docstring states it exists so a time cap can still close at a real price when the
    mint later goes unquotable. The design spec measured a mid running 2.4% above an executable route
    on a settled coin and 7.5-11% above at entry, and these rows are rugged or dumping coins, where
    the gap is worst. So `priced` is the OPTIMISTIC end of the band, never a neutral estimate.

    A ROUTE QUOTE IS A COVERAGE FLAG, NOT A PRICE (2026-09-22, Alpha's scope call).
    `memecoin_route_series` is the only route-space series we have, but its `unit_price` is
    `expected_usdc / open_qty` for a FUNDED position's bag, not this row's. A funded exit read of a
    different quantity is not this row's fill, and a bound built on it would be read as a price. So
    the nearest observation is recorded as a signed distance and nothing else.

    MEASURED COVERAGE. The report projects the WHOLE ledger, so read the all-history row, and read
    the recent window beside it because the two disagree on Solana for a reason that matters.

    All history, 6,931 censored closes (live run 2026-09-22T22:16Z):
      chain    censored  priced from a mark   route observation seen   neither
      solana   5,527     1,303  (23.6%)       1,202                    3,105
      4663      1,318    1,234  (93.6%)          98                       76
      56           76        4   (5.3%)          14                       58
      8453         10        2  (20.0%)           0                        8
      ALL      6,931     2,543  (36.7%)       1,314                    3,247

    Forward-cohort window only, closes since 2026-08-29 (n=3,006): solana 44 of 2,568 = 1.7%,
    4663 322 of 352 = 91.5%, ALL 372 = 12.4%, neither 1,962 = 65.3%.

    THE TWO WINDOWS DISAGREE BECAUSE THE ARM ROSTER CHANGED, NOT BECAUSE ANYTHING BROKE. An earlier
    version of this docstring called the September fall a regression in the mark writer. That was
    WRONG and is retracted. The writer is unchanged: `git log -S"update_position_last_quote"` shows
    no call site removed at any date, and the one commit in the window that touched it (706661b1,
    2026-08-29) ADDED a call.

    The cause is the exit reason. Mark share on Solana censored closes by reason:
      unquotable_noquote   Aug   164 rows, 0 marked = 0.0%  |  Sep 2,386 rows, 0 marked = 0.0%
      a_timecap_noquote    Aug   386 rows, 0 marked = 0.0%
      every other reason   Aug   60-100% marked (d_stop, d_trail, e_stop, f_stop, mirror,
                           a_rollover, real_trail and the *_stale family are all 100%)
    `unquotable_noquote` has never carried a mark in either month, and it is 2,386 of September's
    2,430 Solana censored closes (98%) against 164 of 1,314 in August. Having no mark is what that
    reason MEANS -- the sweep could not get a quote at all -- so there is nothing to record.

    The reason mix moved because the roster did: the eight lab arms stopped 2026-08-23, and the two
    arms that replaced them on 08-29 (funded_clone, profile_matched_v1, 1,215 censored rows each at
    1.8%) close almost entirely on `unquotable`. So the low recent Solana share is STRUCTURAL for the
    current arms and is a real ceiling on this pass, not a defect waiting to be fixed.

    SCOPE, and it limits what this pass buys on Solana: neither of those two arms is in
    MEMECOIN_ARMS, so the primary forward cohort excludes them by construction. The September Solana
    censoring sits almost entirely outside the section this pass informs. The 4663 coverage (93.6%)
    is where it earns its place.

    OBSERVABLE BUT NOT PRICED, BY DECISION (Alpha, 2026-09-22). 890 of the 2,386 unmarked September
    Solana rows (37.3%) DO have a mid in `memecoin_quote_ledger` within 10 minutes of the close,
    because that lane and the position sweep have different coverage. It was considered as a third
    source and REFUSED, for three reasons worth keeping so nobody rebuilds it:
      1. a mid from another lane is not this row's price -- the sweep that owned this position could
         not quote it, which is what `unquotable` records;
      2. on a dumping coin a 10-minute-old mid is the stale-HIGH end of a band we already label
         optimistic, so it would widen the bias it appears to reduce;
      3. it prices rows the primary forward cohort never scores, since neither arm is in
         MEMECOIN_ARMS.
    The ceiling stands as measured. Do not wire this in without revisiting all three.

    CONSEQUENCE FOR A READER. This pass closes the censoring on 4663 (93.6%) and does almost nothing
    on RECENT Solana -- and Solana is where the lane's sign is undecided across a band of about
    $130k. A reader must not take the priced subset for a resolution; `censored_priced_by_chain`
    carries the split so the surface can say so.

    Mark age is 0 s at p10, median and p90 (n=372): the sweep stamps `last_quote_ts` with its own
    clock in the same pass that closes the row, so the mark is contemporaneous, not stale. The
    nearest route observation is a median 5,915 s BEFORE the close (p10 -516,881, p90 +84,550),
    which is the second reason it cannot price the row.
    """
    censored = [row for row in rows if row.get("report_outcome_state") == "censored_noquote"]
    for row in censored:
        row.update(report_priced_pnl=None, report_priced_source=None,
                   report_priced_age_s=None, report_route_observed_within_s=None)
    if not censored:
        return rows
    mints = sorted({str(row.get("mint")) for row in censored if row.get("mint")})
    series = defaultdict(list)
    # ONE batched read. A per-row query here would be 3,000 scans of a 116k-row table -- the
    # correlated-subquery load pattern the research-load rule forbids on this host.
    # Keyed by (chain, mint): the same 0x address exists on several EVM chains, and a Base quote
    # must not read as coverage of a Robinhood row (Codex review 2026-09-23, R5).
    for chunk in (mints[i:i + 900] for i in range(0, len(mints), 900)):
        placeholders = ",".join("?" for _ in chunk)
        for network_id, mint, observed_at in conn.execute(
                "SELECT network_id, mint, observed_at FROM memecoin_route_series "
                f"WHERE seam=? AND unit_price IS NOT NULL AND mint IN ({placeholders})",
                (ROUTE_SEAM, *chunk)):
            stamp = _parse(observed_at)
            if stamp is not None:
                series[(str(network_id), str(mint))].append(stamp)
    for values in series.values():
        values.sort()
    for row in censored:
        closed = _parse(row.get("closed_at"))
        mark = row.get("last_quote_price")
        mark = float(mark) if mark is not None and float(mark) > 0 else None
        if mark is not None and row.get("report_conservative_pnl") is not None:
            row["report_priced_pnl"] = (float(row["report_conservative_pnl"])
                                        + mark * float(row.get("report_observed_open_qty") or 0.0))
            row["report_priced_source"] = "mark"
            marked_at = _parse(row.get("last_quote_ts"))
            if closed is not None and marked_at is not None:
                # Positive means the mark is OLDER than the close.
                row["report_priced_age_s"] = (closed - marked_at).total_seconds()
        stamps = series.get((str(row.get("network_id")), str(row.get("mint"))))
        if stamps and closed is not None:
            index = bisect_left(stamps, closed)
            nearest = None
            for candidate in (stamps[index] if index < len(stamps) else None,
                              stamps[index - 1] if index > 0 else None):
                if candidate is None:
                    continue
                delta = (candidate - closed).total_seconds()
                if nearest is None or abs(delta) < abs(nearest):
                    nearest = delta
            # Positive means the observation is AFTER the close. Coverage only, never a price.
            row["report_route_observed_within_s"] = nearest
    return rows


def wallet_arm_cells_from_forward_cohort(conn, arms, chain):
    """Per-wallet, per-arm PnL over the ACTIVE forward cohort: {uid: {arm: {pnl, closed, wins}}}.

    This is the funded selector's evidence source, and it is deliberately NOT `trader_arm_matrix`.
    That matrix derives a wallet's arm set AFTER censor filtering and then compares whichever arms
    survive — missing-not-at-random selection, because an arm that produces more censored outcomes
    removes itself from the competition and cannot lose. Selecting funded exit rails from that is
    selecting on the censoring mechanism.

    Here every requirement holds simultaneously (2026-08-04 review):
      1. active `paper_cohort_id` only,
      2. exactly one valid row per CONFIGURED arm on each origin (`_exact_groups`),
      3. identical frozen cohort/roster identity (`_cohort_roster == set(arms)`),
      4. the whole origin is dropped when ANY arm is missing, censored, invalid or duplicated.
    So every arm is scored over an identical set of origins, and a censored outcome removes the
    EVENT from all arms rather than one arm from the comparison.
    """
    from sniper.util.clock import real_utc_now

    arms = tuple(dict.fromkeys(str(arm) for arm in arms if arm))
    now = datetime.fromtimestamp(real_utc_now(), timezone.utc)
    rows = [_add_outcome(row, now) for row in _project_rows(conn, chain)]
    candidates = [
        row for row in rows
        if row.get("paper_cohort_id") and row.get("experiment_status") == "eligible"
        and not row.get("report_missing_entry_fill") and not row.get("report_fomo_fills")
        and row.get("entry_fill_source") != "fomo" and paper_policy_is_valid(row)
        and _cohort_roster(row) == set(arms)
    ]
    if not candidates:
        return {}
    active = max(
        candidates, key=lambda row: (str(row.get("opened_at") or ""), int(row["id"])),
    ).get("paper_cohort_id")
    cells: dict = {}
    for key, group in _exact_groups(rows, arms, forward=True):
        if key[0] != active:
            continue
        # Requirement 4. A censored or still-open row on ANY arm invalidates the whole origin —
        # scoring the rest would compare arms over different event sets, which is the bias this
        # function exists to remove.
        if any(row.get("is_open") or row["report_pnl"] is None for row in group):
            continue
        uid = group[0].get("source_user_id")
        if not uid:
            continue
        wallet = cells.setdefault(uid, {})
        for row in group:
            cell = wallet.setdefault(row["strategy"], {"pnl": 0.0, "closed": 0, "wins": 0})
            cell["pnl"] += float(row["report_pnl"])
            cell["closed"] += 1
            cell["wins"] += float(row["report_pnl"]) > 0
    return cells


def open_exposure_by_wallet(conn, chain):
    """Per-wallet OPEN marked-to-market exposure on one chain: {uid: {...}}.

    The trader arm matrix is built entirely from CLOSED `realized_pnl`, so on a chain whose losses
    live in open inventory it shows nothing — or worse, the opposite sign. Live robin: the paper
    report is ~-$1,017 across 143 open rows, while the matrix shows +$218 from 10 closes, and the
    three largest losers have no closed row at all so they are absent from the table entirely.

    Exposed from HERE rather than recomputed in SQL so the mark model has exactly one definition
    (`_add_outcome`) and the two surfaces cannot drift into disagreeing about the same position.
    """
    from sniper.util.clock import real_utc_now

    now = datetime.fromtimestamp(real_utc_now(), timezone.utc)
    out = {}
    for row in _project_rows(conn, chain):
        if not row.get("is_open"):
            continue
        row = _add_outcome(row, now)
        uid = row.get("source_user_id")
        if not uid:
            continue
        entry = out.setdefault(uid, {
            # None, not 0.0: a wallet whose open positions are ALL unpriced knows nothing about
            # its exposure, and 0.0 would render as breakeven — the same "no evidence reads as
            # neutral" failure the realized/unrealized split exists to stop.
            "handle": row.get("source_handle"), "open_positions": 0,
            "open_marked_pnl": None, "open_unpriced": 0, "open_stale": 0,
        })
        entry["open_positions"] += 1
        if row["report_pnl"] is None:
            entry["open_unpriced"] += 1
        else:
            entry["open_marked_pnl"] = (entry["open_marked_pnl"] or 0.0) + float(row["report_pnl"])
        if row.get("report_mark_stale"):
            entry["open_stale"] += 1
        if entry["handle"] is None:
            entry["handle"] = row.get("source_handle")
    for entry in out.values():
        if entry["open_marked_pnl"] is not None:
            entry["open_marked_pnl"] = round(entry["open_marked_pnl"], 2)
    return out


def _cohort_roster(row):
    try:
        contract = json.loads(row.get("paper_cohort_json") or "")
        entries = contract.get("arms")
        if not isinstance(entries, list):
            return None
        roster = frozenset(
            entry.get("strategy") for entry in entries
            if isinstance(entry, dict) and entry.get("strategy")
        )
        return roster if len(roster) == len(entries) else None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _exact_groups(rows, arms, *, forward):
    grouped = defaultdict(list)
    for row in rows:
        event = row.get("origin_event_id")
        cohort = row.get("paper_cohort_id")
        if not event:
            continue
        if forward:
            if not cohort:
                continue
            grouped[(cohort, event)].append(row)
        elif row.get("experiment_status") in (None, "", "legacy_unversioned"):
            grouped[event].append(row)
    matches = []
    armset = set(arms)
    for key, group in grouped.items():
        by_arm = defaultdict(list)
        for row in group:
            if row.get("strategy") in armset:
                by_arm[row["strategy"]].append(row)
        if any(len(by_arm[arm]) != 1 for arm in arms):
            continue
        selected = [by_arm[arm][0] for arm in arms]
        if any(row.get("report_missing_entry_fill") for row in selected):
            continue
        if forward and any(_cohort_roster(row) != armset for row in selected):
            continue
        if any(row.get("report_fomo_fills") or row.get("entry_fill_source") == "fomo"
               for row in selected):
            continue
        if forward and any(
            row.get("experiment_status") != "eligible" or not paper_policy_is_valid(row)
            for row in selected
        ):
            continue
        matches.append((key, selected))
    return matches


def _arm_summary(matches, arms):
    buckets = {arm: [] for arm in arms}
    for _key, rows in matches:
        for row in rows:
            buckets[row["strategy"]].append(row)
    result = []
    for arm in arms:
        rows = buckets[arm]
        outcomes = [row["report_pnl"] for row in rows if row["report_pnl"] is not None]
        conservative = [row["report_conservative_pnl"] for row in rows
                        if row["report_conservative_pnl"] is not None]
        closed = [row for row in rows if not row.get("is_open")]
        closed_known = [row for row in closed if row["report_pnl"] is not None]
        # REALIZED vs UNREALIZED (2026-08-04). `total_pnl` sums both, which is fine on a chain
        # with real closes and actively misleading on one without: on 4663 EVERY closed row is
        # censored or fomo-excluded, so the arm's headline PnL was 100% open marked-to-market
        # with no completed trade behind it, rendered identically to solana's grounded blend.
        # Split them so a mark can never read as a track record.
        open_rows = [row for row in rows if row.get("is_open")]
        open_known = [row for row in open_rows if row["report_pnl"] is not None]
        holds = []
        for row in closed:
            opened, ended = _parse(row.get("opened_at")), _parse(row.get("closed_at"))
            if opened and ended and ended >= opened:
                holds.append((ended - opened).total_seconds())
        result.append({
            "strategy": arm,
            "matched_events": len(matches),
            "opened": len(rows),
            "closed": len(closed),
            "open_count": len(rows) - len(closed),
            "known_outcomes": len(outcomes),
            "censored_outcomes": len(rows) - len(outcomes),
            # The two halves of `censored_outcomes`, which meant different things under one name:
            # a CLOSED position we could never price is permanently unmeasurable evidence, an OPEN
            # one without a fresh mark is just not priced yet and may resolve on the next sweep.
            "closed_censored": len(closed) - len(closed_known),
            "open_unpriced": len(open_rows) - len(open_known),
            # Realized = completed trades. Unrealized = open marks. `total_pnl` remains their sum
            # for compatibility, but a reader can now tell which it is — and `realized_pnl` is None
            # (not 0.0) when nothing closed measurably, so "no evidence" cannot read as "breakeven".
            "closed_known": len(closed_known),
            "realized_pnl": (round(sum(row["report_pnl"] for row in closed_known), 2)
                             if closed_known else None),
            # NOT "unrealized" (corrected 2026-08-04): an open row's report_pnl is
            # `booked + (mark - entry) * remaining` — booked is REALIZED proceeds from partial
            # exits already taken. Live robin carries $150 of booked PnL across 9 open rows, so
            # calling the total unrealized was inexact. `open_marked_pnl` is the honest name, and
            # `open_booked_pnl` exposes the realized component rather than hiding it inside.
            "open_marked_pnl": (round(sum(row["report_pnl"] for row in open_known), 2)
                                if open_known else None),
            "open_booked_pnl": (
                round(sum(float(row.get("report_booked_pnl") or 0.0) for row in open_known), 2)
                if open_known else None),
            "total_pnl": round(sum(outcomes), 2) if outcomes else None,
            "conservative_total_pnl": round(sum(conservative), 2) if conservative else None,
            "win_rate": (sum(row["report_pnl"] > 0 for row in closed_known) / len(closed_known)
                         if closed_known else None),
            "median_hold_s": statistics.median(holds) if holds else None,
            "peak_in_first_sweep_rate": (
                sum(float(row["time_to_peak_s"]) <= 15.0 for row in closed
                    if row.get("time_to_peak_s") is not None)
                / sum(row.get("time_to_peak_s") is not None for row in closed)
                if any(row.get("time_to_peak_s") is not None for row in closed) else None
            ),
        })
    return result


def _drop_top_mean(deltas, count):
    """Mean after removing the `count` largest PROFITABLE deltas. None when nothing is left."""
    kept = sorted(deltas, reverse=True)
    while count > 0 and kept and kept[0] > 0:
        kept.pop(0)
        count -= 1
    return round(statistics.mean(kept), 4) if kept else None


def _paired(matches, arms):
    if not arms:
        return []
    control = "control" if "control" in arms else arms[0]
    output = []
    for challenger in arms:
        if challenger == control:
            continue
        deltas, closed_deltas, coins, wallets = [], [], set(), set()
        for _key, rows in matches:
            by_arm = {row["strategy"]: row for row in rows}
            base, other = by_arm[control], by_arm[challenger]
            if base["report_pnl"] is None or other["report_pnl"] is None:
                continue
            delta = other["report_pnl"] - base["report_pnl"]
            deltas.append(delta)
            if not base.get("is_open") and not other.get("is_open"):
                closed_deltas.append(delta)
            coins.add((other.get("network_id"), other.get("mint")))
            wallets.add(other.get("source_user_id"))
        absolute = sorted((abs(value) for value in deltas), reverse=True)
        absolute_total = sum(absolute)
        output.append({
            "control": control,
            "challenger": challenger,
            "paired_outcomes": len(deltas),
            "mean_delta": round(statistics.mean(deltas), 4) if deltas else None,
            "median_delta": round(statistics.median(deltas), 4) if deltas else None,
            "p10_delta": round(_percentile(deltas, 0.10), 4) if deltas else None,
            "p90_delta": round(_percentile(deltas, 0.90), 4) if deltas else None,
            "worst_delta": round(min(deltas), 4) if deltas else None,
            "best_delta": round(max(deltas), 4) if deltas else None,
            "top_two_abs_concentration": (
                round(sum(absolute[:2]) / absolute_total, 4) if absolute_total else None
            ),
            # A mean carried by one or two winners is not an effect. These are the same mean
            # with the biggest profitable event(s) removed; if the sign flips, the mean was
            # the outlier.
            "mean_delta_drop_top1": _drop_top_mean(deltas, 1),
            "mean_delta_drop_top2": _drop_top_mean(deltas, 2),
            # Open rows are marked, not realised. Closed-only is the primary read.
            "closed_paired_outcomes": len(closed_deltas),
            "closed_mean_delta": (
                round(statistics.mean(closed_deltas), 4) if closed_deltas else None
            ),
            "unique_coins": len(coins),
            "unique_wallets": len(wallets),
        })
    return output


def _fill_model_label(matches):
    """Describe the cohort's ACTUAL entry pricing instead of asserting one.

    Until 2026-07-30 this was hardcoded "modeled-fill / not an executable route quote",
    which became false the moment era-2 entries started pricing off Jupiter route quotes.
    Deriving it from the rows means the label cannot drift from the evidence again.
    """
    sources = {row.get("entry_fill_source") for _key, group in matches for row in group}
    sources.discard(None)
    if sources == {"route_quote"}:
        return ("Forward matched executable-route cohort",
                "entries are priced by an executable route quote (entry_fill_source="
                "route_quote); exact route identity is not retained on the position row, "
                "so this is fill-source provenance, not exact-route provenance")
    if sources and "route_quote" not in sources:
        return ("Forward matched modeled-fill cohort",
                "entries are DexScreener mid + modeled impact, not an executable route quote")
    if not sources:
        return ("Forward matched cohort", "no entries yet")
    return ("Forward matched mixed-fill cohort",
            "this cohort mixes executable-route and modeled fills; do not compare across them")


def _section(matches, arms):
    from sniper.settings.memecoin import MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS
    coins = {(row.get("network_id"), row.get("mint")) for _key, group in matches for row in group}
    wallets = {row.get("source_user_id") for _key, group in matches for row in group}
    # Conservative cluster floor: repeated events on ONE coin or ONE wallet are not
    # independent evidence, so the smallest of the three bounds what this sample can say.
    clusters = min(len(matches), len(coins), len(wallets))
    arm_rows = _arm_summary(matches, arms)
    # A chain can accumulate matched events, open positions and a headline PnL while having NEVER
    # completed one measurable trade — 4663 does exactly that: it is DexScreener-blind, so every
    # close books either a `noquote`/`cleanup` leg (censored) or a fomo-priced mirror fill
    # (excluded), and the arm PnL is 100% open marks. Stated here rather than left for the reader
    # to derive from closed == censored across every row.
    realized_closes = sum(int(row["closed_known"]) for row in arm_rows)
    # The WEAKEST arm's measurable evidence. A cross-arm comparison cannot rank an arm that has
    # never produced one measurable close, however many clusters the cohort has accumulated —
    # so this, not the cluster count alone, decides whether a grade may read "sufficient".
    min_arm_closed_known = min((int(row["closed_known"]) for row in arm_rows), default=0)
    return {
        "matched_events": len(matches),
        "unique_coins": len(coins),
        "unique_wallets": len(wallets),
        "evidence_clusters": clusters,
        "realized_closes": realized_closes,
        "pnl_is_all_unrealized": bool(matches) and realized_closes == 0,
        "min_evidence_clusters": MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS,
        "min_arm_closed_known": min_arm_closed_known,
        # Clusters alone were the whole grade, so a blind chain that accrued 30 clusters would
        # read `sufficient_sample` on ZERO measurable outcomes — 4663 is on that trajectory today
        # (33 matched events, 0 measurable closes). Sample size and measurability are different
        # questions and both must pass; a cohort where any arm has never closed measurably gets
        # its own grade rather than being folded into "insufficient", which reads as "wait longer"
        # when the real answer is "this chain cannot price its own exits".
        "decision_grade": "blocked_protocol",
        "research_eligibility": {"eligible": False, "codes": ["protocol_missing_or_legacy"],
                                 "basis": "legacy_sampled_diagnostic"},
        "arms": arm_rows,
        "paired": _paired(matches, arms),
    }


def _row_view(row):
    return {key: row.get(key) for key in _VIEW_COLS} | {
        "booked_pnl": row.get("report_booked_pnl"),
        "open_qty": row.get("report_open_qty"),
        "full_book_pnl": row.get("report_pnl"),
        "conservative_pnl": row.get("report_conservative_pnl"),
        # The optimistic end of a censored row's band, and the labels that say what it is worth.
        "priced_pnl": row.get("report_priced_pnl"),
        "priced_source": row.get("report_priced_source"),
        "priced_age_s": row.get("report_priced_age_s"),
        "route_observed_within_s": row.get("report_route_observed_within_s"),
        "outcome_state": row.get("report_outcome_state"),
        "quote_age_s": row.get("report_quote_age_s"),
        "mark_stale": row.get("report_mark_stale"),
    }


def _censored_price_coverage(rows):
    """Counts for the censored band, with the per-chain split that stops a misreading.

    On 4663 the mark prices 91.5% of the censored closes; on Solana it prices 1.7%. A single overall
    percentage hides that completely, and Solana is the chain whose lane sign is undecided. Any
    surface that shows a priced censored figure must show `censored_priced_by_chain` beside it.
    """
    censored = [row for row in rows if row.get("report_outcome_state") == "censored_noquote"]
    by_chain = {}
    for row in censored:
        cell = by_chain.setdefault(str(row.get("network_id") or "unknown"),
                                   {"censored": 0, "priced_mark": 0, "route_observed": 0,
                                    "unpriced": 0})
        cell["censored"] += 1
        priced = row.get("report_priced_pnl") is not None
        observed = row.get("report_route_observed_within_s") is not None
        cell["priced_mark"] += bool(priced)
        cell["route_observed"] += bool(observed)
        cell["unpriced"] += not (priced or observed)
    return {
        "censored_noquote": len(censored),
        "censored_priced_mark": sum(row.get("report_priced_pnl") is not None for row in censored),
        "censored_route_observed": sum(
            row.get("report_route_observed_within_s") is not None for row in censored),
        "censored_unpriced": sum(
            row.get("report_priced_pnl") is None
            and row.get("report_route_observed_within_s") is None for row in censored),
        "censored_priced_by_chain": by_chain,
    }


def build_paper_report(conn, arms, chain=None, episode_limit=200, closed_limit=200):
    """Project the complete scoped paper ledger once; never fetch a live quote."""
    chain = (chain or "all").strip().lower()
    if chain not in CHAIN_IDS:
        raise ValueError(f"invalid chain: {chain}")
    arms = tuple(dict.fromkeys(str(arm) for arm in arms if arm))
    from sniper.util.clock import real_utc_now
    now = datetime.fromtimestamp(real_utc_now(), timezone.utc)
    rows = [_add_outcome(row, now) for row in _project_rows(conn, chain)]
    _price_censored(conn, rows, now)
    all_forward_matches = _exact_groups(rows, arms, forward=True)
    forward_candidates = [
        row for row in rows
        if row.get("paper_cohort_id") and row.get("experiment_status") == "eligible"
        and not row.get("report_missing_entry_fill") and not row.get("report_fomo_fills")
        and row.get("entry_fill_source") != "fomo" and paper_policy_is_valid(row)
        and _cohort_roster(row) == set(arms)
    ]
    active_cohort = None
    if forward_candidates:
        active_cohort = max(
            forward_candidates,
            key=lambda row: (str(row.get("opened_at") or ""), int(row["id"])),
        ).get("paper_cohort_id")
    forward_matches = [
        match for match in all_forward_matches if match[0][0] == active_cohort
    ]
    legacy_matches = _exact_groups(rows, arms, forward=False)
    event_rows = [row for row in rows if row.get("origin_event_id")]
    event_ids = {row["origin_event_id"] for row in event_rows}
    open_rows = [row for row in rows if row.get("is_open")]
    coverage = {
        "positions": len(rows),
        "unique_events": len(event_ids),
        "matched_events": len(forward_matches),
        "excluded_fill": sum(
            row.get("experiment_status") == "excluded_fill"
            or row.get("report_fomo_fills")
            or row.get("entry_fill_source") == "fomo"
            for row in rows
        ),
        "censored_outcomes": sum(row["report_pnl"] is None for row in rows),
        "operator_modified": sum(row.get("experiment_status") == "operator_modified" for row in rows),
        "manual": sum(row.get("experiment_status") == "manual" for row in rows),
        "invalid_policy": sum(
            row.get("experiment_status") == "invalid_policy"
            or (row.get("experiment_status") in ("eligible", "excluded_fill")
                and not paper_policy_is_valid(row))
            for row in rows
        ),
        "missing_entry_fill": sum(bool(row.get("report_missing_entry_fill")) for row in rows),
        "forward_cohorts": len({
            row.get("paper_cohort_id") for row in forward_candidates
            if row.get("paper_cohort_id")
        }),
        "legacy_rows": sum(
            row.get("experiment_status") in (None, "", "legacy_unversioned") for row in rows
        ),
        "open_fresh": sum(row["report_quote_age_s"] is not None and not row["report_mark_stale"]
                          for row in open_rows),
        "open_stale": sum(bool(row["report_mark_stale"]) for row in open_rows),
        "open_unpriced": sum(row["report_quote_age_s"] is None for row in open_rows),
        **_censored_price_coverage(rows),
    }
    by_event = defaultdict(list)
    for row in event_rows:
        by_event[row["origin_event_id"]].append(row)
    ranked_all = sorted(
        by_event,
        key=lambda event: max(str(row.get("closed_at") or row.get("opened_at") or "")
                              for row in by_event[event]),
        reverse=True,
    )
    # Episodes are the Strategy Lab surface, which renders only events with a live arm. Padding
    # the list out to episode_limit with fully-closed events shipped ~440 KiB per poll that the
    # UI filtered straight back out; the Closed view reads `closed`, not this.
    open_events = [event for event in ranked_all if any(
        row.get("is_open") for row in by_event[event]
    )]
    episode_limit = max(0, int(episode_limit))
    ranked_events = open_events[:episode_limit]
    coverage["open_episodes_truncated"] = max(0, len(open_events) - len(ranked_events))
    forward_keys = {key for key, _rows in forward_matches}
    episodes = [{
        "origin_event_id": event,
        "forward_matched": any(
            (row.get("paper_cohort_id"), event) in forward_keys for row in by_event[event]
        ),
        "latest_at": max(str(row.get("closed_at") or row.get("opened_at") or "")
                         for row in by_event[event]),
        "arms": [_row_view(row) for row in sorted(
            by_event[event], key=lambda item: (str(item.get("strategy")), int(item["id"])))],
    } for event in ranked_events]
    closed = sorted(
        (row for row in rows if not row.get("is_open")),
        key=lambda row: (str(row.get("closed_at") or ""), int(row["id"])), reverse=True,
    )[:max(0, int(closed_limit))]
    source_counts = Counter((row.get("signal_source") or "unknown") for row in rows)
    fill_counts = Counter((row.get("entry_fill_source") or "unknown") for row in rows)
    return {
        "generated_at": now.isoformat(),
        "chain": chain,
        "configured_arms": list(arms),
        "coverage": coverage,
        "forward": {
            # Label + caveat are DERIVED from the rows' entry_fill_source, so the surface can
            # never claim a fill model the evidence does not have (audit 2026-07-30).
            "label": _fill_model_label(forward_matches)[0],
            "fill_model_note": _fill_model_label(forward_matches)[1],
            "cohort_id": active_cohort,
            **_section(forward_matches, arms),
        },
        "legacy": {
            "label": "Legacy matched diagnostic - mutable/unversioned policy",
            **_section(legacy_matches, arms),
        },
        "sources": {
            "signal_source": [{"source": key, "positions": value}
                              for key, value in sorted(source_counts.items())],
            "entry_fill_source": [{"source": key, "positions": value}
                                  for key, value in sorted(fill_counts.items())],
        },
        "episodes": episodes,
        "closed": [_row_view(row) for row in closed],
    }
