"""Desk producer U worker: frames in, day files and emissions out (no network, no ledger)."""
import importlib.util
import json
import struct
import base64
from pathlib import Path

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_universe as U

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_universe_worker", ROOT / "scripts/memecoin_desk_universe_worker.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
MINT, USER, CREATOR, FEE = (bytes([i]) * 32 for i in (1, 2, 3, 4))
SOL = 150.0


def trade_event(vq, vt, *, is_buy=True, quote=1_000_000_000, token=10**9):
    return (TRADE + MINT + struct.pack("<QQ?", quote, token, is_buy) + USER + struct.pack("<q", 1)
            + struct.pack("<QQQQ", vq, vt, 10**9, 10**12) + FEE + struct.pack("<QQ", 100, 1)
            + CREATOR + struct.pack("<QQ", 30, 1))


def frame_tx(slot, sig_byte, payload, index=0):
    u = pb.SubscribeUpdate()
    t = u.transaction
    t.slot = slot
    t.transaction.index = index
    t.transaction.signature = bytes([sig_byte]) * 64
    t.transaction.transaction.message.account_keys.extend([b"\x09" * 32, U.b58decode(U.PUMP_PROGRAM)])
    g = t.transaction.meta.inner_instructions.add()
    g.index = 0
    ix = g.instructions.add()
    ix.program_id_index = 1
    ix.data = U.CPI_EVENT_PREFIX + payload
    return u.SerializeToString()


def frame_meta(slot, block_time):
    u = pb.SubscribeUpdate()
    u.block_meta.slot = slot
    u.block_meta.block_time.timestamp = block_time
    return u.SerializeToString()


def vq_for(mcap, vt=500_000_000_000_000):
    return int(mcap / 1e9 / SOL * (vt / 1e6) * 1e9), vt


def worker(tmp_path, **over):
    cfg = W.knobs({})
    cfg.update({"cooldown_s": "0", **over})
    alerts = []
    w = W.Worker(cfg, base=tmp_path, sol_price_fn=lambda: SOL, alert_fn=lambda k, t: alerts.append(k))
    w.sol_usd, w.sol_at = SOL, __import__("time").time()
    return w, alerts


