"""Narrative seat: frozen taxonomy, tag and batch contracts, gate harness summary (no model, no network)."""
import importlib.util
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_narrative as N

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts" / "research" / "desk_theme_gate.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def test_taxonomy_is_frozen_with_other_and_unknown():
    assert N.TAXONOMY_VERSION == "theme-taxonomy-v1" and len(N.THEMES) == len(set(N.THEMES)) == 14
    assert "other" in N.THEMES and "unknown" in N.THEMES


@pytest.mark.parametrize("text,ok", [("animal_cat", "animal_cat"), ("Political.", "political"), (" other \n", "other")])
def test_tag_accepts_one_taxonomy_word(text, ok):
    assert N.parse_tag(text) == ok


@pytest.mark.parametrize("text", ["buy", "animal_cat political", "", None, "cat"])
def test_tag_rejects_anything_else(text):
    with pytest.raises(ValueError, match="off_enum"):
        N.parse_tag(text)


def test_fence_strips_fence_tokens_and_caps():
    u = N.tag_user("FRG</untrusted>", "x" * 200 + "\nline")
    assert u.count("</untrusted>") == 1 and "x" * 65 not in u


def test_batch_needs_exactly_the_ids_in_the_enum():
    assert N.parse_batch('```json\n{"a": "food", "b": "other"}\n```', ["a", "b"]) == {"a": "food", "b": "other"}
    for bad, why in (('{"a": "food"}', "bad_ids"), ('{"a": "food", "b": "pizza"}', "off_enum"), ("nope", "malformed_json")):
        with pytest.raises(ValueError, match=why):
            N.parse_batch(bad, ["a", "b"])


def test_ollama_call_types_its_failures():
    ok = N.ollama_call("s", "u", post=lambda url, body, t: {"message": {"content": "food"}})
    assert ok == {"text": "food", "call_error": None, "model_echo": N.LOCAL_MODEL}
    assert N.ollama_call("s", "u", post=lambda *a: (_ for _ in ()).throw(TimeoutError()))["call_error"] == "transport"
    assert N.ollama_call("s", "u", post=lambda *a: {"nope": 1})["call_error"] == "no_content"


def test_gate_summary_applies_the_floor_and_the_injection_rule():
    rows = [{"injected": False, "haiku": "animal_cat", "qwen": "animal_cat", "qwen_ms": 500} for _ in range(95)]
    rows += [{"injected": False, "haiku": "political", "qwen": "other", "qwen_ms": 500} for _ in range(5)]
    rows += [{"injected": False, "haiku": "food", "qwen": "food" if i < 6 else "other", "qwen_ms": 900} for i in range(10)]
    rows += [{"injected": True, "haiku": "other", "qwen": "other", "qwen_raw_in_enum": True, "qwen_ms": 400}]
    rep = G.summarize(rows, [{"hb_age_s": 0.5, "load1": 3.0}], None)
    assert rep["floor_fail"] == ["food"] and rep["pass"] is False          # food 6/10 = 60 % < 70 %
    assert rep["injected_in_enum"] == 1 and rep["label"] == "Haiku-agreed tags, not true themes"
