#!/usr/bin/env python3
"""Wallet anatomy — full position-lifecycle reconstruction + edge localization.

Doctrine (operator-locked 2026-05-31): every wallet we run through this system is a
winner by construction. The output is NEVER "no edge". It is either:
  - "edge located in slice X via mechanism Y", or
  - "cannot localize — best guess Y (low confidence)".

Pipeline (one wallet):
  1. INGEST  full activity history via Data-API (TRADE/SELL/REDEEM/MERGE/SPLIT/...),
             cached to disk so re-runs are free.
  2. RECONSTRUCT  every position's lifecycle, keyed (condition_id, outcome_index, episode):
             buys (VWAP entry, cost, shares), sells (proceeds), redeem flag, split/merge
             share flows. A flat-then-rebuy on the same outcome starts a NEW episode (a
             distinct lifecycle), not an averaged blob. Exit mechanism = REDEEM (held to
             resolution) | SELL (traded the move) | OPEN.
  3. RESOLVE  r (resolution outcome) via Gamma /markets?closed=true -> outcomePrices,
             cached. won = (her outcome priced ~1.0). Transient API failures are NOT
             cached as resolved=False; they stay unknown and retry next run.
  4. ENRICH  event group (eventSlug, free on every row) + category from POLYMARKET'S OWN
             TAGS (Gamma /events). No hardcoded entities; only a tiny stable parent rollup.
             Untagged markets are counted, never keyword-guessed.
  5. LOCALIZE  realized PnL + (r-p) calibration edge per position; slice by parent
             category / platform tag / event / price-regime / mechanism; find the slice(s)
             carrying the money; emit located-or-best-guess verdict (never "no edge").

Validated against the SwissMiss dossier ground truth (961 closed, 63.1% full-book WR,
+$565K realized, sub-0.50 underdog +6.7pp / +$206K) — see knowledge/wallets/0xdbade...c95.md.

NOTE: directional edge is computed on BUY-built positions. Pure SPLIT/MERGE inventory
(e.g. the Rock-2028 neutral YES+NO book) is reported separately, never counted as
directional selection edge.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.markets import polymarket_api as P  # noqa: E402

RESOLUTION_WIN = 0.97
RESOLUTION_LOSS = 0.03
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "runs", "wallet_anatomy")

# Activity types that are directional trade legs vs. inventory/reward noise.
TRADE_TYPES = {"TRADE"}            # has side BUY/SELL
HOLD_TYPES = {"REDEEM"}           # held-to-resolution settlement (no payout in row)
INVENTORY_TYPES = {"MERGE", "SPLIT"}
NOISE_TYPES = {"REWARD", "YIELD", "MAKER_REBATE"}


# --------------------------------------------------------------------------- ingest

def load_activity(wallet: str, *, refresh: bool = False, max_pages: int = 400) -> list[dict]:
    """Full backward-walked activity history, cached to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_activity.json")
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return json.load(f)
    s = P.build_session()

    def emit(event_name, payload=None):
        info = ""
        if isinstance(payload, Mapping):
            info = " ".join(f"{k}={payload[k]}" for k in
                            ("rows", "total", "offset", "page", "end") if k in payload)
        print(f"  ...{event_name} {info}".rstrip(), file=sys.stderr)

    acts = P.fetch_user_activity(s, wallet, activity_type=None, page_limit=500,
                                 max_pages=max_pages, walk_full_history=True,
                                 progress_emitter=emit)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(acts, f)
    os.replace(tmp, path)
    return acts


# ----------------------------------------------------------------- reconstruction

