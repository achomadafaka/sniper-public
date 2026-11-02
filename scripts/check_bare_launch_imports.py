"""Reproduce a BARE launch's import condition without running the script's work.

`python3 scripts/research/X.py` puts scripts/research on sys.path[0], not the repo root. This execs
only the file's header, up to and including its first `sniper.` import, with sys.path[0] set the
same way. It cannot run the script's side effects -- onchain_wallet_watch writes to the funded
ledger and is a live lane, so actually launching it is not an option.
"""
import pathlib, subprocess, sys

HARNESS = '''
import sys, pathlib
f = pathlib.Path(%r).resolve()
sys.path[0] = str(f.parent)          # exactly what a bare launch gives you
import ast
text = f.read_text()
src = text.splitlines(keepends=True)
# Cut at the FIRST MODULE-LEVEL sniper import -- the one a bare launch actually hits. Cutting at
# the last one drags in half the file and can slice a multi-line statement, which surfaced as a
# bogus SyntaxError on cache_hl_altshort_history.py rather than the real import question.
tree = ast.parse(text)
cut = None
for n in tree.body:
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        m = getattr(n, "module", "") or " ".join(a.name for a in n.names)
        if str(m).startswith("sniper"):
            cut = (n.end_lineno or n.lineno)
            break
if cut is None:
    print("OK (no module-level sniper import)"); raise SystemExit(0)
g = {"__file__": str(f), "__name__": "__bare_check__"}
exec("".join(src[:cut]), g)
print("OK")
'''

fail = 0
for name in sys.argv[1:]:
    p = pathlib.Path(name)
    r = subprocess.run([sys.executable, "-c", HARNESS % str(p)],
                       capture_output=True, text=True, cwd=".", timeout=60)
    ok = r.returncode == 0 and "OK" in r.stdout
    err = (r.stderr.strip().splitlines() or [""])[-1][:90]
    print(f"  {'PASS' if ok else 'FAIL'}  {p.name:36s} {'' if ok else err}")
    fail += 0 if ok else 1
sys.exit(1 if fail else 0)
