"""Walk-forward backtesting harness for the Sniper trading system.

Tests the finance analyst's ability to predict market outcomes using historical
news data frozen at a given date, then scores predictions against resolved
market outcomes.

Usage:
    python3 scripts/backtest_analyst.py --date 2026-03-17 --num-markets 10
    python3 scripts/backtest_analyst.py --date 2026-03-17 --num-markets 5 --dry-run
    python3 scripts/backtest_analyst.py --show-last
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import uuid

# Ensure project root is on path so `from db import get_connection` works
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_connection  # noqa: E402

DB_PATH = "/home/sniper/sniper/sniper_tracker.db"

BACKTEST_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    freeze_date TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    market_title TEXT,
    predicted_outcome TEXT,
    confidence REAL,
    reasoning TEXT,
    actual_outcome TEXT,
    resolved_price REAL,
    brier_score REAL,
    correct INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


# ---------------------------------------------------------------------------
# Step 1 — CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Walk-forward backtesting harness for Sniper finance analyst"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Freeze date YYYY-MM-DD (default: earliest date in news_items)",
    )
    parser.add_argument(
        "--num-markets",
        type=int,
        default=10,
        help="Number of markets to test (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write results to the database",
    )
    parser.add_argument(
        "--show-last",
        action="store_true",
        help="Print the most recent backtest run and exit",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Step 2 — Frozen news context
# ---------------------------------------------------------------------------

def build_frozen_news(conn, freeze_date: str) -> str:
    """Query news_items published before T+1 day, most recent first, limit 50."""
    # T+1 day so that news published on T itself is included
    t_plus_one = (
        datetime.datetime.strptime(freeze_date, "%Y-%m-%d") + datetime.timedelta(days=1)
    ).strftime("%Y-%m-%d")

    rows = conn.execute(
        """
        SELECT published_at, source, title, description
        FROM news_items
        WHERE published_at < ?
        ORDER BY published_at DESC
        LIMIT 50
        """,
        (t_plus_one,),
    ).fetchall()

    if not rows:
        return "(no news items found before this date)"

    lines = []
    for row in rows:
        pub = row["published_at"] or ""
        src = row["source"] or ""
        title = row["title"] or ""
        desc = (row["description"] or "")[:200].replace("\n", " ")
        lines.append(f"{pub} | {src} | {title} — {desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3 — Find testable markets
# ---------------------------------------------------------------------------

def find_testable_markets(conn, freeze_date: str, num_markets: int) -> list[dict]:
    """
    Find binary markets that:
    - had a price snapshot within ±2 days of freeze_date
    - resolved AFTER freeze_date (so the analyst couldn't know the outcome)
    - are not h2h (sports book) markets
    """
    t_dt = datetime.datetime.strptime(freeze_date, "%Y-%m-%d")
    t_unix = int(t_dt.timestamp())
    t_low = (t_dt - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    t_high = (t_dt + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

    # Find snapshots near T, excluding h2h markets
    snap_rows = conn.execute(
        """
        SELECT condition_id, title, outcome, price, captured_at, market_type
        FROM poly_price_snapshots
        WHERE captured_at >= ?
          AND captured_at <= ?
          AND (market_type IS NULL OR market_type != 'h2h')
          AND (outcome = 'yes' OR outcome = 'Yes' OR outcome = 'YES')
        ORDER BY ABS(julianday(captured_at) - julianday(?))
        """,
        (t_low, t_high, freeze_date),
    ).fetchall()

    if not snap_rows:
        return []

    # Gather condition_ids that have a resolved outcome AFTER T
    seen_condition_ids: set[str] = set()
    markets: list[dict] = []

    for snap in snap_rows:
        cid = snap["condition_id"]
        if cid in seen_condition_ids:
            continue

        # Check for resolution after T
        outcome_row = conn.execute(
            """
            SELECT outcome, resolved_price, resolved_at
            FROM market_outcomes
            WHERE condition_id = ?
              AND outcome IN ('yes', 'Yes', 'YES', 'no', 'No', 'NO')
              AND resolved_at > ?
            ORDER BY resolved_at ASC
            LIMIT 1
            """,
            (cid, t_unix),
        ).fetchone()

        if outcome_row is None:
            continue

        seen_condition_ids.add(cid)
        markets.append(
            {
                "condition_id": cid,
                "title": snap["title"],
                "price_yes": snap["price"],
                "captured_at": snap["captured_at"],
                "actual_outcome": outcome_row["outcome"].lower(),
                "resolved_price": float(outcome_row["resolved_price"]),
                "resolved_at": outcome_row["resolved_at"],
            }
        )

        if len(markets) >= num_markets:
            break

    return markets


# ---------------------------------------------------------------------------
# Step 4 — Build prompt and call Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a prediction market analyst with access to a time machine. "
    "You must predict market outcomes based ONLY on the news provided. "
    "Do not use any knowledge of events after the stated date. "
    "Respond with valid JSON only — no markdown, no explanation outside JSON."
)


def build_user_prompt(freeze_date: str, news_block: str, markets: list[dict]) -> str:
    market_lines = []
    for m in markets:
        market_lines.append(
            f"Market ID: {m['condition_id']} | "
            f"Question: {m['title']} | "
            f"Current price YES: {m['price_yes']:.3f} | "
            f"Outcome: yes or no?"
        )

    markets_text = "\n".join(market_lines)

    return f"""The date is {freeze_date}. Based ONLY on the news below, predict each market outcome.

=== RECENT NEWS (as of {freeze_date}) ===
{news_block}

=== MARKETS TO PREDICT ===
{markets_text}

Respond with JSON in this exact format:
{{
  "predictions": [
    {{
      "condition_id": "...",
      "predicted_outcome": "yes or no",
      "confidence": 0.0,
      "reasoning": "brief explanation"
    }}
  ]
}}

Rules:
- predicted_outcome must be exactly "yes" or "no"
- confidence is your probability that the predicted_outcome is correct (0.0 to 1.0)
- Use only the news above; do not use knowledge of events after {freeze_date}
- Include one entry per market
"""


def call_claude(system_prompt: str, user_prompt: str) -> dict | None:
    """Invoke Claude CLI and parse JSON response."""
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found on PATH.", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("ERROR: Claude CLI timed out after 120s.", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"ERROR: Claude CLI exited {result.returncode}", file=sys.stderr)
        print(result.stderr[:500], file=sys.stderr)
        return None

    raw = result.stdout.strip()

    # The CLI may wrap the model response in its own JSON envelope
    # Try outer parse first, then look for the inner "result" key
    try:
        outer = json.loads(raw)
        # If it's the CLI envelope, the model text lives in "result"
        if isinstance(outer, dict) and "result" in outer:
            inner_text = outer["result"]
        elif isinstance(outer, dict) and "predictions" in outer:
            return outer
        else:
            inner_text = raw
    except json.JSONDecodeError:
        inner_text = raw

    # Strip markdown fences if present
    if "```" in inner_text:
        lines = inner_text.splitlines()
        stripped = [
            l for l in lines
            if not l.strip().startswith("```")
        ]
        inner_text = "\n".join(stripped)

    try:
        return json.loads(inner_text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse Claude response as JSON: {exc}", file=sys.stderr)
        print(f"Raw response (first 500 chars): {inner_text[:500]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Step 5 — Score predictions
# ---------------------------------------------------------------------------

def score_predictions(
    predictions: list[dict], markets: list[dict]
) -> list[dict]:
    """Compute Brier score and correctness for each prediction."""
    market_by_cid = {m["condition_id"]: m for m in markets}
    scored = []

    for pred in predictions:
        cid = pred.get("condition_id", "")
        predicted_outcome = (pred.get("predicted_outcome") or "").lower()
        confidence = float(pred.get("confidence", 0.5))
        reasoning = pred.get("reasoning", "")

        market = market_by_cid.get(cid)
        if market is None:
            print(f"  WARN: prediction for unknown condition_id={cid}, skipping.")
            continue

        actual_outcome = market["actual_outcome"]  # 'yes' or 'no'
        resolved_price = market["resolved_price"]  # 1.0 = yes won, 0.0 = no won

        # Clamp confidence to avoid 0/1 extremes in scoring
        confidence = max(0.0, min(1.0, confidence))

        # Brier score: (confidence_for_predicted_outcome - actual)^2
        # actual_for_predicted: 1.0 if the predicted outcome actually happened, else 0.0
        if predicted_outcome == "yes":
            actual_for_predicted = resolved_price  # 1.0 if YES won
        else:
            actual_for_predicted = 1.0 - resolved_price  # 1.0 if NO won

        brier_score = (confidence - actual_for_predicted) ** 2

        correct = 1 if (predicted_outcome == actual_outcome) else 0

        scored.append(
            {
                "condition_id": cid,
                "market_title": market["title"],
                "predicted_outcome": predicted_outcome,
                "confidence": confidence,
                "reasoning": reasoning,
                "actual_outcome": actual_outcome,
                "resolved_price": resolved_price,
                "brier_score": brier_score,
                "correct": correct,
            }
        )

    return scored


# ---------------------------------------------------------------------------
# Step 6 — Store results
# ---------------------------------------------------------------------------

def ensure_backtest_table(conn) -> None:
    conn.execute(BACKTEST_TABLE_DDL)
    conn.commit()


def store_results(conn, run_id: str, freeze_date: str, scored: list[dict]) -> None:
    for r in scored:
        conn.execute(
            """
            INSERT INTO backtest_results
                (run_id, freeze_date, condition_id, market_title,
                 predicted_outcome, confidence, reasoning,
                 actual_outcome, resolved_price, brier_score, correct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                freeze_date,
                r["condition_id"],
                r["market_title"],
                r["predicted_outcome"],
                r["confidence"],
                r["reasoning"],
                r["actual_outcome"],
                r["resolved_price"],
                r["brier_score"],
                r["correct"],
            ),
        )
    conn.commit()