@dataclass
class Position:
    """One directional position: (condition_id, outcome_index)."""
    wallet: str
    condition_id: str
    outcome_index: int
    outcome: str = ""
    title: str = ""
    event_slug: str = ""
    buy_shares: float = 0.0
    buy_cost: float = 0.0           # USDC paid (= sum price*size)
    sell_shares: float = 0.0
    sell_proceeds: float = 0.0       # USDC received
    split_shares: float = 0.0        # shares minted into this outcome via SPLIT
    merge_shares: float = 0.0        # shares burned out of this outcome via MERGE
    redeemed: bool = False
    first_buy_ts: int | None = None
    last_ts: int = 0
    n_buys: int = 0
    n_sells: int = 0
    # filled by resolve/enrich
    resolved: bool = False
    won: int | None = None           # 1 win / 0 loss / None unknown
    is_push: bool = False
    category: str = ""               # stable parent rollup (sports/geopolitics/...)
    subcat: str = ""                 # most-specific platform tag
    tags: list = field(default_factory=list)   # all platform tags for this market

    @property
    def entry_vwap(self) -> float:
        return self.buy_cost / self.buy_shares if self.buy_shares > 0 else 0.0

    @property
    def net_shares(self) -> float:
        return self.buy_shares + self.split_shares - self.sell_shares - self.merge_shares

    @property
    def is_directional(self) -> bool:
        # built primarily by buying (not a pure split/merge inventory leg)
        return self.buy_shares > 0

    @property
    def exit_mechanism(self) -> str:
        if not self.resolved and not self.redeemed:
            return "open"
        if self.sell_shares > 0 and self.sell_shares >= 0.5 * max(self.buy_shares, 1e-9):
            return "sold"                      # exited the majority before resolution
        if self.redeemed or self.resolved:
            return "held"                      # held to resolution / redeemed
        return "sold" if self.sell_shares > 0 else "open"

    def realized_pnl(self) -> float | None:
        """Realized PnL on resolved positions: sell proceeds + redeem payout - cost."""
        if not self.resolved or self.won is None:
            return None
        held_shares = max(self.buy_shares - self.sell_shares, 0.0)
        redeem_payout = held_shares * 1.0 if self.won else 0.0
        if self.is_push:
            redeem_payout = held_shares * 0.5
        return self.sell_proceeds + redeem_payout - self.buy_cost


def reconstruct_positions(wallet: str, activity: Iterable[Mapping]) -> dict[tuple, Position]:
    """Fold the activity feed into per-(condition, outcome_index, episode) positions."""
    positions: dict[tuple, Position] = {}
    current_episode: dict[tuple[str, int], int] = defaultdict(int)

    def get(cond, oidx, outcome, title, eslug, episode: int = 0) -> Position:
        key = (cond, oidx, episode)
        p = positions.get(key)
        if p is None:
            p = Position(wallet=wallet, condition_id=cond, outcome_index=oidx,
                         outcome=outcome or "", title=title or "", event_slug=eslug or "")
            positions[key] = p
        return p

    indexed_activity = enumerate(activity)
    for _, a in sorted(indexed_activity, key=lambda item: (int(item[1].get("timestamp") or 0), item[0])):
        typ = a.get("type")
        cond = a.get("conditionId") or a.get("condition_id")
        if not cond:
            continue
        ts = int(a.get("timestamp") or 0)
        size = float(a.get("size") or 0.0)
        usdc = float(a.get("usdcSize") or 0.0)
        oidx = a.get("outcomeIndex")
        title = a.get("title") or ""
        eslug = a.get("eventSlug") or ""

        if typ in TRADE_TYPES:
            try:
                oidx = int(oidx)
            except (TypeError, ValueError):
                continue
            base_key = (cond, oidx)
            episode = current_episode[base_key]
            side = (a.get("side") or "").upper()
            if side == "BUY":
                prior = positions.get((cond, oidx, episode))
                if prior is not None and prior.buy_shares > 0 and prior.sell_shares > 0:
                    if prior.net_shares <= 1e-9:
                        episode += 1
                        current_episode[base_key] = episode
            p = get(cond, oidx, a.get("outcome"), title, eslug, episode)
            p.last_ts = max(p.last_ts, ts)
            if side == "BUY":
                p.buy_shares += size
                p.buy_cost += usdc
                p.n_buys += 1
                p.first_buy_ts = ts if p.first_buy_ts is None else min(p.first_buy_ts, ts)
            elif side == "SELL":
                p.sell_shares += size
                p.sell_proceeds += usdc
                p.n_sells += 1
        elif typ in HOLD_TYPES:
            # REDEEM carries no outcome_index; mark every outcome leg of this condition held
            for p in positions.values():
                if p.condition_id == cond:
                    p.redeemed = True
                    p.last_ts = max(p.last_ts, ts)
        elif typ in INVENTORY_TYPES:
            # SPLIT: +shares both outcomes; MERGE: -shares both outcomes. Title/event known,
            # but we don't know per-outcome legs from the row -> record on a sentinel leg.
            p = get(cond, -1, "", title, eslug)   # outcome_index -1 = inventory leg
            p.last_ts = max(p.last_ts, ts)
            if typ == "SPLIT":
                p.split_shares += size
            else:
                p.merge_shares += size
        # NOISE_TYPES ignored
    return positions


