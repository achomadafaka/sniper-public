#!/usr/bin/env python3
"""Kyber BSC WBNB health probe (Alpha GO, 2026-09-29, after pos 66990).

Kyber's BSC index answered HTTP 400 {"code":4011,"message":"token not found"} for WBNB, so every
chain-56 Kyber quote on the WBNB quote token failed and a Kyber-only exit went blind. This asks
WBNB->USDT once per run (cron: every 10 minutes) and pages only on a CHANGE: into a reject, and back
to healthy. A transport failure is unknown: it is recorded and never pages. Read-only; it changes
no trading behaviour.

    python3 scripts/research/kyber_bsc_probe.py            # one probe, one summary line
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf46de08c9173bc095"
USDT = "0x55d398326f99059ff775485246999027b3197955"
URL = "https://aggregator-api.kyberswap.com/bsc/api/v1/routes?" + urllib.parse.urlencode(
    {"tokenIn": WBNB, "tokenOut": USDT, "amountIn": str(10 ** 17), "gasInclude": "true"})
RETRY_S = 3600          # an undelivered page is retried at most once an hour (Alpha, 2026-09-29)


def fetch():
    request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def classify(answer):
    """'ok' | 'reject:<kyber code>' | 'unknown:<why>'. `answer` is the JSON or the exception."""
    if isinstance(answer, urllib.error.HTTPError):
        # Only Kyber's own 400 with an error code is a reject (Gamma MED, 2026-09-29): a 429 rate
        # limit or a 503 is the provider not answering, never "Kyber rejects WBNB".
        code = None
        if answer.code == 400:
            try:
                code = json.loads(answer.read(500) or b"{}").get("code")
            except (ValueError, OSError, AttributeError):
                code = None
        return "reject:%s" % code if code is not None else "unknown:http_%s" % answer.code
    if isinstance(answer, Exception):
        return "unknown:%s" % type(answer).__name__
    summary = ((answer or {}).get("data") or {}).get("routeSummary") if isinstance(answer, dict) else None
    return "ok" if summary and int(summary.get("amountOut") or 0) > 0 else "unknown:no_route_summary"


def page_text(previous, now):
    """The page for a state change, or None. Unknown is neither a reject nor ok, so it never pages."""
    if now.startswith("reject") and now != previous:
        return ("56 Kyber rejects WBNB (%s): every Kyber quote on the WBNB quote token is blind, "
                "Kyber-only 56 exits cannot quote" % now)
    if now == "ok" and previous is not None and previous.startswith("reject"):
        return "56 Kyber WBNB recovered (was %s)" % previous
    return None


def run(state_path, *, fetch_fn=fetch, send_fn=None, now_fn=time.time):
    """One probe. The last KNOWN state is kept through an unknown, so a transport blip between two
    rejects cannot page twice. A page that was not delivered (send_fn returned falsy) does not
    advance the known state, so it is sent again, at most once per RETRY_S (2026-09-29: a missing
    token lost the first reject page for good). Returns this run's state."""
    try:
        now = classify(fetch_fn())
    except Exception as exc:                 # noqa: BLE001 -- every failure is an answer here
        now = classify(exc)
    state_path = Path(state_path)
    try:
        saved = json.loads(state_path.read_text())
    except (OSError, ValueError):
        saved = {}
    previous = saved.get("known")
    text = page_text(previous, now)
    if text and send_fn is not None:
        unsent = saved.get("unsent") or {}
        if unsent.get("state") == now and now_fn() - float(unsent.get("at") or 0) < RETRY_S:
            return now                                # retried within the hour: wait
        if not send_fn(text):
            state_path.write_text(json.dumps({"known": previous, "unsent": {"state": now, "at": now_fn()}}))
            return now
    if not now.startswith("unknown"):
        state_path.write_text(json.dumps({"known": now}))
    return now


def telegram_send(text, *, send=None):
    """True only when the page was delivered: send_telegram returns (ok, elapsed_ms)."""
    if send is None:
        from sniper.services.memecoin_telegram import send_telegram as send
    ok, _elapsed_ms = send("🟠 " + text)
    return bool(ok)


def main():
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))                 # .env and .fomo.env (the Telegram keys), as the runner
    from sniper.util.clock import real_utc_now_iso
    state = Path(os.getenv("SNIPER_STATE_ROOT") or REPO) / "data" / "kyber_bsc_probe.json"
    now = run(state, send_fn=telegram_send)
    print(json.dumps({"at": real_utc_now_iso(), "kyber_bsc_wbnb": now}))


if __name__ == "__main__":
    main()
