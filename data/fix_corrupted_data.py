#!/usr/bin/env python3
"""
Fix corrupted data in paper_trades based on validation report.
Supports --dry-run (default) and --apply modes.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import sys as _sys, pathlib as _pathlib   # a bare `python3 data/fix_corrupted_data.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.settings.paper import PAPER_STOP_LOSS_PCT, PAPER_TAKE_PROFIT_PCT


def calculate_expected_values(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate expected values based on formulas."""
    direction = trade.get('direction', '')
    entry = trade.get('entry_price', 0) or 0
    exit_price = trade.get('exit_price')
    size = trade.get('size_usdc', 0) or 0
    
    result = {}
    
    if entry <= 0:
        return result
    
    # Calculate expected stop/target (direction-specific)
    if direction == 'YES':
        result['stop_loss_price'] = entry * (1 - PAPER_STOP_LOSS_PCT)
        result['take_profit_price'] = min(entry * (1 + PAPER_TAKE_PROFIT_PCT), 0.99)
    else:  # NO
        result['stop_loss_price'] = max(entry * (1 + PAPER_STOP_LOSS_PCT), 0.01)
        result['take_profit_price'] = entry * (1 - PAPER_TAKE_PROFIT_PCT)
    
    # ROI calculation is the same for YES and NO (buy low, sell high)
    if exit_price is not None:
        roi = (exit_price - entry) / entry if entry > 0 else 0
        result['roi_percent'] = roi * 100
        result['pnl_usdc'] = size * roi
    
    return result


def generate_fix_plan(validation_report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate list of fixes needed based on validation report."""
    fixes = []
    
    for trade_report in validation_report.get('trades', []):
        trade_id = trade_report.get('trade_id')
        status = trade_report.get('status')
        
        if status not in ['corrupt', 'mismatch']:
            continue
        
        issues = trade_report.get('issues', [])
        
        # Determine which fields need fixing
        fields_to_fix = {}
        
        for issue in issues:
            issue_type = issue.get('type')
            
            if issue_type == 'take_profit_corrupt':
                fields_to_fix['take_profit_price'] = {
                    'stored': issue.get('stored'),
                    'expected': issue.get('expected'),
                    'reason': 'formula_corrupt'
                }
            elif issue_type == 'stop_loss_mismatch':
                fields_to_fix['stop_loss_price'] = {
                    'stored': issue.get('stored'),
                    'expected': issue.get('expected'),
                    'reason': 'formula_mismatch'
                }
            elif issue_type == 'pnl_mismatch':
                fields_to_fix['pnl_usdc'] = {
                    'stored': issue.get('stored'),
                    'expected': issue.get('expected'),
                    'reason': 'calculation_error'
                }
            elif issue_type == 'roi_mismatch':
                fields_to_fix['roi_percent'] = {
                    'stored': issue.get('stored'),
                    'expected': issue.get('expected'),
                    'reason': 'calculation_error'
                }
        
        if fields_to_fix:
            fixes.append({
                'trade_id': trade_id,
                'wallet': trade_report.get('wallet'),
                'direction': trade_report.get('direction'),
                'fields': fields_to_fix
            })
    
    return fixes


def apply_fixes(db_path: str, fixes: List[Dict[str, Any]], dry_run: bool = True) -> Dict[str, Any]:
    """Apply fixes to database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    changes = []
    
    for fix in fixes:
        trade_id = fix['trade_id']
        
        # Fetch current trade data
        c.execute('SELECT * FROM paper_trades WHERE trade_id = ?', (trade_id,))
        row = c.fetchone()
        
        if not row:
            changes.append({
                'trade_id': trade_id,
                'status': 'skipped',
                'reason': 'Trade not found'
            })
            continue
        
        trade = dict(row)
        
        # Calculate all expected values
        expected = calculate_expected_values(trade)
        
        # Determine which fields to actually update
        updates = {}
        before = {}
        after = {}
        
        for field, details in fix['fields'].items():
            if field in expected:
                current_value = trade.get(field)
                new_value = expected[field]
                
                # Only update if different
                if current_value != new_value:
                    updates[field] = new_value
                    before[field] = current_value
                    after[field] = new_value
        
        if not updates:
            changes.append({
                'trade_id': trade_id,
                'status': 'no_change',
                'reason': 'Values already correct'
            })
            continue
        
        if dry_run:
            changes.append({
                'trade_id': trade_id,
                'status': 'would_update',
                'wallet': fix['wallet'],
                'direction': fix['direction'],
                'before': before,
                'after': after
            })
        else:
            # Apply updates
            set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
            values = list(updates.values()) + [trade_id]
            
            c.execute(
                f'UPDATE paper_trades SET {set_clause} WHERE trade_id = ?',
                values
            )
            
            changes.append({
                'trade_id': trade_id,
                'status': 'updated',
                'wallet': fix['wallet'],
                'direction': fix['direction'],
                'before': before,
                'after': after
            })
    
    if not dry_run:
        conn.commit()
    
    conn.close()
    
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'db_path': db_path,
        'dry_run': dry_run,
        'total_fixes': len(fixes),
        'changes': changes
    }


