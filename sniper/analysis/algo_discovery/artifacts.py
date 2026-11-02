"""Per-run artifact store + resume manifest for the algorithm-discovery driver.

Each pipeline stage writes one JSON artifact (`<stage>.json`) into a run
directory and records its status + input hash in `run_manifest.json`. A stage
is skipped on re-run if its artifact exists and the upstream input hash is
unchanged — making the expensive fetch/dossier stages reusable across iterations
(`force` / `from_stage` override).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def hash_obj(obj: Any) -> str:
    """Stable short hash of a JSON-serializable object (sorted keys)."""
    payload = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class RunDir:
    """A run directory holding one JSON artifact per stage + a manifest."""

    def __init__(self, root: Path | str, *, force: bool = False) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.force = force
        self.manifest_path = self.root / "run_manifest.json"
        self._manifest: dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self._manifest, indent=2, sort_keys=True))

    def artifact_path(self, stage: str) -> Path:
        return self.root / f"{stage}.json"

    def should_run(self, stage: str, input_hash: str) -> bool:
        """True unless this stage already completed with the same input hash."""
        if self.force:
            return True
        entry = self._manifest.get(stage)
        if not entry or entry.get("status") != "done":
            return True
        if not self.artifact_path(stage).exists():
            return True
        return entry.get("input_hash") != input_hash

    def read_artifact(self, stage: str) -> Any:
        return json.loads(self.artifact_path(stage).read_text())

    def write_artifact(self, stage: str, obj: Any, *, input_hash: str, ts: int | None = None) -> Any:
        self.artifact_path(stage).write_text(
            json.dumps(obj, indent=2, sort_keys=True, default=str)
        )
        self._manifest[stage] = {"status": "done", "input_hash": input_hash, "ts": ts}
        self._save_manifest()
        return obj

    def mark_failed(self, stage: str, error: str, *, ts: int | None = None) -> None:
        self._manifest[stage] = {"status": "failed", "error": str(error)[:500], "ts": ts}
        self._save_manifest()

    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest)
