"""One write() per print() in the runner and the armed watchers (2026-10-07, operator order).

CPython's print() writes the text and `end` in two calls; under `python -u` each is its own
write(2), so a second thread appending to the same file between them joins two lines (runner log
2026-10-07 15:56:33Z `-> retry2026-...`, 20:16:18Z `live_cap_per_hour=1000[real] ...`).
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY_SCRIPTS = ("scripts/memecoin_runner_loop.py",
                 "scripts/research/onchain_evm_wallet_watch.py",
                 "scripts/research/onchain_wallet_watch.py")

_THREADS = r'''
import sys, threading
sys.path.insert(0, sys.argv[1])
from sniper.util.line_print import install
install()
def run(tag, log=print):           # a `log=print` default bound after install, as the services do
    for i in range(20000):
        log("%s %06d %s" % (tag, i, "x" * 40), flush=True)
threads = [threading.Thread(target=run, args=(t,)) for t in "ABCD"]
[t.start() for t in threads]
[t.join() for t in threads]
'''


def test_threads_printing_to_one_append_file_never_join_lines(tmp_path):
    out = tmp_path / "runner.log"
    with open(out, "a") as fh:            # O_APPEND, as the watchdog's `>> "$LOGFILE" 2>&1`
        subprocess.run([sys.executable, "-u", "-c", _THREADS, str(REPO)], stdout=fh,
                       stderr=subprocess.STDOUT, check=True, timeout=120)
    lines = out.read_text().split("\n")
    assert lines[-1] == ""
    bad = [x for x in lines[:-1] if not re.fullmatch(r"[ABCD] \d{6} x{40}", x)]
    assert bad == [] and len(lines) - 1 == 80000, bad[:3]


def test_line_print_matches_builtin_print_bytes(capsys):
    from sniper.util.line_print import line_print
    for args, kw in (((), {}), (("a", 1, None), {}), (("a", "b"), {"sep": None, "end": None}),
                     (("a", "b"), {"sep": "", "end": ""}), ((1.5,), {"end": "|\n"})):
        print(*args, **kw)
        want = capsys.readouterr().out
        line_print(*args, **kw)
        assert capsys.readouterr().out == want


def test_entry_scripts_install_before_any_other_sniper_import():
    """A service that binds `log=print` as a default at import keeps whatever print was then: the
    install must come first, and only when the file runs as a script (tests import these)."""
    for rel in ENTRY_SCRIPTS:
        tree = ast.parse((REPO / rel).read_text())
        install_at = first_sniper_at = None
        for node in tree.body:
            if (isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
                    and "line_print" in ast.unparse(node) and "install()" in ast.unparse(node)):
                install_at = install_at or node.lineno
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and "sniper" in ast.unparse(node):
                first_sniper_at = first_sniper_at or node.lineno
        assert install_at and first_sniper_at and install_at < first_sniper_at, rel
