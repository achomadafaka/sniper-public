#!/usr/bin/env python3
"""One-off audited repair: book a de-risk that LANDED on chain but was never booked.

2026-09-25, position 64934 (PepeBot, solana). The operator's 50% manual de-risk landed, but the
fast lane's recovery pass settled its signature 0.72 s after submission -- while the executor was
still inside its confirm wait -- and the executor's claim-owned booking then matched 0 rows. The
ledger kept the full bag and no derisk fill; the wallet held about half. The race is fixed by
MEMECOIN_DERISK_RECOVERY_MIN_AGE_S; this repairs the one row it already damaged.

WHAT IT WRITES, in one transaction, only with --apply:
  - one `memecoin_fills` row: kind='derisk', qty and notional from the CHAIN (the seller's token
    and USDC deltas in that transaction), price = notional / qty, the intent's fraction,
    fill_source='repair_chain', created_at = the position's `derisked_at` (the submission stamp);
  - `realized_pnl += notional` on the position. The Solana close computes
    `realized = received + derisk_proceeds(conn, pid) - entry_notional`
    (sniper/execution/solana_exit_executor.py:293); with no derisk fill that term was 0 at the
    close, so adding the notional afterwards is the missing term exactly once;
  - one `memecoin_pnl_repairs` row carrying the old and new realized_pnl.

IT REFUSES unless: the position is real, solana and CLOSED (repairing an open one would double
count at its close); no derisk fill exists for it; the signature is the tx identity of a
confirmed action='derisk' intent for that position; the transaction succeeded; exactly one owner
both sold the mint and received USDC in it. A read of the chain is refused while ANY real Solana
position is open, because the research RPC shares this host with funded reads -- in that case the
dry run prints the ledger facts and stops.

Dry-run by default. Reads the research endpoint MEMECOIN_RESEARCH_HELIUS_RPC_URL, never custody.
"""
import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(str(ROOT))
import os  # noqa: E402

from sniper.util.clock import real_utc_now_iso  # noqa: E402

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _deltas(meta, mint):
    """{owner: (mint_delta, usdc_delta)} from a transaction's pre/post token balances."""
    out = {}
    for sign, rows in ((-1, meta.get("preTokenBalances") or []),
                       (1, meta.get("postTokenBalances") or [])):
        for row in rows:
            owner, row_mint = row.get("owner"), row.get("mint")
            if row_mint not in (mint, USDC_MINT) or not owner:
                continue
            amount = float((row.get("uiTokenAmount") or {}).get("uiAmountString") or 0.0)
            m_d, u_d = out.get(owner, (0.0, 0.0))
            if row_mint == mint:
                m_d += sign * amount
            else:
                u_d += sign * amount
            out[owner] = (m_d, u_d)
    return out