def test_band_crossing_frames_write_swaps_and_one_emission(tmp_path):
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    w.files.commit()
    days = list((tmp_path / "candidate_emissions").glob("*.jsonl"))
    rows = [json.loads(l) for l in days[0].read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["producer"] == "U" and r["gate_state"] is None and r["schema"] == "desk-candidate/v1"
    assert r["ticket_id"].startswith("desk:U:") and r["band_entry_slot"] == 11
    assert r["pool"] == U.bonding_curve(r["mint"]) and r["pool"] != r["mint"]
    f = r["snapshot"]["fields"]["mcap_usd"]
    assert abs(f["value"] - 40_000) < 1 and f["completeness"] == "complete"
    assert r["snapshot"]["fields"]["flow_60s"]["completeness"] == "partial"   # worker < 60 s old
    swaps = list((tmp_path / "swaps").glob("*.db"))
    import sqlite3
    assert sqlite3.connect(swaps[0]).execute("select count(*) from swaps").fetchone()[0] == 2


def test_ring_overflow_drops_oldest_counts_and_marks_unknown(tmp_path):
    w, _ = worker(tmp_path, ring_mb=str(100 / 1024 / 1024))       # 100-byte ring
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    assert w.book.state(U.b58encode(MINT)) == "outside"
    for i in range(5):
        w.push(b"x" * 60)
    assert w.stats["ring_dropped"] >= 1 and w.ring_bytes <= 100
    assert w.book.state(U.b58encode(MINT)) == "unknown"
    w.files.commit()
    import sqlite3
    gaps = sqlite3.connect(next((tmp_path / "gaps").glob("*.db"))).execute("select cause from gaps").fetchall()
    assert ("ring_overflow",) in gaps


def test_hard_cap_stops_and_pages_soft_cap_evicts(tmp_path):
    w, alerts = worker(tmp_path, hard_gb="0", min_free_gb="0")
    (tmp_path / "x.bin").write_bytes(b"0" * 10)
    w.check_disk()
    assert w.stop and alerts == ["desk-universe-disk"]
    w2, alerts2 = worker(tmp_path / "b", soft_gb="0", hard_gb="1000", min_free_gb="0")
    w2.bars.watch("p1")
    w2.watch_order.append("p1")
    (tmp_path / "b").mkdir(exist_ok=True)
    (tmp_path / "b" / "y.bin").write_bytes(b"0" * 10)
    w2.check_disk()
    assert not w2.stop and alerts2 == [] and w2.bars.watched() == []


def test_disabled_knob_exits_without_connecting(monkeypatch, capsys):
    monkeypatch.setenv("MEMECOIN_DESK_UNIVERSE_ENABLED", "0")
    assert W.main() == 0
    assert "not 1" in capsys.readouterr().out


def test_emitted_at_round_trips_to_snapshot_emission_at_exactly(tmp_path, monkeypatch):
    """Step-3 validate_candidate rejects any drift between the ISO stamp and emission_at."""
    from datetime import datetime
    monkeypatch.setattr(W.time, "time", lambda: 1_800_000_000.1234567)   # sub-microsecond part
    w, _ = worker(tmp_path)
    w.sol_at = 1_800_000_000.0
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    row = json.loads(next((tmp_path / "candidate_emissions").glob("*.jsonl")).read_text().splitlines()[0])
    assert datetime.fromisoformat(row["emitted_at"]).timestamp() == row["snapshot"]["emission_at"]
    for name, f in row["snapshot"]["fields"].items():
        v = f["value"]
        if isinstance(v, (int, float)) and not name.startswith("price_vs_"):
            assert v >= 0, name


def test_unknown_events_are_classified_from_the_idl():
    assert W.classify_unknown_event(bytes.fromhex("742b4dbd117a482b") + b"x" * 145) == (
        "irrelevant", "SweepBondingCurveFeeEvent")
    mint = bytes([7]) * 32
    raw = bytes.fromhex(W.POST_COMPLETE_BUY) + bytes([1]) * 32 + mint + b"\0" * 160
    assert W.classify_unknown_event(raw) == ("post_complete_buy", U.b58encode(mint))
    assert W.classify_unknown_event(bytes.fromhex("0011223344556677")) == ("unknown", "0011223344556677")


def test_quote_control_config_is_exact_non_sol_layout_only():
    fixture = json.loads((ROOT / "tests/fixtures/desk_capture_v1/quote_control.json").read_text())
    for encoded in fixture["payloads_b64"]:
        raw = base64.b64decode(encoded)
        assert len(raw) == 128
        assert W.classify_unknown_event(raw) == ("irrelevant", "SetQuoteControlMintReservesEvent")
        for bad in (raw[:-1], raw + b"x", raw[:72] + bytes(32) + raw[104:],
                    raw[:72] + U.b58decode("So11111111111111111111111111111111111111112") + raw[104:]):
            assert W.classify_unknown_event(bad)[0] == "unknown"


def test_v1_empty_wrapper_preserves_both_real_trades_once_without_gap(tmp_path):
    fixture = json.loads((ROOT / "tests/fixtures/desk_capture_v1/empty_wrapper.json").read_text())
    update = pb.SubscribeUpdate()
    update.transaction.slot = fixture["slot"]
    info = update.transaction.transaction
    info.signature = U.b58decode(fixture["transaction"]["signatures"][0])
    keys = fixture["transaction"]["message"]["accountKeys"]
    info.transaction.message.account_keys.extend(U.b58decode(k) for k in keys)
    for side in ("writable", "readonly"):
        getattr(info.meta, "loaded_" + side + "_addresses").extend(
            U.b58decode(k) for k in fixture["meta"]["loadedAddresses"][side])
    info.meta.log_messages.extend(fixture["meta"]["logMessages"])
    for source in fixture["meta"]["innerInstructions"]:
        group = info.meta.inner_instructions.add()
        group.index = source["index"]
        for source_ix in source["instructions"]:
            ix = group.instructions.add()
            ix.program_id_index = source_ix["programIdIndex"]
            ix.data = U.b58decode(source_ix["data"])
    w, _ = worker(tmp_path)
    w.process(update.SerializeToString())
    w.files.commit()
    assert w.stats["known_irrelevant:empty_event_wrapper"] == 1
    assert w.stats["decode_blocked:DECODER_FAILURE"] == 0
    import sqlite3
    rows = sqlite3.connect(next((tmp_path / "swaps").glob("*.db"))).execute(
        "select signature, ordinal from swaps order by ordinal").fetchall()
    assert rows == [(fixture["transaction"]["signatures"][0], 196613),
                    (fixture["transaction"]["signatures"][0], 196616)]


def test_empty_alone_and_unknown_or_truncated_trade_still_gap(tmp_path):
    for index, payloads in enumerate(((b"",), (trade_event(*vq_for(40_000)), b"", TRADE),
                                    (trade_event(*vq_for(40_000)), b"", b"unknown!"))):
        w, _ = worker(tmp_path / str(index))
        update = pb.SubscribeUpdate()
        update.ParseFromString(frame_tx(10, 1, payloads[0]))
        group = update.transaction.transaction.meta.inner_instructions[0]
        for payload in payloads[1:]:
            ix = group.instructions.add()
            ix.program_id_index = 1
            ix.data = U.CPI_EVENT_PREFIX + payload
        w.process(update.SerializeToString())
        assert sum(v for k, v in w.stats.items() if k.startswith("decode_blocked:")) >= 1


def test_foreign_program_trade_cannot_validate_empty_pump_wrapper(tmp_path):
    w, _ = worker(tmp_path)
    update = pb.SubscribeUpdate()
    update.ParseFromString(frame_tx(10, 1, b""))
    ix = update.transaction.transaction.meta.inner_instructions[0].instructions.add()
    ix.program_id_index = 0  # foreign program, even with a valid-looking TradeEvent
    ix.data = U.CPI_EVENT_PREFIX + trade_event(*vq_for(40_000))
    w.process(update.SerializeToString())
    assert w.stats["known_irrelevant:empty_event_wrapper"] == 0
    assert sum(v for k, v in w.stats.items() if k.startswith("decode_blocked:")) == 1


def test_non_sol_quote_config_frames_do_not_invalidate_sol_tape(tmp_path):
    fixture = json.loads((ROOT / "tests/fixtures/desk_capture_v1/quote_control.json").read_text())
    w, _ = worker(tmp_path)
    for index, encoded in enumerate(fixture["payloads_b64"]):
        w.process(frame_tx(fixture["slot"], index + 1, base64.b64decode(encoded)))
    assert w.stats["known_irrelevant:SetQuoteControlMintReservesEvent"] == 12
    assert sum(v for k, v in w.stats.items() if k.startswith("decode_blocked:")) == 0


CREATE = bytes([27, 114, 169, 77, 222, 235, 99, 118])


def create_event():
    def string(t):
        b = t.encode()
        return struct.pack("<I", len(b)) + b
    return (CREATE + string("Name") + string("SYM") + string("uri") + MINT + bytes([5]) * 32 + USER + CREATOR
            + struct.pack("<q", 1) + struct.pack("<QQQQ", 10**15, 30 * 10**9, 793 * 10**12, 10**15) + bytes([6]) * 32
            + bytes([0, 0]) + bytes([7]) * 32 + struct.pack("<Q", 30 * 10**9))


def test_age_uses_the_create_slot_time_even_when_block_meta_arrives_after_the_create(tmp_path):
    w, _ = worker(tmp_path)
    w.process(frame_tx(9, 9, create_event()))            # create first, its block meta later
    w.process(frame_meta(9, int(w.sol_at) - 30))
    w.process(frame_meta(10, int(w.sol_at)))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    row = json.loads(next((tmp_path / "candidate_emissions").glob("*.jsonl")).read_text().splitlines()[0])
    age = row["snapshot"]["fields"]["age_s"]
    assert age["completeness"] == "complete" and age["slot"] == 9 and 25 <= age["value"] <= 60


def test_an_old_swaps_day_file_gains_the_probe_columns_in_place(tmp_path):
    import sqlite3
    old = tmp_path / "swaps" / "2026-10-21.db"
    old.parent.mkdir()
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
              "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
              "block_time INTEGER, received_at REAL, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")
    c.execute("INSERT INTO swaps VALUES(1,'s',0,'m','c',1,1,1,'a',1,1,1,1.0)")
    c.commit()
    c.close()
    files = W.DayFiles(tmp_path)
    conn = files.conn("swaps", 1792540800.0 + 3600)              # 2026-10-21 UTC
    cols = [r[1] for r in conn.execute("PRAGMA table_info(swaps)")]
    assert cols[-11:] == ["real_token", "fee_bps", "creator_fee_bps", "tx_index", "real_quote",
                          "quote_mint", "q_amount", "q_virtual", "q_real", "cashback_fee_bps", "cashback"]
    assert conn.execute("SELECT fee_bps FROM swaps").fetchone() == (None,)  # old rows: unknown, never 0
    names = {r[1] for r in conn.execute("PRAGMA index_list(swaps)")}
    assert {"swaps_mint_slot", "swaps_slot"} <= names


