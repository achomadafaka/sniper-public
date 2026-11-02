#!/usr/bin/env python3
"""Inspect Axiom identity candidates or verify them with bounded public RPC reads.

The default mode reads the database only. --verify performs public RPC reads.
--confirm also writes proof rows. The command never changes wallet authority.

Scheduled hourly with --j7 (P1, Alpha GO 2026-09-29). A --j7 run reads receipts only from a host
whose budget no other role uses (`taken_budgets`: every env value, the resolved EVM read pools and the
free-submission endpoints), and refuses to run when the chain's custody URL is not in the environment,
because then nothing proves the host is not custody. `.env` is loaded from the state root first, so the pools
resolve as the funded runner resolves them.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402
load_env_files(str(state_root()), names=(".env",))                  # before any settings import

from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av
from sniper.util.clock import real_utc_now_iso


def bounded_rpc(call, *, max_calls, max_seconds, min_interval_s=0.0, clock=time.monotonic,
                sleep=time.sleep, stop_on_error=False):
    """Stop requests at the count or time limit. An expired budget remains unknown. Requests start
    at least `min_interval_s` apart, so a scheduled run cannot burst a shared public host.
    `stop_on_error` (a public host, operator 2026-10-14): after the first read that answers an
    error (transport, HTTP or JSON-RPC), every later read is refused unread. A null result is an
    answer (unknown), not an error."""
    deadline = clock() + max_seconds
    calls, last, stopped = 0, None, False

    def read(*args):
        nonlocal calls, last, stopped
        if stopped:
            return None, "rpc_stopped_after_error"
        if last is not None and min_interval_s > 0:
            wait = last + min_interval_s - clock()
            if wait > 0:
                sleep(wait)
        if calls >= max_calls or clock() >= deadline:
            return None, "rpc_budget_exhausted"
        calls += 1
        last = clock()
        result, error = call(*args)
        if error is not None and stop_on_error:
            stopped = True
        return result, error
    read.calls = lambda: calls
    read.stopped = lambda: stopped
    return read


def j7_read_url(chain, url):
    """`url` (default: the chain's EVM_RPC) when its domain is used by no other role, else an error
    string. A chain whose custody URL is not in the environment is refused: the read pool then holds
    only library defaults, and nothing proves the host is not custody."""
    from sniper.services.memecoin_evm_exact_capture import budget, taken_budgets
    from urllib.parse import urlsplit
    url = url or av.EVM_RPC.get(str(chain))
    host = (urlsplit(url).hostname or "").lower() if url else ""
    if not host:
        return None, "no read host for chain %s" % chain
    # A chain with no custody yet (Ink, evm-key-v1) cannot name its own custody URL; there the
    # proof that .env was loaded is another funded chain's custody URL, and taken_budgets below still
    # refuses every configured pool member and free-submission endpoint of the chain.
    # Chain 1 is in the same position until the operator sets MEMECOIN_EVM_RPC_URL_1; from then on
    # its own custody URL is the marker and its domain is refused like any custody.
    own = "MEMECOIN_EVM_RPC_URL_%s" % chain
    markers = (("MEMECOIN_EVM_RPC_URL_%s" % c for c in av.EVM_CHAINS if str(c) != str(chain))
               if str(chain) in av.PRE_CUSTODY_CHAINS and not os.environ.get(own, "").strip()
               else (own,))
    if not any(os.environ.get(name, "").strip() for name in markers):
        return None, ("custody URL for chain %s is not set (.env not loaded?); cannot prove %s is "
                      "not custody" % (chain, host))
    if budget(url) in taken_budgets(os.environ):
        return None, "%s is used by another role (custody, a read pool or submission)" % host
    return url, None


def code_read_url(value):
    """(url, None) or (None, why) for one --code-url value: an env var name or an https URL. An
    evm-key code read uses a RESEARCH host only (rule 0724bc08, Alpha 2026-10-02): refused are a URL
    any other env var holds (custody, the lane hosts, the inline key, another role's key: matched on
    the FULL URL, since the 4663 research key and the lane's own QuickNode keys share a domain), a
    funded read-pool member, a free-submission endpoint, and any host on a
    MEMECOIN_CAPTURE_EVM_READ_URL_* or EVM_RPC default domain. A
    reason never echoes a URL (a key rides in its path)."""
    from urllib.parse import urlsplit
    from sniper.services.memecoin_evm_exact_capture import budget
    from sniper.services.memecoin_rh_fast_lane import safety_host_refusal
    if not any(os.environ.get("MEMECOIN_EVM_RPC_URL_%s" % c, "").strip() for c in av.EVM_CHAINS):
        return None, "no custody URL in the environment (.env not loaded?): cannot prove the host is not custody"
    name = None
    if "://" not in value:
        name, value = value, os.environ.get(value, "").strip()
        if not value:
            return None, "%s is not set" % name
    why = safety_host_refusal(value, os.environ, own_name=name)
    if why:
        return None, why
    banned = {budget(v) for k, v in os.environ.items()
              if k.startswith("MEMECOIN_CAPTURE_EVM_READ_URL_") and urlsplit(v.strip()).hostname}
    banned |= {budget(u) for u in av.EVM_RPC.values()}
    domain = budget(value)
    if domain in banned:
        return None, "%s is a capture or default-RPC domain" % domain
    return value, None


J7_EXTRA_PER_RUN = 5


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path(os.getenv("SNIPER_STATE_ROOT") or REPO) /
                                            "sniper_tracker.db"))
    parser.add_argument("--verify", action="store_true", help="perform public RPC reads")
    parser.add_argument("--confirm", action="store_true", help="verify and write proof rows")
    parser.add_argument("--chain", choices=('4663', '56', '8453', '5042', '1', '57073', 'solana'),
                        help="check only this chain")
    parser.add_argument("--j7", action="store_true",
                        help="verify armed wallets' J7-named EVM addresses by their buy receipts "
                             "(needs --chain 4663, 56 or 8453)")
    parser.add_argument("--rpc-url", help="with --j7: the receipt endpoint for --chain "
                                          "(default: the chain's own public RPC)")
    parser.add_argument("--evm-key", action="store_true",
                        help="prove --chain addresses (Ink, or a chain in MEMECOIN_EVM_KEY_PROOF_CHAINS) "
                             "from direct proofs on the other EVM chains and eth_getCode reads")
    parser.add_argument("--code-url", action="append", default=[], metavar="CHAIN=VAR_OR_URL",
                        help="with --evm-key or --j7, repeatable: the read host of CHAIN (the target or "
                             "a basis chain), an env var name (never printed) or an https URL; "
                             "research hosts only. A basis chain without one is recorded unread")
    parser.add_argument("--any-evm-armed", action="store_true",
                        help="with --j7: also verify wallets armed on another EVM chain that have J7 "
                             "claims here and no proof here yet (at most J7_EXTRA_PER_RUN a run)")
    parser.add_argument("--rpc-url-env", help="with --j7: the environment variable holding the "
                                              "receipt endpoint (never printed)")
    parser.add_argument("--min-interval-s", type=float, default=0.0,
                        help="at least this long between RPC requests")
    parser.add_argument("--summary", action="store_true",
                        help="print one line: chain, host, written, status counts, rpc calls")
    parser.add_argument("--wallets", help="with --j7: also prove these wallets (source_user_id or "
                                          "handle, comma list) though not armed on --chain. Proof only: "
                                          "arming stays the operator's (chain 1 before it is funded)")
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--max-events", type=int, default=5)
    parser.add_argument("--max-rpc-calls", type=int, default=300)
    parser.add_argument("--stop-on-error", action="store_true",
                        help="stop every read after the first failed one (a public receipt host)")
    parser.add_argument("--max-seconds", type=float, default=120)
    args = parser.parse_args(argv)
    for value, maximum, name in [(args.max_candidates, 50, "max-candidates"),
                                  (args.max_events, 30, "max-events"),
                                  (args.max_rpc_calls, 1000, "max-rpc-calls"),
                                  (args.max_seconds, 600, "max-seconds")]:
        if not 0 < value <= maximum:
            parser.error(f"--{name} must be greater than zero and at most {maximum}")
    if args.wallets and not args.j7:
        parser.error("--wallets needs --j7")
    read_url, code_urls = None, {}
    if args.code_url and not (args.evm_key or args.j7):
        parser.error("--code-url needs --evm-key or --j7")
    for item in args.code_url:
        chain, sep, value = item.partition("=")
        chain = chain.strip()
        if not sep or chain not in av.EVM_CHAINS + av.EVM_KEY_CHAINS + ('5042',):
            parser.error("--code-url takes CHAIN=VAR_OR_URL with CHAIN one of %s" % ", ".join(
                av.EVM_CHAINS + av.EVM_KEY_CHAINS + ('5042',)))
        code_urls[chain], why = code_read_url(value.strip())
        if why:
            parser.error("refusing --code-url %s: %s" % (chain, why))
    # EACH NAMED HOST SERVES ITS CHAIN (Gamma M2, 2026-10-02): a swapped mapping would read another
    # chain's state, where these addresses mostly answer "0x", a key read. One eth_chainId per host
    # before any other read; a mismatch or no answer refuses the run.
    # Every read on the operator's keyed dRPC account takes its ONE cross-process pacer (Alpha
    # 2026-10-02): ~120 requests per rolling 60 s across every network and every process.
    from sniper.services.memecoin_research_pacer import drpc_paced
    http_call = drpc_paced(av._http_call)
    for chain, url in code_urls.items():
        result, error = http_call(url, "eth_chainId", [])
        try:
            answered = int(str(result), 16) if error is None else None
        except (TypeError, ValueError):
            answered = None
        if answered != int(chain):
            parser.error("refusing --code-url %s: its host answers eth_chainId %s" % (
                chain, answered if answered is not None else "nothing"))
    if args.evm_key and args.chain not in av.evm_key_chains():
        parser.error("--evm-key needs --chain 57073, or a chain listed in MEMECOIN_EVM_KEY_PROOF_CHAINS")
    if args.j7 or args.evm_key:            # refused before the ledger is opened or any host is asked
        if args.j7 and args.chain not in av.EVM_CHAINS:
            parser.error("--j7 needs --chain %s" % ", ".join(av.EVM_CHAINS))
        if args.chain in code_urls:
            read_url = code_urls[args.chain]              # already passed the research-host guard
        else:
            wanted = args.rpc_url
            if args.rpc_url_env:
                wanted = os.environ.get(args.rpc_url_env, "").strip()
                if not wanted:
                    parser.error("%s is not set" % args.rpc_url_env)
            read_url, why = j7_read_url(args.chain, wanted)
            if why:
                parser.error("refusing chain %s: %s" % (args.chain, why))
    path = Path(args.db).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if not (args.verify or args.confirm):
            candidates = [c for c in av._fan_out(av._candidates(conn))
                          if args.chain is None or c['chain'] == args.chain]
            print(json.dumps({"mode": "candidates", "candidates":
                              candidates[:args.max_candidates]}, indent=2))
            return
        expected = proof_snapshot(conn)
        call = bounded_rpc(http_call, max_calls=args.max_rpc_calls,
                           max_seconds=args.max_seconds, min_interval_s=args.min_interval_s,
                           stop_on_error=args.stop_on_error)
        if args.evm_key:
            # Code reads only on the research hosts named by --code-url (rule 0724bc08, Alpha
            # 2026-10-02: never a CAPTURE host, never EVM_RPC's defaults), the target's possibly
            # by --rpc-url. A basis chain with no host is never asked and reads unread.
            code_urls = dict(code_urls, **{args.chain: read_url})

            def code_fn(chain, address):
                url = code_urls.get(str(chain))
                if not url:
                    return av.NO_HOST                     # never asked: 'unread', not a failed read
                result, error = call(url, "eth_getCode", [address, "latest"])
                return result if error is None and isinstance(result, str) else None
            results = av.verify_evm_key_addresses(conn, chain=args.chain, code_fn=code_fn)
        elif args.j7:
            from sniper.persistence import db_memecoin as m
            from sniper.services.memecoin_chains import CHAINS
            conn.row_factory = sqlite3.Row          # real_approved_user_ids reads by column name
            # ARMED ON ANY EVM CHAIN, OPT-IN (--any-evm-armed; Alpha, operator yes 2026-10-02). A wallet
            # proven only on 56
            # (LevisNFT and four more) has its own J7 swaps on 4663, but only wallets armed on THIS
            # chain were candidates, so it never got a 4663 proof and evm-key had no 4663 basis to
            # prove it on Base. A proof records an address and arms nothing: the watcher subscribes
            # an address only on a chain where its owner is armed (evm_watched_owners).
            evm_chains = [c["network_ids"][0] for c in CHAINS if c["key"] not in ("solana", "unresolved")]
            here = m.real_approved_user_ids(conn, network_id=args.chain)
            elsewhere = (set().union(*(m.real_approved_user_ids(conn, network_id=c) for c in evm_chains))
                         - here) if args.any_evm_armed else set()
            conn.row_factory = None
            # A wallet armed elsewhere is read only until it holds a proof here, at most
            # J7_EXTRA_PER_RUN a run, so a backlog never bursts its read host (Alpha). Without the
            # flag the candidate set is exactly the wallets armed on this chain, as before.
            # Gamma MEDIUM on 47b67156c: cap only wallets with J7 claims here, drop a terminal
            # SHARED/REFUTED row on this chain, and take the least recently tried first
            # (never-tried first), so no wallet that cannot prove holds a slot every run.
            proven = {str(u) for (net, _a), (u, _h) in av.watcher_address_owners(conn).items()
                      if str(net) == args.chain}
            claimants = {str(c[0]) for c in av._j7_claims(conn, args.chain)}
            tried = {str(r[0]): (r[1], r[2] or "") for r in conn.execute(
                "SELECT source_user_id, verify_status, verified_at FROM axiom_verified_addresses "
                "WHERE network_id=?", (args.chain,))}
            terminal = {u for u, (status, _at) in tried.items() if status in (av.REFUTED, av.SHARED)}
            extra = sorted((set(map(str, elsewhere)) & claimants) - proven - terminal,
                           key=lambda u: (tried.get(u, (None, ""))[1], u))
            armed = here | set(extra[:J7_EXTRA_PER_RUN])
            if args.wallets:
                named = [w.strip() for w in args.wallets.split(",") if w.strip()]
                known = {}
                for uid, handle in conn.execute("SELECT source_user_id, handle FROM memecoin_wallets"):
                    known[str(uid).lower()] = str(uid)
                    if handle:
                        known.setdefault(str(handle).lower(), str(uid))
                missing = [w for w in named if w.lower() not in known]
                if missing:
                    parser.error("unknown --wallets: %s" % ", ".join(missing))
                armed |= {known[w.lower()] for w in named}
            read = av.evm_receipt_reader(call_fn=call, urls={args.chain: read_url})
            results = av.verify_j7_addresses(
                conn, receipt_fn=read, chain=args.chain, armed=armed,
                code_fn=av.evm_code_reader(call_fn=call, urls={args.chain: read_url}))
        else:
            read = av.public_rpc_reader(call_fn=call)
            results = av.verify_candidates(conn, rpc_read_fn=read, now_iso=real_utc_now_iso,
                                            max_events=args.max_events,
                                            max_candidates=args.max_candidates,
                                            write_results=False, chain=args.chain)
    finally:
        conn.close()
    written = 0
    if args.confirm:
        # Remote reads finish before this short write connection opens.
        for result in results:
            result["detail"] = av._detail(result)
        write = sqlite3.connect(path, timeout=2)
        try:
            written = save_verification_results(write, results, expected=expected,
                                                 now=real_utc_now_iso())
        finally:
            write.close()
    mode = "confirm" if args.confirm else "verify"
    if args.summary:
        from collections import Counter
        from urllib.parse import urlsplit
        status = Counter("%s/%s" % (r["verify_status"], r.get("proof_version"))
                         + ("/%s" % r["skip_reason"] if r.get("skip_reason") else "") for r in results)
        print(json.dumps({"mode": mode, "chain": args.chain, "written": written,
                          "host": urlsplit(read_url).hostname if read_url else None,
                          "rpc_calls": call.calls(), "status": dict(sorted(status.items())),
                          **({"stopped_after_error": True} if call.stopped() else {})}))
        return
    print(json.dumps({"mode": mode, "written": written, "results": results}, indent=2))


if __name__ == "__main__":
    main()