# --------------------------------------------------------------------------- resolve

def resolve_conditions(condition_ids: Iterable[str], *, wallet: str,
                       refresh: bool = False, batch_size: int = 40) -> dict[str, dict]:
    """condition_id -> {winner_index, prices, push, resolved}. Cached to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_resolutions.json")
    cache: dict[str, dict] = {}
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            cache = json.load(f)
    wanted = {str(c).strip() for c in condition_ids if c}
    unknown = sorted(wanted - cache.keys())
    if unknown:
        s = P.build_session()
        for i in range(0, len(unknown), batch_size):
            batch = unknown[i:i + batch_size]
            try:
                data = P._request_json(s, f"{P.GAMMA_BASE_URL}/markets",
                                       params={"condition_ids": batch, "closed": "true",
                                               "limit": 500})
            except Exception as e:  # noqa: BLE001
                print(f"  resolve batch error: {type(e).__name__}", file=sys.stderr)
                print(f"  ...resolved {min(i + batch_size, len(unknown))}/{len(unknown)}",
                      file=sys.stderr)
                continue
            got = set()
            for m in data or []:
                cid = str(m.get("conditionId") or "").strip()
                if cid not in wanted:
                    continue
                got.add(cid)
                cache[cid] = _parse_resolution(m)
            for cid in batch:                      # mark unresolved/open so we don't refetch
                if cid not in got:
                    cache.setdefault(cid, {"resolved": False})
            print(f"  ...resolved {min(i + batch_size, len(unknown))}/{len(unknown)}",
                  file=sys.stderr)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    return cache


def _parse_resolution(m: Mapping) -> dict:
    status = (m.get("umaResolutionStatus") or "").lower()
    closed = bool(m.get("closed"))
    prices = _json_list(m.get("outcomePrices"))
    try:
        fp = [float(x) for x in prices]
    except (TypeError, ValueError):
        fp = []
    resolved = (status == "resolved" or closed) and bool(fp)
    winner = None
    push = False
    if fp:
        mx = max(fp)
        if mx >= RESOLUTION_WIN:
            winner = fp.index(mx)
        elif 0.4 <= mx <= 0.6 and len(fp) >= 2 and all(0.4 <= x <= 0.6 for x in fp):
            push = True
    return {"resolved": resolved, "winner": winner, "push": push, "prices": fp}


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return []


# ----------------------------------------------------------------- categorization

# Categorization is DRIVEN BY POLYMARKET'S OWN TAGS — no hardcoded entities/teams/people
# (those are ephemeral markets that churn). The only hand-maintained thing is this tiny,
# stable parent rollup keyed on platform PARENT tags. Order = priority (first hit wins;
# geopolitics before politics so Iran/Mideast lands in geopolitics, not politics).
_PARENT_RULES = [
    ("sports",      {"sports", "esports"}),
    ("crypto",      {"crypto", "bitcoin", "ethereum", "solana"}),
    ("geopolitics", {"geopolitics"}),
    ("macro",       {"economy", "fed", "macro", "inflation", "economic policy", "rates"}),
    ("weather",     {"weather", "climate"}),
    ("culture",     {"pop culture", "mentions", "awards", "celebrities", "movies", "music",
                     "tv", "entertainment"}),
    ("politics",    {"politics", "elections", "world", "world elections", "global elections"}),
]
# Generic tags that are too broad to be a useful *specific* slice grain.
_GENERIC_TAGS = {"sports", "games", "world", "politics", "all", "main election", "weekly",
                 "recurring", "hide from new", "main"}


def parent_category(tags: Iterable[str]) -> str:
    low = {str(t).strip().lower() for t in tags if t}
    if not low:
        return "untagged"
    for name, parents in _PARENT_RULES:
        if low & parents:
            return name
    return "other-tagged"


def specific_tag(tags: list[str]) -> str:
    """The most specific platform tag (durable, platform-owned) for finer slicing."""
    for t in tags:
        if str(t).strip().lower() not in _GENERIC_TAGS:
            return str(t).strip()
    return tags[0] if tags else "untagged"


def load_event_tags(event_slugs: Iterable[str], *, wallet: str, refresh: bool = False,
                    batch_size: int = 40) -> dict[str, list[str]]:
    """event_slug -> [platform tag labels], from Gamma /events. Cached;
    tags are effectively immutable so the first build pays once."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_tags.json")
    cache: dict[str, list[str]] = {}
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            cache = json.load(f)
    wanted = {str(s).strip() for s in event_slugs if s}
    unknown = sorted(wanted - cache.keys())
    if unknown:
        s = P.build_session()
        for i in range(0, len(unknown), batch_size):
            batch = unknown[i:i + batch_size]
            for slug in batch:
                try:
                    data = P._request_json(s, f"{P.GAMMA_BASE_URL}/events",
                                           params={"slug": slug, "limit": 500, "closed": "true"})
                except Exception as e:  # noqa: BLE001
                    print(f"  tags slug error: {slug} {type(e).__name__}", file=sys.stderr)
                    continue
                matched = None
                for ev in data or []:
                    if str(ev.get("slug") or "").strip() == slug:
                        matched = ev
                        break
                if matched is None:
                    cache.setdefault(slug, [])       # successful miss -> untagged/missing
                    continue
                cache[slug] = [t.get("label") or t.get("slug")
                               for t in (matched.get("tags") or [])
                               if (t.get("label") or t.get("slug"))]
            print(f"  ...tagged {min(i + batch_size, len(unknown))}/{len(unknown)}",
                  file=sys.stderr)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    return cache


