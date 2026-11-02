#!/usr/bin/env python3
"""
Validate paper_trades for formula integrity and Polymarket price verification.
Read-only validation - no database mutations.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

import sys as _sys, pathlib as _pathlib   # a bare `python3 data/validate_trades.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.settings.paper import PAPER_STOP_LOSS_PCT, PAPER_TAKE_PROFIT_PCT

# Import polymarket client
try:
    from data.polymarket_client import PolymarketClient
except ImportError:
    from polymarket_client import PolymarketClient


def get_expected_values(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate expected values based on formulas."""
    direction = trade.get('direction', '')
    entry = trade.get('entry_price', 0) or 0
    exit_price = trade.get('exit_price')
    size = trade.get('size_usdc', 0) or 0
    
    result = {
        'direction': direction,
        'entry_price': entry,
        'exit_price': exit_price,
        'size_usdc': size
    }
    
    if entry <= 0:
        return {**result, 'error': 'Invalid entry price'}
    
    # Calculate expected stop/target
    # Calculate expected stop/target (direction-specific)
    if direction == 'YES':
        expected_stop = entry * (1 - PAPER_STOP_LOSS_PCT)
        expected_target = min(entry * (1 + PAPER_TAKE_PROFIT_PCT), 0.99)
    else:  # NO
        expected_stop = max(entry * (1 + PAPER_STOP_LOSS_PCT), 0.01)
        expected_target = entry * (1 - PAPER_TAKE_PROFIT_PCT)
    
    # ROI calculation is the same for YES and NO (buy low, sell high)
    if exit_price is not None:
        expected_roi = (exit_price - entry) / entry if entry > 0 else 0
    else:
        expected_roi = None
    
    result['expected_stop_loss'] = expected_stop
    result['expected_take_profit'] = expected_target
    
    if expected_roi is not None:
        expected_pnl = size * expected_roi
        result['expected_roi'] = expected_roi
        result['expected_pnl'] = expected_pnl
    
    return result


def validate_trade(trade: Dict[str, Any], client: Optional[PolymarketClient] = None) -> Dict[str, Any]:
    """Validate a single trade. Returns validation report."""
    trade_id = trade.get('trade_id')
    
    result = {
        'trade_id': trade_id,
        'wallet': trade.get('wallet', '')[:12] + '...',
        'market': (trade.get('market_title', '') or '')[:40],
        'direction': trade.get('direction'),
        'status': 'unverifiable'  # default
    }
    
    # Get expected values
    expected = get_expected_values(trade)
    
    if 'error' in expected:
        result['status'] = 'error'
        result['issue'] = expected['error']
        return result
    
    # Check formula integrity
    issues = []
    
    stored_stop = trade.get('stop_loss_price')
    stored_target = trade.get('take_profit_price')
    stored_pnl = trade.get('pnl_usdc')
    stored_roi = trade.get('roi_percent')
    exit_price = trade.get('exit_price')
    
    # Validate stop/target
    if stored_stop is not None and abs(stored_stop - expected['expected_stop_loss']) > 0.001:
        issues.append({
            'type': 'stop_loss_mismatch',
            'stored': stored_stop,
            'expected': expected['expected_stop_loss'],
            'delta': stored_stop - expected['expected_stop_loss']
        })
    
    if stored_target is not None and abs(stored_target - expected['expected_take_profit']) > 0.001:
        issues.append({
            'type': 'take_profit_corrupt',
            'stored': stored_target,
            'expected': expected['expected_take_profit'],
            'delta': stored_target - expected['expected_take_profit']
        })
    
    # Validate PnL if exit exists
    if exit_price is not None and 'expected_pnl' in expected:
        if stored_pnl is None:
            issues.append({
                'type': 'pnl_missing',
                'expected': expected['expected_pnl']
            })
        elif abs(stored_pnl - expected['expected_pnl']) > 0.01:
            issues.append({
                'type': 'pnl_mismatch',
                'stored': stored_pnl,
                'expected': expected['expected_pnl'],
                'delta': stored_pnl - expected['expected_pnl']
            })
        
        if stored_roi is not None and 'expected_roi' in expected:
            expected_roi_pct = expected['expected_roi'] * 100
            if abs(stored_roi - expected_roi_pct) > 0.1:
                issues.append({
                    'type': 'roi_mismatch',
                    'stored': stored_roi,
                    'expected': expected_roi_pct,
                    'delta': stored_roi - expected_roi_pct
                })
    elif exit_price is None:
        # No exit price - can't verify PnL
        issues.append({
            'type': 'unverifiable_exit',
            'note': 'No exit_price, cannot validate PnL calculations'
        })
    
    # Fetch Polymarket prices for verification
    condition_id = trade.get('condition_id')
    entry_anchor_ts = trade.get('entry_anchor_ts')
    
    if client and condition_id and entry_anchor_ts:
        try:
            outcome = trade.get('outcome', 'YES')
            price_point = client.get_price_at(
                condition_id,
                outcome,
                entry_anchor_ts,
                window_seconds=3600,
                max_gap_seconds=300
            )
            
            if price_point.price is not None:
                stored_entry = trade.get('entry_price', 0)
                delta_pct = abs(price_point.price - stored_entry) / stored_entry * 100 if stored_entry > 0 else 0
                
                result['polymarket_entry'] = {
                    'price': price_point.price,
                    'matched_ts': price_point.matched_ts,
                    'lag_seconds': price_point.lag_seconds,
                    'source': price_point.source,
                    'confidence': price_point.confidence,
                    'stored_entry': stored_entry,
                    'delta_pct': delta_pct
                }
                
                if delta_pct > 1.0:  # >1% difference
                    issues.append({
                        'type': 'entry_price_discrepancy',
                        'stored': stored_entry,
                        'polymarket': price_point.price,
                        'delta_pct': delta_pct,
                        'source': price_point.source,
                        'confidence': price_point.confidence
                    })
            else:
                result['polymarket_entry'] = {
                    'error': price_point.note,
                    'source': price_point.source
                }
        except Exception as e:
            result['polymarket_entry'] = {'error': str(e)}
    
    if issues:
        result['issues'] = issues
        # Determine overall status
        has_corruption = any(i['type'] in ['take_profit_corrupt', 'stop_loss_mismatch'] for i in issues)
        has_mismatch = any(i['type'] in ['pnl_mismatch', 'roi_mismatch'] for i in issues)
        
        if has_corruption:
            result['status'] = 'corrupt'
        elif has_mismatch:
            result['status'] = 'mismatch'
        else:
            result['status'] = 'warning'
    else:
        result['status'] = 'clean'
    
    return result