def test_block_meta_after_its_transactions_still_stamps_block_time(tmp_path):
    # Gamma 2026-10-21: live block_meta arrives after the slot's txs; 550,537 swaps and 6,728
    # launches had block_time NULL because the time was looked up at write time only
    import sqlite3
    w, _ = worker(tmp_path)
    w.process(frame_tx(20, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(21, 2, trade_event(*vq_for(21_000))))
    w.process(frame_meta(20, 1_700_000_020))
    w.files.commit()
    db = sqlite3.connect(next((tmp_path / "swaps").glob("*.db")))
    assert db.execute("select slot, block_time from swaps order by slot").fetchall() == \
        [(20, 1_700_000_020), (21, None)]
    w.process(frame_meta(21, 1_700_000_021))
    w.files.commit()
    assert db.execute("select block_time from swaps where slot=21").fetchone() == (1_700_000_021,)
    blocks = sqlite3.connect(next((tmp_path / "blocks").glob("*.db")))
    assert blocks.execute("select slot, block_time from blocks order by slot").fetchall() == \
        [(20, 1_700_000_020), (21, 1_700_000_021)]


def test_a_launch_row_keeps_the_create_event_symbol_and_name(tmp_path):
    # the source scout resolves $TICKER against U launches (scout design 85a5ba94f §3)
    import sqlite3
    w, _ = worker(tmp_path)
    w.process(frame_tx(9, 9, create_event()))
    w.files.commit()
    db = sqlite3.connect(next((tmp_path / "launches").glob("*.db")))
    assert db.execute("select symbol, name from launches").fetchall() == [("SYM", "Name")]


def test_a_launch_row_keeps_the_signing_dev_beside_the_curve_creator_and_old_rows_stay_null(tmp_path):
    # Alpha 2026-10-28: launches.creator is the curve creator (fee recipient); the signing dev is a separate field with its own source, never backfilled
    import sqlite3
    old = tmp_path / "launches"
    old.mkdir()
    day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    pre = sqlite3.connect(old / (day + ".db"))
    pre.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, slot INTEGER, signature TEXT, block_time INTEGER, received_at REAL, symbol TEXT, name TEXT)")
    pre.execute("INSERT INTO launches VALUES('OLD','c','cr',1,'s',1,1.0,'S','N')")
    pre.commit()
    pre.close()
    w, _ = worker(tmp_path)
    w.process(frame_tx(9, 9, create_event()))
    w.files.commit()
    db = sqlite3.connect(next((tmp_path / "launches").glob("*.db")))
    assert db.execute("select mint, dev, dev_source from launches where mint='OLD'").fetchone() == ("OLD", None, None)
    row = db.execute("select creator, dev, dev_source from launches where mint != 'OLD'").fetchone()
    assert row == (U.b58encode(CREATOR), U.b58encode(USER), "create_event_user") and row[0] != row[1]