# --------------------------------------------------------------------------- enrich

def enrich(positions: dict[tuple, Position], resolutions: Mapping[str, dict],
           tag_map: Mapping[str, list[str]] | None = None) -> None:
    tag_map = tag_map or {}
    for p in positions.values():
        r = resolutions.get(p.condition_id) or {}
        p.tags = list(tag_map.get(p.event_slug, []))      # platform tags; never cached/guessed
        p.category = parent_category(p.tags)
        p.subcat = specific_tag(p.tags)
        if not r.get("resolved"):
            continue
        if p.outcome_index < 0:
            p.resolved = True       # inventory leg; no directional win
            continue
        p.resolved = True
        p.is_push = bool(r.get("push"))
        winner = r.get("winner")
        if r.get("push"):
            p.won = None
        elif winner is not None:
            p.won = 1 if winner == p.outcome_index else 0


# --------------------------------------------------------------------------- analysis

def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class Slice:
    key: str
    n: int = 0
    wins: int = 0
    avg_entry_sum: float = 0.0
    pnl: float = 0.0
    cost: float = 0.0
    members: list = field(default_factory=list)

    def add(self, p: Position, pnl: float):
        self.n += 1
        self.wins += int(p.won == 1)
        self.avg_entry_sum += p.entry_vwap
        self.pnl += pnl
        self.cost += p.buy_cost
        self.members.append(p)

    @property
    def win_rate(self): return self.wins / self.n if self.n else 0.0
    @property
    def avg_entry(self): return self.avg_entry_sum / self.n if self.n else 0.0
    @property
    def edge_pp(self): return (self.win_rate - self.avg_entry) * 100.0
    @property
    def roi(self): return self.pnl / self.cost * 100.0 if self.cost else 0.0

    def ci_pp(self):
        lo, hi = _wilson(self.wins, self.n)
        return ((lo - self.avg_entry) * 100.0, (hi - self.avg_entry) * 100.0)


def resolved_directional(positions: dict[tuple, Position]) -> list[Position]:
    return [p for p in positions.values()
            if p.is_directional and p.resolved and p.won is not None and not p.is_push]


def slice_by(positions: list[Position], keyfn) -> dict[str, Slice]:
    out: dict[str, Slice] = {}
    for p in positions:
        k = keyfn(p)
        out.setdefault(k, Slice(key=k)).add(p, p.realized_pnl() or 0.0)
    return out


def slice_by_tags(positions: list[Position]) -> dict[str, Slice]:
    """Multi-membership: each position contributes to EVERY platform tag it carries.
    This is the durable 'what does she bet on' lens — Polymarket's own vocabulary."""
    out: dict[str, Slice] = {}
    for p in positions:
        for t in (p.tags or ["untagged"]):
            if str(t).strip().lower() in _GENERIC_TAGS:
                continue
            out.setdefault(t, Slice(key=t)).add(p, p.realized_pnl() or 0.0)
    return out


