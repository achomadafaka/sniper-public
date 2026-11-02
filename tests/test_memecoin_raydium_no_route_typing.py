"""Exact exit quotes that "came back malformed" were Raydium no-route answers (Alpha GO 2026-10-15).

37 lines from 10-14 12:17Z to 10-15, all status 200 with body keys [id, msg, success, version]: when
Jupiter refuses a coin at request level, _quote_with_fallback asks Raydium transaction-v1 last, so
_typed_jupiter_exit_quote typed Raydium's {success: false, msg} body. It read only error/message,
so a coin neither provider could route read as MALFORMED_RESPONSE: infra, the ops streak and the
quote-infra page, never the token-unsellable counter.

Raydium's msg is typed now, by the same words as Jupiter's error, but only when Jupiter itself
refused the route (200 without a quote, 400, 404, 422). A throttled or locked-out Jupiter (429, 401,
403) answered nothing about the route, so Raydium's refusal alone stays malformed: it never
condemns a coin Jupiter might still route.

Also here: a native pump exit refused 6003 (TooLittleSolReceived) logs its signed floor, the
curve leg's expected out, the rung's slippage and the program's log lines (73882, n=2).
"""
import logging
import sqlite3
import unittest.mock as mock

import pytest

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution import solana_real_seams as S
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

MINT = "7K52aYQWnoRouteCoin1111111111111111111111pump"
RAY_NO_ROUTE = {"id": "e1", "success": False, "version": "V1", "msg": "ROUTE_NOT_FOUND"}
JUP_NO_ROUTE = {"error": "No routes found", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"}


@pytest.fixture(autouse=True)
def venue_fallback(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True, raising=False)
    monkeypatch.setattr(S, "_jup_host_cooldown", {})


def _http(jupiter, raydium=RAY_NO_ROUTE, calls=None):
    def http(method, url, headers=None, body=None):
        if calls is not None:
            calls.append(url)
        if url.startswith(S.RAY_BASE):
            return 200, raydium, None
        return jupiter[0], jupiter[1], None
    return http


@pytest.mark.parametrize("jupiter", [(400, JUP_NO_ROUTE), (200, JUP_NO_ROUTE), (404, {}), (422, {})])
def test_a_raydium_no_route_after_a_jupiter_refusal_is_no_route(jupiter):
    calls = []
    quote, failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http(jupiter, calls=calls))
    assert quote is None and failure is QuoteFailure.NO_ROUTE
    assert calls[-1].startswith(S.RAY_BASE)                  # Raydium answered last


@pytest.mark.parametrize("status", [429, 401, 403, 408])          # 408: slow, not "no route"
def test_a_throttled_or_locked_out_jupiter_never_lets_raydium_condemn_the_coin(status):
    quote, failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((status, {})))
    assert quote is None and failure is QuoteFailure.MALFORMED_RESPONSE


@pytest.mark.parametrize("msg, expected", [
    ("INSUFFICIENT_LIQUIDITY", QuoteFailure.INSUFFICIENT_LIQUIDITY),
    ("REQ_INPUT_MINT_ERROR", QuoteFailure.TOKEN_REJECTED),
    ("SERVER_BUSY", QuoteFailure.MALFORMED_RESPONSE),
])
def test_raydium_words_type_as_jupiters_do(msg, expected):
    body = dict(RAY_NO_ROUTE, msg=msg)
    assert S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((400, JUP_NO_ROUTE), body))[1] is expected


def test_an_unknown_raydium_msg_is_named_in_the_malformed_line(caplog):
    body = dict(RAY_NO_ROUTE, msg="SERVER_BUSY")
    with caplog.at_level(logging.WARNING):                 # its own mint: the line samples per mint
        S._typed_jupiter_exit_quote("MsgLogCoin" + MINT[10:], 1000, 8.0, http=_http((400, JUP_NO_ROUTE), body))
    lines = [r.getMessage() for r in caplog.records if "exact exit quote malformed" in r.getMessage()]
    assert lines and "msg=SERVER_BUSY" in lines[-1]


def _counters_after_prepass(failure):
    from sniper.execution.exit_preview import PreviewResult
    from sniper.services import memecoin_real_runner as R
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=MINT, ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
                          their_size_usd=0.0, opened_at="2026-10-15T10:00:00+00:00",
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    pos = m.get_position(c, pid)
    preview = PreviewResult.failed(failure, provider_id="solana:jupiter", provider_group="solana:exact_quote")
    with mock.patch.object(R, "context_for_position", lambda contexts, p: {"preview_exit_fn": None}), \
         mock.patch("sniper.execution.solana_quote.quote_for", lambda pm, mint, net: {"price": 1.0}), \
         mock.patch.object(R, "_resolve_override_and_mcap", lambda *a, **k: (None, None)), \
         mock.patch.object(R, "effective_trail", lambda pos, ov: (30.0, False)), \
         mock.patch.object(R, "_effective_derisk_pct", lambda pos, ov: 40.0), \
         mock.patch.object(R, "_executable_unit_price", lambda conn, pos, preview, **k: None):
        R._wo4_miss_prepass(
            [pos], conn=c, contexts={}, price_map={}, frozen_by_id={pid: None}, engine_ids=set(),
            cfg=type("C", (), {"MEMECOIN_REAL_UNSELLABLE_SWEEPS": 0})(),
            preview_by_position={pid: preview}, now_iso="2026-10-15T10:05:00+00:00")
    row = m.get_position(c, pid)
    return row["exact_quote_misses"], row["exact_quote_infra_misses"]


def test_a_no_route_from_both_providers_advances_the_unsellable_counter_never_the_infra_streak():
    failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((400, JUP_NO_ROUTE)))[1]
    assert _counters_after_prepass(failure) == (1, 0)


