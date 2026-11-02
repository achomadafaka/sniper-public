#!/usr/bin/env python3
"""Desk CRAWLER worker (design 2026-10-21-desk-crawler-seat-design.md, placement (a)).

Every pass: new X producer posts and J7 token_meta rows that name one Solana coin put the coin on the
watch set; a coin with new input and no crawl in the last REFRESH_S gets one Haiku call (the JUDGE
worker's subscription adapter, no tools); the enum record goes to data/desk/crawler/crawls.db. U stamps
the newest fresh crawl into its snapshot at emission. Off every decision and fill path; one writer.

Knobs (state root .env): MEMECOIN_DESK_CRAWLER_ENABLED=1, MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR and
MEMECOIN_DESK_CRAWLER_CALLS_PER_DAY (required, no default; current operator caps 20/hour and 600/day).

    python3 scripts/memecoin_desk_crawler.py [--loop]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_desk_crawler_live import LiveResolver, finite
from sniper.services import memecoin_desk_crawler as C               # noqa: E402
from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

REFRESH_S = 600
DEADLINE_S = 20.0
PASS_S = 30.0
B58 = re.compile(r"\A[1-9A-HJ-NP-Za-km-z]{32,44}\Z")


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SC = _module("desk_source_scout", ROOT / "scripts" / "memecoin_desk_source_scout.py")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def payload_dict(raw):
    """A payload's JSON object, or None when it is malformed or its root is not an object (Codex/Gamma
    poison audit 2026-10-21: a list root or a list text raised and stalled every coin behind it)."""
    try:
        out = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return out if isinstance(out, dict) else None


def post_text(raw):
    """The post's text when it is a string, else None (unreadable)."""
    out = payload_dict(raw)
    text = out.get("text") if out is not None else None
    return text if isinstance(text, str) else None


def meta_name(raw):
    """(readable, name) of a J7 token_meta payload: a missing tokenMeta or name is readable with name None;
    a malformed payload, a non-object tokenMeta or a non-string name is unreadable."""
    out = payload_dict(raw)
    if out is None:
        return False, None
    meta = out.get("tokenMeta")
    if meta is None:
        return True, None
    if not isinstance(meta, dict) or not (meta.get("name") is None or isinstance(meta.get("name"), str)):
        return False, None
    return True, meta.get("name")


def solana_mint(mint, chain):
    """J7 token_meta sends chain NULL for Solana mints (2026-10-21 sample: eth/robinhood named, Solana not)."""
    return isinstance(mint, str) and B58.match(mint) and (chain is None or str(chain).lower() in ("sol", "solana"))


def ingest(db, x_dir, desk_dir, now, stats=None, *, resolver=None):
    """Resolve new posts with the supplied live snapshot (legacy Tape only for replay helpers)."""
    days = sorted({utc_day(now - 86400), utc_day(now)})
    tape = resolver if resolver is not None else SC.Tape(desk_dir, SC.days_back(utc_day(now), 2))
    if resolver is not None and stats is not None:
        stats.update(resolver_source=resolver.source, resolver_state=resolver.state,
                     resolver_metadata_capped=resolver.metadata_capped, resolver_tape_capped=resolver.tape_capped)
    cut = min(now, resolver.cut_host) if resolver is not None else now
    added = 0
    for day in days:
        path = Path(x_dir) / ("%s.db" % day)
        if not path.exists():
            continue
        src = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
        src.row_factory = sqlite3.Row
        for kind, sql in (("post", "SELECT id, received_at, handle, event, payload_json FROM posts WHERE id > ? "
                                   "AND event NOT IN ('tweet_deleted','quoted_tweet','reply_tweet') ORDER BY id"),
                          ("meta", "SELECT id, received_at, mint, chain FROM token_meta WHERE id > ? ORDER BY id")):
            if kind == "post" and resolver is not None and resolver.state == "unavailable":
                continue  # keep cursor: retry unreadable live inputs next pass
            cur = db.execute("SELECT last_id FROM cursors WHERE kind=? AND day=?", (kind, day)).fetchone()
            last = cur[0] if cur else 0
            for row in src.execute(sql, (last,)):
                if not finite(row["received_at"]) or row["received_at"] > cut:
                    # Stop in ID order: filtering/skipping could consume a deferred receipt.
                    if stats is not None:
                        stats["deferred_receipts"] = stats.get("deferred_receipts", 0) + 1
                    break
                last = row["id"]
                if kind == "post":
                    text = post_text(row["payload_json"])
                    if text is None:                   # unreadable or wrong shape: skipped, cursor advances
                        if stats is not None:
                            stats["unreadable_rows"] += 1
                        continue
                    mint, _ = tape.resolve(text, row["received_at"])
                elif solana_mint(row["mint"], row["chain"]):
                    mint = row["mint"]
                else:
                    mint = None
                if not mint:
                    continue
                db.execute("INSERT OR IGNORE INTO inputs VALUES (?,?,?,?,?)", (kind, day, row["id"], mint, row["received_at"]))
                # new input re-opens a watch that left on quiet_6h; graduation stays terminal (Codex/Gamma)
                db.execute("INSERT INTO watch (mint, first_at, last_input_at) VALUES (?,?,?) ON CONFLICT(mint) DO UPDATE "
                           "SET last_input_at=MAX(last_input_at, excluded.last_input_at), left_at=NULL, left_reason=NULL "
                           "WHERE left_at IS NULL OR left_reason='quiet_6h'",
                           (mint, row["received_at"], row["received_at"]))
                added += 1
            db.execute("INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(kind, day) DO UPDATE SET last_id=excluded.last_id",
                       (kind, day, last))
        src.close()
    db.commit()
    return added


