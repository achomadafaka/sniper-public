#!/usr/bin/env python3
"""Measurement-only tap on the J7Tracker Socket.IO feed.

WHY THIS EXISTS. fomo refused our third account on 2026-09-09. J7Tracker relays fomo trades and
theses for up to 50 tracked fomo users through ITS accounts, over the same Socket.IO feed its web
app reads. Before a feed adapter can be designed, one real relayed fomo trade must be observed:
nobody has seen the payload shape yet, and a normalizer written to a guessed shape is the
"answer key nobody checked" failure. This script records what arrives. It opens no position,
touches no ledger, and holds no fomo credential.

WHAT IT DOES
  1. Reads `.j7.env` (operator-written, gitignored): J7_TOKEN (the JWT the web app stores after
     login; 15-day TTL, login is Turnstile-gated so it cannot be minted here). J7_USERNAME and
     J7_WATCH (comma-separated handles) are optional.
  2. Connects to https://nyc.j7tracker.io with browser-shaped headers (J7's edge 403s bare
     clients), auth {token}, websocket transport, and emits `user_connected <username>` exactly
     as the web client does on connect.
  3. Appends one JSON line per event to data/j7_tap.jsonl, tagged src=feed|wallets:
       - full payload for every non-tweet event, and for any tweet whose JSON mentions fomo,
         thesis, or a handle listed in J7_WATCH (comma-separated, optional);
       - a compact stub for every other tweet (id, handle, provider, type, createdAt) so an
         unknown carrier event can still be found by elimination.
  4. Logs connect / auth_error / disconnect with wall-clock stamps to stderr.

RUN
  python3 scripts/j7_socket_tap.py                 # foreground
  nohup python3 scripts/j7_socket_tap.py >> logs/j7_tap.log 2>&1 &
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import socketio

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(REPO, ".j7.env")
OUT = os.path.join(REPO, "data", "j7_tap.jsonl")
FEED_URL = "https://nyc.j7tracker.io"
WALLETS_URL = "https://nj.j7tracker.io"
WALLETS_PATH = "/wallets/socket.io"
# What the web app sends. J7's edge refuses requests that do not look like the site.
HEADERS = {
    "Origin": "https://j7tracker.io",
    "Referer": "https://j7tracker.io/",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
}
STUB_ONLY = {"tweet", "tweet_update", "quoted_tweet", "reply_tweet"}
NOISE = {"ai_suggestion", "ai_suggestion_update", "connected_users", "pnl_update",
         "custom_accounts_available_list"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_env(path):
    env = {}
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
                if m:
                    env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _interesting(pattern, ev, payload_json):
    if ev not in STUB_ONLY:
        return True
    return bool(pattern.search(payload_json))


def _stub(payload):
    a = (payload or {}).get("author") or {}
    return {"id": payload.get("id"), "handle": a.get("handle"), "provider": payload.get("provider"),
            "type": payload.get("type"), "createdAt": payload.get("createdAt")}


def main():
    env = _load_env(ENV_FILE)
    token, user = env.get("J7_TOKEN"), env.get("J7_USERNAME")
    if not token:
        print("REFUSED: %s needs J7_TOKEN" % ENV_FILE, file=sys.stderr)
        return 2
    watch = [h.strip().lower() for h in env.get("J7_WATCH", "").split(",") if h.strip()]
    # Word-bounded, and handles only where a handle can appear. The first run matched `origin`,
    # `change`, and `koy` inside ordinary tweet text and stored nearly every tweet in full.
    alts = [r"\bfomo\b", r"\bthesis\b"]
    if watch:
        joined = "|".join(re.escape(h) for h in watch)
        alts += [r"@(?:%s)\b" % joined, r'"(?:handle|username|userHandle)"\s*:\s*"(?:%s)"' % joined]
    pattern = re.compile("|".join(alts), re.I)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = open(OUT, "a", buffering=1)
    counts = {}

    def make_client(label, *, emit_user_connected):
        sio = socketio.Client(reconnection=True, reconnection_delay=5, reconnection_delay_max=120,
                              logger=False, engineio_logger=False)

        @sio.event
        def connect():
            print("%s [%s] connected sid=%s" % (_now(), label, sio.sid), file=sys.stderr)
            if emit_user_connected:
                sio.emit("user_connected", token)

        @sio.event
        def connect_error(data):
            print("%s [%s] connect_error %s" % (_now(), label, str(data)[:300]), file=sys.stderr)

        @sio.event
        def disconnect():
            print("%s [%s] disconnected" % (_now(), label), file=sys.stderr)

        @sio.on("auth_error")
        def on_auth_error(data=None):
            print("%s [%s] AUTH_ERROR %s -- the JWT in .j7.env is refused or expired"
                  % (_now(), label, str(data)[:300]), file=sys.stderr)

        @sio.on("*")
        def on_any(ev, *args):
            payload = args[0] if args else None
            key = label + ":" + ev
            counts[key] = counts.get(key, 0) + 1
            if ev in NOISE:
                return
            pj = json.dumps(payload, ensure_ascii=False) if payload is not None else "null"
            full = label == "wallets" or _interesting(pattern, ev, pj)
            row = {"recv": _now(), "src": label, "ev": ev,
                   "payload": payload if full else _stub(payload if isinstance(payload, dict) else {}),
                   "full": full}
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            if label == "wallets":
                print("%s [wallets] %s %s" % (_now(), ev, pj[:300]), file=sys.stderr)
            elif full and ev in STUB_ONLY:
                print("%s [feed] MATCH %s %s" % (_now(), ev, pj[:200]), file=sys.stderr)
            elif ev not in STUB_ONLY:
                print("%s [feed] %s keys=%s" % (_now(), ev, list(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__),
                      file=sys.stderr)
        return sio

    feed = make_client("feed", emit_user_connected=True)
    wallets = make_client("wallets", emit_user_connected=False)
    for label, sio, url, path in (("feed", feed, FEED_URL, "socket.io"),
                                  ("wallets", wallets, WALLETS_URL, WALLETS_PATH.strip("/"))):
        try:
            sio.connect(url, socketio_path=path, transports=["websocket"], headers=HEADERS,
                        auth={"token": token}, wait_timeout=20)
        except socketio.exceptions.ConnectionError as exc:
            # Probed 2026-09-09 with a deliberately bad token: the edge admits the browser-shaped
            # handshake and the server answers `connect_error {'message': 'Invalid token'}`. So a
            # failure HERE is the JWT (expired or mis-copied), not the transport.
            print("REFUSED: J7 %s did not accept the session: %s -- re-export J7_TOKEN from the browser"
                  % (label, str(exc)[:200]), file=sys.stderr)
            return 3
    last = time.time()
    try:
        while True:
            feed.sleep(30)
            if time.time() - last >= 600:
                print("%s alive feed=%s wallets=%s counts=%s"
                      % (_now(), feed.connected, wallets.connected, counts), file=sys.stderr)
                last = time.time()
    except KeyboardInterrupt:
        feed.disconnect()
        wallets.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
