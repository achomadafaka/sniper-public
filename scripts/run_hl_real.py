#!/usr/bin/env python3
"""HL real copy leg — tails the paper mirror book (hl_copy_book) and mirrors eligible
rows with REAL Hyperliquid orders at small size, under write-ahead intents with
deterministic cloids.

Hard behaviors (tested in tests/test_run_hl_real.py):
  * write-ahead: intent row lands BEFORE any submission; crash recovery settles by
    cloid query, never by blind resend.
  * HL_REAL_ENABLED=0 blocks ENTRIES ONLY — exits, settlement, reconcile always run.
  * HL_REAL_DRY_RUN=1 records every decision as 'dry_run' and never touches the SDK.
  * rail/eligibility skips are TERMINAL (copy at detection or not at all — a late entry
    would diverge from its paper twin and poison the drift measurement).
  * cumulative UTC-day realized loss <= -HL_REAL_DAY_LOSS_BREAKER_USD latches the
    breaker (persisted; operator clears via scripts/hl_real_resolve.py).
  * reconcile faults block new entries until the book matches the exchange again.

Spec: docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import intel_store as IS       # noqa: E402
from sniper.hyperlive import real_exec as RE         # noqa: E402
from sniper.hyperlive import real_store as RS        # noqa: E402
from sniper.hyperlive.real_notify import notify      # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
HEARTBEAT = "/home/bigpapi/Desktop/Sniper/data/hl_real.heartbeat"
RECONCILE_EVERY = 6                                   # tail cycles between reconciles

_fault_block = False                                  # sticky until a clean reconcile
_reconcile_error: str | None = None
_notified_faults: set[tuple] = set()
_cycle_n = 0


def _day_utc(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _paper_row(conn, copy_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM hl_copy_book WHERE id=?", (copy_id,)).fetchone()
    return dict(r) if r else None


_finalize_close = RS.refinalize_close                 # shared with hl_real_resolve.py


def _record_landed(conn, it: dict, norm: dict, now_ms: int) -> None:
    RS.set_intent_state(conn, it["cloid"], norm["state"], now_ms=now_ms)
    if norm["state"] in ("filled", "partial"):
        RS.record_fill(conn, cloid=it["cloid"], copy_id=it["copy_id"], leg=it["leg"],
                       fill_px=norm["fill_px"], fill_sz=norm["fill_sz"],
                       filled_ts_ms=now_ms)
        if it["leg"] == "close":
            _finalize_close(conn, it["cloid"])


def _settle_pending(conn, ex, now_ms: int) -> None:
    for it in RS.intents_in(conn, ["submitted", "ambiguous"]):
        try:
            state = RE.settle_from_status(ex.order_status(it["cloid"]))
        except Exception:
            continue                                   # transient — retry next cycle
        if state == "filled":
            agg = None
            try:
                agg = ex.fills_for_cloid(it["cloid"])
            except Exception:
                pass
            norm = {"state": "filled",
                    "fill_px": agg["px"] if agg else None,
                    "fill_sz": agg["sz"] if agg else it.get("requested_sz")}
            _record_landed(conn, it, norm, now_ms)
            if agg and agg.get("fee") is not None:
                RS.set_fill_fee(conn, it["cloid"], agg["fee"])
                if it["leg"] == "close":
                    _finalize_close(conn, it["cloid"])
        elif state == "rejected":
            RS.set_intent_state(conn, it["cloid"], "rejected", now_ms=now_ms)
            notify(f"order rejected on settle: {it['leg']} {it['coin']} copy_id={it['copy_id']}")


def _backfill_fees(conn, ex) -> None:
    rows = conn.execute("SELECT cloid, leg FROM hl_real_fills WHERE fee_usd IS NULL").fetchall()
    for r in rows:
        try:
            agg = ex.fills_for_cloid(r["cloid"])
        except Exception:
            return
        if agg and agg.get("fee") is not None:
            RS.set_fill_fee(conn, r["cloid"], agg["fee"])
            if r["leg"] == "close":
                _finalize_close(conn, r["cloid"])


def _process_closes(conn, ex, cfg, dep, now_ms: int) -> int:
    """ALWAYS runs — the kill switch must never strand a real position."""
    closed = 0
    for pos in RS.leg_open_positions(conn):
        paper = _paper_row(conn, pos["copy_id"])
        if not paper or paper.get("status") == "open":
            continue
        cloid = RE.derive_cloid(dep["deployment_id"], pos["copy_id"], "close")
        existing = RS.intent_for(conn, pos["copy_id"], "close")
        if existing and existing["state"] in ("submitted", "ambiguous", "filled",
                                              "partial", "dry_run"):
            continue                                   # in flight / done / dry
        if existing and existing["state"] == "rejected":
            # a rejected close might actually have landed (duplicate-cloid resend path):
            # trust on-exchange fills before retrying.
            try:
                agg = ex.fills_for_cloid(cloid)
            except Exception:
                agg = None
            if agg:
                _record_landed(conn, dict(existing), {"state": "filled", "fill_px": agg["px"],
                                                      "fill_sz": agg["sz"]}, now_ms)
                RS.set_fill_fee(conn, cloid, agg["fee"])
                _finalize_close(conn, cloid)
                closed += 1
                continue
        RS.record_intent(conn, cloid=cloid, deployment_id=dep["deployment_id"],
                         copy_id=pos["copy_id"], leg="close", coin=pos["coin"],
                         side=pos["side"], requested_usd=None,
                         requested_sz=pos["fill_sz"], state="intended", reason=None,
                         now_ms=now_ms)
        if cfg.HL_REAL_DRY_RUN:
            RS.set_intent_state(conn, cloid, "dry_run", now_ms=now_ms)
            continue
        try:
            resp = ex.market_close(pos["coin"], cloid)
        except Exception as e:                         # noqa: BLE001
            resp = e
        norm = RE.normalize_order_result(resp, pos["fill_sz"] or 0.0)
        it = RS.intent_for(conn, pos["copy_id"], "close")
        _record_landed(conn, it, norm, now_ms)
        if norm["state"] in ("filled", "partial"):
            closed += 1
            notify(f"closed {pos['coin']} {pos['side']} copy_id={pos['copy_id']} "
                   f"@{norm['fill_px']}")
        elif norm["state"] == "ambiguous":
            notify(f"AMBIGUOUS close {pos['coin']} copy_id={pos['copy_id']} — will settle by cloid")
    return closed


def _process_opens(conn, ex, cfg, dep, now_ms: int) -> tuple[int, int]:
    opened = skipped = 0
    breaker = RS.breaker_latched(conn) is not None
    marks = None
    rows = conn.execute(
        "SELECT * FROM hl_copy_book WHERE status='open' AND detect_ts_ms>=? ORDER BY id",
        (dep["epoch_ms"],)).fetchall()
    for row in rows:
        row = dict(row)
        if RS.intent_for(conn, row["id"], "open"):
            continue
        cloid = RE.derive_cloid(dep["deployment_id"], row["id"], "open")

        def _skip(reason: str) -> None:
            RS.record_intent(conn, cloid=cloid, deployment_id=dep["deployment_id"],
                             copy_id=row["id"], leg="open", coin=row["coin"],
                             side=row["side"] or "", requested_usd=cfg.HL_REAL_PER_POS_USD,
                             requested_sz=None, state="skipped", reason=reason,
                             now_ms=now_ms)

        ok, why = RE.eligibility(row, allow_targets=cfg.HL_REAL_TARGETS,
                                 epoch_ms=dep["epoch_ms"],
                                 alt_short_only=getattr(cfg, "HL_REAL_ALT_SHORT_ONLY", False))
        if not ok:
            _skip(why)
            skipped += 1
            continue
        if _fault_block:
            _skip("reconcile_fault")
            skipped += 1
            continue
        if marks is None:
            try:
                marks = ex.fetch_marks()
            except Exception:
                marks = {}
        mark = marks.get(str(row["coin"]).upper())
        if not mark:
            continue                                   # transient — retry next cycle
        book = RS.leg_open_positions(conn)
        total_notional = sum((p["fill_px"] or 0) * (p["fill_sz"] or 0) for p in book)
        try:
            szd = ex.sz_decimals(row["coin"])
        except Exception:
            continue                                   # transient
        dec = RE.entry_decision(row, enabled=cfg.HL_REAL_ENABLED, breaker_latched=breaker,
                                open_n=len(book), total_notional=total_notional,
                                mark=mark, sz_decimals=szd, cfg=cfg)
        if dec.action == "skip":
            _skip(dec.reason)
            skipped += 1
            continue
        RS.record_intent(conn, cloid=cloid, deployment_id=dep["deployment_id"],
                         copy_id=row["id"], leg="open", coin=row["coin"],
                         side=row["side"], requested_usd=cfg.HL_REAL_PER_POS_USD,
                         requested_sz=dec.sz, state="intended", reason=None, now_ms=now_ms)
        if cfg.HL_REAL_DRY_RUN:
            RS.set_intent_state(conn, cloid, "dry_run", now_ms=now_ms)
            continue
        try:
            ex.set_isolated_1x(row["coin"])
            resp = ex.market_open(row["coin"], row["side"] == "long", dec.sz, cloid)
        except Exception as e:                         # noqa: BLE001
            resp = e
        norm = RE.normalize_order_result(resp, dec.sz)
        it = RS.intent_for(conn, row["id"], "open")
        _record_landed(conn, it, norm, now_ms)
        if norm["state"] in ("filled", "partial"):
            opened += 1
            notify(f"OPENED {row['coin']} {row['side']} sz={norm['fill_sz']} "
                   f"@{norm['fill_px']} (paper twin #{row['id']})")
        elif norm["state"] == "ambiguous":
            notify(f"AMBIGUOUS open {row['coin']} copy_id={row['id']} — will settle by cloid")
    return opened, skipped


def _check_breaker(conn, cfg, now_ms: int) -> None:
    if RS.breaker_latched(conn) is not None:
        return
    day = _day_utc(now_ms)
    realized = RS.day_realized_usd(conn, day)
    if realized <= -cfg.HL_REAL_DAY_LOSS_BREAKER_USD:
        RS.latch_breaker(conn, day_utc=day, now_ms=now_ms)
        notify(f"BREAKER LATCHED: day {day} realized {realized:+.2f} USD — entries stopped "
               f"until cleared via hl_real_resolve.py")


def _reconcile(conn, ex) -> int:
    global _fault_block, _reconcile_error
    try:
        exchange_pos = ex.positions()
    except Exception as exc:                           # noqa: BLE001
        _fault_block = True
        _reconcile_error = type(exc).__name__
        sig = ("transport", _reconcile_error, None)
        if sig not in _notified_faults:
            _notified_faults.add(sig)
            notify(f"RECONCILE TRANSPORT FAULT {_reconcile_error} — entries blocked")
        return 0
    faults = RE.reconcile(RS.leg_open_positions(conn), exchange_pos)
    _fault_block = bool(faults)
    _reconcile_error = None
    for f in faults:
        sig = (f["kind"], f["coin"], f.get("copy_id"))
        if sig not in _notified_faults:
            _notified_faults.add(sig)
            notify(f"RECONCILE FAULT {f['kind']} {f['coin']} copy_id={f.get('copy_id')} "
                   f"— entries blocked; resolve via hl_real_resolve.py")
    if not faults:
        _notified_faults.clear()
    return len(faults)


def cycle(conn, ex, cfg, now_ms: int, *, force_reconcile: bool = False) -> dict:
    global _cycle_n
    _cycle_n += 1
    dep = RS.get_or_create_deployment(conn, now_ms=now_ms)
    _settle_pending(conn, ex, now_ms)
    _backfill_fees(conn, ex)
    closed = _process_closes(conn, ex, cfg, dep, now_ms)
    _check_breaker(conn, cfg, now_ms)
    faults = 0
    if force_reconcile or _cycle_n == 1 or (_cycle_n % RECONCILE_EVERY == 0):
        faults = _reconcile(conn, ex)
    opened, skipped = _process_opens(conn, ex, cfg, dep, now_ms)
    return {"opened": opened, "closed": closed, "skipped": skipped, "faults": faults,
            "reconcile_error": _reconcile_error}


def _write_heartbeat(conn, cfg, out: dict, now_s: float) -> None:
    try:
        hb = {"ts": now_s, "enabled": bool(cfg.HL_REAL_ENABLED),
              "dry_run": bool(cfg.HL_REAL_DRY_RUN),
              "open_n": len(RS.leg_open_positions(conn)),
              "pending": len(RS.intents_in(conn, ["submitted", "ambiguous"])),
              "breaker": RS.breaker_latched(conn), "fault_block": _fault_block,
              "reconcile_error": _reconcile_error,
              "last": out}
        with open(HEARTBEAT, "w") as f:
            json.dump(hb, f)
    except Exception:                                  # noqa: BLE001
        pass                                           # heartbeat must never kill the loop


def main(argv=None) -> int:
    # Load .env BEFORE the settings import reads os.getenv — the watchdog launches this
    # process with a bare environment (found live 2026-07-17: leg started enabled=False
    # despite .env saying 1).
    from sniper.util.envfiles import load_env_files
    load_env_files("/home/bigpapi/Desktop/Sniper")
    from sniper.settings import hl_real as cfg
    from sniper.hyperlive.hl_exchange import HlExchange
    from sniper.markets import hyperliquid_api as HL

    ap = argparse.ArgumentParser(description="HL real copy leg (tails hl_copy_book)")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    # Single-instance lock held for the PROCESS LIFETIME. This leg signs REAL orders, so a
    # second copy is a double-submit. The watchdog cannot prevent one on its own: it matches a
    # running process by "$CODE_ROOT/scripts/run_hl_real.py --db-path $DB" and CODE_ROOT
    # resolves through .runtime/hyperliquid/current, so repointing that symlink orphans the
    # running process and the next cron tick launches another (the same defect that left three
    # copies each of the squeeze/altshort workers on 2026-08-03).
    from sniper.services.memecoin_runner import acquire_run_lock
    _run_lock = acquire_run_lock(os.getenv(
        "HL_REAL_RUN_LOCK", str(Path(args.db_path).resolve().parent / "hl_real.run.lock")))
    if _run_lock is None:
        print("another run_hl_real holds the run lock — refusing to start "
              "(double-runner = double-submit)", file=sys.stderr)
        return 0

    ex = HlExchange(api_wallet_file=cfg.HL_REAL_API_WALLET_FILE,
                    account_address=cfg.HL_REAL_ACCOUNT_ADDRESS,
                    base_url=cfg.HL_REAL_BASE_URL,
                    entry_slippage=cfg.HL_REAL_ENTRY_SLIPPAGE,
                    exit_slippage=cfg.HL_REAL_EXIT_SLIPPAGE)
    ex.fetch_marks = HL.fetch_marks                    # marks come from the public info API
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    print(f"hl-real starting enabled={cfg.HL_REAL_ENABLED} dry_run={cfg.HL_REAL_DRY_RUN} "
          f"targets={cfg.HL_REAL_TARGETS}", file=sys.stderr)
    try:
        while True:
            now_ms = int(time.time() * 1000)
            try:
                out = cycle(conn, ex, cfg, now_ms)
                _write_heartbeat(conn, cfg, out, time.time())
                if any(out.values()):
                    print(f"{time.strftime('%FT%TZ', time.gmtime())} hl-real: {out}",
                          file=sys.stderr)
            except Exception as e:                     # noqa: BLE001
                print(f"hl-real cycle failed: {e}", file=sys.stderr)
            if args.once:
                break
            time.sleep(cfg.HL_REAL_TAIL_SECONDS)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