def test_a_throttled_jupiter_with_a_raydium_refusal_stays_on_the_infra_streak():
    failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((429, {})))[1]
    assert _counters_after_prepass(failure) == (0, 1)


# --- 6003 on a native pump exit: name the floor, the expected out and the program's own lines ---

def test_a_6003_simulation_refusal_logs_floor_expected_slippage_and_program_lines(caplog):
    from sniper.execution import pump_native_exit as N
    from test_memecoin_pump_native_exit import FRIES, US, _fake_jup, _fake_rpc
    rpc = _fake_rpc(sim_err={"InstructionError": [2, {"Custom": 6003}]})
    inner = rpc

    def rpc_with_logs(method, params):
        out = inner(method, params)
        if method == "simulateTransaction":
            out["value"]["logs"] = [
                "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
                "Program log: Instruction: SellV2",
                "Program log: AnchorError occurred. Error Code: TooLittleSolReceived. Error Number: 6003.",
                "Program log: Left: 9811234",
                "Program log: Right: 10374739",
                "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P failed: custom program error: 0x1773"]
        return out
    with caplog.at_level(logging.WARNING):
        assert N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=rpc_with_logs,
                                   jup_call=_fake_jup()) is None
    line = next(r.getMessage() for r in caplog.records if "6003" in r.getMessage() and "floor=" in r.getMessage())
    for part in ("floor=10374739", "expected=11527488", "slippage_bps=1000", "amount=5707019249939",
                 "Left: 9811234", "Right: 10374739"):
        assert part in line, (part, line)


def test_another_simulation_refusal_keeps_todays_short_line(caplog):
    from sniper.execution import pump_native_exit as N
    from test_memecoin_pump_native_exit import FRIES, US, _fake_jup, _fake_rpc
    with caplog.at_level(logging.WARNING):
        N.build_native_exit(FRIES, 5707019249939, 10.0, user=US,
                            rpc=_fake_rpc(sim_err={"InstructionError": [2, {"Custom": 6001}]}), jup_call=_fake_jup())
    assert not any("floor=" in r.getMessage() for r in caplog.records)
    assert any("simulation refused" in r.getMessage() for r in caplog.records)


def test_only_raydiums_own_body_is_read_for_msg(monkeypatch):
    """With the venue fallback off Raydium is never asked; a Jupiter body shaped like Raydium's is
    not Raydium's answer and stays malformed."""
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", False)
    lookalike = {"success": False, "msg": "ROUTE_NOT_FOUND"}
    calls = []
    failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((200, lookalike), calls=calls))[1]
    assert failure is QuoteFailure.MALFORMED_RESPONSE and not any(u.startswith(S.RAY_BASE) for u in calls)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_jupiter_5xx_never_reaches_raydium_and_stays_infra(status):
    """Gamma Y4: a Jupiter 5xx does not fall back, so Raydium is never asked and the miss is
    RPC_TRANSPORT (infra). Never a token class."""
    from sniper.analysis.memecoin_quote_failures import is_infra
    calls = []
    failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http((status, {}), calls=calls))[1]
    assert failure is QuoteFailure.RPC_TRANSPORT and is_infra(failure)
    assert not any(url.startswith(S.RAY_BASE) for url in calls)


@pytest.mark.parametrize("status", [500, 503, 429, 401, 403])
def test_a_raydium_refusal_after_a_jupiter_failure_is_never_a_route_answer(status):
    """The same rule if a 5xx ever did fall back: only a Jupiter route refusal lets Raydium's msg type."""
    answers = [("https://api.jup.ag/swap/v1/quote?x", status),
               (S.RAY_BASE + "/compute/swap-base-in?x", 200)]
    assert S._jupiter_refused_before_raydium(answers) is False
    assert S._jupiter_refused_before_raydium([(answers[0][0], 400), answers[1]]) is True


@pytest.mark.parametrize("jupiter", [(409, {"error": "conflict"}), (410, None)])
def test_raydium_no_route_with_jupiter_silent_about_the_route_is_route_not_indexed(jupiter):
    """ANIMA 73930 (2026-10-17): 6.5 min of Raydium ROUTE_NOT_FOUND typed malformed = infra; the
    unsellable counters never moved while the display ran +70% and the TP could not confirm. Raydium
    is asked only after a non-5xx Jupiter refusal; Jupiter's route answers (200/400/404/422) already
    type NO_ROUTE, its access answers (401/403/408/429) stay malformed (2026-10-15 pin), the rest here."""
    quote, failure = S._typed_jupiter_exit_quote(MINT, 1000, 8.0, http=_http(jupiter))
    assert quote is None and failure is QuoteFailure.ROUTE_NOT_INDEXED


def test_route_not_indexed_advances_the_misses_unless_the_group_has_no_success_at_all():
    from sniper.analysis.memecoin_quote_failures import ADVANCE, OPS, sweep_miss_verdicts
    rni = QuoteFailure.ROUTE_NOT_INDEXED
    assert sweep_miss_verdicts([(1, rni, "solana:exact_quote")]) == {1: ADVANCE}
    assert sweep_miss_verdicts([(1, rni, "g"), (2, None, "g")])[1] == ADVANCE, "other mints quote fine"
    assert sweep_miss_verdicts([(1, rni, "g"), (2, rni, "g")]) == {1: OPS, 2: OPS}, "outage defense kept"


def test_the_malformed_line_names_jupiters_answers(caplog):
    with caplog.at_level(logging.WARNING):
        S._typed_jupiter_exit_quote("JupNamed" + MINT[8:], 1000, 8.0, http=_http((429, {})))
    lines = [r.getMessage() for r in caplog.records if "exact exit quote malformed" in r.getMessage()]
    assert lines and "jupiter=[429" in lines[-1]          # one entry per Jupiter host asked
