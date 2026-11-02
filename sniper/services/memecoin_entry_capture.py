"""Entry feature capture, phase P1: zero network (spec 2026-09-29-memecoin-entry-feature-capture-design.md).

One feature row per TERMINAL entry attempt, fills and refusals alike, computed after the decision
from committed ledger rows and our own swap tape, written to the capture's own database. The funded
entry path never imports this module (a test reads their source to prove it).

Every feature is a value with coverage "ok", or null with a typed reason: "unavailable:<why>",
"unsupported_chain", "not_attempted:t1" (a network feature P1 does not run). A guessed 0 is never
written: "no trades in the window" is 0 only when the tape proves it saw the window.

CLOCKS (Gamma's review of the spec, LOW 5). The decision anchor is `decision_started_at`; our own
receipt stamps (`memecoin_events.received_at`, `memecoin_entry_attempts.observed_at`) share its
clock (observed_real_utc). A relay's `created_at` does NOT: on 4663 it ran 23 s ahead of our receipt
on 09-29. So every history read keeps the feed's created_at window AND requires received_at <= the
decision: nothing we had not received at the decision is counted. Tape windows are in chain block
time (market truth). The quote-asset USD mark is latest-only, so its signed offset from the
decision is stored in read_lag_json.

WINDOWS (review Q1). Decision-anchored (decision-N, decision] for N = 30/60/300; source-anchored
[src_t-N, src_t) for the same N, which excludes the source's whole block; and the copier queue
(src_t, decision], which starts at the block AFTER the source's. Buyers inside the source's own
block after it are `same_block_buyers_after_source`. src_t is the source transaction's block time
when the tape holds it, else our receipt time, and the anchor is labelled.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_entry_features as store
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_volume as ov

logger = logging.getLogger("memecoin")

FEATURE_VERSION = 1
WINDOWS_S = (30, 60, 300)
TAPE_CHAINS = frozenset({"4663", "8453"})     # the chains our volume tape subscribes today
# The quote-asset USD mark is latest-only (refreshed about every 30 s). A capture that runs late (a
# restart's backlog) reads a mark newer than its decision; up to 30 min of ETH drift (about 1%) is
# kept, with the signed offset in read_lag_json, rather than dropping the row. The tape itself keeps
# only an hour.
MAX_PRICE_LAG_S = 1800.0
LOOKBACK_S = 900.0                             # re-read behind the cursor for late commits
# The tape trails the chain by about 1 s on 4663 and 1-2 s on 8453 (Gamma, 2026-09-29), and a fresh
# attempt is read within seconds of its decision. A window read before the tape has passed the
# decision is not a finding about the coin, so the attempt is left for a later pass (the lookback
# re-reads it) until the tape covers it. Only a tape still behind after TAPE_WAIT_S is written as
# 'tape_behind'. It must stay well inside LOOKBACK_S, or a waiting attempt leaves the re-read window
# without a row.
TAPE_WAIT_S = 600.0
PAGE_ROWS = 200
SIZE_HISTORY_DAYS = 30


class Deferred(Exception):
    """This attempt cannot be read correctly yet; no row is written and a later pass retries it."""


def _ts(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class _Row:
    """The four maps of one capture row, filled one feature at a time."""

    def __init__(self):
        self.features, self.coverage, self.sources, self.lags = {}, {}, {}, {}

    def put(self, name, value, coverage, source, lag=None):
        self.features[name] = value if coverage == "ok" else None
        self.coverage[name] = coverage
        self.sources[name] = source
        if lag is not None:
            self.lags[name] = round(float(lag), 3)


def _facts(row):
    try:
        return (json.loads(row.get("classification_json") or "{}") or {}).get("facts") or {}
    except (TypeError, ValueError):
        return {}


def _substages(row):
    """{name: the LAST substage of that name} from the attempt's trace."""
    try:
        events = (json.loads(row.get("trace_json") or "{}") or {}).get("substage_events") or []
    except (TypeError, ValueError):
        return {}
    return {e.get("name"): e for e in events if isinstance(e, dict) and e.get("name")}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


# ------------------------------------------------------------------------------ ledger features

