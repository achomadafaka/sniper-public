#!/usr/bin/env python3
"""
fomo_latency_probe.py — measure how early we can catch fomo.family trade events.

WHAT THIS ANSWERS
-----------------
For the memecoin copy-mirror design: how many seconds after a fomo trader's
trade does each fomo API surface expose it, and which surface is earliest?
This is the empirical "is instant-copying via fomo fast enough" measurement.
It does NOT place any trade. Read-only polling + a CSV of arrival latencies.

BACKGROUND (verified 2026-07-05 by live inspection)
----------------------------------------------------
- fomo has NO public websocket. The web app polls REST at ~2.5s.
- Surfaces, earliest -> latest: /v2/users/{id}/swaps (per-trader) ~200ms before
  /feed (global), both before the mobile push. This probe polls faster than 2.5s
  to find the true floor and compares the two surfaces on the same trades.
- Auth = Privy. fomo API wants `Authorization: Bearer <privy_access_token>`
  (a ~413-char JWT). That token expires (~1h) and is refreshed via Privy, which
  ROTATES the refresh token on every refresh. This script runs that loop.

CREDENTIALS — you provide these; they never get printed or committed
--------------------------------------------------------------------
From your logged-in fomo browser tab (DevTools > Application > Local Storage,
and DevTools > Network > any prod-api.fomo.family request):

  FOMO_REFRESH_TOKEN  -> localStorage key `privy:refresh_token` (strip the quotes)
  FOMO_ACCESS_TOKEN   -> the `Authorization: Bearer ...` value on any live
                         prod-api.fomo.family request (Network tab). Optional but
                         recommended for a clean first refresh.
  FOMO_CA_ID          -> localStorage key `privy:caid` (strip the quotes). Optional.

Set them in your shell (NOT in a committed file):
  export FOMO_REFRESH_TOKEN='...'
  export FOMO_ACCESS_TOKEN='...'
  export FOMO_CA_ID='...'
Optional:
  export FOMO_PROBE_TRADER='<userId>'   # also poll this trader's /swaps to compare surfaces
  export FOMO_POLL_MS=500               # feed poll interval (default 500ms)
  export FOMO_PROBE_SECONDS=600         # run duration (default 600s; 0 = forever)

The rotated refresh token is persisted to .fomo_tokens.json (gitignored) so you
can restart without re-grabbing it.

USAGE
-----
  python3 scripts/fomo_latency_probe.py
  # -> writes fomo_latency_<epoch>.csv and prints a live latency summary

NOTE: app-id / client / client-id below are from the fomo web app build seen on
2026-07-05. If refresh starts 401ing after a fomo update, re-capture them from a
live auth.privy.io/api/v1/sessions request.
"""
import os
import sys
import csv
import json
import time
import statistics
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- fomo/Privy app constants (config, not secrets; may change on app updates) ---
PRIVY_APP_ID = "cm6h485o300n3zj9yl6vpedq7"
PRIVY_CLIENT = "react-auth:3.21.3"
PRIVY_CLIENT_ID = "client-WY5gFSayQjxnQhG4rP6SnwPAyPZWZpNRhJ6b9rzMnYwqH"
PRIVY_SESSIONS_URL = "https://auth.privy.io/api/v1/sessions"
FOMO_API = "https://prod-api.fomo.family"

# the same feedTypes the web app subscribes to
FEED_TYPES = [
    "large_buy", "large_sell", "large_transfer_in", "large_transfer_out",
    "single_user_transfer_out", "single_user_sell", "thesis_created",
    "multi_user_buy", "multi_user_sell", "new_token_listing",
    "price_since_listing", "user_trade_profit_milestone",
    "user_with_smart_following", "manual",
]

TOKEN_STORE = os.path.join(os.path.dirname(__file__), os.pardir, ".fomo_tokens.json")
ENV_FILE = os.path.join(os.path.dirname(__file__), os.pardir, ".fomo.env")


def _load_env_file():
    """Load KEY=VALUE lines from gitignored .fomo.env into os.environ (does not override real env)."""
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and v and not os.environ.get(k):
                    os.environ[k] = v
    except Exception as e:
        print(f"[warn] could not read {ENV_FILE}: {e}", file=sys.stderr)


def _mask(tok):
    """Never log a token; only its shape."""
    if not tok:
        return "<none>"
    return f"<jwt len={len(tok)} ...{tok[-4:]}>" if len(tok) > 12 else "<short>"


