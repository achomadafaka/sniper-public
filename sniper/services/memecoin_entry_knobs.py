"""Operator-adjustable ENTRY knobs for the funded runner.

WHY THIS EXISTS. Every entry threshold lived in `.env`, so changing one meant editing a file the
deploy owns and restarting a release-pinned real-money process. The cockpit could read the knobs
(`/api/memecoin/real/status`.`knobs`) but not move them. This module adds one overlay the operator
can write from the cockpit, and the runner applies it once per entry decision.

THE LINE THIS KEEPS. A knob is adjustable when it judges whether the coin is a GOOD BUY. A knob is
LOCKED when it protects the capital. That is the same selection-versus-safety split
`EXEMPTIBLE_GATE_FAMILIES` already draws for per-wallet exemptions, and it is drawn here once so
the API, the UI and the runner cannot each decide it differently. A locked knob is still reported,
because an operator who cannot see a control does not know it is there — but a write naming one is
refused.

FAIL-SAFE. An override is applied only when it parses, is in the registry, is adjustable, and is
inside its declared bounds. Anything else falls back to the `.env` default. A knob with no active
override is always restored to that default, so clearing a row is a real revert and a corrupt row
can never stick to the process.
"""
import json
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ONE WRITER, AND NEVER WHILE A DECISION IS RUNNING (2026-09-08, Alpha review).
#
# `real_hook` is handed to several independent daemon threads in the runner loop — the main cycle,
# the activity worker, the fast-activity worker, the on-chain lane and the retry lane. Applying the
# overlay from inside the entry path therefore had every one of those threads writing attributes on
# the settings module.
#
# Steady state was benign, because `apply_overrides` only writes when a value actually differs. The
# window that bites is the one the feature exists for: while the operator is changing a knob, a
# decision could read threshold X from before the change and threshold Y from after it. That
# decision is then internally inconsistent, and no later reader can tell which pair it saw.
#
# Making the write single-threaded is necessary and NOT sufficient — one writer moving 28 attributes
# is still not atomic to a reader part-way through a decision. So the applier also waits for the
# runner to be idle: a decision registers itself, and an apply that finds any decision in flight is
# DEFERRED rather than half-landed. The cost is that a change waits for a quiet moment; the gain is
# that no funded decision ever straddles one.
_APPLY_LOCK = threading.Lock()
_IN_FLIGHT = 0
_ACTIVE_ORIGINS = set()
_DELIVERED_ORIGINS = OrderedDict()
_DELIVERY_TTL_S = 120.0
_DELIVERY_MAX = 4096
_PENDING_SINCE = None


@contextmanager
def entry_decision(*, origin=None, delivery=False):
    """Hold for the duration of ONE funded entry decision, so no knob change lands inside it."""
    global _IN_FLIGHT
    completed = False
    with _APPLY_LOCK:
        seen = _DELIVERED_ORIGINS.get(origin) if delivery and origin is not None else None
        duplicate = seen is not None and 0 <= time.monotonic() - seen <= _DELIVERY_TTL_S
        if seen is not None and not duplicate:
            _DELIVERED_ORIGINS.pop(origin, None)
        owner = not duplicate and (origin is None or origin not in _ACTIVE_ORIGINS)
        if owner:
            _IN_FLIGHT += 1
            if origin is not None:
                _ACTIVE_ORIGINS.add(origin)
    try:
        yield owner
        completed = True
    finally:
        with _APPLY_LOCK:
            if owner:
                _IN_FLIGHT -= 1
                if origin is not None:
                    # Publish completion before releasing the claim. A delayed twin cannot enter
                    # between those operations. Explicit retries retain the active-claim check.
                    if delivery and completed:
                        _DELIVERED_ORIGINS[origin] = time.monotonic()
                        _DELIVERED_ORIGINS.move_to_end(origin)
                        if len(_DELIVERED_ORIGINS) > _DELIVERY_MAX:
                            _DELIVERED_ORIGINS.popitem(last=False)
                    _ACTIVE_ORIGINS.remove(origin)


def pending_state():
    """{'deferred': bool, 'since': epoch|None, 'in_flight': int} — for the cockpit.

    An override that is written but not yet applied is a REAL state and the panel says so. Silently
    showing it as live would be the same class of lie as a gate that reports `applied` without
    having compared anything.
    """
    with _APPLY_LOCK:
        return {"deferred": _PENDING_SINCE is not None, "since": _PENDING_SINCE,
                "in_flight": _IN_FLIGHT}