def graduated(desk_dir, mint, now):
    for day in sorted({utc_day(now - 86400), utc_day(now)}):
        path = Path(desk_dir) / "swaps" / ("%s.db" % day)
        if path.exists():
            c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
            try:
                if c.execute("SELECT 1 FROM swaps WHERE mint=? AND real_token=0 LIMIT 1", (mint,)).fetchone():
                    return True
            finally:
                c.close()
    return False


def evidence(db, x_dir, desk_dir, mint, now):
    """(posts, metas, input ids, unreadable refs) for the call; text is read here and never stored. An
    unreadable ref adds nothing and is counted."""
    rows = db.execute("SELECT kind, day, ref_id FROM inputs WHERE mint=? ORDER BY received_at", (mint,)).fetchall()
    posts, metas, ids, unreadable = [], [], [[k, d, i] for k, d, i in rows], 0
    by_day = {}
    for kind, day, ref in rows:
        by_day.setdefault(day, []).append((kind, ref))
    for day, refs in sorted(by_day.items()):
        path = Path(x_dir) / ("%s.db" % day)
        if not path.exists():                          # lost evidence is unreadable, never an empty read (Codex)
            unreadable += len(refs)
            continue
        src = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
        for kind, ref in refs:
            if kind == "post":
                r = src.execute("SELECT handle, received_at, payload_json FROM posts WHERE id=?", (ref,)).fetchone()
                if r and post_text(r[2]) is not None:
                    posts.append((r[0], r[1], post_text(r[2])))
                else:                                  # a deleted row counts like a malformed one
                    unreadable += 1
            else:
                r = src.execute("SELECT symbol, payload_json FROM token_meta WHERE id=?", (ref,)).fetchone()
                readable, name = meta_name(r[1]) if r else (False, None)
                if readable and (r[0] is None or isinstance(r[0], str)):
                    metas.append(("j7_token_meta", r[0], name))
                else:
                    unreadable += 1
        src.close()
    for day in SC.days_back(utc_day(now), 2):
        path = Path(desk_dir) / "launches" / ("%s.db" % day)
        if path.exists():
            c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
            try:
                r = c.execute("SELECT symbol, name FROM launches WHERE mint=?", (mint,)).fetchone()
            except sqlite3.Error:
                r = None                                   # a day file before the symbol column
            c.close()
            if r and (r[0] or r[1]):
                metas.append(("launch", r[0], r[1]))
                break
    return posts, metas, ids, unreadable


