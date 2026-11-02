from __future__ import annotations
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    predicate: dict[str, Any]
    exit_policy: dict[str, Any]
    sizing: dict[str, Any]
    costs: dict[str, Any]
    controls: dict[str, Any]
    gates: dict[str, Any]
    discovery_cutoff: str
    capture_version_floor: int = 2
    generation: int = 0
    parent_id: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    name: str
    status: str
    spawn_index: int
    generation: int
    parent_id: str | None
    predicate: dict[str, Any]
    exit_policy: dict[str, Any]
    sizing: dict[str, Any]
    costs: dict[str, Any]
    controls: dict[str, Any]
    gates: dict[str, Any]
    params_hash: str
    born_at: str
    discovery_cutoff: str
    capture_version_floor: int
    execution_mode: str
    timecap_s: int
    alpha_alloc: float
    notes: str
