"""Shared env-file loader. The memecoin runner and the smoke scripts must read the SAME set of files
so arm creds can't be stranded in a file one loader ignores (blocker 5). Loads KEY=VALUE lines from
each named file that exists; setdefault semantics mean the real shell env is never overridden and an
earlier file wins over a later one on conflict."""
from __future__ import annotations

import os


def load_env_files(root, names=(".env", ".fomo.env"), env=None):
    """Load each env file in `names` (relative to `root`) that exists into `env` (default os.environ),
    without overriding an already-present key. Returns the list of file names actually loaded."""
    env = os.environ if env is None else env
    loaded = []
    for name in names:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k not in env:                       # setdefault: shell env + earlier files win
                    env[k] = v.strip().strip("'").strip('"')
        loaded.append(name)
    return loaded