def _source_context(ledger, row, rec):
    source, decision = row.get("source_user_id"), row.get("decision_started_at")
    if source and decision:
        n, total, wins = m.source_record_before(ledger, source, decision)
        rec.put("src_prior_n", n, "ok", "ledger")
        rec.put("src_prior_wins", wins, "ok", "ledger")
        rec.features["src_prior_sum"] = total                 # None with n=0: no record, not $0
        rec.coverage["src_prior_sum"], rec.sources["src_prior_sum"] = "ok", "ledger"
    else:
        for name in ("src_prior_n", "src_prior_wins", "src_prior_sum"):
            rec.put(name, None, "unavailable:no_source_or_decision_time", "ledger")

    event_time = row.get("source_created_at")
    size = _number(m.source_event_size_usd(ledger, row.get("origin_event_id")))
    rec.put("src_size_usd", size, "ok" if size is not None else "unavailable", "ledger")
    if not (source and _ts(event_time)):
        for name in ("src_prior_buys_mint", "src_size_median_30d", "src_size_over_median"):
            rec.put(name, None, "unavailable", "ledger")
        return
    since = _iso(_ts(event_time) - SIZE_HISTORY_DAYS * 86400)
    buys = m.source_buys_between(ledger, source, since, event_time,
                                 received_before_iso=row.get("decision_started_at"))
    same = [b for b in buys if str(b.get("mint") or "").lower() == str(row.get("mint") or "").lower()
            and str(b.get("network_id") or "") == str(row.get("network_id") or "")]
    rec.put("src_prior_buys_mint", len(same), "ok", "ledger")
    sizes = [v for v in (_number(b.get("their_size_usd")) for b in buys) if v and v > 0]
    median = statistics.median(sizes) if sizes else None
    rec.put("src_size_median_30d", median, "ok" if median else "unavailable:no_size_history",
            "ledger")
    ratio = size / median if (size and median) else None
    rec.put("src_size_over_median", ratio, "ok" if ratio is not None else "unavailable", "ledger")


def _consensus(ledger, row, rec):
    """The runner computes this only while its gate is active; here, for every attempt, counting
    only buys we had received by the decision. The runner's own number is kept beside it (when it
    computed one) so the two can be compared."""
    try:
        n = m.prior_buyers_60m_causal(ledger, mint=row.get("mint"), network_id=row.get("network_id"),
                                      source_user_id=row.get("source_user_id"),
                                      before_iso=row.get("source_created_at"),
                                      received_before_iso=row.get("decision_started_at"))
    except Exception as exc:                         # recorded, never raised
        rec.put("consensus_n_60m", None, "error:%s" % type(exc).__name__, "ledger")
        n = "error"
    if n != "error":
        rec.put("consensus_n_60m", n, "ok" if n is not None else "unavailable", "ledger")
    runner = _number(_facts(row).get("consensus_n_60m"))
    rec.put("consensus_n_60m_runner", runner,
            "ok" if runner is not None else "not_applicable:runner_did_not_compute", "ledger")


def _drift_and_chase(row, rec):
    filled = row.get("terminal_status") == "filled"
    for name in ("source_drift_pct", "execution_slippage_pct", "total_copy_drift_pct"):
        value = _number(row.get(name))
        rec.put(name, value, "ok" if value is not None else
                ("unavailable" if filled else "not_applicable:not_filled"), "ledger")
    for name in ("chase_naive_premium_pct", "chase_effective_cap_pct", "chase_base_cap_pct"):
        value = _number(row.get(name))
        rec.put(name, value, "ok" if value is not None else "unavailable", "ledger")
    # The chase numbers live in different places by lane (review LOW 4): the display chase writes
    # columns only when refusing and, since P1, a `chase_display_check` substage when passing; the
    # executor's exact-quote gate (Solana onchain_watch, EVM) writes `chase_quote_confirmation`;
    # the slot lane writes neither. Coverage says which one answered.
    subs = _substages(row)
    display = _number(row.get("chase_naive_premium_pct"))
    source = "ledger"
    if display is None and "chase_display_check" in subs:
        display, source = _number(subs["chase_display_check"].get("gap_pct")), "trace"
    rec.put("chase_display_gap_pct", display, "ok" if display is not None else "unavailable",
            source)
    quote_gap = _number((subs.get("chase_quote_confirmation") or {}).get("gap_pct"))
    rec.put("chase_quote_gap_pct", quote_gap, "ok" if quote_gap is not None else "unavailable",
            "trace")
    facts = _facts(row)
    stored = {k: v for k, v in facts.items() if _number(v) is not None}
    rec.put("stored_facts", stored, "ok" if stored else "unavailable", "stored_stats")


# ------------------------------------------------------------------------------ EVM tape flow