def generate_report(fix_log: Dict[str, Any], output_dir: str = 'reports'):
    """Generate JSON report."""
    Path(output_dir).mkdir(exist_ok=True)
    
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    json_path = Path(output_dir) / f'phase0_fix_{timestamp}.json'
    
    with open(json_path, 'w') as f:
        json.dump(fix_log, f, indent=2)
    
    return json_path


def main():
    parser = argparse.ArgumentParser(description='Fix corrupted paper trades')
    parser.add_argument('--db-path', default='sniper_tracker.db', help='Database path')
    parser.add_argument('--report', required=True, help='Validation report JSON path')
    parser.add_argument('--dry-run', action='store_true', default=True, help='Show changes without applying')
    parser.add_argument('--apply', action='store_true', help='Actually apply fixes')
    parser.add_argument('--output-dir', default='reports', help='Output directory')
    
    args = parser.parse_args()
    
    # If --apply is set, override dry_run
    dry_run = not args.apply
    
    print(f'Loading validation report: {args.report}')
    with open(args.report) as f:
        validation_report = json.load(f)
    
    print(f'Generating fix plan...')
    fixes = generate_fix_plan(validation_report)
    
    if not fixes:
        print('No fixes needed.')
        sys.exit(0)
    
    print(f'Found {len(fixes)} trades needing fixes')
    
    if dry_run:
        print('\n=== DRY RUN MODE ===')
        print('Changes will NOT be applied. Use --apply to execute.\n')
    else:
        print('\n=== APPLY MODE ===')
        print('Changes WILL be applied to database.\n')
    
    print(f'Applying fixes to {args.db_path}...')
    fix_log = apply_fixes(args.db_path, fixes, dry_run=dry_run)
    
    json_path = generate_report(fix_log, args.output_dir)
    
    print(f'\nFix log generated: {json_path}')
    
    # Summary
    updated = len([c for c in fix_log['changes'] if c['status'] == 'updated'])
    would_update = len([c for c in fix_log['changes'] if c['status'] == 'would_update'])
    no_change = len([c for c in fix_log['changes'] if c['status'] == 'no_change'])
    
    print(f'\nSummary:')
    if dry_run:
        print(f'  Would update: {would_update}')
    else:
        print(f'  Updated: {updated}')
    print(f'  No change needed: {no_change}')
    
    # Show detailed changes
    print(f'\nDetailed changes:')
    for change in fix_log['changes']:
        if change['status'] in ['would_update', 'updated']:
            print(f"\nTrade {change['trade_id']} ({change['wallet']}):")
            for field, after_val in change['after'].items():
                before_val = change['before'].get(field)
                print(f"  {field}: {before_val} -> {after_val}")
    
    if dry_run:
        print('\n=== DRY RUN COMPLETE ===')
        print('No changes were made. Review the report, then run with --apply.')
        sys.exit(0)
    else:
        print('\n=== FIXES APPLIED ===')
        
        # Re-run validation automatically
        print('\nRe-running validation to confirm fixes...')
        import subprocess
        result = subprocess.run(
            [sys.executable, 'data/validate_trades.py', '--db-path', args.db_path],
            capture_output=True,
            text=True
        )
        print(result.stdout)
        if result.returncode != 0:
            print('ERROR: Validation still shows issues after fix!')
            sys.exit(1)
        else:
            print('All issues resolved.')
            sys.exit(0)


if __name__ == '__main__':
    main()
