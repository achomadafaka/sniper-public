#!/usr/bin/env python3
"""What would the Solana chase-cap refusals have made, and did the market move inside the source's slot?

Pre-registered in HANDOFF.md 2026-09-29 06:45Z (Gamma) before any outcome in this population except
CARDANO e3c706bf. Read-only: the ledger and data/memecoin_refusal_captures.db; `inslot` also reads
the chain on the research key (funded_exit_truth.rpc), gated on a flat Solana book before every call.

Population: Solana attempts refused "exact quote is N% above source price (cap N%)", one per origin.
Grade (as scripts/research/cap_refusal_outcomes.py): the exact capture buy of the clip ~1-3 s after
the refusal, sells of the same tokens every ~25 s; TP at the first sell >= +25%, stop at the first
<= -35%, else the sell nearest +300 s. Bands on the gate's own gap_pct (chase_quote_confirmation).

In-slot: for origins that carry the source tx signature, the source's slot is read from chain and
the mint's trades after the source in that slot are replayed on the source's pool. The end-of-slot
spot over the source's post-trade spot is (B_post / B_end)^2 on the pool's base vault (constant
product; fees accrue on the input side, so the base vault is the reserve) or the ratio of pump
TradeEvent virtual reserves. The quote vs the end-of-slot price is then
(1 + gap) * (reference / source_post) / (end / source_post), where reference / source_post is 1 on
the post_trade basis and avg / post from the source's own trade on the avg_fill basis. Other
venues (CLMM, DLMM, split routes) stay unknown.

    python3 scripts/research/chase_refusal_outcomes.py report [--since 2026-09-21T00:00:00]
    python3 scripts/research/chase_refusal_outcomes.py inslot --out logs/acceptance/chase_inslot.jsonl
    python3 scripts/research/chase_refusal_outcomes.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
from cap_refusal_outcomes import CAPTURES, LEDGER, capture_obs, grade, mark, ro, summary  # noqa: E402

POST_TRADE_LIVE = "2026-09-28T15:01:04"
CHASE = re.compile(r"^exact quote is \+?([0-9.]+)% above source price \(cap ([0-9.]+)%\)")
BANDS = ((0, 30), (30, 50), (50, 100), (100, 1e9))
DUP_S = 120.0


def band(gap):
    return next("%g-%s" % (lo, "" if hi > 1e8 else "%g" % hi) for lo, hi in BANDS if lo <= gap < hi)


def source_sig(origin):
    """The source tx signature carried by an origin id, else None (J7 uuid origins carry none)."""
    parts = origin.split(":")
    if parts[0] == "onchain" and len(parts) == 2:
        return parts[1]
    if parts[0] == "pump" and len(parts) > 1:
        return parts[1] or None
    if parts[0] == "swap" and len(parts) > 2 and parts[1] == "raybot":
        return parts[2] or None
    return None


def refusals(since):
    out = {}
    with contextlib.closing(ro(LEDGER)) as c:
        for r in c.execute(
                "SELECT attempt_id, origin_event_id, mint, decision_started_at, terminal_reason, signal_source, "
                "source_handle, trace_json FROM memecoin_entry_attempts WHERE network_id='solana' "
                "AND decision_started_at>=? AND terminal_status='refused' AND terminal_reason LIKE 'exact quote is %' "
                "ORDER BY decision_started_at", (since,)):
            m = CHASE.match(r["terminal_reason"] or "")
            if not m or r["origin_event_id"] in out:
                continue
            ev = next((s for s in json.loads(r["trace_json"] or "{}").get("substage_events") or []
                       if s.get("name") == "chase_quote_confirmation"), {})
            out[r["origin_event_id"]] = {
                "attempt": r["attempt_id"], "origin": r["origin_event_id"], "mint": r["mint"],
                "at": r["decision_started_at"], "lane": r["signal_source"], "handle": r["source_handle"],
                "gap": float(ev.get("gap_pct", m.group(1))), "cap": float(ev.get("cap_pct", m.group(2))),
                "basis": ev.get("reference_basis") or "avg_fill", "note": ev.get("reference_note"),
                "ref": ev.get("reference_price"), "src": ev.get("source_price"), "quote": ev.get("quote_price")}
    rows, seen = list(out.values()), {}
    for r in rows:   # the same source trade reached both lanes (J7 and on-chain): flag the second
        t = datetime.fromisoformat(r["at"])
        prev = seen.get((r["mint"], r["handle"]))
        r["dup"] = prev is not None and (t - prev).total_seconds() <= DUP_S
        seen[(r["mint"], r["handle"])] = t
    return rows


# ---- in-slot replay -------------------------------------------------------------------------

def _keys(tx):
    ld = tx["meta"].get("loadedAddresses") or {}
    return tx["transaction"]["message"]["accountKeys"] + ld.get("writable", []) + ld.get("readonly", [])


def _balances(tx, mint=None):
    """{account: (owner, mint, pre, post)} over token balances."""
    kk, out = _keys(tx), {}
    for side in ("preTokenBalances", "postTokenBalances"):
        for b in tx["meta"].get(side) or []:
            if mint and b.get("mint") != mint:
                continue
            acct = kk[b["accountIndex"]]
            o, m_, pre, post = out.get(acct, (b.get("owner"), b.get("mint"), 0, 0))
            amt = int(b["uiTokenAmount"]["amount"])
            out[acct] = (o, m_, amt if side == "preTokenBalances" else pre, amt if side == "postTokenBalances" else post)
    return out


def curve_event(tx, mint):
    """(virtual_token_reserves, virtual_sol_reserves, sol, tokens, is_buy) from a pump TradeEvent, else None."""
    import funded_exit_truth as fet
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    for _i, line in program_data_logs(tx["meta"].get("logMessages") or [], fet.PUMP):
        try:
            if anchor_event_bytes(line)[:8] != fet.TRADE_DISC:
                continue
            e = parse_trade_event(line)
        except Exception:   # noqa: BLE001 - a foreign Program data line is not a TradeEvent
            continue
        if e.get("mint") == mint and e.get("virtual_token_reserves"):
            return e
    return None


def base_vault(src_tx, mint):
    """The account whose `mint` balance fell the most in the source tx (the pool paying out), or None."""
    best = None
    for acct, (owner, _m, pre, post) in _balances(src_tx, mint).items():
        d = post - pre
        if d < 0 and (best is None or d < best[1]):
            best = (acct, d, pre, post)
    return best


def slot_replay(src_tx, window, mint):
    """{'venue', 'post_over_avg', 'end_over_post', 'after': [...]} for the source's slot, or {'venue': 'unknown'}."""
    sig = src_tx["transaction"]["signatures"][0]
    S = src_tx["slot"]
    idx = next((t.get("transactionIndex") for t in window if t["transaction"]["signatures"][0] == sig), None)
    after = [t for t in window if t["slot"] == S and idx is not None and (t.get("transactionIndex") or 0) > idx]
    ev = curve_event(src_tx, mint)
    rows = []
    if ev and ev.get("is_buy"):
        v_post = ev["virtual_token_reserves"]
        # constant product on the virtual reserves: post/avg = vT_pre / vT_post. Unit-free, so a curve
        # quoted in another token (sol_amount 0 in the event) needs no quote amount.
        post_over_avg = (v_post + ev["token_amount"]) / v_post
        end = v_post
        for t in after:
            e = curve_event(t, mint)
            if e:
                rows.append({"idx": t.get("transactionIndex"), "signer": _keys(t)[0], "buy": bool(e["is_buy"]),
                             "quote": e["sol_amount"] / 1e9})
                end = e["virtual_token_reserves"]
        # curve spot = vS/vT with vS*vT = k: spot ratio = (vT_post / vT_end)^2
        return {"venue": "pump_curve", "post_over_avg": post_over_avg, "end_over_post": (v_post / end) ** 2,
                "after": rows, "slot": S, "idx": idx}
    bv = base_vault(src_tx, mint)
    if bv is None:
        return {"venue": "unknown", "slot": S, "idx": idx}
    acct, _d, pre, post = bv
    if pre <= 0 or post <= 0:
        return {"venue": "unknown", "slot": S, "idx": idx}
    end = post
    for t in after:
        b = _balances(t, mint).get(acct)
        if b is None:
            continue
        rows.append({"idx": t.get("transactionIndex"), "signer": _keys(t)[0], "buy": b[3] < b[2],
                     "base": (b[2] - b[3])})
        end = b[3]
    # constant product: post/avg = B_pre/B_post; end/post = (B_post/B_end)^2
    return {"venue": "pool", "vault": acct, "post_over_avg": pre / post, "end_over_post": (post / end) ** 2,
            "after": rows, "slot": S, "idx": idx}