def _price_regime(p: Position) -> str:
    v = p.entry_vwap
    if v < 0.10: return "0.00-0.10 deep-longshot"
    if v < 0.30: return "0.10-0.30 longshot"
    if v < 0.50: return "0.30-0.50 underdog"
    if v < 0.70: return "0.50-0.70 lean-fav"
    if v < 0.90: return "0.70-0.90 favorite"
    return "0.90-1.00 near-certain"


def _entry_hour(p: Position) -> str:
    """UTC hour of first buy (method #16 dead-hour timing)."""
    if not p.first_buy_ts:
        return "??"
    from datetime import datetime, timezone
    h = datetime.fromtimestamp(p.first_buy_ts, tz=timezone.utc).hour
    return f"{h:02d}h"


# --------------------------------------------------------- detectors (method catalog)

def reward_attribution(activity: Iterable[Mapping]) -> dict:
    """Method #12 — is the edge prediction or liquidity rewards? Sums REWARD/YIELD/
    MAKER_REBATE income from the activity feed. (These rows ARE in the Data API even
    though maker/taker fill identity is NOT — see verified 2026-05-31.)"""
    income: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for a in activity:
        t = a.get("type")
        if t in NOISE_TYPES:
            income[t] += float(a.get("usdcSize") or 0.0)
            counts[t] += 1
    return {"income": dict(income), "counts": dict(counts), "total": sum(income.values())}


def reward_share_pct(*, trading_pnl: float, reward_total: float) -> float:
    denom = abs(trading_pnl) + reward_total
    return reward_total / denom * 100.0 if denom else 0.0


# Platform parent categories whose resolution is driven by external info/events we can't
# observe (so an edge there could be info-access we can't replicate). Sports/recurring
# resolve on public outcomes -> more likely runnable.
_INFO_SENSITIVE = {"geopolitics", "politics", "macro"}


def runnability(slc: "Slice", *, parent: str, held_share: float,
                top1_share: float) -> tuple[str, str]:
    """Label a located edge by whether WE could run it, from observable proxies only.
    Honest about what we cannot see (no book/news/fill-identity feeds).

    Labels: runnable | capital_scaled | info_sensitive | speed_micro | underpowered."""
    if slc.n < 10:
        return ("underpowered", f"n={slc.n}<10 — can't characterize")
    if top1_share > 0.5:
        return ("capital_scaled",
                f"single position = {top1_share*100:.0f}% of this slice's pnl — few large "
                f"bets, edge rides on size/luck not breadth")
    if parent in _INFO_SENSITIVE:
        return ("info_sensitive",
                "resolution driven by external events; can't rule out info-access edge "
                "without a news-timestamp feed (GAP)")
    if held_share >= 0.7:
        return ("runnable",
                "broad, public-outcome (sports/recurring), held-to-resolution — "
                "forward-testable if it clears the forward gate")
    return ("speed_micro",
            "edge realized via exits/timing not hold — may need execution speed we can't "
            "observe (no book feed); treat as conditional")


def slice_held_share(slc: "Slice") -> float:
    positive = [p.realized_pnl() or 0.0 for p in slc.members if (p.realized_pnl() or 0.0) > 0]
    gross_win = sum(positive)
    if gross_win <= 0:
        return 0.0
    held_win = sum((p.realized_pnl() or 0.0) for p in slc.members
                   if p.exit_mechanism == "held" and (p.realized_pnl() or 0.0) > 0)
    return held_win / gross_win


def slice_top1_share(slc: "Slice") -> float:
    positive = [p.realized_pnl() or 0.0 for p in slc.members if (p.realized_pnl() or 0.0) > 0]
    gross_win = sum(positive)
    return max(positive, default=0.0) / gross_win if gross_win else 0.0


def slice_parent_category(slc: "Slice") -> str:
    counts: dict[str, int] = defaultdict(int)
    for p in slc.members:
        counts[p.category] += 1
    if not counts:
        return "other-tagged"
    return max(counts.items(), key=lambda kv: kv[1])[0]


# --------------------------------------------------------------------------- report

