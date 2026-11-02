import importlib
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, *, mint="M", strategy="control", quote=None):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T10:00:00+00:00", strategy=strategy)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0,
                  notional=100.0, fill_source="j", fraction=1.0,
                  created_at="2026-07-07T10:00:00+00:00")
    if quote is not None:
        m.update_position_last_quote(c, pid, quote, "2026-07-07T10:01:00+00:00")
    return pid


def test_memecoin_measurement_audit_reports_honesty_gaps():
    audit = importlib.import_module("scripts.memecoin_measurement_audit")
    c = _conn()
    p_partial = _open(c, mint="PART", quote=1.2)
    m.insert_fill(c, position_id=p_partial, kind="partial_exit", price=1.5, qty=40.0,
                  notional=60.0, fill_source="d", fraction=0.4,
                  created_at="2026-07-07T10:02:00+00:00")
    p_unpriced = _open(c, mint="DARK")
    p_noquote = _open(c, mint="NOQ", strategy="alpha_b")
    m.close_position(c, p_noquote, "2026-07-07T10:05:00+00:00", 0.0,
                     exit_reason="b_timecap_noquote")
    m.mark_sell_classified(c, "s1", "u", "M", True, "2026-07-07T10:10:00+00:00")
    m.mark_sell_classified(c, "s2", "u", "M", False, "2026-07-07T10:10:00+00:00")

    report = audit.build_report(c)
    assert report["open_partial_exit_positions"][0]["position_id"] == p_partial
    assert report["open_unpriced_positions"][0]["position_id"] == p_unpriced
    assert report["sell_coverage"] == {"total": 2, "seen": 1, "missed": 1}
    assert report["arm_summary"]["alpha_b"]["noquote_closes"] == 1
    assert report["arm_summary"]["alpha_b"]["conservative_total_pnl"] == -100.0

    rendered = audit.render_report(report)
    assert "open partial-exit positions: 1" in rendered
    assert "open unpriced positions: 1" in rendered
    assert "sell coverage: 1 seen / 1 missed / 2 total" in rendered
