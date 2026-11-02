#!/usr/bin/env python3
"""Price the funded Solana refusals on Meteora DBC transfer-hook mints: is a hook adapter worth it?

Pre-registered in HANDOFF 2026-09-29 ~08:25Z (Gamma). Population: funded Solana attempts since
2026-09-13 whose terminal reason is "launchpad-gated: transfer hook ..." or a Jupiter
TOKEN_NOT_TRADABLE no-route. Per mint (research key, flat-gated, getAccountInfo only): the token
program and the Token-2022 TransferHook extension; per hook program: the executable's sha256 and
the strings it carries, which class it:
  size_limit  UP's executable (audit output/research/up-hook-20260928) or BuyTooBig/SellTooBig and
              no co-sign string; caps read from the per-mint config PDA ["cfg", mint]
  cosign      a Fomo / platform co-sign string (FOMO_REQUIRED, "Fomo verification", verificadora)
  unknown     neither
  no_hook     classic SPL or Token-2022 without a hook (a different builder's market)

    python3 scripts/research/dbc_hook_refusals.py [--since 2026-09-13]
    python3 scripts/research/dbc_hook_refusals.py --selftest
"""
import argparse
import base64
import collections
import contextlib
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

LEDGER = "file:%s?mode=ro" % (REPO / "sniper_tracker.db")
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
UP_HOOK_SHA = "fe491cf9373ec56bfa642214e9acca65e786c109c1b7172e5c19a1c09e2979d3"
UP_CFG_DISC = bytes.fromhex("ec45f0c7bd7b2363")
COSIGN = (b"FOMO_REQUIRED", b"Fomo verification", b"verificadora", b"co-assin")
SIZE = (b"BuyTooBig", b"SellTooBig")
EXT_TRANSFER_HOOK = 14
REASONS = ("launchpad-gated: transfer hook %", "no quote (no_route: jupiter%TOKEN_NOT_TRADABLE%")


def attempts(since):
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT attempt_id, origin_event_id, mint, decision_started_at, terminal_status, terminal_reason, "
            "source_handle, source_price, classification_json FROM memecoin_entry_attempts "
            "WHERE network_id='solana' AND decision_started_at>=? AND (terminal_reason LIKE ? OR terminal_reason LIKE ?) "
            "ORDER BY decision_started_at", (since,) + REASONS).fetchall()
    out = []
    for r in rows:
        try:
            facts = (json.loads(r["classification_json"] or "{}") or {}).get("facts") or {}
        except (TypeError, ValueError):
            facts = {}
        out.append({**dict(r), "clip": facts.get("base_clip_usd")})
    return out


def hook_of(mint_data, owner):
    """The TransferHook program id of a Token-2022 mint, or None (classic SPL or no hook)."""
    if owner != TOKEN_2022 or len(mint_data) <= 166:
        return None
    i = 166                                 # 165-byte base + account type byte
    while i + 4 <= len(mint_data):
        etype = int.from_bytes(mint_data[i:i + 2], "little")
        elen = int.from_bytes(mint_data[i + 2:i + 4], "little")
        body = mint_data[i + 4:i + 4 + elen]
        if etype == EXT_TRANSFER_HOOK and len(body) >= 64:
            from solders.pubkey import Pubkey
            pid = bytes(body[32:64])
            return None if pid == bytes(32) else str(Pubkey.from_bytes(pid))
        if etype == 0 and elen == 0:
            break
        i += 4 + elen
    return None