def _price_at(tape, chain, token, at_ts):
    """(usd, signed offset observed - decision in s) of a quote token, or (None, why). The mark is
    latest-only, so the offset is recorded rather than called 'at the decision'."""
    token = str(token).lower()
    if token in ov._usd_stables(chain):
        return 1.0, 0.0
    row = tape.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? AND token=?",
                       (chain, token)).fetchone()
    if not row or _number(row[0]) is None or _number(row[1]) is None or float(row[0]) <= 0:
        return None, "quote_not_priceable"
    offset = float(row[1]) - float(at_ts)
    if abs(offset) > MAX_PRICE_LAG_S:
        return None, "quote_price_stale"
    return float(row[0]), offset


# block_times.block_ts is ISO TEXT on the live tape and a number in older stores; read both as
# unix seconds, exactly as memecoin_onchain_volume._swaps does.
_BLOCK_TS = ("(CASE WHEN typeof(block_ts) IN ('integer','real') THEN block_ts "
             "ELSE unixepoch(block_ts) END)")


def _window_problem(tape, chain, lo, hi):
    """None when the tape demonstrably saw (lo, hi], else why not.

    RETENTION (review MED 2): the writer prunes pool_ticks and block_times older than an hour
    and leaves no tick_gaps row for it, so a window outside the retained blocks would read as
    '0 trades'. The window must lie inside the retained block range. Ticks are pruned by their
    observed time, which is at or after their block's, so the block range is the conservative
    bound."""
    span = tape.execute(
        f"SELECT MIN({_BLOCK_TS}), MAX({_BLOCK_TS}) FROM block_times WHERE chain_id=?",
        (chain,)).fetchone()
    if not span or span[0] is None or float(span[0]) > lo:
        return "tape_retention"
    # Blocks share whole-second stamps: only a block stamped AFTER hi proves every block at or
    # before hi is in. _order_flow defers until then; this is the backstop for any other caller.
    if float(span[1]) <= hi:
        return "tape_behind"
    blocks = tape.execute(f"SELECT MIN(block_no), MAX(block_no) FROM block_times WHERE chain_id=? "
                          f"AND {_BLOCK_TS} > ? AND {_BLOCK_TS} <= ?", (chain, lo, hi)).fetchone()
    if blocks and blocks[0] is not None and tape.execute(
            "SELECT 1 FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL "
            "AND from_block <= ? AND to_block >= ? LIMIT 1",
            (chain, blocks[1], blocks[0])).fetchone():
        return "tape_gap"
    for (at,) in tape.execute("SELECT at FROM volume_feed_resets WHERE chain_id=?", (chain,)):
        t = _ts(at)
        if t is not None and lo < t <= hi:
            return "feed_reset"
    return None


class _Flow:
    def __init__(self):
        self.buy_n = self.sell_n = 0
        self.buy_usd = self.sell_usd = 0.0
        self.largest_buy_usd = 0.0
        self.makers = set()
        self.source_buy_usd = 0.0

    def add(self, signed, usd, maker, is_source):
        if signed > 0:
            self.buy_n += 1
            self.buy_usd += usd
            self.largest_buy_usd = max(self.largest_buy_usd, usd)
            if is_source:
                self.source_buy_usd += usd
        elif signed < 0:
            self.sell_n += 1
            self.sell_usd += usd
        if maker:
            self.makers.add(str(maker).lower())

    def as_dict(self, with_source):
        return {"buy_n": self.buy_n, "sell_n": self.sell_n,
                "buy_usd": round(self.buy_usd, 6), "sell_usd": round(self.sell_usd, 6),
                "net_flow_usd": round(self.buy_usd - self.sell_usd, 6),
                # The tape's maker is the swap's sender, often a router: a LOWER bound on traders.
                "unique_makers": len(self.makers),
                "largest_buy_usd": round(self.largest_buy_usd, 6),
                "source_buy_usd": round(self.source_buy_usd, 6) if with_source else None,
                "source_share_of_buys": (round(self.source_buy_usd / self.buy_usd, 6)
                                         if with_source and self.buy_usd > 0 else None)}


