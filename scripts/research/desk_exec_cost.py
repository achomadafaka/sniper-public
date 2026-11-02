#!/usr/bin/env python3
"""Measured execution cost of real desk fills beyond the tape model (descriptive). usage: [--limit N]
For every desk fill signature (entry, bracket_exit, manual_close): getTransaction (research Solana RPC, <= 3 req/s, stops on the first error), meta.fee (lamports: base + priority), the signer's SOL balance delta,
and where the tape holds the swap (curve), the tape's gross SOL amount: entry extra = -delta - tape_spend; exit shortfall = tape_proceeds - delta (positive = the chain cost more than the tape model).
Rent for new token accounts and returned rent are inside the residual and are not separated (said in the read)."""
import json, os, sqlite3, sys, time, urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts" / "research"))
FX0 = 104.329875


def url():
    for line in open(ROOT / ".env"):
        if line.startswith("MEMECOIN_RESEARCH_HELIUS_RPC_URL="):
            return line.split("=", 1)[1].strip().strip('"').split(",")[0]
    raise SystemExit("no research Solana RPC url")


def tx(u, sig):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}]}).encode()
    req = urllib.request.Request(u, body, {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    out = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if "error" in out or "result" not in out:
        raise RuntimeError(str(out.get("error"))[:120])
    return out["result"]


def tape_row(mint, sig, near):
    for d in (0, -1, 1):
        day = time.strftime("%Y-%m-%d", time.gmtime(near + d * 86400))
        p = ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)
        if not p.exists():
            continue
        con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        r = con.execute("select is_buy, quote_atomic, token_atomic, fee_bps, creator_fee_bps from swaps where signature=? and mint=?", (sig, mint)).fetchall()
        con.close()
        if len(r) == 1:
            return r[0]
    return None


def main(limit=1000):
    db = sqlite3.connect("file:%s?mode=ro" % (ROOT / "sniper_tracker.db"), uri=True)
    rows = db.execute("select f.id, f.kind, f.tx_sig, p.mint, f.created_at from memecoin_fills f join memecoin_positions p on p.id=f.position_id "
                      "where p.signal_source='desk' and f.tx_sig is not null and f.tx_sig<>'' and f.kind in ('entry','bracket_exit','manual_close') order by f.id").fetchall()[:int(limit)]
    u = url(); out = []
    for fid, kind, sig, mint, at in rows:
        try:
            r = tx(u, sig)
        except Exception as exc:
            print("stop at fill", fid, "error:", str(exc)[:100]); break
        time.sleep(0.34)
        if r is None:
            out.append({"fid": fid, "kind": kind, "err": "not_found"}); continue
        meta = r["meta"]; keys = r["transaction"]["message"]["accountKeys"]
        delta = sum(meta["postBalances"][i] - meta["preBalances"][i] for i, k in enumerate(keys) if (k.get("signer") if isinstance(k, dict) else False))     # all signer wallets together (fee payer + trader)
        t = time.mktime(time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        tr = tape_row(mint, sig, t)
        rent = 0
        for grp in meta.get("innerInstructions") or []:
            for ins in grp.get("instructions", []):
                pi = ins.get("parsed")
                if isinstance(pi, dict) and pi.get("type") == "createAccount":
                    rent += int(pi["info"].get("lamports", 0))
        row = {"fid": fid, "kind": kind, "fee": meta["fee"], "delta": delta, "rent_created": rent, "failed": meta.get("err") is not None}
        if tr:
            is_buy, qa, ta, fb, fc = tr
            gross = int(qa)
            row["tape_quote"] = gross
            # NOTE: the signer-SOL delta is NOT comparable to the tape amounts (the swaps are funded/paid through token accounts), so no tape residual is computed.
        out.append(row)
    ok = [o for o in out if "fee" in o and not o["failed"]]
    print("fills read %d of %d, readable %d, failed txs %d" % (len(out), len(rows), len(ok), sum(1 for o in out if o.get("failed"))))
    for kind in ("entry", "bracket_exit", "manual_close"):
        g = [o for o in ok if o["kind"] == kind]
        if not g:
            continue
        fee = np.array([o["fee"] for o in g]) / 1e9
        print("%-13s n %2d | meta.fee SOL: median %.6f p90 %.6f max %.6f (USD@FX0 median %.3f)" % (kind, len(g), np.median(fee), np.percentile(fee, 90), fee.max(), np.median(fee) * FX0))
        rent = np.array([o["rent_created"] for o in g]) / 1e9
        print("              rent lamports for accounts created in the tx (SOL): median %.6f p90 %.6f (n with rent > 0: %d); fee+rent USD@FX0 median %.3f = %.2f%% of a 25 USD clip" % (
            np.median(rent), np.percentile(rent, 90), int((rent > 0).sum()), (np.median(fee) + np.median(rent)) * FX0, 100 * (np.median(fee) + np.median(rent)) * FX0 / 25))
        ex = []
        print("              mean fee %.6f SOL (%.3f USD = %.2f%% of 25), at the 0.005005 SOL cap: %d of %d" % (fee.mean(), fee.mean() * FX0, 100 * fee.mean() * FX0 / 25, int((fee >= 0.005).sum()), len(fee)))
        if len(ex):
            print("              vs tape (%d with a curve swap row): extra SOL median %+.6f p90 %+.6f min %+.6f max %+.6f | USD@FX0 median %+.3f p90 %+.3f | %% of 25 USD clip: median %+.2f%% p90 %+.2f%%" % (
                len(ex), np.median(ex), np.percentile(ex, 90), ex.min(), ex.max(), np.median(ex) * FX0, np.percentile(ex, 90) * FX0, 100 * np.median(ex) * FX0 / 25, 100 * np.percentile(ex, 90) * FX0 / 25))
    json.dump(out, open("/tmp/claude-1000/-home-bigpapi-Desktop-Sniper/6a7c6910-619d-453f-8ce0-d59c296abe08/scratchpad/exec_cost.json", "w"))


if __name__ == "__main__":
    main(*(sys.argv[2:3] if len(sys.argv) > 2 and sys.argv[1] == "--limit" else []))