def source_sig(origin):
    """The Solana source signature an origin carries by shape, or None (J7 uuid)."""
    import re
    return next((p for p in str(origin or "").split(":") if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{80,90}", p)), None)


def hook_in_logs(logs):
    """The program a Token-2022 instruction invoked one level deeper (its transfer hook), or None."""
    for i, line in enumerate(logs):
        parts = line.split()
        if len(parts) == 4 and parts[0] == "Program" and parts[1] == TOKEN_2022 and parts[2] == "invoke":
            inner = "[%d]" % (int(parts[3].strip("[]")) + 1)
            for nxt in logs[i + 1:]:
                q = nxt.split()
                if len(q) >= 3 and q[0] == "Program" and q[1] == TOKEN_2022 and q[2] in ("success", "failed:"):
                    break                  # this token call returned without a deeper invoke
                if len(q) == 4 and q[0] == "Program" and q[2] == "invoke" and q[3] == inner and q[1] != TOKEN_2022:
                    return q[1]
    return None


def classify(executable, sha):
    cos = [s.decode() for s in COSIGN if s in executable]
    size = [s.decode() for s in SIZE if s in executable]
    if cos:
        return "cosign", cos + size
    if sha == UP_HOOK_SHA or size:
        return "size_limit", size
    return "unknown", []


def caps(cfg_data):
    """(buy_cap_atomic, sell_cap_atomic) from a config with UP's layout, or None."""
    if not cfg_data or len(cfg_data) < 25 or cfg_data[:8] != UP_CFG_DISC:
        return None
    return int.from_bytes(cfg_data[9:17], "little"), int.from_bytes(cfg_data[17:25], "little")


def run(args):
    import funded_exit_truth as fet
    from slot_minout_grading import wait_flat
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    from solders.pubkey import Pubkey
    load_env_files(str(REPO))
    fet.SPACING_S = 0.5

    def rpc(method, params):
        wait_flat(lambda: get_readonly_connection(str(REPO / "sniper_tracker.db")), max_wait_s=1800)
        return fet.rpc(method, params)

    def account(key):
        v = (rpc("getAccountInfo", [key, {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
        return (base64.b64decode(v["data"][0]), v["owner"]) if v else (None, None)

    rows = attempts(args.since)
    mints = sorted({r["mint"] for r in rows})
    print("attempts %d, origins %d, mints %d" % (len(rows), len({r["origin_event_id"] for r in rows}), len(mints)))
    info, hooks = {}, {}
    by_mint = collections.defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    for mint in mints:
        data, owner = account(mint)
        hook = hook_of(data, owner) if data else None
        info[mint] = {"token_program": owner, "hook": hook, "decimals": data[44] if data else None,
                      "hook_basis": "mint_now" if hook else None}
        if hook is None and owner == TOKEN_2022:
            # A DBC migration revokes the hook, so today's mint can read no-hook for a coin that was
            # hooked when we refused it. The source transaction's logs name the hook it ran then.
            for r in by_mint[mint]:
                sig = source_sig(r["origin_event_id"])
                tx = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                  "commitment": "confirmed"}]) if sig else None
                hook = hook_in_logs((tx or {}).get("meta", {}).get("logMessages") or [])
                if hook:
                    info[mint].update(hook=hook, hook_basis="source_tx")
                    break
        if hook and hook not in hooks:
            pdata, _o = account(hook)
            prog_data_key = str(Pubkey.from_bytes(pdata[4:36])) if pdata and len(pdata) >= 36 else None
            pd, _o = account(prog_data_key) if prog_data_key else (None, None)
            exe = pd[45:].rstrip(b"\x00") if pd else b""
            sha = hashlib.sha256(pd[45:]).hexdigest() if pd else None
            klass, marks = classify(exe, sha) if pd else ("unknown", [])
            hooks[hook] = {"sha": sha, "class": klass, "marks": marks}
        if hook and hooks[hook]["class"] == "size_limit":
            cfg_key, _b = Pubkey.find_program_address([b"cfg", bytes(Pubkey.from_string(mint))], Pubkey.from_string(hook))
            cdata, _o = account(str(cfg_key))
            info[mint]["caps"] = caps(cdata)
    for h, v in hooks.items():
        print("hook %s sha %s class %s marks %s" % (h[:8], (v["sha"] or "?")[:12], v["class"], v["marks"]))
    for mint in mints:
        print("mint %s token %s hook %s basis %s caps %s" % (mint[:8], (info[mint]["token_program"] or "?")[:6],
              (info[mint]["hook"] or "-")[:8], info[mint]["hook_basis"], info[mint].get("caps")))
    per = collections.Counter()
    weekly = collections.defaultdict(collections.Counter)
    seen = set()
    for r in rows:
        mi = info[r["mint"]]
        klass = "no_hook" if not mi["hook"] else hooks[mi["hook"]]["class"]
        r["class"] = klass
        wk = datetime.fromisoformat(r["decision_started_at"]).strftime("%G-W%V")
        weekly[wk][klass + ":attempts"] += 1
        if r["origin_event_id"] not in seen:
            seen.add(r["origin_event_id"])
            per[klass] += 1
            weekly[wk][klass + ":origins"] += 1
    print("origins by class:", dict(per))
    for wk in sorted(weekly):
        print("  %s %s" % (wk, dict(sorted(weekly[wk].items()))))
    print("\nsize-limit attempts (sell cap vs clip at the source price):")
    for r in rows:
        if r["class"] != "size_limit":
            continue
        mi = info[r["mint"]]
        cap = mi.get("caps")
        dec = mi["decimals"] or 6
        sell_usd = cap[1] / 10 ** dec * r["source_price"] if cap and r["source_price"] else None
        print("  %s %s %-14s %s buy_cap %s sell_cap %s sell_cap_usd %s clip %s" % (
            r["decision_started_at"][:16], r["attempt_id"][:8], (r["source_handle"] or "")[:14], r["mint"][:8],
            cap and cap[0] / 10 ** dec, cap and cap[1] / 10 ** dec,
            None if sell_usd is None else round(sell_usd, 2), r["clip"]))
    caps_db = REPO / "data" / "memecoin_refusal_captures.db"
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % caps_db, uri=True)) as cap:
        have = {r[0] for r in cap.execute(
            "SELECT DISTINCT origin_event_id FROM memecoin_refusal_captures WHERE network_id='solana' AND quality='ok' "
            "AND origin_event_id IN (%s)" % ",".join("?" * len(seen)), tuple(seen))} if seen else set()
    print("\norigins with an ok refusal capture: %d of %d (by class %s)" % (
        len(have), len(seen), dict(collections.Counter(r["class"] for r in rows if r["origin_event_id"] in have))))


def selftest():
    import base64 as b64  # noqa: F401
    from solders.pubkey import Pubkey
    hook = Pubkey.from_string("Ft8R4BsJp7n3dGKsd1RbAi3WrvkiH5s5z2jyDztEQJ6f")
    base = bytearray(166)
    base[44] = 6
    ext = (14).to_bytes(2, "little") + (64).to_bytes(2, "little") + bytes(32) + bytes(hook)
    other = (1).to_bytes(2, "little") + (8).to_bytes(2, "little") + bytes(8)
    assert hook_of(bytes(base) + other + ext, TOKEN_2022) == str(hook)
    assert hook_of(bytes(base) + other + ext, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA") is None
    null_hook = (14).to_bytes(2, "little") + (64).to_bytes(2, "little") + bytes(64)
    assert hook_of(bytes(base) + null_hook, TOKEN_2022) is None
    assert classify(b"..FOMO_REQUIRED..BuyTooBig", "x")[0] == "cosign"
    assert classify(b"..SellTooBig..", "x")[0] == "size_limit"
    assert classify(b"....", UP_HOOK_SHA)[0] == "size_limit" and classify(b"....", "x")[0] == "unknown"
    cfg = bytes.fromhex("ec45f0c7bd7b2363ff0040e59c3012000000a89c1346020000") + bytes(32)
    assert caps(cfg) == (20_000_000_000_000, 2_500_000_000_000)          # UP's captured config
    assert caps(b"\x00" * 57) is None
    logs = ["Program %s invoke [2]" % TOKEN_2022, "Program log: Instruction: TransferChecked",
            "Program Ft8R4BsJp7n3dGKsd1RbAi3WrvkiH5s5z2jyDztEQJ6f invoke [3]", "Program log: ok"]
    assert hook_in_logs(logs) == "Ft8R4BsJp7n3dGKsd1RbAi3WrvkiH5s5z2jyDztEQJ6f"
    assert hook_in_logs(["Program %s invoke [2]" % TOKEN_2022, "Program %s success" % TOKEN_2022]) is None
    assert source_sig("onchain:" + "5" * 88) == "5" * 88 and source_sig("swap:abc-uuid") is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-13T00:00:00")
    a = ap.parse_args()
    return selftest() if a.selftest else run(a)


if __name__ == "__main__":
    main()
