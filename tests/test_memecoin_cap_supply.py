"""Market caps on the total-supply basis GMGN shows (operator order "a", 2026-10-02).

Position 68538 (56) read $175K on the card, GMGN $221.8K at the same price: the card multiplied by
the ~796M entry ratio, GMGN by the 1B total supply. The runner pins the coin's on-chain totalSupply
as a DISPLAY-ONLY column; no gate, target or band reads it. An mcap target the operator types off
an FDV card pins fdv_supply on save, so the runner reads that target on the same supply.
"""
import sqlite3
import threading
import time

import pytest
from flask import Flask

from _web_csrf import csrf_client
from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api
from sniper.services import memecoin_evm_price as ep
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

MINT = "0xbe147a143226036c000dc9a0e538d674ce7b7777"
ENTRY_PX = 0.00020633711348225196
ENTRY_MCAP = 164190.5                      # the provider's number: 796M implied supply
TOTAL = 1_000_000_000.0


def _ledger():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open56(c, pid=1, *, total=None, fdv=None, chain="56", mint=MINT):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "entry_mcap, network_id, is_open, execution_mode, opened_at, strategy, "
              "display_total_supply, fdv_supply) "
              "VALUES(?,?,'DANTE',?,89.85,?,?,1,'real','2026-10-02T14:48:23+00:00','alpha_b',?,?)",
              (pid, mint, ENTRY_PX, ENTRY_MCAP, chain, total, fdv))
    c.commit()


def _client(c):
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app)


# --- the column and the tiers -------------------------------------------------------------------

def test_the_display_supply_is_written_once():
    c = _ledger()
    _open56(c)
    assert m.pin_position_display_total_supply(c, 1, TOTAL) == TOTAL
    assert m.pin_position_display_total_supply(c, 1, 5.0) == TOTAL       # never moved


def test_the_cap_supply_tiers():
    assert m.position_cap_supply({"fdv_supply": 9e8, "display_total_supply": TOTAL}) == (9e8, "fdv")
    assert m.position_cap_supply({"display_total_supply": TOTAL}) == (TOTAL, "total")
    for unknown in ({}, None, {"fdv_supply": 0, "display_total_supply": -1},
                    {"display_total_supply": "x"}):
        assert m.position_cap_supply(unknown) == (None, None)


def test_the_display_supply_prefers_the_total_supply_to_a_marks_ratio():
    c = _ledger()
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap) "
              "VALUES ('56', ?, '2026-10-02T14:50:00Z', 2e-4, 159200)", (MINT,))
    pos = {"mint": MINT, "network_id": "56"}
    assert m.position_display_supply(c, pos) == pytest.approx(796e6)
    assert m.position_display_supply(c, dict(pos, display_total_supply=TOTAL)) == TOTAL


# --- the 1 s ticker -------------------------------------------------------------------------------

def _exact_mark(c, mid, *, ts=None):
    from sniper.util.clock import real_utc_now_iso
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, source, quality) "
              "VALUES ('56', ?, ?, ?, ?, 'fastlane_rpc', 'exact_route')",
              (MINT, ts or real_utc_now_iso(), mid, mid))
    c.commit()


def _ticker_row(c):
    from sniper.util.clock import real_utc_now_iso
    return api._real_ticker_payload(c, real_utc_now_iso())["positions"][0]


def test_68538_reads_on_the_total_supply_and_the_targets_keep_their_basis():
    c = _ledger()
    _open56(c, total=TOTAL)
    _exact_mark(c, 0.00023)
    row = _ticker_row(c)
    assert row["cap_supply"] == TOTAL and row["cap_basis"] == "total"
    assert row["market_cap"] == pytest.approx(230_000)
    assert row["market_cap_basis"] == "fdv"
    # The supply the rails convert operator targets with is untouched: the entry ratio.
    assert row["target_supply"] == pytest.approx(ENTRY_MCAP / ENTRY_PX)
    assert row["mcap_basis"] == "circulating"