class _Tape:
    """One coin on one chain's tape: its covered pools, the source trade, our own trades."""

    def __init__(self, tape, chain, mint, source_tx, own, price_ts):
        self.tape, self.chain, self.mint = tape, chain, str(mint).lower()
        self.source = str(source_tx or "").lower() or None
        self.own, self.price_ts = own, price_ts
        self.pools = [p["pool"] for p in ov._pool_protocols(tape, chain, self.mint)
                      if p["protocol"] in ov.covered_protocols(chain, tape)]
        self.source_block = None               # (block_no, log_index, block_ts)
        if self.pools and self.source:
            marks = ",".join("?" * len(self.pools))
            row = tape.execute(
                f"SELECT t.block_no, MIN(t.log_index), {_BLOCK_TS.replace('block_ts', 'b.block_ts')} "
                f"FROM pool_ticks t JOIN block_times b ON b.chain_id=t.chain_id "
                f"AND b.block_no=t.block_no AND lower(b.block_hash)=lower(t.block_hash) "
                f"WHERE t.chain_id=? AND t.pool IN ({marks}) AND lower(t.tx_sig)=? "
                f"GROUP BY t.block_no", (chain, *self.pools, self.source)).fetchone()
            if row and row[0] is not None:
                self.source_block = (row[0], row[1], float(row[2]))

    def problem(self):
        """Why no window on this coin can be read, or None."""
        if not self.pools:
            return "no_known_pool"
        if self.source and self.source_block is None:
            # The source trade certainly happened; a tape without it is blind to the venue the
            # source used (memecoin_onchain_volume rule 3).
            return "source_not_on_tape"
        return None

    def window(self, lo, hi):
        """(flow, flow_ex_source, max |price offset|, reason) over (lo, hi]."""
        problem = _window_problem(self.tape, self.chain, lo, hi)
        if problem:
            return None, None, None, problem
        rows, _orphans, unverified = ov._swaps(self.tape, self.chain, self.pools, lo, hi)
        if unverified:
            return None, None, None, "hash_unverified"
        meta = {r["pool"]: r for r in self.tape.execute(
            "SELECT pool, token0, token1, quote_side, token0_decimals, token1_decimals "
            "FROM pool_meta WHERE chain_id=? AND pool IN (%s)" % ",".join("?" * len(self.pools)),
            (self.chain, *self.pools))}
        txs = [str(r["tx_sig"]) for r in rows] or [""]
        makers = {(r["pool"], str(r["tx_sig"]).lower()): r["maker"] for r in self.tape.execute(
            "SELECT pool, tx_sig, maker FROM pool_ticks WHERE chain_id=? AND pool IN (%s) "
            "AND tx_sig IN (%s)" % (",".join("?" * len(self.pools)), ",".join("?" * len(txs))),
            (self.chain, *self.pools, *txs))}
        flows, worst = {"all": _Flow(), "ex_source": _Flow()}, 0.0
        for r in rows:
            tx = str(r["tx_sig"] or "").lower()
            if tx in self.own:
                continue
            pm = meta.get(r["pool"])
            if not pm or pm["quote_side"] not in (0, 1):
                return None, None, None, "quote_not_priceable"
            side = int(pm["quote_side"])
            price, offset = _price_at(self.tape, self.chain,
                                      pm["token0"] if side == 0 else pm["token1"], self.price_ts)
            if price is None:
                return None, None, None, offset
            worst = max(worst, abs(offset))
            try:
                signed = int(r["amount0"] if side == 0 else r["amount1"])
                usd = abs(signed) / (10 ** int(pm["token0_decimals"] if side == 0
                                               else pm["token1_decimals"])) * price
            except (TypeError, ValueError):
                return None, None, None, "quote_not_priceable"
            is_source = self.source is not None and tx == self.source
            flows["all"].add(signed, usd, makers.get((r["pool"], tx)), is_source)
            if not is_source:
                flows["ex_source"].add(signed, usd, makers.get((r["pool"], tx)), False)
        with_source = self.source is not None
        return flows["all"].as_dict(with_source), flows["ex_source"].as_dict(False), worst, None

    def same_block_after_source(self):
        if self.source_block is None:
            return None
        block, log_index, _ts_ = self.source_block
        marks = ",".join("?" * len(self.pools))
        n = 0
        for r in self.tape.execute(
                f"SELECT t.tx_sig, t.amount0, t.amount1, m.quote_side FROM pool_ticks t "
                f"JOIN pool_meta m ON m.chain_id=t.chain_id AND m.pool=t.pool "
                f"WHERE t.chain_id=? AND t.pool IN ({marks}) AND t.block_no=? AND t.log_index>? "
                f"AND lower(t.tx_sig)!=?", (self.chain, *self.pools, block, log_index, self.source)):
            if str(r["tx_sig"]).lower() in self.own or r["quote_side"] not in (0, 1):
                continue
            try:
                if int(r["amount0"] if r["quote_side"] == 0 else r["amount1"]) > 0:
                    n += 1
            except (TypeError, ValueError):
                continue
        return n


