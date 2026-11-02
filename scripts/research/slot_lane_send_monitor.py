#!/usr/bin/env python3
"""Read-only monitor for LIVE slot-lane sends and refusals (Gamma, 2026-09-26).

For every memecoin_pending_entries row with signal_source='slot_lane' and a tx_sig past
--after-id, print one line: S (the source signature's slot, from origin_event_id
'slotlane:<sig>'), our landed slot, landed minus S, the guard verdict (which Lighthouse assert
failed, if any), the venue program our tx called, the attempt row's stage times, and which
submission leg accepted first. At exit, print a histogram of the lane's refusals parsed from
the runner log since --refusal-since, because the lane writes no ledger row for a refusal.

Chain reads: getTransaction at `confirmed` with maxSupportedTransactionVersion 1 (bot routers
send version-1 transactions; version 0 refuses them with -32015), on the FIRST entry of
MEMECOIN_RESEARCH_HELIUS_RPC_URLS, two calls per send, >= 1.1 s apart. The JSON-RPC `error`
field is read; a null result means result=null only. Never the funded custody RPC.

    python3 scripts/research/slot_lane_send_monitor.py --after-id 4019 \
        --refusal-since 2026-09-26T21:30:25Z --duration 1740
    python3 scripts/research/slot_lane_send_monitor.py --selftest
"""
import argparse
import collections
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPO = "/home/bigpapi/Desktop/Sniper"
DB = os.path.join(REPO, "sniper_tracker.db")
LOG = os.path.join(REPO, "logs", "memecoin_runner.log")
LIGHTHOUSE = "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"
VENUES = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_curve",
          "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap"}
TERMINAL = {"confirmed", "failed"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Trailing fields are optional: 8698bd92 (2026-09-26 22:52Z) appended "recv-S=0 land-S=None", and
# an end-anchored "head-S=\S+$" then matched nothing, so every REFUSALS line read n=0 for 1.5 h.
LINE = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) (\S+) S=(\S+) -> (\S+) ?(.*?) ms=\S+ head-S=\S+(?: .*)?$")


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def normalize_reason(reason):
    """Collapse the per-buy numbers so refusals group by family."""
    r = re.sub(r"\$[\d,]+(?:\.\d+)?", "$N", reason or "")
    return re.sub(r"\d+(?:\.\d+)?%", "N%", r).strip()


def guard_verdict(err, lighthouse_ixs):
    """Map the tx error to the slot guard. The first Lighthouse ix asserts the MIN slot (fails =
    landed too early), the second the MAX slot (fails = too late). Custom 6001 is shared with
    Jupiter, so only the instruction index decides it."""
    if err is None:
        return "pass"
    ie = err.get("InstructionError") if isinstance(err, dict) else None
    if not ie:
        return "tx_error"
    ix = ie[0]
    if len(lighthouse_ixs) == 2 and ix == lighthouse_ixs[0]:
        return "too_early"
    if len(lighthouse_ixs) == 2 and ix == lighthouse_ixs[1]:
        return "too_late"
    return "other_ix_%s" % ix


def pick_research_url(env):
    """First MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, refused when its key is also a funded pool
    key or the watcher's key (the live slot-lane signal socket). Overlap is judged by key_id, so a
    key on another host alias still matches, and the error names sha8 key ids only, never a URL.
    The singular MEMECOIN_RESEARCH_HELIUS_RPC_URL is deliberately not a fallback (quota spent)."""
    sys.path.insert(0, REPO)
    from sniper.services.memecoin_research_rpc import funded_overlap
    urls = [u.strip() for u in (env.get("MEMECOIN_RESEARCH_HELIUS_RPC_URLS") or "").split(",") if u.strip()]
    if not urls:
        raise ValueError("MEMECOIN_RESEARCH_HELIUS_RPC_URLS is unset")
    watcher = {"MEMECOIN_SOLANA_RPC_URL": env.get("ONCHAIN_WATCH_HELIUS_RPC_URL") or ""}
    overlap = sorted(set(funded_overlap(urls[:1], env)) | set(funded_overlap(urls[:1], watcher)))
    if overlap:
        raise ValueError("research endpoint shares key %s with a funded or watcher endpoint; refusing"
                         % ",".join(overlap))
    return urls[0]


