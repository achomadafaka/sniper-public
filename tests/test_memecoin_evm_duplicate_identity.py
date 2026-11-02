"""Two EVM lanes reporting one retained buy deliver its owning attempt's result."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
from sniper.services.memecoin_entry_view import entry_view

NOW = "2026-10-13T16:07:38+00:00"
TX = "0x51980e25d3d3b7130a790332fd7d7dc1a6cb77f1ab80160060e9be33399adf18"
MINT = "0x061732dc141e0a2d37f5fa91cf5343f24064e60f"
ACTOR = "0xe9132a6e4a610b796abba5fd4210911fafa55a22"
UID = "2f672788-0d2a-59f9-890d-b2c2316eb0bd"
CHAIN = "4663"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def pair(c, *, tx=TX, actor=ACTOR, mint=MINT, uid=UID, chain=CHAIN,
         origin=None, detection=None, feed=None):
    origin = origin or f"swap:raybot:{tx}:{actor}:{mint}"
    detected = dict(signature=f"{CHAIN}:{TX}:{MINT}", source_user_id=UID,
                    address=ACTOR, network_id=CHAIN, mint=MINT,
                    detected_at=NOW, created_at=NOW)
    detected.update(detection or {})
    event = dict(fomo_event_id=origin, source_user_id=uid, network_id=chain,
                 mint=mint, side="buy", signal_source="follow_activity", created_at=NOW)
    event.update(feed or {})
    for table, row in [("onchain_wallet_detections", detected), ("memecoin_events", event)]:
        c.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                  tuple(row.values()))
    c.commit()
    for aid, source, event_id, net, token, user, reason, gates in [
        ("primary", "follow_activity", origin, chain, mint, uid,
         "exact quote is +42% above source price (cap 30%)", None),
        ("blocked", "onchain_watch", f"onchain:{detected['signature']}", CHAIN, MINT, UID,
         "one position per coin", {"duplicate_blocker": {"kind": "reservation", "attempt_id": "primary"}}),
    ]:
        m.start_entry_attempt(c, attempt_id=aid, origin_event_id=event_id,
            network_id=net, mint=token, source_user_id=user, signal_source=source,
            decision_started_at=NOW)
        c.execute("UPDATE memecoin_entry_attempts SET terminal_status='refused', terminal_reason=?,"
                  "completed_at=?, gate_inputs_json=? WHERE attempt_id=?",
                  (reason, NOW, json.dumps(gates) if gates else None, aid))
        c.commit()


def test_same_retained_evm_buy_coalesces_across_onchain_and_j7_lanes(conn):
    pair(conn)
    projected = entry_view(conn, attempt_id="blocked")
    assert projected["duplicate_same_source"] is True
    identity = (CHAIN, UID, ACTOR, (CHAIN, MINT), TX)
    for aid in ("blocked", "primary"):
        attempt = dict(conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (aid,)).fetchone())
        assert m.entry_attempt_source_identity(conn, attempt) == identity


def test_same_buy_duplicate_does_not_hide_owning_price_refusal(conn):
    pair(conn)
    sent = []
    life = NotificationLifecycle(conn, now_fn=lambda: "2026-10-13T16:07:40+00:00",
        read_view=lambda **kw: entry_view(conn, **kw), send_fn=lambda text: sent.append(text) or (True, 1),
        render_fn=lambda v: v["reason"], eligible_fn=lambda v: True)
    life.activate("2026-10-13T16:07:30+00:00")
    life.observe(entry_view(conn, attempt_id="blocked"))
    life.deliver()
    life.observe(entry_view(conn, attempt_id="primary"))
    life.deliver()
    assert sent == ["exact quote is +42% above source price (cap 30%)"]


def test_evm_hex_identity_is_case_insensitive(conn):
    pair(conn, tx="0x" + TX[2:].upper(), actor="0x" + ACTOR[2:].upper(), mint="0x" + MINT[2:].upper())
    assert entry_view(conn, attempt_id="blocked")["duplicate_same_source"] is True


@pytest.mark.parametrize("changes", [
    {"tx": "0x" + "b" * 64}, {"actor": "0x" + "b" * 40},
    {"mint": "0x" + "c" * 40}, {"uid": "another-wallet"}, {"chain": "56"},
    {"origin": "swap:opaque-uuid"}, {"origin": f"swap:raybot:{TX}:{ACTOR}"},
    {"origin": f"swap:raybot:not-a-hash:{ACTOR}:{MINT}"},
    {"origin": f"swap:raybot:{TX}:unknown:{MINT}"},
    {"origin": f"swap:raybot:{TX}:{ACTOR}:0x{'c' * 40}"},
    {"detection": {"network_id": "56"}}, {"detection": {"source_user_id": "other"}},
    {"detection": {"mint": "0x" + "c" * 40}}, {"detection": {"address": "unknown"}},
    {"detection": {"signature": f"56:{TX}:{MINT}"}},
    {"detection": {"signature": f"{CHAIN}:{TX}:0x{'c' * 40}"}},
    {"detection": {"signature": f"{CHAIN}:opaque:{MINT}"}},
    {"feed": {"network_id": "56"}}, {"feed": {"source_user_id": "other"}},
    {"feed": {"mint": "0x" + "c" * 40}},
])
def test_unknown_or_conflicting_source_facts_keep_refusals_independent(conn, changes):
    pair(conn, **changes)
    assert entry_view(conn, attempt_id="blocked")["duplicate_same_source"] is False


def test_missing_retained_feed_row_cannot_coalesce_from_origin_alone(conn):
    pair(conn)
    conn.execute("DELETE FROM memecoin_events")
    assert entry_view(conn, attempt_id="blocked")["duplicate_same_source"] is False


def lane_pair(c, *, fill_tx=TX, fill_wallet=ACTOR):
    """A Relay-routed 4663 buy seen by the on-chain watcher (owner) and the rh fast lane (refused
    duplicate). The lane's facts name the buyer only in the proven fill: Relay's EOA is the payer.
    Live 2026-10-14 22:36:48Z (OmakaseOnly, 72019cd9) the duplicate paged its in-progress refusal."""
    detected = dict(signature=f"{CHAIN}:{TX}:{MINT}", source_user_id=UID, address=ACTOR,
                    network_id=CHAIN, mint=MINT, detected_at=NOW, created_at=NOW)
    c.execute(f"INSERT INTO onchain_wallet_detections ({','.join(detected)}) "
              f"VALUES ({','.join('?' for _ in detected)})", tuple(detected.values()))
    c.commit()
    facts = dict(signature=TX, network_id=CHAIN, mint=MINT, source_user_id=UID,
                 source_initiation={"payer": "0x" + "f7" * 20, "beneficiary": ACTOR, "tx_hash": TX},
                 source_fill={"wallet": fill_wallet, "tx_hash": fill_tx, "mint": MINT,
                              "proof_version": "wallet-net-fill-v1"})
    for aid, source, event_id, reason, gates, trace in [
        ("primary", "onchain_watch", f"onchain:{detected['signature']}",
         "route quote is 1.53x the source price (cap 1.50x)", None, None),
        ("blocked", "rh_fast_lane", f"rhfast:{CHAIN}:{TX}:{MINT}", "one position per coin",
         {"duplicate_blocker": {"kind": "reservation", "attempt_id": "primary"}},
         {"source_evidence": facts}),
    ]:
        m.start_entry_attempt(c, attempt_id=aid, origin_event_id=event_id, network_id=CHAIN,
            mint=MINT, source_user_id=UID, signal_source=source, decision_started_at=NOW)
        c.execute("UPDATE memecoin_entry_attempts SET terminal_status='refused', terminal_reason=?,"
                  "completed_at=?, gate_inputs_json=?, trace_json=? WHERE attempt_id=?",
                  (reason, NOW, json.dumps(gates) if gates else None,
                   json.dumps(trace) if trace else None, aid))
        c.commit()


def test_relay_routed_lane_duplicate_binds_its_buyer_from_the_proven_fill(conn):
    lane_pair(conn)
    blocked = dict(conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id='blocked'").fetchone())
    assert m.entry_attempt_source_identity(conn, blocked) == (CHAIN, UID, ACTOR, (CHAIN, MINT), TX)
    assert entry_view(conn, attempt_id="blocked")["duplicate_same_source"] is True


@pytest.mark.parametrize("changes", [{"fill_tx": "0x" + "b" * 64}, {"fill_wallet": None},
                                     {"fill_wallet": "0x" + "b" * 40}])
def test_lane_fill_for_another_buy_or_wallet_keeps_the_refusal_independent(conn, changes):
    lane_pair(conn, **changes)
    assert entry_view(conn, attempt_id="blocked")["duplicate_same_source"] is False
