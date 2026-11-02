#!/usr/bin/env python3
"""
Collect signal outcomes from Polymarket for entry price validation.
Phase 0.5 scope: entry validation only (no exit simulation yet).
"""

import argparse
import sqlite3
import hashlib
from datetime import datetime, timezone
from typing import Optional
try:
    from data.polymarket_client import PolymarketClient, PricePoint
except ImportError:
    from polymarket_client import PolymarketClient, PricePoint


def get_or_create_signal_id(wallet: str, condition_id: str, outcome: str, entry_ts: int) -> str:
    """Generate deterministic signal ID from natural key."""
    natural_key = f"{wallet}:{condition_id}:{outcome}:{entry_ts}"
    return hashlib.sha256(natural_key.encode()).hexdigest()[:16]


def create_table(conn: sqlite3.Connection):
    """Create signal_outcomes table if not exists."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT UNIQUE NOT NULL,
            signal_rowid INTEGER,
            wallet TEXT,
            condition_id TEXT,
            outcome TEXT,
            entry_anchor_ts INTEGER,
            entry_price_stored REAL,
            entry_price_snapshot REAL,
            matched_ts INTEGER,
            lag_seconds INTEGER,
            source TEXT,
            noise_band_pct REAL,
            validated INTEGER DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create indexes
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_lookup 
        ON signal_outcomes(wallet, condition_id, outcome, entry_anchor_ts)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_validated 
        ON signal_outcomes(validated)
    ''')
    
    conn.commit()


def upsert_outcome(
    conn: sqlite3.Connection,
    signal_id: str,
    signal_rowid: int,
    wallet: str,
    condition_id: str,
    outcome: str,
    entry_anchor_ts: int,
    stored_price: float,
    price_point: PricePoint
):
    """Upsert signal outcome (idempotent)."""
    conn.execute('''
        INSERT INTO signal_outcomes (
            signal_id, signal_rowid, wallet, condition_id, outcome,
            entry_anchor_ts, entry_price_stored, entry_price_snapshot,
            matched_ts, lag_seconds, source, noise_band_pct, validated, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id) DO UPDATE SET
            entry_price_snapshot = excluded.entry_price_snapshot,
            matched_ts = excluded.matched_ts,
            lag_seconds = excluded.lag_seconds,
            source = excluded.source,
            noise_band_pct = excluded.noise_band_pct,
            validated = excluded.validated,
            note = excluded.note,
            updated_at = CURRENT_TIMESTAMP
    ''', (
        signal_id,
        signal_rowid,
        wallet,
        condition_id,
        outcome,
        entry_anchor_ts,
        stored_price,
        price_point.price,
        price_point.matched_ts,
        price_point.lag_seconds,
        price_point.source,
        price_point.noise_band_pct,
        1 if price_point.validated else 0,
        price_point.note
    ))
    conn.commit()


def collect_outcomes(
    db_path: str,
    client: PolymarketClient,
    limit: Optional[int] = None,
    offset: int = 0,
    max_gap_seconds: int = 300
):
    """Collect outcomes for signals."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Create table if needed
    create_table(conn)
    
    # Fetch signals to process
    query = '''
        SELECT rowid, wallet, condition_id, outcome, entry_anchor_ts, entry_price
        FROM position_signals
        WHERE entry_anchor_ts > 0
    '''
    
    if limit:
        query += f' LIMIT {limit}'
    if offset:
        query += f' OFFSET {offset}'
    
    c = conn.cursor()
    c.execute(query)
    signals = c.fetchall()
    
    print(f'Processing {len(signals)} signals...')
    
    stats = {
        'total': 0,
        'validated': 0,
        'fallback': 0,
        'failed': 0
    }
    
    for signal in signals:
        stats['total'] += 1
        
        rowid = signal['rowid']
        wallet = signal['wallet']
        condition_id = signal['condition_id']
        outcome = signal['outcome'] or 'YES'
        entry_ts = signal['entry_anchor_ts']
        stored_price = signal['entry_price'] or 0
        
        # Generate signal ID
        signal_id = get_or_create_signal_id(wallet, condition_id, outcome, entry_ts)
        
        # Fetch Polymarket price
        try:
            price_point = client.get_price_at(
                condition_id,
                outcome,
                entry_ts,
                window_seconds=3600,
                max_gap_seconds=max_gap_seconds
            )
            
            # Track stats
            if price_point.validated:
                stats['validated'] += 1
            elif price_point.price is not None:
                stats['fallback'] += 1
            else:
                stats['failed'] += 1
            
            # Upsert outcome
            upsert_outcome(
                conn, signal_id, rowid, wallet, condition_id, outcome,
                entry_ts, stored_price, price_point
            )
            
            # Progress
            if stats['total'] % 100 == 0:
                print(f'  Processed {stats["total"]}... (validated: {stats["validated"]}, fallback: {stats["fallback"]}, failed: {stats["failed"]})')
            
        except Exception as e:
            stats['failed'] += 1
            print(f'  Error processing signal {rowid}: {e}')
            
            # Still upsert with error note
            error_point = PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='error',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note=str(e)
            )
            upsert_outcome(
                conn, signal_id, rowid, wallet, condition_id, outcome,
                entry_ts, stored_price, error_point
            )
    
    conn.close()
    
    return stats


def main():
    parser = argparse.ArgumentParser(description='Collect signal outcomes from Polymarket')
    parser.add_argument('--db-path', default='sniper_tracker.db', help='Database path')
    parser.add_argument('--limit', type=int, help='Limit number of signals to process')
    parser.add_argument('--offset', type=int, default=0, help='Offset for pagination')
    parser.add_argument('--max-gap-seconds', type=int, default=300, help='Max acceptable time gap')
    
    args = parser.parse_args()
    
    print(f'Collecting outcomes from {args.db_path}')
    print(f'Max gap: {args.max_gap_seconds}s')
    if args.limit:
        print(f'Limit: {args.limit}')
    if args.offset:
        print(f'Offset: {args.offset}')
    
    client = PolymarketClient()
    
    stats = collect_outcomes(
        args.db_path,
        client,
        limit=args.limit,
        offset=args.offset,
        max_gap_seconds=args.max_gap_seconds
    )
    
    print(f'\nComplete!')
    print(f'  Total processed: {stats["total"]}')
    print(f'  Validated (high confidence): {stats["validated"]}')
    print(f'  Fallback (trade proxy): {stats["fallback"]}')
    print(f'  Failed: {stats["failed"]}')


if __name__ == '__main__':
    main()