def test_without_a_verified_supply_caps_stay_unknown_and_targets_keep_their_basis():
    c = _ledger()
    _open56(c)
    _exact_mark(c, 0.00023)
    row = _ticker_row(c)
    assert row["cap_supply"] is None and row["cap_basis"] is None
    assert row["market_cap"] is None and row["market_cap_basis"] is None
    assert row["target_supply"] == pytest.approx(ENTRY_MCAP / ENTRY_PX)


# --- (d) a stuck bag with no price says what it last knew --------------------------------------

def test_a_stuck_bag_with_no_mark_shows_its_entry_quote():
    # DAODUN 68544: no mark of any kind after the fill, stuck_bag=1.
    c = _ledger()
    _open56(c)
    c.execute("UPDATE memecoin_positions SET stuck_bag=1, quote_price=4.3758e-05, "
              "quote_observed_at='2026-10-02T14:52:28.764060+00:00' WHERE id=1")
    c.commit()
    row = _ticker_row(c)
    assert row["selected_exit_price"] is None
    assert (row["last_known_price"], row["last_known_at"], row["last_known_source"]) == (
        4.3758e-05, "2026-10-02T14:52:28.764060+00:00", "entry quote")
    assert row["no_exit_route"] is True


def test_an_old_mark_beats_the_entry_quote_and_a_priced_row_carries_none():
    c = _ledger()
    _open56(c)
    c.execute("UPDATE memecoin_positions SET stuck_bag=1, quote_price=4e-05, "
              "quote_observed_at='2026-10-02T14:52:28+00:00' WHERE id=1")
    _exact_mark(c, 3.1e-05, ts="2026-10-02T14:55:00+00:00")       # far older than any carry window
    row = _ticker_row(c)
    assert (row["last_known_price"], row["last_known_at"], row["last_known_source"]) == (
        3.1e-05, "2026-10-02T14:55:00+00:00", "fastlane_rpc")
    c2 = _ledger()
    _open56(c2)
    _exact_mark(c2, 0.00023)
    priced = _ticker_row(c2)
    assert priced["last_known_price"] is None and priced["no_exit_route"] is False


# --- the runner's display-only pin ---------------------------------------------------------------

