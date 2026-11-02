"""The brain never sits on the money path (spec 5.1; plan review finding 24): nothing the funded
processes run may import it. The funded runner and the release workers run from scripts/, not only
from sniper/, so both are scanned, and every repo module they import is followed (Lambda review C4):
a bot.* helper that later reads the brain would otherwise carry it in unseen.

Not handled: importlib/__import__ with a computed name, and processes no memecoin watchdog launches."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNDED_SCRIPTS = ("scripts/memecoin_runner_loop.py", "scripts/research/onchain_wallet_watch.py",
                  "scripts/research/onchain_evm_wallet_watch.py")
# The watchdogs that launch the memecoin release stack; every script they name is an entry point.
WATCHDOGS = ("scripts/memecoin_watchdog.sh", "scripts/memecoin_onchain_volume_watchdog.sh",
             "scripts/memecoin_sol_tape_watchdog.sh", "scripts/memecoin_refusal_capture_watchdog.sh",
             "scripts/research/pump_snipe_tape_watchdog.sh")
# Seen running from the release on 2026-09-28 (Lambda); the derivation must keep finding them. The
# research lab's impulse collector and runner_scanner were archived 2026-10-02 (archive/legacy/research-lab-2026-10-02/).
RELEASE_WORKERS = ("scripts/memecoin_onchain_volume_worker.py", "scripts/memecoin_sol_tape_worker.py",
                   "scripts/memecoin_refusal_capture_loop.py", "scripts/research/pump_snipe_tape.py",
                   "sniper/services/memecoin_bsc_v2_live.py")
MARKERS = ("brain_graph", "brain_cli")


def watchdog_scripts(root=ROOT):
    import re
    found = set()
    for w in WATCHDOGS:
        found |= set(re.findall(r"(?:scripts|sniper)/[A-Za-z0-9_/]+\.py", (root / w).read_text()))
    return found


def _resolve(mod, base_dirs):
    parts = mod.split(".") if mod else []
    for base in base_dirs:
        for cand in (base.joinpath(*parts).with_suffix(".py") if parts else None,
                     base.joinpath(*parts, "__init__.py")):
            if cand is not None and cand.is_file():
                return cand
    return None


def _imports(path, root):
    """Repo files `path` imports, at any depth in the file (lazy imports count)."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods, bases = [a.name for a in node.names], (root, path.parent)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bases = (path.parents[node.level - 1],)
            else:
                bases = (root, path.parent)
            head = node.module or ""
            mods = [head] + [f"{head}.{a.name}" if head else a.name for a in node.names]
        else:
            continue
        for m in mods:
            f = _resolve(m, bases)
            if f is not None:
                out.append(f)
    return out


def closure(entries, root=ROOT):
    """{file: the file that imported it (None for an entry)} over every repo module reachable."""
    via = {Path(e): None for e in entries}
    todo = list(via)
    while todo:
        cur = todo.pop()
        for f in _imports(cur, root):
            if f not in via:
                via[f] = cur
                todo.append(f)
    return via


def offenders(via):
    bad = []
    for p in via:
        if any(m in p.read_text(errors="ignore") for m in MARKERS):
            chain, cur = [], p
            while cur is not None:
                chain.append(str(cur))
                cur = via[cur]
            bad.append(" <- ".join(chain))
    return bad


def test_the_release_workers_are_derived_from_the_watchdogs():
    derived = watchdog_scripts()
    assert set(RELEASE_WORKERS) <= derived, set(RELEASE_WORKERS) - derived


def test_nothing_on_the_money_path_imports_the_brain_graph():
    names = set(FUNDED_SCRIPTS) | set(RELEASE_WORKERS) | watchdog_scripts()
    missing = [n for n in names if not (ROOT / n).exists()]
    assert not missing, f"a funded script moved; update the list: {missing}"
    entries = list((ROOT / "sniper").rglob("*.py")) + [ROOT / n for n in sorted(names)]
    via = closure(entries)
    assert any(p.parent.name == "bot" for p in via), "the closure never reached bot/; the resolver is broken"
    assert offenders(via) == []


def test_the_scan_can_fire(tmp_path):
    (tmp_path / "bot").mkdir()
    (tmp_path / "sniper").mkdir()
    (tmp_path / "bot" / "__init__.py").write_text("")
    (tmp_path / "bot" / "helper.py").write_text("def f():\n    from bot.brain_graph import api\n")
    direct = tmp_path / "x.py"
    direct.write_text("from bot.brain_graph import api\n")
    entry = tmp_path / "sniper" / "svc.py"
    entry.write_text("import os\n\n\ndef g():\n    from bot import helper\n")          # lazy import
    assert offenders(closure([direct], tmp_path)) == [str(direct)]
    # transitive: the entry is clean, the bot helper it imports is not
    assert offenders(closure([entry], tmp_path)) == [f"{tmp_path / 'bot' / 'helper.py'} <- {entry}"]
