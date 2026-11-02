#!/usr/bin/env python3
"""Measure processed wallet buys without touching the funded runner or ledger.

Default mode decodes Pump trade events directly from free processed logs, with no getTransaction.
The full-transaction mode needs Enhanced WebSocket access. Both print compact JSON lines; neither
signs, submits, or writes a detection row. Reconcile candidates against final chain outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import sys
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def subscription_request(watch):
    return {"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe", "params": [
        {"failed": False, "accountInclude": sorted(watch)},
        {"commitment": "processed", "encoding": "jsonParsed",
         "transactionDetails": "full", "maxSupportedTransactionVersion": 1},
    ]}


def logs_request(address, request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "logsSubscribe", "params": [
        {"mentions": [address]}, {"commitment": "processed"},
    ]}


def classify_processed_log(frame, watch):
    """An exact Pump Buy event names its actor and mint in the processed log itself."""
    from sniper.services.memecoin_opportunity_live import (
        parse_solana_log_notification, program_data_logs,
    )
    from sniper.research.memecoin_opportunity.pumpfun import (
        PUMPFUN_PROGRAM, decode_pumpfun_event,
    )
    from sniper.research.memecoin_opportunity.types import EventKind
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes, base58_encode

    parsed = parse_solana_log_notification(frame)
    if not parsed or parsed["err"] or parsed["slot"] <= 0:
        return None
    matches = []
    for _, line in program_data_logs(parsed["logs"], PUMPFUN_PROGRAM):
        try:
            event = decode_pumpfun_event(PUMPFUN_PROGRAM, line)
        except binascii.Error:
            continue
        if (event.blocker is not None or event.event_kind is not EventKind.BUY
                or event.actor_id not in watch or not event.token_id):
            continue
        info = watch[event.actor_id]
        matches.append({"signature": parsed["signature"], "source_slot": parsed["slot"],
                        "source_user_id": info["uid"], "handle": info["handle"],
                        "address": event.actor_id, "mint": event.token_id,
                        "quote_amount_atomic": event.quote_delta_atomic,
                        "token_amount_atomic": -event.token_delta_atomic,
                        "decoder_version": event.decoder_version})
    amm_program = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    buy_disc = bytes([103, 244, 82, 31, 44, 245, 119, 119])
    for _, line in program_data_logs(parsed["logs"], amm_program):
        try:
            raw = anchor_event_bytes(line)
        except (ValueError, binascii.Error):
            continue
        # Official Pump AMM BuyEvent: discriminator, timestamp, 13 u64s, pool, user.
        if raw[:8] != buy_disc or len(raw) < 184:
            continue
        pool, actor = base58_encode(raw[120:152]), base58_encode(raw[152:184])
        if actor not in watch:
            continue
        amounts = struct.unpack_from("<13Q", raw, 16)
        info = watch[actor]
        matches.append({"signature": parsed["signature"], "source_slot": parsed["slot"],
                        "source_user_id": info["uid"], "handle": info["handle"],
                        "address": actor, "mint": None, "pool": pool,
                        "base_amount_out_atomic": amounts[0],
                        "quote_amount_in_atomic": amounts[6],
                        "user_quote_amount_in_atomic": amounts[12],
                        "decoder_version": "pump-amm-official-buy-event-prefix"})
    return matches[0] if len(matches) == 1 else None


def decode_pool_account(account):
    """Official Pump AMM Pool layout: discriminator, bump, index, creator, base/quote mints."""
    from sniper.research.memecoin_opportunity.decoders import base58_encode

    program = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    if not isinstance(account, dict) or account.get("owner") != program:
        return None
    data = account.get("data") or []
    if not isinstance(data, list) or len(data) < 2 or data[1] != "base64":
        return None
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError, binascii.Error):
        return None
    if len(raw) < 107 or raw[:8] != bytes([241, 154, 109, 4, 17, 177, 109, 188]):
        return None
    return base58_encode(raw[43:75]), base58_encode(raw[75:107])


def fetch_pool_mint(pool):
    """Public read only; an unavailable pool is unknown, never an inferred mint."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [
        pool, {"encoding": "base64", "commitment": "processed",
               "dataSlice": {"offset": 0, "length": 107}},
    ]}).encode()
    request = urllib.request.Request("https://api.mainnet-beta.solana.com", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.load(response)
    if payload.get("error"):
        return None, "rpc_error"
    account = (payload.get("result") or {}).get("value")
    decoded = decode_pool_account(account)
    return decoded, None if decoded else "missing_or_invalid_pool"


async def resolve_pool_event(event, cache):
    started = time.monotonic()
    decoded, error = cache.get(event["pool"]), None
    cached = decoded is not None
    if not cached:
        try:
            decoded, error = await asyncio.to_thread(fetch_pool_mint, event["pool"])
        except Exception as exc:  # never silently turn provider failure into zero
            decoded, error = None, ("http_429" if getattr(exc, "code", None) == 429
                                    else type(exc).__name__)
        if decoded:
            # ponytail: bounded FIFO of stable pool identities; no failure is cached.
            if len(cache) >= 1024:
                cache.pop(next(iter(cache)))
            cache[event["pool"]] = decoded
    print(json.dumps({"type": "pool_resolution", "signature": event["signature"],
                      "source_slot": event["source_slot"], "pool": event["pool"],
                      "mint": decoded[0] if decoded else None,
                      "quote_mint": decoded[1] if decoded else None, "error": error, "cached": cached,
                      "resolve_ms": round((time.monotonic() - started) * 1000, 2)}), flush=True)


async def admit_pool_resolution(event, pending, cache):
    pool = event["pool"]
    if pool in cache:
        await resolve_pool_event(event, cache)
    elif pool in pending or len(pending) >= 4:
        print(json.dumps({"type": "pool_resolution", "signature": event["signature"],
                          "source_slot": event["source_slot"], "pool": pool, "mint": None,
                          "error": "pool_pending" if pool in pending else "capacity_deferred"}),
              flush=True)
    else:
        task = asyncio.create_task(resolve_pool_event(event, cache))
        pending[pool] = task
        task.add_done_callback(lambda _task, pool=pool: pending.pop(pool, None))


def classify_notification(frame, watch):
    """Reuse the existing balance parser; require one exact watched owner and a source slot."""
    if frame.get("method") != "transactionNotification":
        return None
    result = (frame.get("params") or {}).get("result") or {}
    slot, signature, tx = result.get("slot"), result.get("signature"), result.get("transaction")
    if not isinstance(slot, int) or slot <= 0 or not signature or not isinstance(tx, dict):
        return None
    if not isinstance(tx.get("meta"), dict) or tx["meta"].get("err"):
        return None
    from scripts.research.onchain_wallet_watch import classify_buy

    matches = []
    for address, info in watch.items():
        try:
            buy = classify_buy(tx, address)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if buy:
            mint, token_delta, sol_spent, usdc_spent = buy
            matches.append({"signature": signature, "source_slot": slot,
                            "source_user_id": info["uid"], "handle": info["handle"],
                            "address": address, "mint": mint, "token_delta": token_delta,
                            "sol_spent": sol_spent, "usdc_spent": usdc_spent})
    return matches[0] if len(matches) == 1 else None


def research_ws_url(*, enhanced=False):
    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    url = os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "")
    from sniper.services.memecoin_research_rpc import funded_overlap
    if not url or funded_overlap([url]):
        raise ValueError("separate research Helius endpoint required")
    key = parse_qs(urlparse(url).query).get("api-key", [""])[0]
    if not key:
        raise ValueError("research Helius endpoint lacks api-key")
    host = "atlas-mainnet.helius-rpc.com" if enhanced else "mainnet.helius-rpc.com"
    return "wss://" + host + "/?api-key=" + key


