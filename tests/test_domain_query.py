# tests/test_domain_query.py
import json
import pytest
from unittest.mock import patch, MagicMock

def test_query_unknown_domain():
    from agents.tools.domain_query import query_domain
    result = query_domain("sports", "nba")
    assert "error" in result

def test_query_geopolitics_returns_structure():
    from agents.tools.domain_query import query_domain
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "iran")
    assert result["domain"] == "geopolitics"
    assert result["topic"] == "iran"
    assert "sections" in result
    assert "memories" in result["sections"]
    assert "world_events" in result["sections"]
    assert "observations" in result["sections"]

def test_query_finance_returns_structure():
    from agents.tools.domain_query import query_domain
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("finance", "oil")
    assert result["domain"] == "finance"
    assert "tickers" in result["sections"]

def test_query_filters_by_topic():
    from agents.tools.domain_query import query_domain
    mock_events = [
        {"slug": "iran-war", "title": "Iran War", "status": "active", "region": "middle_east"},
        {"slug": "uk-election", "title": "UK Election", "status": "developing", "region": "europe_west"},
    ]
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=mock_events), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "iran")
    assert len(result["sections"]["world_events"]) == 1
    assert result["sections"]["world_events"][0]["slug"] == "iran-war"

def test_stale_detection():
    from agents.tools.domain_query import query_domain
    old_mem = {
        "name": "world_state",
        "updated_at": "2026-03-20T00:00:00+00:00",
        "content": "old data",
    }
    with patch("agents.tools.domain_query.get_memory", return_value=old_mem), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "")
    assert result["sections"]["memories"][0]["stale"] == True

def test_create_flag():
    from agents.tools.domain_query import create_flag
    with patch("agents.tools.domain_query.upsert_memory") as mock_upsert:
        result = create_flag("geopolitics", "finance", "high", "Hormuz closed")
    assert result["ok"] == True
    assert "flag:" in result["flag_name"]
    assert "geopolitics\u2192finance" in result["flag_name"]
    mock_upsert.assert_called_once()

def test_read_flags_filters_by_domain():
    from agents.tools.domain_query import read_flags
    mock_flags = [
        {"name": "flag:geopolitics\u2192finance:hormuz", "content": json.dumps({
            "from": "geopolitics", "to": "finance", "urgency": "high",
            "message": "Hormuz", "expires_at": "2099-01-01T00:00:00+00:00",
            "created_at": "2026-03-21T00:00:00+00:00",
        }), "updated_at": "2026-03-21T00:00:00"},
        {"name": "flag:finance\u2192geopolitics:rates", "content": json.dumps({
            "from": "finance", "to": "geopolitics", "urgency": "low",
            "message": "Rates", "expires_at": "2099-01-01T00:00:00+00:00",
            "created_at": "2026-03-21T00:00:00+00:00",
        }), "updated_at": "2026-03-21T00:00:00"},
    ]
    with patch("agents.tools.domain_query.search_memories", return_value=mock_flags), \
         patch("agents.tools.domain_query.delete_memory"):
        flags = read_flags("finance")
    assert len(flags) == 1
    assert "geopolitics\u2192finance" in flags[0]["name"]

def test_expired_flag_gets_cleaned():
    from agents.tools.domain_query import read_flags
    expired_flag = [{
        "name": "flag:geo\u2192finance:old",
        "content": json.dumps({
            "expires_at": "2020-01-01T00:00:00+00:00",
            "created_at": "2020-01-01T00:00:00+00:00",
        }),
        "updated_at": "2020-01-01",
    }]
    with patch("agents.tools.domain_query.search_memories", return_value=expired_flag), \
         patch("agents.tools.domain_query.delete_memory") as mock_del:
        flags = read_flags("finance")
    assert len(flags) == 0
    mock_del.assert_called_once()