@pytest.fixture
def reads(monkeypatch):
    """A fake on-chain read that blocks until the test releases it, counting calls."""
    gate, calls, answer = threading.Event(), [], {"v": TOTAL}

    def fake(chain, mint, *, http=None):
        calls.append((chain, mint))
        assert gate.wait(5)
        return answer["v"], False, 1.0

    monkeypatch.setattr(ep, "evm_total_supply", fake)
    monkeypatch.setattr(ep, "_supply_cache", {})
    monkeypatch.setattr(rr, "_DISPLAY_SUPPLY_READS", {})
    monkeypatch.setattr(rr, "_DISPLAY_SUPPLY_RETRY_AT", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", frozenset({"56", "4663", "8453"}))
    return gate, calls, answer


def _wait_done(key):
    fut = rr._DISPLAY_SUPPLY_READS[key]
    for _ in range(200):
        if fut.done():
            return
        time.sleep(0.01)
    raise AssertionError("read never finished")


def test_a_cached_supply_pins_at_once_and_reads_nothing(reads):
    _gate, calls, _ = reads
    c = _ledger()
    _open56(c)
    ep._supply_cache[("56", MINT)] = TOTAL
    pos = dict(m.get_position(c, 1))
    assert rr.pin_display_total_supply(c, pos) == TOTAL
    assert calls == [] and pos["display_total_supply"] == TOTAL
    assert m.get_position(c, 1)["display_total_supply"] == TOTAL


def test_a_miss_starts_one_read_off_the_sweep_and_a_later_sweep_pins_it(reads):
    gate, calls, _ = reads
    c = _ledger()
    _open56(c)
    pos = dict(m.get_position(c, 1))
    t0 = time.monotonic()
    assert rr.pin_display_total_supply(c, pos) is None          # never waits on the read
    assert rr.pin_display_total_supply(c, pos) is None          # in flight: no second read
    assert time.monotonic() - t0 < 0.5
    gate.set()
    _wait_done(("56", MINT))
    assert rr.pin_display_total_supply(c, pos) == TOTAL
    assert calls == [("56", MINT)]
    assert m.get_position(c, 1)["display_total_supply"] == TOTAL


def test_a_failed_read_backs_off_instead_of_hammering_the_rpc(reads):
    gate, calls, answer = reads
    answer["v"] = None
    gate.set()
    c = _ledger()
    _open56(c)
    pos = dict(m.get_position(c, 1))
    rr.pin_display_total_supply(c, pos, now_mono=100.0)
    _wait_done(("56", MINT))
    assert rr.pin_display_total_supply(c, pos, now_mono=100.0) is None   # collects the failure
    rr.pin_display_total_supply(c, pos, now_mono=200.0)                  # inside the back-off
    assert rr._DISPLAY_SUPPLY_READS == {} and calls == [("56", MINT)]
    rr.pin_display_total_supply(c, pos, now_mono=100.0 + rr.DISPLAY_SUPPLY_RETRY_S + 1)
    _wait_done(("56", MINT))
    assert len(calls) == 2
    assert m.get_position(c, 1)["display_total_supply"] is None


def test_an_unlisted_chain_and_a_pinned_row_are_left_alone(reads):
    _gate, calls, _ = reads
    c = _ledger()
    _open56(c, 2, chain="evm-unknown", mint="0xabc")
    _open56(c, 3, total=TOTAL, mint="0xdef")
    _open56(c, 4, fdv=TOTAL, chain="solana", mint="SoLMint")
    assert rr.pin_display_total_supply(c, dict(m.get_position(c, 2))) is None
    assert rr.pin_display_total_supply(c, dict(m.get_position(c, 3))) == TOTAL
    assert rr.pin_display_total_supply(c, dict(m.get_position(c, 4))) == TOTAL
    assert rr._DISPLAY_SUPPLY_READS == {} and calls == []


@pytest.mark.parametrize("supply", [TOTAL, None])
def test_solana_display_supply_is_read_off_sweep_without_changing_rails(reads, monkeypatch, supply):
    from sniper.execution import solana_real_seams as sol
    gate, calls, _ = reads

    def read(mint, rpc, *, http=None):
        calls.append((mint, rpc))
        assert gate.wait(5)
        return supply, 6 if supply else None

    monkeypatch.setattr(sol, "token_supply", read)
    monkeypatch.setattr(cfg, "solana_read_rpc", lambda: "https://observation.invalid")
    c = _ledger()
    _open56(c, chain="solana", mint="EIN")
    pos = dict(m.get_position(c, 1))
    started = time.monotonic()
    try:
        assert rr.pin_display_total_supply(c, pos) is None
        assert rr.pin_display_total_supply(c, pos) is None
        assert time.monotonic() - started < 0.5
    finally:
        gate.set()
    _wait_done(("solana", "EIN"))
    assert rr.pin_display_total_supply(c, pos) == supply
    stored = m.get_position(c, 1)
    assert stored["display_total_supply"] == supply
    assert stored["fdv_supply"] is None
    assert stored["entry_mcap"] == ENTRY_MCAP
    assert calls == [("EIN", "https://observation.invalid")]


@pytest.mark.parametrize("chain", ["solana", "56", "4663", "8453", "1", "5042"])
def test_display_caps_never_infer_supply_from_our_fill_price(monkeypatch, chain):
    """EIN 71409: entry_mcap belongs to the source; our fill costs 15.6% more."""
    c = _ledger()
    _open56(c, chain=chain, mint="EIN")
    monkeypatch.setattr(api, "_live_marks", lambda now: {})
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES(?, 'EIN', '2026-10-07T10:40:00Z', 0.00013424, 'fastlane_rpc', 'exact_route')",
              (chain,))
    row = api._real_ticker_payload(c, "2026-10-07T10:40:01Z")["positions"][0]
    assert row["market_cap"] is None
    assert row["display_supply"] is None
    assert row["target_supply"] == pytest.approx(ENTRY_MCAP / ENTRY_PX)
    # The next push must carry the newly pinned supply to cards and charts.
    m.pin_position_display_total_supply(c, 1, TOTAL)
    row = api._real_ticker_payload(c, "2026-10-07T10:40:01Z")["positions"][0]
    assert row["display_supply"] == TOTAL
    assert row["market_cap"] == pytest.approx(134240)
    assert row["market_cap_basis"] == "fdv"
    assert row["target_supply"] == pytest.approx(ENTRY_MCAP / ENTRY_PX)