def _flow_names():
    names = [f"flow_{w}s" for w in WINDOWS_S] + [f"flow_ex_source_{w}s" for w in WINDOWS_S]
    names += [f"pre_source_{w}s" for w in WINDOWS_S] + ["copier_queue"]
    return names + ["same_block_buyers_after_source", "source_anchor"]


def _order_flow(ledger, tapes, row, rec, now_ts):
    chain = str(row.get("network_id") or "")

    def all_flow(coverage, source="evm_tape"):
        for name in _flow_names():
            rec.put(name, None, coverage, source)

    if chain == "solana":
        # P2: a T1/T2 attempt's Solana flow is filled later by the T1 worker when its switch is on
        # (memecoin_entry_capture_sol); T0 rows are never T1 (review Q3).
        from sniper.services import memecoin_entry_capture_sol as sol
        return all_flow(sol.PENDING if sol.enabled() and tier_of(row) >= 1 else "not_attempted:t1",
                        "helius_gtfa")
    if chain not in TAPE_CHAINS:
        return all_flow("unsupported_chain")
    tape = tapes.get(chain)
    if tape is None:
        return all_flow("unavailable:tape_missing")
    decision = _ts(row.get("decision_started_at"))
    if decision is None:
        return all_flow("unavailable:no_decision_time")
    head = tape.execute(f"SELECT MAX({_BLOCK_TS}) FROM block_times WHERE chain_id=?",
                        (chain,)).fetchone()[0]
    if head is None or float(head) <= decision:
        if now_ts is not None and now_ts - decision < TAPE_WAIT_S:
            raise Deferred("tape_behind")
        return all_flow("unavailable:tape_behind")
    source_tx = m.source_tx_of(row.get("origin_event_id"))
    own = m.own_tx_hashes(ledger, chain, row.get("mint"))
    if source_tx:
        own.discard(source_tx.lower())
    coin = _Tape(tape, chain, row.get("mint"), source_tx, own, decision)
    problem = coin.problem()
    if problem:
        for name in _flow_names():
            rec.put(name, None, "unavailable:%s" % problem, "evm_tape")
        return

    def put_window(name, lo, hi, *, ex_name=None):
        try:
            flow, ex_source, worst, why = coin.window(lo, hi)
        except Exception as exc:
            logger.exception("entry capture: flow failed attempt=%s", row.get("attempt_id"))
            flow, ex_source, worst, why = None, None, None, "error:%s" % type(exc).__name__
        coverage = "ok" if why is None else (why if why.startswith("error:")
                                             else "unavailable:%s" % why)
        rec.put(name, flow if ex_name else ex_source, coverage, "evm_tape:block_time", lag=worst)
        if ex_name:
            rec.put(ex_name, ex_source, coverage, "evm_tape:block_time", lag=worst)

    for w in WINDOWS_S:
        put_window(f"flow_{w}s", decision - w, decision, ex_name=f"flow_ex_source_{w}s")
    # The source-anchored windows and the copier queue (review Q1).
    if coin.source_block is not None:
        src_t, anchor = coin.source_block[2], "block_time"
    else:
        src_t, anchor = _ts(row.get("observed_at")), "received_at"
    rec.put("source_anchor", anchor if src_t is not None else None,
            "ok" if src_t is not None else "unavailable", "evm_tape")
    if src_t is None:
        for name in [f"pre_source_{w}s" for w in WINDOWS_S] + ["copier_queue"]:
            rec.put(name, None, "unavailable:no_source_time", "evm_tape")
    else:
        for w in WINDOWS_S:
            # [src_t - w, src_t): the tape's windows are (lo, hi], so the source's own block is cut
            # by ending just before it.
            put_window(f"pre_source_{w}s", src_t - w - 1e-6, src_t - 1e-6)
        put_window("copier_queue", src_t, decision)
    n = coin.same_block_after_source()
    rec.put("same_block_buyers_after_source", n, "ok" if n is not None else
            "unavailable:no_source_tx" if not source_tx else "unavailable:source_not_on_tape",
            "evm_tape")


# ------------------------------------------------------------------------------ the row and loop

def tier_of(row):
    if row.get("terminal_status") == "filled":
        return 2
    reason = str(row.get("terminal_reason") or "")
    if reason in ("not_approved", "quiet_hours") or reason.startswith(("disarmed", "not_approved")):
        return 0
    return 1


