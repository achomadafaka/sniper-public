"""4663 fast lane M0, the sequencer-feed shadow (spec 2026-10-01-memecoin-4663-fast-lane-design.md
§2.1, §6). Read-only: it decodes armed wallets' Relay fills from the public feed and records what the
lane would have done. It never signs and never sends."""
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import to_checksum_address

from sniper.services import memecoin_rh_feed_shadow as shadow
from sniper.services.memecoin_evm_onchain_watch import PERMIT2_TYPES

FIX = json.loads((Path(__file__).parent / "fixtures" / "rh_relay_fill_4663.json").read_text())
RAW = bytes.fromhex(FIX["raw"][2:])
WALLET = FIX["wallet"].lower()
MINT = FIX["mint"].lower()
SOLVER = FIX["solver"].lower()
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
QUOTES = frozenset({USDG, WETH})
WATCHED = {WALLET: ("uid-degen", "DegenCapitalLLC")}


def _deadline():
    _payer, permit, *_rest = decode(PERMIT2_TYPES, _input()[4:])
    return permit[2]


def _input():
    import rlp
    return bytes(rlp.decode(RAW[1:])[7])


def _frame(raws, seq=77614300, ts=None):
    ts = _deadline() - 60 if ts is None else ts
    seg = b"".join((len(r) + 1).to_bytes(8, "big") + bytes([4]) + r for r in raws)
    l2 = base64.b64encode(bytes([3]) + seg).decode()
    return json.dumps({"version": 1, "messages": [{
        "sequenceNumber": seq, "blockHash": "0x" + "ab" * 32, "signatureV2": "0x",
        "message": {"message": {"header": {"kind": 3, "timestamp": ts}, "l2Msg": l2}}}]})


def _resign(data, key=None, to=None):
    """The fixture's calldata in a fresh type-2 tx signed by `key` (an attacker, not a solver)."""
    acct = Account.create() if key is None else Account.from_key(key)
    tx = {"type": 2, "chainId": 4663, "nonce": 1, "maxFeePerGas": 40_000_000,
          "maxPriorityFeePerGas": 0, "gas": 900_000, "value": 0, "data": data,
          "to": Account.from_key(b"\x01" * 32).address if to == "other" else
          to_checksum_address(shadow.RELAY_APPROVAL_PROXY)}
    return bytes(Account.sign_transaction(tx, acct.key).raw_transaction), acct.address.lower()


PAYER = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"


def _shadow(watched=WATCHED, solvers=(SOLVER,), payers=(PAYER,)):
    out = []
    s = shadow.FeedShadow(watched_fn=lambda: watched, solvers=frozenset(solvers),
                          payers=frozenset(payers), quote_assets=QUOTES, sink=out.append)
    return s, out


def test_l2_batches_unpack_to_signed_tx_bytes():
    one, two = b"\x02abc", b"\x02defg"
    seg = lambda k, p: (len(p) + 1).to_bytes(8, "big") + bytes([k]) + p
    batch = bytes([3]) + seg(4, one) + seg(4, two) + seg(9, b"xx")
    assert list(shadow.l2_txs(batch)) == [one, two]
    assert list(shadow.l2_txs(bytes([4]) + one)) == [one] and list(shadow.l2_txs(b"")) == []


def test_the_real_relay_fill_decodes_to_its_wallet_mint_and_payment():
    to, data = shadow.tx_to_input(RAW)
    assert to == shadow.RELAY_APPROVAL_PROXY and data[:4].hex() == "0a2b8f36"
    fill = shadow.decode_relay_fill(data, WATCHED, QUOTES)
    assert fill["wallet"] == WALLET and fill["mint"] == MINT and fill["side"] == "buy"
    assert fill["payment_token"] == USDG and fill["paid"] == 994518588
    assert shadow.recover_sender(RAW) == SOLVER