def _http_json(method, url, headers, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        server_date = r.headers.get("Date")  # fomo/Privy server clock, skew-free reference
        return r.status, json.loads(raw) if raw else {}, server_date


def _parse_http_date(s):
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class Auth:
    """Holds the fomo access token; refreshes via Privy, rotating the refresh token."""

    def __init__(self):
        _load_env_file()
        self.access = os.environ.get("FOMO_ACCESS_TOKEN", "").strip()
        self.refresh = os.environ.get("FOMO_REFRESH_TOKEN", "").strip()
        self.ca_id = os.environ.get("FOMO_CA_ID", "").strip()
        # a persisted rotated refresh token wins over the env one (it's newer)
        if os.path.exists(TOKEN_STORE):
            try:
                with open(TOKEN_STORE) as f:
                    saved = json.load(f)
                if saved.get("refresh"):
                    self.refresh = saved["refresh"]
                if saved.get("access") and not self.access:
                    self.access = saved["access"]
                if saved.get("ca_id") and not self.ca_id:
                    self.ca_id = saved["ca_id"]
            except Exception:
                pass
        if not self.refresh:
            sys.exit("FOMO_REFRESH_TOKEN is required (see the header of this script).")

    def _persist(self):
        try:
            with open(TOKEN_STORE, "w") as f:
                json.dump({"refresh": self.refresh, "access": self.access, "ca_id": self.ca_id}, f)
            os.chmod(TOKEN_STORE, 0o600)
        except Exception as e:
            print(f"[warn] could not persist token store: {e}", file=sys.stderr)

    def refresh_token(self):
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "privy-app-id": PRIVY_APP_ID,
            "privy-client": PRIVY_CLIENT,
            "privy-client-id": PRIVY_CLIENT_ID,
        }
        if self.ca_id:
            headers["privy-ca-id"] = self.ca_id
        if self.access:
            headers["authorization"] = "Bearer " + self.access
        try:
            status, j, _ = _http_json("POST", PRIVY_SESSIONS_URL, headers,
                                      body={"refresh_token": self.refresh})
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            sys.exit(f"[auth] Privy refresh failed {e.code}: {body}\n"
                     f"       Re-grab FOMO_REFRESH_TOKEN/FOMO_ACCESS_TOKEN from the browser, "
                     f"or the app constants changed.")
        new_access = j.get("privy_access_token") or j.get("token")
        if not new_access:
            sys.exit(f"[auth] refresh response missing access token; keys={list(j)}")
        self.access = new_access
        if j.get("refresh_token"):
            self.refresh = j["refresh_token"]  # Privy rotates it
        self._persist()
        print(f"[auth] refreshed access token {_mask(self.access)}")
        return self.access


# fomo's edge (WAF) 403s requests that don't look like the web app; mimic its browser headers.
BROWSER_HEADERS = {
    "accept": "application/json",
    "origin": "https://fomo.family",
    "referer": "https://fomo.family/",
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "x-supported-chains": "1,56,143,8453,1399811149",
}


def fomo_get(auth, path, retry=True):
    headers = dict(BROWSER_HEADERS)
    headers["authorization"] = "Bearer " + (auth.access or "x")
    try:
        return _http_json("GET", FOMO_API + path, headers)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            auth.refresh_token()
            return fomo_get(auth, path, retry=False)
        raise


def build_feed_path(limit=50):
    qs = "&".join(["limit=%d" % limit] + ["feedTypes=%s" % t for t in FEED_TYPES])
    return "/feed?" + qs


def event_key(ev):
    return ev.get("id") or ev.get("tradeId") or json.dumps(ev, sort_keys=True)[:64]


def summarize(ev, surface, recv_local, server_epoch):
    body = ev.get("body") or {}
    created = _iso_to_epoch(ev.get("createdAt"))
    # skew-free lag: fomo server clock (Date header) minus event createdAt
    lag_server = (server_epoch - created) if (server_epoch and created) else None
    # local lag (subject to your machine's clock offset — reported but not trusted)
    lag_local = (recv_local - created) if created else None
    return {
        "recv_iso": datetime.fromtimestamp(recv_local, timezone.utc).isoformat(),
        "created_iso": ev.get("createdAt"),
        "lag_server_s": round(lag_server, 3) if lag_server is not None else "",
        "lag_local_s": round(lag_local, 3) if lag_local is not None else "",
        "surface": surface,
        "type": ev.get("type"),
        "handle": body.get("userHandle") or body.get("displayName"),
        "mint": ev.get("tokenAddress"),
        "size_usd": body.get("currentSizeUsd") or body.get("inHumanAmount"),
        "is_first_buy": body.get("isFirstBuy"),
    }


