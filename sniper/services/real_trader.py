#!/usr/bin/env python3
"""
Polymarket Real Trader with Paper-Parallel Mode

Strategy: Copy proven wallets with safety limits and real execution.
Can run in parallel with paper mode to compare before going full real.
"""

import os
import sys
import time
import json
import logging
import sqlite3
import socket
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dotenv import load_dotenv
from db import (
    acquire_decision,
    claim_or_create_position,
    get_connection,
    get_db_path,
    init_db as init_core_db,
    paper_trade_gate_status,
    reconcile_order_intent,
)
from sniper.runtime.wallet_key_loader import load_private_key

# Systemd loads the service environment. Direct program use loads the local file.
# Imports must not change the caller's complete process environment.
if __name__ == "__main__":
    load_dotenv()

# Import CLOB client
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    import httpx
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('real_trader')

# SOCKS5 Proxy setup
SOCKS5_HOST = os.getenv('SOCKS5_HOST', '')
SOCKS5_PORT = os.getenv('SOCKS5_PORT', '1080')
SOCKS5_USER = os.getenv('SOCKS5_USER', '')
SOCKS5_PASS = os.getenv('SOCKS5_PASS', '')

if SOCKS5_HOST and SOCKS5_USER:
    # Monkey-patch httpx.Client to use SOCKS5 proxy by default
    proxy_url = f"socks5://{SOCKS5_USER}:{SOCKS5_PASS}@{SOCKS5_HOST}:{SOCKS5_PORT}"
    _original_httpx_client_init = httpx.Client.__init__
    
    def _patched_httpx_client_init(self, *args, **kwargs):
        # Inject proxy if not already set
        if 'proxies' not in kwargs:
            kwargs['proxies'] = {
                "http://": proxy_url,
                "https://": proxy_url,
            }
        return _original_httpx_client_init(self, *args, **kwargs)
    
    httpx.Client.__init__ = _patched_httpx_client_init
    logger.info(f"SOCKS5 proxy configured: {SOCKS5_HOST}:{SOCKS5_PORT}")

# Paths and config
DB_PATH = get_db_path()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '-5244655612')
CLOB_SIGNATURE_TYPE = int(os.getenv('POLY_CLOB_SIGNATURE_TYPE', '0'))

# ============ CAPITAL & SIZING CONFIG (mirrors paper trader) ============
TOTAL_CAPITAL = float(os.getenv('RT_TOTAL_CAPITAL', '200'))
MAX_POSITION_SIZE = float(os.getenv('RT_MAX_POSITION_SIZE', '50'))
MIN_POSITION_SIZE = float(os.getenv('RT_MIN_POSITION_SIZE', '5'))
BASE_POSITION_SIZE = float(os.getenv('RT_BASE_POSITION_SIZE', '25'))
MAX_OPEN_POSITIONS = int(os.getenv('RT_MAX_OPEN_POSITIONS', '15'))
MAX_POSITION_PCT_OF_CAPITAL = 0.10
MIN_ORDER_NOTIONAL_USD = 1.0
STRATEGY_ID = os.getenv('REAL_TRADER_STRATEGY_ID', 'real_trader_copy')

# ============ SAFETY CIRCUIT BREAKERS ============
DAILY_LOSS_LIMIT = float(os.getenv('RT_DAILY_LOSS_LIMIT', '50'))
MAX_SLIPPAGE_PCT = float(os.getenv('RT_MAX_SLIPPAGE_PCT', '20'))
RT_MAX_ADVERSE_DRIFT = float(os.getenv('RT_MAX_ADVERSE_DRIFT_PCT', '15'))
RT_MAX_FAVORABLE_DRIFT = float(os.getenv('RT_MAX_FAVORABLE_DRIFT_PCT', '40'))

# ============ EXIT SAFETY THRESHOLDS ============
RESOLUTION_WIN_THRESHOLD = float(os.getenv('RT_RESOLUTION_WIN_THRESHOLD', '0.97'))
RESOLUTION_LOSS_THRESHOLD = float(os.getenv('RT_RESOLUTION_LOSS_THRESHOLD', '0.03'))
STALE_DAYS = int(os.getenv('RT_STALE_DAYS', '14'))
STALE_ACTIVITY_LOOKBACK_DAYS = 7
SOURCE_PARTIAL_EXIT_PCT = float(os.getenv('RT_SOURCE_PARTIAL_EXIT_PCT', '0.50'))

# ============ ENTRY GATES (mirrors paper_trader.py gates exactly) ============
MAX_ENTRY_PRICE = float(os.getenv('RT_MAX_ENTRY_PRICE', '0.70'))
MIN_ENTRY_PRICE = float(os.getenv('RT_MIN_ENTRY_PRICE', '0.01'))
ENTRY_MIN_INSIDER_SCORE = float(os.getenv('RT_MIN_INSIDER_SCORE', '55'))
MAX_SPECULATOR_SCORE = float(os.getenv('RT_MAX_SPECULATOR_SCORE', '70'))
MIN_QUALIFYING_ROI = float(os.getenv('RT_MIN_QUALIFYING_ROI', '10.0'))
REAL_MAX_DAYS_TO_RESOLUTION = int(os.getenv('REAL_MAX_DAYS_TO_RESOLUTION', '60'))
SKIP_WALLET_CLASSES = frozenset(os.getenv('RT_SKIP_WALLET_CLASSES', 'arb_fund').split(','))
SPECULATOR_BOT_MIN_WIN_RATE = float(os.getenv('RT_SPEC_BOT_MIN_WIN_RATE', '0.85'))
SPECULATOR_BOT_MIN_QUALIFYING_ROI = float(os.getenv('RT_SPEC_BOT_MIN_QROI', '15.0'))

# ============ SIZING PARAMETERS (mirrors paper_trader.py sizing) ============
SIZING_MIN_QROI = float(os.getenv('RT_SIZING_MIN_QROI', '10'))
SIZING_ELITE_QROI = float(os.getenv('RT_SIZING_ELITE_QROI', '50'))
SIZING_MIN_INSIDER_SCORE = float(os.getenv('RT_SIZING_MIN_INSIDER_SCORE', '45'))
SIZING_ELITE_INSIDER_SCORE = float(os.getenv('RT_SIZING_ELITE_INSIDER_SCORE', '90'))

# ============ EXECUTION PRIORITY WALLETS ============
# Looser slippage tolerance for known high-performers (execution concern only).
PRIORITY_WALLETS = [
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",
    "0x96489abcb9f583d6835c8ef95ffc923d05a86825",
    "0xead02f1568144be782d762e2b6a719302830f3ba",
    "0x1117eade22241333071fe1f14e6268f935d1d4f5",
]

# ============ FRESHNESS (logging only, not entry gate) ============
FRESH_HOURS = 24
RECENT_HOURS = 72
# >$0.50: declining returns

