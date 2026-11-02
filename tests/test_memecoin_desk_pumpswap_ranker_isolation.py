"""Misfiled PumpSwap emissions cannot enter the curve ranker or supersede curve identities."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_pumpswap_contract import ps_candidate
from test_memecoin_desk_contract import candidate, rehash, NOW

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("curve_ranker_isolation", ROOT / "scripts/memecoin_desk_ranker_worker.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)


def curve(at=NOW, ordinal=0):
    c = candidate()
    c["pool"] = S.curve_address(c["mint"])
    c["trigger_ordinal"] = ordinal
    c["ticket_id"] = U.ticket_id("solana", c["mint"], c["band_entry_slot"], c["trigger_signature"], ordinal)
    return stamp(c, at)


def stamp(c, at):
    c["snapshot"]["emission_at"] = at
    c["emitted_at"] = datetime.fromtimestamp(at, timezone.utc).isoformat()
    return rehash(c)


def put(base, rows):
    directory = base / "candidate_emissions"
    directory.mkdir()
    (directory / f"{W.utc_day(NOW)}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_pumpswap_contamination_cannot_supersede_curve_same_mint(tmp_path):
    old, wrong = curve(), stamp(ps_candidate(), NOW + 1)
    put(tmp_path, [old, wrong])
    refused = {}
    newest, superseded = W.emissions(tmp_path, NOW + 2, refused)
    assert newest == {old["mint"]: old}
    assert superseded == {}
    assert refused == {"emission_venue": 1}


def test_pumpswap_refusal_precedes_the_shared_verifier(tmp_path, monkeypatch):
    put(tmp_path, [ps_candidate()])
    def forbidden(*_a, **_k):
        raise AssertionError("PumpSwap reached the shared verifier from the curve ranker")
    monkeypatch.setattr(S, "verify_u_emission", forbidden)
    refused = {}
    assert W.emissions(tmp_path, NOW + 1, refused) == ({}, {})
    assert refused == {"emission_venue": 1}


def test_curve_newest_and_superseded_behavior_is_unchanged(tmp_path):
    old, new = curve(), curve(NOW + 1, ordinal=1)
    put(tmp_path, [old, new])
    refused = {}
    assert W.emissions(tmp_path, NOW + 2, refused) == ({new["mint"]: new}, {old["ticket_id"]: old})
    assert refused == {}