def test_emission_stamps_the_crawl_cache_and_the_sheet_reads_it(tmp_path):
    # CRAWLER design (a): the newest fresh crawl known at emission enters the frozen snapshot
    import time as _t
    from sniper.services import memecoin_desk_contract as K
    from sniper.services import memecoin_desk_crawler as C
    w, _ = worker(tmp_path)
    mint = U.b58encode(MINT)
    (tmp_path / "crawler").mkdir()
    db = C.connect(tmp_path / "crawler" / "crawls.db")
    flags = {"copycat_ticker": True, "bought_hype_pattern": False, "suspicious_text": False, "rug_history_claim": None}
    db.execute("INSERT INTO crawls (network_id, mint, known_at, input_hash, inputs_json, intent, flags_json, "
               "prompt_version) VALUES ('solana',?,?,'h','[]','warning',?,?)",
               (mint, _t.time() - 30, json.dumps(flags), C.CRAWLER_VERSION))
    db.commit()
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    row = json.loads(next((tmp_path / "candidate_emissions").glob("*.jsonl")).read_text().splitlines()[0])
    f = row["snapshot"]["fields"]
    assert f["crawler_intent"]["value"] == "warning" and f["crawler_intent"]["source"].startswith("crawler:1:")
    sheet = K.render_judge_sheet({**row, "opportunity_id": row["ticket_id"]})
    assert sheet["crawler_intent"] == "warning" and sheet["crawler_flags"] == flags


def test_emission_without_a_crawl_marks_the_fields_absent(tmp_path):
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    row = json.loads(next((tmp_path / "candidate_emissions").glob("*.jsonl")).read_text().splitlines()[0])
    assert row["snapshot"]["fields"]["crawler_flags"]["completeness"] == "absent"


def test_swaps_carry_the_tx_position_and_a_shared_index_is_counted(tmp_path):
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000)), index=7))
    w.process(frame_tx(10, 2, trade_event(*vq_for(21_000)), index=3))     # same slot, an earlier tx position
    w.files.commit()
    swaps = w.files.conn("swaps", __import__("time").time())
    rows = swaps.execute("SELECT slot, tx_index FROM swaps").fetchall()
    assert sorted(rows) == [(10, 3), (10, 7)] and w.stats["tx_index_collision"] == 0
    w.process(frame_tx(10, 3, trade_event(*vq_for(22_000)), index=3))     # a second tx at index 3: collision
    assert w.stats["tx_index_collision"] == 1


