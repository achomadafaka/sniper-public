"""Pump curve volume tape on REAL public processed frames (tests/fixtures/memecoin/pump_tape_frames.jsonl,
captured 2026-09-26 from wss://api.mainnet-beta.solana.com, slot 450791133).

The oracle for each trade is Beta's independent full-layout decoder
(pump_curve_buy.parse_trade_event), not this module.
"""
import json
from pathlib import Path

from solders.pubkey import Pubkey

from sniper.execution.pump_curve_buy import parse_trade_event
from sniper.services import memecoin_pump_tape as pt
from sniper.services.memecoin_opportunity_live import parse_solana_log_notification

# A REAL frame whose TradeEvent carries shareholders (the variable-length vec before quote_mint);
# the six above all carry none, which put a wrong element size out of reach.
SHAREHOLDER_FRAME = (Path(__file__).parent / "fixtures/memecoin/pump_tape_shareholder_frame.json").read_text()
FRAMES = [l for l in (Path(__file__).parent / "fixtures/memecoin/pump_tape_frames.jsonl").read_text().splitlines() if l]


def _oracle(text):
    logs = parse_solana_log_notification(json.loads(text))["logs"]
    return [parse_trade_event(l) for l in logs if l.startswith("Program data: vdt/")]


def _fed(t0=10_000.0):
    tape = pt.PumpTape(clock=lambda: t0)
    tape.mark_connected(t0 - 4000.0)           # up for longer than an hour
    for i, text in enumerate(FRAMES):
        assert tape.ingest(text, t0 - 10.0 + i) == 1
    return tape


def test_decode_matches_the_independent_decoder():
    assert _oracle(SHAREHOLDER_FRAME)[0]["shareholders"]
    for text in FRAMES + [SHAREHOLDER_FRAME]:
        (want,) = _oracle(text)
        tape = pt.PumpTape(clock=lambda: 0.0)
        tape.ingest(text, 0.0)
        ((mint, m),) = tape._mints.items()
        (mono, lamports, is_buy, user, sig) = m.recent[0]
        assert str(Pubkey.from_bytes(mint)) == want["mint"]
        assert str(Pubkey.from_bytes(user)) == want["user"]
        assert (lamports, is_buy) == (want["sol_amount"], want["is_buy"])


def test_windows_sum_real_trades_and_exclude_the_source():
    tape, t0 = _fed(), 10_000.0
    by_mint = {}
    for text in FRAMES:
        (e,) = _oracle(text)
        by_mint.setdefault(e["mint"], []).append((e["sol_amount"], e["is_buy"], e["user"],
                                                  json.loads(text)["params"]["result"]["value"]["signature"]))
    mint, trades = next((k, v) for k, v in by_mint.items() if len(v) == 2)   # two trades, one mint
    f = tape.volume_facts(mint)
    total = sum(t[0] for t in trades) / 1e9
    assert f["m1_sol"] == f["m5_sol"] == f["h1_sol"] == total
    assert f["trades_m5"] == 2 and f["traders_m5"] == len({t[2] for t in trades})
    assert f["m5_buy_sol"] == sum(t[0] for t in trades if t[1]) / 1e9
    g = tape.volume_facts(mint, exclude_signatures={trades[0][3]})
    assert g["m5_sol"] == g["h1_sol"] == trades[1][0] / 1e9 and g["trades_m5"] == 1


def test_unknown_is_not_zero():
    t0 = 10_000.0
    tape = pt.PumpTape(clock=lambda: t0)
    assert tape.volume_facts(str(Pubkey.default())) is None               # never connected
    tape.mark_connected(t0 - 120.0)
    tape.ingest(FRAMES[0], t0 - 1.0)
    f = tape.volume_facts(str(Pubkey.default()))                          # up 120 s, unseen mint
    assert (f["m1_sol"], f["m5_sol"], f["h1_sol"], f["last_trade_age_s"]) == (0.0, None, None, None)
    assert (f["m5_uncovered_s"], f["h1_uncovered_s"]) == (180.0, 3480.0)
    assert tape.volume_facts(str(Pubkey.default()), now=t0 + pt.DEAD_TAPE_S + 1) is None  # silent tape


def test_an_outage_makes_overlapping_windows_a_lower_bound():
    tape, t0 = _fed(), 10_000.0                 # up since t0-4000, trades at t0-10 .. t0-5
    tape.mark_disconnected(t0 - 100.0)
    tape.mark_connected(t0 - 90.0)              # a 10 s outage, inside m5 and h1, outside m1
    mint = _oracle(FRAMES[0])[0]["mint"]
    f = tape.volume_facts(mint)
    assert f["m1_sol"] is not None and f["m1_uncovered_s"] == 0.0
    assert f["m5_sol"] is None and f["h1_sol"] is None and f["trades_m5"] is None
    assert f["m5_uncovered_s"] == f["h1_uncovered_s"] == 10.0
    assert f["m5_sol_min"] == f["m1_sol"] > 0 and f["coverage_since"] == t0 - 90.0
    tape.mark_disconnected(t0 - 2.0)            # socket down right now: every window is blind
    g = tape.volume_facts(mint)
    assert g["m1_sol"] is None and g["m1_uncovered_s"] == 2.0 and g["m1_sol_min"] == f["m1_sol"]


def test_prefilter_failed_tx_and_eviction():
    tape = pt.PumpTape(clock=lambda: 0.0, max_mints=2)
    quiet = FRAMES[0].replace("Program data: vdt/", "Program data: xdt/")
    assert tape.ingest(quiet, 0.0) == 0 and tape.stats["prefiltered"] == 1
    failed = json.loads(FRAMES[0])
    failed["params"]["result"]["value"]["err"] = {"InstructionError": [2, {"Custom": 6003}]}
    assert tape.ingest(json.dumps(failed), 0.0) == 0
    for i, text in enumerate(FRAMES):        # 4 distinct mints into a cap of 2
        tape.ingest(text, 100.0 + i)
    tape._sweep(200.0)
    assert len(tape._mints) == 2
    tape._sweep(200.0 + pt.H1_S + 10)         # idle past an hour
    assert tape._mints == {}
