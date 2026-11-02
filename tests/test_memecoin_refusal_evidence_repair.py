"""Check refusal identity, reservation size, and fast premium observations."""
import sqlite3

import pytest

from scripts.memecoin_refusal_capture_loop import poll_refusals
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_refusal_capture as cap

CASES = [
    ("price impact 90.22% over 12.0% cap", "impact_cap"),
    ("route quote is 1.51x the chart mid (cap 1.50x)", "route_over_mid"),
    ("route quote is 1.51x the source price (cap 1.50x)", "route_over_source"),
    ("our quote is +43% above source price (cap 30%)", "evm_chase_quote"),
]


@pytest.mark.parametrize("reason,family", CASES)
def test_refused_quote_keeps_its_reserved_size_through_capture(reason, family):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    at = "2026-09-16T11:00:00+00:00"
    pending = m.reserve_pending(conn, mint="M", source_user_id="u", strategy="alpha_b",
                                notional=175, reserved_at=at, attempt_id="A")
    conn.execute("INSERT INTO memecoin_entry_attempts "
                 "(attempt_id,mint,origin_event_id,terminal_reason,decision_started_at,"
                 "network_id,terminal_status,pending_id,completed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("A", "M", "O", reason, at, "solana", "refused", pending, at))
    rows, _ = poll_refusals(conn, since_iso="2026-09-16T10:59:00+00:00")
    assert len(rows) == 1
    assert rows[0]["gate"] == family
    capture = cap.start({}, **rows[0], horizon_s=810, cadence_s=25, max_concurrent=2,
                        now=0, now_epoch=1789556400)
    assert capture.quote_input_usd == 175
    row = cap.quote_once(capture, http=lambda *a, **kw: (200, {}, None), rpc_url="rpc",
                         clip_usd=20, now_iso=at, now_mono=0,
                         quote_fn=lambda mint, usd, **kw: {"outAmount": str(int(usd * 1e6))},
                         decimals_fn=lambda *a, **kw: 6)
    assert row["quote_input_usd"] == 175
    assert row["token_qty_atomic"] == "175000000"
    conn.close()


@pytest.mark.parametrize("family", [family for _, family in CASES])
@pytest.mark.parametrize("size", [None, float("nan"), 0, -1])
def test_quote_dependent_refusal_does_not_substitute_a_default_size(family, size):
    refused = []
    c = cap.start({}, mint="M", origin_event_id="O", gate=family,
                  refused_at="2026-09-16T11:00:00+00:00", horizon_s=810, cadence_s=25,
                  max_concurrent=1, now=0, now_epoch=1789556400, quote_input_usd=size,
                  on_refuse=lambda *args: refused.append(args))
    assert c is None
    assert refused[0][1] == "not_captured_size_unknown"


def test_chart_premium_has_fast_observations_without_changing_other_captures():
    kw = dict(mint="M", origin_event_id="O", refused_at="2026-09-16T11:00:00+00:00",
              horizon_s=810, cadence_s=25, max_concurrent=2, now=0,
              now_epoch=1789556400, quote_input_usd=175)
    premium = cap.start({}, gate="route_over_mid", **kw)
    ordinary = cap.start({}, gate="impact_cap", **kw)
    premium.advance(0)
    ordinary.advance(0)
    assert premium.due(5)
    assert not ordinary.due(5)
    assert premium.expected_obs == 162
    assert ordinary.expected_obs == 32