# --- a typed target means what the card showed ---------------------------------------------------

def _runner_mcap_at(c, price):
    pos = dict(m.get_position(c, 1))
    _ov, mcap = rr._resolve_override_and_mcap(c, pos, {}, price, {1: "frozen"}, cfg)
    return mcap


def test_a_typed_target_fires_at_the_price_the_card_shows_for_that_mcap():
    c = _ledger()
    _open56(c, total=TOTAL)
    cli = _client(c)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_target_mcap": 300_000, "mcap_basis": "total", "mcap_supply": TOTAL,
                       "edited_leg": "tp"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["mcap_basis"] == "fdv"
    assert m.get_position(c, 1)["fdv_supply"] == TOTAL
    # The card shows price x cap_supply; the runner must read the same mcap at that price.
    card_supply = _ticker_row(c)["cap_supply"]
    fire_px = 300_000 / card_supply
    assert _runner_mcap_at(c, fire_px) == pytest.approx(300_000)


def test_before_the_save_the_runner_read_the_entry_ratio():
    # The defect the save repairs: on the circ basis $300K fires at about $377K on the card.
    c = _ledger()
    _open56(c, total=TOTAL)
    assert _runner_mcap_at(c, 300_000 / TOTAL) == pytest.approx(300_000 * ENTRY_MCAP / ENTRY_PX / TOTAL)


@pytest.mark.parametrize("body", [
    {"mcap_basis": "circ", "edited_leg": "sl"},               # painted before the pin landed
    {"mcap_basis": "total", "mcap_supply": 0.8 * TOTAL, "edited_leg": "sl"},   # another supply
    {},                                                        # an old page: no basis, no leg
])
def test_a_basis_the_server_does_not_hold_is_refused_and_nothing_is_written(body):
    c = _ledger()
    _open56(c, total=TOTAL)
    r = _client(c).post("/api/memecoin/real/positions/1/exit",
                        json=dict(body, sl_target_mcap=150_000))
    assert r.status_code == 409
    assert m.get_position(c, 1)["fdv_supply"] is None
    assert m.effective_exit_override(c, 1) is None


def test_percent_targets_and_unknown_supply_change_nothing():
    c = _ledger()
    _open56(c, total=TOTAL)
    _open56(c, 2, mint="0xnosupply")
    cli = _client(c)
    assert cli.post("/api/memecoin/real/positions/1/exit", json={"tp_pct": 40}).status_code == 200
    assert m.get_position(c, 1)["fdv_supply"] is None
    r = cli.post("/api/memecoin/real/positions/2/exit", json={"tp_target_mcap": 300_000})
    assert r.status_code == 200 and r.get_json()["mcap_basis"] == "circ"
    assert m.get_position(c, 2)["fdv_supply"] is None


def test_a_pinned_fdv_position_accepts_an_old_page_and_never_moves_its_supply():
    c = _ledger()
    _open56(c, chain="solana", mint="SoLMint", fdv=9e8, total=TOTAL)
    cli = _client(c)
    assert cli.post("/api/memecoin/real/positions/1/exit",
                    json={"tp_target_mcap": 300_000}).status_code == 200
    assert cli.post("/api/memecoin/real/positions/1/exit",
                    json={"tp_target_mcap": 300_000, "mcap_basis": "fdv",
                          "mcap_supply": TOTAL, "edited_leg": "tp"}).status_code == 409
    assert m.get_position(c, 1)["fdv_supply"] == 9e8


# --- the card ---------------------------------------------------------------------------------------

