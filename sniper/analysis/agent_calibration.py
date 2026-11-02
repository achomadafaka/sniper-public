"""Retrieve and format analyst calibration stats for pre-mortem prompt injection."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_connection

def get_calibration_blurb(source_agent: str, db_path: str | None = None) -> str:
    """
    Return a compact calibration summary for injection into an analyst system prompt.
    If fewer than 3 scored predictions exist, returns an empty string (not enough data).
    """
    _CONF_MAP = {"high": 0.80, "medium": 0.60, "low": 0.40}

    try:
        conn = get_connection(db_path or "sniper_tracker.db")
        rows = conn.execute(
            """
            SELECT direction, confidence, resolution_outcome, brier_score, market_title
            FROM trade_ideas
            WHERE source_agent = ?
              AND resolution_outcome IS NOT NULL
              AND brier_score IS NOT NULL
            ORDER BY resolved_at DESC
            LIMIT 30
            """,
            (source_agent,)
        ).fetchall()
        conn.close()
    except Exception:
        return ""

    if len(rows) < 3:
        return ""  # not enough data for meaningful feedback

    total = len(rows)

    def is_correct(direction, resolution_outcome):
        if direction == "buy_yes":
            return resolution_outcome == "yes"
        if direction == "buy_no":
            return resolution_outcome == "no"
        return False  # unknown direction — don't count as correct

    correct = sum(1 for r in rows if is_correct(r[0], r[2]))
    mean_bs = sum(r[3] for r in rows) / total
    bss = 1.0 - (mean_bs / 0.25)

    # Per-confidence breakdown
    conf_stats = {}
    for r in rows:
        raw = r[1]
        try:
            label = "high" if float(raw) >= 0.75 else "medium" if float(raw) >= 0.55 else "low"
        except (ValueError, TypeError):
            label = str(raw).lower() if raw in ("high","medium","low") else "unknown"
        conf_stats.setdefault(label, {"n": 0, "correct": 0})
        conf_stats[label]["n"] += 1
        if is_correct(r[0], r[2]):
            conf_stats[label]["correct"] += 1

    conf_lines = []
    for label in ("high", "medium", "low"):
        if label in conf_stats:
            s = conf_stats[label]
            acc = s["correct"] / s["n"] * 100
            conf_lines.append(f"  - {label}: {s['correct']}/{s['n']} correct ({acc:.0f}%)")

    blurb = (
        f"\n\n--- YOUR RECENT PREDICTION TRACK RECORD (last {total} scored) ---\n"
        f"Accuracy: {correct}/{total} ({correct/total*100:.0f}%)\n"
        f"Mean Brier score: {mean_bs:.3f} (lower=better; 0.25=coin-flip; 0.0=perfect)\n"
        f"Brier Skill Score: {bss:+.2f} (positive=better than guessing)\n"
        f"By confidence tier:\n" + "\n".join(conf_lines) + "\n"
    )

    # Add calibration guidance based on what the data shows
    if correct/total < 0.45:
        blurb += "⚠ Your accuracy is BELOW coin-flip. Be more cautious, especially on high-confidence calls.\n"
    elif correct/total > 0.70:
        blurb += "✓ Strong accuracy. Your confidence assignments appear well-calibrated.\n"

    if "high" in conf_stats and conf_stats["high"]["n"] >= 3:
        high_acc = conf_stats["high"]["correct"] / conf_stats["high"]["n"]
        if high_acc < 0.60:
            blurb += f"⚠ HIGH-confidence calls are only {high_acc*100:.0f}% accurate — consider downgrading more ideas to MEDIUM.\n"

    blurb += "--- END TRACK RECORD ---\n"
    return blurb
