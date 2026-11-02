#!/usr/bin/env python3
"""Fill the durable V4 PoolKey cache offline, so a funded entry never waits on a chain read.

WHY THIS EXISTS. Discovery and the PoolKey read both ask the same endpoint, and on chain 4663 that
endpoint is the ONLY one that serves `eth_getLogs` at any useful width: Alchemy refuses the method
on the free tier and publicnode wants an archive token. Measured 2026-09-03 over 20 identical
full-range reads to it: 17 answered, 2 returned an error body, 1 was rate limited, p50 665 ms and
p90 2157 ms. One transient failure on the one serving endpoint costs an entry, and it did -- for 12
of the 13 coins refused that day the PoolKey was cached one to three SECONDS later by the runner's
own next attempt. The read was never impossible, it just failed at the moment that mattered.

So do the read when nothing is at stake. A pool key is immutable, so a pool is paid for once, ever,
and `_stored_pools_for_mint` then answers discovery from the table in about 3 ms with no network.

SAFETY RULES THIS SCRIPT KEEPS.
  1. It reads only the archive/on-chain URL, never the custody variable. On 4663 that is NOT a
     separate host: no archive URL is set, and MEMECOIN_ONCHAIN_EVM_RPC_URL_4663,
     MEMECOIN_EVM_RPC_URL_4663 and the 4663 watcher all resolve to rpc.mainnet.chain.robinhood.com
     (Gamma, 2026-10-07). Its reads share that host's limits with the money path, so each run is
     bounded: --max-pages backfill pages and --wanted-calls topic reads (09-03: 1 of 20 full-range
     reads on this host was rate limited). If the runner's 4663 rpc_rate_limit marks line up with
     this script's run windows, lower --wanted-calls first.
  2. Every decoded key is verified by hashing it back to the pool id it was read for. A key that
     does not hash back is DROPPED, never stored: a wrong cached key would make a funded entry
     quote a pool that does not exist.
  3. It is bounded and resumable. Each run does a little, records how far it got, and exits.
"""
import argparse
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

# BEFORE the settings import. `sniper.settings.memecoin` reads os.getenv at IMPORT time, so a bare
# import from cron would silently take DEFAULTS -- no archive endpoint, and this script would look
# like it had nothing to do instead of failing loudly.
#
# THE ENV LIVES AT THE STATE ROOT, NOT BESIDE THIS FILE. Cron runs this out of the PINNED RELEASE
# directory, and a release has no `.env` of its own -- only the state root does. Reading it from
# `_REPO` would load nothing whenever it matters most.
try:
    from dotenv import load_dotenv

    # State root first (cron sets the working directory to it), then beside this file as a
    # last resort. A release directory has no `.env`, so `_REPO` alone finds nothing.
    _roots = [os.environ.get("SNIPER_STATE_ROOT"), os.environ.get("MEMECOIN_STATE_ROOT"),
              os.getcwd(), _REPO]
    for _root in _roots:
        _candidate = os.path.join(_root, ".env") if _root else None
        if _candidate and os.path.exists(_candidate):
            load_dotenv(_candidate)
            # ANCHOR THE STATE ROOT TO WHERE THE ENV CAME FROM. `state_root()` otherwise defaults
            # to the directory this file sits in, and cron runs it out of the PINNED RELEASE -- so
            # the resume cursor was written inside the release and every deploy discarded it,
            # silently restarting the backfill from the head each time.
            os.environ.setdefault("SNIPER_STATE_ROOT", os.path.abspath(_root))
            break
except Exception:
    pass

from sniper.execution import evm_v4_quote as V           # noqa: E402
from sniper.execution import evm_wallet as W             # noqa: E402
from sniper.persistence import db_memecoin as M          # noqa: E402
from sniper.util.state_paths import state_path           # noqa: E402

BLOCKS_PER_S = 9.91          # measured on 4663, 2026-09-03


def _state_file():
    return state_path("data", "memecoin_v4_poolkey_index.json")