def _card(expr, p):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    app = Path("static/app.js").read_text()

    def method(name, end):
        return app[app.index(f"    {name}("):app.index(f"    {end}(")]

    body = (method("_supplyOf", "_fmtAgo") + method("_operatorPrice", "_tokenPrice")
            + method("_tokenPrice", "_operatorTime") + method("_capBasisTxt", "_realFactsLine")
            + method("_circTargetNote", "_railsBlindWarning"))
    script = ("const esc = s => String(s); global.window = { mcRange: require(process.cwd() + "
              "'/static/mc-range.js') }; const view = { _quoteAge(v) { return v ? 'AGE' : null; }, "
              "_n(v) { return String(v); }, _mcapShort(v) { return String(v); }, " + body + "};"
              " const p = " + json.dumps(p) + "; console.log(JSON.stringify(" + expr + "));")
    return json.loads(subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                     text=True, check=True).stdout)


def test_the_card_values_68538_on_the_total_supply():
    p = {"cap_supply": TOTAL, "cap_basis": "total", "target_supply": ENTRY_MCAP / ENTRY_PX,
         "display_supply": 5.0}
    assert _card("view._supplyOf(p)", p) == TOTAL
    assert _card("view._pxOrMcap(0.00023, view._supplyOf(p))", p) == "$230K"
    assert _card("view._supplyOf(p)", {"target_supply": 7e8}) == 7e8          # no total: as before


def test_an_old_circ_target_says_it_still_reads_circ():
    eff = {"tp_target_mcap": 300000, "tp_pct": 0, "source": "operator"}
    note = "circ basis, re-save to switch"
    assert note in _card("view._exitLegTxt(p.e, 'tp', 25, null, 'total')", {"e": eff})
    assert note not in _card("view._exitLegTxt(p.e, 'tp', 25, null, 'fdv')", {"e": eff})
    assert note not in _card("view._exitLegTxt(p.e, 'tp', 25, null, 'total')",
                             {"e": {"tp_pct": 40, "source": "operator"}})


def test_daodun_says_no_exit_route_and_what_it_last_knew():
    p = {"no_exit_route": True, "last_known_price": 4.3758e-05, "last_known_source": "entry quote",
         "last_known_at": "2026-10-02T14:52:28Z", "cap_supply": TOTAL}
    txt = _card("view._lastKnownTxt(p)", p)
    assert txt == " · no exit route · last known $44K (entry quote) · AGE ago", txt
    assert _card("view._lastKnownTxt(p)", {"no_exit_route": False}) == ""


def test_the_editor_sends_the_basis_it_drew_with_an_mcap_target():
    from pathlib import Path
    app = Path("static/app.js").read_text()
    save = app[app.index("    async _saveExit("):app.index("    async _toggleMirror(")]
    assert "payload.mcap_basis = ctx.capBasis;" in save and "payload.mcap_supply = ctx.capSupply;" in save
    assert "payload.edited_leg = ctx.leg;" in save
    assert "ctx.capBasis === 'total' && mode === 'mcap' && carried != null" in save
    assert "capBasis: p.cap_basis || 'circ'" in app


def test_a_failed_override_write_takes_the_pin_back_with_it(monkeypatch):
    c = _ledger()
    _open56(c, total=TOTAL)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(m, "set_position_exit_override", boom)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    app.testing = False                  # the 500 a real request gets, not a re-raised exception
    r = csrf_client(app).post("/api/memecoin/real/positions/1/exit",
                              json={"tp_target_mcap": 300_000, "mcap_basis": "total",
                                    "mcap_supply": TOTAL, "edited_leg": "tp"})
    assert r.status_code == 500
    assert m.get_position(c, 1)["fdv_supply"] is None


# --- Gamma MED: the pin must not move the other leg's trigger price ---------------------------------

CIRC = ENTRY_MCAP / ENTRY_PX


def _sl_floor_set_while_circ(c, cli):
    # The operator set a $100K floor right after entry, before the display supply pinned.
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"sl_target_mcap": 100_000, "mcap_basis": "circ", "edited_leg": "sl"})
    assert r.status_code == 200, r.get_json()
    c.execute("UPDATE memecoin_positions SET display_total_supply=? WHERE id=1", (TOTAL,))
    c.commit()