def test_a_real_fill_from_a_known_solver_is_a_signal():
    s, out = _shadow()
    recs = s.handle_frame(_frame([RAW]), arrival_utc="2026-10-01T19:00:00+00:00", arrival_mono=1.0)
    assert len(recs) == 1 and out == recs
    r = recs[0]
    assert r["signal"] is True and r["reason"] == "signal" and r["sender"] == SOLVER
    assert r["wallet"] == WALLET and r["mint"] == MINT and r["seq"] == 77614300
    assert r["decode_ms"] >= 0 and r["recover_ms"] >= 0 and r["source_user_id"] == "uid-degen"


def test_a_relay_shaped_tx_from_a_non_solver_naming_an_armed_wallet_is_never_a_signal():
    _to, data = shadow.tx_to_input(RAW)
    forged, attacker = _resign("0x" + data.hex())
    s, _out = _shadow()
    (r,) = s.handle_frame(_frame([forged]), arrival_utc="2026-10-01T19:00:00+00:00", arrival_mono=1.0)
    assert r["sender"] == attacker and r["signal"] is False and r["reason"] == "unknown_sender"


def test_an_expired_permit_and_a_repeated_wallet_nonce_are_not_signals():
    s, _out = _shadow()
    deadline = shadow.decode_relay_fill(shadow.tx_to_input(RAW)[1], WATCHED, QUOTES)["deadline"]
    (late,) = s.handle_frame(_frame([RAW], ts=deadline + 1), arrival_utc="x", arrival_mono=1.0)
    assert late["signal"] is False and late["reason"] == "permit_expired"
    s, _out = _shadow()
    (first,) = s.handle_frame(_frame([RAW], seq=1), arrival_utc="x", arrival_mono=1.0)
    (again,) = s.handle_frame(_frame([RAW], seq=2), arrival_utc="x", arrival_mono=2.0)
    assert first["signal"] is True and again["signal"] is False and again["reason"] == "duplicate"


def test_a_sell_shape_and_an_unwatched_wallet_are_not_signals():
    _to, data = shadow.tx_to_input(RAW)
    payer, permit, calls, refund, nft, meta, sig = decode(PERMIT2_TYPES, data[4:])
    (payment, paid), = permit[0]
    approve, swap, cleanup = calls
    tokens, recipients, minima, cmeta = decode(
        ["address[]", "address[]", "uint256[]", "bytes"], cleanup[3][4:])
    sell_cleanup = (cleanup[0], cleanup[1], cleanup[2], cleanup[3][:4] + encode(
        ["address[]", "address[]", "uint256[]", "bytes"], [[USDG], recipients, minima, cmeta]))
    sell_permit = ([(MINT, paid)], permit[1], permit[2])
    sell = data[:4] + encode(PERMIT2_TYPES, [payer, sell_permit, [approve, swap, sell_cleanup],
                                             refund, nft, meta, sig])
    assert shadow.decode_relay_fill(sell, WATCHED, QUOTES)["side"] == "other"
    assert shadow.decode_relay_fill(data, {"0x" + "11" * 20: ("u", "h")}, QUOTES) is None


def test_non_relay_traffic_is_skipped_without_decoding(monkeypatch):
    _to, data = shadow.tx_to_input(RAW)
    other, _addr = _resign("0x" + data.hex(), to="other")
    calls = {"n": 0}
    real = shadow.decode_relay_fill
    monkeypatch.setattr(shadow, "decode_relay_fill",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or real(*a, **k))
    s, out = _shadow()
    assert s.handle_frame(_frame([other]), arrival_utc="x", arrival_mono=1.0) == []
    assert calls["n"] == 0 and out == [] and s.counters["txs"] == 1


def _tape(tmp_path, rows):
    tape = sqlite3.connect(tmp_path / "tape.db")
    tape.executescript(
        "CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);"
        "CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, kind TEXT,"
        " block_no INTEGER, tx_sig TEXT, log_index INTEGER, observed_at TEXT, tick INTEGER,"
        " liquidity TEXT, sqrt_price_x96 TEXT);")
    tape.execute("INSERT INTO onchain_pool_protocols VALUES ('4663','0xpool','v3',?)", (MINT,))
    for block, tx, at, sqrt in rows:
        tape.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,"
                     "observed_at,tick,liquidity,sqrt_price_x96) VALUES "
                     "('4663','0xpool','v3_swap',?,?,1,?,7,'55',?)", (block, tx, at, sqrt))
    tape.commit()
    return str(tmp_path / "tape.db")