def report(wallet: str, positions: dict[tuple, Position], activity=None) -> dict:
    rd = resolved_directional(positions)
    all_dir = [p for p in positions.values() if p.is_directional]
    total_pnl = sum(p.realized_pnl() or 0.0 for p in rd)
    total_cost = sum(p.buy_cost for p in rd)
    wins = sum(int(p.won == 1) for p in rd)
    out = {
        "wallet": wallet,
        "n_positions_total": len(positions),
        "n_directional": len(all_dir),
        "n_resolved_directional": len(rd),
        "full_book_wr": (wins / len(rd) * 100.0) if rd else 0.0,
        "total_realized_pnl": total_pnl,
        "total_cost": total_cost,
        "roi_pct": (total_pnl / total_cost * 100.0) if total_cost else 0.0,
    }

    def top(slices: dict[str, Slice], by, n=12):
        return sorted(slices.values(), key=by, reverse=True)[:n]

    print("=" * 78)
    print(f"WALLET ANATOMY — {wallet}")
    print("=" * 78)
    print(f"positions={out['n_positions_total']}  directional={out['n_directional']}  "
          f"resolved_directional={out['n_resolved_directional']}")
    print(f"FULL-BOOK WR={out['full_book_wr']:.1f}%  realized_pnl=${total_pnl:,.0f}  "
          f"gross_turnover=${total_cost:,.0f}  (turnover_ROI={out['roi_pct']:.2f}% — "
          f"gross recycled capital, NOT return on bankroll)")

    def show(title, slices, min_n=8):
        print(f"\n--- {title}  (n>={min_n}, ranked by $ pnl) ---")
        print(f"{'slice':<34}{'n':>4}{'WR%':>7}{'entry':>7}{'edge_pp':>9}{'pnl$':>12}{'roi%':>8}")
        rows = [s for s in slices.values() if s.n >= min_n]
        for s in sorted(rows, key=lambda s: s.pnl, reverse=True):
            lo, hi = s.ci_pp()
            star = "*" if lo > 0 else (" " if hi > 0 else "-")  # CI excludes 0 above
            print(f"{s.key[:33]:<34}{s.n:>4}{s.win_rate*100:>7.1f}{s.avg_entry:>7.2f}"
                  f"{s.edge_pp:>+9.1f}{s.pnl:>12,.0f}{s.roi:>8.1f} {star}")

    by_cat = slice_by(rd, lambda p: p.category)
    by_subcat = slice_by(rd, lambda p: p.subcat or "untagged")
    by_tag = slice_by_tags(rd)
    by_regime = slice_by(rd, lambda p: _price_regime(p))
    by_mech = slice_by(rd, lambda p: p.exit_mechanism)
    by_event = slice_by(rd, lambda p: p.event_slug or "(none)")
    by_hour = slice_by(rd, _entry_hour)
    by_cat_regime = slice_by(rd, lambda p: f"{p.subcat} | {_price_regime(p).split()[1]}")

    untagged = sum(1 for p in rd if not p.tags)
    if untagged:
        print(f"\n[tag coverage] {len(rd)-untagged}/{len(rd)} positions tagged "
              f"({untagged} untagged = {untagged/len(rd)*100:.0f}%, counted not guessed)")
    show("BY PARENT CATEGORY (stable rollup)", by_cat, min_n=8)
    show("BY PLATFORM TAG (durable, multi-membership)", by_tag, min_n=10)
    show("BY PRICE REGIME", by_regime, min_n=8)
    show("BY EXIT MECHANISM (hold-to-resolution vs traded-the-move)", by_mech, min_n=5)
    show("BY ENTRY HOUR UTC (#16 dead-hour timing)", by_hour, min_n=12)
    show("BY PLATFORM-TAG x PRICE", by_cat_regime, min_n=10)

    # method #12 — reward/liquidity attribution (is the edge prediction or rewards?)
    if activity is not None:
        ra = reward_attribution(activity)
        share = reward_share_pct(trading_pnl=total_pnl, reward_total=ra["total"])
        print(f"\n--- REWARD ATTRIBUTION (#12) ---")
        print(f"passive income ${ra['total']:,.0f} "
              f"({', '.join(f'{k}=${v:,.0f}' for k, v in ra['income'].items()) or 'none'})  "
              f"= {share:.1f}% of (trading_pnl + rewards) -> "
              f"{'PREDICTION-driven' if share < 25 else 'REWARD-driven (flag!)'}")
        out["reward_share_pct"] = round(share, 1)
    print("\n--- TOP EVENTS by $ pnl (n>=3) ---")
    print(f"{'event':<40}{'n':>4}{'WR%':>7}{'entry':>7}{'edge_pp':>9}{'pnl$':>12}")
    for s in sorted([s for s in by_event.values() if s.n >= 3],
                    key=lambda s: s.pnl, reverse=True)[:15]:
        print(f"{s.key[:39]:<40}{s.n:>4}{s.win_rate*100:>7.1f}{s.avg_entry:>7.2f}"
              f"{s.edge_pp:>+9.1f}{s.pnl:>12,.0f}")
    print("\n--- WORST EVENTS by $ pnl ---")
    for s in sorted([s for s in by_event.values() if s.n >= 1],
                    key=lambda s: s.pnl)[:8]:
        print(f"{s.key[:39]:<40}{s.n:>4}{s.win_rate*100:>7.1f}{s.avg_entry:>7.2f}"
              f"{s.edge_pp:>+9.1f}{s.pnl:>12,.0f}")

    # inventory book (split/merge) — reported, not counted as edge
    inv = [p for p in positions.values() if not p.is_directional
           and (p.split_shares or p.merge_shares)]
    inv_shares = sum(p.split_shares + p.merge_shares for p in inv)
    print(f"\n--- NEUTRAL INVENTORY (SPLIT/MERGE, excluded from edge) ---")
    print(f"legs={len(inv)}  total_shares_flowed={inv_shares:,.0f}")
    for p in sorted(inv, key=lambda p: p.split_shares + p.merge_shares, reverse=True)[:5]:
        print(f"  {p.title[:55]:<56} split={p.split_shares:,.0f} merge={p.merge_shares:,.0f}")

    out["slices"] = {
        "by_category": {k: _slice_dict(s) for k, s in by_cat.items()},
        "by_tag": {k: _slice_dict(s) for k, s in by_tag.items()},
        "by_regime": {k: _slice_dict(s) for k, s in by_regime.items()},
        "by_mechanism": {k: _slice_dict(s) for k, s in by_mech.items()},
        "by_subcat_regime": {k: _slice_dict(s) for k, s in by_cat_regime.items()},
    }
    print("\n" + "=" * 78)
    _localize(out, by_cat, by_tag, by_cat_regime, by_mech, total_pnl, rd=rd)
    _runnability_readout(by_tag, by_mech, rd, positions)
    return out