def test_an_existing_day_file_gains_tx_index_in_place_with_null_for_old_rows(tmp_path):
    import sqlite3
    day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    (tmp_path / "swaps").mkdir()
    old = sqlite3.connect(tmp_path / "swaps" / f"{day}.db")
    old.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
                "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
                "block_time INTEGER, received_at REAL, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")
    old.execute("INSERT INTO swaps VALUES(5,'s',0,'m','c',1,1,1,'a',1,1,1,1.0)")
    old.commit()
    old.close()
    w, _ = worker(tmp_path)
    c = w.files.conn("swaps", __import__("time").time())
    assert c.execute("SELECT slot, tx_index FROM swaps").fetchall() == [(5, None)]
def _filtered(names, slot=5):
    u = pb.SubscribeUpdate()
    u.filters.extend(names)
    u.transaction.slot = slot
    return u.SerializeToString()


def test_frame_filters_reads_the_leading_names_without_decoding():
    assert W.frame_filters(_filtered(["pumpswap"])) == {b"pumpswap"}
    assert W.frame_filters(_filtered(["pump", "pumpswap"])) == {b"pump", b"pumpswap"}
    assert W.frame_filters(frame_meta(1, 2)) == set()          # no filters on a hand-built meta frame
    assert W.frame_filters(b"") == set() and W.frame_filters(b"\x0a\x09ab") == set()   # truncated: never counted


def test_census_counts_pumpswap_only_frames_and_leaves_the_curve_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", "1")
    w, _ = worker(tmp_path)
    assert w.census is True
    only, both, pump = _filtered(["pumpswap"]), _filtered(["pump", "pumpswap"]), _filtered(["pump"])
    assert [w.census_frame(f) for f in (only, only, both, pump)] == [True, True, False, False]
    assert w.stats["ps_tx"] == 2 and w.stats["ps_bytes"] == 2 * len(only)
    assert w.stats["cs_pump_frames"] == 2 and w.stats["cs_pump_bytes"] == len(both) + len(pump)
    assert not w.ring and "ps_hist" in json.loads(__import__("json").dumps({"ps_hist": dict(w.ps_hist)}))


def test_census_is_off_by_default_and_the_request_has_no_second_filter(monkeypatch):
    monkeypatch.delenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", raising=False)
    assert W.build_request(pb, False).transactions.keys() == {"pump"}
    on = W.build_request(pb, True)
    assert set(on.transactions) == {"pump", "pumpswap"}
    assert list(on.transactions["pumpswap"].account_include) == [W.PUMPSWAP_PROGRAM]
    assert list(on.transactions["pump"].account_include) == [U.PUMP_PROGRAM]


