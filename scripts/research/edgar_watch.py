#!/usr/bin/env python3
"""Alert on new SEC EDGAR filings of chosen forms by chosen companies (operator ask, 2026-09-30).

First target: Payward, Inc. (Kraken's parent, CIK 1785695). Its IPO registration was filed
confidentially in 2025-11; the first public S-1 / F-1 on EDGAR is the earliest public sign the
listing is live. 8-A12B (exchange registration) and CERT* (exchange certification) mean days away.

Reads data.sec.gov/submissions (public, one GET per company per run; SEC allows 10 req/s).
State: data/edgar_watch_state.json (seen accession numbers, consecutive failure count).
A failed read is never "no filing": after FAIL_ALERT_AFTER consecutive failures it pages once.

    python3 scripts/research/edgar_watch.py            # one pass, alert on new matches
    python3 scripts/research/edgar_watch.py --dry-run  # print matches, no alert, no state write
    python3 scripts/research/edgar_watch.py --selftest
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

UA = "Sniper-research edgar-watch (personal, low-volume)"
SINCE = "2026-09-30"          # ignore filings older than the watch itself
FAIL_ALERT_AFTER = 3
IPO_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A", "424B1", "424B4", "8-A12B", "CERT")

# name, 10-digit CIK, forms that alert (exact; CERT matches CERT*). Add a line to watch another company.
WATCH = [
    ("Payward, Inc. (Kraken)", "0001785695", IPO_FORMS),
]


def fetch_submissions(cik):
    req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{cik}.json",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def new_matches(data, forms, seen, since=SINCE):
    """(date, form, accession) for unseen recent filings of a watched form."""
    r = data["filings"]["recent"]
    out = []
    for date, form, acc in zip(r["filingDate"], r["form"], r["accessionNumber"]):
        # Exact form, except CERT* (CERTNYS, CERTNAS, ...); a prefix match also caught S-11/F-10.
        if date >= since and acc not in seen and (form in forms or (
                "CERT" in forms and form.startswith("CERT"))):
            out.append((date, form, acc))
    return out


def run(state, fetch=fetch_submissions, alert=None, dry_run=False):
    """One pass. Mutates and returns state; alert(text) is called per new match."""
    seen = set(state.get("seen", []))
    failed = False
    for name, cik, forms in WATCH:
        try:
            matches = new_matches(fetch(cik), forms, seen)
        except Exception as exc:          # unknown, not "nothing filed" (incl. a malformed 200)
            print(f"edgar_watch: read failed for {name}: {exc!r}")
            failed = True
            continue
        for date, form, acc in matches:
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{acc.replace('-', '')}/")
            text = f"\U0001F4C4 EDGAR: {name} filed {form} on {date}\n{url}"
            print(text)
            if dry_run or not alert or alert(text):
                seen.add(acc)
            else:                         # undelivered: retry next run, counts toward the page
                print(f"edgar_watch: alert not delivered for {acc}")
                failed = True
    fails = state.get("fails", 0) + 1 if failed else 0
    if failed and fails == FAIL_ALERT_AFTER and not dry_run and alert:
        alert(f"⚠️ edgar_watch: {fails} consecutive failed reads; filings are UNKNOWN")
    return {"seen": sorted(seen), "fails": fails}


def selftest():
    data = {"filings": {"recent": {
        "filingDate": ["2026-10-02", "2026-10-01", "2026-08-04", "2026-10-03"],
        "form": ["S-1", "D", "S-1", "S-1/A"],
        "accessionNumber": ["a1", "a2", "old", "a3"]}}}
    sent = []
    ok = lambda t: sent.append(t) or True        # a delivered send
    st = run({}, fetch=lambda c: data, alert=ok)
    assert [s.split(" filed ")[1][:5] for s in sent] == ["S-1 o", "S-1/A"], sent
    assert st == {"seen": ["a1", "a3"], "fails": 0}
    sent.clear()
    assert run(st, fetch=lambda c: data, alert=ok)["seen"] == ["a1", "a3"] and not sent

    def boom(_c):
        raise OSError("down")
    st2 = {"seen": [], "fails": 0}
    for i in range(1, 5):
        st2 = run(st2, fetch=boom, alert=ok)
        assert st2["fails"] == i
    assert len(sent) == 1 and "UNKNOWN" in sent[0], sent   # pages once, at the threshold
    assert run(st2, fetch=lambda c: data, alert=lambda t: True)["fails"] == 0
    # An undelivered alert is not seen: it retries next run and counts as a failure.
    st3 = run({}, fetch=lambda c: data, alert=lambda t: False)
    assert st3 == {"seen": [], "fails": 1}, st3
    # A malformed 200 counts as a failed read, not a crash before the state write.
    assert run({}, fetch=lambda c: {"oops": 1}, alert=lambda t: True)["fails"] == 1
    # Exact forms: S-11 / F-10 do not alert; CERTNYS does.
    odd = {"filings": {"recent": {"filingDate": ["2026-10-02"] * 3,
                                  "form": ["S-11", "F-10", "CERTNYS"],
                                  "accessionNumber": ["b1", "b2", "b3"]}}}
    assert [m[2] for m in new_matches(odd, IPO_FORMS, set())] == ["b3"]
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--state", default=os.path.join(ROOT, "data", "edgar_watch_state.json"))
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    load_env_files(ROOT)
    from sniper.services.memecoin_telegram import send_telegram
    try:
        with open(args.state) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {}
    state = run(state, alert=lambda t: send_telegram(t)[0], dry_run=args.dry_run)
    if not args.dry_run:
        tmp = args.state + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, args.state)
    return 1 if state["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