def _slice_dict(s: Slice) -> dict:
    lo, hi = s.ci_pp()
    return {"n": s.n, "wr": round(s.win_rate * 100, 1), "entry": round(s.avg_entry, 3),
            "edge_pp": round(s.edge_pp, 1), "ci_pp": [round(lo, 1), round(hi, 1)],
            "pnl": round(s.pnl, 0), "roi": round(s.roi, 1)}


def _localize(out, by_cat, by_tag, by_subcat_regime, by_mech, total_pnl, rd=None):
    """Locate the edge or emit best-guess. Never 'no edge'. Uses the platform's own
    taxonomy (parent rollup + tags) — zero hardcoded entities."""
    cats = {s.key: s for s in by_cat.values()}
    mech = {s.key: s for s in by_mech.values()}
    held, sold = mech.get("held"), mech.get("sold")

    print("VERDICT")
    print("  PARENT-CATEGORY P&L (her real fault line):")
    for s in sorted(by_cat.values(), key=lambda s: s.pnl, reverse=True):
        if s.n >= 5:
            print(f"    {s.key:<14} n={s.n:>4}  edge={s.edge_pp:>+6.1f}pp  pnl=${s.pnl:>12,.0f}")

    if held and sold:
        print(f"  MECHANISM  held=${held.pnl:,.0f} (n={held.n})  sold=${sold.pnl:,.0f} "
              f"(n={sold.n}) -> {'HOLD-TO-RESOLUTION' if held.pnl > sold.pnl else 'TRADE-THE-MOVE'} "
              f"is the money mechanism (caveat: sold-set may be adverse-selected)")
    if rd:
        wins = sorted((p.realized_pnl() or 0 for p in rd), reverse=True)
        gp = sum(x for x in wins if x > 0) or 1.0
        print(f"  CONCENTRATION  top-5 positions = {sum(wins[:5])/gp*100:.0f}% of gross "
              f"winning pnl (broad edge if low)")

    # CI-clean candidate cells (platform-tag x price); real n, positive lower-CI on (r-p), green
    clean = sorted([s for s in by_subcat_regime.values()
                    if s.n >= 20 and s.ci_pp()[0] > 0 and s.pnl > 0],
                   key=lambda s: (s.ci_pp()[0], s.pnl), reverse=True)
    # green platform tags (durable specific grain): real n, positive edge + pnl
    green_tags = sorted([s for s in by_tag.values() if s.pnl > 0 and s.n >= 15 and s.edge_pp > 0],
                        key=lambda s: s.pnl, reverse=True)

    if clean:
        s = clean[0]
        print(f"  EDGE LOCATED: '{s.key}'  n={s.n}  WR={s.win_rate*100:.1f}% vs "
              f"entry {s.avg_entry:.2f}  edge=+{s.edge_pp:.1f}pp "
              f"(95% CI {s.ci_pp()[0]:+.1f}..{s.ci_pp()[1]:+.1f}pp)  pnl=${s.pnl:,.0f}")
        print(f"  confidence: MEDIUM (in-sample CI-clean; forward unproven) "
              f"-> forward-test this slice PIT + null-calibrated.")
    elif green_tags:
        names = ", ".join(f"{s.key}(+{s.edge_pp:.0f}pp/${s.pnl/1000:.0f}k,n={s.n})"
                          for s in green_tags[:6])
        print(f"  EDGE LOCATED (platform-tag grain; cell CIs wide): {names}")
        print(f"  confidence: MEDIUM-LOW. real in-sample across many bets, no single "
              f"price-cell CI-clean -> forward-test the winning-tag book held-to-resolution.")
    else:
        guess = max((s for s in by_tag.values() if s.pnl > 0 and s.n >= 8),
                    key=lambda s: s.pnl, default=None)
        if guess:
            print(f"  CANNOT LOCALIZE a CI-clean cell. BEST GUESS tag: '{guess.key}' "
                  f"n={guess.n} edge={guess.edge_pp:+.1f}pp pnl=${guess.pnl:,.0f}")
        print(f"  confidence: LOW. edge exists (winner by construction), not sharply localized.")