def rpc_url():
    sys.path.insert(0, REPO)
    from sniper.util.envfiles import load_env_files
    load_env_files(REPO)
    try:
        return pick_research_url(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc))


def get_tx(url, sig, tries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [
        sig, {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 1}]}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception as exc:  # transport: report, do not guess
            if i == tries - 1:
                return {"read_error": "%s: %s" % (type(exc).__name__, str(exc)[:120])}
            time.sleep(2.0)
            continue
        if r.get("error"):
            return {"read_error": json.dumps(r["error"])[:200]}
        if r.get("result") is None:
            if i == tries - 1:
                return {"read_error": "result null after %d tries" % tries}
            time.sleep(2.0)
            continue
        res = r["result"]
        keys = res["transaction"]["message"]["accountKeys"]
        ixs = res["transaction"]["message"]["instructions"]
        lh = [n for n, ix in enumerate(ixs) if keys[ix["programIdIndex"]] == LIGHTHOUSE]
        venue = sorted({VENUES[k] for k in keys if k in VENUES}) or ["other"]
        meta, payer = res["meta"], keys[0]
        usdc = lambda side: sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get(side) or []
                                if b.get("mint") == USDC and b.get("owner") == payer)
        return {"slot": res["slot"], "err": meta.get("err"), "lighthouse_ixs": lh,
                "venue": "+".join(venue), "fee": meta.get("fee"), "version": res.get("version"),
                "usdc_spent": (usdc("preTokenBalances") - usdc("postTokenBalances")) / 1e6,
                "owner_native_delta": meta["postBalances"][0] - meta["preBalances"][0]}
    return {"read_error": "unreachable"}


def describe(conn, url, row):
    src_sig = (row["origin_event_id"] or "").split("slotlane:", 1)[-1]
    a = conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
    legs = conn.execute("SELECT provider_id, state, latency_ms, reason_code FROM memecoin_submission_attempts "
                        "WHERE owner_kind='reservation' AND owner_id=? ORDER BY id", (str(row["id"]),)).fetchall()
    pos = conn.execute("SELECT id, entry_price, entry_notional, is_open FROM memecoin_positions WHERE attempt_id=?",
                       (row["attempt_id"],)).fetchone()
    src = get_tx(url, src_sig)
    time.sleep(1.1)
    ours = get_tx(url, row["tx_sig"])
    time.sleep(1.1)
    out = {"pending": row["id"], "attempt": (row["attempt_id"] or "")[:8], "mint": row["mint"],
           "status": row["status"], "reason": row["resolution_reason"],
           "S": src.get("slot"), "landed": ours.get("slot"),
           "landed_minus_S": (ours["slot"] - src["slot"]) if "slot" in src and "slot" in ours else None,
           "guard": guard_verdict(ours.get("err"), ours.get("lighthouse_ixs") or []) if "slot" in ours else None,
           "venue": ours.get("venue"), "tx_err": ours.get("err"),
           "read_errors": {k: v["read_error"] for k, v in (("src", src), ("ours", ours)) if "read_error" in v} or None}
    if a:
        out["stages_ms"] = {k: round(a[k + "_ms"], 1) for k in (
            "market_data", "reservation", "signing", "transaction_submitted", "chain_confirmation",
            "balance_delta", "persistence", "total") if a[k + "_ms"] is not None}
        out["decision_at"] = a["decision_started_at"]
        out["source_price"] = a["source_price"]
    accepted = [l for l in legs if l["state"] == "accepted"]
    out["first_accept"] = min(accepted, key=lambda l: l["latency_ms"] or 9e9)["provider_id"] if accepted else None
    out["legs"] = ["%s:%s:%.0f" % (l["provider_id"].split(":")[-1], l["state"], l["latency_ms"] or -1) for l in legs]
    if pos:
        out["position"] = pos["id"]
        out["entry_price"] = pos["entry_price"]
        out["entry_notional"] = pos["entry_notional"]
        credit = surplus_credit(a["trace_json"] if a else None)
        out["sol_surplus_credit"] = credit
        if credit and "usdc_spent" in ours:
            out["notional_check_usd"] = notional_check(pos["entry_notional"], ours["usdc_spent"],
                                                       ours["owner_native_delta"], credit)
        if a and a["source_price"] and pos["entry_price"]:
            out["entry_vs_source_pct"] = round((pos["entry_price"] / a["source_price"] - 1) * 100, 2)
    return out


