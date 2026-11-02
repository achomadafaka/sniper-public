"""Desk context facts for the JUDGE sheet (operator order 2026-10-22 via Alpha; Beta). Four collect-only collectors
(trending board, narrative tagger + theme aggregates, DexScreener, offchain heat) read AS OF the decision:
every row used is known strictly before as_of (the pinned tape rule, received_at < as_of), a missing or stale source is an explicit unknown (never 0 / False),
and the block carries numbers, bools and closed enums only: no coin name, description, URL or handle.

Supersedes offchain-heat spec 2026-10-21 §7 ("v1 collects only") by operator order; a new cohort carries it.
DexScreener is never a price: only presence flags, boost units and ages are read from it.

The clock per table: trending boards/ranks `at` and narrative theme_agg `at` = when the collector computed the row;
narrative tags and crawler crawls `known_at` = when the tag / crawl was written; DexScreener polls/items and X
token_meta `received_at` = when we received it. Each is compared `< as_of`.

Per-coin heat is recomputed as of the call from the source rows with the offchain seat's own coin_row, not
read from coin_heat: coin_heat is written on change only and never resets when a mint drops out of the hourly
set, so its newest row can be a stale positive.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_offchain as O
from sniper.services.memecoin_desk_narrative import THEMES

CONTEXT_VERSION = "desk-context-v1"
BOARD_FRESH_S = 180.0          # the collectors pass every 60 s; three missed passes = stale
THEME_FRESH_S = 180.0
THEME_WINDOW_S = 900           # the 15 min aggregate; slope = 15 min launch rate / 60 min rate
INTENTS = frozenset({"promotion", "warning", "analysis", "unknown"})
logger = logging.getLogger(__name__)
# log signal (not exported): token_meta rows for our exact mint that carried a named non-Solana chain.
# Single-threaded judge worker; a second thread needs a lock or a returned count.
CHAIN_EXCLUDED = {"rows": 0}
DEX_KINDS = {"boost": "boosted", "ad": "ads", "profile": "paid_profile", "takeover": "takeover"}


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def _days(as_of, back_s):
    return sorted({_day(as_of - back_s), _day(as_of)})


def _ro(path):
    path = Path(path)
    if not path.exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)


def _rows(paths, sql, args):
    """Rows from every existing file; None when no file exists (source absent, not empty)."""
    out, seen = [], False
    for p in paths:
        conn = _ro(p)
        if conn is None:
            continue
        seen = True
        try:
            out += conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    return out if seen else None


def _num(v):
    return float(v) if type(v) in (int, float) and v == v and abs(v) != float("inf") else None


def _unknown(reason):
    return {"state": "unknown", "reason": reason}


def trending(mint, desk_dir, as_of):
    """The newest board at or before as_of: on the top-50 board, rank, and rank by 15 min buy USD."""
    paths = [Path(desk_dir) / "trending" / ("%s.db" % d) for d in _days(as_of, BOARD_FRESH_S)]
    boards = _rows(paths, "SELECT at, coverage FROM boards WHERE at < ? AND at > ? ORDER BY at DESC LIMIT 1",
                   (as_of, as_of - BOARD_FRESH_S))
    if boards is None:
        return _unknown("no_source")
    if not boards:
        return _unknown("stale")
    at = max(boards)[0]
    cov = max(boards)[1]
    if cov == "curve_retired":
        return _unknown("curve_retired")              # an empty board from a retired tape is no data, not 'off board'
    ranks = _rows(paths, "SELECT rank, mint, buy_usd_15m FROM ranks WHERE at = ?", (at,)) or []
    mine = [r for r in ranks if r[1] == mint]
    by_buy = sorted(ranks, key=lambda r: (-(_num(r[2]) or 0.0), r[0]))
    return {"state": "ok", "coverage": cov if cov in ("ok", "gap") else None, "age_s": round(as_of - at, 1),
            "on_board": bool(mine), "rank": mine[0][0] if mine else None,
            "buy15_rank": next((i + 1 for i, r in enumerate(by_buy) if r[1] == mint), None) if mine else None,
            "board_size": len(ranks)}


def narrative(mint, desk_dir, as_of):
    """The coin's theme (tagged at or before as_of) and that theme's 15 min aggregate."""
    live = Path(desk_dir) / "narrative/live-v2"
    is_live = live.is_dir()
    directory = live if is_live else Path(desk_dir) / "narrative"
    paths = [directory / ("%s.db" % d) for d in _days(as_of, 86400)]
    tags = _rows(paths, "SELECT theme, known_at FROM tags WHERE mint = ? AND known_at < ?", (mint, as_of))
    if tags is None:
        return _unknown("no_source")
    if not tags:
        return _unknown("not_tagged")
    theme = min(tags, key=lambda r: r[1])[0]
    if theme not in THEMES:
        return _unknown("off_enum")
    out = {"state": "ok", "theme": theme}
    if is_live:
        # Display adapter only: the new observation population has no JUDGE admission.
        return dict(out, version="narrative-live-v2", theme_15m=_unknown("curve_retired"))
    agg = _rows(paths, "SELECT at, launches, buy_usd, buyers, slope, coverage FROM theme_agg WHERE theme = ? "
                "AND window_s = ? AND at < ? AND at > ? ORDER BY at DESC LIMIT 1",
                (theme, THEME_WINDOW_S, as_of, as_of - THEME_FRESH_S)) or []
    if not agg:
        out.update(theme_15m=_unknown("stale"))
        return out
    at, launches, buy_usd, buyers, slope, cov = max(agg)
    if cov == "curve_retired":
        out.update(theme_15m=_unknown("curve_retired"))   # zero launches from a retired tape is no data
        return out
    slope = _num(slope)
    out.update(theme_15m={"state": "ok", "coverage": cov if cov in ("ok", "gap") else None,
                          "age_s": round(as_of - at, 1), "launches": launches, "buy_usd": _num(buy_usd),
                          "buyers": buyers, "slope": slope, "heating": None if slope is None else slope > 1.0})
    return out


def _dex_inputs(mint, desk_dir, as_of):
    paths = [Path(desk_dir) / "dexscreener" / ("%s.db" % d) for d in _days(as_of, O.WINDOW_S)]
    # a 200 that carried an error (malformed JSON) is not a healthy poll
    polls = _rows(paths, "SELECT endpoint, received_at, CASE WHEN error IS NULL THEN status END FROM polls "
                  "WHERE received_at > ? AND received_at < ?",
                  (as_of - O.DEX_FRESH_S, as_of))
    items = _rows(paths, "SELECT endpoint, received_at, payload_json FROM items WHERE chain_id = 'solana' "
                  "AND token_address = ? AND received_at > ? AND received_at < ?", (mint, as_of - O.WINDOW_S, as_of))
    return polls is not None and O.dex_healthy(polls, now=as_of), items or []


def _x_inputs(mint, desk_dir, as_of):
    try:
        beat = float(json.loads((Path(desk_dir) / "x" / "producer.heartbeat").read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, [], False
    paths = [Path(desk_dir) / "x" / ("%s.db" % d) for d in _days(as_of, O.X_WINDOW_S)]
    # J7 sends no chain for a Solana mint (NULL on every Solana row, 10-21), a named chain for EVM. The author is
    # the post known strictly before as_of (one tweet has several post rows; handles differ only in case).
    links = _rows(paths, "SELECT m.tweet_id, (SELECT MIN(lower(p.handle)) FROM posts p WHERE p.tweet_id = m.tweet_id "
                  "AND p.handle IS NOT NULL AND p.received_at < ?), m.received_at FROM token_meta m "
                  "WHERE m.mint = ? AND COALESCE(m.chain, 'solana') = 'solana' AND m.received_at > ? AND m.received_at < ?",
                  (as_of, mint, as_of - O.X_WINDOW_S, as_of)) or []
    # Gamma 10-22: NULL chain = Solana is observational (10-21 rows), not source-verified. The silent-exclusion
    # case is our exact mint with a named non-Solana chain: counted and logged, never on the sheet.
    excluded = sum(r[0] for r in _rows(paths, "SELECT COUNT(*) FROM token_meta WHERE mint = ? AND chain IS NOT NULL "
                                       "AND chain <> 'solana' AND received_at > ? AND received_at < ?",
                                       (mint, as_of - O.X_WINDOW_S, as_of)) or [])
    if excluded:
        CHAIN_EXCLUDED["rows"] += excluded
        logger.warning("desk context: %d token_meta rows for mint=%s carried a non-solana chain, excluded", excluded, mint)
    # Gamma LOW1: a tweet whose author is not known at as_of is excluded, never counted as organic
    known = [r for r in links if r[1]]
    return 0 <= as_of - beat <= O.X_FRESH_S, known, len(known) < len(links)   # a heartbeat from the future is not fresh


def _crawl(mint, desk_dir, as_of):
    rows = _rows([Path(desk_dir) / "crawler" / "crawls.db"],
                 "SELECT intent, known_at FROM crawls WHERE mint = ? AND network_id = 'solana' AND intent IS NOT NULL "
                 "AND known_at < ? AND known_at > ? ORDER BY known_at DESC LIMIT 1",
                 (mint, as_of, as_of - O.CRAWL_MAX_AGE_S))
    return rows[0] if rows else None


def dex_and_heat(mint, desk_dir, as_of):
    """DexScreener presence flags with ages, and the offchain heat components, both as of the call."""
    dex_ok, items = _dex_inputs(mint, desk_dir, as_of)
    x_ok, links, author_unknown = _x_inputs(mint, desk_dir, as_of)
    handles = _promoters(desk_dir)
    crawl = _crawl(mint, desk_dir, as_of)
    row = O.coin_row(mint, now=as_of, dex_items=items, dex_ok=dex_ok, x_links=links, x_ok=x_ok,
                     promoters=handles[1] if handles else set(), crawl=crawl)
    if not dex_ok:
        dex = _unknown("source_unhealthy")
    else:
        # DexScreener metas carry no token identity; theme/name matching is prohibited (Codex/Gamma 10-22)
        dex = {"state": "ok", "boost_units": row["paid_boost_units"], "trending_meta": None,
               "trending_meta_reason": "no_token_identity"}
        for kind, key in DEX_KINDS.items():
            seen = [t for e, t, _ in items if O.PAID_ENDPOINTS.get(e) == kind]
            dex[key] = bool(seen)
            dex[key + "_age_s"] = round(as_of - max(seen), 1) if seen else None
    # without the promoter list every X count is unknown: an empty set would count promoters as organic
    # Gamma LOW2: a zero while some tweet's author is still unknown is not a known zero (a positive count stays a
    # valid lower bound)
    x_count = lambda v: None if not handles or (author_unknown and v == 0) else v
    heat = {"state": "ok", "x_state": row["x_state"], "x_mentions_15m": x_count(row["x_mentions_15m"]),
            "x_accounts_15m": x_count(row["x_accounts_15m"]),
            "x_promoter_mentions_15m": x_count(row["x_promoter_mentions_15m"]),
            "promoter_list": handles[0] if handles else None,
            "crawler_intent": row["crawler_intent"] if row["crawler_intent"] in INTENTS else None,
            "crawler_age_s": round(as_of - row["crawler_known_at"], 1) if row["crawler_known_at"] else None,
            "heat_score": None}       # no per-coin heat score exists yet (the researcher's scores are per theme)
    return dex, heat


def _promoters(desk_dir):
    """(version, handles) read-only; None when the list is absent or malformed (counts then unknown)."""
    try:
        data = json.loads((Path(desk_dir) / "offchain" / "promoters.json").read_text())
    except (OSError, ValueError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    if not isinstance(v, str) or not v.startswith("promoter-list-v"):
        return None
    return v, {str(h).lower().lstrip("@") for h in data.get("handles") or []}


def context_for(mint, *, desk_dir, as_of):
    """The 'ctx' block for one coin as of `as_of` (epoch). Never raises: a source error is that part unknown."""
    out = {"version": CONTEXT_VERSION}
    for key, fn in (("trending", trending), ("narrative", narrative)):
        try:
            out[key] = fn(mint, desk_dir, as_of)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            out[key] = _unknown("unreadable")
    try:
        out["dex"], out["heat"] = dex_and_heat(mint, desk_dir, as_of)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        out["dex"], out["heat"] = _unknown("unreadable"), _unknown("unreadable")
    return out


_STATES = frozenset({"ok", "unknown"})
_REASONS = frozenset({"no_source", "stale", "curve_retired", "not_tagged", "off_enum", "source_unhealthy", "unreadable",
                      "context_error", "context_invalid", "not_applicable"})
_SHAPE = {
    "trending": {"state", "reason", "coverage", "age_s", "on_board", "rank", "buy15_rank", "board_size"},
    "narrative": {"state", "reason", "theme", "theme_15m"},
    "theme_15m": {"state", "reason", "coverage", "age_s", "launches", "buy_usd", "buyers", "slope", "heating"},
    "dex": {"state", "reason", "boost_units", "trending_meta", "trending_meta_reason"} | {k for v in DEX_KINDS.values() for k in (v, v + "_age_s")},
    "heat": {"state", "reason", "x_state", "x_mentions_15m", "x_accounts_15m", "x_promoter_mentions_15m",
             "promoter_list", "crawler_intent", "crawler_age_s", "heat_score"},
}
_ENUMS = {"state": _STATES, "reason": _REASONS, "coverage": frozenset({"ok", "gap"}), "theme": frozenset(THEMES),
          "x_state": frozenset({"ok", "missing"}), "crawler_intent": INTENTS,
          "trending_meta_reason": frozenset({"no_token_identity"})}


def _scalar(key, v):
    if v is None or type(v) is bool:
        return True
    if type(v) in (int, float):
        return v == v and abs(v) < 1e15
    if isinstance(v, str):
        if key == "promoter_list":
            return v.startswith("promoter-list-v") and v[15:].isdigit()
        return v in _ENUMS.get(key, ())
    return False


def validate(ctx) -> dict:
    """Closed legacy scalars or an explicitly versioned retained-M ranking. ValueError('context')."""
    import copy

    def part(name, d):
        if name == 'narrative' and isinstance(d, dict) and 'version' in d:
            if (set(d) != {'state','theme','version','theme_15m'} or d['version'] != 'narrative-live-v2'
                    or d['state'] != 'ok' or d['theme'] not in THEMES
                    or d['theme_15m'] != _unknown('curve_retired')):
                raise ValueError('context')
            return
        if name == 'trending' and isinstance(d, dict) and 'version' in d:
            from sniper.services import memecoin_desk_ps_trending as T
            T.validate(d)
            return
        if not isinstance(d, dict) or not set(d) <= _SHAPE[name] or d.get("state") not in _STATES:
            raise ValueError("context")
        for k, v in d.items():
            if k == "theme_15m":
                part("theme_15m", v)
            elif not _scalar(k, v):
                raise ValueError("context")
    if not isinstance(ctx, dict) or ctx.get("version") != CONTEXT_VERSION or set(ctx) != {"version", *_SHAPE} - {"theme_15m"}:
        raise ValueError("context")
    for name in ("trending", "narrative", "dex", "heat"):
        part(name, ctx[name])
    return copy.deepcopy(ctx)


def unknown_block(reason):
    return {"version": CONTEXT_VERSION, **{k: _unknown(reason) for k in ("trending", "narrative", "dex", "heat")}}


def judge_context_fn(desk_dir, cohort_of):
    """context_fn for memecoin_desk_judge.run_once: (ticket, candidate, now_epoch) -> the ctx block, or None for a cohort
    whose frozen policy has no 'context' (an older cohort gets no block)."""
    def fn(ticket, candidate, ts, *, ps_facts=None):
        rule = (cohort_of(ticket).get("policy") or {}).get("context")
        if not isinstance(rule, dict):
            return None
        if candidate.get("venue") == "pumpswap":
            if rule.get("pumpswap") is not True:
                return unknown_block("not_applicable")
            out = context_for(candidate["mint"], desk_dir=desk_dir, as_of=ts)
            out["trending"] = _unknown("curve_retired")
            if 'ps_trending' in rule:
                from sniper.services import memecoin_desk_ps_trending as T
                out['trending'] = (T.trending_for(candidate, desk_dir=desk_dir, as_of=ts, ps_facts=ps_facts)
                                   if rule['ps_trending'] == T.definition() else T.unknown('definition_mismatch'))
            if out["narrative"].get("state") == "ok":
                out["narrative"]["theme_15m"] = _unknown("curve_retired")
            return out
        return context_for(candidate["mint"], desk_dir=desk_dir, as_of=ts)
    return fn