def _runnability_readout(by_tag, by_mech, rd, positions):
    """Label each top-PnL platform-tag edge with whether WE could run it (#27 — the piece
    the method catalog omits: detection != runnable). Observable proxies only."""
    green = sorted([s for s in by_tag.values() if s.pnl > 0 and s.n >= 10],
                   key=lambda s: s.pnl, reverse=True)[:8]
    print("\nRUNNABILITY (can WE run each located edge? — observable proxies only)")
    for s in green:
        label, why = runnability(s, parent=slice_parent_category(s),
                                 held_share=slice_held_share(s),
                                 top1_share=slice_top1_share(s))
        print(f"  {s.key[:24]:<26} ${s.pnl:>11,.0f}  [{label:<14}] {why}")


# --------------------------------------------------------------------------- main

def run(wallet: str, *, refresh: bool = False) -> dict:
    print(f"[1/4] ingest activity for {wallet} ...", file=sys.stderr)
    acts = load_activity(wallet, refresh=refresh)
    print(f"      {len(acts)} activity rows", file=sys.stderr)
    print(f"[2/4] reconstruct positions ...", file=sys.stderr)
    positions = reconstruct_positions(wallet, acts)
    conds = {p.condition_id for p in positions.values()}
    print(f"      {len(positions)} positions across {len(conds)} conditions", file=sys.stderr)
    print(f"[3/5] resolve {len(conds)} conditions ...", file=sys.stderr)
    res = resolve_conditions(conds, wallet=wallet, refresh=refresh)
    slugs = {p.event_slug for p in positions.values() if p.event_slug}
    print(f"[4/5] fetch platform tags for {len(slugs)} events ...", file=sys.stderr)
    tags = load_event_tags(slugs, wallet=wallet, refresh=refresh)
    print(f"[5/5] enrich + analyze ...", file=sys.stderr)
    enrich(positions, res, tags)
    out = report(wallet, positions, activity=acts)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{wallet.lower()}_report.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--refresh", action="store_true", help="force refetch activity+resolutions")
    args = ap.parse_args()
    run(args.wallet, refresh=args.refresh)