def test_census_off_leaves_worker_flag_false(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", raising=False)
    w, _ = worker(tmp_path)
    assert w.census is False


def test_zero_or_absent_proto_index_is_unknown_not_first_in_block(tmp_path):
    w, _ = worker(tmp_path)
    for n in (1, 2):
        w.process(frame_tx(10, n, trade_event(*vq_for(20_000)), index=0))
    c = w.files.conn("swaps", __import__("time").time())
    assert c.execute("SELECT tx_index FROM swaps").fetchall() == [(None,), (None,)]
    assert w.stats["tx_index_unknown"] == 2
    assert w.stats["tx_index_collision"] == 0


def test_census_filter_scan_accepts_legal_field_order_and_long_names(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", "1")
    w, _ = worker(tmp_path)
    # protobuf field ordering is arbitrary; field1 can follow the transaction body.
    tx = _filtered([], slot=42)
    raw = tx + b"\x0a\x08pumpswap"
    assert W.frame_filters(raw) == {b"pumpswap"}
    assert w.census_frame(raw) is True
    long_name = b"x" * 130
    raw2 = tx + b"\x0a\x82\x01" + long_name + b"\x0a\x08pumpswap"
    assert W.frame_filters(raw2) == {long_name, b"pumpswap"}
    assert w.census_frame(raw2) is True


def test_census_control_frames_are_not_curve_transaction_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CENSUS", "1")
    w, _ = worker(tmp_path)
    assert w.census_frame(frame_meta(1, 2)) is False
    assert w.stats["cs_pump_frames"] == 0


def test_swaps_store_the_events_real_quote_reserves_and_old_rows_stay_null(tmp_path):
    import sqlite3
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.files.commit()
    conn = w.files.conn("swaps", __import__("time").time())
    assert conn.execute("SELECT real_quote, virtual_quote FROM swaps").fetchone()[0] == 10 ** 9       # the event's real_sol_reserves
    (tmp_path / "old" / "swaps").mkdir(parents=True)
    old = tmp_path / "old"
    c = sqlite3.connect(old / "swaps" / "2026-10-21.db")
    c.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
              "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
              "block_time INTEGER, received_at REAL, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")
    c.execute("INSERT INTO swaps VALUES(1,'s',0,'m','c',1,1,1,'a',1,1,1,1.0)")
    c.commit()
    c.close()
    files = W.DayFiles(old)
    conn = files.conn("swaps", 1792540800.0 + 3600)
    assert conn.execute("SELECT real_quote FROM swaps").fetchone() == (None,)                          # unknown, never 0


def _full_event(vq_legacy, *, quote_mint, q_amount=0, q_virtual=0, q_real=0, cashback_bps=0, cashback=0):
    """A complete TradeEvent (every field the on-chain IDL has) via the rabbit encoder."""
    import base64
    from sniper.services.memecoin_rabbit_signal import encode_trade_event
    b58 = lambda b: U.b58encode(b)    # noqa: E731
    e = {"mint": b58(MINT), "sol_amount": 0 if not vq_legacy else 10**9, "token_amount": 10**9, "is_buy": True,
         "user": b58(USER), "timestamp": 1, "virtual_sol_reserves": vq_legacy, "virtual_token_reserves": 10**15,
         "real_sol_reserves": 0, "real_token_reserves": 10**12, "fee_recipient": b58(FEE), "fee_basis_points": 95,
         "fee": 1, "creator": b58(CREATOR), "creator_fee_basis_points": 30, "creator_fee": 1, "track_volume": False,
         "total_unclaimed_tokens": 0, "total_claimed_tokens": 0, "current_sol_volume": 0,
         "last_update_timestamp": 1, "ix_name": "buy", "mayhem_mode": False, "cashback_fee_basis_points": cashback_bps,
         "cashback": cashback, "buyback_fee_basis_points": 0, "buyback_fee": 0, "shareholders": [],
         "quote_mint": quote_mint, "quote_amount": q_amount, "virtual_quote_reserves": q_virtual,
         "real_quote_reserves": q_real, "holder_rewards_bps": 0, "holder_rewards": 0}
    return base64.b64decode(encode_trade_event(e).split("Program data: ", 1)[1])


def test_every_swap_row_names_its_quote_mint_and_only_sol_curves_reach_the_book(tmp_path, monkeypatch):
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    w, _ = worker(tmp_path)
    offered = []
    monkeypatch.setattr(w.book, "on_trade", lambda **kw: offered.append(kw["mint"]))
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))                                   # SOL curve
    w.process(frame_tx(10, 2, _full_event(0, quote_mint=usdc, q_amount=5_000_000,
                                          q_virtual=40_000_000_000, q_real=7_000_000)))         # USDC curve
    w.process(frame_tx(10, 3, trade_event(0, 10**15)))                                        # zero, truncated
    w.files.commit()
    rows = w.files.conn("swaps", __import__("time").time()).execute(
        "SELECT signature, quote_mint, q_amount, q_virtual, q_real FROM swaps ORDER BY signature").fetchall()
    by_sig = {r[0][:2]: r[1:] for r in rows}
    sol_row, usdc_row, unknown_row = (next(v for k, v in sorted(by_sig.items()) if k == s) for s in
                                      sorted(by_sig))
    labels = sorted(r[1] for r in rows)
    assert labels == sorted([W.SOL_QUOTE, usdc, "unknown"])
    assert [r[1:] for r in rows if r[1] == usdc] == [(usdc, 5_000_000, 40_000_000_000, 7_000_000)]
    assert [r[2:] for r in rows if r[1] == W.SOL_QUOTE] == [(None, None, None)]
    assert len(offered) == 1                                       # only the SOL curve is priced
    assert w.stats["non_sol_quote_skipped"] == 2 and w.stats["quote_mint_unparsed"] == 1