def _stored_pre(tmp_path, tape_path, seq):
    s, _out = _shadow()
    (rec,) = s.handle_frame(_frame([RAW], seq=seq), arrival_utc="2026-10-01T19:00:00+00:00",
                            arrival_mono=1.0)
    db = tmp_path / "shadow.db"
    shadow.write_records([rec], db_path=str(db), tape_path=tape_path)
    return sqlite3.connect(db).execute(
        "SELECT signal, pre_pool, pre_block, pre_sqrt_price_x96, pre_liquidity "
        "FROM rh_feed_signals").fetchone()


def test_the_writer_stores_the_last_swap_in_a_block_before_the_source(tmp_path):
    tape = _tape(tmp_path, [(100, "0xt0", "2026-10-01T18:59:58+00:00", "1000"),
                            (101, "0xt1", "2026-10-01T18:59:59+00:00", "1001"),
                            (102, "0xt2", "2026-10-01T18:59:59.5+00:00", "1002")])
    # The source is in block 102: a row of that block is not "pre", even observed before arrival.
    assert _stored_pre(tmp_path, tape, seq=102) == (1, "0xpool", 101, "1001", "55")


def test_the_source_swap_itself_is_never_its_own_pre_state(tmp_path):
    # Gamma MEDIUM (live row id=1): the tape held the source's own swap 7.5 ms before the feed.
    _to, data = shadow.tx_to_input(RAW)
    from eth_utils import keccak
    source_tx = "0x" + keccak(RAW).hex()
    tape = _tape(tmp_path, [(150, "0xold", "2026-10-01T18:59:50+00:00", "900"),
                            (151, source_tx, "2026-10-01T18:59:59.99+00:00", "999")])
    assert _stored_pre(tmp_path, tape, seq=151) == (1, "0xpool", 150, "900", "55")


def test_the_shadow_module_has_no_signing_or_sending_path():
    src = Path(shadow.__file__).read_text()
    for forbidden in ("sign_transaction", "send_raw", "sendRawTransaction", "evm_fast_submit",
                      "submission_backends", "reserve_entry", "reserve_pending"):
        assert forbidden not in src, forbidden


@pytest.mark.parametrize("raw,want", [("", ""), ("shadow", "shadow"), (" Shadow ", "shadow"),
                                      ("live", "live"), ("1", "")])