def _load_state():
    try:
        with open(_state_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, str(path))       # atomic: a killed run never leaves a half-written cursor


def _index_rpc_url(chain, cfg):
    """The archive/on-chain reader, NEVER the funded custody endpoint."""
    for url in (V._archive_rpc_urls(chain, None) or []):
        if url:
            return url
    return getattr(cfg, "MEMECOIN_ONCHAIN_EVM_RPC_URL_%s" % chain, None)


def _rpc(url, method, params, timeout=60):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    status, payload, _h = W._http("POST", url, body=body, timeout=timeout)
    if not isinstance(payload, dict) or "result" not in payload:
        raise RuntimeError("rpc %s: %s" % (method, str(payload)[:160]))
    return payload["result"]


def index_page(conn, *, chain, url, pool_manager, from_block, to_block, verbose=False):
    """One page. Returns (stored, seen, rejected)."""
    logs = _rpc(url, "eth_getLogs", [{
        "address": pool_manager, "topics": [V._INIT_TOPIC],
        "fromBlock": hex(from_block), "toBlock": hex(to_block)}])
    stored = rejected = 0
    for log in logs:
        topics = log.get("topics") or []
        pool_id = str(topics[1]).lower() if len(topics) > 1 else ""
        if not V._is_v4_pool_id(pool_id):
            rejected += 1
            continue
        key = V.decode_initialize_log(log)
        # HASH IT BACK. A V4 pool id IS the keccak of its PoolKey, so this proves the decode
        # matches what the quote path would compute. Anything else is dropped.
        if key is None or "0x" + V.keccak(bytes.fromhex(V._encode_pool_key(key))).hex() != pool_id:
            rejected += 1
            continue
        M.put_evm_v4_pool_key(conn, chain_id=str(chain), pool_id=pool_id,
                              pool_manager=pool_manager, key=key)
        stored += 1
    conn.commit()
    if verbose:
        print("  [%d,%d] %d logs -> %d stored, %d rejected"
              % (from_block, to_block, len(logs), stored, rejected))
    return stored, len(logs), rejected


# WANTED KEYS (2026-10-07, Alpha GO). A live key walk that answered every page empty without the full
# range (evm_v4_quote._KEY_SCAN_EXHAUSTED_TTL_S) appends the pool id to the chain's wanted list. Those
# pools are older than any window this script backfills (4663 0x6444a8e0 at block 18.6M, 0x8674c1c5 at
# 37.3M), so each is read by its OWN Initialize topic, walking back from the head in `window`-block
# reads; the 4663 archive host served 10M blocks per topic-filtered read on 2026-10-07. Progress per
# id survives runs (its own state file, not the backfill cursor's), and every run spends at most
# `max_calls` reads on it. A found key is hash-checked and stored like any other.
WANTED_WINDOW_4663 = 10_000_000


def _wanted_state_file(chain):
    return state_path("data", "v4_poolkey_wanted_%s.json" % chain)


def _take_wanted(chain):
    """Ids from the chain's wanted list. The file is renamed before it is read, so a line the runner
    appends meanwhile starts a new file; a line caught mid-write is lost and comes back on the next
    walk that runs out (the runner's memo lasts 30 minutes)."""
    path = V.wanted_pool_keys_path(chain)
    taken = str(path) + ".taken"
    try:
        os.replace(str(path), taken)
    except FileNotFoundError:
        return []
    ids = []
    try:
        with open(taken) as f:
            for line in f:
                pid = (line.split() or [""])[0].lower()
                if V._is_v4_pool_id(pid) and pid not in ids:
                    ids.append(pid)
    finally:
        os.unlink(taken)
    return ids


WANTED_PACE_S = 1.0       # between wanted reads: the 4663 host is the money path's too (Alpha, 2026-10-07)


