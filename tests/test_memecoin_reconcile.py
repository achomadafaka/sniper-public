"""Item 17 (min bar) — real position vs on-chain reconciliation. Each sweep, compare the ledger's
expected token qty (open_qty) to the wallet's actual balance; divergence beyond tolerance (missed
sell / manual move / mis-recorded fill) is DRIFT -> alert. Paper positions are ignored."""
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_reconcile import reconcile_real_positions


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _real_pos(c, mint, qty):
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint=mint, ticker="",
                          entry_price=0.02, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.02, qty=qty, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t")
    return pid


def test_no_drift_when_chain_matches_ledger():
    c = _c(); _real_pos(c, "M1", 500.0)
    alerts = []
    drifts = reconcile_real_positions(c, lambda mint: 500.0,
                                      alert_fn=lambda mint, pid: alerts.append(mint))
    assert drifts == [] and alerts == []


def test_drift_when_tokens_gone_alerts():
    c = _c(); pid = _real_pos(c, "M1", 500.0)
    alerts = []
    drifts = reconcile_real_positions(c, lambda mint: 0.0,     # position open but wallet empty
                                      alert_fn=lambda mint, p: alerts.append((mint, p)))
    assert len(drifts) == 1 and drifts[0]["position_id"] == pid
    assert alerts == [("M1", pid)]


def test_small_dust_within_tolerance_is_not_drift():
    c = _c(); _real_pos(c, "M1", 500.0)
    drifts = reconcile_real_positions(c, lambda mint: 490.0, tolerance_pct=5.0)   # 2% < 5%
    assert drifts == []


def test_paper_positions_ignored():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="MP", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", strategy="alpha_b")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="j", fraction=1.0, created_at="t")
    drifts = reconcile_real_positions(c, lambda mint: 0.0)     # would drift IF it were counted
    assert drifts == []


def test_position_aware_balance_fn_routes_by_network():
    # a Solana and an EVM real position, each reconciled against its own venue's balance reader
    c = _c()
    sol = _real_pos(c, "So1Mint", 500.0)
    m.open_position(c, source_user_id="u", source_handle="(real)", mint="0xevm", ticker="",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
                    their_size_usd=0.0, opened_at="t", strategy="alpha_b", execution_mode="real",
                    network_id="8453")
    # re-tag the solana pos so routing is by network_id
    c.execute("UPDATE memecoin_positions SET network_id='solana' WHERE id=?", (sol,))
    evm_pid = c.execute("SELECT id FROM memecoin_positions WHERE mint='0xevm'").fetchone()[0]
    m.insert_fill(c, position_id=evm_pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t")

    def balance_fn(pos):                                     # route by venue
        return 500.0 if pos["network_id"] == "solana" else 0.0   # EVM tokens gone -> drift

    alerts = []
    drifts = reconcile_real_positions(c, balance_fn=balance_fn,
                                      alert_fn=lambda mint, pid: alerts.append(mint))
    assert [d["mint"] for d in drifts] == ["0xevm"]         # only the EVM position drifted
    assert alerts == ["0xevm"]


def test_failed_balance_read_is_unreadable_not_drift():
    """A read that RAISES is not evidence the tokens are gone. 2026-07-29: pos#23496 held
    39,178.307637 NEEGY against a ledger expecting 39,178.307636 and was alerted as a 100% drift
    because both RPCs were 429ing and the balance readers report 0.0 on any error."""
    c = _c(); pid = _real_pos(c, "M1", 500.0)
    alerts = []

    def unreadable(mint):
        raise RuntimeError("HTTP 429 Too Many Requests")

    findings = reconcile_real_positions(c, unreadable,
                                        alert_fn=lambda mint, p: alerts.append((mint, p)))
    assert [f["status"] for f in findings] == ["unreadable"]
    assert findings[0]["position_id"] == pid and findings[0]["actual"] is None
    assert alerts == []                                     # never paged on a failed read


def test_genuine_zero_balance_still_drifts_and_alerts():
    """The fix must not mute a REAL empty wallet — only a failed read."""
    c = _c(); pid = _real_pos(c, "M1", 500.0)
    alerts = []
    findings = reconcile_real_positions(c, lambda mint: 0.0,
                                        alert_fn=lambda mint, p: alerts.append((mint, p)))
    assert [f["status"] for f in findings] == ["drift"]
    assert alerts == [("M1", pid)]


def _real_on(c, mint, network):
    pid = _real_pos(c, mint, 500.0)
    c.execute("UPDATE memecoin_positions SET network_id=? WHERE id=?", (network, pid))
    return pid


def test_network_ids_limits_reconcile_to_the_named_networks():
    c = _c(); _real_on(c, "So1A", "solana"); evm = _real_on(c, "0xb", "1")
    findings = reconcile_real_positions(c, lambda mint: 0.0, network_ids={"1"})
    assert [f["position_id"] for f in findings] == [evm]


def test_exclude_network_ids_skips_only_the_excluded_networks():
    c = _c(); sol = _real_on(c, "So1A", "solana"); _real_on(c, "0xb", "1")
    orphan = _real_on(c, "0xc", "999")                    # inventory with no context anywhere
    findings = reconcile_real_positions(c, lambda mint: 0.0, exclude_network_ids={"1"})
    assert sorted(f["position_id"] for f in findings) == sorted([sol, orphan])
