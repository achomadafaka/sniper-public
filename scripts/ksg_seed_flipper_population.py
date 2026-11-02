#!/usr/bin/env python3
"""FH4 peer-population growth — seed runner.

Builds and optionally executes a batch of run_wallet_protocol.py calls with
--no-persist --persist-flipper-grains to seed flipper-grain data for
under-populated (grain, tag) pairs.

Usage (dry-run):
    python3 scripts/ksg_seed_flipper_population.py \
        --queue-json /tmp/candidates.json \
        --max-wallets 5 --n-iter 500 --max-exec-trades 100

Usage (execute — writes wallet flipper grain rows to DB):
    python3 scripts/ksg_seed_flipper_population.py \
        --wallet 0xaaa --wallet 0xbbb \
        --n-iter 500 --max-exec-trades 100 \
        --execute \
        --i-understand-this-writes-wallet-flipper-tag-grain
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def build_seed_commands(*, wallets, n_iter, max_exec_trades):
    """Build the list of subprocess commands to seed flipper grains for each wallet.

    Args:
        wallets: Iterable of wallet addresses (strings).
        n_iter: Number of bootstrap iterations to pass to run_wallet_protocol.
        max_exec_trades: If not None, pass --max-exec-trades <n> to the sub-script.

    Returns:
        List of list[str] — each inner list is a complete argv for subprocess.run.
    """
    commands = []
    for wallet in wallets:
        cmd = [
            "python3",
            "scripts/run_wallet_protocol.py",
            wallet.lower(),
            "--no-persist",
            "--persist-flipper-grains",
            "--n-iter",
            str(n_iter),
        ]
        if max_exec_trades is not None:
            cmd.extend(["--max-exec-trades", str(max_exec_trades)])
        commands.append(cmd)
    return commands


def validate_execute_args(*, execute, acknowledged, max_wallets):
    """Guard rails for the execute mode.

    Raises SystemExit(2) if:
    - execute=True and acknowledged=False (explicit write acknowledgment required).
    - execute=True and max_wallets > 10 (hard cap to prevent runaway batch).

    Dry-run (execute=False) is always a no-op regardless of other args.
    """
    if not execute:
        return
    if not acknowledged:
        print(
            "ERROR: --execute requires --i-understand-this-writes-wallet-flipper-tag-grain",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if max_wallets > 10:
        print(
            f"ERROR: max_wallets={max_wallets} exceeds hard cap of 10; reduce --max-wallets",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Seed flipper-grain data for under-populated (grain, tag) pairs.")
    p.add_argument("--queue-json", default=None,
                   help="Path to scored candidates JSON (from ksg_flipper_candidate_score); "
                        "used when --wallet is not provided")
    p.add_argument("--wallet", action="append", dest="wallets", default=[],
                   help="Explicit wallet address to seed (repeatable; overrides --queue-json)")
    p.add_argument("--max-wallets", type=int, default=3,
                   help="Maximum number of wallets to process (default 3, hard cap 10)")
    p.add_argument("--n-iter", type=int, default=500,
                   help="Bootstrap iterations for run_wallet_protocol (default 500)")
    p.add_argument("--max-exec-trades", type=int, default=100,
                   help="Pass --max-exec-trades to run_wallet_protocol (default 100)")
    p.add_argument("--execute", action="store_true", default=False,
                   help="Actually run the commands (default: dry-run, prints commands only)")
    p.add_argument("--i-understand-this-writes-wallet-flipper-tag-grain",
                   action="store_true", dest="acknowledged", default=False,
                   help="Required acknowledgment when --execute is set")
    args = p.parse_args(argv)

    validate_execute_args(
        execute=args.execute,
        acknowledged=args.acknowledged,
        max_wallets=args.max_wallets,
    )

    # Resolve wallet list
    wallets: list[str] = list(args.wallets)
    if not wallets and args.queue_json:
        candidates = json.loads(Path(args.queue_json).read_text())
        wallets = [c["wallet"] for c in candidates[: args.max_wallets]]
    wallets = wallets[: args.max_wallets]

    if not wallets:
        print("ERROR: no wallets to process — provide --wallet or --queue-json", file=sys.stderr)
        return 2

    commands = build_seed_commands(
        wallets=wallets,
        n_iter=args.n_iter,
        max_exec_trades=args.max_exec_trades,
    )

    if not args.execute:
        print("DRY-RUN — commands that would be executed:")
        for cmd in commands:
            print(" ".join(cmd))
        return 0

    # Execute mode — parse the structured persist marker from each
    # child's stdout (see scripts/run_wallet_protocol.py for the contract).
    # Reviewer finding #3: subprocess rc=0 does NOT imply rows were
    # written. The analyzer's persist helper logs and returns on import
    # error / env missing / writer raise, leaving rc=0. We must parse
    # the marker to know the real outcome.
    outcomes: list[dict] = []
    for wallet, cmd in zip(wallets, commands):
        print(f"Running: {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        # Pass through child output so operators still see the live log.
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

        if result.returncode != 0:
            outcomes.append({
                "wallet": wallet, "status": "subprocess_error",
                "detail": f"rc={result.returncode}",
            })
            _emit_seed_summary(outcomes)
            print(
                f"ERROR: command exited {result.returncode}: {' '.join(cmd)}",
                file=sys.stderr,
            )
            return result.returncode

        markers = parse_persist_markers(result.stdout)
        if not markers:
            outcomes.append({
                "wallet": wallet, "status": "marker_missing",
                "detail": "no [PERSIST_FLIPPER_GRAINS_RESULT] line on stdout",
            })
            continue

        marker = markers[-1]
        status = marker.get("status", "unknown")
        rows_written = marker.get("rows_written")
        # Defensive layer: even if the upstream helper claims status=wrote,
        # require rows_written > 0. Either the upstream invariant fails
        # (caught here) or a future contract drift sneaks zero-row writes
        # past the helper. Reclassify malformed wrote markers as failures
        # rather than counting them as success.
        if status == "wrote" and not (isinstance(rows_written, int) and rows_written > 0):
            outcomes.append({
                "wallet": wallet,
                "status": "wrote_zero_rows",
                "detail": (
                    f"marker claimed wrote but rows_written={rows_written!r} "
                    "(expected positive int)"
                ),
            })
            continue
        outcomes.append({
            "wallet": wallet,
            "status": status,
            "detail": (
                marker.get("detail")
                or marker.get("skipped_reason")
                or f"rows_written={rows_written}"
            ),
        })

    _emit_seed_summary(outcomes)
    failures = [
        o for o in outcomes
        if o["status"] not in {"wrote", "skipped"}
    ]
    return 1 if failures else 0


PERSIST_FLIPPER_GRAINS_MARKER_PREFIX = "[PERSIST_FLIPPER_GRAINS_RESULT]"


def parse_persist_markers(stdout: str) -> list[dict]:
    """Extract all `[PERSIST_FLIPPER_GRAINS_RESULT] {...}` marker lines
    from a child's stdout and parse their JSON payloads. Lines that
    look like markers but don't parse are silently dropped (operator
    sees the raw stdout passthrough)."""
    out: list[dict] = []
    for line in stdout.splitlines():
        if not line.startswith(PERSIST_FLIPPER_GRAINS_MARKER_PREFIX):
            continue
        payload_text = line[len(PERSIST_FLIPPER_GRAINS_MARKER_PREFIX):].strip()
        try:
            out.append(json.loads(payload_text))
        except json.JSONDecodeError:
            continue
    return out


def _emit_seed_summary(outcomes: list[dict]) -> None:
    """Print a one-line-per-wallet end-of-run summary to stderr so the
    operator can see exactly which wallets persisted, skipped, or
    failed without re-parsing the marker stream."""
    print("", file=sys.stderr)
    print("=== seed runner per-wallet summary ===", file=sys.stderr)
    for o in outcomes:
        print(
            f"  {o['wallet']}: {o['status']}"
            + (f" — {o['detail']}" if o.get("detail") else ""),
            file=sys.stderr,
        )
    counts = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    print(
        f"=== totals: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))} ===",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