def print_summary(freeze_date: str, scored: list[dict]) -> None:
    n = len(scored)
    if n == 0:
        print("No scored predictions to summarize.")
        return

    n_correct = sum(r["correct"] for r in scored)
    mean_brier = sum(r["brier_score"] for r in scored) / n
    # Brier Skill Score: 1 - (mean_brier / baseline)
    # Baseline (random) Brier score = 0.25 (50/50 with confidence=0.5 → (0.5-1)^2=0.25)
    baseline = 0.25
    bss = 1.0 - (mean_brier / baseline)

    print()
    print("=== BACKTEST RESULTS ===")
    print(f"Freeze date:     {freeze_date}")
    print(f"Markets tested:  {n}")
    print(f"Correct:         {n_correct}/{n} ({100*n_correct/n:.0f}%)")
    print(f"Mean Brier score: {mean_brier:.4f}")
    print(f"Brier Skill Score: {bss:.4f} (vs base rate {baseline})")
    print()
    print(f"{'Market':<50} {'Pred':<5} {'Conf':>6} {'Actual':<7} {'BS':>6} {'OK'}")
    print("-" * 90)
    for r in scored:
        title_trunc = (r["market_title"] or "")[:48]
        ok = "YES" if r["correct"] else "no"
        print(
            f"{title_trunc:<50} {r['predicted_outcome']:<5} "
            f"{r['confidence']:>6.2f} {r['actual_outcome']:<7} "
            f"{r['brier_score']:>6.4f} {ok}"
        )