def quote_over_post(r, rep):
    """The quote over the source's own post-trade price, or None."""
    if rep.get("venue") not in ("pool", "pump_curve") or not rep.get("post_over_avg"):
        return None
    ratio = (r["ref"] / r["src"]) if r.get("ref") and r.get("src") else (
        rep["post_over_avg"] if r["basis"].startswith("post_trade") else 1.0)
    return (1.0 + r["gap"] / 100.0) * ratio / rep["post_over_avg"]


def where_moved(r, rep):
    """SECONDARY (not pre-registered): which move put the quote over the cap.
    own_impact: the quote is within the cap of the source's own post-trade price;
    others_in_slot: over it, but within the cap of the end-of-source-slot price;
    after_slot: over the cap even against the end of the source's slot."""
    qp, qe = quote_over_post(r, rep), quote_over_end(r, rep)
    if qp is None or qe is None:
        return "unknown"
    if (qp - 1.0) * 100.0 <= r["cap"]:
        return "own_impact"
    return "others_in_slot" if (qe - 1.0) * 100.0 <= r["cap"] else "after_slot"


def quote_over_end(r, rep):
    """The quote over the end-of-source-slot price, or None."""
    if rep.get("venue") not in ("pool", "pump_curve") or not rep.get("end_over_post"):
        return None
    # The gate's reference over the source's average, over the pool's own post/avg: about 1 on the
    # post_trade basis, 1 / (post/avg) on avg_fill, 2.0 / (post/avg) when the reference was clamped.
    ratio = (r["ref"] / r["src"]) if r.get("ref") and r.get("src") else (
        rep["post_over_avg"] if r["basis"].startswith("post_trade") else 1.0)
    ref_over_post = ratio / rep["post_over_avg"]
    return (1.0 + r["gap"] / 100.0) * ref_over_post / rep["end_over_post"]


