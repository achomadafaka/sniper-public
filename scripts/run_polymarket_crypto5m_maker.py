#!/usr/bin/env python3
"""THE crypto5m engine — live PAPER maker for the Polymarket BTC 5m binary (campaign maker-regime-v1).

The one algo (2026-07-04 unification): every taker variant lost to cost (forward C0 random = −19.5% ROI,
the vig), so this MAKER posts resting quotes centered on the Binance-led fair value (which leads the book
~1s) and EARNS the spread. LONG-ONLY per token (asks only against held inventory — no naked shorts; the
binary's other side is expressed by bidding the other token). The retired scalper's brain runs inside:
a CONFIRMED directional read pulls the quotes the move would run through; a vol jump doubles the margin.

PAPER ONLY — no orders are sent anywhere. Fills are PRINT-ONLY: a resting quote fills only when a trade
PRINTS to/through it on the live tape (capture DB last_trade_price events). The book merely quoting to
our price is not a fill, and an unreachable tape means no fills — the campaign pauses honestly rather
than accrue unverifiable fills. Settlement is retried every poll after a 12s grace (Gamma posts the
outcome late; the old one-shot attempt stranded windows unsettled forever).

Watchdog-managed (scripts/crypto5m_watchdog.sh). Restart-safe: window inventory is persisted every poll
and restored on boot (a mid-window restart used to silently zero inventory and corrupt that settlement).
"""
from __future__ import annotations
import argparse, hashlib, json, os, random, signal, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.algos.polymarket_crypto5m import direction as DIR
from sniper.algos.polymarket_crypto5m import direction_state as DS
from sniper.algos.polymarket_crypto5m import live_desk as LD
from sniper.algos.polymarket_crypto5m import maker_adaptive as AD
from sniper.algos.polymarket_crypto5m import maker_adaptive_v3 as AD3
from sniper.algos.polymarket_crypto5m import maker_adaptive_v33 as AD33
from sniper.algos.polymarket_crypto5m import maker_regime as RG
from sniper.algos.polymarket_crypto5m import maker_regime_gated as RGF
from sniper.algos.polymarket_crypto5m import maker_regime_state as RS
from sniper.algos.polymarket_crypto5m import maker_policy as MP
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import settlement as S

WINDOW_S = 300
# Fleet runner context: regime-v1 is the control campaign, and adaptive paper challengers run the
# same market windows with isolated campaign DBs. The maker retained the retired scalper's confirmed
# directional read for quote-pull defense, while adaptive variants test one paper-only lever at a time.
# z_v2 remains a desk diagnostic only; prior shadow evidence showed market mid beat it as a 5m outcome
# forecast, so it is not used as the quoting fair.
ENGINE_VERSION = "maker-regime-v1"

REGIME_META_COLUMNS = [
    ("regime_id", "TEXT"),
    ("policy_id", "TEXT"),
    ("route_reason", "TEXT"),
    ("regime_inputs_hash", "TEXT"),
    ("margin_used", "REAL"),
    ("clip_used", "REAL"),
    ("max_shares_used", "REAL"),
    ("min_edge_used", "REAL"),
    ("min_seconds_quote_used", "REAL"),
    ("bid_enabled", "INTEGER"),
    ("ask_enabled", "INTEGER"),
    ("exit_only", "INTEGER"),
    ("risk_action", "TEXT"),
    ("can_hard_cut", "INTEGER"),
    ("quote_ttl_ms", "INTEGER"),
]

REGIME_STATE_COLUMNS = [
    ("episode_id", "TEXT"),
    ("moment_key", "TEXT"),
    ("moment_schema_version", "INTEGER"),
    ("profile_id", "TEXT"),
    ("profile_mode", "TEXT"),
    ("profile_decision_id", "TEXT"),
    ("policy_hash", "TEXT"),
    ("quote_created_regime_id", "TEXT"),
    ("quote_created_episode_id", "TEXT"),
    ("quote_episode_id", "TEXT"),
    ("fill_episode_id", "TEXT"),
    ("quote_regime_id", "TEXT"),
    ("fill_regime_id", "TEXT"),
    ("quote_profile_id", "TEXT"),
    ("fill_profile_id", "TEXT"),
    ("selector_version", "TEXT"),
    ("reward_version", "TEXT"),
    ("selection_mode", "TEXT"),
    ("exploration_flag", "INTEGER"),
    ("profile_disabled_flag", "INTEGER"),
    ("parent_context_used", "TEXT"),
    ("adaptive_context_key", "TEXT"),
]