NUMBER = "number"
BOOL = "bool"


class Knob:
    """One entry control: what it is, what may change it, and what it may be set to."""

    def __init__(self, name, *, group, label, kind, unit="", minimum=None, maximum=None,
                 gate_family=None, adjustable=True, locked_reason="", note="", off_value=None):
        self.name = name
        self.group = group
        self.label = label
        self.kind = kind
        self.unit = unit
        self.minimum = minimum
        self.maximum = maximum
        self.gate_family = gate_family
        self.adjustable = adjustable
        self.locked_reason = locked_reason
        self.note = note
        # The value that DISABLES this gate, when it has one. The cockpit's enable/disable switch
        # writes this rather than deleting the row, so "off" is a stated decision with an audit
        # trail and not an absent override that reads as the default.
        self.off_value = off_value

    def coerce(self, raw):
        """Registry-typed value, or ValueError. Bounds are part of the type, not a later check."""
        if self.kind == BOOL:
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{self.name}: not a boolean: {raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{self.name}: not a number: {raw!r}")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{self.name}: not finite")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}: {value} below minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name}: {value} above maximum {self.maximum}")
        return value

    def as_dict(self):
        return {"name": self.name, "group": self.group, "label": self.label, "kind": self.kind,
                "unit": self.unit, "min": self.minimum, "max": self.maximum,
                "gate_family": self.gate_family, "adjustable": self.adjustable,
                "locked_reason": self.locked_reason, "note": self.note,
                "off_value": self.off_value}


def _k(*a, **kw):
    return Knob(*a, **kw)


