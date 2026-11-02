from sniper.services.memecoin_links import operator_links


def test_ink_manual_close_intent_reaches_runner_exit_after_entry_disarm():
    import sqlite3
    from sniper.execution.evm_exit_executor import execute_exit_evm
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_manual_exit_intents import drain, enqueue, read_status
    from sniper.services.memecoin_real_runner import make_venue_exit_fn

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    mint = "0x" + "b" * 40
    pid = db.open_position(
        conn, source_user_id="source", source_handle="source", mint=mint, ticker="INK",
        entry_price=2.0, entry_notional=10.0, entry_fill_source="chain",
        entry_lag_s=1.0, their_size_usd=10.0,
        opened_at="2026-09-29T09:00:00Z", strategy="alpha_b",
        execution_mode="real", network_id="57073")
    db.insert_fill(conn, position_id=pid, kind="entry", price=2.0, qty=5.0,
                   notional=10.0, fill_source="chain", fraction=1.0,
                   created_at="2026-09-29T09:00:00Z")
    intent_id = enqueue(conn, position_id=pid, action="close",
                        now_iso="2026-09-29T09:01:00Z")
    sent = []
    def ink_exit(c, position, reason, at):
        return execute_exit_evm(
            c, position, reason, at,
            quote_fn=lambda *_a: {"sellAmount": str(5 * 10**18),
                "buyAmount": str(6 * 10**15), "minBuyAmount": str(5 * 10**15),
                "transaction": {"to": "0xrouter"}, "issues": {}},
            approve_fn=lambda *_a: True,
            sign_fn=lambda _tx: ("0xraw", "0x" + "c" * 64, 2),
            send_fn=lambda raw: sent.append(raw) or True,
            confirm_fn=lambda _hash: "confirmed", usdc_fn=lambda: 100.0,
            token_balance_fn=lambda _mint: 5.0,
            receipt_proceeds_fn=lambda _hash: 12.0, require_receipt_proceeds=True,
            quote_asset_usd=2000.0, quote_asset_decimals=18, slippage_ladder=[3])
    contexts = {"57073": {"exit_fn": ink_exit}}
    result = drain(conn, make_venue_exit_fn(contexts), lambda *_a: None,
                   now_fn=lambda: "2026-09-29T09:01:01Z", position_fn=db.get_position)
    assert result == [(intent_id, "done")]
    assert len(sent) == 1
    assert read_status(conn, intent_id)["result_status"] == "closed"
    assert db.get_position(conn, pid)["is_open"] == 0


def test_ink_position_has_verified_explorer_and_no_invented_chart():
    links = operator_links("57073", "0x" + "b" * 40,
                           tx_sig="0x" + "a" * 64, position_id=17)
    assert links["transaction"] == "https://explorer.inkonchain.com/tx/" + "0x" + "a" * 64
    assert links["explorer"] == "https://explorer.inkonchain.com/address/" + "0x" + "b" * 40
    assert links["position"].endswith("mc_chain=57073&mc_position=17")
    assert "chart" not in links


# --- dashboard chain truth (Alpha, 2026-09-29) --------------------------------------------------
import json as _json
import pathlib as _pathlib
import shutil as _shutil
import subprocess as _subprocess

_APP = (_pathlib.Path(__file__).resolve().parents[2] / "static" / "app.js").read_text()


def _chain_block():
    """CHAIN_META .. chainLabel: the chain registry the whole tab reads, run as plain JS."""
    start = _APP.index("  const CHAIN_META = {")
    return _APP[start:_APP.index("  function chainLabel(chain)")]


def _run(js):
    node = _shutil.which("node")
    assert node, "node is required for the dashboard contract tests"
    out = _subprocess.run([node, "-e", _chain_block() + js], capture_output=True, text=True,
                          check=True)
    return _json.loads(out.stdout)


def test_an_ink_position_renders_as_ink_not_an_unknown_chain():
    got = _run("console.log(JSON.stringify([chainOf(null, '57073'), chainOf('0xabc', 'ink'), "
               "CHAIN_META.ink && CHAIN_META.ink.label, CHAIN_ORDER.includes('ink')]));")
    assert got == ["ink", "ink", "INK", True]


def test_ink_is_not_funded_until_the_server_allowlists_it():
    got = _run("const before = FUNDED_CHAINS.has('ink'); "
               "syncFundedChains({'8453': 3, '4663': 5, '57073': 0}); const on = [...FUNDED_CHAINS].sort(); "
               "syncFundedChains({'8453': 3}); const off = FUNDED_CHAINS.has('ink'); "
               "syncFundedChains(undefined); const kept = FUNDED_CHAINS.has('base'); "
               "console.log(JSON.stringify([before, on, off, kept]));")
    before, on, off, kept = got
    assert before is False, "the first-paint default must not mark Ink funded"
    assert on == ["base", "ink", "robin", "solana"]
    assert off is False, "a chain removed from the allowlist must stop being armable here"
    assert kept is True, "an unreadable accounting reply keeps the last known set"


def test_ink_labels_gas_and_marks_use_the_ink_chain_id():
    assert "'57073': 'Ink (EVM)'" in _APP
    assert "57073: 'ETH'" in _APP
    assert "ink: '57073'" in _APP[_APP.index("_CHAIN_ID_OF_SLUG"):][:200]
    slugs = _APP[_APP.index("_GMGN_CHAIN_SLUG:"):][:200]
    assert "ink" not in slugs.split("}")[0], "no GMGN page is verified for Ink: no invented link"


