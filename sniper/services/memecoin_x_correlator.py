"""Crash-safe incremental runner for display-only memecoin X forward correlation."""
from __future__ import annotations

from datetime import datetime

from sniper.analysis.memecoin_x.correlator import build_forward_links
from sniper.analysis.memecoin_x.fomo_adapter import fetch_eligible_fomo_buy_page
from sniper.persistence import db_memecoin_x as X


def _aware(value: str) -> str:
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return parsed.isoformat()


def run_once(
        x_conn, fomo_conn, *, run_id, observed_at, page_size=1000,
        max_pages=100) -> dict:
    page_size = int(page_size)
    max_pages = int(max_pages)
    if not 1 <= page_size <= 10_000:
        raise ValueError("page_size must be between 1 and 10000")
    if not 1 <= max_pages <= 10_000:
        raise ValueError("max_pages must be between 1 and 10000")
    observed_at = _aware(observed_at)
    after = X.last_completed_fomo_id(x_conn)
    if not X.start_correlator_run(
            x_conn, run_id=run_id, started_at=observed_at, after_fomo_id=after):
        raise ValueError(f"duplicate correlator run_id: {run_id}")

    through = after
    source_rows = eligible_buys = links_created = links_upgraded = 0
    try:
        for _ in range(max_pages):
            page = fetch_eligible_fomo_buy_page(
                fomo_conn, after_id=through, limit=page_size)
            if page.source_rows == 0:
                break
            result = build_forward_links(x_conn, page.buys)
            source_rows += page.source_rows
            eligible_buys += len(page.buys)
            links_created += int(result["created"])
            links_upgraded += int(result["upgraded"])
            through = page.through_id
            if page.source_rows < page_size:
                break
        X.finish_correlator_run(
            x_conn, run_id=run_id, finished_at=observed_at, status="ok",
            through_fomo_id=through, source_rows=source_rows,
            eligible_buys=eligible_buys, links_created=links_created,
            links_upgraded=links_upgraded)
    except Exception as exc:
        X.finish_correlator_run(
            x_conn, run_id=run_id, finished_at=observed_at, status="error",
            through_fomo_id=through, source_rows=source_rows,
            eligible_buys=eligible_buys, links_created=links_created,
            links_upgraded=links_upgraded, error_detail=f"{type(exc).__name__}: {exc}")
        raise
    return {
        "run_id": run_id, "after_fomo_id": after, "through_fomo_id": through,
        "source_rows": source_rows, "eligible_buys": eligible_buys,
        "links_created": links_created, "links_upgraded": links_upgraded,
        "status": "ok",
    }