def validate_all_trades(db_path: str, check_polymarket: bool = True) -> Dict[str, Any]:
    """Validate all closed trades."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Fetch all closed trades
    c.execute('''
        SELECT * FROM paper_trades 
        WHERE status != 'open'
        ORDER BY trade_id
    ''')
    
    trades = [dict(row) for row in c.fetchall()]
    conn.close()
    
    # Initialize client if requested
    client = PolymarketClient() if check_polymarket else None
    
    # Validate each trade
    results = []
    for trade in trades:
        result = validate_trade(trade, client)
        results.append(result)
    
    # Summary
    summary = {
        'total_trades': len(results),
        'clean': len([r for r in results if r['status'] == 'clean']),
        'corrupt': len([r for r in results if r['status'] == 'corrupt']),
        'mismatch': len([r for r in results if r['status'] == 'mismatch']),
        'warning': len([r for r in results if r['status'] == 'warning']),
        'unverifiable': len([r for r in results if r['status'] == 'unverifiable']),
        'error': len([r for r in results if r['status'] == 'error'])
    }
    
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'db_path': db_path,
        'summary': summary,
        'trades': results
    }


def generate_report(validation: Dict[str, Any], output_dir: str = 'reports'):
    """Generate JSON and Markdown reports."""
    Path(output_dir).mkdir(exist_ok=True)
    
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    json_path = Path(output_dir) / f'phase0_validation_{timestamp}.json'
    md_path = Path(output_dir) / f'phase0_validation_{timestamp}.md'
    
    # JSON report
    with open(json_path, 'w') as f:
        json.dump(validation, f, indent=2)
    
    # Markdown report
    with open(md_path, 'w') as f:
        f.write('# Phase 0 Validation Report\n\n')
        f.write(f'**Timestamp:** {validation["timestamp"]}\n\n')
        f.write(f'**Database:** {validation["db_path"]}\n\n')
        
        # Summary table
        f.write('## Summary\n\n')
        f.write('| Status | Count |\n')
        f.write('|--------|-------|\n')
        for status, count in validation['summary'].items():
            f.write(f'| {status} | {count} |\n')
        f.write('\n')
        
        # Detailed issues
        f.write('## Corrupted/Mismatched Trades\n\n')
        for trade in validation['trades']:
            if trade['status'] in ['corrupt', 'mismatch']:
                f.write(f"### Trade {trade['trade_id']} - {trade['wallet']}\n\n")
                f.write(f"- **Market:** {trade['market']}\n")
                f.write(f"- **Direction:** {trade['direction']}\n")
                f.write(f"- **Status:** {trade['status']}\n\n")
                
                if 'issues' in trade:
                    f.write('**Issues:**\n\n')
                    for issue in trade['issues']:
                        f.write(f"- **{issue['type']}**:\n")
                        for key, val in issue.items():
                            if key != 'type':
                                f.write(f"  - {key}: {val}\n")
                    f.write('\n')
        
        # All trades table
        f.write('## All Trades\n\n')
        f.write('| ID | Wallet | Direction | Status | Issues |\n')
        f.write('|----|--------|-----------|--------|--------|\n')
        for trade in validation['trades']:
            issue_types = ', '.join([i['type'] for i in trade.get('issues', [])]) or '-'
            f.write(f"| {trade['trade_id']} | {trade['wallet']} | {trade['direction']} | {trade['status']} | {issue_types} |\n")
    
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description='Validate paper trades')
    parser.add_argument('--db-path', default='sniper_tracker.db', help='Database path')
    parser.add_argument('--no-polymarket', action='store_true', help='Skip Polymarket verification')
    parser.add_argument('--output-dir', default='reports', help='Output directory')
    
    args = parser.parse_args()
    
    print(f'Validating trades in {args.db_path}...')
    print(f'Polymarket check: {"enabled" if not args.no_polymarket else "disabled"}')
    
    validation = validate_all_trades(args.db_path, check_polymarket=not args.no_polymarket)
    
    json_path, md_path = generate_report(validation, args.output_dir)
    
    print(f'\nReports generated:')
    print(f'  JSON: {json_path}')
    print(f'  Markdown: {md_path}')
    
    print(f'\nSummary:')
    for status, count in validation['summary'].items():
        print(f'  {status}: {count}')
    
    # Exit code
    if validation['summary']['corrupt'] > 0 or validation['summary']['mismatch'] > 0:
        print('\nCorruption/mismatch found. Exit code 2.')
        sys.exit(2)
    else:
        print('\nValidation clean. Exit code 0.')
        sys.exit(0)


if __name__ == '__main__':
    main()