def _accounting(monkeypatch, manifest=None, raise_exc=False):
    import sqlite3
    from flask import Flask
    from sniper.persistence import db_memecoin as m
    from sniper.routes import api_memecoin as api
    from sniper.settings import memecoin as cfg
    if raise_exc:
        def boom():
            raise OSError("disk")
        monkeypatch.setattr(cfg, "_ink_capability_manifest", boom)
    elif manifest is not None:
        monkeypatch.setattr(cfg, "_ink_capability_manifest", lambda: manifest)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({
        "conn": conn, "acct_stats_fn": lambda _m: {}, "real_mark_fn": lambda _p: {},
        "acct_balances_fn": lambda: {}}))
    r = app.test_client().get("/api/memecoin/real/accounting")
    assert r.status_code == 200
    return r.get_json()["chain_admission"]["57073"]


def test_accounting_reports_why_ink_cannot_admit(monkeypatch):
    got = _accounting(monkeypatch)                      # the pinned repo manifest
    assert got["reason"] == "ink_activation_blocked"
    assert got["capabilities"]["route_full_exit"] == "unsupported"
    assert any(v["route_class"] == "uniswap_v3" for v in got["venues"])


def test_an_unreadable_ink_gate_is_reported_never_ready(monkeypatch):
    got = _accounting(monkeypatch, raise_exc=True)
    assert got["reason"].startswith("ink_capability_unreadable")
    ready = {"chain_id": 57073, "funded_activation": "ready", "required_capabilities": {}}
    assert _accounting(monkeypatch, manifest=ready)["reason"] == "ink_market_data_unsupported"


# --- manual control edge cases on Ink (Alpha, 2026-09-29) ---------------------------------------
def _ink_book():
    import sqlite3
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pid = db.open_position(
        conn, source_user_id="source", source_handle="source", mint="0x" + "b" * 40, ticker="INK",
        entry_price=2.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=1.0,
        their_size_usd=10.0, opened_at="2026-09-29T09:00:00Z", strategy="alpha_b",
        execution_mode="real", network_id="57073")
    db.insert_fill(conn, position_id=pid, kind="entry", price=2.0, qty=5.0, notional=10.0,
                   fill_source="chain", fraction=1.0, created_at="2026-09-29T09:00:00Z")
    return conn, pid


def test_ink_close_without_an_ink_context_is_refused_by_name_and_keeps_the_position():
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_manual_exit_intents import drain, enqueue, read_status
    from sniper.services.memecoin_real_runner import make_venue_exit_fn
    conn, pid = _ink_book()
    iid = enqueue(conn, position_id=pid, action="close", now_iso="2026-09-29T09:01:00Z")
    base_only = {"8453": {"exit_fn": lambda *_a: (_ for _ in ()).throw(AssertionError("wrong chain"))}}
    got = drain(conn, make_venue_exit_fn(base_only), lambda *_a: None,
                now_fn=lambda: "2026-09-29T09:01:01Z", position_fn=db.get_position)
    assert got == [(iid, "refused")]
    st = read_status(conn, iid)
    assert st["result_status"] == "no_venue"
    assert db.get_position(conn, pid)["is_open"] == 1, "a refused close must never fake-close"


def test_ink_duplicate_close_sends_once_and_the_second_is_refused_not_open():
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_manual_exit_intents import drain, enqueue, read_status
    from sniper.services.memecoin_real_runner import make_venue_exit_fn
    conn, pid = _ink_book()
    sent = []

    def ink_exit(c, position, reason, at):
        sent.append(position["id"])
        db.close_position(c, position["id"], at, 2.0, reason)
        return {"status": "closed", "realized": 2.0, "tx_hash": "0x" + "c" * 64}
    first = enqueue(conn, position_id=pid, action="close", now_iso="2026-09-29T09:01:00Z")
    second = enqueue(conn, position_id=pid, action="close", now_iso="2026-09-29T09:01:00Z")
    got = drain(conn, make_venue_exit_fn({"57073": {"exit_fn": ink_exit}}), lambda *_a: None,
                now_fn=lambda: "2026-09-29T09:01:01Z", position_fn=db.get_position)
    assert sent == [pid], "one close must reach the executor exactly once"
    assert dict(got) == {first: "done", second: "refused"}
    assert read_status(conn, second)["result_status"] == "not_open"


def test_an_expired_ink_intent_never_reaches_the_executor():
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_manual_exit_intents import drain, enqueue, read_status
    from sniper.services.memecoin_real_runner import make_venue_exit_fn
    conn, pid = _ink_book()
    iid = enqueue(conn, position_id=pid, action="close", now_iso="2026-09-29T09:01:00Z")
    got = drain(conn, make_venue_exit_fn({"57073": {"exit_fn": lambda *_a: (_ for _ in ()).throw(
                    AssertionError("expired intent executed"))}}), lambda *_a: None,
                now_fn=lambda: "2026-09-30T09:01:00Z", position_fn=db.get_position)
    assert got == []
    assert read_status(conn, iid)["state"] == "expired"
    assert db.get_position(conn, pid)["is_open"] == 1
