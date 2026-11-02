#!/usr/bin/env python3
"""Verify the frozen chase-cap protocol AND the code that implements it.

Hashing the specification alone was not a freeze. The protocol's meaning lives as much in the
analyzer and its tests as in the prose: the stop count, the terminal backstop, the permutation
unit and the hypothesis family are all code. With only the spec hashed, that code could change
mid-run without a mismatch, which is the failure this file exists to prevent.

The manifest therefore covers three artifacts:

    docs/superpowers/specs/2026-08-19-solana-chase-cap-prospective.md   (frozen section only)
    scripts/research/chase_cap_prospective.py                          (whole file)
    tests/test_chase_cap_prospective_analyzer.py                       (whole file)

A commit cannot name its own hash, so the authority is the content digest, not a commit id. An
early draft of the spec claimed `19393f37`, which predates the protocol entirely.

    python3 scripts/research/verify_chase_cap_freeze.py            # check
    python3 scripts/research/verify_chase_cap_freeze.py --write    # stamp, before the cohort opens

Exit 0 when every digest matches, 1 when any does not. A mismatch after the cohort opens means the
protocol or its implementation changed mid-run and the run is invalid.
"""
import argparse
import hashlib
import json
import pathlib
import re
import sys


SPEC = pathlib.Path("docs/superpowers/specs/2026-08-19-solana-chase-cap-prospective.md")
ANALYZER = pathlib.Path("scripts/research/chase_cap_prospective.py")
TESTS = pathlib.Path("tests/test_chase_cap_prospective_analyzer.py")
MANIFEST = pathlib.Path("docs/superpowers/specs/2026-08-19-solana-chase-cap-freeze.json")
MARKER = "# FROZEN — everything below is fixed before any data is collected"
STAMP = re.compile(r"^FREEZE_DIGEST = ([0-9a-f]{64})$", re.MULTILINE)


def frozen_spec_text(path=SPEC):
    body = path.read_text()
    if MARKER not in body:
        raise SystemExit(f"{path}: FROZEN marker not found")
    # The stamp lives inside the frozen half, so exclude its line from what it certifies.
    return STAMP.sub("", body.split(MARKER, 1)[1]).strip()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digests(spec=SPEC, analyzer=ANALYZER, tests=TESTS):
    return {
        str(spec): _sha(frozen_spec_text(spec)),
        str(analyzer): _sha(analyzer.read_text()),
        str(tests): _sha(tests.read_text()),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=pathlib.Path, default=SPEC)
    ap.add_argument("--analyzer", type=pathlib.Path, default=ANALYZER)
    ap.add_argument("--tests", type=pathlib.Path, default=TESTS)
    ap.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    ap.add_argument("--write", action="store_true",
                    help="stamp the current digests. Legitimate only before the cohort opens")
    args = ap.parse_args()

    current = digests(args.spec, args.analyzer, args.tests)

    if args.write:
        args.manifest.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        body = args.spec.read_text()
        line = f"FREEZE_DIGEST = {_sha(json.dumps(current, sort_keys=True))}"
        body = STAMP.sub(line, body) if STAMP.search(body) else body.rstrip() + f"\n\n{line}\n"
        args.spec.write_text(body)
        # The spec's own digest changed when the stamp line moved, so re-stamp the manifest.
        final = digests(args.spec, args.analyzer, args.tests)
        args.manifest.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
        print(f"stamped {len(final)} artifacts into {args.manifest}")
        for path, value in sorted(final.items()):
            print(f"  {value}  {path}")
        return 0

    if not args.manifest.exists():
        print(f"NO MANIFEST at {args.manifest}. Run with --write before the cohort opens.")
        return 1
    stamped = json.loads(args.manifest.read_text())

    ok = True
    for path in sorted(set(stamped) | set(current)):
        was, now = stamped.get(path), current.get(path)
        if was is None:
            print(f"UNFROZEN   {path}  (present now, absent from the manifest)")
            ok = False
        elif now is None:
            print(f"MISSING    {path}  (in the manifest, absent now)")
            ok = False
        elif was != now:
            print(f"MISMATCH   {path}\n    stamped {was}\n    current {now}")
            ok = False
        else:
            print(f"OK         {path}")
    if not ok:
        print("\nThe frozen protocol or its implementation changed. "
              "If the cohort has opened, the run is invalid.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
