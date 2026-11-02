import argparse
import logging
import os
import time
from typing import Any, Dict, Sequence

from db import get_connection, init_db, record_low_buy_trade
from sniper.filters import is_excluded_market
from sniper.markets.polymarket_api import build_session, fetch_market_by_condition
from sniper.utils import _safe_float as _utils_safe_float

DATA_TRADES_URL = "https://data-api.polymarket.com/trades"
DEFAULT_MIN_ORDER_NOTIONAL = float(os.getenv("MIN_ORDER_NOTIONAL", os.getenv("MIN_TRADE_NOTIONAL", "2000")))


def _safe_float(value: Any, default: float = 0.0) -> float:
    return _utils_safe_float(value, default)


class BackfillRunner:
    def __init__(
        self,
        low_price_threshold: float,
        min_trade_notional: float,
        min_trade_impact_ratio: float,
        min_market_volume_24h: float,
        page_size: int,
        max_pages: int,
        request_delay_seconds: float,
        max_retries: int,
    ) -> None:
        self.http = build_session()
        self.conn = get_connection()
        init_db(self.conn)

        self.low_price_threshold = low_price_threshold
        self.min_trade_notional = min_trade_notional
        self.min_trade_impact_ratio = min_trade_impact_ratio
        self.min_market_volume_24h = min_market_volume_24h
        self.page_size = page_size
        self.max_pages = max_pages
        self.request_delay_seconds = request_delay_seconds
        self.max_retries = max_retries

        self.market_cache: Dict[str, Dict[str, Any]] = {}

    def get_market_meta(self, condition_id: str) -> Dict[str, Any]:
        if not condition_id:
            return {}
        cached = self.market_cache.get(condition_id)
        if cached is not None:
            return cached

        try:
            market = fetch_market_by_condition(self.http, condition_id)
        except Exception as exc:
            logging.debug("Market lookup failed for %s: %s", condition_id, exc)
            market = {}

        meta = {
            "title": str(market.get("question") or ""),
            "slug": str(market.get("slug") or ""),
            "volume_24h": _safe_float(market.get("volume24hr"), 0.0),
        }
        self.market_cache[condition_id] = meta
        return meta

    def fetch_trades_page(self, offset: int) -> list[Dict[str, Any]]:
        params = {
            "side": "BUY",
            "limit": self.page_size,
            "offset": offset,
            "takerOnly": "false",
        }

        for attempt in range(1, self.max_retries + 1):
            resp = self.http.get(DATA_TRADES_URL, params=params, timeout=30)
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except Exception:
                    logging.warning("Non-JSON response for offset=%s; stopping", offset)
                    return []
                return payload if isinstance(payload, list) else []

            if resp.status_code == 429 and attempt < self.max_retries:
                sleep_for = min(1.5 * attempt, 8)
                logging.warning(
                    "429 on offset=%s attempt=%s, retrying in %.1fs",
                    offset,
                    attempt,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue

            if resp.status_code == 429:
                logging.warning("Rate limited repeatedly at offset=%s; stopping backfill loop", offset)
                return []

            if resp.status_code == 400 and "offset" in resp.text.lower():
                logging.info("Offset limit reached at offset=%s; stopping backfill loop", offset)
                return []

            resp.raise_for_status()

        return []

    def run(self) -> None:
        inserted_total = 0
        scanned_total = 0

        for page_idx in range(self.max_pages):
            offset = page_idx * self.page_size
            rows = self.fetch_trades_page(offset)
            if not rows:
                logging.info("No more trades at offset=%s; stopping", offset)
                break

            page_inserted = 0
            for trade in rows:
                scanned_total += 1

                side = str(trade.get("side") or "").upper()
                if side != "BUY":
                    continue

                price = _safe_float(trade.get("price"), 1.0)
                if price > self.low_price_threshold:
                    continue

                size = _safe_float(trade.get("size"), 0.0)
                notional = price * size
                if notional < self.min_trade_notional:
                    continue

                condition_id = str(trade.get("conditionId") or "")
                market_meta = self.get_market_meta(condition_id)

                market_volume_24h = _safe_float(market_meta.get("volume_24h"), 0.0)
                if market_volume_24h <= 0:
                    continue
                if market_volume_24h < self.min_market_volume_24h:
                    continue

                impact_ratio = notional / market_volume_24h
                if impact_ratio < self.min_trade_impact_ratio:
                    continue

                title = market_meta.get("title") or trade.get("title")
                slug = market_meta.get("slug") or trade.get("slug")
                excluded, reason = is_excluded_market(
                    title,
                    slug,
                    trade.get("eventSlug"),
                )
                if excluded:
                    logging.debug(
                        "Skipping tx=%s (%s)",
                        trade.get("transactionHash"),
                        reason,
                    )
                    continue

                inserted = record_low_buy_trade(
                    self.conn,
                    trade,
                    ws_price=price,
                    ws_timestamp_ms=int(_safe_float(trade.get("timestamp")) * 1000),
                    market_volume_24h=market_volume_24h,
                    source="backfill",
                    trigger_type="low_price_entry",
                    trigger_reason=(
                        f"backfill low-price buy: price={price:.4f} notional={notional:.2f} impact={impact_ratio:.4f}"
                    ),
                )
                if inserted:
                    page_inserted += 1

            inserted_total += page_inserted
            logging.info(
                "Backfill page=%s offset=%s scanned=%s inserted=%s total_inserted=%s",
                page_idx + 1,
                offset,
                len(rows),
                page_inserted,
                inserted_total,
            )

            if len(rows) < self.page_size:
                break
            time.sleep(self.request_delay_seconds)

        logging.info("Backfill complete: scanned=%s inserted=%s", scanned_total, inserted_total)


RETIRED_MESSAGE = (
    "backfill_polymarket_low_buys.py is RETIRED. It writes the legacy "
    "`low_buy_trades` table that fed the old discovery / insider-score "
    "pipeline, which is being replaced by the wallet-analyzer-protocol-v2 "
    "stack (see `scripts/run_wallet_protocol_batch.py` + "
    "`wallet_analyzer_intake_queue`). This script must not run again. "
    "If you genuinely need historical low-buy data, restore from backup "
    "or query the archive — do not regenerate via this path."
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Kept only so `--help` exits cleanly without surprising operators.

    The runner has been retired; no flags affect behavior.
    """
    parser = argparse.ArgumentParser(
        description=(
            "RETIRED. Legacy low-buy backfill — replaced by "
            "wallet-analyzer-protocol-v2. Refuses to run."
        )
    )
    parser.add_argument(
        "--ignored",
        action="store_true",
        help="Reserved; ignored. The script will refuse regardless of args.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Unconditional refusal. Returns exit code 2 every time.

    No DB access, no network calls, no `BackfillRunner` instantiation —
    this script is fully retired. The `BackfillRunner` class above is
    preserved only so import-time references in old tooling don't break;
    its `__init__` will still acquire DB + HTTP if invoked directly, which
    is the operator's problem, not this entry point's.
    """
    parse_args(argv)
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.error(RETIRED_MESSAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