def _db(path):
    c = sqlite3.connect(path, timeout=30); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT);
      CREATE TABLE IF NOT EXISTS maker_windows(
        condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
        realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER);
      CREATE TABLE IF NOT EXISTS maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
      CREATE TABLE IF NOT EXISTS maker_quotes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, status TEXT, price REAL, size REAL, fair REAL, best_bid REAL, best_ask REAL,
        seconds_left REAL, ts INTEGER, reason TEXT);
      CREATE TABLE IF NOT EXISTS maker_regime_decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, regime_id TEXT, policy_id TEXT, route_reason TEXT, regime_inputs_hash TEXT,
        margin_used REAL, clip_used REAL, max_shares_used REAL, min_edge_used REAL,
        min_seconds_quote_used REAL, bid_enabled INTEGER, ask_enabled INTEGER, exit_only INTEGER,
        risk_action TEXT, can_hard_cut INTEGER, quote_ttl_ms INTEGER, warnings_json TEXT, ts INTEGER);
      CREATE TABLE IF NOT EXISTS maker_shadow_windows(
        campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
        realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, up_vwap REAL, down_vwap REAL,
        ts INTEGER, PRIMARY KEY(campaign_id, condition_id));
      CREATE TABLE IF NOT EXISTS maker_regime_episodes(
        episode_id TEXT PRIMARY KEY, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        window_end_ts INTEGER, regime_id TEXT, previous_regime_id TEXT, started_ts REAL, ended_ts REAL,
        last_seen_ts REAL, dwell_ms REAL, transition_reason TEXT, transition_confidence TEXT,
        regime_confidence TEXT, moment_schema_version INTEGER, moment_key TEXT, moment_features_json TEXT,
        inventory_state TEXT, odds_bucket TEXT, volatility_state TEXT, book_state TEXT, spread_state TEXT,
        direction_state TEXT, source_snapshot_hash TEXT);
      CREATE TABLE IF NOT EXISTS maker_regime_transitions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, episode_id TEXT, condition_id TEXT,
        window_start_ts INTEGER, from_regime_id TEXT, to_regime_id TEXT, ts REAL, reason TEXT,
        confidence TEXT);
      CREATE TABLE IF NOT EXISTS maker_profile_decisions(
        profile_decision_id TEXT PRIMARY KEY, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        episode_id TEXT, token TEXT, outcome TEXT, decision_ts REAL, profile_id TEXT, profile_mode TEXT,
        base_policy_id TEXT, profile_policy_hash TEXT, bid_enabled INTEGER, ask_enabled INTEGER,
        exit_only INTEGER, hard_cut INTEGER, margin_used REAL, clip_used REAL, max_shares_used REAL,
        min_edge_used REAL, quote_ttl_ms INTEGER, reason TEXT, warnings_json TEXT);
    """)
    # idempotent migrations: vwaps make restored windows cut-loss-capable; fill_model tags print vs book
    # fills; campaign_id partitions eval data (v0 rows are contaminated — never pool across campaigns)
    for table, col, typ in (("maker_windows", "up_vwap", "REAL"), ("maker_windows", "down_vwap", "REAL"),
                            ("maker_windows", "campaign_id", "TEXT"), ("maker_fills", "fill_model", "TEXT"),
                            ("maker_fills", "quote_id", "INTEGER")):
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists
    for table in ("maker_fills", "maker_quotes"):
        for col, typ in REGIME_META_COLUMNS:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    for table in ("maker_fills", "maker_quotes", "maker_regime_decisions"):
        for col, typ in REGIME_STATE_COLUMNS:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    # the settlement-identity check replays fills per (campaign, condition_id) for every settled window
    c.execute("CREATE INDEX IF NOT EXISTS idx_maker_fills_campaign_cid ON maker_fills(campaign_id, condition_id)")
    AD.ensure_adaptive_tables(c)
    AD3.ensure_v3_tables(c)
    c.commit(); return c


class WindowBook:
    """Per-window paper inventory for both outcome tokens."""
    def __init__(self, cid, ws):
        self.cid, self.ws = cid, ws
        self.up_inv = 0.0; self.down_inv = 0.0; self.cash = 0.0
        self.up_vwap = None; self.down_vwap = None
        self.prev = {}   # token -> last resting {bid,ask} Quotes for fill modeling

    def _fill(self, token, side, price, size):
        """LONG-ONLY accounting: a sell can never exceed held inventory (naked synthetic shorts are
        impossible on Polymarket without minting, and negative inventory corrupted vwap/cut-loss in the
        first campaign — codex review 2026-07-04). vwap resets to None when the token is fully scaled out."""
        if side == "bid":   # we BUY this token
            self.cash -= price * size
            if token == "up":
                tot = (self.up_vwap or 0) * self.up_inv + price * size
                self.up_inv += size; self.up_vwap = tot / self.up_inv if self.up_inv else None
            else:
                tot = (self.down_vwap or 0) * self.down_inv + price * size
                self.down_inv += size; self.down_vwap = tot / self.down_inv if self.down_inv else None
            return size
        else:               # we SELL what we HOLD (clamped)
            if token == "up":
                size = min(size, max(0.0, self.up_inv))
                self.cash += price * size
                self.up_inv -= size
                if self.up_inv <= 1e-9:
                    self.up_inv, self.up_vwap = 0.0, None
            else:
                size = min(size, max(0.0, self.down_inv))
                self.cash += price * size
                self.down_inv -= size
                if self.down_inv <= 1e-9:
                    self.down_inv, self.down_vwap = 0.0, None
            return size


def model_fills(wb, token, quotes, best_bid, best_ask, clip, prints=None):
    """Fill model for the PREVIOUSLY-resting quotes. PRINT-ONLY:

    prints = trades on the tape since the last poll, in THIS token's price space: [(price, size), ...].
    A resting BID fills only if a trade PRINTED at/below it (we'd have been the counterparty); a resting
    ASK only on a print at/above it. Empty list = no trades = no fill, even if the book QUOTED through our
    price (quoting is not trading). prints=None (tape unreachable) = NO fill this tick — never fail-open
    to book-through (codex review 2026-07-04; capture runs under the same watchdog, so a dead tape means
    the campaign pauses honestly instead of accruing unverifiable fills).
    Fill size = min(qualifying printed size, quote size, clip). Returns [(side, price, size, 'print')]."""
    fills = []
    prev = wb.prev.get(token, {})
    b = prev.get("bid"); a = prev.get("ask")
    if prints:
        if b is not None:
            hit = sum(sz for px, sz in prints if px <= b.price + 1e-9)
            if hit > 0:
                fills.append(("bid", b.price, round(min(hit, b.size, clip), 2), "print"))
        if a is not None:
            hit = sum(sz for px, sz in prints if px >= a.price - 1e-9)
            if hit > 0:
                fills.append(("ask", a.price, round(min(hit, a.size, clip), 2), "print"))
    wb.prev[token] = {"bid": quotes.get("bid"), "ask": quotes.get("ask")}
    return fills


def unwind_fills(wb, token, fair, best_bid, best_ask, hard_cut=0.10):
    """Wired hard cut-loss (was dead code): when the Binance-led fair has moved `hard_cut` THROUGH our
    average entry against the inventory, cross the book and unwind the whole position NOW (paper taker).
    Long -> sell everything at best_bid; short -> buy back at best_ask. Returns [(side, price, size,
    'hard-cut')] or []. No bid/ask to cross into -> can't unwind this tick (retry next poll)."""
    inv = wb.up_inv if token == "up" else wb.down_inv
    vwap = wb.up_vwap if token == "up" else wb.down_vwap
    cut, _reason = MP.should_unwind(fair=fair, inventory=inv, entry_price=vwap, hard_cut=hard_cut)
    if not cut:
        return []
    if inv > 0 and best_bid is not None and 0.0 < best_bid < 1.0:
        return [("ask", best_bid, round(inv, 2), "hard-cut")]
    if inv < 0 and best_ask is not None and 0.0 < best_ask < 1.0:
        return [("bid", best_ask, round(-inv, 2), "hard-cut")]
    return []


SETTLE_GRACE_S = 12       # Gamma needs a moment after close to post the outcome; don't ask before this.
SETTLE_ABANDON_S = 86400  # only mark unresolved after 24h; retryable rows stay excluded from stats.


def settle_stale(c, campaign, now_ts, grace_s=SETTLE_GRACE_S, abandon_s=SETTLE_ABANDON_S):
    """Retry settlement for every closed-but-unsettled window. THE bug this fixes: settlement was
    attempted exactly once at rollover (~2s after close, before Gamma posts the outcome), then the book
    was dropped — windows sat settled=0 forever and the campaign counter never moved. Rebuilds each book
    from the persisted row (restart-safe), settles what Gamma can resolve, and keeps unresolved rows
    retryable. If a row is explicitly abandoned after the long deadline, realized_pnl stays NULL so it is
    excluded from stats, never counted as $0.
    Returns the number of windows settled this sweep."""
    n = 0
    rows = c.execute("""SELECT * FROM maker_windows
                     WHERE campaign_id=?
                       AND (settled=0 OR (settled=1 AND winning_outcome='unresolved' AND realized_pnl IS NULL))
                       AND window_start_ts + 300 + ? <= ?""",
                     (campaign, grace_s, now_ts)).fetchall()
    for r in rows:
        wb = ledger_or_window_book(c, campaign, r)
        realized = settle_window(c, campaign, wb)
        if realized is not None:
            n += 1
            print(f"[maker] settled (retry) {wb.cid[:10]} realized={realized:+.2f}", flush=True)
        elif not (r["settled"] and r["winning_outcome"] == "unresolved") and now_ts >= r["window_start_ts"] + 300 + abandon_s:
            c.execute("UPDATE maker_windows SET settled=1, winning_outcome='unresolved', ts=? "
                      "WHERE condition_id=? AND settled=0", (int(now_ts), r["condition_id"]))
            c.commit()
            print(f"[maker] ABANDON unresolved window {wb.ws} ({abandon_s}s)", flush=True)
    return n


def directional_pull(quotes_by_token, confirmed_side):
    """The retired scalper's read as the maker's adverse-selection defense: when a direction is CONFIRMED
    (persistence + horizons + order-flow, direction_state), pull the quotes that the move is about to run
    through — the ASK on the confirmed side (it's rising; selling it now is selling below where fair is
    going) and the BID on the opposite side (it's falling; that bid is about to be picked off).
    Pure; returns a new dict, None-side quotes preserved."""
    if confirmed_side not in ("Up", "Down"):
        return quotes_by_token
    rising = "up" if confirmed_side == "Up" else "down"
    falling = "down" if rising == "up" else "up"
    out = {t: dict(q) for t, q in quotes_by_token.items()}
    if rising in out and out[rising].get("ask") is not None:
        out[rising]["ask"] = None
        out[rising]["reason"] = f"{out[rising].get('reason', '')}; ask pulled (confirmed {confirmed_side} move)".strip("; ")
    if falling in out and out[falling].get("bid") is not None:
        out[falling]["bid"] = None
        out[falling]["reason"] = f"{out[falling].get('reason', '')}; bid pulled (confirmed {confirmed_side} move)".strip("; ")
    return out


def regime_inputs_hash(payload):
    """Stable short hash for known-at-time route inputs; used to join decisions/quotes/fills."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def effective_regime_knobs(regime_policy, *, base_margin, clip, max_shares, min_edge, min_seconds_quote):
    """Apply regime multipliers in binary-token price units. Does not mutate global defaults."""
    return {
        "margin": round(max(0.0, base_margin * regime_policy.margin_multiplier + regime_policy.margin_add), 4),
        "clip": round(max(0.0, clip * regime_policy.clip_multiplier), 2),
        "max_shares": round(max(0.0, max_shares * regime_policy.max_shares_multiplier), 2),
        "min_edge": max(min_edge, regime_policy.min_edge),
        "min_seconds_quote": max(min_seconds_quote, regime_policy.min_seconds_quote),
    }


def apply_regime_policy_to_quotes(quotes, regime_policy):
    """Apply side enablement from a regime policy to a desired quote dict."""
    out = dict(quotes or {})
    reasons = [out.get("reason") or ""]
    if regime_policy.exit_only and out.get("bid") is not None:
        out["bid"] = None
        reasons.append("exit-only pulled bid")
    if not regime_policy.bid_enabled and out.get("bid") is not None:
        out["bid"] = None
        reasons.append(f"regime {regime_policy.regime_id} pulled bid")
    if not regime_policy.ask_enabled and out.get("ask") is not None:
        out["ask"] = None
        reasons.append(f"regime {regime_policy.regime_id} pulled ask")
    out["reason"] = "; ".join(r for r in reasons if r)
    return out


def route_regime_for_token(desk, token, *, fair, best_bid, best_ask, token_inventory,
                           opposite_inventory, max_shares, confirm):
    """Build a route policy from known-at-time desk state and inventory for one outcome token."""
    inputs = {
        "token": token,
        "fair": fair,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "seconds_left": desk.get("seconds_left"),
        "jump_flag": bool(desk.get("jump_flag")),
        "divergence": desk.get("divergence"),
        "vol_fast": desk.get("vol_fast"),
        "vol_slow": desk.get("vol_slow"),
        "confirmed_side": (confirm or {}).get("confirmed_side"),
        "direction_confidence": float((confirm or {}).get("conviction") or 0.0),
        "book_stale": bool(desk.get("book_stale")),
        "book_quarantined": bool(desk.get("book_quarantined")),
        "fee_conflict": bool(desk.get("fee_conflict")),
        "token_inventory": token_inventory,
        "opposite_inventory": opposite_inventory,
        "max_shares": max_shares,
    }
    snapshot = RG.MakerRegimeSnapshot(
        fair=fair,
        best_bid=best_bid,
        best_ask=best_ask,
        seconds_left=desk.get("seconds_left"),
        jump_flag=bool(desk.get("jump_flag")),
        divergence=desk.get("divergence"),
        vol_fast=desk.get("vol_fast"),
        vol_slow=desk.get("vol_slow"),
        confirmed_side=(confirm or {}).get("confirmed_side"),
        direction_confidence=float((confirm or {}).get("conviction") or 0.0),
        book_stale=bool(desk.get("book_stale")),
        book_quarantined=bool(desk.get("book_quarantined")),
        fee_conflict=bool(desk.get("fee_conflict")),
    )
    inventory = RG.MakerRegimeInventory(
        token_inventory=token_inventory,
        opposite_inventory=opposite_inventory,
        max_shares=max_shares,
    )
    return RG.route_maker_regime(snapshot, inventory), regime_inputs_hash(inputs)


MARKET_REGIME_PRECEDENCE = {
    "hard_invalid": 0,
    "late_window": 1,
    "extreme_odds": 2,
    "inventory_saturated": 3,
    "jump": 4,
    "trend_confirmed": 5,
    "mid_chop": 6,
    "normal": 7,
}


def select_market_regime(regime_policies):
    """Collapse side/token route policies to one market/window episode candidate."""
    ids = [p.regime_id for p in regime_policies if p is not None]
    if not ids:
        return "normal"
    return min(ids, key=lambda rid: MARKET_REGIME_PRECEDENCE.get(rid, 99))


def regime_state_snapshot(desk, *, fair_up, cur, max_shares, confirm):
    max_inv = max(cur.up_inv if cur else 0.0, cur.down_inv if cur else 0.0)
    book_missing = any(desk.get(k) is None for k in ("up_bid", "up_ask", "down_bid", "down_ask"))
    book_health = (
        "quarantined" if desk.get("book_quarantined")
        else "missing" if book_missing
        else "stale" if desk.get("book_stale")
        else "healthy"
    )
    return {
        "fair": fair_up,
        "best_bid": desk.get("up_bid"),
        "best_ask": desk.get("up_ask"),
        "seconds_left": desk.get("seconds_left"),
        "volatility_state": "jump" if desk.get("jump_flag") else "calm",
        "direction_state": (
            "confirmed_up" if confirm.get("confirmed_side") == "Up"
            else "confirmed_down" if confirm.get("confirmed_side") == "Down"
            else "unconfirmed"
        ),
        "book_health": book_health,
        "token_inventory": max_inv,
        "max_shares": max_shares,
        "gross_inventory": (cur.up_inv + cur.down_inv) if cur else 0.0,
        "net_directional_inventory": (cur.up_inv - cur.down_inv) if cur else 0.0,
        "complete_sets": min(cur.up_inv, cur.down_inv) if cur else 0.0,
        "jump_score": desk.get("divergence"),
        "direction_score": (confirm or {}).get("conviction"),
    }


def policy_payload(policy, knobs):
    return {
        "policy_id": policy.policy_id,
        "risk_action": policy.risk_action,
        "bid_enabled": policy.bid_enabled,
        "ask_enabled": policy.ask_enabled,
        "exit_only": policy.exit_only,
        "can_hard_cut": policy.can_hard_cut,
        "margin": knobs.get("margin"),
        "clip": knobs.get("clip"),
        "max_shares": knobs.get("max_shares"),
        "min_edge": knobs.get("min_edge"),
        "quote_ttl_ms": policy.quote_ttl_ms,
        "reason": policy.reason,
        "warnings": policy.warnings,
    }


def episode_json(episode):
    return {
        "episode_id": episode.episode_id,
        "current_regime": episode.current_regime,
        "previous_regime": episode.previous_regime,
        "entered_ts": episode.entered_ts,
        "last_seen_ts": episode.last_seen_ts,
        "dwell_seconds": episode.dwell_seconds,
        "transition_reason": episode.transition_reason,
        "regime_confidence": episode.regime_confidence,
        "moment_key": episode.moment_key,
    }


def profile_decision_json(decision, adaptive_decision=None):
    meta = AD.adaptive_meta(adaptive_decision)
    return {
        "profile_decision_id": decision.profile_decision_id,
        "episode_id": decision.episode_id,
        "profile_id": decision.profile_id,
        "profile_mode": decision.profile_mode,
        "base_policy_id": decision.base_policy_id,
        "profile_policy_hash": decision.profile_policy_hash,
        "reason": decision.reason,
        "selector_version": meta["selector_version"],
        "reward_version": meta["reward_version"],
        "selection_mode": meta["selection_mode"],
        "exploration_flag": bool(meta["exploration_flag"]) if meta["exploration_flag"] is not None else None,
        "parent_context_used": meta["parent_context_used"],
        "adaptive_context_key": meta["adaptive_context_key"],
    }


def active_regime_routes(args, desk, tokens, cur, margin, confirm):
    routes = {}
    for token, f, bb, ba in tokens:
        inv = cur.up_inv if token == "up" else cur.down_inv
        opp_inv = cur.down_inv if token == "up" else cur.up_inv
        policy, ihash = route_regime_for_token(
            desk, token, fair=f, best_bid=bb, best_ask=ba, token_inventory=inv,
            opposite_inventory=opp_inv, max_shares=args.max_shares, confirm=confirm)
        knobs = effective_regime_knobs(
            policy, base_margin=margin, clip=args.clip, max_shares=args.max_shares,
            min_edge=args.min_edge, min_seconds_quote=args.min_seconds_quote)
        routes[token] = (policy, ihash, knobs)
    return routes


def ensure_active_regime_episode(c, args, cid, ws, now_ts, desk, fair_up, cur, confirm, regimes, regime_episode):
    market_candidate = select_market_regime([meta[0] for meta in regimes.values()])
    snapshot = regime_state_snapshot(desk, fair_up=fair_up, cur=cur, max_shares=args.max_shares,
                                     confirm=confirm)
    if regime_episode is None or regime_episode.condition_id != cid or regime_episode.window_start_ts != ws:
        moment = RS.build_moment(
            snapshot,
            token_inventory=float(snapshot.get("token_inventory") or 0.0),
            max_shares=args.max_shares,
        )
        regime_episode = RS.start_episode(args.campaign, cid, ws, ws + WINDOW_S, market_candidate,
                                          now_ts, moment)
        transition = None
    else:
        regime_episode, transition = RS.advance_episode(
            regime_episode, market_candidate, now_ts=now_ts, snapshot=snapshot)
    persist_episode(c, regime_episode)
    if transition is not None:
        persist_transition(c, args.campaign, transition)
    return regime_episode


def build_profile_decisions(c, args, cid, ws, now_ts, cur, regime_episode, regimes, *, persist_route_rows,
                            adaptive_decisions=None):
    decisions = {}
    for token, (policy, ihash, knobs) in regimes.items():
        inv = cur.up_inv if token == "up" else cur.down_inv
        adaptive_decision = (adaptive_decisions or {}).get(token)
        if adaptive_decision is not None:
            decision = AD.profile_decision_from_adaptive(
                regime_episode,
                token=token,
                active_policy=policy_payload(policy, knobs),
                adaptive=adaptive_decision,
                now_ts=now_ts,
            )
        else:
            decision = RS.decide_profile(
                regime_episode,
                token=token,
                token_inventory=inv,
                max_shares=args.max_shares,
                active_policy=policy_payload(policy, knobs),
                now_ts=now_ts,
                profile_mode="log_only",
            )
        persist_profile_decision(c, args.campaign, cid, ws, decision, adaptive_decision=adaptive_decision)
        decisions[token] = decision
        if persist_route_rows:
            persist_regime_decision(
                c, args.campaign, cid, ws, token, policy, inputs_hash=ihash,
                margin_used=knobs["margin"], clip_used=knobs["clip"],
                max_shares_used=knobs["max_shares"], min_edge_used=knobs["min_edge"],
                min_seconds_quote_used=knobs["min_seconds_quote"], now_ts=now_ts,
                episode=regime_episode, profile_decision=decision, adaptive_decision=adaptive_decision)
    return decisions


def persist_window(c, campaign, wb, *, commit=True):
    """Persist the in-flight window's inventory every poll (settled=0) so a mid-window restart restores it
    instead of silently zeroing inventory/cash (which corrupted that window's settlement)."""
    c.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,realized_pnl,
                 settled,winning_outcome,up_vwap,down_vwap,campaign_id,ts) VALUES(?,?,?,?,?,NULL,0,NULL,?,?,?,?)
                 ON CONFLICT(condition_id) DO UPDATE SET up_inv=excluded.up_inv,down_inv=excluded.down_inv,
                 cash=excluded.cash,up_vwap=excluded.up_vwap,down_vwap=excluded.down_vwap,
                 campaign_id=excluded.campaign_id,ts=excluded.ts
                 WHERE settled=0""",
              (wb.cid, wb.ws, wb.up_inv, wb.down_inv, wb.cash, wb.up_vwap, wb.down_vwap, campaign,
               int(time.time())))
    if commit:
        c.commit()


def _book_from_window_row(row):
    wb = WindowBook(row["condition_id"], row["window_start_ts"])
    wb.up_inv = row["up_inv"] or 0.0
    wb.down_inv = row["down_inv"] or 0.0
    wb.cash = row["cash"] or 0.0
    wb.up_vwap = row["up_vwap"]
    wb.down_vwap = row["down_vwap"]
    return wb


def book_from_fill_ledger(c, campaign, cid, ws):
    """Rebuild the authoritative book from persisted fills.

    This covers a mid-tick kill where a helper commit flushed maker_fills before maker_windows was updated.
    """
    rows = c.execute(
        "SELECT token, side, price, size FROM maker_fills "
        "WHERE campaign_id=? AND condition_id=? ORDER BY id",
        (campaign, cid),
    ).fetchall()
    if not rows:
        return None
    wb = WindowBook(cid, ws)
    for r in rows:
        wb._fill(r["token"], r["side"], float(r["price"]), float(r["size"]))
    return wb


def ledger_or_window_book(c, campaign, row):
    return (
        book_from_fill_ledger(c, campaign, row["condition_id"], row["window_start_ts"])
        or _book_from_window_row(row)
    )


def _regime_meta(regime_policy=None, *, regime_inputs_hash=None, margin_used=None, clip_used=None,
                 max_shares_used=None, min_edge_used=None, min_seconds_quote_used=None):
    if regime_policy is None:
        return {
            "regime_id": None, "policy_id": None, "route_reason": None,
            "regime_inputs_hash": regime_inputs_hash, "margin_used": margin_used, "clip_used": clip_used,
            "max_shares_used": max_shares_used, "min_edge_used": min_edge_used,
            "min_seconds_quote_used": min_seconds_quote_used, "bid_enabled": None, "ask_enabled": None,
            "exit_only": None, "risk_action": None, "can_hard_cut": None, "quote_ttl_ms": None,
            "warnings_json": None,
        }
    return {
        "regime_id": regime_policy.regime_id,
        "policy_id": regime_policy.policy_id,
        "route_reason": regime_policy.reason,
        "regime_inputs_hash": regime_inputs_hash,
        "margin_used": margin_used,
        "clip_used": clip_used,
        "max_shares_used": max_shares_used,
        "min_edge_used": min_edge_used,
        "min_seconds_quote_used": min_seconds_quote_used,
        "bid_enabled": int(bool(regime_policy.bid_enabled)),
        "ask_enabled": int(bool(regime_policy.ask_enabled)),
        "exit_only": int(bool(regime_policy.exit_only)),
        "risk_action": regime_policy.risk_action,
        "can_hard_cut": int(bool(regime_policy.can_hard_cut)),
        "quote_ttl_ms": regime_policy.quote_ttl_ms,
        "warnings_json": json.dumps(list(regime_policy.warnings)),
    }


def _compact_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def persist_episode(c, episode):
    key = _compact_json(episode.moment_key)
    features = _compact_json(episode.moment_features)
    c.execute("""INSERT INTO maker_regime_episodes(
                   episode_id,campaign_id,condition_id,window_start_ts,window_end_ts,regime_id,
                   previous_regime_id,started_ts,ended_ts,last_seen_ts,dwell_ms,transition_reason,
                   transition_confidence,regime_confidence,moment_schema_version,moment_key,
                   moment_features_json,inventory_state,odds_bucket,volatility_state,book_state,
                   spread_state,direction_state,source_snapshot_hash)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(episode_id) DO UPDATE SET
                   ended_ts=excluded.ended_ts,last_seen_ts=excluded.last_seen_ts,dwell_ms=excluded.dwell_ms,
                   regime_confidence=excluded.regime_confidence,moment_key=excluded.moment_key,
                   moment_features_json=excluded.moment_features_json,inventory_state=excluded.inventory_state,
                   odds_bucket=excluded.odds_bucket,volatility_state=excluded.volatility_state,
                   book_state=excluded.book_state,spread_state=excluded.spread_state,
                   direction_state=excluded.direction_state,source_snapshot_hash=excluded.source_snapshot_hash""",
              (episode.episode_id, episode.campaign_id, episode.condition_id, episode.window_start_ts,
               episode.window_end_ts, episode.current_regime, episode.previous_regime, episode.entered_ts,
               None, episode.last_seen_ts, episode.dwell_seconds * 1000, episode.transition_reason,
               episode.transition_confidence, episode.regime_confidence, episode.moment_schema_version, key,
               features, episode.moment_key.get("inventory_state"), episode.moment_key.get("odds_bucket"),
               episode.moment_key.get("volatility_state"), episode.moment_key.get("book_health"),
               episode.moment_key.get("spread_state"), episode.moment_key.get("direction_state"),
               episode.source_snapshot_hash))


def persist_transition(c, campaign, transition):
    c.execute("""INSERT INTO maker_regime_transitions(
                   campaign_id,episode_id,condition_id,window_start_ts,from_regime_id,to_regime_id,ts,
                   reason,confidence)
                 VALUES(?,?,?,?,?,?,?,?,?)""",
              (campaign, transition.episode_id, transition.condition_id, transition.window_start_ts,
               transition.from_regime, transition.to_regime, transition.ts, transition.reason,
               transition.confidence))


def persist_profile_decision(c, campaign, cid, ws, decision, adaptive_decision=None):
    c.execute("""INSERT INTO maker_profile_decisions(
                   profile_decision_id,campaign_id,condition_id,window_start_ts,episode_id,token,outcome,
                   decision_ts,profile_id,profile_mode,base_policy_id,profile_policy_hash,bid_enabled,
                   ask_enabled,exit_only,hard_cut,margin_used,clip_used,max_shares_used,min_edge_used,
                   quote_ttl_ms,reason,warnings_json)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(profile_decision_id) DO NOTHING""",
              (decision.profile_decision_id, campaign, cid, ws, decision.episode_id, decision.token,
               decision.outcome, decision.decision_ts, decision.profile_id, decision.profile_mode,
               decision.base_policy_id, decision.profile_policy_hash,
               None if decision.bid_enabled is None else int(decision.bid_enabled),
               None if decision.ask_enabled is None else int(decision.ask_enabled),
               None if decision.exit_only is None else int(decision.exit_only),
               None if decision.hard_cut is None else int(decision.hard_cut),
               decision.margin_used, decision.clip_used, decision.max_shares_used, decision.min_edge_used,
               decision.quote_ttl_ms, decision.reason, _compact_json(list(decision.warnings))))
    meta = AD.adaptive_meta(adaptive_decision)
    c.execute("""UPDATE maker_profile_decisions SET selector_version=?,reward_version=?,selection_mode=?,
                 exploration_flag=?,profile_disabled_flag=?,parent_context_used=?,adaptive_context_key=?,
                 fallback_depth=?,fallback_reason=?,exploration_budget_remaining=?,adaptive_status=?
                 WHERE profile_decision_id=?""",
              (meta["selector_version"], meta["reward_version"], meta["selection_mode"],
               meta["exploration_flag"], meta["profile_disabled_flag"], meta["parent_context_used"],
               meta["adaptive_context_key"], meta["fallback_depth"], meta["fallback_reason"],
               meta["exploration_budget_remaining"], meta["adaptive_status"], decision.profile_decision_id))


def _episode_profile_meta(episode=None, profile_decision=None, adaptive_decision=None):
    if episode is None and profile_decision is None:
        return {
            "episode_id": None, "moment_key": None, "moment_schema_version": None, "profile_id": None,
            "profile_mode": None, "profile_decision_id": None, "policy_hash": None,
            "quote_created_regime_id": None, "quote_created_episode_id": None,
            **AD.adaptive_meta(adaptive_decision),
        }
    adaptive_meta = AD.adaptive_meta(adaptive_decision)
    return {
        "episode_id": episode.episode_id if episode else None,
        "moment_key": _compact_json(episode.moment_key) if episode else None,
        "moment_schema_version": episode.moment_schema_version if episode else None,
        "profile_id": profile_decision.profile_id if profile_decision else None,
        "profile_mode": profile_decision.profile_mode if profile_decision else None,
        "profile_decision_id": profile_decision.profile_decision_id if profile_decision else None,
        "policy_hash": profile_decision.profile_policy_hash if profile_decision else None,
        "quote_created_regime_id": episode.current_regime if episode else None,
        "quote_created_episode_id": episode.episode_id if episode else None,
        **adaptive_meta,
    }


def persist_regime_decision(c, campaign, cid, ws, token, regime_policy, *, inputs_hash, margin_used,
                            clip_used, max_shares_used, min_edge_used, min_seconds_quote_used, now_ts,
                            episode=None, profile_decision=None, adaptive_decision=None):
    """Append the route decision even when no quote is posted, so no-quote states are evaluable."""
    m = _regime_meta(regime_policy, regime_inputs_hash=inputs_hash, margin_used=margin_used,
                     clip_used=clip_used, max_shares_used=max_shares_used, min_edge_used=min_edge_used,
                     min_seconds_quote_used=min_seconds_quote_used)
    cur = c.execute("""INSERT INTO maker_regime_decisions(campaign_id,condition_id,window_start_ts,token,
                 regime_id,policy_id,route_reason,regime_inputs_hash,margin_used,clip_used,max_shares_used,
                 min_edge_used,min_seconds_quote_used,bid_enabled,ask_enabled,exit_only,risk_action,
                 can_hard_cut,quote_ttl_ms,warnings_json,ts)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (campaign, cid, ws, token, m["regime_id"], m["policy_id"], m["route_reason"],
               m["regime_inputs_hash"], m["margin_used"], m["clip_used"], m["max_shares_used"],
               m["min_edge_used"], m["min_seconds_quote_used"], m["bid_enabled"], m["ask_enabled"],
               m["exit_only"], m["risk_action"], m["can_hard_cut"], m["quote_ttl_ms"],
               m["warnings_json"], int(now_ts)))
    sm = _episode_profile_meta(episode, profile_decision, adaptive_decision)
    c.execute("""UPDATE maker_regime_decisions SET episode_id=?,moment_key=?,moment_schema_version=?,
                 profile_id=?,profile_mode=?,profile_decision_id=?,policy_hash=?,
                 selector_version=?,reward_version=?,selection_mode=?,exploration_flag=?,
                 profile_disabled_flag=?,parent_context_used=?,adaptive_context_key=?,
                 fallback_depth=?,fallback_reason=?,exploration_budget_remaining=?,adaptive_status=?
                 WHERE id=?""",
              (sm["episode_id"], sm["moment_key"], sm["moment_schema_version"], sm["profile_id"],
               sm["profile_mode"], sm["profile_decision_id"], sm["policy_hash"],
               sm["selector_version"], sm["reward_version"], sm["selection_mode"], sm["exploration_flag"],
               sm["profile_disabled_flag"], sm["parent_context_used"], sm["adaptive_context_key"],
               sm["fallback_depth"], sm["fallback_reason"], sm["exploration_budget_remaining"],
               sm["adaptive_status"],
               cur.lastrowid))


