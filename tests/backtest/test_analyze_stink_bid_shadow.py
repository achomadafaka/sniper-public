import json

from scripts.analyze_stink_bid_shadow import analyze_shadow_file


def test_analyze_shadow_file_counts_queue_model_outcomes(tmp_path):
    path = tmp_path / "shadow.jsonl"
    rows = [
        {
            "event_type": "book_snapshot",
            "condition_id": "cid",
            "token_id": "tok",
        },
        {
            "event_type": "trade_evaluation",
            "condition_id": "cid",
            "token_id": "tok",
            "queue_models": [
                {"queue_model": "price_cross", "price_crossed": True, "size_qualified": True, "queue_fillable": True},
                {"queue_model": "optimistic", "price_crossed": True, "size_qualified": True, "queue_fillable": True},
                {"queue_model": "midpoint", "price_crossed": True, "size_qualified": True, "queue_fillable": False},
                {"queue_model": "pessimistic", "price_crossed": True, "size_qualified": True, "queue_fillable": False},
            ],
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    out = analyze_shadow_file(path)

    assert out["book_snapshots"] == 1
    assert out["trade_evaluations"] == 1
    assert out["models"]["price_cross"]["queue_fillable"] == 1
    assert out["models"]["midpoint"]["queue_fillable"] == 0
    assert out["models"]["pessimistic"]["price_crossed"] == 1
