"""What a refused coin would have done, priced on our own EVM swap tape. MEASUREMENT ONLY.

Spec: Alpha, 2026-09-23, recorded in HANDOFF.md under "REFUSAL SHADOW LANE SPEC" (the only copy).

THE QUESTION. Should a strong source buy overturn a blocker, and should we have entered at the
FIRST refusal on a coin we entered later? Both need a price series for a coin we did NOT buy, and
no such series exists anywhere in this system today.

WHY THE TAPE AND NOT A QUOTE. `memecoin_position_marks` and `memecoin_quote_ledger` only hold a
coin once WE held or quoted it, so they cannot price a refusal we never touched. Measured
2026-09-23 on 14 days of Solana refusals: of 423 refused mints we never held, **0** carry a single
mark. Of 998 refused mints we did hold later, 700 (70.1%) do. The store follows our own positions,
exactly as the spec says. The EVM swap tape does not: it subscribes to pools, not to our book.

WHY SOLANA IS NOT SAMPLED. `data/memecoin_sol_tape.db` follows our positions' pools, so the same
hole applies there. Solana rows are written with `sampled=0` and reason `no_tape_for_chain`. They
are still WRITTEN, because the enqueue census is evidence even where the price is not.

WHY THE PRICE COMES FROM `sqrt_price_x96` AND NOT FROM THE AMOUNTS. The amounts path must pair each
pool with its own quote leg, and that pairing is the open defect in the Solana decoder (it mispairs
legs on routed swaps, so that replay stays stopped). A post-swap `sqrt_price_x96` needs no pairing:
it is the pool's own spot price after that swap, one value per tick. It is a SPOT price, not an
executed one, which is the correct input here because our own clip's movement is added on top of it
by `clip_impact_pct`.

WHAT THIS NUMBER IS NOT.
  * The fill pays impact; the modelled EXIT does not. So every PnL here is OPTIMISTIC by roughly one
    clip's impact. A family that looks good earns a replay, never a knob change.
  * `MEMECOIN_REFUSAL_SHADOW_FILL_LAG_S` (6.0) is a modelling choice, not a measurement. Chain 4663
    measured p50 4.78 s and p90 12.94 s from decision to recorded fill, so 6.0 s sits between them.
  * A position still open at the horizon is CENSORED (`exit_reason='open_at_horizon'`), never closed
    at the last bar. Closing it there would fabricate an exit the policy did not take.
  * Depth from one tick of constant liquidity is a FLOOR on impact, never a ceiling (see
    `clip_impact_pct`).
  * A pool that STOPS TRADING holds its last spot price, because a V3 spot price only moves on a
    swap. Silence is therefore modelled as a flat price, not as a fall, which again biases against
    the stop firing. A coin that became untradeable lands in `open_at_horizon` and is censored.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3

from sniper.services.memecoin_pool_depth import clip_impact_pct

logger = logging.getLogger("memecoin_refusal_shadow")

_Q96 = 2 ** 96

# THE ALLOWLIST IS EXPLICIT, AND `other:` IS NEVER ENQUEUED (2026-09-23).
#
# `refusal_family_outcomes.family()` falls back to a generated `other: ...` name for any reason it
# has no pattern for. Two things follow, and both are the point:
#   1. The RETIRED bare mcap band strings land in `other: over_ceiling` (measured: 42 Solana, 33 on
#      chain 56, 13 on 4663 at >=$700 over 14 days). Excluding every `other:` family therefore
#      excludes the retired band STRUCTURALLY, with no special case to forget. Gamma's 2026-08-26
#      spec had to correct exactly this defect: gate identity is the exact string, and a prefix
#      match merges a retired gate with a live one.
#   2. A reason that ships tomorrow is reported as unclassified instead of silently sampled.
#
# `memecoin_refusal_capture.should_capture` is NOT reusable here: `NON_CAPTURED_FAMILIES` was
# emptied on 2026-09-17 on an operator order, so it now admits `source_buy_floor` — a judgement
# about the COPIED TRADER, not the coin. Reusing it would spend this lane on roster refusals.
MARKET_FAMILIES = frozenset({
    "mcap over ceiling", "mcap under floor",
    "m5 volume under floor", "m5 volume over ceiling", "h1 volume under floor", "m5 trade count",
    "round trip over cap", "price impact over cap", "route quote vs chart mid",
    "chase: quote above source price", "chase: coin already ran",
    "coin too young", "quiet_hours",
})

# Excluded on purpose, grouped by WHY, so a later reader does not read absence as an oversight:
#   governance          not_approved, disarmed, unroutable_chain, excluded, filled, kill switch
#   our own state       drawdown pause, daily loss limit, max concurrent, one position per coin,
#                       re-entry cooldown, winner re-entry caps, global_feed entries disabled
#   plumbing / infra    no quote, no route, route build refused, below min-trade floor, entry age,
#                       source buy floors (about the trader), balances, decimals, sign failed,
#                       interrupted, preflight simulation, reverted, mcap_unresolved
#   fiction if modelled unsellable quote asset -- a modelled exit assumes a sale is possible


def is_market_family(fam) -> bool:
    """Whether this family states a fact about the COIN that a strong source buy could argue against."""
    return bool(fam) and fam in MARKET_FAMILIES


def sqrt_price_to_quote_per_token(sqrt_price_x96, *, quote_side, token0_decimals,
                                  token1_decimals):
    """Price of the base token in quote units from a post-swap `sqrt_price_x96`, or None.

    `sqrt_price_x96` encodes sqrt(token1_atoms per token0_atom). With `quote_side=1` the quote is
    token1 and the base is token0, so the atomic ratio is sqrt(P)**2; with `quote_side=0` it is the
    reciprocal.

    THE BASE TOKEN'S DECIMALS ARE OPTIONAL, AND THAT IS WHAT MAKES THIS USABLE (measured
    2026-09-23). `pool_meta` knows BOTH decimals for only 346 of 3,319 ticked pools on chain 4663
    and 33 of 1,446 on 8453 -- about 10% and 2%. It knows the QUOTE side's own decimals on 76,998
    of 76,998 and 4,018 of 4,018, i.e. always. Requiring both would have thrown away ~90% of the
    sampleable population to gain a unit label, because the 10**(base-quote) factor CANCELS out of
    every ratio this lane reports: peak percent, the take-profit and stop levels, and the PnL are
    all one pool's price divided by the same pool's price. `clip_impact_pct` needs the quote side
    only, and that is never missing. So a missing base decimal drops the factor and the row is
    labelled `atomic_ratio` by `price_basis` -- never silently mixed with a real-unit price.

    Returns None rather than 0.0 on anything unreadable: a zero would read as a measured free coin.
    """
    try:
        sqrt_p = float(sqrt_price_x96) / _Q96
        qs = int(quote_side)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sqrt_p) or sqrt_p <= 0 or qs not in (0, 1):
        return None
    base_raw, quote_raw = ((token0_decimals, token1_decimals) if qs == 1
                           else (token1_decimals, token0_decimals))
    atomic = (sqrt_p ** 2) if qs == 1 else 1.0 / (sqrt_p ** 2)
    shift = 0
    if base_raw is not None and quote_raw is not None:
        try:
            base_dec, quote_dec = int(base_raw), int(quote_raw)
        except (TypeError, ValueError):
            return None
        if not (0 <= base_dec <= 36 and 0 <= quote_dec <= 36):
            return None
        shift = base_dec - quote_dec
    try:
        price = atomic * (10 ** shift)
    except OverflowError:
        return None
    return price if math.isfinite(price) and price > 0 else None


def price_basis(quote_side, token0_decimals, token1_decimals):
    """'quote_per_token' when both decimals are known, else 'atomic_ratio'. Never guesses a unit."""
    try:
        qs = int(quote_side)
    except (TypeError, ValueError):
        return None
    base_raw, quote_raw = ((token0_decimals, token1_decimals) if qs == 1
                           else (token1_decimals, token0_decimals))
    return "quote_per_token" if base_raw is not None and quote_raw is not None else "atomic_ratio"


def bars_1s(ticks, *, quote_side, token0_decimals, token1_decimals):
    """[(epoch_second, price, liquidity, sqrt_price_x96)] at one row per second, last swap wins.

    `ticks` are (observed_epoch, sqrt_price_x96, liquidity) ordered by time. A second with several
    swaps keeps the LAST one, which is the pool's state a sweep at that second would have read.
    """
    out = {}
    for observed, sqrt_price, liquidity in ticks or ():
        price = sqrt_price_to_quote_per_token(
            sqrt_price, quote_side=quote_side,
            token0_decimals=token0_decimals, token1_decimals=token1_decimals)
        if price is None:
            continue
        try:
            sec = int(float(observed))
        except (TypeError, ValueError):
            continue
        out[sec] = (sec, price, liquidity, sqrt_price)
    return [out[k] for k in sorted(out)]


def bar_at_or_before(bars, epoch):
    """The newest bar at or before `epoch`, or None. Bars are sorted; a linear walk is enough here
    because one coin's hour of tape is thousands of rows, not millions.
    """
    found = None
    for bar in bars:
        if bar[0] <= epoch:
            found = bar
        else:
            break
    return found


def model_fill(bars, *, decision_epoch, fill_lag_s, clip_usd, quote_is_token0, quote_decimals,
               quote_usd):
    """(price_at_signal, fill_price, impact_pct) for a clip entering `fill_lag_s` after the decision.

    `price_at_signal` is the last swap before the decision. The fill takes the bar at
    decision+lag and pays `clip_impact_pct` of impact on top, because a buy moves the pool against
    itself. When the impact cannot be computed the fill is the bar price and the impact is None --
    recorded as unknown, never silently treated as zero.
    """
    at_signal = bar_at_or_before(bars, decision_epoch)
    fill_bar = bar_at_or_before(bars, decision_epoch + float(fill_lag_s))
    if fill_bar is None:
        return (at_signal[1] if at_signal else None), None, None
    impact = clip_impact_pct(
        clip_usd, liquidity=fill_bar[2], sqrt_price_x96=fill_bar[3],
        quote_is_token0=bool(quote_is_token0), quote_decimals=quote_decimals, quote_usd=quote_usd)
    fill = fill_bar[1] * (1.0 + (impact or 0.0) / 100.0)
    return (at_signal[1] if at_signal else None), fill, impact


def horizons(bars, *, fill_epoch, minutes=(1, 2, 5, 10, 30)):
    """peak/trough/last of the bar price at each horizon after the fill, as a JSON-ready dict.

    A horizon with no bar at all is absent from the result rather than zero, so the report can tell
    "the coin did not trade" from "the coin went to nothing".
    """
    out = {}
    for m in minutes:
        end = fill_epoch + m * 60
        window = [b[1] for b in bars if fill_epoch <= b[0] <= end]
        if not window:
            continue
        out[f"{m}m"] = {"peak": max(window), "trough": min(window), "last": window[-1]}
    return out


def simulate_exit(bars, *, fill_epoch, fill_price, policy, sweep_s=15.0, horizon_s=1800.0,
                  require_new_bar=False):
    """Run the LIVE funded exit policy over the tape and return the exit it would have taken.

    Reads every threshold off the `policy` object, never a literal: the id `follow-30-50-v1` was
    minted at +30/-50 and runs at +25/-35 today, so a replay keyed on the name gets the wrong
    answer. The stop needs `policy.stop_debounce_sweeps` CONSECUTIVE breaching sweeps, which is how
    the live runner confirms it while `MEMECOIN_REAL_STOP_CONFIRM_SECONDS` is 0.

    Returns (exit_price, exit_reason, seconds_after_fill). `exit_reason='open_at_horizon'` means the
    policy did not exit inside the window, and that row is CENSORED by the report -- it is not a
    close at the last bar.
    """
    if not fill_price or fill_price <= 0:
        return None, "no_fill", None
    tp = fill_price * (1.0 + float(policy.tp_pct) / 100.0)
    stop = fill_price * (1.0 - float(policy.stop_pct) / 100.0)
    floor = fill_price * (1.0 - float(policy.catastrophe_pct) / 100.0)
    need = max(1, int(getattr(policy, "stop_debounce_sweeps", 1) or 1))
    max_hold = float(getattr(policy, "max_hold_s", 0) or 0)
    breaches = 0
    t = fill_epoch + sweep_s
    last_price = None
    last_bar_epoch = None
    while t <= fill_epoch + horizon_s:
        bar = bar_at_or_before(bars, t)
        if bar is None:
            t += sweep_s
            continue
        if require_new_bar and bar[0] == last_bar_epoch:
            t += sweep_s
            continue
        last_bar_epoch = bar[0]
        price, elapsed = bar[1], t - fill_epoch
        last_price = price
        if price <= floor:
            return price, "catastrophe", elapsed
        if price >= tp:
            return price, "tp", elapsed
        if price <= stop:
            breaches += 1
            if breaches >= need:
                return price, "stop", elapsed
        else:
            breaches = 0
        if max_hold and elapsed >= max_hold:
            return price, "max_hold", elapsed
        t += sweep_s
    return last_price, "open_at_horizon", None


def pnl(fill_price, exit_price, clip_usd):
    """(pnl_usd, pnl_pct) for a clip bought at `fill_price` and sold at `exit_price`, or (None, None).

    Guards a zero fill: a zero-price fill would divide by zero, and those do occur on this tape.
    """
    try:
        fill, out, clip = float(fill_price), float(exit_price), float(clip_usd)
    except (TypeError, ValueError):
        return None, None
    if not all(math.isfinite(v) for v in (fill, out, clip)) or fill <= 0:
        return None, None
    ratio = out / fill - 1.0
    return clip * ratio, ratio * 100.0


# --- the store: its own file, its own lock, nothing else writes it -------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_refusal_shadow (
    id                  INTEGER PRIMARY KEY,
    network_id          TEXT NOT NULL,
    mint                TEXT NOT NULL,
    origin_event_id     TEXT NOT NULL,
    attempt_id          TEXT,
    family              TEXT NOT NULL,
    terminal_reason     TEXT,
    terminal_status     TEXT,
    source_size_usd     REAL,
    decision_started_at TEXT NOT NULL,
    signal_received_at  TEXT,
    enqueued_at         TEXT NOT NULL,
    sampled             INTEGER NOT NULL DEFAULT -1,
    sampled_at          TEXT,
    sample_reason       TEXT,
    pool                TEXT,
    pools_seen          INTEGER,
    ticks_used          INTEGER,
    price_basis         TEXT,
    quote_usd           REAL,
    price_at_signal     REAL,
    fill_price          REAL,
    fill_impact_pct     REAL,
    clip_usd            REAL,
    exit_price          REAL,
    exit_reason         TEXT,
    exit_after_s        REAL,
    pnl_usd             REAL,
    pnl_pct             REAL,
    horizons_json       TEXT,
    UNIQUE (network_id, mint, origin_event_id)
);
CREATE INDEX IF NOT EXISTS idx_refusal_shadow_pending
    ON memecoin_refusal_shadow (sampled, decision_started_at);
CREATE INDEX IF NOT EXISTS idx_refusal_shadow_family
    ON memecoin_refusal_shadow (family, network_id);
"""
# ponytail: the 15 peak/trough/last values live in `horizons_json`, not in 15 columns. They are
# descriptive and the report parses them in Python anyway; the PnL the report GROUPs on has its own
# columns. Promote one to a column when a SQL query actually needs it.