# ORDER IS THE COCKPIT ORDER. Groups run from "which coin" through "how fresh" to "what we pay".
_KNOBS = (
    # --- which coin ------------------------------------------------------------------------
    _k("MEMECOIN_REAL_MIN_MCAP_USD", group="Coin size", label="Market cap floor",
       kind=NUMBER, unit="USD", minimum=0, maximum=1e9, off_value=0,
       note="0 = no floor. Refuses coins priced under this."),
    _k("MEMECOIN_REAL_MAX_MCAP_USD", group="Coin size", label="Market cap ceiling",
       kind=NUMBER, unit="USD", minimum=0, maximum=1e10, off_value=0,
       note="With AUTO_TEMPLATES on, coins at or above the scalp cap open as scalps instead of "
            "being refused here."),
    _k("MEMECOIN_REAL_MAX_PAIR_AGE_S", group="Coin size", label="Coin age ceiling",
       kind=NUMBER, unit="s", minimum=0, maximum=31536000, gate_family="coin_age_ceiling",
       off_value=0, note="0 = off. Refuses pairs older than this."),
    _k("MEMECOIN_REAL_MICRO_POLICY_ENABLED", group="Coin size", label="Micro band admitted",
       kind=BOOL, note="Off refuses the micro band outright."),

    # --- how fresh -------------------------------------------------------------------------
    _k("MEMECOIN_REAL_ENTRY_MAX_AGE_S", group="Event freshness", label="Source event max age",
       kind=NUMBER, unit="s", minimum=1, maximum=86400,
       note="How stale a source buy may be before we refuse to copy it."),
    _k("MEMECOIN_REAL_FOLLOW_MAX_AGE_S", group="Event freshness",
       label="Follow-activity max age", kind=NUMBER, unit="s", minimum=1, maximum=604800,
       note="The follow lane's own, looser ceiling."),
    _k("MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S", group="Event freshness",
       label="Fresh-follow lag window", kind=NUMBER, unit="s", minimum=0, maximum=86400,
       note="Inside this lag the fresh-follow chase cap applies instead of the general one."),

    # --- what we chase ---------------------------------------------------------------------
    _k("MEMECOIN_REAL_CHASE_MAX_PCT", group="Price chase", label="Chase past source entry",
       kind=NUMBER, unit="%", minimum=0, maximum=500, gate_family="chase_past_entry",
       off_value=0, note="0 = cap OFF, not zero tolerance. Refuses a coin already this far above "
                         "the copied trader's entry."),
    _k("MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", group="Price chase",
       label="Chase cap, fresh follow", kind=NUMBER, unit="%", minimum=0, maximum=500,
       gate_family="chase_past_entry", off_value=0),
    _k("MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT", group="Price chase",
       label="Chase cap, nano band", kind=NUMBER, unit="%", minimum=0, maximum=500,
       gate_family="chase_past_entry", off_value=0),
    _k("MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", group="Price chase",
       label="Chase cap, 4663 nano", kind=NUMBER, unit="%", minimum=0, maximum=500,
       gate_family="chase_past_entry", off_value=0),
    _k("MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT", group="Price chase",
       label="Display drift cap, 4663 nano", kind=NUMBER, unit="%", minimum=0, maximum=500,
       off_value=0),
    _k("MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", group="Price chase",
       label="First-seen price multiple", kind=NUMBER, unit="x", minimum=0, maximum=100,
       gate_family="chase_first_seen", off_value=0,
       note="0 = off. Refuses a coin trading this many times its first-seen price."),
    _k("MEMECOIN_REAL_FIRST_SEEN_WINDOW_S", group="Price chase", label="First-seen window",
       kind=NUMBER, unit="s", minimum=0, maximum=604800, gate_family="chase_first_seen"),

    # --- is anyone else trading it ----------------------------------------------------------
    _k("MEMECOIN_REAL_MIN_TXNS_M5", group="Liquidity and turnover", label="m5 transaction floor",
       kind=NUMBER, unit="txns", minimum=0, maximum=100000, gate_family="m5_txns_under_floor",
       off_value=0),
    _k("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5", group="Liquidity and turnover",
       label="m5 turnover floor", kind=NUMBER, unit="ratio of mcap", minimum=0, maximum=100,
       gate_family="m5_turnover_under_floor", off_value=0,
       note="0.02 = 2% of market cap traded in five minutes. THE LIVE FLOOR for coins below the "
            "market-cap boundary, which is the whole funded nano/micro book. Organic m5 volume: "
            "the source purchase is subtracted first. A MISSING reading passes; a reading of zero "
            "refuses."),
    _k("MEMECOIN_REAL_MIN_TURNOVER_M5", group="Liquidity and turnover",
       label="m5 turnover floor, above band (inactive)", kind=NUMBER, unit="ratio of mcap",
       minimum=0, maximum=100,
       gate_family="m5_turnover_under_floor", off_value=0,
       note="Inactive, for the same reason as the ceiling. It is supplied only when the caller "
            "marks the block active (market cap ABOVE the boundary), and the leg that reads it "
            "runs only at or below the boundary. Above the band the over-band floor decides."),
    _k("MEMECOIN_REAL_MAX_TURNOVER_M5", group="Liquidity and turnover",
       label="m5 turnover ceiling (inactive)", kind=NUMBER, unit="ratio of mcap", minimum=0, maximum=100,
       gate_family="m5_turnover_over_ceiling", off_value=0,
       note="Inactive in funded entry. The caller supplies this m5 ceiling above the market-cap boundary, "
            "but the ceiling applies only at or below that boundary. Changing this value does not activate it."),
    _k("MEMECOIN_REAL_MIN_TURNOVER_H1", group="Liquidity and turnover", label="h1 turnover floor",
       kind=NUMBER, unit="ratio of mcap", minimum=0, maximum=100, gate_family="turnover_under_floor",
       off_value=0,
       note="Applies across market-cap bands except Solana nano/micro when its hourly floor is disabled. "
            "With known market cap, this ratio replaces the absolute h1 floor. "
            "Missing h1 data passes. Zero disables the ratio."),
    _k("MEMECOIN_REAL_MIN_VOLUME_H1_USD", group="Liquidity and turnover",
       label="h1 volume floor", kind=NUMBER, unit="USD", minimum=0, maximum=1e9, off_value=0,
       note="Applies across market-cap bands when the h1 ratio is off or market cap is unknown; "
            "Solana nano/micro can disable both hourly legs with the runner setting. "
            "Missing h1 data passes. Zero disables this fallback."),
    _k("MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", group="Liquidity and turnover",
       label="m5 turnover floor, above band", kind=NUMBER, unit="ratio of mcap",
       minimum=0, maximum=100,
       note="0.03 = 3% of market cap in five minutes. NOT the boundary itself — this is the floor "
            "APPLIED above it. Labelled 'Big-mcap boundary' with a USD unit and a 1e10 maximum "
            "until 2026-09-08, so typing the intended 2000000 would have set a 2,000,000x turnover "
            "floor and refused every above-band entry. The boundary is "
            "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD and is not adjustable here."),

    # --- who is selling ---------------------------------------------------------------------
    _k("MEMECOIN_REAL_MAX_SELL_SHARE_PCT", group="Sell pressure", label="Sell share ceiling",
       kind=NUMBER, unit="% of m5 txns", minimum=0, maximum=100, gate_family="sell_share",
       off_value=0, note="0 = off. Counts transactions, not volume. Unknown data passes."),
    _k("MEMECOIN_REAL_SELL_SHARE_MIN_TXNS", group="Sell pressure",
       label="Sell share minimum sample", kind=NUMBER, unit="txns", minimum=0, maximum=10000,
       gate_family="sell_share", note="Below this transaction count the share is not judged."),

    # --- how big was their buy --------------------------------------------------------------
    _k("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", group="Source buy",
       label="Source buy floor", kind=NUMBER, unit="ratio of mcap", minimum=0, maximum=100,
       gate_family="source_buy_floor", off_value=0,
       note="Judges the copied trader's conviction, not the coin."),
    _k("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", group="Source buy", label="Source buy floor, absolute",
       kind=NUMBER, unit="USD", minimum=0, maximum=1000000, gate_family="source_buy_floor",
       off_value=0,
       note="0 = off. Absolute dollars on the source's own reported buy, in ADDITION to the "
            "mcap ratio. Unknown size passes."),
    _k("MEMECOIN_REAL_MAX_SOURCE_BUY_USD", group="Source buy", label="Source buy ceiling, absolute",
       kind=NUMBER, unit="USD", minimum=0, maximum=10000000, gate_family="source_buy_floor",
       off_value=0,
       note="0 = off. Half of a replicated conjunction with 10-50% turnover, NOT evidence that a "
            "large buy is bad on its own — source size proxies for coin size."),
    _k("MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", group="Source buy",
       label="Source buy floor tolerance", kind=NUMBER, unit="% of the floor", minimum=0,
       maximum=50, gate_family="source_buy_floor", off_value=0,
       note="0 = off, the exact floor. Percent OF THE FLOOR, not percentage points of the mcap "
            "ratio: at 2 a $1,493 bar admits $1,463. Added after a $1,486 buy was refused by $7 "
            "at 0.797% against 0.800%."),
    _k("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", group="Source buy",
       label="Source buy floor, unknown mcap", kind=NUMBER, unit="USD", minimum=0,
       maximum=1000000, gate_family="source_buy_floor", off_value=0),

    # --- buying it again --------------------------------------------------------------------
    _k("MEMECOIN_REENTRY_WINNER_UNBLOCKED", group="Re-entry", label="Winner re-entry allowed",
       kind=BOOL, gate_family="winner_reentry_price"),
    _k("MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT", group="Re-entry",
       label="Winner re-entry price cap", kind=NUMBER, unit="% above our exit", minimum=0,
       maximum=500, gate_family="winner_reentry_price", off_value=0),
    _k("MEMECOIN_REENTRY_WINNER_DAILY_CAP", group="Re-entry", label="Winner re-entries per day",
       kind=NUMBER, unit="per day", minimum=0, maximum=100,
       gate_family="winner_reentry_daily_cap", off_value=0),

    # --- which lane may open ----------------------------------------------------------------
    _k("MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", group="Source authority",
       label="Global feed may open funded", kind=BOOL,
       note="Measured to 2026-09-06: global_feed produced 16 real fills for -$79.9; follow "
            "activity produced 394 for +$942.8."),
    _k("MEMECOIN_SLOT_LANE_CROWDED_PRIORITY_LAMPORTS", group="Source authority",
       label="Crowded priority fee (Solana slot lane, early sources)", kind=NUMBER, unit="lamports",
       minimum=0, maximum=20_000_000, off_value=0,
       note="Priority fee on an early-source slot-lane pump buy, a 1-in-4 hash holdout bids normally. "
            "Read per send; 0 = off. Priority + tip capped at MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS, "
            "optional daily limit MEMECOIN_SLOT_LANE_CROWDED_DAILY_LAMPORTS (0 = unlimited)."),
    _k("MEMECOIN_SLOT_LANE_RABBIT_BUY", group="Source authority",
       label="Rabbit-led buys (Solana slot lane, pre-execution)", kind=BOOL,
       note="Fires a funded copy from the source's RabbitStream frame BEFORE it executes; a source "
            "that does not land is exited (source_not_landed). Read per signal: off is instant and "
            "also clears a breaker pause. Needs MEMECOIN_SLOT_LANE_RABBIT_SIGNAL=1 at start."),
    _k("MEMECOIN_SLOT_LANE_PRECONF", group="Source authority",
       label="Preconf-led buys (Solana slot lane, Helius preconfirmations)", kind=BOOL,
       note="Fires a funded copy from the source's Helius preconfirmation (owner signature verified "
            "locally) before it lands; a source that does not land is exited (source_not_landed). Read "
            "per signal: off is instant and also clears its breaker pause. The socket itself starts "
            "only when this is on at start; credits are capped by MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS."),

    # --- how much goes in ---------------------------------------------------------------------
    _k("MEMECOIN_SOLANA_MAX_CLIP_USD", group="Entry size ceiling",
       label="Max clip, Solana", kind=NUMBER, unit="USD", minimum=0, maximum=100000,
       off_value=0,
       note="A CEILING on one entry, applied only downward. 0 = off, i.e. pure bankroll sizing "
            "with a floor and no ceiling. The floor, the spendable-balance clamp and the "
            "route-capacity reduction all still apply after it."),
    _k("MEMECOIN_EVM_MAX_CLIP_USD", group="Entry size ceiling", label="Max clip, EVM",
       kind=NUMBER, unit="USD", minimum=0, maximum=100000, off_value=0,
       note="Same ceiling on the EVM venues."),

    # --- TRADING BREAKERS, operator-switchable 2026-09-18 ------------------------------------
    # These two are CAPITAL rails by the selection-versus-safety line this module draws above, and
    # they are adjustable anyway because the operator asked for a switch he can reach without a
    # deploy. The reasoning he gave is sound: a breaker that can only be changed by editing a file
    # the deploy owns and restarting a real-money process is a breaker nobody turns off on the day
    # it is wrong. Making the switch reachable makes the decision explicit and audited instead of
    # hidden in `.env`.
    #
    # WHAT THEY ARE WORTH, measured 2026-09-18 by replaying the drawdown rail over 1,190 funded
    # closes since 2026-08-10 (no fill assumption is needed: these are positions we actually took,
    # with realized PnL). At the live $100 / 44 min it blocks 401 entries worth -$440, so the book
    # goes from +$1,940 to +$2,380. The 44-minute length is the peak of the sweep (15m -$106,
    # 30m -$361, 44m -$440, 60m -$414, 120m +$41) and $100 beats $150/$200/$300. It is NOT stable:
    # over 2026-08-25..09-05 the blocked set was +$109, and the blocked population is 224 winners
    # against 177 losers — the losers are simply bigger. Turning it off is a real trade, not a
    # free one.
    _k("MEMECOIN_DRAWDOWN_PAUSE_USD", group="Trading breakers", label="Drawdown pause trigger",
       # NO gate_family: `gate_family` means a WALLET may be excused from this, and a breaker
       # that any wallet can walk past is not a breaker. The switch is global and audited.
       kind=NUMBER, unit="USD", minimum=0, maximum=100000, off_value=0,
       note="0 = OFF. Pauses new entries once the day's realized PnL falls this far below its own "
            "high. It is not a test of being down on the day: entries taken while $100+ below the "
            "high are +$735 over 338 closes. What it catches is the window right after a loss."),
    _k("MEMECOIN_DRAWDOWN_PAUSE_S", group="Trading breakers", label="Drawdown pause length",
       kind=NUMBER, unit="s", minimum=0, maximum=86400, off_value=0,
       note="0 = OFF. How long entries stay paused after a trigger."),
    _k("MEMECOIN_DAILY_LOSS_LIMIT_USD", group="Trading breakers", label="Daily loss breaker",
       kind=NUMBER, unit="USD", minimum=0, maximum=100000, off_value=0,
       note="0 = OFF. Latches for the rest of the UTC day once realized PnL reaches -this. "
            "Setting it to 0 also releases a latch that already fired today."),

    # --- LOCKED: these protect the capital, not the selection --------------------------------
    _k("MEMECOIN_ENTRY_IMPACT_CAP_PCT", group="Locked (capital safety)",
       label="Entry impact cap", kind=NUMBER, unit="%", adjustable=False,
       locked_reason="Sizing safety. Solana-only pre-flight cap; EVM enforces amountOutMinimum."),
    _k("MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT", group="Locked (capital safety)",
       label="Source impact cap", kind=NUMBER, unit="%", adjustable=False,
       locked_reason="Sizing safety."),
    _k("MEMECOIN_REAL_MAX_EXEC_PREMIUM", group="Locked (capital safety)",
       label="Execution premium cap", kind=NUMBER, unit="x reference", adjustable=False,
       gate_family="execution_premium",
       locked_reason="Cost gate. Never exemptible, never adjustable from the cockpit."),
    _k("MEMECOIN_MIN_TRADE_FLOOR_USD", group="Locked (capital safety)", label="Minimum trade",
       kind=NUMBER, unit="USD", adjustable=False,
       locked_reason="Below this a fill cannot cover its own costs."),
)

