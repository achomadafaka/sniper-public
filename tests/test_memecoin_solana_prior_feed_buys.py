"""S1 of the operator spec: prior feed BUYS as a partial hourly dollar-volume estimate.

Eligible: Solana, exact mint, a buy with a finite positive USD amount, trade time in [T-3600, T),
RECEIVED before T, and a trade identity: a validated transaction signature, or (addendum 2026-09-17) the
J7 trade id `swap:<uuid>`. One identity counts once. A cross-lane duplicate (same wallet, mint and side,
USD within 1%, trade time within 5 s, at least one side a trade id) is one trade; contradictory amounts
exclude both. The source is excluded by its identity and by that duplicate rule, and never subtracted again.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

MINT = "B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6"
SRC = "5" * 88
T = "2026-09-16T07:45:21.226799+00:00"
U1 = "swap:3ac25ded-177d-4bac-af84-45a07e00bbbc"
U2 = "swap:5b06384e-14bb-4826-8b81-26c40e6f3d7c"
U3 = "swap:8c87aa25-bd70-4f2b-892d-65f08e35483f"


def _s(c):
    return c * 88


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _row(conn, fid, *, at="2026-09-16T07:30:00.000Z", received="2026-09-16T07:30:00.400000+00:00",
         usd=100.0, side="buy", mint=MINT, network="solana", wallet="w1"):
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, side, their_size_usd, "
        "created_at, received_at_normalized, network_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (fid, ("uid-" + wallet) if wallet else "", wallet, mint, side, usd, at, received, network))
    conn.commit()


def _read(conn, **kw):
    args = dict(decision_at=T, source_signature=SRC, source_event_id="swap:raybot:%s:src:%s" % (SRC, MINT))
    args.update(kw)
    return m.solana_prior_feed_buys(conn, MINT, **args)


def test_signature_rows_count_once_per_transaction_and_sells_or_nan_do_not_count():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:%s" % (_s("A"), MINT), usd=300, wallet="w1")
    _row(conn, "pump:%s:%s:buy:w" % (_s("A"), MINT), usd=300, wallet="w1")              # same tx, other lane
    _row(conn, "swap:raybot:%s:w:%s" % (_s("B"), MINT), usd=200, wallet="w2", at="2026-09-16T07:20:00.000Z")
    _row(conn, "swap:raybot:%s:w:%s" % (_s("C"), MINT), usd=150, side="sell", wallet="w3")
    _row(conn, "swap:raybot:%s:w:%s" % (_s("D"), MINT), usd=float("nan"), wallet="w4")
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"], out["duplicate_count"], out["unknown_row_count"]) == (500.0, 2, 1, 1)
    assert {r["identity_kind"] for r in out["rows"]} == {"signature"}


def test_a_j7_trade_id_row_counts_once_with_its_identity_kind():
    conn = _conn()
    _row(conn, U1, usd=250, wallet="w1")
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"]) == (250.0, 1)
    assert [(r["identity"], r["identity_kind"]) for r in out["rows"]] == [(U1, "trade_id")]


def test_a_trade_id_row_and_a_signature_row_of_one_trade_count_once():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:%s" % (_s("A"), MINT), usd=300.0, wallet="w1", at="2026-09-16T07:30:00.000Z")
    _row(conn, U1, usd=302.5, wallet="w1", at="2026-09-16T07:30:04.500Z")               # 0.8% apart, 4.5 s apart
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"], out["duplicate_count"]) == (300.0, 1, 1)
    assert out["rows"][0]["identity_kind"] == "signature"


def test_two_trade_id_rows_of_one_trade_count_once():
    conn = _conn()
    _row(conn, U1, usd=100.0, wallet="w1")
    _row(conn, U2, usd=100.9, wallet="w1", at="2026-09-16T07:30:02.000Z")
    out = _read(conn)
    assert (out["eligible_count"], out["duplicate_count"]) == (1, 1)


def test_contradictory_amounts_for_one_trade_exclude_both_rows():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:%s" % (_s("A"), MINT), usd=300.0, wallet="w1")
    _row(conn, U1, usd=400.0, wallet="w1", at="2026-09-16T07:30:03.000Z")
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"], out["contradictory_count"]) == (None, 0, 2)


def test_signature_rows_with_different_amounts_for_one_signature_are_excluded():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:%s" % (_s("A"), MINT), usd=300)
    _row(conn, "pump:%s:%s:buy:w" % (_s("A"), MINT), usd=900)
    out = _read(conn)
    assert (out["feed_buy_usd"], out["contradictory_count"]) == (None, 2)


@pytest.mark.parametrize("second", [
    dict(wallet="w2"),                                        # another wallet
    dict(at="2026-09-16T07:30:05.100Z"),                      # more than 5 s apart
])
def test_trades_that_are_not_the_same_trade_both_count(second):
    conn = _conn()
    _row(conn, U1, usd=100.0, wallet="w1", at="2026-09-16T07:30:00.000Z")
    _row(conn, U2, usd=100.0, **{"wallet": "w1", "at": "2026-09-16T07:30:00.000Z", **second})
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"], out["duplicate_count"]) == (200.0, 2, 0)


def test_the_source_is_excluded_by_identity_and_by_the_duplicate_rule_on_every_lane():
    conn = _conn()
    src_id = "swap:raybot:%s:src:%s" % (SRC, MINT)
    _row(conn, src_id, usd=700.0, wallet="src", at="2026-09-16T07:45:20.000Z", received="2026-09-16T07:45:20.200+00:00")
    _row(conn, "onchain:%s" % SRC, usd=700.0, wallet="src", at="2026-09-16T07:45:20.000Z", received="2026-09-16T07:45:20.300+00:00")
    _row(conn, U1, usd=703.0, wallet="src", at="2026-09-16T07:45:17.000Z", received="2026-09-16T07:45:20.100+00:00")
    _row(conn, U2, usd=120.0, wallet="w9", at="2026-09-16T07:40:00.000Z")
    out = _read(conn, source_event_id=src_id)
    assert (out["feed_buy_usd"], out["eligible_count"], out["excluded_source_count"]) == (120.0, 1, 3)


def test_a_trade_id_source_is_excluded_by_its_own_id():
    conn = _conn()
    _row(conn, U3, usd=500.0, wallet="src", at="2026-09-16T07:45:10.000Z", received="2026-09-16T07:45:10.100+00:00")
    out = _read(conn, source_event_id=U3, source_signature=None)
    assert (out["feed_buy_usd"], out["excluded_source_count"]) == (None, 1)


@pytest.mark.parametrize("fid,wallet", [("swap:not-a-uuid", "w1"), (U1, "")])
def test_a_row_without_identity_or_wallet_is_unknown(fid, wallet):
    conn = _conn()
    _row(conn, fid, usd=100.0, wallet=wallet)
    out = _read(conn)
    assert (out["feed_buy_usd"], out["unknown_row_count"]) == (None, 1)


def test_only_rows_traded_inside_the_hour_and_received_before_t_count():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:m" % _s("A"), at="2026-09-16T06:45:21.227Z", received="2026-09-16T06:45:22+00:00", usd=10, wallet="a")
    _row(conn, "swap:raybot:%s:w:m" % _s("B"), at="2026-09-16T06:45:21.000Z", received="2026-09-16T06:45:22+00:00", usd=20, wallet="b")
    _row(conn, U1, at="2026-09-16T07:45:20.000Z", received="2026-09-16T07:45:21.300+00:00", usd=40, wallet="c")
    _row(conn, U2, at="2026-09-16T07:45:22.000Z", received="2026-09-16T07:45:22.100+00:00", usd=80, wallet="d")
    _row(conn, U3, at="2026-09-16T07:40:00.000Z", received=None, usd=160, wallet="e")
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"]) == (10.0, 1)
    assert (out["late_receipt_count"], out["unknown_row_count"]) == (1, 1)


def test_mint_case_and_chain_are_exact():
    conn = _conn()
    _row(conn, U1, mint=MINT.lower(), usd=50)
    _row(conn, U2, network="4663", usd=60)
    assert _read(conn)["feed_buy_usd"] is None


def test_no_eligible_row_is_unknown_not_zero():
    out = _read(_conn())
    assert (out["feed_buy_usd"], out["eligible_count"], out["rows"]) == (None, 0, [])


@pytest.mark.parametrize("fid,sig", [
    ("swap:raybot:%s:w:m" % SRC, SRC), ("onchain:%s" % SRC, SRC), ("pump:%s:m:buy:w" % SRC, SRC),
    (U1, None), ("swap:raybot:0OIl:w:m", None), (None, None)])
def test_one_validated_signature_parser_serves_the_feed_and_the_probe(fid, sig):
    from sniper.services import memecoin_solana_address_activity as A
    assert m.event_transaction_signature(fid) == sig
    assert A.source_signature is m.event_transaction_signature


@pytest.mark.parametrize("fid,kind", [
    ("swap:raybot:%s:w:m" % SRC, "signature"), (U1, "trade_id"), ("swap:not-a-uuid", None), ("rest:123", None)])
def test_event_trade_identity_names_its_kind(fid, kind):
    got = m.event_trade_identity(fid)
    assert (got[1] if got else None) == kind


def test_two_signatures_of_one_wallet_are_two_trades_even_seconds_apart():
    conn = _conn()
    _row(conn, "swap:raybot:%s:w:%s" % (_s("A"), MINT), usd=100.0, wallet="w1", at="2026-09-16T07:30:00.000Z")
    _row(conn, "swap:raybot:%s:w:%s" % (_s("B"), MINT), usd=100.0, wallet="w1", at="2026-09-16T07:30:01.000Z")
    out = _read(conn)
    assert (out["feed_buy_usd"], out["eligible_count"], out["duplicate_count"]) == (200.0, 2, 0)


def test_a_trade_id_row_that_contradicts_the_source_is_excluded_as_contradictory():
    conn = _conn()
    src_id = "swap:raybot:%s:src:%s" % (SRC, MINT)
    _row(conn, src_id, usd=700.0, wallet="src", at="2026-09-16T07:45:20.000Z", received="2026-09-16T07:45:20.200+00:00")
    _row(conn, U1, usd=300.0, wallet="src", at="2026-09-16T07:45:18.000Z", received="2026-09-16T07:45:20.100+00:00")
    out = _read(conn, source_event_id=src_id)
    assert (out["feed_buy_usd"], out["excluded_source_count"], out["contradictory_count"]) == (None, 1, 1)
