import json

from sniper.analysis.winning_wallet_thesis import enrichment as E


def test_parent_category_is_tag_driven():
    assert E.parent_category(["Sports", "UFC", "Games"]) == "sports"
    assert E.parent_category(["Geopolitics", "Iran", "Politics"]) == "geopolitics"
    assert E.parent_category(["Politics", "World", "Elections"]) == "politics"
    assert E.parent_category([]) == "untagged"


def test_specific_tag_skips_generic():
    assert E.specific_tag(["Sports", "UFC", "Games"]) == "UFC"
    assert E.specific_tag(["Games", "Sports"]) in {"Games", "Sports"}


def test_event_template_from_slug_not_title():
    assert E.event_template("ufc-fight-night-oliveira-vs-gamrot") == "ufc-fight-night"
    assert E.event_template("cfb-duke-uconn-2025-11-08") == "cfb"
    assert E.event_template("mlb-cle-det-2026-05-21") == "mlb"
    assert E.event_template("") == ""


def test_resolve_conditions_does_not_cache_unknowns_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(E.P, "build_session", lambda: object())

    def boom(*_a, **_k):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(E.P, "_request_json", boom)
    out = E.resolve_conditions(["0xaaa"], wallet="0xw")
    assert "0xaaa" not in out


def test_load_event_tags_caches_miss_but_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(E.P, "build_session", lambda: object())
    monkeypatch.setattr(E.P, "_request_json", lambda *_a, **_k: [])   # successful empty
    out = E.load_event_tags(["e1"], wallet="0xw")
    assert out["e1"] == []