def _runner_override(c):
    pos = dict(m.get_position(c, 1))
    ov, _mcap = rr._resolve_override_and_mcap(c, pos, {}, 1e-4, {1: "frozen"}, cfg)
    return ov


def test_a_tp_save_that_pins_keeps_the_carried_floor_at_its_trigger_price():
    c = _ledger()
    _open56(c)
    cli = _client(c)
    _sl_floor_set_while_circ(c, cli)
    floor_px = 100_000 / CIRC                        # where the runner fired the floor before
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_target_mcap": 300_000, "sl_target_mcap": 100_000, "edited_leg": "tp",
                       "mcap_basis": "total", "mcap_supply": TOTAL, "supersede_band": True})
    assert r.status_code == 200, r.get_json()
    conv = r.get_json()["converted"]["sl"]
    assert conv["from"] == 100_000 and conv["to"] == pytest.approx(100_000 * TOTAL / CIRC)
    ov = _runner_override(c)
    assert ov["tp_target_mcap"] == 300_000
    # After the pin the runner reads mid x 1B; the floor still fires at the same price.
    assert _runner_mcap_at(c, floor_px) == pytest.approx(ov["sl_target_mcap"])


def test_a_percent_edit_never_pins_and_leaves_the_carried_floor_alone():
    c = _ledger()
    _open56(c)
    cli = _client(c)
    _sl_floor_set_while_circ(c, cli)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_pct": 40, "sl_target_mcap": 100_000, "edited_leg": "tp"})
    assert r.status_code == 200 and r.get_json()["converted"] == {}
    assert m.get_position(c, 1)["fdv_supply"] is None
    assert _runner_override(c)["sl_target_mcap"] == 100_000


def test_a_single_leg_fdv_save_converts_the_current_floor_without_a_carried_value():
    c = _ledger()
    _open56(c)
    cli = _client(c)
    _sl_floor_set_while_circ(c, cli)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_target_mcap": 300_000, "edited_leg": "tp", "mcap_basis": "total",
                       "mcap_supply": TOTAL, "supersede_band": True})
    assert r.status_code == 200
    assert r.get_json()["converted"]["sl"]["from"] == 100_000
    assert m.effective_exit_override(c, 1)["sl_target_mcap"] == pytest.approx(100_000 * TOTAL / CIRC)
    assert not c.in_transaction


def test_a_band_from_elsewhere_blocks_a_pin_unless_the_save_replaces_it():
    c = _ledger()
    _open56(c, total=TOTAL)
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    cli = _client(c)
    body = {"sl_target_mcap": 150_000, "mcap_basis": "total", "mcap_supply": TOTAL,
            "edited_leg": "sl"}
    assert cli.post("/api/memecoin/real/positions/1/exit", json=body).status_code == 409
    tp_keeping_band = {"tp_target_mcap": 500_000, "mcap_basis": "total", "mcap_supply": TOTAL,
                       "edited_leg": "tp"}
    assert cli.post("/api/memecoin/real/positions/1/exit", json=tp_keeping_band).status_code == 409
    assert m.get_position(c, 1)["fdv_supply"] is None
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_target_mcap": 500_000, "mcap_basis": "total", "mcap_supply": TOTAL,
                       "edited_leg": "tp", "supersede_band": True})
    assert r.status_code == 200 and m.active_exit_target(c, 1) is None
    assert r.get_json()["band_cancelled"] == 1
    assert m.get_position(c, 1)["fdv_supply"] == TOTAL


def test_the_superseded_band_is_gone_before_the_pin_commits(monkeypatch):
    # Gamma LOW: no sweep may read the old circulating band on FDV between the two writes.
    c = _ledger()
    _open56(c, total=TOTAL)
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    seen = []
    real_pin = m.pin_position_fdv_supply

    def pin(conn, pid, supply):
        seen.append(m.active_exit_target(conn, pid))
        return real_pin(conn, pid, supply)

    monkeypatch.setattr(m, "pin_position_fdv_supply", pin)
    r = _client(c).post("/api/memecoin/real/positions/1/exit",
                        json={"tp_target_mcap": 500_000, "mcap_basis": "total",
                              "mcap_supply": TOTAL, "edited_leg": "tp", "supersede_band": True})
    assert r.status_code == 200 and seen == [None]


