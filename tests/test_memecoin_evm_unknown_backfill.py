"""Backfill evm-unknown memecoin_events to their real chain — unambiguous mints only."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _evt(c, mint, net):
    c.execute("INSERT INTO memecoin_events(mint, network_id, side, source_user_id) VALUES(?,?,'buy','w')",
              (mint, net))


def _feed(c, mint, net):
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": f"{mint}-{net}", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": "w", "source_handle": "w", "network_id": net, "mint": mint, "ticker": "T",
        "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": "2026-07-19T00:00:00+00:00", "source_ts": "2026-07-19T00:00:00+00:00",
        "observed_at": "2026-07-19T00:00:00+00:00", "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=False)


def test_backfill_resolves_unambiguous_only():
    c = _c()
    _evt(c, "0xRESOLV", "evm-unknown")                  # resolvable: feed has it on 4663
    _feed(c, "0xRESOLV", "4663")
    _evt(c, "0xAMBIG", "evm-unknown")                   # ambiguous: known on BOTH base + 4663
    _feed(c, "0xAMBIG", "8453"); _evt(c, "0xAMBIG", "4663")
    _evt(c, "0xORPHAN", "evm-unknown")                  # no known-chain counterpart
    c.commit()

    dry = m.backfill_evm_unknown_events(c, dry_run=True)
    assert dry["resolved"] == 1 and dry["ambiguous"] == 1 and dry["unresolved"] == 1 and dry["updated"] == 0
    # dry run mutated nothing
    assert c.execute("SELECT COUNT(*) FROM memecoin_events WHERE network_id='evm-unknown'").fetchone()[0] == 3

    rep = m.backfill_evm_unknown_events(c)
    assert rep["updated"] == 1
    assert c.execute("SELECT network_id FROM memecoin_events WHERE mint='0xRESOLV'").fetchone()[0] == "4663"
    # ambiguous + orphan untouched
    assert c.execute("SELECT COUNT(*) FROM memecoin_events WHERE network_id='evm-unknown'").fetchone()[0] == 2
    # idempotent: second run resolves nothing new
    assert m.backfill_evm_unknown_events(c)["updated"] == 0