def connect(path, *, readonly=False):
    """Open this lane's own database. WAL, so a reader never blocks the writer."""
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def enqueue(conn, item, *, enqueued_at, sampled=-1, sample_reason=None):
    """Insert one refusal, ignoring a repeat of the same (network_id, mint, origin_event_id).

    Returns True when a row was added. NEVER RAISES: this lane must not be able to change or delay
    anything funded, and its only caller is a worker that also reads the funded ledger.
    """
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO memecoin_refusal_shadow "
            "(network_id, mint, origin_event_id, attempt_id, family, terminal_reason, "
            " terminal_status, source_size_usd, decision_started_at, signal_received_at, "
            " enqueued_at, sampled, sample_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item["network_id"], item["mint"], item["origin_event_id"], item.get("attempt_id"),
             item["family"], item.get("terminal_reason"), item.get("terminal_status"),
             item.get("source_size_usd"), item["decision_started_at"],
             item.get("signal_received_at"), enqueued_at, sampled, sample_reason))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        logger.exception("refusal shadow: enqueue failed for %s/%s",
                         item.get("network_id"), item.get("mint"))
        return False


def record_sample(conn, row_id, **fields):
    """Write one sample result once. Never raises, for the same reason `enqueue` does not."""
    cols = [k for k in (
        "sampled", "sampled_at", "sample_reason", "pool", "pools_seen", "ticks_used", "quote_usd",
        "price_basis", "price_at_signal", "fill_price", "fill_impact_pct", "clip_usd",
        "exit_price", "exit_reason",
        "exit_after_s", "pnl_usd", "pnl_pct", "horizons_json") if k in fields]
    if not cols:
        return False
    try:
        conn.execute(
            f"UPDATE memecoin_refusal_shadow SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
            [fields[c] for c in cols] + [row_id])
        conn.commit()
        return True
    except Exception:
        logger.exception("refusal shadow: sample write failed for row %s", row_id)
        return False


def dumps_horizons(h):
    return json.dumps(h, sort_keys=True) if h else None
