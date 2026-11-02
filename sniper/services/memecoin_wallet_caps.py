"""Immutable wallet/chain ceiling snapshot, refreshed outside the funded entry path."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import logging
import re
import math
from pathlib import Path
import threading
import time
from types import MappingProxyType

from sniper.settings import memecoin as cfg

log = logging.getLogger(__name__)
WINDOW_DAYS = 30
MIN_COINS = 10
MAX_AGE_S = 180
REFRESH_S = 30


def _instant(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def evidence(rows, now):
    """Recorded prior-only automatic chain fills, with coin-level outlier removal."""
    cells = defaultdict(lambda: {"coins": defaultdict(list), "invalid": False, "closes": 0})
    cutoff = now - timedelta(days=WINDOW_DAYS)
    for r in rows:
        uid, chain = r.get("source_user_id"), str(r.get("network_id") or "")
        if not uid or not chain or r.get("signal_source") in {"manual", "manual-unmanaged"}:
            continue
        cell = cells[(str(uid), chain)]
        stamp = _instant(r.get("closed_recorded_at"))
        if stamp is None:
            cell["invalid"] = True
            continue
        if not cutoff <= stamp <= now:
            continue
        pnl, stake, mint = r.get("realized_pnl"), r.get("entry_notional"), r.get("mint")
        if (r.get("entry_fill_source") != "chain" or not mint
                or isinstance(pnl, bool) or not isinstance(pnl, (int, float)) or not math.isfinite(pnl)
                or isinstance(stake, bool) or not isinstance(stake, (int, float))
                or not math.isfinite(stake) or stake <= 0):
            cell["invalid"] = True
            continue
        coin = str(mint) if chain == "solana" else str(mint).lower()
        cell["coins"][coin].append(100.0 * pnl / stake)
        cell["closes"] += 1
    result = {}
    for key, cell in cells.items():
        coins = cell["coins"]
        # Equal weight per distinct coin, regardless of stake or how often it was re-entered.
        coin_returns = [math.fsum(returns) / len(returns) for returns in coins.values()]
        mean_return = math.fsum(coin_returns) / len(coin_returns) if coin_returns else 0.0
        trimmed = sorted(coin_returns)[:-1]
        without_best = math.fsum(trimmed) / len(trimmed) if trimmed else 0.0
        result[key] = MappingProxyType(dict(
            proven=(not cell["invalid"] and len(coins) >= MIN_COINS and mean_return > 0.005
                    and without_best > 0.005),
            closes=cell["closes"], coins=len(coins), mean_return_pct=mean_return,
            without_best_coin_return_pct=without_best, incomplete=cell["invalid"]))
    return MappingProxyType(result)


def _trial_promoted_cells(conn):
    """Latest trial decisions only; called on the background reader, never an entry thread."""
    return frozenset((str(uid), str(chain)) for uid, chain in conn.execute(
        "WITH latest AS (SELECT source_user_id,chain,MAX(id) AS id "
        "FROM memecoin_trial_arms GROUP BY source_user_id,chain) "
        "SELECT t.source_user_id,t.chain FROM latest l "
        "JOIN memecoin_trial_arms t ON t.id=l.id "
        "WHERE t.rule=? AND t.ended_at IS NOT NULL AND t.end_reason='promote' "
        "AND EXISTS (SELECT 1 FROM memecoin_trial_verdicts v WHERE v.trial_id=t.id "
        "AND v.verdict='PROMOTE' AND v.action='ended_kept_armed')",
        ("tp-two-of-three-v1",)))


class WalletCapCache:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.snapshot = (None, MappingProxyType({}), "not_ready")
        self.operator_snapshot = None
        self.reentry_snapshot = None   # the re-entry list from the last operator refresh (see below)
        self.raised_clip_snapshot = None   # the raised Solana clip list, likewise
        self.thread = None

    def publish(self, rows, now, *, trial_promotions=()):
        # Build privately; publish one immutable reference. Readers never lock or see half a refresh.
        values = dict(evidence(rows, now))
        for cell in trial_promotions:
            values[cell] = MappingProxyType(dict(values.get(cell, {"proven": False}),
                                                 trial_promoted=True))
        self.snapshot = (self.clock(), MappingProxyType(values), "ready")

    def invalidate(self, reason):
        self.snapshot = (None, MappingProxyType({}), str(reason))

    def full_cap_listed(self, uid, chain):
        """The operator's full-cap list names `uid` bare (every chain) or as `uid@chain`."""
        operator = self.operator_snapshot
        ids = operator[0] if operator is not None else cfg.MEMECOIN_REAL_FULL_CAP_WALLET_IDS
        return bool(uid) and (str(uid) in ids or f"{uid}@{chain}" in ids)

    def reentry_listed(self, uid, chain):
        """Whether MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS names `uid` for `chain`, or None when
        that list is unset or empty (the caller then uses the full-cap list). The last operator
        refresh's value, else the value read at startup."""
        snap = self.reentry_snapshot
        ids = snap[0] if snap is not None else parse_wallet_ids(cfg.MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS)[0]
        if ids is None:
            return None
        return bool(uid) and (str(uid) in ids or f"{uid}@{chain}" in ids)

    def raised_clip_listed(self, uid, chain):
        """Whether MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS names `uid` for `chain`, or None when that
        list is unset or empty (every wallet takes the raised ceiling, per the threshold)."""
        snap = self.raised_clip_snapshot
        ids = (snap[0] if snap is not None else parse_wallet_ids(
            cfg.MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS, RAISED_CLIP_KNOB)[0])
        if ids is None:
            return None
        return bool(uid) and (str(uid) in ids or f"{uid}@{chain}" in ids)

    def cap(self, uid, chain, ceiling_usd=None):
        """The wallet's per-entry cap. `ceiling_usd` (the Solana above-nano ceiling) raises the cap
        of full-cap/proven wallets. A trial promotion restores only the ordinary global cap."""
        top = cfg.MEMECOIN_REAL_MAX_ENTRY_USD
        if ceiling_usd:
            top = max(top, float(ceiling_usd))
        if self.full_cap_listed(uid, chain):
            return top
        stamp, values, _ = self.snapshot
        valid = stamp is not None and 0 <= self.clock() - stamp <= MAX_AGE_S
        cell = values.get((str(uid), str(chain)), {}) if valid else {}
        if cell.get("proven"):
            return top
        # This trial grants the ordinary ceiling only, not the raised Solana ceiling or any of
        # full_cap_listed's independent re-entry/override authority.
        return (cfg.MEMECOIN_REAL_MAX_ENTRY_USD if cell.get("trial_promoted")
                else cfg.MEMECOIN_REAL_UNPROVEN_MAX_ENTRY_USD)

    def refresh_operator_overrides(self, *, root=None):
        """Background-only live budget setting. Never mutate os.environ or settings globals."""
        from sniper.util.envfiles import load_env_files
        from sniper.util.state_paths import state_root
        try:
            root = Path(root) if root is not None else state_root()
            if not (root / ".env").is_file():
                raise OSError("wallet cap state environment unavailable")
            values = {}
            load_env_files(root, env=values)  # private dictionary; same file precedence as startup
            ids = frozenset(uid.strip() for uid in
                            values.get("MEMECOIN_REAL_FULL_CAP_WALLET_IDS", "").split(",")
                            if uid.strip())
            reentry = parse_wallet_ids(values.get("MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", ""))
            raised = parse_wallet_ids(values.get(RAISED_CLIP_KNOB, ""), RAISED_CLIP_KNOB)
        except Exception:
            previous = self.operator_snapshot
            ids = previous[0] if previous is not None else cfg.MEMECOIN_REAL_FULL_CAP_WALLET_IDS
            stamp = previous[2] if previous is not None else None
            self.operator_snapshot = (ids, "refresh_failed", stamp)
            log.exception("wallet operator cap refresh failed; retaining last authorized overrides")
            return False
        self.operator_snapshot = (ids, "ready", self.clock())
        self.reentry_snapshot = reentry      # a failed refresh above keeps the previous one
        self.raised_clip_snapshot = raised
        return True

    def refresh(self, connection_factory, now_fn):
        self.refresh_operator_overrides()
        from sniper.persistence.db_memecoin import wallet_entry_cap_evidence
        conn = None
        try:
            now = _instant(now_fn())
            if now is None:
                raise ValueError("wallet cap clock unavailable")
            conn = connection_factory()
            # Short-lived read-only connection, bounded statement. No WAL pin across refreshes.
            deadline = time.monotonic() + 2
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            rows = wallet_entry_cap_evidence(conn, since=(now-timedelta(days=WINDOW_DAYS)).isoformat())
            promotions = _trial_promoted_cells(conn)
            self.publish(rows, now, trial_promotions=promotions)
        except Exception:
            self.invalidate("refresh_failed")
            log.exception("wallet cap evidence refresh failed; automatic ceilings use $50")
        finally:
            if conn is not None:
                conn.close()

    def start(self, connection_factory, now_fn):
        if self.thread is not None:
            return
        # Startup only, before funded hooks are enabled; no first-entry wait.
        self.refresh(connection_factory, now_fn)
        def run():
            while True:
                time.sleep(REFRESH_S)
                self.refresh(connection_factory, now_fn)
        self.thread = threading.Thread(target=run, name="wallet-entry-caps", daemon=True)
        self.thread.start()

    def status(self):
        stamp, values, state = self.snapshot
        operator = self.operator_snapshot
        ids, operator_state, operator_stamp = operator if operator is not None else (cfg.MEMECOIN_REAL_FULL_CAP_WALLET_IDS, "startup", None)
        fresh = stamp is not None and 0 <= self.clock()-stamp <= MAX_AGE_S
        reentry = (self.reentry_snapshot if self.reentry_snapshot is not None
                   else parse_wallet_ids(cfg.MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS))
        raised = (self.raised_clip_snapshot if self.raised_clip_snapshot is not None
                  else parse_wallet_ids(cfg.MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS, RAISED_CLIP_KNOB))
        return dict(enabled=cfg.MEMECOIN_REAL_WALLET_CAPS_ENABLED,
                    cross_wallet_reentry_wallet_ids=None if reentry[0] is None else sorted(reentry[0]),
                    cross_wallet_reentry_dropped=list(reentry[1]),
                    solana_raised_clip_wallet_ids=None if raised[0] is None else sorted(raised[0]),
                    solana_raised_clip_dropped=list(raised[1]),
                    max_entry_usd=cfg.MEMECOIN_REAL_MAX_ENTRY_USD,
                    unproven_max_entry_usd=cfg.MEMECOIN_REAL_UNPROVEN_MAX_ENTRY_USD,
                    state=state if fresh or stamp is None else "stale", age_s=None if stamp is None else self.clock()-stamp,
                    operator_full_cap_wallet_ids=sorted(ids), operator_config_state=operator_state,
                    operator_config_age_s=None if operator_stamp is None else self.clock()-operator_stamp,
                    proven_cells=[dict(source_user_id=uid, network_id=chain, **dict(v))
                                  for (uid, chain), v in values.items() if fresh and v["proven"]],
                    trial_promoted_cells=[dict(source_user_id=uid, network_id=chain)
                                          for (uid, chain), v in values.items()
                                          if fresh and v.get("trial_promoted")])


