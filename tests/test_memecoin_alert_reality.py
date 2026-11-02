"""Front-run BUY alert content must reflect ACHIEVABLE reality, not an inflated vanity number.

Evaluation (2026-07-15) found the alert's credibility line was built from a raw all-arms/all-chain
`SUM(realized_pnl), COUNT(*)` over memecoin_positions — which (a) inflates the close count ~5-9x
(it is arms x coins, not decisions) and (b) includes fomo-priced fantasy fills no real order can
hit. 11/40 armed wallets showed a POSITIVE record in the alert while being <=0 on the honest gate
(smol_intern alert +$674/159 -> honest -$63/19c; Magneto +$267/87 -> 0c/$0). The alert therefore
told the operator the OPPOSITE of the arming decision.

Fixes under test:
  1. the record shown is the SAME honest metric the arm gate uses (alpha_b_copy_stats: alpha_b-only,
     fomo-excluded), labeled as such, plus a gate ✓/✗ status;
  2. feed-lag is labeled 'unknown' (missing/negative) or 'stale Ns ⚠' (> threshold) instead of a
     bare number that dresses a 15-minute-old signal as a sub-second front-run;
  3. a consensus line surfaces prior distinct tracked buyers of the same coin in the last 60m
     (a real +20% predictor per the 2026-07-11 beat-trader study);
  4. the delayed-catch (follow-activity) alert carries the same honest record + gate.
"""
import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_telegram import (
    build_alert_message, build_follow_activity_alert)


def _ev(**kw):
    d = dict(ticker="RWA", source_handle="iammwealthyGod888", their_size_usd=2500.0,
             their_market_cap=566500.0, created_at="2026-07-15T14:53:26.687Z", lag_s=0.3,
             mint="0x4a380618777eed8d513bcd6e983df3c5d2ba7777", network_id="4663")
    d.update(kw)
    return SimpleNamespace(**d)


# ---- 1. honest record + gate status --------------------------------------------------------------

def test_shows_honest_alpha_b_record_not_a_raw_total():
    msg = build_alert_message(_ev(), context={
        "real_armed": False, "paper_pnl": 171.4, "paper_n": 2, "gate_ok": False,
        "prior_buyers_60m": 0})
    assert "honest" in msg.lower()          # labeled as the achievable metric
    assert "171" in msg and "2 closes" in msg     # the honest number, not an inflated one
    # the OLD misleading phrasing ("N closes") must be gone
    assert "/19 closes" not in msg


def test_gate_check_mark_when_above_threshold():
    msg = build_alert_message(_ev(), context={
        "real_armed": False, "paper_pnl": 358.0, "paper_n": 11, "gate_ok": True,
        "prior_buyers_60m": 0})
    assert "gate ✓" in msg             # ✓  -> ready to arm


def test_front_run_alert_reports_receipt_without_an_entry_result():
    msg = build_alert_message(_ev(), context={
        "real_armed": False,
        "authority_status": "watch_only",
        "gate_applicable": True,
        "paper_pnl": 0.0,
        "paper_n": 0,
        "gate_ok": False,
    })
    assert "Source buy received. This notice does not report our entry result." in msg


def test_gate_cross_when_below_threshold_even_if_armed():
    """An ARMED wallet that fails the honest gate must read 'gate ✗' — the exact 'this armed wallet
    is actually a loser' signal the old alert hid (smol_intern)."""
    msg = build_alert_message(_ev(source_handle="smol_intern"), context={
        "real_armed": True, "paper_pnl": -63.0, "paper_n": 19, "gate_ok": False,
        "prior_buyers_60m": 0})
    assert "ARMED ⚡" in msg
    assert "REAL-ELIGIBLE" not in msg
    assert "gate ✗" in msg             # ✗
    assert "-$63" in msg and "19 closes" in msg


def test_no_honest_closes_reads_honestly_not_as_a_fat_number():
    """Magneto: +$267/87 in the old alert, but 0 honest closes. Must say so."""
    msg = build_alert_message(_ev(source_handle="Magneto"), context={
        "real_armed": True, "paper_pnl": 0.0, "paper_n": 0, "gate_ok": False,
        "prior_buyers_60m": 0})
    assert "no honest" in msg.lower()
    assert "267" not in msg                 # the fantasy number never appears


# ---- 2. feed-lag labeling ------------------------------------------------------------------------

def test_fresh_lag_shows_seconds():
    assert "feed lag 0.3s" in build_alert_message(_ev(lag_s=0.3))


def test_stale_lag_is_flagged_not_dressed_as_a_frontrun():
    msg = build_alert_message(_ev(lag_s=890.0))
    assert "stale" in msg.lower()
    assert "890" in msg


def test_negative_or_missing_lag_reads_unknown():
    assert "unknown" in build_alert_message(_ev(lag_s=-1.0)).lower()
    assert "unknown" in build_alert_message(_ev(lag_s=None)).lower()


# ---- 3. consensus line ---------------------------------------------------------------------------