def surplus_credit(trace_json):
    """The sol_surplus_credit substage from the attempt trace, or None when absent."""
    try:
        events = json.loads(trace_json or "{}").get("substage_events") or []
    except ValueError:
        return None
    hit = [e for e in events if e.get("name") == "sol_surplus_credit"]
    return {k: hit[-1].get(k) for k in ("outcome", "credit_usd", "usdc_spent", "surplus_lamports", "sol_usd")} if hit else None


def notional_check(entry_notional, chain_usdc, chain_native_delta, credit):
    """entry_notional minus what the chain receipt says it should be, in USD. Expected = the chain
    USDC spend less the owner's native SOL gain at the lane's sol_usd, only when credited."""
    expected = chain_usdc
    if credit.get("outcome") == "credited" and credit.get("sol_usd"):
        expected -= max(0, chain_native_delta) / 1e9 * float(credit["sol_usd"])
    return round(float(entry_notional) - expected, 4)


def refusal_histogram(since, until):
    """Counts of slot-lane outcomes in the runner log in [since, until), by status + reason family."""
    hist = collections.Counter()
    with open(LOG, "r", errors="replace") as fh:
        for line in fh:
            if "slot lane" not in line:
                continue
            m = LINE.match(line.rstrip("\n"))
            if not m:
                continue
            ts = parse_ts(m.group(1))
            if since <= ts < until:
                hist["%s %s" % (m.group(5), normalize_reason(m.group(6)))] += 1
    return hist


def follow(args):
    url = rpc_url()
    since = parse_ts(args.refusal_since)
    deadline = time.monotonic() + args.duration
    cursor = args.after_id
    first_seen = {}
    reserved = {}   # pending id -> reserved_at, for rows still holding their reservation
    while time.monotonic() < deadline:
        try:
            with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, mint, attempt_id, origin_event_id, tx_sig, status, resolution_reason "
                    "FROM memecoin_pending_entries WHERE signal_source='slot_lane' AND tx_sig IS NOT NULL "
                    "AND id > ? ORDER BY id", (cursor,)).fetchall()
                now = datetime.now(timezone.utc)
                for r in conn.execute(
                        "SELECT id, status, reserved_at, tx_sig, resolution_reason FROM memecoin_pending_entries "
                        "WHERE signal_source='slot_lane' AND (id > ? OR id IN (%s))" % (",".join(["?"] * len(reserved)) or "NULL"),
                        (cursor, *reserved)).fetchall():
                    if r["status"] not in TERMINAL and r["id"] not in reserved:
                        reserved[r["id"]] = r["reserved_at"]
                        print("RESERVED_OPEN pending=%s reserved_at=%s tx_sig=%s" % (r["id"], r["reserved_at"], bool(r["tx_sig"])), flush=True)
                    elif r["status"] in TERMINAL and r["id"] in reserved:
                        held = (now - parse_ts(reserved.pop(r["id"]))).total_seconds()
                        print("RESERVED_CLOSED pending=%s status=%s reason=%s held_s<=%.1f" % (
                            r["id"], r["status"], r["resolution_reason"], held), flush=True)
                for row in rows:
                    seen = first_seen.setdefault(row["id"], time.monotonic())
                    if row["status"] not in TERMINAL and time.monotonic() - seen < 120:
                        break  # keep order: wait for this send to resolve
                    print("SEND " + json.dumps(describe(conn, url, row), default=str), flush=True)
                    cursor = row["id"]
        except Exception as exc:
            print("MONITOR_ERROR %s: %s" % (type(exc).__name__, str(exc)[:200]), flush=True)
        time.sleep(2.0)
    now = datetime.now(timezone.utc)
    hist = refusal_histogram(since, now)
    print("REFUSALS %s..%s n=%d %s cursor=%d" % (
        since.strftime("%H:%M:%SZ"), now.strftime("%H:%M:%SZ"), sum(hist.values()),
        json.dumps(dict(hist.most_common())), cursor), flush=True)


