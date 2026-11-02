#!/usr/bin/env python3
"""Find memecoin runners and classify them against one pinned script path."""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunnerProcess:
    pid: int
    script_path: Path
    is_pinned: bool


def _python_entry_script(args):
    for value in args[1:]:
        if value in {"-c", "-m"}:
            return None
        if value == "--":
            continue
        if not value.startswith("-"):
            return value
    return None


def find_memecoin_runners(expected_path, proc_root=Path("/proc")):
    expected = Path(expected_path).resolve()
    found = []
    for process_dir in Path(proc_root).iterdir():
        if not process_dir.name.isdigit():
            continue
        try:
            args = (process_dir / "cmdline").read_bytes().split(b"\0")
            args = [value.decode(errors="replace") for value in args if value]
            if not args or not Path(args[0]).name.startswith("python"):
                continue
            cwd = Path(os.readlink(process_dir / "cwd"))
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        script = _python_entry_script(args)
        if (
            script is None
            or Path(script).name != "memecoin_runner_loop.py"
            or Path(script).parent.name != "scripts"
        ):
            continue
        path = Path(script)
        resolved = (path if path.is_absolute() else cwd / path).resolve()
        found.append(RunnerProcess(int(process_dir.name), resolved, resolved == expected))
    return sorted(found, key=lambda item: item.pid)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True)
    args = parser.parse_args()
    for process in find_memecoin_runners(args.expected):
        status = "pinned" if process.is_pinned else "foreign"
        print(f"{process.pid}\t{status}\t{process.script_path}")


if __name__ == "__main__":
    main()