def test_consensus_line_appears_when_prior_buyers_exist():
    msg = build_alert_message(_ev(), context={
        "real_armed": True, "paper_pnl": 100.0, "paper_n": 6, "gate_ok": True,
        "prior_buyers_60m": 3})
    assert "consensus" in msg.lower()
    assert "3" in msg


def test_no_consensus_line_for_a_lone_buy():
    msg = build_alert_message(_ev(), context={
        "real_armed": True, "paper_pnl": 100.0, "paper_n": 6, "gate_ok": True,
        "prior_buyers_60m": 0})
    assert "consensus" not in msg.lower()


# ---- 4. the db helper behind the consensus line --------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _mkev(uid, ts, mint="0xcoin", side="buy"):
    return {"fomo_event_id": f"{uid}-{ts}", "source_user_id": uid, "source_handle": uid,
            "mint": mint, "ticker": "T", "side": side, "their_price": 1.0,
            "their_market_cap": 1.0, "their_size_usd": 100.0, "created_at": ts,
            "received_at": ts, "lag_s": 1.0, "network_id": "4663"}


def test_prior_buyers_60m_counts_distinct_others_in_window():
    c = _conn()
    m.insert_event(c, _mkev("alice", "2026-07-15T13:00:00.000Z"))
    m.insert_event(c, _mkev("bob",   "2026-07-15T13:30:00.000Z"))
    m.insert_event(c, _mkev("bob",   "2026-07-15T13:40:00.000Z"))   # dup wallet -> still 1
    n = m.prior_buyers_60m(c, mint="0xcoin", network_id="4663",
                           source_user_id="carol", before_iso="2026-07-15T13:50:00.000Z")
    assert n == 2                                                    # alice + bob


def test_prior_buyers_60m_excludes_self_old_and_future():
    c = _conn()
    m.insert_event(c, _mkev("carol", "2026-07-15T13:45:00.000Z"))   # self -> excluded
    m.insert_event(c, _mkev("dave",  "2026-07-15T12:00:00.000Z"))   # >60m before -> excluded
    m.insert_event(c, _mkev("erin",  "2026-07-15T14:10:00.000Z"))   # after ref -> excluded
    m.insert_event(c, _mkev("frank", "2026-07-15T13:20:00.000Z"))   # in window -> counts
    n = m.prior_buyers_60m(c, mint="0xcoin", network_id="4663",
                           source_user_id="carol", before_iso="2026-07-15T13:50:00.000Z")
    assert n == 1                                                    # only frank


def test_prior_buyers_60m_ignores_sells_and_bad_input():
    c = _conn()
    m.insert_event(c, _mkev("greg", "2026-07-15T13:30:00.000Z", side="sell"))
    assert m.prior_buyers_60m(c, mint="0xcoin", network_id="4663",
                              source_user_id="x", before_iso="2026-07-15T13:50:00.000Z") == 0
    assert m.prior_buyers_60m(c, mint=None, network_id="4663",
                              source_user_id="x", before_iso="2026-07-15T13:50:00.000Z") is None


def test_prior_buyers_60m_returns_unknown_for_unparseable_time_evidence():
    c = _conn()
    m.insert_event(c, _mkev("bad-prior", "not-an-iso-time"))

    assert m.prior_buyers_60m(
        c, mint="0xcoin", network_id="4663", source_user_id="current",
        before_iso="2026-07-15T13:50:00.000Z",
    ) is None
    assert m.prior_buyers_60m(
        c, mint="0xcoin", network_id="4663", source_user_id="current",
        before_iso="not-an-iso-time",
    ) is None


def test_prior_buyers_60m_is_chain_scoped_for_same_evm_address():
    c = _conn()
    m.insert_event(c, _mkev("base", "2026-07-15T13:20:00.000Z"))
    robinhood = _mkev("robinhood", "2026-07-15T13:30:00.000Z")
    robinhood["network_id"] = "8453"
    m.insert_event(c, robinhood)
    assert m.prior_buyers_60m(
        c, mint="0xcoin", network_id="4663", source_user_id="current",
        before_iso="2026-07-15T13:50:00.000Z",
    ) == 1
    assert m.prior_buyers_60m(
        c, mint="0xcoin", network_id="8453", source_user_id="current",
        before_iso="2026-07-15T13:50:00.000Z",
    ) == 1


def test_ticker_enrichment_is_chain_scoped_and_uses_latest_observation():
    c = _conn()
    base_old = _mkev("base-old", "2026-07-15T13:20:00.000Z")
    base_old.update(network_id="8453", ticker="BASE_OLD",
                    received_at_normalized="2026-07-15T13:20:01.000Z")
    base_new = _mkev("base-new", "2026-07-15T13:30:00.000Z")
    base_new.update(network_id="8453", ticker="BASE_NEW",
                    received_at_normalized="2026-07-15T13:30:01.000Z")
    robinhood = _mkev("robinhood", "2026-07-15T13:40:00.000Z")
    robinhood.update(network_id="4663", ticker="ROBIN",
                     received_at_normalized="2026-07-15T13:40:01.000Z")
    for event in (base_old, base_new, robinhood):
        m.insert_event(c, event)

    assert m.resolve_mint_ticker(c, "0xcoin", network_id="8453") == "BASE_NEW"
    assert m.resolve_mint_ticker(c, "0xcoin", network_id="4663") == "ROBIN"
    assert m.resolve_mint_ticker(c, "0xcoin", network_id="56") is None
    assert m.resolve_mint_ticker(c, "", network_id="8453") is None