def test_the_mode_knob_values(monkeypatch, raw, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", raw)
    assert cfg.memecoin_rh_fast_lane_mode() == want


def test_the_funded_runner_never_hosts_the_shadow():
    # Gamma MEDIUM: the shadow is its own process; inside the runner it would share the GIL with
    # the Solana slot lane's 6 ms decide. M2's per-signal lane (memecoin_rh_fast_lane) is the one
    # fast-lane module the runner hosts, by design (spec §2.9); the per-block detector never is.
    src = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "memecoin_rh_feed_shadow" not in src and "memecoin_rh_fast_decide" not in src


def test_the_shadow_process_exits_unless_the_knob_says_shadow(monkeypatch):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_rh_feed_shadow_loop.py"
    spec = importlib.util.spec_from_file_location("rh_shadow_loop", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "off")
    started = []
    monkeypatch.setattr(shadow, "start_shadow", lambda **k: started.append(k))

    def no_loop(_s):
        raise RuntimeError("the process would have run")
    assert mod.main(sleep_fn=no_loop) == 0 and started == []


def test_the_production_quote_set_holds_the_real_fill_payment_and_weth():
    # Gamma LOW 2: a quote set without the stables would read every USDG fill as side "other".
    quotes = shadow.quote_assets_4663()
    assert USDG in quotes and WETH in quotes
    fill = shadow.decode_relay_fill(shadow.tx_to_input(RAW)[1], WATCHED, quotes)
    assert fill["payment_token"] in quotes and fill["side"] == "buy"


def test_a_failing_frame_is_counted_and_never_reaches_the_reconnect_path():
    s, _out = _shadow()

    def boom(*_a, **_k):
        raise RuntimeError("bad frame")
    s.handle_frame = boom
    stats = {"frames": 0, "cpu_ms": 0.0, "frame_ms_sum": 0.0, "frame_ms_max": 0.0}
    shadow.process_frame(s, stats, "{}", now_utc=lambda: "x")       # must not raise
    assert s.counters["frame_errors"] == 1 and stats["frames"] == 1


def test_the_post_pass_is_fed_the_corrected_clock():
    # Gamma LOW 1: written_at is corrected-clock; host time against it is the offset trap.
    src = Path(shadow.__file__).read_text()
    i = src.index("enrich_post(db_path=db_path")
    assert "now_epoch=real_utc_now()" in src[i:i + 120] and "time.time()" not in src


def test_a_fill_paid_by_an_unknown_payer_is_not_a_signal_even_from_a_known_solver():
    s, _out = _shadow(payers=("0x" + "22" * 20,))
    (r,) = s.handle_frame(_frame([RAW]), arrival_utc="x", arrival_mono=1.0)
    assert r["payer"] == PAYER and r["payer_known"] is False
    assert r["signal"] is False and r["reason"] == "unknown_payer"


def test_the_seed_names_the_relay_payer_and_the_fixture_solver():
    from sniper.services.memecoin_rh_relay_seed_4663 import PAYERS, SOLVERS
    assert PAYER in PAYERS and SOLVER in SOLVERS and len(SOLVERS) == 31


def test_the_post_pass_writes_the_source_swap_state_and_gives_up_later(tmp_path):
    from datetime import datetime
    tape = sqlite3.connect(tmp_path / "tape.db")
    tape.executescript(
        "CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);"
        "CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, kind TEXT,"
        " block_no INTEGER, tx_sig TEXT, log_index INTEGER, observed_at TEXT, tick INTEGER,"
        " liquidity TEXT, sqrt_price_x96 TEXT);")
    s, _out = _shadow()
    (rec,) = s.handle_frame(_frame([RAW]), arrival_utc="2026-10-01T19:00:00+00:00", arrival_mono=1.0)
    db = str(tmp_path / "shadow.db")
    shadow.write_records([rec], db_path=db, tape_path=str(tmp_path / "tape.db"))
    written = sqlite3.connect(db).execute("SELECT written_at FROM rh_feed_signals").fetchone()[0]
    t0 = datetime.fromisoformat(written).timestamp()
    assert shadow.enrich_post(db_path=db, tape_path=str(tmp_path / "tape.db"), now_epoch=t0) == 0
    tape.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,observed_at,"
                 "tick,liquidity,sqrt_price_x96) VALUES ('4663','0xpool','v3_swap',200,?,3,'x',9,"
                 "'77','4242')", (rec["tx_hash"],))
    tape.commit()
    assert shadow.enrich_post(db_path=db, tape_path=str(tmp_path / "tape.db"), now_epoch=t0 + 5) == 1
    row = sqlite3.connect(db).execute(
        "SELECT post_checked, post_pool, post_block, post_sqrt_price_x96 FROM rh_feed_signals").fetchone()
    assert row == (1, "0xpool", 200, "4242")
    shadow.write_records([dict(rec, tx_hash="0xmissing")], db_path=db,
                         tape_path=str(tmp_path / "tape.db"))
    shadow.enrich_post(db_path=db, tape_path=str(tmp_path / "tape.db"), now_epoch=t0 + 500)
    assert sqlite3.connect(db).execute(
        "SELECT post_checked FROM rh_feed_signals WHERE tx_hash='0xmissing'").fetchone() == (2,)


def test_without_a_block_number_the_source_tx_is_still_excluded(tmp_path):
    # A frame with no sequenceNumber falls back to arrival time; the source's own row, observed
    # before arrival, must still not be the pre-state.
    from eth_utils import keccak
    source_tx = "0x" + keccak(RAW).hex()
    tape = _tape(tmp_path, [(150, "0xold", "2026-10-01T18:59:50+00:00", "900"),
                            (151, source_tx, "2026-10-01T18:59:59.99+00:00", "999")])
    assert _stored_pre(tmp_path, tape, seq=None) == (1, "0xpool", 150, "900", "55")
