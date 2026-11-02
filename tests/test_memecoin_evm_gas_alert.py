"""EVM GAS-LOW / GAS-OK page (2026-09-27, Alpha GO): the EVM twin of check_gas_alert.

4663 sat on 0.001051 ETH of native gas (need 0.001310) from 2026-09-26 20:00 to 2026-09-27 17:04Z
and refused 10 armed buys, more than the chain filled in 48 h. The only trace on the phone was five
per-coin REFUSED cards among 194 entry messages, with no WETH figure and no recovery. The gas
top-up pass already reads native and WETH every 15 s off the funded loop; after each pass one
indexed ledger read asks whether a funded entry refused for native gas since the last pass.

Rules: the first such refusal pages GAS-LOW with have / need / WETH; another refusal pages only 6 h
after the last page; GAS-OK pages once when native covers one entry again; an unreadable native
pages nothing and changes nothing. The need is the gate's own formula (required_native_gas).
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_alerts as alerts
from sniper.services import memecoin_evm_risk as er
from sniper.services import memecoin_real_runner as R

T0 = 1_790_000_000.0          # an epoch second; the test clock
HAVE_WEI = 1_050_811_357_363_946
WETH_WEI = 4_200_000_000_000_000


def _ts(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


@pytest.fixture
def pages(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_alert",
                        lambda kind, text, **kw: sent.append((kind, text, kw.get("icon"))) or (True, 0.0))
    return sent


def _refusal(conn, ts, chain="4663", n=[0]):
    n[0] += 1
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, network_id, decision_started_at, "
        "completed_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?)",
        ("gas-%d" % n[0], chain, _iso(ts), _iso(ts), "rejected",
         "insufficient native gas for entry + approval + full-book exit reserve "
         "(have=0.001051, need=0.001310, concurrent=0, pending=0)"))


def _pass(conn, state, ts, native=HAVE_WEI, weth=WETH_WEI, chain="4663"):
    result = {"state": "weth_below_reserve", "chain_id": chain, "native_before_wei": native,
              "weth_wei": weth}
    return R.check_evm_gas_alert(conn, result, state, now_ts=ts)


def test_the_first_native_gas_refusal_pages_once_with_have_need_and_weth(conn, pages):
    state = {}
    _pass(conn, state, T0)                                   # the first pass sets the cursor
    _refusal(conn, T0 + 5)
    _pass(conn, state, T0 + 15)
    need = er.required_entry_native(conn, "4663")
    assert [p[0] for p in pages] == ["GAS-LOW"]
    text = pages[0][1]
    assert "4663" in text and "0.001051" in text and ("%.6f" % need) in text and "0.004200" in text
    _refusal(conn, T0 + 20)
    _pass(conn, state, T0 + 30)                               # a second refusal inside 6 h
    assert len(pages) == 1


def test_a_refusal_six_hours_after_the_page_pages_again(conn, pages):
    state = {}
    _pass(conn, state, T0)
    _refusal(conn, T0 + 5)
    _pass(conn, state, T0 + 15)
    _refusal(conn, T0 + 6 * 3600 + 20)
    _pass(conn, state, T0 + 6 * 3600 + 30)
    assert [p[0] for p in pages] == ["GAS-LOW", "GAS-LOW"]


def test_recovery_pages_gas_ok_once(conn, pages):
    state = {}
    _pass(conn, state, T0)
    _refusal(conn, T0 + 5)
    _pass(conn, state, T0 + 15)
    funded = 8_462_753_277_521_353                            # the 17:04Z deposit
    _pass(conn, state, T0 + 30, native=funded)
    _pass(conn, state, T0 + 45, native=funded)
    assert [p[0] for p in pages] == ["GAS-LOW", "GAS-OK"]
    assert pages[1][2] == "🟢" and "0.008463" in pages[1][1]


def test_an_unreadable_native_pages_nothing_and_changes_nothing(conn, pages):
    state = {}
    _pass(conn, state, T0)
    _refusal(conn, T0 + 5)
    before = {k: dict(v) for k, v in state.items()}
    _pass(conn, state, T0 + 15, native=None)
    assert pages == [] and state == before
    _pass(conn, state, T0 + 30)                               # read again: the refusal still pages
    assert [p[0] for p in pages] == ["GAS-LOW"]


def test_low_native_without_a_refusal_pages_nothing(conn, pages):
    state = {}
    _pass(conn, state, T0)
    _pass(conn, state, T0 + 15)
    assert pages == []


def test_a_refusal_from_before_the_runner_started_does_not_page(conn, pages):
    _refusal(conn, T0 - 3600)
    state = {}
    _pass(conn, state, T0)
    _pass(conn, state, T0 + 15)
    assert pages == []


def test_another_chains_refusal_does_not_page_this_chain(conn, pages):
    state = {}
    _pass(conn, state, T0)
    _refusal(conn, T0 + 5, chain="56")
    _pass(conn, state, T0 + 15)
    assert pages == []


def test_the_need_is_the_gates_own_number(conn):
    """required_entry_native must equal what evm_entry_allowed compares against."""
    ctx = er.build_evm_risk_context(conn, usdc_balance=100.0, native_balance=0.001,
                                    day="2026-09-27", network_id="4663")
    assert er.required_entry_native(conn, "4663") == pytest.approx(er.required_native_gas(ctx))


def test_the_pass_wrapper_runs_the_check_on_its_own_result(conn, pages, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_runner_loop as loop
    state = {}
    stub = lambda c, **kw: {"state": "weth_below_reserve", "chain_id": "4663",
                            "native_before_wei": HAVE_WEI, "weth_wei": WETH_WEI}
    seen = []
    monkeypatch.setattr(R, "check_evm_gas_alert",
                        lambda c, result, st, **kw: seen.append((result["chain_id"], st is state,
                                                                 kw.get("now_ts"))))
    loop.run_evm_gas_topup_pass(conn, contexts={"4663": {"chain": "4663"}}, now_iso="2026-09-27T17:00:00+00:00",
                                topup_fn=stub, gas_alert_state=state)
    # The pass's corrected-UTC instant, never the host clock (attempts are stamped on it).
    assert seen == [("4663", True, _ts("2026-09-27T17:00:00+00:00"))]
    seen.clear()
    loop.run_evm_gas_topup_pass(conn, contexts={"4663": {"chain": "4663"}}, now_iso="2026-09-27T17:00:00+00:00",
                                topup_fn=stub)
    assert seen == []                                         # no state: no check, as before


def test_the_need_tracks_the_chain_book_like_the_gate(conn):
    """A pending 4663 reservation raises the full-book exit reserve for both, by the same amount."""
    empty = er.required_entry_native(conn, "4663")
    m.reserve_pending(conn, mint="0xc0ffee", source_user_id="u1", strategy="s", notional=10.0,
                      reserved_at="2026-09-27T17:00:00+00:00", network_id="4663")
    ctx = er.build_evm_risk_context(conn, usdc_balance=100.0, native_balance=0.001,
                                    day="2026-09-27", network_id="4663")
    assert er.required_entry_native(conn, "4663") == pytest.approx(er.required_native_gas(ctx))
    assert er.required_entry_native(conn, "4663") > empty


def test_the_top_up_pass_hands_the_page_the_weth_it_already_read(tmp_path):
    from test_memecoin_evm_gas_topup_short_lock import E, RUN, _conn, _context
    from sniper.services import memecoin_evm_gas_topup as G
    calls = {"signed": [], "sent": []}
    ctx = _context(calls, native_fn=lambda: int(0.0005 * E), weth_fn=lambda: int(0.005 * E))
    out = G.run_evm_gas_topup_pass(_conn(), context=ctx, now_iso="2026-09-27T13:40:00+00:00",
                                   lock_dir=str(tmp_path), **RUN)
    assert out["state"] == "weth_below_reserve" and calls["sent"] == []
    assert (out["native_before_wei"], out["weth_wei"]) == (int(0.0005 * E), int(0.005 * E))