# ---- 5. delayed-catch carries the honest record too ----------------------------------------------

def test_delayed_catch_shows_honest_record_and_gate():
    ev = _ev(ticker="", mint="0xdef456")
    msg = build_follow_activity_alert(ev, context={
        "paper_pnl": -63.0, "paper_n": 19, "gate_ok": False})
    assert "DELAYED" in msg.upper()
    assert "honest" in msg.lower()
    assert "gate ✗" in msg


def test_delayed_catch_backwards_compatible_without_context():
    # existing callers pass no context -> must still build
    msg = build_follow_activity_alert(_ev(ticker="PONS"))
    assert "PONS" in msg


def test_follow_refusal_renders_exact_attempt_reason_and_pre_reservation_stage():
    msg = build_alert_message(_ev(
        ticker="Jimothy",
        network_id="solana",
        their_market_cap=912_861.0,
        mint="Ge87EtsjwRQbHaqQmKRno69RFTwh9bfSsm99XNxTpump",
    ), context={
        "real_armed": True,
        "paper_pnl": -26.0,
        "paper_n": 10,
        "gate_ok": False,
        "entry_status": "refused",
        "entry_code": "skipped_chase",
        "entry_reason": "coin ran 5.2x above first-seen price (cap 2.0x)",
        "attempt_id": "11a13062d19d4360846db6661e5f4d08",
    })

    assert msg.startswith("🟠 Jimothy —")
    assert "mcap ~$912.9k | chain solana" in msg
    assert "wallet: ARMED ⚡" in msg
    assert "REAL-ELIGIBLE" not in msg
    assert "entry: REFUSED · skipped_chase · pre-reservation" in msg
    assert "reason: coin ran 5.2x above first-seen price (cap 2.0x)" in msg
    assert "attempt: 11a13062d19d4360846db6661e5f4d08" in msg


def test_follow_fill_renders_confirmed_position_without_inventing_missing_stage():
    msg = build_alert_message(_ev(), context={
        "real_armed": True,
        "paper_pnl": 10.0,
        "paper_n": 2,
        "gate_ok": False,
        "entry_status": "filled",
        "entry_reason": "confirmed fill persisted",
        "attempt_id": "attempt-filled",
        "pending_id": 12,
        "tx_sig": "0xabc",
        "position_id": 34,
    })

    assert msg.startswith("🟢 RWA —")
    assert "entry: FILLED · position persisted" in msg
    assert "attempt: attempt-filled" in msg


def test_follow_not_attempted_is_yellow_and_explicit():
    msg = build_follow_activity_alert(_ev(ticker="PONS"), context={
        "real_armed": True,
        "paper_pnl": 0.0,
        "paper_n": 0,
        "gate_ok": False,
        "entry_status": "not_attempted",
        "entry_reason": "follow real lane disabled",
    })

    assert msg.startswith("🟡")
    assert "entry: NOT ATTEMPTED" in msg
    assert "reason: follow real lane disabled" in msg
    assert "attempt:" not in msg


# ---- chart link (2026-09-11) --------------------------------------------------------------------
# A non-fomo chart one tap away, on GMGN (2026-09-23: DexScreener links removed everywhere).

def test_front_run_alert_links_a_gmgn_chart_on_the_coins_own_chain():
    msg = build_alert_message(_ev())
    assert ("chart: https://gmgn.ai/robinhood/token/"
            "0x4a380618777eed8d513bcd6e983df3c5d2ba7777") in msg
    assert "dexscreener" not in msg


def test_solana_chart_link_keeps_the_mint_case():
    mint = "48iQL3fBXS7u2WJwVDfJZPH3jcAs9MJchUCT6mGdwH2v"
    msg = build_alert_message(_ev(network_id="solana", mint=mint))
    assert "chart: https://gmgn.ai/sol/token/%s" % mint in msg


def test_no_chart_link_on_a_chain_dexscreener_is_not_mapped_for():
    msg = build_alert_message(_ev(network_id="143"))
    assert "chart:" not in msg
    assert "Axiom:" not in msg                           # Axiom does not trade Monad


def test_delayed_catch_alert_carries_the_chart_link_too():
    msg = build_follow_activity_alert(_ev())
    assert "chart: https://gmgn.ai/robinhood/token/" in msg


def test_source_notice_timestamp_names_receipt_instead_of_delivery():
    msg = build_alert_message(_ev(), sent_at="2026-09-11T11:25:09.579406+00:00")
    assert "received: 2026-09-11T11:25:09.579406+00:00" in msg
    assert "sent:" not in msg
    assert "This notice does not report our entry result." in msg