_WALLET_ID = re.compile(r"[^@\s]+(?:@(?:solana|[0-9]+))?")


RAISED_CLIP_KNOB = "MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS"


@lru_cache(maxsize=16)
def parse_wallet_ids(raw, name="MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS"):
    """(ids, dropped) for a wallet-list knob in the full-cap list's format: ids is None when the
    value is unset, empty or only separators, else the frozenset of valid entries (possibly empty).
    A valid entry has no whitespace, at most one '@', a non-empty id and a chain token `solana` or
    digits. Every other entry is DROPPED with a WARNING, once per distinct value (cached): dropping
    only narrows, and a value with no valid entry lifts nobody rather than falling back."""
    entries = [e.strip() for e in str(raw or "").split(",") if e.strip()]
    if not entries:
        return None, ()
    ids = frozenset(e for e in entries if _WALLET_ID.fullmatch(e))
    dropped = tuple(e for e in entries if e not in ids)
    if dropped:
        log.warning("%s: dropped malformed entries %s (kept %d)", name, list(dropped), len(ids))
    return ids, dropped


CACHE = WalletCapCache()


def global_cap_usd():
    return cfg.MEMECOIN_REAL_MAX_ENTRY_USD if cfg.MEMECOIN_REAL_WALLET_CAPS_ENABLED else None


