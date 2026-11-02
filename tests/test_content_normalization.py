import brain
from bot import supabase_helper
from sniper.markets.content_normalization import (
    normalize_event_title,
    normalize_memory_content,
    normalize_world_event_payload,
)


def test_event_title_drops_verbose_suffix_after_colon():
    assert (
        normalize_event_title("Israel-Lebanon War: Massive Beirut Suburb Airstrike March 27")
        == "Israel-Lebanon War"
    )


def test_event_title_strips_day_counter_and_score_suffix():
    assert normalize_event_title("Iran-US War Day 28: +1") == "Iran-US War"


def test_event_title_preserves_compact_labels():
    assert normalize_event_title("US Tariffs") == "US Tariffs"


def test_world_event_payload_preserves_raw_title_when_compacted():
    payload = normalize_world_event_payload({
        "slug": "iran-us-war",
        "title": "Iran-US War Day 28: +1",
        "status": "active",
    })
    assert payload["title"] == "Iran-US War"
    assert payload["raw_title"] == "Iran-US War Day 28: +1"


def test_normalize_finance_report_headings():
    raw = (
        "## Summary\n"
        "One paragraph.\n\n"
        "## Cross Domain\n"
        "Bridge section.\n\n"
        "## Market implications\n"
        "Risk section."
    )
    normalized = normalize_memory_content("knowledge", "markets/state", raw)
    assert "## EXECUTIVE SUMMARY: What changed" in normalized
    assert "## CROSS-DOMAIN: Geo to macro" in normalized
    assert "## MARKET IMPLICATIONS: Position impact" in normalized


def test_normalize_world_report_headings():
    raw = (
        "## Summary\n"
        "One paragraph.\n\n"
        "## Geopolitics\n"
        "Core shifts.\n\n"
        "## Diversity\n"
        "Coverage."
    )
    normalized = normalize_memory_content("knowledge", "world_narrative", raw)
    assert "## EXECUTIVE SUMMARY: What changed" in normalized
    assert "## GEOPOLITICS: Core shifts" in normalized
    assert "## DIVERSITY: Coverage balance" in normalized


def test_reference_report_content_is_normalized_on_read_too():
    raw = "## Summary\nBody"
    assert normalize_memory_content("reference", "markets/state", raw).startswith(
        "## EXECUTIVE SUMMARY: What changed"
    )


def test_non_target_memory_content_is_unchanged():
    raw = "## Summary\nBody"
    assert normalize_memory_content("project", "markets/state", raw) == raw
    assert normalize_memory_content("knowledge", "market_boosts_current", raw) == raw


def test_upsert_memory_normalizes_targeted_content_before_request(monkeypatch):
    captured = {}

    monkeypatch.setattr(supabase_helper, "get_memory", lambda name, type_filter=None, active_only=True: None)

    def fake_request(method, path, body=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return [body]

    monkeypatch.setattr(supabase_helper, "_request", fake_request)

    result = supabase_helper.upsert_memory(
        "knowledge",
        "markets/state",
        "Finance report",
        "## Summary\nBody",
        source="vps",
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "memories"
    assert captured["body"]["content"].startswith("## EXECUTIVE SUMMARY: What changed")
    assert result["content"].startswith("## EXECUTIVE SUMMARY: What changed")


def test_brain_read_entry_normalizes_reference_report_content(monkeypatch):
    # Brain KV speaks PostgREST over urllib (bot.supabase_helper), not the supabase SDK.
    rows = [{
        "content": "## Summary\nBody",
        "type": "reference",
        "updated_at": "2026-03-27T00:00:00Z",
    }]
    monkeypatch.setattr(brain, "_sb_available", lambda: True)
    monkeypatch.setattr(brain, "_sb_request", lambda method, path, body=None: rows)

    entry = brain.Brain().read_entry("markets/state")
    assert entry is not None
    assert entry["content"].startswith("## EXECUTIVE SUMMARY: What changed")