def persist_quote_snapshot(c, campaign, cid, ws, token, quotes, *, fair, best_bid, best_ask, seconds_left, now_ts,
                           regime_policy=None, regime_inputs_hash=None, margin_used=None, clip_used=None,
                           max_shares_used=None, min_edge_used=None, min_seconds_quote_used=None,
                           episode=None, profile_decision=None, adaptive_decision=None):
    """Append bid/ask quote state every poll. Replay needs pulled sides as well as posted quotes.
    Returns {side: maker_quotes rowid} for POSTED sides (None for pulled) so the next tick's fills can
    reference the exact quote they filled against (maker_fills.quote_id — adjudicates refill vs double-count)."""
    reason = (quotes or {}).get("reason")
    m = _regime_meta(regime_policy, regime_inputs_hash=regime_inputs_hash, margin_used=margin_used,
                     clip_used=clip_used, max_shares_used=max_shares_used, min_edge_used=min_edge_used,
                     min_seconds_quote_used=min_seconds_quote_used)
    sm = _episode_profile_meta(episode, profile_decision, adaptive_decision)
    rowids = {}
    for side in ("bid", "ask"):
        q = (quotes or {}).get(side)
        status = "posted" if q is not None else "pulled"
        cur = c.execute("""INSERT INTO maker_quotes(campaign_id,condition_id,window_start_ts,token,side,status,
                     price,size,fair,best_bid,best_ask,seconds_left,ts,reason)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (campaign, cid, ws, token, side, status, (q.price if q else None), (q.size if q else None),
                   fair, best_bid, best_ask, seconds_left, int(now_ts), reason))
        rowids[side] = cur.lastrowid if q is not None else None
        c.execute("""UPDATE maker_quotes SET regime_id=?,policy_id=?,route_reason=?,regime_inputs_hash=?,
                     margin_used=?,clip_used=?,max_shares_used=?,min_edge_used=?,min_seconds_quote_used=?,
                     bid_enabled=?,ask_enabled=?,exit_only=?,risk_action=?,can_hard_cut=?,quote_ttl_ms=?,
                     episode_id=?,moment_key=?,moment_schema_version=?,profile_id=?,profile_mode=?,
                     profile_decision_id=?,policy_hash=?,quote_created_regime_id=?,quote_created_episode_id=?,
                     selector_version=?,reward_version=?,selection_mode=?,exploration_flag=?,
                     profile_disabled_flag=?,parent_context_used=?,adaptive_context_key=?,
                     fallback_depth=?,fallback_reason=?,exploration_budget_remaining=?,adaptive_status=?
                     WHERE rowid=last_insert_rowid()""",
                  (m["regime_id"], m["policy_id"], m["route_reason"], m["regime_inputs_hash"],
                   m["margin_used"], m["clip_used"], m["max_shares_used"], m["min_edge_used"],
                   m["min_seconds_quote_used"], m["bid_enabled"], m["ask_enabled"], m["exit_only"],
                   m["risk_action"], m["can_hard_cut"], m["quote_ttl_ms"], sm["episode_id"],
                   sm["moment_key"], sm["moment_schema_version"], sm["profile_id"], sm["profile_mode"],
                   sm["profile_decision_id"], sm["policy_hash"], sm["quote_created_regime_id"],
                   sm["quote_created_episode_id"], sm["selector_version"], sm["reward_version"],
                   sm["selection_mode"], sm["exploration_flag"], sm["profile_disabled_flag"],
                   sm["parent_context_used"], sm["adaptive_context_key"], sm["fallback_depth"],
                   sm["fallback_reason"], sm["exploration_budget_remaining"], sm["adaptive_status"]))
    return rowids


def record_fill(c, campaign, wb, token, side, price, size, fair, *, now_ts, reason, fill_model, max_shares=None,
                quote_id=None, regime_policy=None, regime_inputs_hash=None, margin_used=None, clip_used=None,
                max_shares_used=None, min_edge_used=None, min_seconds_quote_used=None,
                fill_episode=None, profile_decision=None, adaptive_decision=None):
    """Apply a paper fill and persist it with exchange/server time, not the skewed local wall clock.
    quote_id = the maker_quotes rowid the fill executed against (NULL for hard cuts / post-restart ticks)."""
    if side == "bid" and max_shares is not None:
        inv = wb.up_inv if token == "up" else wb.down_inv
        size = min(size, max(0.0, float(max_shares) - inv))
    executed = wb._fill(token, side, price, size)
    if executed <= 0:
        return
    m = _regime_meta(regime_policy, regime_inputs_hash=regime_inputs_hash, margin_used=margin_used,
                     clip_used=clip_used, max_shares_used=max_shares_used, min_edge_used=min_edge_used,
                     min_seconds_quote_used=min_seconds_quote_used)
    sm = _episode_profile_meta(fill_episode, profile_decision, adaptive_decision)
    quote_ctx = None
    if quote_id is not None:
        quote_ctx = c.execute(
            "SELECT episode_id,regime_id,profile_id,quote_created_episode_id,quote_created_regime_id "
            "FROM maker_quotes WHERE id=?", (quote_id,)
        ).fetchone()
    c.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,
                 side,price,size,fair,ts,reason,fill_model,quote_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (campaign, wb.cid, wb.ws, token, side, price, executed, fair, int(now_ts), reason, fill_model,
               quote_id))
    c.execute("""UPDATE maker_fills SET regime_id=?,policy_id=?,route_reason=?,regime_inputs_hash=?,
                 margin_used=?,clip_used=?,max_shares_used=?,min_edge_used=?,min_seconds_quote_used=?,
                 bid_enabled=?,ask_enabled=?,exit_only=?,risk_action=?,can_hard_cut=?,quote_ttl_ms=?,
                 episode_id=?,moment_key=?,moment_schema_version=?,profile_id=?,profile_mode=?,
                 profile_decision_id=?,policy_hash=?,quote_episode_id=?,fill_episode_id=?,
                 quote_regime_id=?,fill_regime_id=?,quote_profile_id=?,fill_profile_id=?,
                 selector_version=?,reward_version=?,selection_mode=?,exploration_flag=?,
                 profile_disabled_flag=?,parent_context_used=?,adaptive_context_key=?,
                 fallback_depth=?,fallback_reason=?,exploration_budget_remaining=?,adaptive_status=?
                 WHERE rowid=last_insert_rowid()""",
              (m["regime_id"], m["policy_id"], m["route_reason"], m["regime_inputs_hash"],
               m["margin_used"], m["clip_used"], m["max_shares_used"], m["min_edge_used"],
               m["min_seconds_quote_used"], m["bid_enabled"], m["ask_enabled"], m["exit_only"],
               m["risk_action"], m["can_hard_cut"], m["quote_ttl_ms"], sm["episode_id"],
               sm["moment_key"], sm["moment_schema_version"], sm["profile_id"], sm["profile_mode"],
               sm["profile_decision_id"], sm["policy_hash"],
               (quote_ctx["quote_created_episode_id"] or quote_ctx["episode_id"]) if quote_ctx else None,
               sm["episode_id"], (quote_ctx["quote_created_regime_id"] or quote_ctx["regime_id"]) if quote_ctx else None,
               fill_episode.current_regime if fill_episode else None,
               quote_ctx["profile_id"] if quote_ctx else None, sm["profile_id"],
               sm["selector_version"], sm["reward_version"], sm["selection_mode"], sm["exploration_flag"],
               sm["profile_disabled_flag"], sm["parent_context_used"], sm["adaptive_context_key"],
               sm["fallback_depth"], sm["fallback_reason"], sm["exploration_budget_remaining"],
               sm["adaptive_status"]))


def restore_window(c, campaign, cid):
    """Rebuild the WindowBook for a still-unsettled window on boot. None if unknown or already settled."""
    r = c.execute("SELECT * FROM maker_windows WHERE condition_id=? AND campaign_id=? AND settled=0",
                  (cid, campaign)).fetchone()
    if r is None:
        return None
    return ledger_or_window_book(c, campaign, r)


tape_prints = LD.tape_prints   # canonical implementation lives in live_desk (shared with the scalper)


def _resolve(slug):
    try:
        ev = LD._get(f"{LD.GAMMA}{slug}")
        if not ev:
            return None
        spec, _ = MR.parse_event(ev[0])
        if spec is None:
            return None
        return S.from_gamma(spec).winning_outcome
    except Exception:  # noqa: BLE001
        return None


def settle_window(c, campaign, wb):
    try:
        wb = book_from_fill_ledger(c, campaign, wb.cid, wb.ws) or wb
        outcome = _resolve(f"{LD.SLUG_PREFIX}{wb.ws}")
        if outcome is None:
            return None
        up_won = str(outcome).lower().startswith("up")
        up_pay = wb.up_inv * (1.0 if up_won else 0.0)
        down_pay = wb.down_inv * (0.0 if up_won else 1.0)
        realized = wb.cash + up_pay + down_pay
        c.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,realized_pnl,
                     settled,winning_outcome,campaign_id,ts) VALUES(?,?,?,?,?,?,1,?,?,?)
                     ON CONFLICT(condition_id) DO UPDATE SET up_inv=excluded.up_inv,down_inv=excluded.down_inv,
                     cash=excluded.cash,realized_pnl=excluded.realized_pnl,settled=1,
                     winning_outcome=excluded.winning_outcome,campaign_id=excluded.campaign_id,ts=excluded.ts""",
                  (wb.cid, wb.ws, wb.up_inv, wb.down_inv, wb.cash, realized, str(outcome), campaign,
                   int(time.time())))
        if c.execute("SELECT 1 FROM crypto5m_maker_adaptive_campaigns WHERE campaign_id=?",
                     (campaign,)).fetchone():
            AD.update_rewards_for_settled_window(
                c,
                campaign_id=campaign,
                condition_id=wb.cid,
                window_start_ts=wb.ws,
                winning_outcome=str(outcome),
                now_ts=int(time.time()),
            )
        if c.execute("SELECT 1 FROM crypto5m_maker_v3_campaigns WHERE campaign_id=?",
                     (campaign,)).fetchone():
            AD3.update_rewards_for_settled_window(
                c,
                campaign_id=campaign,
                condition_id=wb.cid,
                window_start_ts=wb.ws,
                winning_outcome=str(outcome),
                now_ts=int(time.time()),
            )
        if AD33.is_v33_campaign(c, campaign):
            # paired window rewards; sweep also retries windows whose baseline settled late
            AD33.sweep_for_campaign(c, campaign, int(time.time()))
        c.commit()
        return realized
    except Exception as e:  # noqa: BLE001
        print(f"[settle] {wb.cid} error: {e}", flush=True); return None