def chain_fill(sig, mint, rpc_url, tx_json=None):
    """(qty_sold, usdc_received, owner) from one getTransaction, or raise with the reason.

    `tx_json` is a local copy of the raw response: read if it exists, else written right after
    the one network read. It lets the dry run and --apply share a single approved read."""
    if tx_json and Path(tx_json).exists():
        payload = json.loads(Path(tx_json).read_text())
        sigs = ((payload.get("result") or {}).get("transaction") or {}).get("signatures") or []
        if sig not in sigs:
            raise RuntimeError("%s does not hold transaction %s" % (tx_json, sig[:12]))
    else:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                           "params": [sig, {"encoding": "json", "commitment": "confirmed",
                                            "maxSupportedTransactionVersion": 1}]}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers={
            "content-type": "application/json", "user-agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        if tx_json:
            Path(tx_json).write_bytes(raw)
        payload = json.loads(raw)
    # THE ERROR FIELD, not just `result`: a refusal reads as a null result otherwise.
    if payload.get("error"):
        raise RuntimeError("getTransaction error: %s" % payload["error"])
    result = payload.get("result")
    if not result:
        raise RuntimeError("transaction not found at 'confirmed'")
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        raise RuntimeError("transaction failed on chain: %s" % meta.get("err"))
    sellers = [(owner, -m_d, u_d) for owner, (m_d, u_d) in _deltas(meta, mint).items()
               if m_d < 0 and u_d > 0]
    if len(sellers) != 1:
        raise RuntimeError("expected exactly one owner that sold the mint for USDC, found %d"
                           % len(sellers))
    owner, qty, usdc = sellers[0]
    return qty, usdc, owner


def main(position_id, sig, apply=False, rpc_url=None, tx_json=None):
    # A dry run never holds a writable handle on the funded ledger.
    db = ROOT / "sniper_tracker.db"
    conn = (sqlite3.connect(str(db), timeout=30) if apply
            else sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30))
    conn.row_factory = sqlite3.Row
    pos = conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (position_id,)).fetchone()
    if pos is None:
        sys.exit("refused: no position %s" % position_id)
    pos = dict(pos)
    print("position %s %s %s mode=%s open=%s derisked_at=%s realized_pnl=%s entry_notional=%s"
          % (pos["id"], pos["ticker"], pos["network_id"], pos["execution_mode"], pos["is_open"],
             pos["derisked_at"], pos["realized_pnl"], pos["entry_notional"]))
    if pos["execution_mode"] != "real" or pos["network_id"] != "solana":
        sys.exit("refused: not a real solana position")
    if conn.execute("SELECT 1 FROM memecoin_fills WHERE position_id=? AND kind='derisk'",
                    (position_id,)).fetchone():
        sys.exit("refused: a derisk fill already exists for this position")
    if conn.execute("SELECT 1 FROM memecoin_fills WHERE tx_sig=?", (sig,)).fetchone():
        sys.exit("refused: a fill already carries this signature")
    intent_row = None
    for row in conn.execute("SELECT claim_id, status, intent_json FROM memecoin_exit_intents "
                            "WHERE position_id=? AND action='derisk'", (position_id,)):
        intent = json.loads(row["intent_json"] or "{}")
        if intent.get("tx_identity") == sig:
            intent_row = (row["claim_id"], row["status"], intent)
    if intent_row is None:
        sys.exit("refused: no derisk intent for this position carries that signature")
    claim_id, status, intent = intent_row
    print("intent claim=%s status=%s fraction=%s balance_snapshot=%s floor_usdc=%.6f"
          % (claim_id, status, intent.get("fraction"), intent.get("balance_snapshot"),
             (int(intent.get("min_output_atomic") or 0) / 1e6)))
    if status != "confirmed":
        sys.exit("refused: the intent is %r, not confirmed" % status)

    open_sol = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                            "AND is_open=1 AND network_id='solana'").fetchone()[0]
    reads_network = not (tx_json and Path(tx_json).exists())
    if open_sol and reads_network:
        print("chain read DEFERRED: %d real Solana position(s) open; the research RPC shares this "
              "host with funded reads. Re-run after they close." % open_sol)
        return 0
    if pos["is_open"]:
        sys.exit("refused: the position is still open -- its own close will add derisk proceeds")
    # --rpc-url exists for one operator-approved read when the research key is out of quota
    # (2026-09-25: Helius answered 'max usage reached'). The default stays the research key.
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == 'shyft':
        from sniper.services.memecoin_solana_provider import provider_urls
        env = dict(os.environ)
        if rpc_url is not None:
            env['MEMECOIN_SOLANA_READ_RPC_URL'] = rpc_url
        rpc = provider_urls('read', env)[0]
    else:
        rpc = rpc_url or os.getenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL')
    if not rpc:
        sys.exit("refused: MEMECOIN_RESEARCH_HELIUS_RPC_URL is not set")
    print("chain read via %s" % ("host " + rpc.split("/")[2] if reads_network else tx_json))
    qty, usdc, owner = chain_fill(sig, pos["mint"], rpc, tx_json=tx_json)
    price = usdc / qty if qty > 0 else None
    old_realized = pos["realized_pnl"]
    new_realized = (float(old_realized) if old_realized is not None else 0.0) + usdc
    print("chain: owner=%s sold=%.6f received=%.6f USDC price=%.10g" % (owner, qty, usdc, price))
    # THE CHAIN MUST AGREE WITH WHAT THE EXECUTOR MEANT TO SELL, or this is the wrong transaction.
    # The executor sells balance_snapshot x fraction and signed a USDC floor; a disagreement is a
    # refusal, not a warning, because a wrong fill booked by hand is worse than none.
    expected_qty = float(intent.get("balance_snapshot") or 0.0) * float(intent.get("fraction") or 0.0)
    floor = int(intent.get("min_output_atomic") or 0) / 1e6
    if expected_qty <= 0 or abs(qty - expected_qty) / expected_qty > 0.01:
        sys.exit("refused: chain sold %.6f, the intent expected %.6f (>1%% apart)"
                 % (qty, expected_qty))
    if floor > 0 and usdc + 1e-6 < floor:
        sys.exit("refused: chain received %.6f USDC, below the signed floor %.6f" % (usdc, floor))
    print("plan: INSERT derisk fill qty=%.6f notional=%.6f fraction=%s created_at=%s; "
          "realized_pnl %s -> %.6f" % (qty, usdc, intent.get("fraction"), pos["derisked_at"],
                                       old_realized, new_realized))
    if not apply:
        print("DRY RUN: nothing written. Re-run with --apply on the operator's word.")
        return 0
    now = real_utc_now_iso()
    try:
        cur = conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at, tx_sig) VALUES(?,?,?,?,?,?,?,?,?)",
            (position_id, "derisk", price, qty, usdc, "repair_chain",
             float(intent.get("fraction") or 0.0), pos["derisked_at"], sig))
        fill_id = cur.lastrowid
        updated = conn.execute(
            "UPDATE memecoin_positions SET realized_pnl=? WHERE id=? AND is_open=0",
            (new_realized, position_id)).rowcount
        if updated != 1:
            raise RuntimeError("realized_pnl update matched %d rows" % updated)
        conn.execute(
            "INSERT INTO memecoin_pnl_repairs(position_id, fill_id, old_realized, new_realized, "
            "reason, repaired_at) VALUES(?,?,?,?,?,?)",
            (position_id, fill_id, old_realized, new_realized,
             "unbooked landed derisk %s (recovery/executor race, 2026-09-25)" % sig[:12], now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print("APPLIED: fill %s booked, realized_pnl %s -> %.6f" % (fill_id, old_realized, new_realized))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--position-id", type=int, required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rpc-url", help="override the research RPC for this one read (operator-approved)")
    ap.add_argument("--tx-json", help="raw getTransaction copy: read if present, else saved after the read")
    args = ap.parse_args()
    sys.exit(main(args.position_id, args.sig, apply=args.apply, rpc_url=args.rpc_url,
                  tx_json=args.tx_json))