def test_winner_reentry_arms_without_a_block_time(tmp_path):
    """10-23 live bug: blocks_meta lands after its transactions, so bt was None and re-entry never armed
    (MEND 74155 closed +0.36 at ~15.1k, ran to 19.2k, no claim). Unknown bt uses received - max lag."""
    import sqlite3, time
    from sniper.persistence import db_memecoin_desk as desk
    base = tmp_path / "desk"
    base.mkdir()
    mint = U.b58encode(MINT)
    closed_at = time.time() - 30
    iso = lambda t: __import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc).isoformat()
    c = desk.connect(tmp_path / "memecoin_desk.db")
    desk.register_cohort(c, cohort_id="cohort-U", producer="U", prompt_version="v", policy={"clip_usd": 25},
                         created_at=iso(closed_at - 120))
    c.execute("INSERT INTO desk_snapshots VALUES ('h', '{}')")
    c.execute("INSERT INTO desk_ticket_heads VALUES (?,?,?,?,?,?,?,?,?,?,?,'closed')",
              ("t1", "t1", "cohort-U", "U", "solana", mint, "pool", "h", iso(closed_at - 60), iso(closed_at - 60),
               iso(closed_at)))
    c.execute("INSERT INTO desk_tickets (ticket_id,opportunity_id,cohort_id,producer,network_id,mint,pool,stage,seat,"
              "verdict,reason,inputs_hash,inputs_json,output_json,ts,position_id) VALUES "
              "('t1','t1','cohort-U','U','solana',?, 'pool','close','engine','EXIT','position_closed','x','{}',?,?,42)",
              (mint, json.dumps({"state": "closed", "position_id": 42, "is_open": 0, "closed_at": iso(closed_at),
                                 "realized_pnl": 0.36, "exit_price_usd": 35_000 / 1e9}), iso(closed_at + 0.3)))
    c.commit()
    c.close()
    w, _ = worker(base)
    w.process(frame_meta(9, int(time.time()) - 2))                  # a known EARLIER slot after the close: the floor
    w.process(frame_tx(10, 1, trade_event(*vq_for(40_000))))       # own block time unknown
    w.process(frame_tx(11, 2, trade_event(*vq_for(45_000))))
    rows = [json.loads(l) for f in (base / "candidate_emissions").glob("*.jsonl") for l in f.read_text().splitlines()]
    assert [r.get("reentry", {}).get("prev_position_id") for r in rows] == [42]


def _armed_close(tmp_path, closed_at):
    import time
    from sniper.persistence import db_memecoin_desk as desk
    base = tmp_path / "desk"
    base.mkdir()
    mint = U.b58encode(MINT)
    iso = lambda t: __import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc).isoformat()
    c = desk.connect(tmp_path / "memecoin_desk.db")
    desk.register_cohort(c, cohort_id="cohort-U", producer="U", prompt_version="v", policy={"clip_usd": 25},
                         created_at=iso(closed_at - 120))
    c.execute("INSERT INTO desk_snapshots VALUES ('h', '{}')")
    c.execute("INSERT INTO desk_ticket_heads VALUES (?,?,?,?,?,?,?,?,?,?,?,'closed')",
              ("t1", "t1", "cohort-U", "U", "solana", mint, "pool", "h", iso(closed_at - 60), iso(closed_at - 60),
               iso(closed_at)))
    c.execute("INSERT INTO desk_tickets (ticket_id,opportunity_id,cohort_id,producer,network_id,mint,pool,stage,seat,"
              "verdict,reason,inputs_hash,inputs_json,output_json,ts,position_id) VALUES "
              "('t1','t1','cohort-U','U','solana',?, 'pool','close','engine','EXIT','position_closed','x','{}',?,?,42)",
              (mint, json.dumps({"state": "closed", "position_id": 42, "is_open": 0, "closed_at": iso(closed_at),
                                 "realized_pnl": 0.36, "exit_price_usd": 35_000 / 1e9}), iso(closed_at + 0.3)))
    c.commit()
    c.close()
    return worker(base)[0], base


def _claims(base):
    return [json.loads(l).get("reentry") for f in (base / "candidate_emissions").glob("*.jsonl")
            for l in f.read_text().splitlines() if "reentry" in json.loads(l)]