def persist_shadow_window(c, campaign, wb, *, commit=True):
    """Persist regime-shadow inventory separately from maker_windows, which is keyed by condition_id."""
    c.execute("""INSERT INTO maker_shadow_windows(campaign_id,condition_id,window_start_ts,up_inv,down_inv,cash,
                 realized_pnl,settled,winning_outcome,up_vwap,down_vwap,ts)
                 VALUES(?,?,?,?,?,?,NULL,0,NULL,?,?,?)
                 ON CONFLICT(campaign_id,condition_id) DO UPDATE SET
                 window_start_ts=excluded.window_start_ts,up_inv=excluded.up_inv,down_inv=excluded.down_inv,
                 cash=excluded.cash,up_vwap=excluded.up_vwap,down_vwap=excluded.down_vwap,ts=excluded.ts
                 WHERE settled=0""",
              (campaign, wb.cid, wb.ws, wb.up_inv, wb.down_inv, wb.cash, wb.up_vwap, wb.down_vwap,
               int(time.time())))
    if commit:
        c.commit()


def restore_shadow_window(c, campaign, cid):
    r = c.execute("SELECT * FROM maker_shadow_windows WHERE campaign_id=? AND condition_id=? AND settled=0",
                  (campaign, cid)).fetchone()
    if r is None:
        return None
    wb = WindowBook(cid, r["window_start_ts"])
    wb.up_inv = r["up_inv"] or 0.0
    wb.down_inv = r["down_inv"] or 0.0
    wb.cash = r["cash"] or 0.0
    wb.up_vwap = r["up_vwap"]
    wb.down_vwap = r["down_vwap"]
    return wb