def load_candidate_addresses(path, approved_ids):
    """Research-only additions backed by two exact signatures on distinct mints."""
    if not path.exists():
        return {}
    found, conflicted = {}, set()
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            uid, address = row.get("uid"), row.get("address")
            if uid not in approved_ids or not address:
                continue
            observations = {(mint, sig) for owner, mint, sig in row.get("observed", ())
                            if owner == address and mint and sig}
            if len({mint for mint, _ in observations}) < 2 or len({sig for _, sig in observations}) < 2:
                continue
            if address in found and found[address]["uid"] != uid:
                conflicted.add(address)
                continue
            found[address] = {"uid": uid, "handle": row.get("handle"),
                              "status": "exact_two_mints"}
        except (ValueError, TypeError, KeyError):
            continue
    return {address: info for address, info in found.items() if address not in conflicted}


async def run(duration_s, *, mode="logs", public=False):
    from scripts.research.onchain_wallet_watch import load_watchlist
    from sniper.persistence.db_core import get_readonly_connection
    watch = load_watchlist()
    conn = get_readonly_connection(str(ROOT / "sniper_tracker.db"))
    try:
        approved = {r[0] for r in conn.execute(
            "SELECT source_user_id FROM memecoin_wallets WHERE real_approved=1 "
            "AND paused=0 AND excluded=0 AND real_chains LIKE '%solana%'")}
        # A pump: source id is an address; require the independent Axiom directory to agree.
        for row in conn.execute(
                "SELECT w.source_user_id, w.handle, d.address FROM memecoin_wallets w "
                "JOIN axiom_trader_directory d ON LOWER(d.x_handle)=LOWER(w.handle) "
                "AND d.chain='solana' WHERE w.source_user_id LIKE 'pump:%' "
                "AND w.real_approved=1 AND w.paused=0 AND w.excluded=0 "
                "AND w.real_chains LIKE '%solana%'"):
            source_id, handle, directory_address = row
            address = source_id[5:]
            if address == directory_address and address not in watch:
                watch[address] = {"uid": source_id, "handle": handle,
                                  "status": "pump_axiom_exact"}
    finally:
        conn.close()
    watch = {address: info for address, info in watch.items() if info["uid"] in approved}
    for address, info in load_candidate_addresses(
            ROOT / "data" / "solana_exact_address_candidates.jsonl", approved).items():
        if address not in watch:
            watch[address] = info
    if not watch:
        raise ValueError("no verified armed Solana wallet addresses")
    if public and mode != "logs":
        raise ValueError("public RPC supports only the logs probe")
    url = ("wss://api.mainnet-beta.solana.com/" if public else
           research_ws_url(enhanced=(mode == "full")))
    print(json.dumps({"type": "start", "addresses": len(watch),
                      "commitment": "processed", "mode": mode, "public": public,
                      "funded": False}), flush=True)
    seen = set()
    pending, pool_cache = {}, {}
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=2,
                                      max_size=16 * 1024 * 1024) as ws:
            requests = ([subscription_request(watch)] if mode == "full" else
                        [logs_request(address, i) for i, address in enumerate(watch, 1)])
            for request in requests:
                await ws.send(json.dumps(request))
            acknowledged, startup_excluded = set(), 0
            request_ids = {request["id"] for request in requests}
            while acknowledged != request_ids:
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if "error" in ack:
                    print(json.dumps({"type": "subscription_error", "error": ack["error"]}),
                          flush=True)
                    return 2
                if ack.get("id") in request_ids and isinstance(ack.get("result"), int):
                    acknowledged.add(ack["id"])
                elif ack.get("method"):
                    startup_excluded += 1
            started = time.monotonic()
            print(json.dumps({"type": "subscribed", "addresses": len(watch),
                              "capture_started_at": datetime.now(timezone.utc).isoformat(),
                              "startup_notifications_excluded": startup_excluded,
                              "supported_event_scope": "exactly_one_watched_buy_per_transaction"}), flush=True)
            while time.monotonic() - started < duration_s:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5, duration_s))
                    received_mono = time.monotonic()
                except asyncio.TimeoutError:
                    continue
                try:
                    frame = json.loads(raw)
                    event = (classify_notification(frame, watch) if mode == "full" else
                             classify_processed_log(frame, watch))
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError,
                        KeyError) as exc:
                    print(json.dumps({"type": "malformed_frame", "error": type(exc).__name__}),
                          flush=True)
                    continue
                if event is None or event["signature"] in seen:
                    continue
                seen.add(event["signature"])
                if len(seen) > 10000:
                    seen.clear()
                    seen.add(event["signature"])
                event.update(type="candidate_pool_buy" if event.get("pool") else "candidate_buy",
                             receipt_at=datetime.now(timezone.utc).isoformat(),
                             parse_ms=round((time.monotonic() - received_mono) * 1000, 3))
                print(json.dumps(event, separators=(",", ":")), flush=True)
                if event.get("pool"):
                    await admit_pool_resolution(event, pending, pool_cache)
            if pending:
                await asyncio.wait(list(pending.values()), timeout=4)
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
        # Never print the URL: it contains the API key.
        print(json.dumps({"type": "stream_error", "error": type(exc).__name__,
                          "status": getattr(exc, "status_code", None)}), flush=True)
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=float, default=300,
                    help="read-only capture seconds (default 300)")
    ap.add_argument("--mode", choices=("logs", "full"), default="logs",
                    help="processed Pump logs (free) or full Helius transactions")
    ap.add_argument("--public", action="store_true",
                    help="use public Solana WebSocket for a bounded mechanism test; delivery is incomplete")
    args = ap.parse_args()
    if args.duration <= 0:
        ap.error("--duration must be positive")
    try:
        return asyncio.run(run(args.duration, mode=args.mode, public=args.public))
    except ValueError as exc:
        print(json.dumps({"type": "configuration_error", "error": str(exc)}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