# --- Codex R1 (2026-10-06): an exit edit is all-or-nothing ---------------------------------------

def _state(c):
    ov = m.effective_exit_override(c, 1) or {}
    return (m.active_exit_target(c, 1) is not None, m.get_position(c, 1)["fdv_supply"],
            ov.get("tp_target_mcap"), ov.get("sl_target_mcap"))


def _failing_after(monkeypatch, name):
    """`name` does its write, then the request dies: everything it and earlier steps wrote must go."""
    real = getattr(m, name)

    def fail(*a, **k):
        real(*a, **k)
        raise RuntimeError("died after %s" % name)
    monkeypatch.setattr(m, name, fail)


@pytest.mark.parametrize("step", ["cancel_exit_target", "pin_position_fdv_supply",
                                  "set_position_exit_override"])
def test_a_tp_edit_that_dies_after_any_step_keeps_the_whole_old_exit(monkeypatch, step):
    c = _ledger()
    _open56(c, total=TOTAL)
    m.set_position_exit_override(c, 1, "operator", sl_pct=-30.0, actor="operator")
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    before = _state(c)
    _failing_after(monkeypatch, step)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    app.testing = False
    r = csrf_client(app).post("/api/memecoin/real/positions/1/exit",
                              json={"tp_target_mcap": 500_000, "mcap_basis": "total",
                                    "mcap_supply": TOTAL, "edited_leg": "tp", "supersede_band": True})
    assert r.status_code == 500
    assert _state(c) == before == (True, None, None, None)


def test_a_circulating_tp_edit_that_dies_after_the_override_keeps_its_band(monkeypatch):
    # The supersede cancel after the override write is part of the same edit.
    c = _ledger()
    _open56(c)
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    calls = {"n": 0}
    real = m.cancel_exit_target

    def cancel_then_die(*a, **k):
        real(*a, **k)
        raise RuntimeError("died after the band cancel")
    monkeypatch.setattr(m, "cancel_exit_target", cancel_then_die)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    app.testing = False
    r = csrf_client(app).post("/api/memecoin/real/positions/1/exit",
                              json={"tp_pct": 50, "supersede_band": True})
    assert r.status_code == 500
    assert m.active_exit_target(c, 1) is not None and m.effective_exit_override(c, 1) is None


def test_a_clear_that_dies_after_its_band_cancel_keeps_the_override_and_the_band(monkeypatch):
    c = _ledger()
    _open56(c)
    m.set_position_exit_override(c, 1, "operator", tp_pct=80.0, actor="operator")
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    _failing_after(monkeypatch, "cancel_exit_target")
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    app.testing = False
    r = csrf_client(app).post("/api/memecoin/real/positions/1/exit", json={"clear": True})
    assert r.status_code == 500
    assert m.active_exit_target(c, 1) is not None
    assert (m.effective_exit_override(c, 1) or {}).get("tp_pct") == 80.0


def test_a_tp_edit_that_succeeds_writes_exactly_the_new_exit():
    c = _ledger()
    _open56(c, total=TOTAL)
    m.set_exit_target(c, 1, 400_000, 400_000, now_iso="2026-10-02T15:00:00+00:00", actor="telegram")
    c.commit()
    r = _client(c).post("/api/memecoin/real/positions/1/exit",
                        json={"tp_target_mcap": 500_000, "mcap_basis": "total", "mcap_supply": TOTAL,
                              "edited_leg": "tp", "supersede_band": True})
    assert r.status_code == 200 and r.get_json()["band_cancelled"] == 1
    assert _state(c) == (False, TOTAL, 500_000, None)
    assert not c.in_transaction                                    # committed, nothing left open
