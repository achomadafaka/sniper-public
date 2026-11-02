#!/usr/bin/env python3
"""Seed baseline replay configurations.

Usage:
    python scripts/seed_replay_configs.py [DB_PATH]
"""
import json
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"

CONFIGS = [
    {
        "name": "paper_like_current",
        "notes": "Mirrors current paper trader gates: insider>=55, spec<70, price<=0.70",
        "config": {
            "initial_bankroll": 1000.0,
            "base_size_usdc": 25.0,
            "max_concurrent_positions": 20,
            "max_exposure_per_market_usdc": 200.0,
            "max_total_exposure_usdc": 1000.0,
            "min_insider_score": 55.0,
            "max_speculator_score": 70.0,
            "max_entry_price": 0.70,
            "skip_classifications": ["arb_fund"],
            "circuit_breaker_max_consecutive_losses": None,
            "circuit_breaker_max_drawdown_pct": None,
            "start_ts": None,
            "end_ts": None,
        },
    },
    {
        "name": "loose_exploration",
        "notes": "Relaxed gates for exploring signal breadth: insider>=0, price<=1.0, spec<100",
        "config": {
            "initial_bankroll": 1000.0,
            "base_size_usdc": 25.0,
            "max_concurrent_positions": 50,
            "max_exposure_per_market_usdc": 500.0,
            "max_total_exposure_usdc": 5000.0,
            "min_insider_score": 0.0,
            "max_speculator_score": 100.0,
            "max_entry_price": 1.0,
            "skip_classifications": ["arb_fund"],
            "circuit_breaker_max_consecutive_losses": None,
            "circuit_breaker_max_drawdown_pct": None,
            "start_ts": None,
            "end_ts": None,
        },
    },
    {
        "name": "proof_bar_candidate",
        "notes": "Proof bar target: insider>=60, spec<70, price<=0.70, 10% drawdown breaker",
        "config": {
            "initial_bankroll": 500.0,
            "base_size_usdc": 25.0,
            "max_concurrent_positions": 10,
            "max_exposure_per_market_usdc": 200.0,
            "max_total_exposure_usdc": 500.0,
            "min_insider_score": 60.0,
            "max_speculator_score": 70.0,
            "max_entry_price": 0.70,
            "skip_classifications": ["arb_fund"],
            "circuit_breaker_max_consecutive_losses": 5,
            "circuit_breaker_max_drawdown_pct": 0.10,
            "start_ts": None,
            "end_ts": None,
        },
    },
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    inserted = 0
    for cfg in CONFIGS:
        try:
            conn.execute(
                "INSERT INTO replay_configs (name, config_json, notes) VALUES (?, ?, ?)",
                (cfg["name"], json.dumps(cfg["config"]), cfg["notes"]),
            )
            inserted += 1
            print(f"  + {cfg['name']}")
        except sqlite3.IntegrityError:
            print(f"  ~ {cfg['name']} (already exists)")
    conn.commit()
    conn.close()
    print(f"\nSeeded {inserted} config(s) into {DB_PATH}")


if __name__ == "__main__":
    main()