def settle_shadow_window(c, campaign, wb):
    try:
        outcome = _resolve(f"{LD.SLUG_PREFIX}{wb.ws}")
        if outcome is None:
            return None
        up_won = str(outcome).lower().startswith("up")
        up_pay = wb.up_inv * (1.0 if up_won else 0.0)
        down_pay = wb.down_inv * (0.0 if up_won else 1.0)
        realized = wb.cash + up_pay + down_pay
        c.execute("""INSERT INTO maker_shadow_windows(campaign_id,condition_id,window_start_ts,up_inv,down_inv,
                     cash,realized_pnl,settled,winning_outcome,up_vwap,down_vwap,ts)
                     VALUES(?,?,?,?,?,?,?,1,?,?,?,?)
                     ON CONFLICT(campaign_id,condition_id) DO UPDATE SET
                     up_inv=excluded.up_inv,down_inv=excluded.down_inv,cash=excluded.cash,
                     realized_pnl=excluded.realized_pnl,settled=1,winning_outcome=excluded.winning_outcome,
                     up_vwap=excluded.up_vwap,down_vwap=excluded.down_vwap,ts=excluded.ts""",
                  (campaign, wb.cid, wb.ws, wb.up_inv, wb.down_inv, wb.cash, realized, str(outcome),
                   wb.up_vwap, wb.down_vwap, int(time.time())))
        c.commit()
        return realized
    except Exception as e:  # noqa: BLE001
        print(f"[shadow-settle] {wb.cid} error: {e}", flush=True); return None


def settle_shadow_stale(c, campaign, now_ts, grace_s=SETTLE_GRACE_S, abandon_s=SETTLE_ABANDON_S):
    n = 0
    rows = c.execute("""SELECT * FROM maker_shadow_windows
                     WHERE campaign_id=?
                       AND (settled=0 OR (settled=1 AND winning_outcome='unresolved' AND realized_pnl IS NULL))
                       AND window_start_ts + 300 + ? <= ?""",
                     (campaign, grace_s, now_ts)).fetchall()
    for r in rows:
        wb = WindowBook(r["condition_id"], r["window_start_ts"])
        wb.up_inv = r["up_inv"] or 0.0
        wb.down_inv = r["down_inv"] or 0.0
        wb.cash = r["cash"] or 0.0
        wb.up_vwap = r["up_vwap"]
        wb.down_vwap = r["down_vwap"]
        realized = settle_shadow_window(c, campaign, wb)
        if realized is not None:
            n += 1
        elif not (r["settled"] and r["winning_outcome"] == "unresolved") and now_ts >= r["window_start_ts"] + 300 + abandon_s:
            c.execute("UPDATE maker_shadow_windows SET settled=1, winning_outcome='unresolved', ts=? "
                      "WHERE campaign_id=? AND condition_id=? AND settled=0",
                      (int(now_ts), campaign, r["condition_id"]))
            c.commit()
    return n


def _v3_seconds_bucket(seconds_left):
    try:
        left = float(seconds_left)
    except (TypeError, ValueError):
        return "unknown"
    if left <= 20:
        return "final"
    if left <= 60:
        return "late"
    if left >= 210:
        return "early"
    return "mid"


def _v3_disagreement_bucket(q_structural, q_market_mid):
    if q_structural is None or q_market_mid is None:
        return "unknown"
    diff = abs(float(q_structural) - float(q_market_mid))
    if diff >= 0.20:
        return "very_wide"
    if diff >= 0.10:
        return "wide"
    if diff >= 0.04:
        return "moderate"
    return "tight"


def _v3_fair_context(desk, moment_key, q_structural, q_market_mid):
    moment = moment_key if isinstance(moment_key, dict) else {}
    if not moment and isinstance(moment_key, str):
        try:
            moment = json.loads(moment_key)
        except json.JSONDecodeError:
            moment = {}
    return {
        "seconds_bucket": _v3_seconds_bucket(desk.get("seconds_left")),
        "odds_bucket": moment.get("odds_bucket") or "unknown",
        "volatility_state": moment.get("volatility_state") or ("jump" if desk.get("jump_flag") else "unknown"),
        "jump_state": "jump" if desk.get("jump_flag") else "none",
        "source_health": "healthy" if q_structural is not None else "structural_missing",
        "spread_state": moment.get("spread_state") or "unknown",
        "disagreement_bucket": _v3_disagreement_bucket(q_structural, q_market_mid),
        "book_health": moment.get("book_health") or moment.get("book_state") or "healthy",
    }


def _v3_transition_state(episode, now_ts):
    if episode is None:
        return "unknown"
    dwell_ms = getattr(episode, "dwell_ms", None)
    if dwell_ms is not None and dwell_ms < 3_000:
        return "fresh_transition"
    entered = getattr(episode, "entered_ts", None)
    if entered is not None and (float(now_ts) - float(entered)) < 3.0:
        return "fresh_transition"
    return "stable"


def _v3_decision_json(fair, risk, action):
    return {
        "fair": {
            "fair_model_id": fair.fair_model_id,
            "q_structural": fair.q_structural,
            "q_market_mid": fair.q_market_mid,
            "q_used": round(fair.q_used, 4),
            "blend_weight": round(fair.blend_weight, 4),
            "residual_adjustment": round(fair.residual_adjustment, 4),
            "fair_confidence": fair.fair_confidence,
            "fair_source": fair.fair_source,
            "fair_context_key": fair.fair_context_key,
        },
        "risk": {
            "risk_state": risk.risk_state,
            "tail_risk_score": round(risk.tail_risk_score, 4),
            "transition_risk_score": round(risk.transition_risk_score, 4),
            "forced_arm": risk.forced_arm,
            "reason": risk.reason,
        },
        "action": {
            "arm_id": action.arm_id,
            "selection_source": action.selection_source,
            "exploration": bool(action.exploration_flag),
            "context_key": action.context_key,
            "parent_context_used": action.parent_context_used,
            "fallback_depth": action.fallback_depth,
            "n_fills": action.n_fills,
            "mean_reward": round(action.mean_reward, 6),
        },
    }


