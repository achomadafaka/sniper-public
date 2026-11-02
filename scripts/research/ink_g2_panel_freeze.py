#!/usr/bin/env python3
"""Freeze the Ink G2 observation panel from a finished source census (Beta, 2026-09-30).

The panel is an OBSERVATION-ONLY list of senders (tx.from) the G2 shadow's canonical backfill reads
for ground truth (MEMECOIN_INK_OBSERVATION_PANEL, 1589c08a9); the funded-ledger projection never
sees it. Rule, from docs/superpowers/research/2026-09-30-ink-g2-shadow-preregistration.md R2:
  - every owner in the census report's top_realized list (top 20 by realized ETH PnL, >= 5 trades,
    >= 3 tokens), plus
  - every owner in its most_active list (>= 3 active UTC days, relayer-like senders excluded) whose
    custody label is 'self'.
Sample check: per FULL UTC day inside the census window, the distinct meme-swap transactions sent by
panel owners (txs.jsonl holds exactly the census's meme-swap txs). The G2 unit is any successful
non-deposit transaction by a panel sender, so this count is a lower bound. The file is refused
unless the census is complete (log phase done, report window == [lo, head], zero unread txs) and
every full day reaches --min-daily (G2 needs >= 30 eligible transactions in 24 h).

    python3 scripts/research/ink_g2_panel_freeze.py --census <dir> --out <panel.json>
    python3 scripts/research/ink_g2_panel_freeze.py --selftest
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400


def _jsonl(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def select(report):
    """(owners in order, members) from a census report.json. Top-realized first, then the most
    active self-custody owners not already listed."""
    members, seen = [], set()
    for group, rows, keep in (("top_realized", report["top_realized"], lambda r: True),
                              ("most_active_self", report["most_active"],
                               lambda r: r.get("custody") == "self")):
        for row in rows:
            owner = row["owner"].lower()
            if owner in seen or not keep(row):
                continue
            seen.add(owner)
            members.append({"owner": owner, "group": group, **{k: row.get(k) for k in (
                "custody", "realized_eth", "trades", "tokens", "active_days")}})
    return [m["owner"] for m in members], members


def full_day_counts(meta, txs, owners):
    """{UTC day: distinct panel txs} for every day wholly inside [lo, head]. Block times use the
    census's one anchor (Ink blocks are 1.00 s): ts = head_ts - (head - block)."""
    lo_ts = meta["head_ts"] - (meta["head"] - meta["lo"])
    first = -(-lo_ts // DAY) * DAY                                 # first midnight >= lo_ts
    days = {}
    for start in range(first, meta["head_ts"] + 1 - DAY + 1, DAY):  # day end <= head_ts + 1
        days[start] = set()
    owners = set(owners)
    for t in txs:
        if t.get("from") in owners and "ts" in t:
            start = t["ts"] // DAY * DAY
            if start in days:
                days[start].add(t["tx"])
    return {datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d"): len(v)
            for s, v in sorted(days.items())}


def freeze(census, min_daily):
    census = Path(census)
    meta = json.loads((census / "meta.json").read_text())
    if meta.get("done_to") != meta.get("head"):
        raise SystemExit("census fetch not finished: done_to %s head %s" % (
            meta.get("done_to"), meta.get("head")))
    raw = (census / "report.json").read_bytes()
    report = json.loads(raw)
    # done_to == head ends only the LOG phase; tx lookups and the report come after (Gamma MEDIUM,
    # 2026-09-30). The report must cover the whole window and every meme-swap tx must be read.
    if report.get("window_blocks") != [meta["lo"], meta["head"]]:
        raise SystemExit("census report is stale: window %s, fetch %s" % (
            report.get("window_blocks"), [meta["lo"], meta["head"]]))
    # txs_unread is always written; excluded_tx_unread comes from a Counter that omits a zero, so
    # it is absent exactly when nothing was excluded (09-30 census: txs_unread 0, key absent).
    counts = report.get("counts") or {}
    for key, value in (("txs_unread", counts.get("txs_unread")),
                       ("excluded_tx_unread", counts.get("excluded_tx_unread", 0))):
        if value != 0:
            raise SystemExit("census has unread txs: %s=%s" % (key, value))
    owners, members = select(report)
    daily = full_day_counts(meta, _jsonl(census / "txs.jsonl"), owners)
    if not owners or not daily:
        raise SystemExit("empty panel or no full day in the window")
    low = min(daily.values())
    if low < min_daily:
        raise SystemExit("panel too thin: a full day has %d panel txs < %d; raise the report's "
                         "--active and rerun it" % (low, min_daily))
    return {
        "owners": owners,
        "source": "ink_source_census %s report.json sha256 %s, blocks %d-%d" % (
            census, hashlib.sha256(raw).hexdigest(), meta["lo"], meta["head"]),
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": "observation_only_never_projected",
        "rule": "census top_realized + most_active with custody=self (prereg R2)",
        "min_daily_required": min_daily,
        "daily_panel_txs_full_days": daily,
        "members": members,
    }


def selftest():
    import tempfile
    a, b, c, d = ("0x" + ch * 40 for ch in "abcd")
    report = {"window_blocks": [10 ** 6 - 2 * DAY, 10 ** 6],
              "counts": {"excluded_tx_unread": 0, "txs_unread": 0},
              "top_realized": [{"owner": a, "custody": "contract"}, {"owner": b, "custody": "self"}],
              "most_active": [{"owner": b, "custody": "self"}, {"owner": c, "custody": "mixed"},
                              {"owner": d.upper().replace("0X", "0x"), "custody": "self"}]}
    owners, members = select(report)
    # top_realized kept whatever the custody; most_active only 'self'; b not listed twice
    assert owners == [a, b, d], owners
    assert [m["group"] for m in members] == ["top_realized", "top_realized", "most_active_self"]
    # window: lo_ts = 2026-09-28T12:00Z, head_ts = 2026-09-30T12:00Z -> one full day, 09-29
    head_ts = int(datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp())
    meta = {"head": 10 ** 6, "head_ts": head_ts, "lo": 10 ** 6 - 2 * DAY, "done_to": 10 ** 6}
    d29 = int(datetime(2026, 9, 29, tzinfo=timezone.utc).timestamp())
    txs = [{"tx": "t1", "from": a, "ts": d29 + 5}, {"tx": "t1", "from": a, "ts": d29 + 5},
           {"tx": "t2", "from": b, "ts": d29 + DAY - 1}, {"tx": "t3", "from": c, "ts": d29 + 9},
           {"tx": "t4", "from": a, "ts": d29 - 1}, {"tx": "t5", "from": a, "ts": d29 + DAY},
           {"tx": "t6", "error": "unread"}]
    # distinct txs only; c is not a panel owner; partial days 09-28 and 09-30 are not counted
    assert full_day_counts(meta, txs, owners) == {"2026-09-29": 2}, full_day_counts(meta, txs, owners)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "meta.json").write_text(json.dumps(meta))
        (out / "report.json").write_text(json.dumps(report))
        (out / "txs.jsonl").write_text("".join(json.dumps(t) + "\n" for t in txs))
        panel = freeze(out, 2)
        assert panel["owners"] == [a, b, d] and panel["daily_panel_txs_full_days"] == {"2026-09-29": 2}
        try:
            freeze(out, 3)
        except SystemExit as exc:
            assert "panel too thin" in str(exc)
        else:
            raise AssertionError("a thin panel must be refused")
        # a zero excluded_tx_unread is omitted by the census's Counter: accepted
        (out / "report.json").write_text(json.dumps(dict(report, counts={"txs_unread": 0})))
        assert freeze(out, 2)["owners"] == [a, b, d]
        # refused: an unfinished log phase, a stale report window, any unread tx
        for bad_meta, bad_report, why in (
                (dict(meta, done_to=meta["head"] - 1), report, "not finished"),
                (meta, dict(report, window_blocks=[meta["lo"], meta["head"] - 5]), "stale"),
                (meta, dict(report, counts={"excluded_tx_unread": 3, "txs_unread": 0}),
                 "excluded_tx_unread=3"),
                (meta, dict(report, counts={"excluded_tx_unread": 0, "txs_unread": 1}),
                 "txs_unread=1"),
                (meta, dict(report, counts={}), "txs_unread=None")):
            (out / "meta.json").write_text(json.dumps(bad_meta))
            (out / "report.json").write_text(json.dumps(bad_report))
            try:
                freeze(out, 1)
            except SystemExit as exc:
                assert why in str(exc), (why, str(exc))
            else:
                raise AssertionError("an incomplete census must be refused: " + why)
    # the file the shadow loads accepts what freeze writes
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.research.onchain_ink_watch import load_panel
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "panel.json"
        path.write_text(json.dumps(panel))
        assert load_panel(path)["owners"] == frozenset(owners)
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--census")
    ap.add_argument("--out")
    ap.add_argument("--min-daily", type=int, default=30)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not (args.census and args.out):
        ap.error("--census and --out are required")
    panel = freeze(args.census, args.min_daily)
    Path(args.out).write_text(json.dumps(panel, indent=1) + "\n")
    print(json.dumps({"owners": len(panel["owners"]),
                      "daily_panel_txs_full_days": panel["daily_panel_txs_full_days"]}))


if __name__ == "__main__":
    sys.exit(main())
