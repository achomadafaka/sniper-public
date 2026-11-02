# tests/sim/test_compact_writer.py
import os
import tempfile
import pyarrow.parquet as pq

from sniper.sim.compact.writer import ParquetTableWriter


def test_write_and_read_back_trades():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        rows = [
            {"market_id": "pm:0x1", "asset_id": "a1", "capture_ts": "2026-04-03T12:00:00.000Z",
             "exchange_ts": "2026-04-03T12:00:00.000Z", "ingest_seq": 1,
             "price": 0.65, "size": 100.0, "side": "BUY",
             "fee_rate_bps": 0, "transaction_hash": "0xabc", "market_type": "geo", "venue": "polymarket"},
            {"market_id": "pm:0x1", "asset_id": "a1", "capture_ts": "2026-04-03T12:00:01.000Z",
             "exchange_ts": None, "ingest_seq": 2,
             "price": 0.70, "size": 50.0, "side": "SELL",
             "fee_rate_bps": None, "transaction_hash": None, "market_type": "geo", "venue": "polymarket"},
        ]
        w.append("trades", "2026-04-03", rows)
        w.flush()

        path = os.path.join(tmpdir, "trades", "2026-04-03.parquet")
        assert os.path.exists(path)
        table = pq.read_table(path)
        assert len(table) == 2
        assert table.column("price").to_pylist() == [0.65, 0.70]
        assert table.column("transaction_hash").to_pylist() == ["0xabc", None]


def test_append_is_additive():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        row1 = [{"market_id": "pm:0x1", "capture_ts": "t1", "ingest_seq": 1, "best_bid": 0.5,
                 "best_ask": 0.6, "bid_size": None, "ask_size": None,
                 "source_event": "best_bba", "venue": "polymarket",
                 "asset_id": "a1", "exchange_ts": None}]
        row2 = [{"market_id": "pm:0x1", "capture_ts": "t2", "ingest_seq": 2, "best_bid": 0.51,
                 "best_ask": 0.61, "bid_size": None, "ask_size": None,
                 "source_event": "best_bba", "venue": "polymarket",
                 "asset_id": "a1", "exchange_ts": None}]
        w.append("quotes_top", "2026-04-03", row1)
        w.append("quotes_top", "2026-04-03", row2)
        w.flush()

        path = os.path.join(tmpdir, "quotes_top", "2026-04-03.parquet")
        table = pq.read_table(path)
        assert len(table) == 2


def test_date_partitioning():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        w.append("trades", "2026-04-03", [{"market_id": "a", "ingest_seq": 1, "price": 0.5,
                 "size": 10, "side": "BUY", "venue": "pm", "market_type": "x",
                 "asset_id": "a", "capture_ts": "t", "exchange_ts": None,
                 "fee_rate_bps": None, "transaction_hash": None}])
        w.append("trades", "2026-04-04", [{"market_id": "b", "ingest_seq": 2, "price": 0.6,
                 "size": 20, "side": "SELL", "venue": "pm", "market_type": "x",
                 "asset_id": "b", "capture_ts": "t", "exchange_ts": None,
                 "fee_rate_bps": None, "transaction_hash": None}])
        w.flush()

        assert os.path.exists(os.path.join(tmpdir, "trades", "2026-04-03.parquet"))
        assert os.path.exists(os.path.join(tmpdir, "trades", "2026-04-04.parquet"))
