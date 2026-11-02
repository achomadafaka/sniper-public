#!/usr/bin/env python3
"""Add or remove a wallet on a J7Tracker watchlist, on the operator's request.

WHY THIS EXISTS. J7 is the ONLY live delivery lane since the global feed stopped on
2026-09-09T15:48Z, so a wallet that holds no J7 slot sends us nothing, and the retire pass excludes
wallets that are "not on J7 lists" (that removed airtightfish 29 minutes after it was armed on
2026-09-22). Until now the repo could only MIRROR J7's lists into `j7_tracked_wallets`; every change
had to be made by hand in the web app.

WHAT IT TALKS TO. The same endpoints the web app calls, read out of its own bundle
(`https://j7tracker.io/static/js/main.*.js`):

    GET  https://nj.j7tracker.io/wallets/api/{fomo,pump}/list
    POST https://nj.j7tracker.io/wallets/api/{fomo,pump}/{add,remove}   body {"target": "..."}
    Authorization: Bearer <session token from .j7.env>

Each call returns the FULL updated list, so the server is the source of truth for what happened;
this tool reports the list it got back rather than what it hoped for.

TWO THINGS THAT WILL BITE IF FORGOTTEN.
  1. A slot is NOT an arm. Adding a wallet here only makes its signals arrive. Approval lives in
     `memecoin_wallets.real_approved` and stays untouched.
  2. The two lists key on DIFFERENT identities. A fomo slot keys on the fomo handle/uuid; a pump slot
     keys on a raw Solana ADDRESS and produces the roster id `pump:<address>`, which is a SEPARATE
     `source_user_id`. Putting a wallet in a pump slot without arming that id means every signal
     arrives and is refused `not_approved`.

DRY RUN IS THE DEFAULT. Nothing is written without `--apply`.

    python3 scripts/j7_watchlist.py list
    python3 scripts/j7_watchlist.py add --kind fomo --target airtightfish --account 1 --apply
    python3 scripts/j7_watchlist.py remove --kind fomo --target prevaile --account 1 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".j7.env")
CHANGE_LOG = os.path.join(ROOT, "data", "j7_watchlist_changes.jsonl")
BASE = "https://nj.j7tracker.io/wallets"
HEADERS = {
    "Origin": "https://j7tracker.io",
    "Referer": "https://j7tracker.io/",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
}
KIND_FIELD = {"fomo": "fomo_users", "pump": "pump_users"}


def _env():
    out = {}
    with open(ENV_FILE) as handle:
        for line in handle:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    return out


def _token(account):
    env = _env()
    key = "J7_TOKEN" if int(account) == 0 else "J7_TOKEN_%d" % (int(account) + 1)
    token = env.get(key)
    if not token:
        raise SystemExit("no %s in .j7.env; account %s has no stored session" % (key, account))
    return token


def _request(path, token, *, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS, Authorization="Bearer " + token)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf8", "replace")[:300]
        raise SystemExit("J7 refused %s: HTTP %s %s" % (path, exc.code, detail))


def fetch(kind, account):
    got = _request("/api/%s/list" % kind, _token(account))
    return got.get(KIND_FIELD[kind]) or [], got.get("limit")


def _label(kind, row):
    if kind == "fomo":
        return row.get("handle") or row.get("fomo_user_id") or "?"
    return row.get("username") or row.get("wallet") or "?"


def _identity(kind, row):
    return row.get("fomo_user_id") if kind == "fomo" else row.get("wallet")


def cmd_list(args):
    for account in ([args.account] if args.account is not None else (0, 1)):
        for kind in ([args.kind] if args.kind else ("fomo", "pump")):
            try:
                rows, limit = fetch(kind, account)
            except SystemExit as exc:
                print("account %s %-5s -- %s" % (account, kind, exc))
                continue
            print("account %s %-5s %d/%s used, %s free"
                  % (account, kind, len(rows), limit,
                     (limit - len(rows)) if isinstance(limit, int) else "?"))
            for row in rows:
                print("    %-26s %s" % (_label(kind, row)[:26], _identity(kind, row) or ""))


def _write(args, action):
    kind, target, account = args.kind, args.target, args.account or 0
    before, limit = fetch(kind, account)
    names = {(_label(kind, r) or "").lower() for r in before}
    ids = {(_identity(kind, r) or "").lower() for r in before}
    present = target.lower() in names or target.lower() in ids
    print("account %s %s: %d/%s used before" % (account, kind, len(before), limit))
    if action == "add" and present:
        print("  %r is already on the list; nothing to do." % target)
        return 0
    if action == "remove" and not present:
        print("  %r is NOT on the list; nothing to do." % target)
        return 0
    if action == "add" and isinstance(limit, int) and len(before) >= limit:
        print("  REFUSED: the list is full (%d/%d). Remove one first; this tool will not"
              " evict a wallet to make room." % (len(before), limit))
        return 1
    if not args.apply:
        print("  DRY RUN: would %s %r. Re-run with --apply to perform it." % (action, target))
        return 0

    got = _request("/api/%s/%s" % (kind, action), _token(account), body={"target": target})
    after = got.get(KIND_FIELD[kind]) or []
    names_after = {(_label(kind, r) or "").lower() for r in after}
    ids_after = {(_identity(kind, r) or "").lower() for r in after}
    now_present = target.lower() in names_after or target.lower() in ids_after
    ok = now_present if action == "add" else not now_present
    print("  %s %r: list is now %d/%s, target present=%s -> %s"
          % (action, target, len(after), got.get("limit"), now_present,
             "OK" if ok else "NOT WHAT WAS ASKED"))
    os.makedirs(os.path.dirname(CHANGE_LOG), exist_ok=True)
    with open(CHANGE_LOG, "a") as handle:
        handle.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(), "account": account, "kind": kind,
            "action": action, "target": target, "before": len(before), "after": len(after),
            "confirmed": ok}) + "\n")
    if kind == "pump":
        print("  NOTE: a pump slot keys on the ADDRESS, so the roster id is 'pump:%s'."
              " Arming the fomo uuid does NOT arm it." % target)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("list")
    show.add_argument("--account", type=int)
    show.add_argument("--kind", choices=("fomo", "pump"))
    for name in ("add", "remove"):
        one = sub.add_parser(name)
        one.add_argument("--kind", choices=("fomo", "pump"), required=True)
        one.add_argument("--target", required=True,
                         help="fomo: handle or fomo_user_id. pump: the Solana address.")
        one.add_argument("--account", type=int, default=0)
        one.add_argument("--apply", action="store_true", help="perform it; default is a dry run")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args) or 0
    return _write(args, args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