KNOBS = {k.name: k for k in _KNOBS}
ADJUSTABLE = frozenset(k.name for k in _KNOBS if k.adjustable)

# The pristine `.env` values, captured the first time this module resolves anything and never
# rewritten. Clearing an override restores from HERE, so an applied override can never become the
# new baseline — the defect that would make a temporary experiment permanent and invisible.
_DEFAULTS = {}


def _defaults(cfg):
    if not _DEFAULTS:
        for name in KNOBS:
            _DEFAULTS[name] = getattr(cfg, name, None)
    return _DEFAULTS


def default_value(cfg, name):
    return _defaults(cfg).get(name)


def effective(conn, cfg, overrides=None):
    """{name: (value, source)} for every registry knob. `source` is 'override' or 'default'."""
    rows = read_overrides(conn) if overrides is None else overrides
    out = {}
    for name, knob in KNOBS.items():
        base = default_value(cfg, name)
        raw = rows.get(name)
        if raw is None or not knob.adjustable:
            out[name] = (base, "default")
            continue
        try:
            out[name] = (knob.coerce(raw), "override")
        except ValueError:
            logger.warning("entry knob override %s=%r is invalid — using the .env default", name, raw)
            out[name] = (base, "default")
    return out


def read_overrides(conn):
    """{name: raw_value} for ACTIVE overrides. Never raises; unreadable means no overrides."""
    try:
        rows = conn.execute(
            "SELECT name, value FROM memecoin_entry_knob_overrides WHERE active=1").fetchall()
    except Exception:
        logger.exception("entry knob overrides unreadable — applying .env defaults")
        return {}
    out = {}
    for r in rows:
        name = r[0]
        if name not in ADJUSTABLE:
            continue                     # a locked or retired name is inert, never applied
        try:
            out[name] = json.loads(r[1])
        except Exception:
            logger.warning("entry knob override %s has unparseable value — ignoring", name)
    return out