class RealTrader:
    """Real money trader with paper-parallel tracking."""
    
    def __init__(self, dry_run=True):
        self.dry_run = dry_run  # If True, don't actually execute
        self.conn = get_connection(DB_PATH)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.clob = None
        self.last_reset = datetime.now(timezone.utc).date()
        self.daily_pnl = self._load_daily_pnl()

        # Ensure shared schema/migrations are present for idempotency + ownership guards.
        init_core_db(self.conn)
        self._init_tables()
        self._connect_clob()

        # Hard interlock: refuse to start in real mode if paper gate not met.
        if not self.dry_run:
            self._enforce_paper_gate(at_startup=True)
        
    def _init_tables(self):
        """Ensure real_trades table exists with all needed columns."""
        c = self.conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS real_trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id INTEGER,
                wallet TEXT NOT NULL,
                market_title TEXT,
                condition_id TEXT NOT NULL,
                outcome TEXT,
                direction TEXT,
                entry_price REAL,
                executed_price REAL,
                size_usd REAL,
                size_shares REAL,
                token_id TEXT,
                status TEXT DEFAULT 'pending',
                opened_at TIMESTAMP,
                closed_at TIMESTAMP,
                exit_price REAL,
                pnl_usd REAL,
                notes TEXT,
                tx_hash TEXT,
                order_id TEXT,
                dry_run INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Add columns if they don't exist
        new_cols = [
            ('executed_price', 'REAL'),
            ('tx_hash', 'TEXT'),
            ('order_id', 'TEXT'),
            ('dry_run', 'INTEGER'),
            ('size_shares', 'REAL'),
            ('token_id', 'TEXT'),
        ]
        
        for col_name, col_type in new_cols:
            try:
                c.execute(f'ALTER TABLE real_trades ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass
        
        # Create indices
        c.execute('CREATE INDEX IF NOT EXISTS idx_real_status ON real_trades(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_real_wallet ON real_trades(wallet)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_real_dry ON real_trades(dry_run)')
        
        self.conn.commit()
    
    def _connect_clob(self):
        """Connect to Polymarket CLOB."""
        if not CLOB_AVAILABLE:
            logger.warning("CLOB client not available")
            return
        
        try:
            loaded = load_private_key(expected_wallet=None)
            private_key = loaded["private_key"]
            
            self.clob = ClobClient(
                host="https://clob.polymarket.com",
                key=private_key,
                chain_id=137,  # Polygon
                signature_type=CLOB_SIGNATURE_TYPE,
            )
            
            # Generate API credentials (uses monkey-patched httpx.Client)
            self.clob.set_api_creds(self.clob.create_or_derive_api_creds())
            
            # Test connection
            logger.info(
                "✅ Connected to Polymarket CLOB | signer=%s | signature_type=%s",
                loaded["signer"],
                CLOB_SIGNATURE_TYPE,
            )
            
        except Exception as e:
            logger.error(f"❌ CLOB connection failed: {e}")
            self.clob = None
    
    def _load_daily_pnl(self) -> float:
        """Load today's realized PnL from DB so circuit breaker survives restarts."""
        today_str = datetime.now(timezone.utc).date().isoformat()
        try:
            row = self.conn.execute(
                '''SELECT COALESCE(SUM(pnl_usd), 0)
                   FROM real_trades
                   WHERE dry_run = 0
                     AND DATE(closed_at) = ?
                     AND status = 'closed'
                ''',
                (today_str,)
            ).fetchone()
            return float(row[0]) if row else 0.0
        except Exception as e:
            logger.warning(f"Could not load daily PnL from DB: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Paper-trade deployment gate
    # ------------------------------------------------------------------

    def _enforce_paper_gate(self, at_startup: bool = False):
        """Check the paper-trade deployment gate.

        * **at_startup**: hard sys.exit if gate not met (prevents launch).
        * **per-cycle** (at_startup=False): returns (can_trade, reason) so
          the circuit breaker can pause new entries without killing the
          process (existing positions still get exit-managed).
        """
        gate = paper_trade_gate_status(self.conn)
        if gate["gate_ready"]:
            return True, "paper gate OK"

        msg = "Paper-trade gate NOT MET: " + "; ".join(gate["fail_reasons"])
        if at_startup:
            logger.error(f"⛔ REFUSING TO START IN REAL MODE — {msg}")
            sys.exit(1)

        return False, msg

    def _check_circuit_breakers(self) -> Tuple[bool, str]:
        """Check if trading should be halted."""
        # Paper-trade gate recheck (pause new entries, don't kill process)
        if not self.dry_run:
            ok, reason = self._enforce_paper_gate(at_startup=False)
            if not ok:
                return False, reason

        # Reset daily PnL if new day (reload from DB to be safe)
        today = datetime.now(timezone.utc).date()
        if today != self.last_reset:
            self.daily_pnl = self._load_daily_pnl()
            self.last_reset = today

        # Check daily loss limit
        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit hit: ${self.daily_pnl:.2f}"

        # Check max open positions
        open_count = self._count_open_positions()
        if open_count >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({MAX_OPEN_POSITIONS}) reached"

        # Check capital usage
        capital_used = self._get_capital_deployed()
        available = TOTAL_CAPITAL - capital_used
        if available < MIN_POSITION_SIZE:
            return False, f"Insufficient capital: ${available:.2f} available"

        return True, "OK"
    
    def _count_open_positions(self) -> int:
        """Count real open positions."""
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM real_trades WHERE status = 'open'")
        return c.fetchone()[0]
    
    def _get_capital_deployed(self) -> float:
        """Get total capital currently in open positions."""
        c = self.conn.cursor()
        c.execute("SELECT COALESCE(SUM(size_usd), 0) FROM real_trades WHERE status = 'open'")
        return c.fetchone()[0]
    
    def get_proven_wallets(self) -> List[Dict]:
        """Get candidate wallets from the current analysis table.

        Returns wallet-level data including qualifying_roi, insider_score,
        speculator_score, and classification — needed by evaluate_signal()
        to mirror paper trader entry gates.
        """
        c = self.conn.cursor()
        c.execute('''
            SELECT
                wallet,
                COALESCE(win_rate, 0) AS win_rate,
                COALESCE(total_closed_count, closed_sample_size, trade_count, 0) AS trade_count,
                COALESCE(qualifying_pnl, pnl, 0) AS pnl,
                COALESCE(qualifying_roi, 0) AS qualifying_roi,
                COALESCE(insider_score, 0) AS insider_score,
                COALESCE(speculator_score, 0) AS speculator_score,
                COALESCE(classification, '') AS classification
            FROM user_analysis
            WHERE COALESCE(classification, '') <> 'blocked'
              AND (
                    COALESCE(is_bot, 0) = 0
                    OR COALESCE(classification, '') = 'arb_fund'
                    OR COALESCE(high_winrate_qualified, 0) = 1
                  )
              AND COALESCE(total_closed_count, closed_sample_size, trade_count, 0) > 0
            ORDER BY
                COALESCE(high_winrate_qualified, 0) DESC,
                COALESCE(insider_score, 0) DESC,
                COALESCE(qualifying_pnl, pnl, 0) DESC,
                COALESCE(win_rate, 0) DESC,
                COALESCE(total_closed_count, closed_sample_size, trade_count, 0) DESC
        ''')

        wallets = []
        for row in c.fetchall():
            wallets.append({
                'wallet': row['wallet'],
                'win_rate': row['win_rate'],
                'trades': row['trade_count'],
                'pnl': row['pnl'],
                'qualifying_roi': row['qualifying_roi'],
                'insider_score': row['insider_score'],
                'speculator_score': row['speculator_score'],
                'classification': row['classification'],
            })

        return wallets
    
    def classify_freshness(self, signal: Dict) -> Tuple[int, str, Optional[float]]:
        """Classify signal freshness. Returns (tier, label, hours_old)."""
        entry_ts = signal.get('entry_anchor_ts', 0)
        
        if entry_ts and entry_ts > 1000000000:
            # tz=timezone.utc is required: a naive fromtimestamp() reads the host's
            # local zone, and subtracting it from an aware now() raises TypeError —
            # so this branch crashed for every real (non-sentinel) signal.
            entry_time = datetime.fromtimestamp(entry_ts, tz=timezone.utc)
            hours_old = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            
            if hours_old <= FRESH_HOURS:
                return 1, "fresh", hours_old
            elif hours_old <= RECENT_HOURS:
                return 2, "recent", hours_old
            else:
                return 3, "stale", hours_old
        
        return 4, "unknown", None
    
    def calculate_position_size(self, signal: Dict, wallet_data: Dict) -> float:
        """Calculate position size (mirrors paper_trader.py multiplicative sizing).

        Four multiplicative factors:
        1. Upside potential   — lower entry price = more room to run.
        2. Wallet ROI quality — higher qualifying_roi sizes up.
        3. Insider conviction — higher position_insider_score sizes up.
        4. Win rate boost     — secondary quality signal.
        """
        base_size = BASE_POSITION_SIZE
        min_size = MIN_POSITION_SIZE
        max_size = MAX_POSITION_SIZE
        capital_available = TOTAL_CAPITAL - self._get_capital_deployed()

        def _to_float(value, default: float) -> float:
            try:
                return float(value)
            except Exception:
                return default

        entry_price = _to_float(signal.get('current_price', 0.5), 0.5)
        win_rate = _to_float(wallet_data.get('win_rate', 0.5), 0.5)
        if win_rate > 1.0 and win_rate <= 100.0:
            win_rate = win_rate / 100.0
        win_rate = max(0.0, min(1.0, win_rate))

        raw_qroi = wallet_data.get('qualifying_roi')
        qualifying_roi = None if raw_qroi is None else _to_float(raw_qroi, SIZING_MIN_QROI)
        insider_score = _to_float(
            signal.get('position_insider_score', signal.get('insider_score', 0.0)),
            0.0,
        )

        # 1. Upside multiplier: price 0.10 ~= 1.25x, price 0.70 ~= 0.72x.
        upside_mult = max(0.70, min(1.35, 1.35 - (entry_price * 0.9)))

        # 2. ROI quality multiplier.
        qroi_range = max(1.0, SIZING_ELITE_QROI - SIZING_MIN_QROI)
        if qualifying_roi is None:
            roi_mult = 0.85
        else:
            qroi_progress = max(
                0.0,
                min(1.0, (qualifying_roi - SIZING_MIN_QROI) / qroi_range),
            )
            roi_mult = 0.90 + (qroi_progress * 0.50)
            if qualifying_roi > SIZING_ELITE_QROI:
                roi_mult += min(0.10, (qualifying_roi - SIZING_ELITE_QROI) / 100.0)
        roi_mult = max(0.85, min(1.50, roi_mult))

        # 3. Insider conviction multiplier.
        insider_range = max(1.0, SIZING_ELITE_INSIDER_SCORE - SIZING_MIN_INSIDER_SCORE)
        insider_progress = max(
            0.0,
            min(1.0, (insider_score - SIZING_MIN_INSIDER_SCORE) / insider_range),
        )
        insider_mult = 0.90 + (insider_progress * 0.45)
        if insider_score > SIZING_ELITE_INSIDER_SCORE:
            insider_mult += min(0.10, (insider_score - SIZING_ELITE_INSIDER_SCORE) / 100.0)
        insider_mult = max(0.85, min(1.45, insider_mult))

        # 4. Win rate boost (kept intentionally mild).
        if win_rate >= 0.80:
            win_mult = 1.10
        elif win_rate >= 0.70:
            win_mult = 1.03
        elif win_rate >= 0.60:
            win_mult = 1.00
        elif win_rate >= 0.50:
            win_mult = 0.95
        else:
            win_mult = 0.90

        size = base_size * upside_mult * roi_mult * insider_mult * win_mult
        size = max(min_size, min(max_size, size))

        # Don't exceed available capital
        size = min(size, capital_available)

        if size < min_size:
            return 0

        return round(size, 2)
    
    def check_slippage(self, signal_price: float, current_price: float) -> Tuple[bool, float]:
        """Check if slippage is acceptable."""
        if signal_price <= 0:
            return True, 0
        
        slippage = abs(current_price - signal_price) / signal_price * 100
        return slippage <= MAX_SLIPPAGE_PCT, slippage
    
    def send_telegram_alert(self, message: str):
        """Send Telegram notification."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return
        
        try:
            import requests
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }
            with requests.Session() as session:
                session.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Telegram alert failed: {e}")
    
    def _extract_order_id(self, result: Dict[str, Any] | None) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        # Some SDK responses use orderID while older code expected orderId.
        value = result.get('orderID') or result.get('orderId') or result.get('id')
        return str(value) if value else None

    def _extract_tx_hash(self, result: Dict[str, Any] | None) -> Optional[str]:
        if not isinstance(result, dict):
            return None
        value = result.get('tx_hash') or result.get('txHash') or result.get('transactionHash')
        return str(value) if value else None

    def _extract_filled_shares(self, result: Dict[str, Any] | None) -> Optional[float]:
        if not isinstance(result, dict):
            return None
        for key in ('filledSize', 'filled_size', 'matchedSize', 'matched_size', 'sizeMatched'):
            value = result.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _resolve_token_id(self, condition_id: str, outcome: str) -> Tuple[Optional[str], List[str]]:
        market = self.clob.get_market(condition_id)
        if not market:
            return None, []

        available_outcomes = []
        for token in market.get('tokens', []):
            token_outcome = str(token.get('outcome', ''))
            available_outcomes.append(token_outcome)
            if token_outcome.lower() == str(outcome).lower():
                return token.get('token_id'), available_outcomes
        return None, available_outcomes

    def _mark_decision_failed(self, decision_id: int, message: str):
        """Persist a failed decision state so retries don't re-execute blindly."""
        if not decision_id:
            return
        now_ts = int(time.time())
        self.conn.execute(
            '''
            UPDATE execution_decisions
            SET state = 'failed',
                completed_ts = ?,
                error = ?,
                reason = CASE
                    WHEN COALESCE(reason, '') = '' THEN ?
                    ELSE reason
                END
            WHERE decision_id = ?
            ''',
            (now_ts, str(message or ''), str(message or ''), int(decision_id)),
        )
        self.conn.commit()

    def _mark_decision_executed(self, decision_id: int, reason: str = ''):
        """Persist decision completion after an order is accepted/submitted."""
        if not decision_id:
            return
        now_ts = int(time.time())
        self.conn.execute(
            '''
            UPDATE execution_decisions
            SET state = 'executed',
                completed_ts = ?,
                reason = CASE
                    WHEN ? <> '' THEN ?
                    ELSE reason
                END
            WHERE decision_id = ?
            ''',
            (now_ts, str(reason or ''), str(reason or ''), int(decision_id)),
        )
        self.conn.commit()

    def _record_order_intent(
        self,
        *,
        position_id: int,
        decision_id: int,
        side: str,
        token_id: str,
        limit_price: float,
        requested_shares: float,
        requested_notional: float,
        exchange_order_id: Optional[str],
        result: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Store submitted intent for later exchange reconciliation."""
        now_ts = int(time.time())
        payload = result if isinstance(result, dict) else {}
        self.conn.execute(
            '''
            INSERT INTO order_intents (
                position_id,
                decision_id,
                side,
                token_id,
                limit_price,
                requested_shares,
                requested_notional,
                exchange_order_id,
                client_order_id,
                state,
                submit_ts,
                last_update_ts,
                raw_exchange_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(decision_id, side) DO UPDATE SET
                token_id = excluded.token_id,
                limit_price = excluded.limit_price,
                requested_shares = excluded.requested_shares,
                requested_notional = excluded.requested_notional,
                exchange_order_id = excluded.exchange_order_id,
                client_order_id = excluded.client_order_id,
                state = excluded.state,
                submit_ts = CASE
                    WHEN order_intents.submit_ts <= 0 THEN excluded.submit_ts
                    ELSE order_intents.submit_ts
                END,
                last_update_ts = excluded.last_update_ts,
                raw_exchange_json = excluded.raw_exchange_json
            ''',
            (
                int(position_id),
                int(decision_id),
                str(side or 'BUY').upper(),
                str(token_id or ''),
                float(limit_price or 0.0),
                float(requested_shares or 0.0),
                float(requested_notional or 0.0),
                str(exchange_order_id or ''),
                str(exchange_order_id or ''),
                'submitted',
                now_ts,
                now_ts,
                json.dumps(payload, separators=(',', ':'), sort_keys=True),
            ),
        )
        row = self.conn.execute(
            '''
            SELECT intent_id
            FROM order_intents
            WHERE decision_id = ? AND side = ?
            ''',
            (int(decision_id), str(side or 'BUY').upper()),
        ).fetchone()
        self.conn.commit()
        return int(row['intent_id']) if row else 0

    def place_order(
        self,
        condition_id: str,
        outcome: str,
        size_usd: float,
        price: float,
        side: str = 'BUY',
        shares: Optional[float] = None,
        token_id: Optional[str] = None,
    ) -> Optional[Dict]:
        """Place order on Polymarket."""
        if not self.clob:
            logger.error("CLOB not connected")
            return None
        
        side = str(side or 'BUY').upper()
        if side not in {'BUY', 'SELL'}:
            logger.error(f"Invalid side: {side}")
            return None

        if self.dry_run:
            logger.info(f"[DRY RUN] Would place {side}: {outcome} ${size_usd} @ ${price:.3f}")
            return {
                'dry_run': True,
                'orderID': 'simulated',
                '_order_id': 'simulated',
                '_token_id': token_id,
                '_shares': shares if shares is not None else (size_usd / price if price > 0 else None),
            }
        
        try:
            if price <= 0:
                logger.error(f"Invalid price: {price}")
                return None

            if token_id is None:
                token_id, available_outcomes = self._resolve_token_id(condition_id, outcome)
            else:
                available_outcomes = []

            if not token_id:
                logger.error(f"Token not found for outcome: '{outcome}'")
                logger.error(f"Available outcomes: {available_outcomes}")
                return None

            if side == 'BUY':
                if size_usd < MIN_ORDER_NOTIONAL_USD:
                    logger.warning(f"Size ${size_usd:.2f} below minimum ${MIN_ORDER_NOTIONAL_USD:.2f}")
                    return None
                # CLOB size is in shares, not notional USD.
                order_size = size_usd / price
            else:
                order_size = shares if shares is not None else (size_usd / price if size_usd > 0 else 0.0)

            if order_size <= 0:
                logger.error(f"Invalid order size (shares): {order_size}")
                return None

            order_args = OrderArgs(
                token_id=token_id,
                side=side,
                size=order_size,
                price=price,
            )
            
            result = self.clob.create_and_post_order(order_args)
            order_id = self._extract_order_id(result)

            if isinstance(result, dict):
                result.setdefault('_order_id', order_id)
                result.setdefault('_token_id', token_id)
                result.setdefault('_shares', order_size)

            logger.info(f"✅ Order placed: {order_id or 'unknown'} | Side: {side} | Shares: {order_size:.4f}")
            return result
            
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return None
    
    def evaluate_signal(self, signal: Dict, wallet_data: Dict) -> Tuple[bool, str, Dict]:
        """Evaluate if signal passes all rules (mirrors paper_trader.py gates).

        Entry gates (from paper trader):
        0. Wallet archetype filter (skip arb_fund; speculator_bot requires high WR + ROI)
        1. Price cap: entry_price <= MAX_ENTRY_PRICE (0.70)
        2. Position insider score >= ENTRY_MIN_INSIDER_SCORE (55)
        3. Position speculator score < MAX_SPECULATOR_SCORE (70)
        4. Wallet qualifying_roi >= MIN_QUALIFYING_ROI (10%) or insider >= 90
        5. End-date guard: <= REAL_MAX_DAYS_TO_RESOLUTION days

        Execution safety (real-money only):
        - Circuit breakers (daily loss, max positions)
        - Slippage check
        - Minimum position size
        """
        metadata = {}
        wallet = signal.get('wallet', '')

        # ---- Execution safety: circuit breakers ----
        can_trade, reason = self._check_circuit_breakers()
        if not can_trade:
            return False, f"CIRCUIT BREAKER: {reason}", metadata

        # ---- Gate 0: Wallet archetype filter (mirrors paper_trader.py:486-501) ----
        wallet_class = wallet_data.get('classification', '')
        if wallet_class in SKIP_WALLET_CLASSES:
            return False, f"Excluded archetype: {wallet_class}", metadata
        if wallet_class == 'speculator_bot':
            _wr = wallet_data.get('win_rate', 0) or 0.0
            _roi = wallet_data.get('qualifying_roi', 0) or 0.0
            if _wr < SPECULATOR_BOT_MIN_WIN_RATE or _roi < SPECULATOR_BOT_MIN_QUALIFYING_ROI:
                return False, (
                    f"speculator_bot below thresholds "
                    f"(WR={_wr:.2f}<{SPECULATOR_BOT_MIN_WIN_RATE} "
                    f"or ROI={_roi:.1f}%<{SPECULATOR_BOT_MIN_QUALIFYING_ROI}%)"
                ), metadata

        # ---- Gate 1: Price cap (mirrors paper_trader.py:504) ----
        current_price = signal.get('current_price', 0)
        if current_price > MAX_ENTRY_PRICE:
            return False, f"Price {current_price:.3f} > {MAX_ENTRY_PRICE} — insufficient upside", metadata
        if current_price < MIN_ENTRY_PRICE:
            return False, f"Price {current_price:.3f} < {MIN_ENTRY_PRICE}", metadata

        # ---- Gate 2: Position insider score (mirrors paper_trader.py:510) ----
        insider_score = signal.get('position_insider_score', 0) or 0
        if insider_score < ENTRY_MIN_INSIDER_SCORE:
            return False, f"insider_score={insider_score:.0f} < {ENTRY_MIN_INSIDER_SCORE}", metadata

        # ---- Gate 3: Speculator score cap (mirrors paper_trader.py:516) ----
        speculator_score = signal.get('position_speculator_score', 0) or 0
        if speculator_score >= MAX_SPECULATOR_SCORE:
            return False, f"speculator_score={speculator_score:.0f} >= {MAX_SPECULATOR_SCORE}", metadata

        # ---- Gate 4: Wallet qualifying_roi (mirrors paper_trader.py:523-529) ----
        wallet_qroi = wallet_data.get('qualifying_roi')
        if wallet_qroi is None or wallet_qroi == 0:
            wallet_qroi = None  # Treat 0 as unknown (COALESCE default)
        if wallet_qroi is None and insider_score < 90:
            return False, f"qualifying_roi=None and insider_score={insider_score:.0f} < 90", metadata
        if wallet_qroi is not None and wallet_qroi < MIN_QUALIFYING_ROI:
            return False, f"qualifying_roi={wallet_qroi:.1f}% < {MIN_QUALIFYING_ROI}%", metadata

        # ---- Gate 5: End-date guard (mirrors paper_trader.py:531) ----
        end_date_iso = signal.get('end_date_iso')
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(str(end_date_iso).replace('Z', '+00:00'))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                if days_left > REAL_MAX_DAYS_TO_RESOLUTION:
                    return False, (
                        f"Resolution too far: {days_left}d > {REAL_MAX_DAYS_TO_RESOLUTION}d cap"
                    ), metadata
            except (ValueError, TypeError):
                pass

        # ---- Freshness (logging only, not an entry gate) ----
        tier, label, hours_old = self.classify_freshness(signal)
        metadata['freshness_tier'] = tier
        metadata['freshness_label'] = label
        metadata['hours_old'] = hours_old

        # ---- Execution safety: directional drift check ----
        is_priority = wallet in PRIORITY_WALLETS
        wallet_entry = signal.get('entry_price', 0)
        slippage_ok, slippage = self.check_slippage(wallet_entry, current_price)
        metadata['slippage'] = slippage

        # Directional drift: positive = copy pays more than source, negative = copy pays less
        if wallet_entry and wallet_entry > 0:
            drift_pct = ((current_price - wallet_entry) / wallet_entry) * 100
            metadata['drift_pct'] = round(drift_pct, 2)

            adverse_limit = 25 if is_priority else RT_MAX_ADVERSE_DRIFT
            if drift_pct > adverse_limit:
                return False, (
                    f"Adverse drift {drift_pct:+.1f}% > {adverse_limit}% "
                    f"(source {wallet_entry:.3f} → now {current_price:.3f})"
                ), metadata
            if drift_pct < -RT_MAX_FAVORABLE_DRIFT:
                return False, (
                    f"Favorable drift {drift_pct:+.1f}% > {RT_MAX_FAVORABLE_DRIFT}% "
                    f"(source {wallet_entry:.3f} → now {current_price:.3f}, likely late)"
                ), metadata
        elif not slippage_ok and slippage > (25 if is_priority else MAX_SLIPPAGE_PCT):
            return False, f"Slippage too high: {slippage:.1f}%", metadata

        # ---- Sizing ----
        size = self.calculate_position_size(signal, wallet_data)
        metadata['size'] = size
        metadata['is_priority'] = is_priority
        metadata['source_size'] = float(signal.get('size') or signal.get('position_value') or 0)

        if size <= 0:
            return False, "Position size too small", metadata

        return True, "PASS", metadata
    
    def enter_position(self, signal: Dict, wallet_data: Dict):
        """Enter a real position if all rules pass."""
        wallet = signal['wallet']
        condition_id = signal['condition_id']
        outcome = signal.get('outcome', '')
        decision_id = 0
        position_id = 0
        
        # Check if already have open position for this market/outcome
        c = self.conn.cursor()
        c.execute('''
            SELECT 1 FROM real_trades 
            WHERE condition_id = ? AND outcome = ? AND status = 'open'
        ''', (condition_id, outcome))
        if c.fetchone():
            logger.debug(f"Already have open position for {condition_id}/{outcome}")
            return
        
        # Evaluate signal
        should_trade, reason, metadata = self.evaluate_signal(signal, wallet_data)
        
        if not should_trade:
            logger.info(f"❌ REJECTED: {wallet[:12]}... {outcome} | {reason}")
            return
        
        # All checks passed - execute
        current_price = signal.get('current_price', 0)
        size = metadata['size']
        
        logger.info(f"🎯 EXECUTING: {wallet[:12]}... | {outcome}")
        logger.info(f"   Price: ${current_price:.3f} | Size: ${size}")
        logger.info(f"   Freshness: {metadata['freshness_label']} ({metadata.get('hours_old', 'N/A')}h)")
        logger.info(f"   Slippage: {metadata.get('slippage', 0):.1f}%")

        # Safety check #1: idempotent decision acquisition (prevents duplicate retries/restarts).
        try:
            decision = acquire_decision(
                self.conn,
                signal,
                STRATEGY_ID,
                'ENTRY',
                self.worker_id,
            )
            decision_id = int(decision.get('decision_id') or 0)
        except Exception as e:
            logger.error(f"Decision acquisition failed for {wallet[:12]}... {outcome}: {e}")
            return

        if not decision.get('acquired'):
            logger.info(
                f"⏭️ Skip entry (decision already handled/claimed): {wallet[:12]}... {outcome} "
                f"| state={decision.get('state', 'unknown')}"
            )
            return

        # Safety check #2: enforce single active position owner for (wallet, condition, outcome).
        try:
            ownership = claim_or_create_position(
                self.conn,
                signal,
                STRATEGY_ID,
                decision_id,
            )
            position_id = int(ownership.get('position_id') or 0)
        except Exception as e:
            self._mark_decision_failed(decision_id, f"ownership claim failed: {e}")
            logger.error(f"Ownership claim failed: {e}")
            return

        if not ownership.get('claimed'):
            logger.info(
                f"⏭️ Skip entry (owned by {ownership.get('owner_strategy', 'another strategy')}): "
                f"{wallet[:12]}... {outcome}"
            )
            return
        
        # Place order
        result = self.place_order(condition_id, outcome, size, current_price, side='BUY')
        
        if result:
            order_id = self._extract_order_id(result)
            token_id = result.get('_token_id') if isinstance(result, dict) else None
            size_shares = result.get('_shares') if isinstance(result, dict) else None
            tx_hash = self._extract_tx_hash(result) if isinstance(result, dict) else None
            try:
                size_shares = float(size_shares) if size_shares is not None else 0.0
            except (TypeError, ValueError):
                size_shares = 0.0
            if size_shares <= 0 and current_price > 0:
                size_shares = float(size) / float(current_price)

            try:
                with self.conn:
                    # Record trade in existing local tracker table.
                    c.execute('''
                        INSERT INTO real_trades 
                        (paper_trade_id, wallet, market_title, condition_id, outcome, direction,
                         entry_price, executed_price, size_usd, size_shares, token_id, status, opened_at, notes, tx_hash, order_id, dry_run)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', datetime('now'), ?, ?, ?, ?)
                    ''', (
                        signal.get('trade_id'),
                        wallet,
                        signal.get('title', 'Unknown'),
                        condition_id,
                        outcome,
                        outcome,  # store actual outcome string, not a binary YES/NO label
                        signal.get('entry_price'),
                        current_price,  # always store current_price as executed_price (dry_run or real)
                        size,
                        size_shares,
                        token_id,
                        json.dumps(metadata),
                        tx_hash,
                        order_id,
                        1 if self.dry_run else 0
                    ))

                    now_ts = int(time.time())
                    if position_id:
                        self.conn.execute(
                            '''
                            UPDATE strategy_positions
                            SET token_id = CASE WHEN ? <> '' THEN ? ELSE token_id END,
                                entry_price = CASE WHEN entry_price <= 0 THEN ? ELSE entry_price END,
                                avg_entry_price = CASE WHEN avg_entry_price <= 0 THEN ? ELSE avg_entry_price END,
                                entry_shares = CASE WHEN entry_shares <= 0 THEN ? ELSE entry_shares END,
                                state = CASE
                                    WHEN state IN ('pending_entry', 'error') THEN 'open'
                                    ELSE state
                                END,
                                updated_ts = ?,
                                last_reconciled_ts = ?
                            WHERE position_id = ?
                            ''',
                            (
                                str(token_id or ''),
                                str(token_id or ''),
                                float(current_price or 0.0),
                                float(current_price or 0.0),
                                float(size_shares or 0.0),
                                now_ts,
                                now_ts,
                                int(position_id),
                            ),
                        )
                    if decision_id:
                        self.conn.execute(
                            '''
                            UPDATE execution_decisions
                            SET position_id = ?
                            WHERE decision_id = ?
                            ''',
                            (int(position_id or 0), int(decision_id)),
                        )
            except Exception as e:
                self._mark_decision_failed(decision_id, f"local trade persistence failed: {e}")
                logger.error(f"❌ Failed to persist local trade record: {e}")
                return

            # Safety check #3: persist order intent for later fill reconciliation.
            intent_id = 0
            try:
                if position_id and decision_id:
                    intent_id = self._record_order_intent(
                        position_id=position_id,
                        decision_id=decision_id,
                        side='BUY',
                        token_id=str(token_id or ''),
                        limit_price=float(current_price or 0.0),
                        requested_shares=float(size_shares or 0.0),
                        requested_notional=float(size or 0.0),
                        exchange_order_id=order_id,
                        result=result if isinstance(result, dict) else None,
                    )
            except Exception as e:
                logger.warning(f"Order intent persistence failed (non-fatal): {e}")

            # Optional immediate reconciliation for real mode; periodic reconciliation can be added later.
            if intent_id and not self.dry_run and self.clob:
                try:
                    reconcile_order_intent(self.conn, self.clob, intent_id)
                except Exception as e:
                    logger.warning(f"Initial order reconciliation failed (non-fatal): {e}")

            self._mark_decision_executed(
                decision_id,
                f"entry order submitted ({order_id or 'unknown'})",
            )
            
            # Send alert
            mode = "[DRY RUN]" if self.dry_run else "[REAL MONEY]"
            alert = f"{mode} 🎯 ENTER\n"
            alert += f"Wallet: {wallet[:16]}...\n"
            alert += f"Market: {signal.get('title', 'Unknown')[:40]}...\n"
            alert += f"Outcome: {outcome}\n"
            alert += f"Size: ${size:.0f}\n"
            alert += f"Price: ${current_price:.3f}"
            self.send_telegram_alert(alert)
            
            logger.info(f"✅ Position opened: ${size} @ ${current_price:.3f}")
        else:
            self._mark_decision_failed(decision_id, "entry order failed")
            if position_id:
                self.conn.execute(
                    '''
                    UPDATE strategy_positions
                    SET state = 'error',
                        updated_ts = ?
                    WHERE position_id = ?
                    ''',
                    (int(time.time()), int(position_id)),
                )
                self.conn.commit()
            logger.error("❌ Order failed")
    
    def _fetch_live_price(self, condition_id: str, outcome: str) -> Optional[float]:
        """Fetch current market price from CLOB API."""
        import requests
        try:
            r = requests.get(
                f'https://clob.polymarket.com/markets/{condition_id}',
                timeout=8,
            )
            if r.ok:
                tokens = r.json().get('tokens', [])
                for tok in tokens:
                    if str(tok.get('outcome', '')).lower() == outcome.lower():
                        return max(0.0, min(1.0, float(tok['price'])))
        except Exception as e:
            logger.debug(f"CLOB price fetch failed for {condition_id}: {e}")
        return None

    def _source_has_recent_activity(self, wallet: str, condition_id: str, lookback_days: int = 7) -> bool:
        """Check whether source wallet traded this market recently."""
        import requests
        try:
            r = requests.get(
                'https://data-api.polymarket.com/activity',
                params={'user': wallet, 'market': condition_id, 'limit': 5},
                timeout=8,
            )
            if not r.ok:
                return False
            acts = r.json()
            cutoff = time.time() - int(lookback_days) * 86400
            for act in acts:
                ts = act.get('timestamp') or act.get('createdAt', 0)
                # Coerce to seconds if in milliseconds
                if isinstance(ts, (int, float)) and ts > 1e12:
                    ts = ts / 1000.0
                if float(ts) >= cutoff:
                    return True
        except Exception as e:
            logger.debug(f"Source activity check failed for {wallet[:10]}: {e}")
        return False

    def _get_source_current_size(self, wallet: str, condition_id: str) -> Optional[float]:
        """Fetch source wallet's current position size from position_signals."""
        row = self.conn.execute('''
            SELECT current_size FROM position_signals
            WHERE wallet = ? AND condition_id = ? AND is_open = 1
            ORDER BY updated_at DESC LIMIT 1
        ''', (wallet, condition_id)).fetchone()
        if row and row['current_size'] is not None:
            return float(row['current_size'])
        return None

    def check_exits(self):
        """Check exit conditions: source exit, resolution, stale, partial sell."""
        c = self.conn.cursor()
        c.execute('''
            SELECT rt.* FROM real_trades rt
            WHERE rt.status = 'open'
        ''')

        for row in c.fetchall():
            pos = dict(row)
            trade_id = pos['trade_id']
            condition_id = pos['condition_id']
            outcome = pos['outcome']

            # Source wallet for follow-exits: stored in notes JSON metadata,
            # falls back to the wallet column (for trades opened by the automated loop).
            source_wallet = pos['wallet']
            try:
                notes_json = (pos.get('notes') or '')
                json_end = notes_json.find(' | ')
                meta = json.loads(notes_json[:json_end] if json_end > 0 else notes_json)
                if meta.get('source_wallet'):
                    source_wallet = meta['source_wallet']
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

            try:
                # --- 1. Check if source fully exited ---
                c2 = self.conn.cursor()
                c2.execute('''
                    SELECT is_open, current_price FROM position_signals
                    WHERE wallet = ? AND condition_id = ? AND outcome = ?
                    ORDER BY updated_at DESC LIMIT 1
                ''', (source_wallet, condition_id, outcome))

                signal = c2.fetchone()
                if signal and signal['is_open'] == 0:
                    exit_price = signal['current_price']
                    # Try to get a fresher price from CLOB
                    live = self._fetch_live_price(condition_id, outcome)
                    if live is not None:
                        exit_price = live
                    self.close_position(trade_id, exit_price, 'source_exited')
                    continue

                # --- Get current market price for remaining checks ---
                current_price = None
                if signal:
                    current_price = signal['current_price']
                live = self._fetch_live_price(condition_id, outcome)
                if live is not None:
                    current_price = live

                if current_price is None:
                    logger.debug(f"No price available for trade {trade_id}, skipping exit checks")
                    continue

                current_price = max(0.0, min(1.0, float(current_price)))

                # --- 2. Resolution auto-exit ---
                if current_price >= RESOLUTION_WIN_THRESHOLD:
                    logger.info(f"Resolution WIN for trade {trade_id}: price {current_price:.3f} >= {RESOLUTION_WIN_THRESHOLD}")
                    self.close_position(trade_id, current_price, 'resolution_win')
                    continue

                if current_price <= RESOLUTION_LOSS_THRESHOLD:
                    logger.info(f"Resolution LOSS for trade {trade_id}: price {current_price:.3f} <= {RESOLUTION_LOSS_THRESHOLD}")
                    self.close_position(trade_id, current_price, 'resolution_loss')
                    continue

                # --- 3. Source partial sell awareness ---
                if signal and signal['is_open'] == 1:
                    source_current = self._get_source_current_size(source_wallet, condition_id)
                    if source_current is not None:
                        original_source_size = self._parse_source_entry_size(pos)
                        if original_source_size and original_source_size > 0:
                            remaining_pct = source_current / original_source_size
                            if remaining_pct < SOURCE_PARTIAL_EXIT_PCT:
                                logger.info(
                                    f"Source partial exit for trade {trade_id}: "
                                    f"{source_wallet[:10]}... reduced position to {remaining_pct:.0%} "
                                    f"(threshold: {SOURCE_PARTIAL_EXIT_PCT:.0%})"
                                )
                                self.close_position(trade_id, current_price, f'source_partial_exit ({remaining_pct:.0%} remaining)')
                                continue

                # --- 4. Stale position timeout ---
                opened_at = pos.get('opened_at') or pos.get('created_at')
                if opened_at:
                    try:
                        if isinstance(opened_at, str):
                            opened_dt = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
                        else:
                            opened_dt = datetime.fromtimestamp(float(opened_at), tz=timezone.utc)
                        age_days = (datetime.now(timezone.utc) - opened_dt).days
                    except (ValueError, TypeError):
                        age_days = 0

                    if age_days >= STALE_DAYS and not self._source_has_recent_activity(
                        source_wallet, condition_id, lookback_days=STALE_ACTIVITY_LOOKBACK_DAYS
                    ):
                        logger.warning(
                            f"STALE position {trade_id}: {source_wallet[:10]}... open {age_days}d, "
                            f"no source activity in {STALE_ACTIVITY_LOOKBACK_DAYS}d — auto-closing"
                        )
                        self.close_position(trade_id, current_price, f'stale: open {age_days}d, no source activity')
                        continue

            except Exception as e:
                logger.error(f"Error checking exit for trade {trade_id}: {e}")

    @staticmethod
    def _parse_source_entry_size(pos: dict) -> Optional[float]:
        """Extract source entry size from notes field (stored as JSON at entry time)."""
        notes = pos.get('notes') or ''
        # Notes is JSON metadata from entry; look for source_size key
        try:
            # Notes may have appended " | Exit: ..." or " | Partial exit: ..." text
            # from close_position, so try to parse the JSON prefix
            json_end = notes.find(' | ')
            json_str = notes[:json_end] if json_end > 0 else notes
            meta = json.loads(json_str)
            if 'source_size' in meta:
                return float(meta['source_size'])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return None
    
    def cancel_order(self, trade_id: int, reason: str = "manual_cancel"):
        """Cancel an unfilled LIVE order on the CLOB and mark the trade as cancelled."""
        c = self.conn.cursor()
        c.execute('SELECT * FROM real_trades WHERE trade_id = ?', (trade_id,))
        row = c.fetchone()
        if not row:
            logger.error(f"Trade not found: {trade_id}")
            return False

        pos = dict(row)
        if pos.get('status') in ('closed', 'cancelled'):
            logger.info(f"Trade {trade_id} already {pos['status']} — skipping")
            return True

        order_id = pos.get('order_id', '')
        if order_id and self.clob and not pos.get('dry_run'):
            try:
                result = self.clob.cancel(order_id)
                cancelled = result.get('canceled', [])
                if order_id not in cancelled:
                    not_cancelled = result.get('not_canceled', {})
                    logger.warning(f"Cancel may have failed for {order_id}: {not_cancelled}")
            except Exception as e:
                logger.error(f"CLOB cancel failed for trade {trade_id}: {e}")
                return False

        c.execute('''
            UPDATE real_trades SET
                status = 'cancelled',
                closed_at = datetime('now'),
                pnl_usd = 0.0,
                notes = COALESCE(notes, '') || ' | Cancel: ' || ?
            WHERE trade_id = ?
        ''', (reason, trade_id))
        self.conn.commit()
        logger.info(f"Order cancelled for trade {trade_id}: {reason}")
        return True

    def close_position(self, trade_id: int, exit_price: float, reason: str):
        """Close a position — idempotent: checks status before placing SELL order."""
        c = self.conn.cursor()
        c.execute('SELECT * FROM real_trades WHERE trade_id = ?', (trade_id,))
        row = c.fetchone()
        if not row:
            logger.error(f"Trade not found: {trade_id}")
            return

        pos = dict(row)

        # Idempotency guard: if already closed (e.g. crash after SELL but before DB commit),
        # skip placing another SELL order — just ensure DB is up to date.
        if pos.get('status') == 'closed':
            logger.info(f"Trade {trade_id} already closed — skipping duplicate exit")
            return
        
        entry = pos.get('executed_price') or pos['entry_price']
        size = pos['size_usd']
        position_shares = pos.get('size_shares') or (size / entry if entry and entry > 0 else 0)
        token_id = pos.get('token_id')

        # Real positions must send SELL orders before we mark DB status as closed.
        if not pos.get('dry_run'):
            sell_result = self.place_order(
                pos['condition_id'],
                pos['outcome'],
                size,
                exit_price,
                side='SELL',
                shares=position_shares,
                token_id=token_id,
            )
            if not sell_result:
                logger.error(f"Exit order failed for trade {trade_id}; keeping position open")
                return

            filled_shares = self._extract_filled_shares(sell_result)
            if filled_shares is None:
                # If fill size is not returned, assume full fill once order is accepted.
                filled_shares = position_shares
            if filled_shares <= 0:
                logger.error(f"Exit order reported zero fill for trade {trade_id}; keeping position open")
                return
            if filled_shares < position_shares:
                fill_ratio = filled_shares / position_shares if position_shares > 0 else 0
                realized_size = size * fill_ratio
                remaining_size = max(size - realized_size, 0.0)
                remaining_shares = max(position_shares - filled_shares, 0.0)

                roi = (exit_price - entry) / entry if entry > 0 else 0
                partial_pnl = realized_size * roi

                c.execute('''
                    UPDATE real_trades SET
                        size_usd = ?,
                        size_shares = ?,
                        pnl_usd = COALESCE(pnl_usd, 0) + ?,
                        notes = COALESCE(notes, '') || ' | Partial exit: ' || ?
                    WHERE trade_id = ?
                ''', (remaining_size, remaining_shares, partial_pnl, reason, trade_id))
                self.conn.commit()

                self.daily_pnl += partial_pnl
                logger.warning(
                    f"⚠️ Partial exit for trade {trade_id}: sold {filled_shares:.4f}/{position_shares:.4f} shares"
                )
                return
        
        # Calculate PnL
        roi = (exit_price - entry) / entry if entry > 0 else 0
        pnl = size * roi
        
        # Update DB
        c.execute('''
            UPDATE real_trades SET
                status = 'closed',
                exit_price = ?,
                pnl_usd = ?,
                closed_at = datetime('now'),
                notes = COALESCE(notes, '') || ' | Exit: ' || ?
            WHERE trade_id = ?
        ''', (exit_price, pnl, reason, trade_id))
        self.conn.commit()
        
        # Update daily PnL
        self.daily_pnl += pnl
        
        # Send alert
        mode = "[DRY RUN]" if pos.get('dry_run') else "[REAL MONEY]"
        emoji = "🟢" if pnl > 0 else "🔴"
        alert = f"{mode} {emoji} EXIT\n"
        alert += f"Reason: {reason}\n"
        alert += f"PnL: ${pnl:+.2f} ({roi*100:+.1f}%)\n"
        alert += f"Daily PnL: ${self.daily_pnl:+.2f}"
        self.send_telegram_alert(alert)
        
        logger.info(f"{emoji} EXIT: {reason} | PnL: ${pnl:+.2f} | Daily: ${self.daily_pnl:+.2f}")

    def reconcile_open_intents(self, limit: int = 50):
        """Best-effort reconciliation for recently submitted intents."""
        if not self.clob or self.dry_run:
            return
        rows = self.conn.execute(
            '''
            SELECT intent_id
            FROM order_intents
            WHERE state IN ('pending_submit', 'submitted', 'partially_filled')
            ORDER BY last_update_ts ASC, intent_id ASC
            LIMIT ?
            ''',
            (int(limit),),
        ).fetchall()
        for row in rows:
            try:
                reconcile_order_intent(self.conn, self.clob, int(row['intent_id']))
            except Exception as e:
                logger.warning(f"Intent reconciliation failed for {row['intent_id']}: {e}")
    
    def run_loop(self, interval: int = 120, exits_only: bool = False):
        """Main trading loop."""
        mode = "DRY RUN" if self.dry_run else "REAL MONEY"
        if exits_only:
            mode = f"{mode} | EXITS ONLY"
        logger.info("=" * 60)
        logger.info(f"REAL TRADER STARTED - {mode}")
        logger.info(f"Capital: ${TOTAL_CAPITAL} | Max/Pos: ${MAX_POSITION_SIZE}")
        logger.info(f"Daily Loss Limit: ${DAILY_LOSS_LIMIT}")
        logger.info("=" * 60)
        
        while True:
            try:
                if exits_only:
                    self.check_exits()
                    self.reconcile_open_intents()
                    open_count = self._count_open_positions()
                    capital_used = self._get_capital_deployed()
                    logger.info(
                        f"Exit-only status: {open_count} open | ${capital_used:.0f} deployed | Daily PnL: ${self.daily_pnl:+.2f}"
                    )
                    time.sleep(interval)
                    continue

                # Check circuit breakers
                can_trade, reason = self._check_circuit_breakers()
                if not can_trade:
                    logger.warning(f"⛔ HALTED: {reason}")
                    time.sleep(interval)
                    continue
                
                # Get proven wallets
                proven = self.get_proven_wallets()
                wallet_list = [w['wallet'] for w in proven]
                wallet_map = {w['wallet']: w for w in proven}

                if not wallet_list:
                    logger.warning("No proven wallets found; skipping entries this cycle")
                    self.check_exits()
                    time.sleep(interval)
                    continue
                
                # Fetch signals
                c = self.conn.cursor()
                placeholders = ','.join(['?' for _ in wallet_list])
                c.execute(f'''
                    SELECT * FROM position_signals
                    WHERE wallet IN ({placeholders})
                      AND is_open = 1
                    ORDER BY position_value DESC
                ''', wallet_list)
                
                signals = [dict(row) for row in c.fetchall()]
                logger.info(f"Found {len(signals)} signals from {len(proven)} wallets")
                
                # Evaluate and trade
                for signal in signals:
                    wallet_data = wallet_map.get(signal['wallet'])
                    if wallet_data:
                        self.enter_position(signal, wallet_data)
                
                # Check exits
                self.check_exits()

                # Keep intent/fill state in sync with exchange reality.
                self.reconcile_open_intents()
                
                # Log status
                open_count = self._count_open_positions()
                capital_used = self._get_capital_deployed()
                logger.info(f"Status: {open_count} open | ${capital_used:.0f} deployed | Daily PnL: ${self.daily_pnl:+.2f}")
                
                time.sleep(interval)
                
            except Exception as e:
                logger.error(f"Error: {e}")
                time.sleep(interval)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--real', action='store_true', help='Enable real trading (default: dry run)')
    parser.add_argument('--interval', type=int, default=120, help='Check interval in seconds')
    parser.add_argument(
        '--exits-only',
        action='store_true',
        help='Run exit monitoring only; do not evaluate or enter new positions',
    )
    args = parser.parse_args()
    
    trader = RealTrader(dry_run=not args.real)
    trader.run_loop(interval=args.interval, exits_only=args.exits_only)


if __name__ == '__main__':
    main()