def resolve_wanted(conn, *, chain, url, pool_manager, head, window, max_calls, pace_s=WANTED_PACE_S,
                   verbose=False):
    """(stored, calls, refusal) after reading wanted ids by their own Initialize topic. Never raises
    for a read. Every read after the first waits `pace_s`; the first refusal (a 429 included) ends
    the run: `refusal` names it and `run()` reads nothing more (review EVM02, 2026-10-08). An answer
    that is not a list is a refusal, never an empty range."""
    path = _wanted_state_file(chain)
    try:
        with open(path) as f:
            wanted = json.load(f)
    except Exception:
        wanted = {}
    for pid in _take_wanted(chain):
        wanted.setdefault(pid, {"hi": None, "since": int(time.time())})
    stored = calls = 0
    refused = None
    # THE TAKEN IDS LIVE ONLY IN `wanted` NOW (_take_wanted unlinked the runner's file), so the state
    # write runs whatever escapes the loop (Gamma on 0c403758a): a storage or parse error keeps
    # every id and its `hi` progress, and still propagates to run().
    try:
        for pid in sorted(wanted, key=lambda k: wanted[k].get("since") or 0):
            if calls >= max_calls or refused is not None:
                break
            if conn.execute("SELECT 1 FROM memecoin_evm_v4_pool_keys WHERE network_id=? AND pool_id=?",
                            (str(chain), pid)).fetchone():
                del wanted[pid]                # stored meanwhile (tape, live read): nothing to do
                continue
            hi = wanted[pid].get("hi")
            hi = head if hi is None else int(hi)
            while calls < max_calls and pid in wanted:
                lo = max(0, hi - window + 1)
                if calls:
                    time.sleep(pace_s)
                calls += 1
                try:
                    logs = _rpc(url, "eth_getLogs", [{
                        "address": pool_manager, "topics": [V._INIT_TOPIC, pid],
                        "fromBlock": hex(lo), "toBlock": hex(hi)}])
                except Exception as exc:
                    refused = str(exc)[:120]
                else:
                    if not isinstance(logs, list):
                        refused = "malformed eth_getLogs answer: %r" % (logs,)
                if refused is not None:
                    print("  wanted %s [%d,%d] refused, will retry: %s" % (pid[:12], lo, hi, refused))
                    break                      # a refusal says nothing about the range: stop this run
                for log in logs:
                    key = V.decode_initialize_log(log)
                    if key is not None and "0x" + V.keccak(bytes.fromhex(V._encode_pool_key(key))).hex() == pid:
                        M.put_evm_v4_pool_key(conn, chain_id=str(chain), pool_id=pid,
                                              pool_manager=pool_manager, key=key)
                        conn.commit()
                        stored += 1
                        del wanted[pid]
                        print("  wanted %s found at block %s" % (pid[:12], int(str(log.get("blockNumber")), 0)
                                                                if log.get("blockNumber") else "?"))
                        break
                if pid not in wanted:
                    break
                if lo == 0:
                    print("  wanted %s: no Initialize back to block 0, dropped" % pid[:12])
                    del wanted[pid]
                    break
                hi = lo - 1
                wanted[pid]["hi"] = hi
    finally:
        tmp = str(path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(wanted, f, indent=1, sort_keys=True)
        os.replace(tmp, str(path))
    if verbose or stored or wanted:
        print("wanted keys: stored %d | reads %d | still wanted %d" % (stored, calls, len(wanted)))
    return stored, calls, refused


def run(chain="4663", *, days=7.0, page=200_000, max_pages=40, sleep_s=0.4, verbose=False,
        wanted_window=None, wanted_calls=4, wanted_pace_s=WANTED_PACE_S):
    import sniper.settings.memecoin as cfg
    from sniper.persistence.db_core import get_connection

    conf = V.V4.get(str(chain))
    if not conf:
        print("no V4 config for chain %s" % chain)
        return 1
    url = _index_rpc_url(str(chain), cfg)
    if not url:
        print("no archive/on-chain RPC configured for chain %s" % chain)
        return 1
    pm = conf["pool_manager"]
    head = int(str(_rpc(url, "eth_blockNumber", [])), 0)
    floor = max(0, head - int(days * 86400 * BLOCKS_PER_S))

    state = _load_state()
    span = state.get(str(chain)) or {}
    low = int(span.get("low") or head)
    high = int(span.get("high") or head)
    print("chain %s via %s | head %d | indexed [%d,%d] | floor %d"
          % (chain, url.split("/")[2], head, low, high, floor))

    conn = get_connection()
    totals = [0, 0, 0]
    pages = 0
    try:
        if wanted_calls > 0:
            window = wanted_window or (WANTED_WINDOW_4663 if str(chain) == "4663" else page)
            refused = None
            try:
                _s, _c, refused = resolve_wanted(
                    conn, chain=chain, url=url, pool_manager=pm, head=head, window=window,
                    max_calls=wanted_calls, pace_s=wanted_pace_s, verbose=verbose)
            except Exception as exc:       # a storage or parse error never costs the backfill its run
                print("  wanted keys step failed, will retry next run: %s" % str(exc)[:120])
            if refused is not None:        # the provider refused: no more reads this run
                print("provider refused a wanted-key read; tail and backfill wait for the next run")
                return 0
        # TAIL FIRST: new pools are the ones an entry is most likely to need next.
        while high < head and pages < max_pages:
            to_block = min(head, high + page)
            try:
                s, n, r = index_page(conn, chain=chain, url=url, pool_manager=pm,
                                     from_block=high + 1, to_block=to_block, verbose=verbose)
            except Exception as exc:
                print("  tail page failed, will retry next run: %s" % str(exc)[:120])
                break
            totals = [totals[0] + s, totals[1] + n, totals[2] + r]
            high = to_block
            pages += 1
            _save_state({**state, str(chain): {"low": low, "high": high}})
            time.sleep(sleep_s)
        # THEN WALK BACK toward the floor.
        while low > floor and pages < max_pages:
            from_block = max(floor, low - page)
            try:
                s, n, r = index_page(conn, chain=chain, url=url, pool_manager=pm,
                                     from_block=from_block, to_block=low - 1, verbose=verbose)
            except Exception as exc:
                print("  backfill page failed, will retry next run: %s" % str(exc)[:120])
                break
            totals = [totals[0] + s, totals[1] + n, totals[2] + r]
            low = from_block
            pages += 1
            _save_state({**state, str(chain): {"low": low, "high": high}})
            time.sleep(sleep_s)
        rows = conn.execute(
            "SELECT COUNT(*) FROM memecoin_evm_v4_pool_keys WHERE network_id=?",
            (str(chain),)).fetchone()[0]
    finally:
        conn.close()
    print("pages %d | logs %d | stored %d | rejected %d | cache now %d rows | indexed [%d,%d]"
          % (pages, totals[1], totals[0], totals[2], rows, low, high))
    remaining = max(0, low - floor)
    if remaining:
        print("%d blocks (%.1f h) still to backfill; run again to continue"
              % (remaining, remaining / BLOCKS_PER_S / 3600))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chain", default="4663")
    ap.add_argument("--days", type=float, default=7.0, help="how far back to backfill")
    ap.add_argument("--page", type=int, default=200_000, help="blocks per read")
    ap.add_argument("--max-pages", type=int, default=40, help="bound one run")
    ap.add_argument("--sleep", type=float, default=0.4, help="pause between reads")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--wanted-window", type=int, default=None,
                    help="blocks per wanted-key read (default 10M on 4663, else --page)")
    ap.add_argument("--wanted-calls", type=int, default=4,
                    help="reads per run for wanted keys; 0 skips them (4: the 4663 host is shared)")
    ap.add_argument("--wanted-pace", type=float, default=WANTED_PACE_S,
                    help="seconds between wanted reads")
    a = ap.parse_args()
    return run(a.chain, days=a.days, page=a.page, max_pages=a.max_pages,
               sleep_s=a.sleep, verbose=a.verbose, wanted_window=a.wanted_window,
               wanted_calls=a.wanted_calls, wanted_pace_s=a.wanted_pace)


if __name__ == "__main__":
    raise SystemExit(main())