def main():
    poll_ms = int(os.environ.get("FOMO_POLL_MS", "500"))
    run_secs = int(os.environ.get("FOMO_PROBE_SECONDS", "600"))
    trader = os.environ.get("FOMO_PROBE_TRADER", "").strip()

    auth = Auth()
    if not auth.access:
        auth.refresh_token()

    out_path = "fomo_latency_%d.csv" % int(time.time())
    cols = ["recv_iso", "created_iso", "lag_server_s", "lag_local_s", "surface",
            "type", "handle", "mint", "size_usd", "is_first_buy"]
    fh = open(out_path, "w", newline="")
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()

    feed_path = build_feed_path()
    swaps_path = "/v2/users/%s/swaps" % trader if trader else None

    print(f"[probe] polling /feed every {poll_ms}ms"
          + (f" + trader {trader[:8]}… /swaps" if trader else "")
          + f" for {run_secs}s -> {out_path}")
    print(f"[probe] lag_server_s = fomo Date header minus event createdAt (clock-skew-free); "
          f"lag_local_s uses your machine clock (may be offset).")

    seen = set()
    lags = []
    first_surface = {}  # key -> surface that saw it first (for /feed vs /swaps delta)
    start = time.time()
    next_summary = start + 15

    while run_secs == 0 or time.time() - start < run_secs:
        cycle = time.time()
        for surface, path in (("feed", feed_path), ("swaps", swaps_path)):
            if not path:
                continue
            try:
                status, j, sdate = fomo_get(auth, path)
            except urllib.error.HTTPError as e:
                print(f"[warn] {surface} HTTP {e.code}", file=sys.stderr)
                continue
            except Exception as e:
                print(f"[warn] {surface} {e}", file=sys.stderr)
                continue
            recv_local = time.time()
            server_epoch = _parse_http_date(sdate)
            ro = j.get("responseObject") or j
            events = ro.get("feed") if isinstance(ro, dict) and "feed" in ro else (
                ro.get("swaps") if isinstance(ro, dict) and "swaps" in ro else [])
            for ev in (events or []):
                # /swaps records use a different shape; skip if no createdAt/type
                if "createdAt" not in ev:
                    continue
                k = surface + ":" + event_key(ev)
                if k in seen:
                    continue
                seen.add(k)
                first_surface.setdefault(event_key(ev), surface)
                row = summarize(ev, surface, recv_local, server_epoch)
                w.writerow(row)
                if isinstance(row["lag_server_s"], (int, float)):
                    lags.append(row["lag_server_s"])
        fh.flush()

        if time.time() >= next_summary:
            next_summary = time.time() + 15
            if lags:
                recent = [x for x in lags if 0 <= x < 3600]  # drop clock-skew nonsense
                if recent:
                    print(f"[stat] events={len(lags)}  "
                          f"lag_server_s min={min(recent):.2f} "
                          f"median={statistics.median(recent):.2f} "
                          f"p90={sorted(recent)[int(len(recent)*0.9)-1]:.2f}")
                else:
                    print(f"[stat] events={len(lags)} but all lags look clock-skewed "
                          f"(check machine clock vs UTC)")
            else:
                print("[stat] no events yet")

        elapsed = time.time() - cycle
        time.sleep(max(0, poll_ms / 1000.0 - elapsed))

    fh.close()
    print(f"[done] wrote {out_path}")
    if lags:
        recent = [x for x in lags if 0 <= x < 3600]
        if recent:
            print(f"[done] {len(recent)} events; earliest observed lag {min(recent):.2f}s, "
                  f"median {statistics.median(recent):.2f}s")
    # /feed vs /swaps surface-lead tally (skew-free, same clock)
    if swaps_path:
        leads = {}
        for k, s in first_surface.items():
            leads[s] = leads.get(s, 0) + 1
        print(f"[done] surface that saw a trade first: {leads} "
              f"(swaps leading feed = per-trader endpoint is earlier)")


if __name__ == "__main__":
    main()