def selftest():
    assert normalize_reason("source buy $30 (0.094%) under mcap floor 0.500% ($158)") == \
        "source buy $N (N%) under mcap floor N% ($N)"
    assert normalize_reason("mcap $18408070 over ceiling $250000") == "mcap $N over ceiling $N"
    # The first live send failed ix 3 with the second Lighthouse assert: too late, not too early.
    assert guard_verdict({"InstructionError": [3, {"Custom": 6001}]}, [2, 3]) == "too_late"
    assert guard_verdict({"InstructionError": [2, {"Custom": 6001}]}, [2, 3]) == "too_early"
    assert guard_verdict(None, [2, 3]) == "pass"
    # Jupiter shares custom 6001: an error on another ix must not read as the guard.
    assert guard_verdict({"InstructionError": [5, {"Custom": 6001}]}, [2, 3]) == "other_ix_5"
    m = LINE.match("2026-09-26T21:34:07.415299+00:00 [memecoin] slot lane notdecu Gm9gqiHu S=450796749 "
                   "-> rejected source buy $30 (0.094%) under mcap floor 0.500% ($158) ms=0.0 head-S=None")
    assert m and m.group(5) == "rejected" and m.group(4) == "450796749"
    m = LINE.match("2026-09-26T21:33:40.136738+00:00 [memecoin] slot lane notdecu Gm9gqiHu S=450796647 "
                   "-> filled  ms=452.5 head-S=None")
    assert m and m.group(5) == "filled" and m.group(6) == ""
    m = LINE.match("2026-09-27T00:21:52.145614+00:00 [memecoin] slot lane untaxxable AcjCTKkT S=450834242 "
                   "-> rejected slot_lane_scaled_clip ms=4.5 head-S=None recv-S=0 land-S=None")
    assert m and m.group(5) == "rejected" and m.group(6) == "slot_lane_scaled_clip", m
    # 65813 on 2026-09-26: USDC 170.5, owner native +19,214,340 lamports at 121.547 $/SOL.
    credit = {"outcome": "credited", "sol_usd": 121.547}
    assert notional_check(170.5 - 0.019214340 * 121.547, 170.5, 19214340, credit) == 0.0
    assert notional_check(170.5, 170.5, 19214340, credit) > 2.3        # uncredited booking shows
    assert notional_check(170.5, 170.5, 19214340, {"outcome": "no_surplus"}) == 0.0
    assert surplus_credit(json.dumps({"substage_events": [{"name": "sol_surplus_credit", "outcome": "credited",
                                                           "credit_usd": 2.3}]}))["credit_usd"] == 2.3
    assert surplus_credit(None) is None
    # Credential isolation: a research key shared with a funded pool member on another host alias,
    # or with the watcher key, refuses; the error carries the sha8 id and never the key itself.
    research, other = "https://mainnet.helius-rpc.com/?api-key=RESEARCHKEY", "https://x.example/?api-key=OTHERKEY"
    ok = {"MEMECOIN_RESEARCH_HELIUS_RPC_URLS": research + "," + other,
          "MEMECOIN_SOLANA_RPC_URL": other, "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://api.mainnet-beta.solana.com"}
    assert pick_research_url(ok) == research
    for bad in ({**ok, "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://beta.helius-rpc.com/?api-key=RESEARCHKEY"},
                {**ok, "ONCHAIN_WATCH_HELIUS_RPC_URL": "https://rpc.helius.xyz/?api-key=RESEARCHKEY"}):
        try:
            pick_research_url(bad)
            raise AssertionError("shared key not refused")
        except ValueError as exc:
            assert "RESEARCHKEY" not in str(exc) and "helius" not in str(exc), exc
    print("selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--after-id", type=int, default=0)
    p.add_argument("--refusal-since", default="2026-09-26T21:30:25Z")
    p.add_argument("--duration", type=float, default=1740.0)
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    selftest() if a.selftest else follow(a)
