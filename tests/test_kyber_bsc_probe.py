"""The 56 Kyber WBNB health probe (Alpha GO, 2026-09-29, after pos 66990).

Kyber's BSC index answered 4011 "token not found" for WBNB, so every 56 Kyber quote with the WBNB
quote token failed and exits went blind. The probe asks WBNB->USDT every 10 minutes and pages only
on a CHANGE: into a reject, and back to healthy. A transport failure is unknown and never pages.
It changes no behaviour.
"""
import io
import json
import urllib.error

from scripts.research import kyber_bsc_probe as probe


def _reject(code, message):
    return urllib.error.HTTPError("https://aggregator-api.kyberswap.com/bsc/api/v1/routes", 400,
                                  "Bad Request", {}, io.BytesIO(json.dumps(
                                      {"code": code, "message": message}).encode()))


def test_the_three_states_are_told_apart():
    assert probe.classify({"data": {"routeSummary": {"amountOut": "123"}}}) == "ok"
    assert probe.classify(_reject(4011, "token not found")) == "reject:4011"
    assert probe.classify(OSError("reset")) == "unknown:OSError"
    assert probe.classify({"data": {}}) == "unknown:no_route_summary"


def test_it_pages_on_a_reject_and_on_recovery_only():
    assert probe.page_text(None, "ok") is None                       # first run, healthy
    assert "4011" in probe.page_text("ok", "reject:4011")
    assert probe.page_text("reject:4011", "reject:4011") is None     # still broken: silent
    assert "recovered" in probe.page_text("reject:4011", "ok")
    assert probe.page_text("ok", "unknown:OSError") is None          # unknown never pages
    assert probe.page_text("reject:4011", "unknown:OSError") is None
    assert "4011" in probe.page_text(None, "reject:4011")            # starts broken: say so


def test_a_run_keeps_the_last_known_state_through_an_unknown(tmp_path):
    state = tmp_path / "kyber_bsc_probe.json"
    sent = []
    fetch = iter([_reject(4011, "token not found"), OSError("reset"),
                  {"data": {"routeSummary": {"amountOut": "1"}}}])

    def run_once():
        item = next(fetch)
        def get():
            if isinstance(item, Exception):
                raise item
            return item
        return probe.run(state, fetch_fn=get, send_fn=lambda text: sent.append(text) or True)
    assert run_once() == "reject:4011"
    assert run_once() == "unknown:OSError"
    assert run_once() == "ok"
    assert len(sent) == 2 and "4011" in sent[0] and "recovered" in sent[1]



def test_only_a_400_with_a_kyber_code_is_a_reject():
    """Gamma MED: a 429 {"message":"rate limit exceeded"} or a 503 read as reject:None and paged
    "Kyber rejects WBNB", then "recovered". Only HTTP 400 with a non-null code is a reject."""
    rate = urllib.error.HTTPError("https://x", 429, "Too Many", {}, io.BytesIO(b'{"message":"rate limit exceeded"}'))
    down = urllib.error.HTTPError("https://x", 503, "Unavailable", {}, io.BytesIO(b"<html>"))
    no_code = urllib.error.HTTPError("https://x", 400, "Bad", {}, io.BytesIO(b'{"message":"bad"}'))
    assert probe.classify(rate) == "unknown:http_429"
    coded_rate = urllib.error.HTTPError("https://x", 429, "Too Many", {},
                                        io.BytesIO(b'{"code":4290,"message":"rate limit"}'))
    assert probe.classify(coded_rate) == "unknown:http_429"        # a coded 429 is still not a reject
    assert probe.classify(down) == "unknown:http_503"
    assert probe.classify(no_code) == "unknown:http_400"
    assert probe.page_text("ok", probe.classify(rate)) is None


def _reject_forever():
    raise _reject(4011, "token not found")


def test_a_failed_send_keeps_the_state_and_retries_at_most_hourly(tmp_path):
    """2026-09-29: the first scheduled run found no Telegram token (.fomo.env was not loaded), still
    saved known=reject:4011, and the page was lost for good. A failed send now leaves the known state
    unchanged; the same page is retried at most once an hour (Alpha: cap the repeat)."""
    state = tmp_path / "kyber_bsc_probe.json"
    attempts = []

    def run_at(t, ok):
        return probe.run(state, fetch_fn=_reject_forever, now_fn=lambda: t,
                         send_fn=lambda text: attempts.append((t, text)) or ok)
    assert run_at(0, False) == "reject:4011"
    assert json.loads(state.read_text()).get("known") is None           # not delivered: not known
    run_at(600, True)
    assert [t for t, _ in attempts] == [0]                               # capped inside the hour
    run_at(3600, True)
    assert [t for t, _ in attempts] == [0, 3600] and "4011" in attempts[-1][1]
    assert json.loads(state.read_text())["known"] == "reject:4011"


def test_a_delivered_page_saves_the_state_and_never_repeats(tmp_path):
    state = tmp_path / "kyber_bsc_probe.json"
    attempts = []
    for t in (0, 600, 7200):
        probe.run(state, fetch_fn=_reject_forever, now_fn=lambda t=t: t,
                  send_fn=lambda text: attempts.append(text) or True)
    assert len(attempts) == 1 and json.loads(state.read_text())["known"] == "reject:4011"


def test_main_loads_both_env_files():
    """The Telegram keys live in .fomo.env; the probe must load the same files the runner does."""
    import inspect
    src = inspect.getsource(probe.main)
    assert "load_env_files(" in src and "names=" not in src


def test_the_telegram_sender_reports_delivery_not_the_tuple():
    """send_telegram returns (ok, elapsed_ms); a (False, 0.0) tuple is truthy and would read as sent."""
    assert probe.telegram_send("x", send=lambda text: (False, 0.0)) is False
    assert probe.telegram_send("x", send=lambda text: (True, 12.0)) is True
