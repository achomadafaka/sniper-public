import json
import sqlite3

import pytest

from test_codex_crawler_worker_review import F, W, C


@pytest.mark.parametrize("surface,payload", [
    ("post", "{"),
    ("post", '["wrong-root"]'),
    ("post", '{"text":["wrong-text-shape"]}'),
    ("meta", "{"),
    ("meta", '["wrong-root"]'),
    ("meta", '{"tokenMeta":["wrong-meta-shape"]}'),
], ids=["post-malformed-json", "post-root-list", "post-text-list", "meta-malformed-json", "meta-root-list", "tokenMeta-list"])
def test_one_poison_payload_cannot_block_good_following_input_across_restarts(tmp_path, surface, payload):
    desk = F.world(tmp_path)
    other_mint = F.U.b58encode(bytes(range(32)))
    good_mint = F.MINT if surface == "post" else other_mint
    x = sqlite3.connect(desk / "x" / (F.DAY + ".db"))
    if surface == "post":
        x.execute("INSERT INTO posts(event,received_at,handle,payload_json) VALUES('tweet',?,?,?)",
                  (F.T, "poison", payload))
        x.execute("INSERT INTO posts(event,received_at,handle,payload_json) VALUES('tweet',?,?,?)",
                  (F.T + 1, "good", json.dumps({"text": F.MINT})))
    else:
        x.execute("INSERT INTO token_meta(mint,chain,symbol,received_at,payload_json) VALUES(?,NULL,'BAD',?,?)",
                  (F.MINT, F.T, payload))
        x.execute("INSERT INTO token_meta(mint,chain,symbol,received_at,payload_json) VALUES(?,NULL,'GOOD',?,?)",
                  (good_mint, F.T + 1, json.dumps({"tokenMeta": {"name": "Good"}})))
    x.commit()
    x.close()
    calls, errors = [], []

    def model(_system, user):
        mint = user.splitlines()[0].removeprefix("mint: ")
        calls.append(mint)
        return {"text": F.answer(mint=mint), "model_echo": C.MODEL}

    for now in (F.T + 10, F.T + 40):
        db = C.connect(tmp_path / "crawls.db")
        try:
            W.crawl_pass(db, x_dir=desk / "x", desk_dir=desk, call_model=model,
                         per_hour=20, per_day=600, now=now, clock=lambda: now + 2)
        except Exception as exc:
            errors.append((type(exc).__name__, str(exc)))
        finally:
            db.close()  # main's cleanup/restart: roll back any unfinished input transaction
    assert errors == [], {"escaped_on_each_pass": errors, "model_calls": calls, "good_mint": good_mint}
    assert good_mint in calls, {"model_calls": calls, "good_mint": good_mint}