def effective_control_view(conn, cfg, values, *, source_user_id=None):
    """Resolve chain and band controls without changing the settings module."""
    import os
    from sniper.persistence import db_memecoin as m

    def value(name):
        return values.get(name, (getattr(cfg, name, None), "default"))[0]

    env = dict(os.environ)
    env["MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5"] = str(value("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5"))
    boundary = float(cfg.MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD)
    if source_user_id in cfg.MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS:
        boundary = float(cfg.MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD)
    bands = (("nano", 0, cfg.MEMECOIN_REAL_NANO_MCAP_USD),
             ("micro", cfg.MEMECOIN_REAL_NANO_MCAP_USD, cfg.MEMECOIN_REAL_MICRO_MCAP_USD))
    contexts = []
    for chain in ("solana", *sorted(str(c) for c in cfg.MEMECOIN_EVM_ALLOWLIST)):
        waivers = m.entry_gate_exemptions(conn, source_user_id, chain)
        for band, lower, upper in bands:
            rows = []

            def add(key, label, number, unit, family=None, *, state=None, missing="not_applicable", note=""):
                resolved = ("waived" if family in waivers else
                            state or ("active" if number else "disabled"))
                rows.append(dict(key=key, label=label, value=number, unit=unit,
                                 state=resolved, gate_family=family,
                                 missing_input=missing, note=note))

            inband = boundary <= 0 or upper <= boundary
            add("m5_floor", "Five-minute turnover floor",
                cfg.inband_min_turnover_m5_for_chain(chain, env=env), "ratio",
                "m5_turnover_under_floor", state=None if inband else "conditional",
                missing="passes", note="Adjusted volume. Above the volume boundary, the over-band floor applies.")
            add("m5_ceiling", "Five-minute turnover ceiling",
                cfg.inband_max_turnover_m5_for(chain, band), "ratio",
                "m5_turnover_over_ceiling", state=None if inband else "conditional",
                missing="passes", note="Raw volume. Applies at or below the volume boundary.")
            add("m5_txns", "Five-minute transaction floor", value("MEMECOIN_REAL_MIN_TXNS_M5"), "transactions",
                "m5_txns_under_floor", state="out_of_scope" if inband else "conditional",
                missing="conditional", note="Applies only above the volume boundary with known market cap.")
            ratio = value("MEMECOIN_REAL_MIN_TURNOVER_H1")
            add("h1_ratio", "Hourly turnover floor", ratio, "ratio", "turnover_under_floor",
                missing="passes", note="Adjusted volume. Requires known market cap.")
            absolute = value("MEMECOIN_REAL_MIN_VOLUME_H1_USD")
            add("h1_absolute", "Hourly dollar floor", absolute, "USD",
                state="fallback" if absolute and ratio else None, missing="passes",
                note="Applies when the hourly ratio is disabled or market cap is unknown.")
            add("sell_share", "Sell share ceiling", value("MEMECOIN_REAL_MAX_SELL_SHARE_PCT"), "%",
                "sell_share", missing="passes",
                note="Transaction count, not dollars. Minimum sample: %s transactions." % value("MEMECOIN_REAL_SELL_SHARE_MIN_TXNS"))
            add("chase_first_seen", "First-seen price multiple", value("MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO"), "x",
                "chase_first_seen", missing="passes")
            add("winner_reentry_price", "Winner re-entry price cap", value("MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT"), "%",
                "winner_reentry_price", note="Requires an eligible prior winning exit.")
            add("winner_reentry_daily_cap", "Winner daily cap", value("MEMECOIN_REENTRY_WINNER_DAILY_CAP"), "entries",
                "winner_reentry_daily_cap")
            add("pair_age", "Minimum pair age", cfg.min_coin_age_s_for_chain(chain), "seconds", missing="passes")
            add("follow_age", "Follow signal maximum age", value("MEMECOIN_REAL_FOLLOW_MAX_AGE_S"), "seconds")
            contexts.append(dict(network_id=chain, coin_band=band, mcap_min=lower, mcap_max=upper,
                                 volume_boundary_usd=boundary, waivers=sorted(waivers), controls=rows))
    return {"basis": "web_configuration", "runner_acknowledged": False,
            "source_user_id": source_user_id, "contexts": contexts,
            "note": "Resolved web configuration and stored overrides. This is not a runner acknowledgement. "
                    "These rows cover known nano and micro market caps. Wallet waivers and event inputs can change applicability."}


def apply_overrides(conn, cfg):
    """Push the effective values onto the settings module. ONE CALLER: the runner's main cycle.

    Do NOT call this from the entry path or from a lane thread. Every registry knob is written on
    every successful call — the ones with no override are written back to their pristine default —
    so removing a row is a real revert and no stale value survives in the process.

    Returns {'changed': {...}, 'deferred': bool}. `deferred` means an entry decision was running and
    NOTHING was written; the next cycle retries. A caller that treats a deferred result as applied
    would report a knob as live before the runner reads it.
    """
    global _PENDING_SINCE
    values = effective(conn, cfg)          # DB read stays outside the lock
    with _APPLY_LOCK:
        if _IN_FLIGHT:
            if _PENDING_SINCE is None:
                _PENDING_SINCE = time.time()
            return {"changed": {}, "deferred": True}
        changed = {}
        for name, (value, source) in values.items():
            if value is None:
                continue                   # the setting does not exist in this release
            if getattr(cfg, name, None) != value:
                setattr(cfg, name, value)
                if source == "override":
                    changed[name] = value
        _PENDING_SINCE = None
        return {"changed": changed, "deferred": False}