def _book_at_decision(ledger, row, rec):
    """Real Solana positions open at the decision (Gamma MEDIUM on 752569c54): T1 reads wait for a
    flat book, so a row that expires waiting is more likely from a busy stretch. This lets a read
    compare covered and expired rows on the book state they were decided in."""
    if str(row.get("network_id") or "") != "solana":
        return rec.put("sol_real_open_at_decision", None, "not_applicable:chain", "ledger")
    decision = row.get("decision_started_at")
    try:
        n = m.real_positions_open_at(ledger, "solana", decision) if decision else None
    except Exception as exc:                         # recorded, never raised
        return rec.put("sol_real_open_at_decision", None, "error:%s" % type(exc).__name__, "ledger")
    rec.put("sol_real_open_at_decision", n, "ok" if n is not None else "unavailable", "ledger")


def _holders(row, rec):
    """Top-holder shares (P2b). Solana: filled later by the T1 worker when its switch is on and the
    attempt is T1/T2. EVM: no Transfer-log collector yet (review MED 2)."""
    from sniper.services import memecoin_entry_capture_sol as sol
    if str(row.get("network_id") or "") == "solana":
        cov = sol.PENDING if sol.holders_enabled() and tier_of(row) >= 1 else "not_attempted:t1"
        source = sol.HOLDER_SOURCE
    else:
        cov, source = "unavailable:no_transfer_collector", "evm_tape"
    for name in sol.HOLDER_NAMES:
        rec.put(name, None, cov, source)


def capture_attempt(ledger, tapes, row, *, now_iso):
    rec = _Row()
    _source_context(ledger, row, rec)
    _consensus(ledger, row, rec)
    _drift_and_chase(row, rec)
    _order_flow(ledger, tapes, row, rec, _ts(now_iso))
    _holders(row, rec)
    _book_at_decision(ledger, row, rec)
    return {"attempt_id": row["attempt_id"], "network_id": row.get("network_id"),
            "mint": row.get("mint"), "source_user_id": row.get("source_user_id"),
            "signal_source": row.get("signal_source"), "delivery_lane": row.get("delivery_lane"),
            "decision_at": row.get("decision_started_at"), "completed_at": row.get("completed_at"),
            "outcome": row.get("terminal_status"), "outcome_reason": row.get("terminal_reason"),
            "tier": tier_of(row), "feature_version": FEATURE_VERSION,
            "features_json": json.dumps(rec.features, sort_keys=True, default=str),
            "coverage_json": json.dumps(rec.coverage, sort_keys=True),
            "sources_json": json.dumps(rec.sources, sort_keys=True),
            "read_lag_json": json.dumps(rec.lags, sort_keys=True), "computed_at": now_iso}


def open_ledger(path=None):
    """The ledger, READ-ONLY: this process never writes the funded ledger."""
    from sniper.persistence.db_core import get_readonly_connection
    return get_readonly_connection(path)


def run_once(ledger, tapes, db, *, now_iso, page=PAGE_ROWS, stats=None):
    """Capture every terminal attempt not yet captured. Returns how many rows were written.

    Every attempt completed since cursor - LOOKBACK_S is read, keyset-paged on (completed_at,
    attempt_id) until a short page. A single fixed-size read stopped at its first page: with a page
    or more of attempts inside the lookback, the last row never passed the cursor and nothing after
    it was ever captured (Gamma MED 1, 2026-09-29). `stats`, when given, receives the deferred count."""
    cursor = store.get_cursor(db)
    since = _iso((_ts(cursor) if cursor else _ts(now_iso)) - LOOKBACK_S)
    written = deferred = 0
    after = None
    while True:
        rows = m.terminal_attempts_completed_since(ledger, since, after=after, limit=page)
        done = store.captured(db, [r["attempt_id"] for r in rows])
        for row in rows:
            if row["attempt_id"] in done:
                continue
            try:
                written += store.insert(db, capture_attempt(ledger, tapes, row, now_iso=now_iso))
            except Deferred:
                deferred += 1
            except Exception:
                logger.exception("entry capture failed attempt=%s", row.get("attempt_id"))
        if rows:
            after = (rows[-1]["completed_at"], rows[-1]["attempt_id"])
            store.set_cursor(db, after[0])
        if len(rows) < page:
            break
    if stats is not None:
        stats["deferred"] = deferred
    return written