def crawl_pass(db, *, x_dir, desk_dir, call_model, per_hour, per_day, now=None, clock=time.time, resolver=None):
    """One pass. `now` is the pass clock (watch, refresh, caps); a crawl's known_at is `clock()` when its
    answer is in, so U never stamps a crawl before it existed."""
    now = now or clock()
    stats = {"ingested": 0, "crawled": 0, "failed": 0, "cap_skipped": 0, "left": 0, "unreadable_rows": 0}
    stats["ingested"] = ingest(db, x_dir, desk_dir, now, stats, resolver=resolver)
    for mint, last_input in db.execute("SELECT mint, last_input_at FROM watch WHERE left_at IS NULL").fetchall():
        why = "quiet_6h" if now - last_input > C.FRESH_S else "graduated" if graduated(desk_dir, mint, now) else None
        if why:
            db.execute("UPDATE watch SET left_at=?, left_reason=? WHERE mint=?", (now, why, mint))
            stats["left"] += 1
    # due = input not yet covered by a SUCCESSFUL crawl (a failed one retries at the next refresh) and no
    # attempt in REFRESH_S; last_crawl_at is the attempt start, written with the charge (Codex/Gamma)
    due = db.execute("SELECT mint FROM watch WHERE left_at IS NULL AND last_input_at > COALESCE(last_ok_at, 0) "
                     "AND ? - COALESCE(last_crawl_at, 0) >= ? ORDER BY last_input_at", (clock(), REFRESH_S)).fetchall()
    for (mint,) in due:
        start = clock()                                # each call's own start: the rolling caps count real starts
        hour = db.execute("SELECT COUNT(*) FROM attempts WHERE at > ?", (start - 3600,)).fetchone()[0]
        day = db.execute("SELECT COUNT(*) FROM attempts WHERE at > ?", (start - 86400,)).fetchone()[0]
        try:
            posts, metas, ids, unreadable = evidence(db, x_dir, desk_dir, mint, now)
            error = None if (posts or metas or not unreadable) else "evidence_unreadable"
        except Exception as exc:                   # one mint's bad input never stops the pass (Gamma)
            ids, unreadable, error = [], 0, "evidence_error:" + type(exc).__name__
        stats["unreadable_rows"] += unreadable
        if unreadable:
            ids = ids + [["unreadable", None, unreadable]]
        input_hash = hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()
        if error:
            # nothing readable: no model call on empty evidence, nothing charged, never stamped; the attempt
            # time keeps the refresh spacing, new input or the next refresh tries again
            db.execute("INSERT INTO crawls (network_id, mint, known_at, input_hash, inputs_json, call_error, "
                       "prompt_version) VALUES ('solana',?,?,?,?,?,?)",
                       (mint, start, input_hash, json.dumps(ids), error, C.CRAWLER_VERSION))
            db.execute("UPDATE watch SET last_crawl_at=? WHERE mint=?", (start, mint))
            db.commit()
            stats["failed"] += 1
            continue
        if hour >= per_hour or day >= per_day:
            # one cap row per new input, so U tells a cap-censored coin from an un-posted one (Gamma)
            last = db.execute("SELECT input_hash, call_error FROM crawls WHERE mint=? ORDER BY id DESC LIMIT 1",
                              (mint,)).fetchone()
            if last != (input_hash, "cap"):
                db.execute("INSERT INTO crawls (network_id, mint, known_at, input_hash, inputs_json, call_error, "
                           "prompt_version) VALUES ('solana',?,?,?,?,'cap',?)",
                           (mint, start, input_hash, json.dumps(ids), C.CRAWLER_VERSION))
                stats["cap_skipped"] += 1
            continue
        db.execute("INSERT INTO attempts (mint, at) VALUES (?,?)", (mint, start))   # charged before the call
        db.execute("UPDATE watch SET last_crawl_at=? WHERE mint=?", (start, mint))  # same commit: a crash keeps the 600 s
        db.commit()
        t0 = time.monotonic()
        res = call_model(C.SYSTEM, C.build_user(mint, posts, metas))
        latency = (time.monotonic() - t0) * 1000
        try:
            if C.result_error(res):
                raise ValueError(C.result_error(res))
            intent, flags = C.validate(res.get("text"), mint)
            err = None
        except ValueError as exc:
            intent, flags, err = None, None, str(exc)
        known_at = clock()
        db.execute("INSERT INTO crawls (network_id, mint, known_at, input_hash, inputs_json, intent, flags_json, model, "
                   "call_error, latency_ms, prompt_version) VALUES ('solana',?,?,?,?,?,?,?,?,?,?)",
                   (mint, known_at, input_hash, json.dumps(ids), intent, json.dumps(flags) if flags else None,
                    res.get("model_echo"), err, round(latency, 1), C.CRAWLER_VERSION))
        if err is None:
            db.execute("UPDATE watch SET last_ok_at=? WHERE mint=?", (start, mint))
        db.commit()
        stats["failed" if err else "crawled"] += 1
    db.commit()
    return stats


def main(argv=None, *, call_model=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    load_env_files(str(state_root()))                  # cron has no environment
    if os.getenv("MEMECOIN_DESK_CRAWLER_ENABLED", "0").strip() != "1":
        print("[desk-crawler] MEMECOIN_DESK_CRAWLER_ENABLED is not 1; exiting")
        return 0
    try:
        per_hour = min(20, int(os.environ["MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR"]))
        per_day = min(600, int(os.environ["MEMECOIN_DESK_CRAWLER_CALLS_PER_DAY"]))
    except (KeyError, ValueError):
        print("[desk-crawler] MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR and _PER_DAY must be set integers; exiting",
              file=sys.stderr)
        return 2
    desk = Path(state_root()) / "data" / "desk"
    out = desk / "crawler"
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / "crawler.lock", "w")
    try:                                              # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-crawler] another crawler holds the lock; exiting")
        return 0
    if call_model is None:
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):   # the seat runs on the subscription
            os.environ.pop(key, None)
        jw = _module("desk_judge_worker", ROOT / "scripts" / "memecoin_desk_judge_worker.py")
        call_model = lambda system, user: jw.sdk_call_model(system, user, deadline_s=DEADLINE_S, model=C.MODEL, persist_session=False)
    db = C.connect(out / "crawls.db")
    try:
        while True:
            from sniper.util.clock import offset_s
            resolver = LiveResolver(desk, time.time(), clock_offset_s=offset_s())
            print(json.dumps(crawl_pass(db, x_dir=desk / "x", desk_dir=desk, call_model=call_model,
                                        per_hour=per_hour, per_day=per_day, resolver=resolver), sort_keys=True), flush=True)
            if not a.loop:
                return 0
            time.sleep(PASS_S)
    except KeyboardInterrupt:
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