# ---------------------------------------------------------------------------
# Step 7 — Show last run
# ---------------------------------------------------------------------------

def show_last_run(conn) -> None:
    # Find the most recent run_id
    row = conn.execute(
        "SELECT run_id, freeze_date FROM backtest_results ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print("No backtest results found in the database.")
        return

    run_id = row["run_id"]
    freeze_date = row["freeze_date"]

    rows = conn.execute(
        """
        SELECT condition_id, market_title, predicted_outcome, confidence,
               reasoning, actual_outcome, resolved_price, brier_score, correct
        FROM backtest_results
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()

    scored = [dict(r) for r in rows]
    print(f"Showing run: {run_id}")
    print_summary(freeze_date, scored)

    print("\n=== REASONING ===")
    for r in scored:
        title = (r["market_title"] or "")[:60]
        print(f"\n[{title}]")
        print(f"  Predicted: {r['predicted_outcome']} (conf={r['confidence']:.2f}) | Actual: {r['actual_outcome']}")
        print(f"  Reasoning: {r['reasoning']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_freeze_date(conn, cli_date: str | None) -> str:
    if cli_date:
        # Validate format
        datetime.datetime.strptime(cli_date, "%Y-%m-%d")
        return cli_date

    # Use earliest date in news_items
    row = conn.execute(
        "SELECT MIN(published_at) AS earliest FROM news_items"
    ).fetchone()
    if row and row["earliest"]:
        # published_at may include time component; take date part
        return str(row["earliest"])[:10]

    # Fallback: yesterday (UTC — utcnow() is banned by CLAUDE.md and deprecated in 3.12+)
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    ).strftime("%Y-%m-%d")


def main() -> None:
    args = parse_args()

    conn = get_connection(DB_PATH)
    ensure_backtest_table(conn)

    # --show-last
    if args.show_last:
        show_last_run(conn)
        conn.close()
        return

    # Resolve freeze date
    freeze_date = resolve_freeze_date(conn, args.date)
    print(f"Freeze date: {freeze_date}")
    print(f"Num markets: {args.num_markets}")
    print(f"Dry run:     {args.dry_run}")
    print()

    # Step 2: Build frozen news context
    print("Building frozen news context...")
    news_block = build_frozen_news(conn, freeze_date)
    news_lines = news_block.count("\n") + 1
    print(f"  {news_lines} news item(s) found before {freeze_date}")

    # Step 3: Find testable markets
    print("Finding testable markets...")
    markets = find_testable_markets(conn, freeze_date, args.num_markets)
    print(f"  {len(markets)} testable market(s) found")

    if not markets:
        print("No testable markets found. Try a different --date or check DB contents.")
        conn.close()
        sys.exit(0)

    for m in markets:
        print(f"  - [{m['condition_id'][:12]}...] {m['title'][:60]} | YES price: {m['price_yes']:.3f}")

    # Step 4: Build prompt and call Claude
    print("\nCalling Claude for predictions...")
    user_prompt = build_user_prompt(freeze_date, news_block, markets)
    response = call_claude(SYSTEM_PROMPT, user_prompt)

    if response is None:
        print("ERROR: Failed to get predictions from Claude. Aborting.")
        conn.close()
        sys.exit(1)

    raw_predictions = response.get("predictions", [])
    print(f"  Received {len(raw_predictions)} prediction(s) from Claude")

    # Step 5: Score predictions
    print("Scoring predictions...")
    scored = score_predictions(raw_predictions, markets)
    print(f"  Scored {len(scored)} prediction(s)")

    if not scored:
        print("No scorable predictions. Check Claude response format.")
        conn.close()
        sys.exit(0)

    # Step 6: Store results
    run_id = str(uuid.uuid4())
    if args.dry_run:
        print(f"  [DRY RUN] Would store {len(scored)} result(s) with run_id={run_id}")
    else:
        store_results(conn, run_id, freeze_date, scored)
        print(f"  Stored {len(scored)} result(s) with run_id={run_id}")

    # Print summary
    print_summary(freeze_date, scored)

    conn.close()


if __name__ == "__main__":
    main()