def cross_wallet_reentry_closes_by(uid, chain):
    """The re-entry scope a leg passes the risk builders: `uid` when the cross-wallet re-entry rule
    applies to this wallet on this chain, else None (every close blocks). The rule needs
    MEMECOIN_REAL_CROSS_WALLET_REENTRY on and the wallet on MEMECOIN_REAL_CROSS_WALLET_REENTRY_
    WALLET_IDS when that list is set (operator 2026-10-08 13:10Z), else on the full-cap list (same
    parser and live cache as the clip). Independent of MEMECOIN_REAL_WALLET_CAPS_ENABLED."""
    if not cfg.MEMECOIN_REAL_CROSS_WALLET_REENTRY:
        return None
    listed = CACHE.reentry_listed(uid, chain)
    if listed is None:
        listed = CACHE.full_cap_listed(uid, chain)
    return str(uid) if listed else None


def raised_clip_wallet_allowed(uid, chain="solana"):
    """Whether `uid` may take the raised Solana ceiling: True while
    MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS is unset or empty, else whether it names the wallet."""
    listed = CACHE.raised_clip_listed(uid, chain)
    return True if listed is None else listed


def raised_clip_list_set():
    """True when MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS is set (non-empty, possibly all-malformed)."""
    return CACHE.raised_clip_listed(None, "solana") is not None


def mark_reentry_lift(trace, decision, closes_by):
    """Record on the attempt trace which re-entry family the cross-wallet scope lifted for this
    allowed entry and the close that would have blocked it (Gamma 2026-10-08: the cohort must be
    gradable apart from ordinary entries). Evidence only: a trace fault is logged, never raised."""
    lift = getattr(decision, "reentry_lift", None)
    if not lift or trace is None:
        return
    try:
        trace.mark_substage("reentry_cross_wallet", closes_by=closes_by, **lift)
    except Exception:
        log.exception("cross-wallet re-entry: trace marker failed closes_by=%s lift=%s",
                      closes_by, lift)


def entry_cap_usd(uid, chain, *, existing_cap=None, ceiling_usd=None):
    """The entry's size cap: the wallet's cap (raised by `ceiling_usd`, the Solana above-nano
    ceiling, where the wallet gets the global), bounded by the caller's `existing_cap`."""
    if not cfg.MEMECOIN_REAL_WALLET_CAPS_ENABLED:
        return existing_cap
    cap = CACHE.cap(uid, chain, ceiling_usd)
    if existing_cap is None:
        return cap
    value = float(existing_cap)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("entry size cap unavailable")
    return min(cap, value)