def test_a_late_pre_close_print_without_a_gap_never_rearms(tmp_path):
    """Codex 3d4ae9b77: a backlog print from before the close, received late, must not re-arm. Its floor is the known
    block time of an earlier slot, which is before the close: not provably after it."""
    import time
    closed_at = time.time() - 30
    w, base = _armed_close(tmp_path, closed_at)
    w.process(frame_meta(9, int(closed_at) - 20))                  # an earlier slot, well before the close
    w.process(frame_tx(10, 1, trade_event(*vq_for(40_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(45_000))))
    assert _claims(base) == []


def test_unknown_floor_decides_nothing_then_resumes_on_later_meta(tmp_path):
    import time
    closed_at = time.time() - 30
    w, base = _armed_close(tmp_path, closed_at)
    w.process(frame_meta(20, int(time.time()) - 1))                # highest known slot is ABOVE the trades: no floor
    w.process(frame_tx(10, 1, trade_event(*vq_for(40_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(45_000))))
    assert _claims(base) == []
    w.process(frame_tx(21, 3, trade_event(*vq_for(46_000))))       # slot 21 > 20: floor = slot 20's time, after close
    assert [c["prev_position_id"] for c in _claims(base)] == [42]



def test_the_cashback_leg_is_captured_and_unknown_stays_null(tmp_path):
    """Alpha 2026-10-23: the all-in cost needs the buyer-paid cashback leg; a truncated event is NULL, never 0."""
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    sol = "So11111111111111111111111111111111111111112"
    w.process(frame_tx(10, 1, _full_event(vq_for(20_000)[0], quote_mint=sol, cashback_bps=25, cashback=12345)))
    w.process(frame_tx(10, 2, trade_event(*vq_for(20_000))))                                   # truncated event
    w.files.commit()
    rows = w.files.conn("swaps", __import__("time").time()).execute(
        "SELECT signature, cashback_fee_bps, cashback FROM swaps ORDER BY signature").fetchall()
    assert [r[1:] for r in rows] == [(25, 12345), (None, None)]


def test_curve_capture_off_records_no_curve_row_no_emission_and_says_retired(tmp_path, monkeypatch):
    """Operator 2026-10-27 (brain 48d0986c): MEMECOIN_DESK_CURVE_CAPTURE=0 retires the curve tape. The same band
    crossing as above writes no swap/launch row and no emission, the curve cut never advances, and the heartbeat
    names the retirement (readers say 'retired', never 0)."""
    import sqlite3
    monkeypatch.setenv("MEMECOIN_DESK_CURVE_CAPTURE", "0")
    w, _ = worker(tmp_path)
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_tx(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_tx(11, 2, trade_event(*vq_for(40_000))))
    w.process(frame_tx(12, 3, b"\x00" * 8))                     # an undecodable curve event: no curve gap either
    w.files.commit()
    w.commit_files()
    assert not list((tmp_path / "candidate_emissions").glob("*.jsonl"))
    for stream in ("swaps", "launches"):
        for f in (tmp_path / stream).glob("*.db"):
            assert sqlite3.connect(f).execute("select count(*) from %s" % stream).fetchone()[0] == 0
    assert not list((tmp_path / "gaps").glob("*.db")) or all(
        sqlite3.connect(f).execute("select count(*) from gaps").fetchone()[0] == 0 for f in (tmp_path / "gaps").glob("*.db"))
    w.heartbeat(True)
    beat = json.loads((tmp_path / "universe.heartbeat").read_text())
    assert beat["curve_capture"] is False and beat["curve_retired_since"] is not None
    assert beat["curve_committed_through"] is None and w.stats["curve_retired_skipped"] >= 2


def test_curve_capture_defaults_off_and_one_turns_it_on(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_DESK_CURVE_CAPTURE", raising=False)
    w, _ = worker(tmp_path)
    assert w.curve_capture is False                        # the operator's order is the default
    monkeypatch.setenv("MEMECOIN_DESK_CURVE_CAPTURE", "1")
    w, _ = worker(tmp_path)
    w.heartbeat(True)
    beat = json.loads((tmp_path / "universe.heartbeat").read_text())
    assert beat["curve_capture"] is True and beat["curve_retired_since"] is None


class _Ev:
    def __init__(self, token_id, kind=None, blocker=None, lifecycle=None):
        from types import SimpleNamespace as N
        self.token_id, self.blocker = token_id, blocker
        self.event_kind = None if kind is None else N(value=kind)
        self.lifecycle = None if lifecycle is None else N(value=lifecycle)


def _retired(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_CURVE_CAPTURE", "0")
    w, _ = worker(tmp_path)
    seen = []
    w.book.on_graduated = seen.append
    return w, seen


def test_retired_mode_keeps_the_post_complete_buy_graduation(tmp_path, monkeypatch):
    """Alpha (blocking): every live graduation path still reaches book.on_graduated with the curve retired."""
    w, seen = _retired(tmp_path, monkeypatch)
    mint = bytes(range(1, 33))
    raw = bytes.fromhex(W.POST_COMPLETE_BUY) + bytes(32) + mint + bytes(16)
    line = "Program data: " + base64.b64encode(raw).decode()
    w._retired_curve_event(_Ev("ignored", blocker="unknown_disc"), 1, "s", 0, 0, None, 0.0, 0.0, line=line)
    assert seen == [U.b58encode(mint)]


def test_retired_mode_keeps_the_lifecycle_migrated_graduation(tmp_path, monkeypatch):
    w, seen = _retired(tmp_path, monkeypatch)
    w._retired_curve_event(_Ev("MINTx", kind="other", lifecycle="migrated"), 1, "s", 0, 0, None, 0.0, 0.0)
    w._retired_curve_event(_Ev("MINTy", kind="buy"), 1, "s", 0, 0, None, 0.0, 0.0)          # a trade: skipped
    assert seen == ["MINTx"] and w.stats["curve_retired_skipped"] == 1