def run_inslot(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import wallet_crowd_dump_scoreboard as wcs
    from slot_minout_grading import wait_flat
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    fet.SPACING_S = 0.5
    real_rpc = fet.rpc

    def gated(method, params):
        wait_flat(lambda: get_readonly_connection(str(REPO / "sniper_tracker.db")), max_wait_s=600)
        return real_rpc(method, params)
    fet.rpc = gated
    done = set()
    out = Path(args.out)
    if out.exists():
        done = {json.loads(line)["origin"] for line in out.read_text().splitlines() if line.strip()}
    rows = [r for r in refusals(args.since) if r["origin"] not in done]
    print("in-slot: %d to read (%d done)" % (len(rows), len(done)), flush=True)
    with out.open("a") as fh:
        for r in rows:
            sig = source_sig(r["origin"])
            rec = {"origin": r["origin"], "sig": sig}
            if sig is None:
                rec["venue"] = "no_source_sig"
            else:
                try:
                    src = dfx.get_tx(sig)
                    if not src:
                        rec["venue"] = "source_tx_missing"
                    else:
                        win, complete = wcs.gtfa(r["mint"], {"slot": {"gte": src["slot"], "lte": src["slot"]}},
                                                 limit=100, pages=3)
                        rep = slot_replay(src, win, r["mint"])
                        rep["complete"] = complete
                        rec.update(rep)
                except fet.Stop as exc:
                    print("stopped: %s" % exc, flush=True)
                    break
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(r["origin"][:40], rec.get("venue"), rec.get("end_over_post"), flush=True)


def capture_drift(conn, r):
    """The capture's exact buy price over our refused quote price (> 1 = the capture paid more)."""
    row = conn.execute("SELECT quote_price FROM memecoin_refusal_captures WHERE network_id='solana' AND "
                       "origin_event_id=? AND quote_side='buy' AND quality='ok' ORDER BY age_s LIMIT 1",
                       (r["origin"],)).fetchone()
    return (row["quote_price"] / r["quote"]) if row and row["quote_price"] and r.get("quote") else None


def report(args):
    refs = refusals(args.since)
    ins = {}
    p = Path(args.inslot)
    if p.exists():
        ins = {d["origin"]: d for d in map(json.loads, filter(str.strip, p.read_text().splitlines()))}
    with contextlib.closing(ro(CAPTURES)) as cap:
        for r in refs:
            r["clip"], r["obs"] = capture_obs(cap, r["origin"])
            r["grade"], r["how"] = grade(r["obs"]) if r["clip"] and r["obs"] else (None, "no_capture")
            rep = ins.get(r["origin"])
            r["q_end"] = quote_over_end(r, rep) if rep else None
            r["where"] = where_moved(r, rep) if rep else "no_read"
            r["cap_drift"] = capture_drift(cap, r)
            r["inslot"] = ("no_read" if rep is None else
                           "unknown" if r["q_end"] is None else
                           "explains" if (r["q_end"] - 1.0) * 100.0 <= r["cap"] else "not_explained")
    for label, rs in (("ALL since %s" % args.since, refs),
                      ("POST_TRADE live (>= %s)" % POST_TRADE_LIVE, [r for r in refs if r["at"] >= POST_TRADE_LIVE])):
        print("\n== %s: %d refusals; lanes %s; basis %s; grade %s" % (
            label, len(rs), dict(collections.Counter(r["lane"] for r in rs)),
            dict(collections.Counter(r["basis"] for r in rs)), dict(collections.Counter(r["how"] for r in rs))))
        dec = [r for r in rs if r["grade"] is not None]
        summary("all graded", [(r["mint"], r["grade"], r["grade"] * r["clip"]) for r in dec])
        summary("all graded, one per source trade", [(r["mint"], r["grade"], r["grade"] * r["clip"])
                                                     for r in dec if not r["dup"]])
        for lo, hi in BANDS:
            b = [r for r in dec if lo <= r["gap"] < hi]
            summary("gap %s%%" % band(lo), [(r["mint"], r["grade"], r["grade"] * r["clip"]) for r in b])
        for h in (60.0, 300.0):
            ms = [(r["mint"], v, v * r["clip"]) for r in rs if r["clip"] for v in [mark(r["obs"], h)] if v is not None]
            summary("plain mark +%ds" % h, ms)
        print("  in-slot split: %s" % dict(collections.Counter(r["inslot"] for r in rs)))
        for k in ("explains", "not_explained", "unknown", "no_read"):
            b = [r for r in dec if r["inslot"] == k]
            summary("in-slot %s" % k, [(r["mint"], r["grade"], r["grade"] * r["clip"]) for r in b])
        print("  SECONDARY (not pre-registered) where the move over the cap happened: %s" % dict(
            collections.Counter(r["where"] for r in rs)))
        for k in ("own_impact", "others_in_slot", "after_slot", "unknown"):
            b = [r for r in dec if r["where"] == k]
            summary("moved: %s" % k, [(r["mint"], r["grade"], r["grade"] * r["clip"]) for r in b])
        d = sorted(r["cap_drift"] for r in rs if r["cap_drift"] is not None)
        if d:
            print("  capture buy price / our refused quote: n=%d p25 %.3f p50 %.3f p75 %.3f" % (
                len(d), d[len(d) // 4], d[len(d) // 2], d[3 * len(d) // 4]))
    if args.rows:
        for r in refs:
            print("%s %-15s %-14s gap %+6.1f cap %4.0f %-10s q/end %s %-28s %s %s" % (
                r["at"][:19], r["lane"], (r["handle"] or "")[:14], r["gap"], r["cap"], r["basis"][:10],
                "%.3f" % r["q_end"] if r["q_end"] else "  -  ", r["inslot"] + "/" + r["where"], r["how"],
                "%+.1f%%" % (100 * r["grade"]) if r["grade"] is not None else ""))


def selftest():
    assert source_sig("onchain:5ivf") == "5ivf" and source_sig("pump:SIG:ADDR:buy:MINT") == "SIG"
    assert source_sig("swap:raybot:TX:TRADER:MINT") == "TX" and source_sig("swap:1234-uuid") is None
    assert source_sig("onchain:solana:TX:MINT") is None       # EVM-shaped on-chain origins are not Solana sigs
    assert band(35) == "30-50" and band(64) == "50-100" and band(150) == "100-" and band(12) == "0-30"
    assert CHASE.match("exact quote is +64% above source price (cap 30%)").groups() == ("64", "30")

    def tx(sig, slot, idx, pre, post, vault="V", mint="M"):
        return {"slot": slot, "transactionIndex": idx,
                "transaction": {"signatures": [sig], "message": {"accountKeys": ["SIGNER_" + sig, vault]}},
                "meta": {"logMessages": [], "loadedAddresses": {},
                         "preTokenBalances": [{"accountIndex": 1, "owner": "POOL", "mint": mint,
                                               "uiTokenAmount": {"amount": str(pre), "decimals": 6}}],
                         "postTokenBalances": [{"accountIndex": 1, "owner": "POOL", "mint": mint,
                                                "uiTokenAmount": {"amount": str(post), "decimals": 6}}]}}
    # CARDANO shape: source takes 194.2M -> 178.3M; a later buy in the slot -> 136.2M; next slot ignored
    src = tx("S", 10, 183, 194195933, 178252430)
    win = [src, tx("A", 10, 449, 178252430, 138412381), tx("B", 10, 997, 138412381, 136187068),
           tx("C", 11, 5, 136187068, 100000000), tx("P", 10, 100, 200000000, 194195933)]
    rep = slot_replay(src, win, "M")
    assert rep["venue"] == "pool" and len(rep["after"]) == 2 and rep["idx"] == 183
    assert abs(rep["post_over_avg"] - 194195933 / 178252430) < 1e-12
    assert abs(rep["end_over_post"] - (178252430 / 136187068) ** 2) < 1e-12      # 1.713
    r = {"gap": 63.8, "cap": 30.0, "basis": "post_trade", "ref": 1.0896, "src": 1.0}   # e3c706bf
    q = quote_over_end(r, rep)
    assert 0.95 < q < 0.96                                     # the quote sits ~4% under the end-of-slot price
    r2 = dict(r, basis="avg_fill", ref=1.0)                    # avg basis: the gap also holds the source's impact
    assert quote_over_end(r2, rep) < q
    assert quote_over_end(r, {"venue": "unknown"}) is None
    assert where_moved(r, rep) == "others_in_slot"            # CARDANO: +64% over post, -4% vs end of slot
    assert where_moved(dict(r2, gap=35.0), rep) == "own_impact"  # avg basis: 1.35 / 1.0894 = +24% over post
    assert where_moved(dict(r, gap=150.0), rep) == "after_slot"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="report", choices=("report", "inslot"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-21T00:00:00")
    ap.add_argument("--inslot", default=str(REPO / "logs" / "acceptance" / "chase_inslot.jsonl"))
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "chase_inslot.jsonl"))
    ap.add_argument("--rows", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run_inslot(a) if a.cmd == "inslot" else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