def run(args):
    c = _db(args.paper_db)
    vol_floor = getattr(args, "vol_floor", None)
    if vol_floor:
        # per-PROCESS fair-anchor override (v3.2f lever). Each campaign is its own process, so this
        # never touches the control's or siblings' fair values — the module constant stays 1e-4.
        LD.VOL_FLOOR_PER_S = float(vol_floor)
        print(f"[maker] vol floor override: VOL_FLOOR_PER_S={LD.VOL_FLOOR_PER_S}")
    # campaign maker-regime-gated-v1: per-window floor switch. None => feature off; every existing
    # campaign leaves this unset, so their tick path is unchanged. Tuple (quiet_floor, contested_floor).
    regime_gated_floor = tuple(args.regime_gated_floor) if getattr(args, "regime_gated_floor", None) else None
    if regime_gated_floor:
        print(f"[maker] regime-gated floor ON: quiet={regime_gated_floor[0]} contested={regime_gated_floor[1]}")
    # deep-only (maker-deep-only-v1): min bid depth below the touch; 0 = normal. Threaded into desired_quotes.
    deep_only_min_depth = float(getattr(args, "deep_only_min_depth", 0.0) or 0.0)
    if deep_only_min_depth > 0:
        print(f"[maker] DEEP-ONLY ON: no top-of-book bids; rest bids >= {deep_only_min_depth} below the touch")
    # regime-gated-v2: continuous log-ramp floor (HONEST, WIDE, X_LO, X_HI). Mutually exclusive with v1.
    regime_gated_ramp = tuple(args.regime_gated_ramp) if getattr(args, "regime_gated_ramp", None) else None
    if regime_gated_ramp:
        if regime_gated_floor:
            raise SystemExit("[maker] --regime-gated-ramp and --regime-gated-floor are mutually exclusive")
        _h, _w, _xlo, _xhi = regime_gated_ramp
        if not (_xlo < _xhi):
            raise SystemExit(f"[maker] --regime-gated-ramp requires X_LO < X_HI (got {_xlo}, {_xhi})")
        if not (_h > 0 and _w > 0):
            # log-interpolation needs positive anchors; catch a config typo LOUDLY at launch instead
            # of a per-tick math.log ValueError silently swallowed by the loop's blanket except.
            raise SystemExit(f"[maker] --regime-gated-ramp requires HONEST>0 and WIDE>0 (got {_h}, {_w})")
        print(f"[maker] regime-gated RAMP ON: honest={_h} wide={_w} x_lo={_xlo} x_hi={_xhi}")
    adaptive_v3_enabled = bool(getattr(args, "maker_adaptive_v3", False))
    adaptive_frozen = bool(getattr(args, "adaptive_frozen", False))
    if adaptive_frozen:
        # frozen-learner ablation: the launch-prior policy held constant. Epsilon is forced to 0
        # regardless of the flag so a copy-pasted launch line cannot accidentally explore.
        args.adaptive_epsilon = 0.0
    if adaptive_v3_enabled:
        args.maker_regime_router = "active"
        AD3.register_campaign(
            c,
            args.campaign,
            getattr(args, "adaptive_control_campaign", "maker-adaptive-paper-v2"),
            int(time.time()),
            {
                "epsilon": getattr(args, "adaptive_epsilon", 0.10),
                "control_campaign": getattr(args, "adaptive_control_campaign", "maker-adaptive-paper-v2"),
                "vol_floor": float(vol_floor) if vol_floor else None,
                "frozen": adaptive_frozen,
            },
            reward_version=getattr(args, "adaptive_reward_version", None),
        )
        c.commit()
    adaptive_v33_enabled = bool(getattr(args, "maker_adaptive_v33", False))
    if adaptive_v33_enabled:
        # V3.3-learner: window-level PAIRED reward vs a baseline campaign; frozen fair;
        # evidence-gated arm deviation. Own state tables — never registers in the v3
        # campaigns table, so the v3 per-fill reward path stays off.
        args.maker_regime_router = "active"
        AD33.register_campaign(
            c,
            args.campaign,
            baseline_campaign=getattr(args, "v33_baseline_campaign", "maker-adaptive-paper-v3.2f-frozen"),
            baseline_db=getattr(args, "v33_baseline_db", "data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite"),
            now_ts=int(time.time()),
            config={"vol_floor": float(vol_floor) if vol_floor else None},
        )
        c.commit()
    if getattr(args, "maker_adaptive_paper", False):
        args.maker_regime_router = "active"
        adaptive_learner_version = getattr(args, "adaptive_learner_version", "v1")
        AD.register_campaign(
            c,
            args.campaign,
            getattr(args, "adaptive_control_campaign", "maker-regime-v1"),
            int(time.time()),
            {
                "epsilon": getattr(args, "adaptive_epsilon", 0.10),
                "control_campaign": getattr(args, "adaptive_control_campaign", "maker-regime-v1"),
            },
            learner_version=adaptive_learner_version,
        )
    else:
        adaptive_learner_version = "v1"
    adaptive_cfg = AD.learner_config(adaptive_learner_version)
    adaptive_rng = random.Random(getattr(args, "adaptive_seed", 1))
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(v=True))
    with open(args.pid, "w") as f:
        f.write(str(os.getpid()))
    print(f"[maker] start campaign={args.campaign} poll={args.poll}s db={args.paper_db}", flush=True)

    cur = None  # current WindowBook
    shadow_cur = None
    regime_episode = None
    shadow_campaign = getattr(args, "regime_shadow_campaign", "maker-regime-shadow-v1")
    iters = 0
    last_poll_ts = None   # bounds the tape-print window for the fill model
    dstate = DS.DirectionState()   # the retired scalper's confirmation memory — one instance, spans windows
    live_qids = {}    # token -> {side: maker_quotes rowid} from the PREVIOUS tick — fills link to these
    live_reasons = {}  # token -> reason string of the PREVIOUS tick's quote — fills explain against these (codex 2026-07-07)
    shadow_qids = {}
    while not stop["v"]:
        if args.max_iterations and iters >= args.max_iterations:
            break
        iters += 1
        try:
            desk = LD.build_live_desk()
            cid = desk.get("condition_id"); ws = desk.get("window_start_ts")
            fair_up = desk.get("fair_value_up"); left = desk.get("seconds_left")
            now_ts = desk.get("now_ts") or int(time.time())
            # directional read (persistence + horizons + order-flow) — quotes get pulled on a CONFIRMED move
            _ua, _ub = desk.get("up_ask"), desk.get("up_bid")
            _spread = (_ua - _ub) if (_ua is not None and _ub is not None and _ua > _ub) else None
            if regime_gated_floor is not None:
                # regime-gated-v1: classify this window (up-fair + book spread, both causal) and set
                # the floor for the NEXT build_live_desk() — quiet/one-sided/tight -> honest tight
                # floor (harvest), else -> wide protective floor. Per-process global, isolated.
                LD.VOL_FLOOR_PER_S = RGF.select_gated_floor(
                    fair_up, _spread, regime_gated_floor[0], regime_gated_floor[1])
            elif regime_gated_ramp is not None:
                # regime-gated-v2: continuous log-ramp of the floor by odds-extremity |fair-0.5|.
                # Same causal inputs, same per-process global isolation as v1.
                LD.VOL_FLOOR_PER_S = RGF.ramp_floor(
                    fair_up, _spread, regime_gated_ramp[0], regime_gated_ramp[1],
                    regime_gated_ramp[2], regime_gated_ramp[3])
            thesis = DIR.directional_thesis(
                ret_5s=desk.get("ret_5s"), ret_15s=desk.get("ret_15s"), ret_30s=desk.get("ret_30s"),
                ema_slope=desk.get("ema_slope"), microprice_gap=desk.get("microprice_gap"),
                imbalance=desk.get("book_imbalance"), volume_dir=desk.get("volume_dir"),
                order_flow=desk.get("tape_ofi_10s"), vol_per_s=desk.get("vol_per_s"), spread=_spread)
            confirm = dstate.update(thesis, desk, now_ts)
            # window rollover: persist the closed book and hand it to the RETRY sweep below. Settling
            # inline here (~2s after close) always raced Gamma and the one-shot attempt stranded windows.
            if cur and cur.ws != ws:
                persist_window(c, args.campaign, cur)
                cur = None
                regime_episode = None
                live_qids = {}
                live_reasons = {}
            if args.maker_regime_router == "shadow" and shadow_cur and shadow_cur.ws != ws:
                persist_shadow_window(c, shadow_campaign, shadow_cur)
                shadow_cur = None
                shadow_qids = {}
            # settlement retry sweep: settle every closed window once Gamma has the outcome (12s grace,
            # keeps retrying, abandons at 30m). This is what advances the n>=200 campaign counter.
            settle_stale(c, args.campaign, now_ts)
            if args.maker_regime_router == "shadow":
                settle_shadow_stale(c, shadow_campaign, now_ts)
            if cur is None and cid:
                cur = restore_window(c, args.campaign, cid) or WindowBook(cid, ws)
                if cur.up_inv or cur.down_inv or cur.cash:
                    print(f"[maker] restored {cid[:10]} up={cur.up_inv} down={cur.down_inv} "
                          f"cash={cur.cash:+.2f} (mid-window restart)", flush=True)
            if args.maker_regime_router == "shadow" and shadow_cur is None and cid:
                shadow_cur = restore_shadow_window(c, shadow_campaign, cid) or WindowBook(cid, ws)

            think = {"ts": int(time.time()), "window_start_ts": ws, "seconds_left": left,
                     "condition_id": cid, "fair_value_up": fair_up,
                     "btc_now": desk.get("btc_now"), "btc_start": desk.get("btc_start"),
                     "up_book": [desk.get("up_bid"), desk.get("up_ask")],
                     "down_book": [desk.get("down_bid"), desk.get("down_ask")],
                     "engine_version": (args.campaign if (getattr(args, "maker_adaptive_paper", False)
                                                          or adaptive_v3_enabled)
                                        else ENGINE_VERSION),
                     "campaign": args.campaign,
                     "read": {"side": thesis.get("side"), "score": thesis.get("score"),
                              "conviction": thesis.get("conviction"), "votes": thesis.get("votes"),
                              "confirmed_side": confirm.get("confirmed_side"),
                              "persistence_ticks": confirm.get("persistence_ticks"),
                              "horizons_agree": confirm.get("horizons_agree"),
                              "flow_agree": confirm.get("flow_agree")},
                     "z": desk.get("z"), "z_v2": desk.get("z_v2"),
                     "jump_flag": desk.get("jump_flag"), "divergence": desk.get("divergence"),
                     "vol_fast": desk.get("vol_fast"), "vol_slow": desk.get("vol_slow"),
                     "signal_series": desk.get("signal_series")}

            if cur and fair_up is not None:
                # jump regime: double the quote margin when the fast vol regime flags a jump (quote wider
                # into chaos instead of getting picked off at calm-market width)
                margin = args.margin * (2.0 if desk.get("jump_flag") else 1.0)
                tokens = [("up", fair_up, desk.get("up_bid"), desk.get("up_ask")),
                          ("down", 1 - fair_up, desk.get("down_bid"), desk.get("down_ask"))]
                tokens_by_token = {token: (f, bb, ba) for token, f, bb, ba in tokens}
                desired = {}
                regimes = {}
                active_profile_decisions = {}
                shadow_desired = {}
                shadow_regimes = {}
                pending_unwinds = {
                    token: list(unwind_fills(cur, token, f, bb, ba))
                    for token, f, bb, ba in tokens
                }
                adaptive_decisions = {}
                v3_decisions = {}
                v3_fair_overrides = {}
                if args.maker_regime_router == "active":
                    regimes = active_regime_routes(args, desk, tokens, cur, margin, confirm)
                    regime_episode = ensure_active_regime_episode(
                        c, args, cid, ws, now_ts, desk, fair_up, cur, confirm, regimes,
                        regime_episode)
                    if getattr(args, "maker_adaptive_paper", False):
                        applied = {}
                        for token, (policy, ihash, knobs) in regimes.items():
                            inv = cur.up_inv if token == "up" else cur.down_inv
                            adaptive = AD.select_profile(
                                c,
                                campaign_id=args.campaign,
                                regime_id=policy.regime_id,
                                moment_key=regime_episode.moment_key,
                                token_inventory=inv,
                                now_ts=int(now_ts),
                                rng=adaptive_rng,
                                epsilon=getattr(args, "adaptive_epsilon", 0.10),
                                learner_version=adaptive_learner_version,
                            )
                            policy, knobs = AD.apply_profile(policy, knobs, adaptive.profile_id,
                                                             token_inventory=inv)
                            adaptive_decisions[token] = adaptive
                            applied[token] = (policy, ihash, knobs)
                        regimes = applied
                    if adaptive_v3_enabled or adaptive_v33_enabled:
                        applied = {}
                        context_key = AD3.context_key_label(regime_episode.moment_key)
                        transition_state = _v3_transition_state(regime_episode, now_ts)
                        for token, (policy, ihash, knobs) in regimes.items():
                            f, bb, ba = tokens_by_token[token]
                            inv = cur.up_inv if token == "up" else cur.down_inv
                            market_mid = _mid(bb, ba)
                            fair_context = _v3_fair_context(desk, regime_episode.moment_key, f, market_mid)
                            fair = AD3.choose_fair_value(
                                c,
                                campaign_id=args.campaign,
                                q_structural=f,
                                q_market_mid=market_mid,
                                context=fair_context,
                                now_ts=int(now_ts),
                                # v33 fair is FROZEN by design: no Brier blend learning
                                frozen=adaptive_frozen or adaptive_v33_enabled,
                            )
                            risk = AD3.govern_tail_risk(
                                regime_id=policy.regime_id,
                                moment_key=regime_episode.moment_key,
                                token_inventory=inv,
                                seconds_left=left,
                                fair_confidence=fair.fair_confidence,
                                transition_state=transition_state,
                            )
                            if adaptive_v33_enabled:
                                d33 = AD33.select_action_arm(
                                    c,
                                    campaign_id=args.campaign,
                                    context_key=context_key,
                                    regime_id=policy.regime_id,
                                    fair_confidence=fair.fair_confidence,
                                    risk_state=risk.risk_state,
                                    token_inventory=inv,
                                    rng=adaptive_rng,
                                    epsilon=getattr(args, "adaptive_epsilon", AD33.V33_EPSILON),
                                    now_ts=int(now_ts),
                                )
                                action = AD3._action_decision(
                                    d33.arm_id, context_key, d33.level_used, 0,
                                    d33.selection_source, d33.exploration_flag, False,
                                    d33.n_windows, d33.decayed_mean or 0.0,
                                    d33.ci_low, None, fair.fair_confidence,
                                    risk.risk_state, d33.reason,
                                )
                            else:
                                action = AD3.select_action_arm(
                                    c,
                                    campaign_id=args.campaign,
                                    context_key=context_key,
                                    regime_id=policy.regime_id,
                                    fair_confidence=fair.fair_confidence,
                                    risk_state=risk.risk_state,
                                    token_inventory=inv,
                                    rng=adaptive_rng,
                                    epsilon=getattr(args, "adaptive_epsilon", 0.10),
                                    now_ts=int(now_ts),
                                    frozen=adaptive_frozen,
                                )
                            policy, knobs = AD3.apply_action_arm(
                                policy, knobs, action.arm_id, token_inventory=inv, token_price=fair.q_used)
                            AD3.record_policy_action(
                                c,
                                campaign_id=args.campaign,
                                condition_id=cid,
                                window_start_ts=ws,
                                token=token,
                                context_key=context_key,
                                fair_model_id=fair.fair_model_id,
                                q_structural=fair.q_structural,
                                q_market_mid=fair.q_market_mid,
                                q_used=fair.q_used,
                                fair_blend_weight=fair.blend_weight,
                                fair_context_key=fair.fair_context_key,
                                residual_adjustment=fair.residual_adjustment,
                                fair_confidence=fair.fair_confidence,
                                risk_state=risk.risk_state,
                                tail_risk_score=risk.tail_risk_score,
                                quote_action_arm=action.arm_id,
                                selector_source=action.selection_source,
                                exploration_flag=action.exploration_flag,
                                parent_context_used=action.parent_context_used,
                                fallback_depth=action.fallback_depth,
                                disabled_profile_flag=action.profile_disabled_flag,
                                now_ts=int(now_ts),
                                # v33 owns its own state — never bump v3 arm stats
                                frozen=adaptive_frozen or adaptive_v33_enabled,
                            )
                            AD3.record_ablation_rows(
                                c,
                                campaign_id=args.campaign,
                                condition_id=cid,
                                window_start_ts=ws,
                                token=token,
                                context_key=context_key,
                                selected_arm=action.arm_id,
                                q_used=fair.q_used,
                                risk_state=risk.risk_state,
                                now_ts=int(now_ts),
                            )
                            v3_decisions[token] = {"fair": fair, "risk": risk, "action": action}
                            v3_fair_overrides[token] = fair.q_used
                            applied[token] = (policy, ihash, knobs)
                        regimes = applied
                    active_profile_decisions = build_profile_decisions(
                        c, args, cid, ws, now_ts, cur, regime_episode, regimes, persist_route_rows=True,
                        adaptive_decisions=adaptive_decisions)
                    think["regime_episode"] = episode_json(regime_episode)
                    think["profile_decisions"] = {
                        token: profile_decision_json(
                            decision, adaptive_decision=adaptive_decisions.get(token))
                        for token, decision in active_profile_decisions.items()
                    }
                    if getattr(args, "maker_adaptive_paper", False):
                        think["adaptive_paper"] = {
                            "campaign": args.campaign,
                            "control_campaign": getattr(args, "adaptive_control_campaign", "maker-regime-v1"),
                            "selector_version": adaptive_cfg.selector_version,
                            "reward_version": adaptive_cfg.reward_version,
                            "learner_version": adaptive_cfg.learner_version,
                            "profiles": {
                                token: {
                                    "profile_id": adaptive.profile_id,
                                    "selection_source": adaptive.selection_source,
                                    "exploration": adaptive.exploration_flag,
                                    "context_key": adaptive.context_key,
                                    "parent_context_used": adaptive.parent_context_used,
                                    "fallback_depth": adaptive.fallback_depth,
                                    "fallback_reason": adaptive.fallback_reason,
                                    "exploration_budget_remaining": adaptive.exploration_budget_remaining,
                                    "adaptive_status": adaptive.adaptive_status,
                                    "n_fills": adaptive.n_fills,
                                    "mean_reward": round(adaptive.mean_reward, 6),
                                }
                                for token, adaptive in adaptive_decisions.items()
                            },
                        }
                    if adaptive_v3_enabled:
                        think["adaptive_v3"] = {
                            "campaign": args.campaign,
                            "control_campaign": getattr(args, "adaptive_control_campaign", "maker-adaptive-paper-v2"),
                            "learner_version": AD3.LEARNER_VERSION,
                            "selector_version": AD3.SELECTOR_VERSION,
                            # the campaign's actual reward version, not the module constant — v3.2r
                            # runs with --adaptive-reward-version and must not report v3.1
                            "reward_version": getattr(args, "adaptive_reward_version", None) or AD3.REWARD_VERSION,
                            "ablation_groups": ["A0", "A1", "A2", "A3", "A4"],
                            "tokens": {
                                token: _v3_decision_json(parts["fair"], parts["risk"], parts["action"])
                                for token, parts in v3_decisions.items()
                            },
                        }
                for token, f, bb, ba in tokens:
                    if token in v3_fair_overrides:
                        f = v3_fair_overrides[token]
                    # HARD CUT-LOSS first (was dead code): fair moved through our avg entry -> cross + flatten
                    unwinds = (list(unwind_fills(cur, token, f, bb, ba))
                               if adaptive_v3_enabled else pending_unwinds.get(token, []))
                    for side, price, size, fmodel in unwinds:
                        hard_cut_meta = regimes.get(token) if regimes else None
                        meta = hard_cut_meta or (None, None, {})
                        record_fill(c, args.campaign, cur, token, side, price, size, f, now_ts=now_ts,
                                    reason="fair moved through entry — hard cut", fill_model=fmodel,
                                    regime_policy=meta[0], regime_inputs_hash=meta[1],
                                    margin_used=meta[2].get("margin"), clip_used=meta[2].get("clip"),
                                    max_shares_used=meta[2].get("max_shares"),
                                    min_edge_used=meta[2].get("min_edge"),
                                    min_seconds_quote_used=meta[2].get("min_seconds_quote"),
                                    fill_episode=regime_episode,
                                    profile_decision=active_profile_decisions.get(token),
                                    adaptive_decision=adaptive_decisions.get(token))
                        cur.prev[token] = {}   # pull our resting quotes with the cut
                        print(f"[maker] HARD-CUT {token} {size} @ {price} (fair {f:.2f})", flush=True)
                    inv = cur.up_inv if token == "up" else cur.down_inv
                    knobs = {"margin": margin, "clip": args.clip, "max_shares": args.max_shares,
                             "min_edge": args.min_edge, "min_seconds_quote": args.min_seconds_quote}
                    policy = ihash = None
                    if args.maker_regime_router == "active":
                        policy, ihash, knobs = regimes[token]
                    q = MP.desired_quotes(
                        fair=f, best_bid=bb, best_ask=ba, inventory=inv,
                        max_inventory=(knobs["max_shares"] if args.maker_regime_router == "active" else args.max_shares),
                        seconds_left=left,
                        base_margin=(knobs["margin"] if args.maker_regime_router == "active" else margin),
                        base_size=(knobs["clip"] if args.maker_regime_router == "active" else args.clip),
                        min_seconds_quote=(knobs["min_seconds_quote"] if args.maker_regime_router == "active"
                                           else args.min_seconds_quote),
                        min_quote_edge=(knobs["min_edge"] if args.maker_regime_router == "active" else args.min_edge),
                        min_bid_depth=deep_only_min_depth)
                    if args.maker_regime_router == "active" and policy is not None:
                        q = apply_regime_policy_to_quotes(q, policy)
                    desired[token] = (q, f, bb, ba)
                    if args.maker_regime_router == "shadow" and shadow_cur is not None:
                        s_inv = shadow_cur.up_inv if token == "up" else shadow_cur.down_inv
                        s_opp = shadow_cur.down_inv if token == "up" else shadow_cur.up_inv
                        spolicy, sihash = route_regime_for_token(
                            desk, token, fair=f, best_bid=bb, best_ask=ba, token_inventory=s_inv,
                            opposite_inventory=s_opp, max_shares=args.max_shares, confirm=confirm)
                        sknobs = effective_regime_knobs(
                            spolicy, base_margin=margin, clip=args.clip, max_shares=args.max_shares,
                            min_edge=args.min_edge, min_seconds_quote=args.min_seconds_quote)
                        persist_regime_decision(
                            c, shadow_campaign, cid, ws, token, spolicy, inputs_hash=sihash,
                            margin_used=sknobs["margin"], clip_used=sknobs["clip"],
                            max_shares_used=sknobs["max_shares"], min_edge_used=sknobs["min_edge"],
                            min_seconds_quote_used=sknobs["min_seconds_quote"], now_ts=now_ts)
                        sq = MP.desired_quotes(
                            fair=f, best_bid=bb, best_ask=ba, inventory=s_inv,
                            max_inventory=sknobs["max_shares"], seconds_left=left,
                            base_margin=sknobs["margin"], base_size=sknobs["clip"],
                            min_seconds_quote=sknobs["min_seconds_quote"],
                            min_quote_edge=sknobs["min_edge"])
                        sq = apply_regime_policy_to_quotes(sq, spolicy)
                        shadow_desired[token] = (sq, f, bb, ba)
                        shadow_regimes[token] = (spolicy, sihash, sknobs)
                # the read's adverse-selection defense: pull the ask on the confirmed side + the bid opposite
                pulled = directional_pull({t: v[0] for t, v in desired.items()}, confirm.get("confirmed_side"))
                shadow_pulled = (directional_pull({t: v[0] for t, v in shadow_desired.items()},
                                                   confirm.get("confirmed_side"))
                                 if shadow_desired else {})
                quoted = {}
                for token, (_q, f, bb, ba) in desired.items():
                    q = pulled[token]
                    active_meta = regimes.get(token) if args.maker_regime_router == "active" else None
                    fill_clip = active_meta[2]["clip"] if active_meta else args.clip
                    fill_cap = active_meta[2]["max_shares"] if active_meta else args.max_shares
                    # model fills of the PREVIOUSLY-resting quotes: print-verified tape only
                    prints = tape_prints(cid, last_poll_ts, now_ts, token=token)
                    prev_qids = live_qids.get(token, {})   # the fills execute against LAST tick's quotes
                    prev_reason = live_reasons.get(token)  # ...so explain them with LAST tick's quote reason
                    for side, price, size, fmodel in model_fills(cur, token, q, bb, ba, fill_clip, prints=prints):
                        meta = active_meta or (None, None, {})
                        record_fill(c, args.campaign, cur, token, side, price, size, f, now_ts=now_ts,
                                    reason=(prev_reason if prev_reason is not None else q["reason"]),
                                    fill_model=fmodel, max_shares=fill_cap,
                                    quote_id=prev_qids.get(side),
                                    regime_policy=meta[0], regime_inputs_hash=meta[1],
                                    margin_used=meta[2].get("margin"), clip_used=meta[2].get("clip"),
                                    max_shares_used=meta[2].get("max_shares"),
                                    min_edge_used=meta[2].get("min_edge"),
                                    min_seconds_quote_used=meta[2].get("min_seconds_quote"),
                                    fill_episode=regime_episode,
                                    profile_decision=active_profile_decisions.get(token),
                                    adaptive_decision=adaptive_decisions.get(token))
                    quoted[token] = {"bid": (q["bid"].__dict__ if q["bid"] else None),
                                     "ask": (q["ask"].__dict__ if q["ask"] else None), "reason": q["reason"]}
                    meta = active_meta or (None, None, {})
                    live_reasons[token] = q["reason"]   # remember this tick's reason for next tick's fills
                    live_qids[token] = persist_quote_snapshot(
                                           c, args.campaign, cid, ws, token, q, fair=f, best_bid=bb, best_ask=ba,
                                           seconds_left=left, now_ts=now_ts, regime_policy=meta[0],
                                           regime_inputs_hash=meta[1], margin_used=meta[2].get("margin"),
                                           clip_used=meta[2].get("clip"),
                                           max_shares_used=meta[2].get("max_shares"),
                                           min_edge_used=meta[2].get("min_edge"),
                                           min_seconds_quote_used=meta[2].get("min_seconds_quote"),
                                           episode=regime_episode,
                                           profile_decision=active_profile_decisions.get(token),
                                           adaptive_decision=adaptive_decisions.get(token))
                    if getattr(args, "maker_adaptive_paper", False) and token in adaptive_decisions:
                        AD.record_decision(
                            c,
                            campaign_id=args.campaign,
                            context_key=adaptive_decisions[token].context_key,
                            profile_id=adaptive_decisions[token].profile_id,
                            quote_posted=bool(q.get("bid") or q.get("ask")),
                            now_ts=int(now_ts),
                        )
                    if adaptive_v3_enabled and not adaptive_frozen and token in v3_decisions:
                        # frozen campaigns own zero learner state — quote counts included
                        action = v3_decisions[token]["action"]
                        AD3.upsert_action_arm_stats(
                            c,
                            campaign_id=args.campaign,
                            context_key=action.context_key,
                            arm_id=action.arm_id,
                            n_quotes=1 if bool(q.get("bid") or q.get("ask")) else 0,
                            now_ts=int(now_ts),
                        )
                persist_window(c, args.campaign, cur, commit=False)   # keep fills/book atomic for this tick
                if shadow_cur is not None and shadow_pulled:
                    for token, (_sq, f, bb, ba) in shadow_desired.items():
                        sq = shadow_pulled[token]
                        spolicy, sihash, sknobs = shadow_regimes[token]
                        prints = tape_prints(cid, last_poll_ts, now_ts, token=token)
                        sprev_qids = shadow_qids.get(token, {})
                        for side, price, size, fmodel in model_fills(
                                shadow_cur, token, sq, bb, ba, sknobs["clip"], prints=prints):
                            record_fill(c, shadow_campaign, shadow_cur, token, side, price, size, f,
                                        now_ts=now_ts, reason=sq["reason"], fill_model=fmodel,
                                        max_shares=sknobs["max_shares"], quote_id=sprev_qids.get(side),
                                        regime_policy=spolicy,
                                        regime_inputs_hash=sihash, margin_used=sknobs["margin"],
                                        clip_used=sknobs["clip"], max_shares_used=sknobs["max_shares"],
                                        min_edge_used=sknobs["min_edge"],
                                        min_seconds_quote_used=sknobs["min_seconds_quote"])
                        shadow_qids[token] = persist_quote_snapshot(
                            c, shadow_campaign, cid, ws, token, sq, fair=f, best_bid=bb, best_ask=ba,
                            seconds_left=left, now_ts=now_ts, regime_policy=spolicy,
                            regime_inputs_hash=sihash, margin_used=sknobs["margin"],
                            clip_used=sknobs["clip"], max_shares_used=sknobs["max_shares"],
                            min_edge_used=sknobs["min_edge"],
                            min_seconds_quote_used=sknobs["min_seconds_quote"])
                    persist_shadow_window(c, shadow_campaign, shadow_cur, commit=False)
                    think["regime_shadow"] = {
                        "campaign": shadow_campaign,
                        "up_inv": round(shadow_cur.up_inv, 1),
                        "down_inv": round(shadow_cur.down_inv, 1),
                        "cash": round(shadow_cur.cash, 2),
                    }
                if args.maker_regime_router == "active" and regimes:
                    think["regime_active"] = {
                        token: {
                            "regime_id": policy.regime_id,
                            "policy_id": policy.policy_id,
                            "risk_action": policy.risk_action,
                            "reason": policy.reason,
                            "margin": knobs["margin"],
                            "clip": knobs["clip"],
                            "max_shares": knobs["max_shares"],
                            "min_edge": knobs["min_edge"],
                            "min_seconds_quote": knobs["min_seconds_quote"],
                            "quote_ttl_ms": policy.quote_ttl_ms,
                            "bid_enabled": bool(policy.bid_enabled),
                            "ask_enabled": bool(policy.ask_enabled),
                            "exit_only": bool(policy.exit_only),
                        }
                        for token, (policy, _ihash, knobs) in regimes.items()
                    }
                # unrealized mark: inventory * current outcome-prob proxy (book mid), minus cash spent
                up_mid = _mid(desk.get("up_bid"), desk.get("up_ask"))
                down_mid = _mid(desk.get("down_bid"), desk.get("down_ask"))
                unreal = cur.cash + cur.up_inv * (up_mid or fair_up) + cur.down_inv * (down_mid or (1 - fair_up))
                think.update({"quotes": quoted, "up_inv": round(cur.up_inv, 1), "down_inv": round(cur.down_inv, 1),
                              "cash": round(cur.cash, 2), "unrealized_pnl": round(unreal, 2)})
                c.commit()

            c.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET think_json=excluded.think_json,ts=excluded.ts",
                      (json.dumps(think), int(time.time())))
            c.commit()
            last_poll_ts = now_ts
        except Exception as e:  # noqa: BLE001
            print(f"[maker] loop error: {e}", flush=True)
        for _ in range(int(args.poll * 2)):
            if stop["v"]:
                break
            time.sleep(0.5)
    print("[maker] stopped", flush=True)


