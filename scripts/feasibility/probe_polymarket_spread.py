"""Sample bid-ask spread on 10 live tennis markets.

Writes results as JSONL to stdout. For each market:
  - condition_id, title, best_bid, best_ask, spread_abs, spread_pct, mid
"""
import json
import sys
import time

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


def fetch_active_tennis_events(target=25, page_size=200, max_pages=10):
    """Fetch active tennis events and pick the H2H moneyline market per event.

    On Polymarket tennis events, the H2H match-winner market has
    `market.question == event.title` (e.g. "Madrid Open: Zhang vs Kopriva").
    All other markets under the event are set/game/total derivatives.

    Pages through Gamma /events until we collect `target` tennis H2H events
    or exhaust `max_pages`.
    """
    out = []
    offset = 0
    for _ in range(max_pages):
        r = requests.get(
            GAMMA,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": page_size,
                "offset": offset,
            },
            timeout=20,
        )
        r.raise_for_status()
        events = r.json()
        if not events:
            break
        for e in events:
            tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
                    for t in (e.get("tags") or [])]
            if "tennis" not in tags:
                continue
            etitle = (e.get("title") or "").strip()
            if " vs " not in etitle.lower():
                continue  # Not an H2H event (e.g. outright winner futures)
            for m in (e.get("markets") or []):
                cid = m.get("conditionId")
                if cid and (m.get("question") or "").strip() == etitle:
                    try:
                        token_ids = json.loads(m.get("clobTokenIds", "[]"))
                    except Exception:
                        continue
                    if token_ids:
                        out.append({
                            "condition_id": cid,
                            "title": etitle,
                            "token_id": token_ids[0],
                            "event_end_date": e.get("endDate"),
                        })
                    break
            if len(out) >= target:
                return out
        if len(events) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    return out


def fetch_book(token_id):
    try:
        r = requests.get(CLOB_BOOK, params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def main():
    markets = fetch_active_tennis_events(target=25)
    print(f"[probe] collected {len(markets)} tennis H2H events", file=sys.stderr)
    for m in markets:
        book = fetch_book(m["token_id"])
        bids = book.get("bids", []) if isinstance(book, dict) else []
        asks = book.get("asks", []) if isinstance(book, dict) else []
        # CLOB /book returns price levels; "best bid" is the HIGHEST bid price
        # and "best ask" is the LOWEST ask price. Sort to be safe.
        bid_prices = sorted([float(b["price"]) for b in bids if b.get("price")], reverse=True)
        ask_prices = sorted([float(a["price"]) for a in asks if a.get("price")])
        best_bid = bid_prices[0] if bid_prices else None
        best_ask = ask_prices[0] if ask_prices else None
        has_both = best_bid is not None and best_ask is not None
        spread_abs = (best_ask - best_bid) if has_both else None
        mid = ((best_bid + best_ask) / 2) if has_both else None
        spread_pct = (spread_abs / mid * 100) if (spread_abs is not None and mid) else None
        print(json.dumps({
            "condition_id": m["condition_id"],
            "title": m["title"],
            "event_end_date": m.get("event_end_date"),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_abs": spread_abs,
            "spread_pct": spread_pct,
            "mid": mid,
            "empty_side": None if has_both else ("bid" if best_bid is None else "ask"),
        }))
        time.sleep(0.2)


if __name__ == "__main__":
    main()
