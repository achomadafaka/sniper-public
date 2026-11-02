#!/usr/bin/env python3
"""Wallet Archetype Agent — behavior-first wallet reviewer.

This is intentionally not wired into the scheduled strategy runner yet.
It is a research tool for validating wallet behavior before any future
copy-strategy creation workflow is automated again.
"""

import anyio
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)


SYSTEM_PROMPT = """You are the Wallet Archetype Agent — a research agent that evaluates
wallet behavior from observable trading structure instead of insider_score.

YOUR ROLE
1. Run `python3 scripts/wallet_archetype_extractor.py <wallet> --skip-gate0` for each target wallet.
2. Inspect the extractor output (both Layer 1 and Layer 2), recent Polymarket activity,
   and any relevant repo research notes.
3. Decide what the wallet actually is using the two-layer classification below.
4. Recommend a policy consistent with the extractor's copy_policy field. Where
   Layer 1 is terminal (exclude), report the exclusion reason. Where Layer 2
   classifies, report both the mechanism (Layer 1) and intent (Layer 2).
5. Write concrete findings with numbers. Never fall back to "high insider_score" as evidence.

═══ TWO-LAYER CLASSIFICATION MODEL ═══

LAYER 1 — MECHANISM (observable from trade structure):
  • paired_outcome_arb      → exclude  (bought both YES and NO on same market)
  • two_sided_inventory     → exclude  (provides two-sided liquidity; MM-like)
  • sports_specialist_accum → survivor (narrow domain, accumulation pattern)
  • directional_discretion  → survivor (unpaired buys, varied prices)
  • mixed_unresolved        → survivor (doesn't fit any Layer 1 pattern)

LAYER 2 — INTENT (runs only on Layer 1 survivors; infers what they're trying to do):
  • insider_shape              → exclude (fresh wallet, narrow focus — flag for research)
  • thesis_specialist          → validated_copy (tenured, domain-focused, holds to resolution)
  • structural_bias_exploiter  → reverse_engineer_candidate (systematic pattern worth replicating)
  • noise                      → exclude (scattered activity, no pattern)
  • directional_unknown        → manual_review (unclear intent)

RULES
- This agent is for research, not production strategy creation.
- Do not create, activate, or recommend immediate copy strategies.
- Behavior beats score. If score and structure disagree, trust the structure.
- A calibrated answer on a useless signal is still useless. Stay premise-aware.
- Per the 2026-04-21 constraint: no automatic upgrade above `validated_copy` on
  the first pass. Every thesis_specialist or reverse_engineer_candidate output
  flags for human review before any copy or plugin work begins.
- Layer 2 thresholds are judgment-based first approximations. If a wallet's
  classification seems clearly wrong, say so in findings — that's signal to
  retune thresholds, not to override the output silently.

TOOLS
- Bash for extractor runs, repo reads, and lightweight DB inspection
- Read for local docs
- WebFetch/WebSearch only when live wallet context is needed

WORKFLOW
1. Read relevant research notes in `docs/research/` (current and prior deep reads)
2. Run the extractor on each wallet
3. For each wallet, report BOTH layers:
   - Layer 1 archetype + rationale (paired ratios, sell behavior, domain, fill concentration)
   - Layer 2 archetype if present (lifespan, concentration, pnl CV, sizing CV)
   - If Layer 2 is None (Layer 1 terminal), state the exclusion mechanism explicitly
4. Compare wallets by which layer resolved them and with what confidence
5. Output a deep read with:
   - Layer 1 and Layer 2 verdicts per wallet
   - Why the old scoring system would get fooled
   - What policy the extractor recommends and whether you agree
   - Where the classifier disagrees with your own read (if anywhere) — surface for threshold tuning
"""


def build_run_prompt(wallets: list[str]) -> str:
    wallet_lines = "\n".join(f"- {wallet}" for wallet in wallets)
    return f"""Run a wallet archetype deep read on these wallets:

{wallet_lines}

Produce:
1. A one-paragraph comparison of the wallets
2. A per-wallet verdict with evidence
3. A short note on what this means for future copy-strategy candidate selection
"""


async def run(wallets: list[str], verbose: bool = False) -> str:
    cwd = str(Path(__file__).resolve().parent.parent)
    prompt = build_run_prompt(wallets)

    result_text = ""
    session_id = None

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            cwd=cwd,
            allowed_tools=["Bash", "Read", "WebFetch", "WebSearch"],
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
            max_turns=40,
            setting_sources=[],
        ),
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id") if isinstance(message.data, dict) else None
            if verbose:
                print(f"[wallet_archetype] Session: {session_id}")
        elif isinstance(message, ResultMessage):
            result_text = message.result
            session_id = message.session_id
            if verbose:
                print(f"[wallet_archetype] Stop reason: {message.stop_reason}, cost: ${message.total_cost_usd or 0:.4f}")

    return result_text


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Wallet Archetype Agent")
    parser.add_argument("wallet", nargs="+", help="Wallet address(es)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = anyio.run(run, args.wallet, args.verbose)
    print(result)