def _mid(bid, ask):
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-db", default="data/crypto5m/maker_v0.sqlite")
    ap.add_argument("--pid", default="data/crypto5m/maker_v0.pid")
    ap.add_argument("--campaign", default="maker-regime-v1")    # prior rows are contaminated/mixed-contract
                                                                # cutover data; never pool
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--margin", type=float, default=0.02)
    ap.add_argument("--clip", type=float, default=25.0)
    ap.add_argument("--max-shares", type=float, default=50.0)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--min-seconds-quote", type=int, default=30)
    ap.add_argument("--maker-regime-router", choices=("off", "shadow", "active"), default="off",
                    help="off=current v4; shadow=record regime decisions only; active=apply regime policy")
    ap.add_argument("--regime-shadow-campaign", default="maker-regime-shadow-v1")
    ap.add_argument("--maker-adaptive-paper", action="store_true",
                    help="paper-only adaptive profile learner; forces --maker-regime-router active")
    ap.add_argument("--adaptive-control-campaign", default="maker-regime-v1")
    ap.add_argument("--adaptive-epsilon", type=float, default=0.10)
    ap.add_argument("--adaptive-seed", type=int, default=1)
    ap.add_argument("--adaptive-learner-version", choices=("v1", "v2"), default="v1")
    ap.add_argument("--maker-adaptive-v3", action="store_true",
                    help="paper-only Adaptive Maker Brain V3; forces --maker-regime-router active")
    ap.add_argument("--adaptive-reward-version", default=None,
                    help="override the V3 campaign reward_version (e.g. maker-reward-v3.2 = markout-penalized)")
    ap.add_argument("--vol-floor", type=float, default=None,
                    help="per-process override of live_desk.VOL_FLOOR_PER_S (v3.2f fair-anchor lever)")
    ap.add_argument("--regime-gated-floor", type=float, nargs=2, default=None,
                    metavar=("QUIET_FLOOR", "CONTESTED_FLOOR"),
                    help="campaign maker-regime-gated-v1: switch VOL_FLOOR_PER_S per window by live "
                         "regime — QUIET_FLOOR (one-sided & tight book) vs CONTESTED_FLOOR (else). "
                         "Overrides --vol-floor per tick. See 2026-07-07 tournament-finding spec.")
    ap.add_argument("--deep-only-min-depth", type=float, default=0.0,
                    help="campaign maker-deep-only-v1: never lead the book; only rest bids at least this far "
                         "(e.g. 0.03) below the touch. 0 = normal top-of-book behavior. Removes the "
                         "adverse near-touch fills, keeps the deep stale-seller edge. See 2026-07-07 review.")
    ap.add_argument("--regime-gated-ramp", type=float, nargs=4, default=None,
                    metavar=("HONEST", "WIDE", "X_LO", "X_HI"),
                    help="campaign maker-regime-gated-v2: CONTINUOUS VOL_FLOOR_PER_S, log-interpolated "
                         "by odds-extremity |fair-0.5| — WIDE at |f-.5|<=X_LO (contested), HONEST at "
                         ">=X_HI (one-sided), log ramp between. Overrides --vol-floor per tick. "
                         "Mutually exclusive with --regime-gated-floor. See 2026-07-07 continuous-floor spec.")
    ap.add_argument("--maker-adaptive-v33", action="store_true",
                    help="paper-only V3.3-learner: window-level paired reward vs a baseline "
                         "campaign, frozen fair, evidence-gated arm deviation")
    ap.add_argument("--v33-baseline-db", default="data/crypto5m/maker_adaptive_v3_2f_frozen.sqlite")
    ap.add_argument("--v33-baseline-campaign", default="maker-adaptive-paper-v3.2f-frozen")
    ap.add_argument("--adaptive-frozen", action="store_true",
                    help="freeze the V3 learner at launch priors: default fair blend, deterministic "
                         "prior arm, no exploration, no learner-state updates (rewards still recorded)")
    ap.add_argument("--max-iterations", type=int, default=0, help="0 = run forever (continuous)")
    ap.add_argument("--continuous", action="store_true")
    run(ap.parse_args())
