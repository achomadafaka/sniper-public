"""Desk X producer: frame -> table mapping on the 2026-10-21 tap shapes, day files, credentials."""
import importlib.util
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_x", ROOT / "scripts" / "memecoin_desk_x_producer.py")
X = importlib.util.module_from_spec(spec)
spec.loader.exec_module(X)

AT = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).timestamp()
TWEET = {"id": "2112886781028839482", "createdAt": 1792586438792, "type": "REPLY", "text": "Awesome, Nick!",
         "author": {"id": "1346948635347582978", "handle": "JoshMandell6"}}
QUOTED = {"id": "2112231637840765012", "authorId": "1655716352756260864", "handle": "SimbasJungles",
          "text": "x", "type": "TWEET", "createdAt": 0}
DELETED = {"id": "2112887305161859306", "tweet": {"id": "2112887305161859306", "type": "TWEET",
                                                   "created_at": 1792586563755,
                                                   "author": {"id": "1389913567671975937", "handle": "cb_doge"}}}
EXTERNAL = {"id": "bsq-379680012824255", "createdAt": "2026-10-21T12:41:58.134101039Z", "type": "EXTERNAL",
            "author": {"handle": "BinanceWallet"}, "text": "t"}
SUGGEST = {"tweet_url": "https://twitter.com/USATODAY/status/2112886683834151135", "prediction": "GlobalX",
           "ticker": "GLOBALX", "ai_ticker": "GLOBALX", "pair": None, "tweet_id": "2112886683834151135"}
SUGGEST_UPDATE = {"tweet_id": "2112886678113153389", "results": [{"name": "n", "symbol": "bonks"}]}
META = {"id": "2112887698876670125", "mint": "0xda5e1988097297dcdc1f90d4dfe7909e847cbef6",
        "tokenMeta": {"mint": "0xda5e1988097297dcdc1f90d4dfe7909e847cbef6", "chain": "eth", "symbol": "WLFI"}}


def one(event, payload):
    rows = X.rows_for(event, payload, AT)
    assert len(rows) == 1
    return rows[0]


def test_post_kinds_carry_identity_handle_and_ms_clock():
    t, r = one("tweet", TWEET)
    assert (t, r["tweet_id"], r["handle"], r["author_id"], r["created_at_ms"], r["post_type"]) == \
        ("posts", TWEET["id"], "JoshMandell6", "1346948635347582978", 1792586438792, "REPLY")
    assert json.loads(r["payload_json"]) == TWEET
    _, q = one("quoted_tweet", QUOTED)
    assert (q["handle"], q["author_id"], q["created_at_ms"]) == ("SimbasJungles", "1655716352756260864", None)
    _, d = one("tweet_deleted", DELETED)
    assert (d["tweet_id"], d["handle"], d["created_at_ms"]) == (DELETED["id"], "cb_doge", 1792586563755)
    _, e = one("external_message", EXTERNAL)
    assert (e["handle"], e["created_at_ms"]) == ("BinanceWallet", None)        # ISO string stays in payload


def test_suggestions_and_token_meta_get_their_own_tables():
    t, r = one("ai_suggestion", SUGGEST)
    assert (t, r["tweet_id"], r["ticker"]) == ("ai_suggestion", SUGGEST["tweet_id"], "GLOBALX")
    t, r = one("ai_suggestion_update", SUGGEST_UPDATE)
    assert (t, r["tweet_id"], r["ticker"]) == ("ai_suggestion", SUGGEST_UPDATE["tweet_id"], None)
    t, r = one("token_meta", META)
    assert (t, r["tweet_id"], r["mint"], r["chain"], r["symbol"]) == \
        ("token_meta", META["id"], META["mint"], "eth", "WLFI")


def test_initial_tweets_split_and_odd_shapes_go_to_other():
    rows = X.rows_for("initialTweets", [TWEET, QUOTED, "junk"], AT)
    assert [t for t, _ in rows] == ["posts", "posts"]
    assert all(row['event'] == 'initialTweets' for _, row in rows)
    assert one("following_update", {"id": "f"})[0] == "other"
    assert one("tweet", None)[0] == "other"
    assert one("tweet", {"author": "not-a-dict", "id": 5})[1]["handle"] is None


def test_coverage_checkpoint_binds_session_offset_and_committed_rows(tmp_path):
    feed = X.Feed({'now': False}, clock=lambda: AT, clock_offset_fn=lambda: .25)
    feed.report('connect')
    feed.frame('initialTweets', [TWEET])
    feed.frame('token_meta', META)
    items, beat = feed.checkpoint()
    assert beat['connected'] is True and beat['committed_through'] == AT
    assert beat['clock_offset_s'] == .25 and len(beat['session_id']) == 32
    assert [r['kind'] for table,r in items if table=='connection'] == ['epoch','connect','heartbeat']
    assert all(r['clock_offset_s'] == .25 for table,r in items if table in ('posts','token_meta','connection'))
    store=X.DayStore(tmp_path);store.write(items);store.close()
    c=sqlite3.connect(tmp_path/(X.utc_day(AT)+'.db'))
    assert c.execute('SELECT event,clock_offset_s FROM posts').fetchone() == ('initialTweets',.25)
    assert c.execute('SELECT clock_offset_s FROM token_meta').fetchone() == (.25,)
    assert c.execute("SELECT session_id FROM connection WHERE kind='heartbeat'").fetchone() == (beat['session_id'],)
    c.close()


def test_writer_migrates_only_nullable_clock_columns_without_rewriting_old_rows(tmp_path):
    path=tmp_path/(X.utc_day(AT)+'.db');c=sqlite3.connect(path)
    c.executescript('''CREATE TABLE posts(id INTEGER PRIMARY KEY,event TEXT NOT NULL,tweet_id TEXT,
        received_at REAL NOT NULL,created_at_ms INTEGER,handle TEXT,author_id TEXT,post_type TEXT,payload_json TEXT NOT NULL);
        CREATE TABLE token_meta(id INTEGER PRIMARY KEY,tweet_id TEXT,mint TEXT,chain TEXT,symbol TEXT,
        received_at REAL NOT NULL,payload_json TEXT NOT NULL);
        CREATE TABLE connection(id INTEGER PRIMARY KEY,at REAL NOT NULL,kind TEXT NOT NULL,detail TEXT);''')
    c.execute("INSERT INTO posts(event,tweet_id,received_at,payload_json) VALUES('tweet','old',?,'{}')",(AT-1,))
    c.commit();c.close()
    store=X.DayStore(tmp_path);store.write(X.rows_for('tweet',TWEET,AT));store.close()
    c=sqlite3.connect(path)
    assert c.execute("SELECT clock_offset_s FROM posts WHERE tweet_id='old'").fetchone() == (None,)
    assert 'idx_posts_received' in {r[1] for r in c.execute('PRAGMA index_list(posts)')}
    c.close()


def test_day_store_splits_on_utc_midnight_and_keeps_both_sides(tmp_path):
    store = X.DayStore(tmp_path)
    late, early = AT + 12 * 3600 - 1, AT + 12 * 3600 + 1                       # 23:59:59 / 00:00:01
    store.write(X.rows_for("tweet", TWEET, late) + X.rows_for("tweet", TWEET, early)
                + [("connection", {"at": early, "kind": "disconnect", "detail": None})])
    store.close()
    for day, n in (("2026-10-21", 1), ("2026-10-22", 1)):
        conn = sqlite3.connect(tmp_path / ("%s.db" % day))
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == n
    assert sqlite3.connect(tmp_path / "2026-10-22.db").execute("SELECT kind FROM connection").fetchone() == \
        ("disconnect",)


class FakeClient:
    """Stands in for socketio.Client: records connect args, feeds frames, stops the loop."""

    def __init__(self, env_path, feed, frames, stop, refuse=None):
        self.env_path, self.feed, self.frames, self.stop, self.refuse = env_path, feed, frames, stop, refuse
        self.connected_to, self.auth = [], None

    def connect(self, url, **kw):
        self.connected_to.append((url, kw["socketio_path"]))
        self.auth = kw["auth"]
        if self.refuse == "raise":
            raise ConnectionError("Invalid token")
        self.feed.report("connect")
        for event, payload in self.frames:
            self.feed.frame(event, payload)
        if self.refuse:
            self.feed.report(self.refuse, "{'message': 'Invalid token'}")
        self.stop["now"] = True

    def disconnect(self):
        pass


def _setup(tmp_path, monkeypatch, token="tok.one.sig"):
    monkeypatch.setenv("MEMECOIN_DESK_X_PRODUCER_ENABLED", "1")
    monkeypatch.delenv("J7_ENV_FILE", raising=False)
    (tmp_path / ".j7.env").write_text("J7_TOKEN=other.acct.sig\nJ7_TOKEN_2=%s\n" % token)


def test_main_opens_only_the_feed_socket_and_rereads_token_2(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, token="secret.two.sig")
    made, stop = [], {"now": False}

    def factory(env_path, feed):
        made.append(FakeClient(env_path, feed, [("tweet", TWEET), ("token_meta", META)], stop))
        return made[0]
    assert X.main(env_root=tmp_path, client_factory=factory, stop=stop) == 0
    assert made[0].connected_to == [(X.FEED_URL, "socket.io")]                 # never the wallets socket
    assert made[0].auth() == {"token": "secret.two.sig"}
    (tmp_path / ".j7.env").write_text("J7_TOKEN_2=rotated.by.runner\n")      # the runner's renewal
    assert made[0].auth() == {"token": "rotated.by.runner"}
    day = next((tmp_path / "data" / "desk" / "x").glob("*.db"))
    conn = sqlite3.connect(day)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM token_meta").fetchone()[0] == 1
    out = capsys.readouterr()
    assert "secret" not in out.out + out.err


def test_main_refuses_without_token_2_or_knob(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    (tmp_path / ".j7.env").write_text("J7_TOKEN=only.first.sig\n")
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("connected without J7_TOKEN_2"))
    assert X.main(env_root=tmp_path, client_factory=boom) == 2
    monkeypatch.setenv("MEMECOIN_DESK_X_PRODUCER_ENABLED", "0")
    assert X.main(env_root=tmp_path, client_factory=boom) == 0


def test_a_refused_token_gets_one_attempt_per_backoff_window(tmp_path, monkeypatch):
    # Gamma M1 on 11d07d85d: auth_error was retried forever, and a first-connect failure every cron minute
    for refusal in ("auth_error", "connect_error", "raise"):
        _setup(tmp_path, monkeypatch, token="refused.%s.sig" % refusal.replace("_", ""))
        attempts = []

        def factory(env_path, feed):
            attempts.append(FakeClient(env_path, feed, [], feed.stop, refuse=refusal))
            return attempts[-1]
        assert X.main(env_root=tmp_path, client_factory=factory, stop={"now": False}) in (3, 4)
        for _ in range(3):                                                     # three cron minutes
            assert X.main(env_root=tmp_path, client_factory=factory, stop={"now": False}) == 0
        assert len(attempts) == 1, refusal
        (tmp_path / ".j7.env").write_text("J7_TOKEN_2=renewed.by.runner.%s\n" % refusal)
        X.main(env_root=tmp_path, client_factory=factory, stop={"now": False})
        assert len(attempts) == 2, "a new token must not wait out the old token's window"
        marker = tmp_path / "data" / "desk" / "x" / "auth_backoff.json"
        assert "renewed" not in marker.read_text() and "refused" not in marker.read_text()
        old = json.loads(marker.read_text())
        marker.write_text(json.dumps({**old, "at": old["at"] - X.AUTH_BACKOFF_S - 1}))
        X.main(env_root=tmp_path, client_factory=factory, stop={"now": False})
        assert len(attempts) == 3, "the window ends"
        marker.unlink()


def test_heartbeat_snapshot_survives_new_count_keys_from_the_socket_thread():
    # Gamma L1: json.dumps over state while the socket thread added keys could raise
    import threading
    feed = X.Feed({"now": False})
    done = threading.Event()

    def writer():
        for i in range(20000):
            feed.frame("ev%d" % i, None)
        done.set()
    t = threading.Thread(target=writer)
    t.start()
    while not done.is_set():
        json.dumps(feed.snapshot())
    t.join()
    assert len(feed.snapshot()["counts"]) == 20000


def test_day_files_older_than_retention_are_pruned_on_rollover(tmp_path):
    for day in ("2026-09-20", "2026-09-21", "2026-10-20"):
        for suffix in (".db", ".db-wal"):
            (tmp_path / (day + suffix)).write_text("x")
    (tmp_path / "producer.heartbeat").write_text("{}")
    store = X.DayStore(tmp_path, retention_days=30)
    store.write(X.rows_for("tweet", TWEET, AT))                               # 2026-10-21 opens
    store.close()
    left = sorted(p.name for p in tmp_path.iterdir())
    assert "2026-09-20.db" not in left and "2026-09-20.db-wal" not in left
    assert {"2026-09-21.db", "2026-10-20.db", "2026-10-21.db", "producer.heartbeat"} <= set(left)


def test_retention_comes_from_the_state_env_file_read_in_main(tmp_path, monkeypatch):
    # Gamma on 5e6725edf: read at import, so cron's .env value was ignored
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMECOIN_DESK_X_RETENTION_DAYS", "30")           # recorded, so teardown unsets what .env loads
    monkeypatch.delenv("MEMECOIN_DESK_X_RETENTION_DAYS")
    (tmp_path / ".env").write_text("MEMECOIN_DESK_X_RETENTION_DAYS=7\n")
    out = tmp_path / "data" / "desk" / "x"
    out.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=8)).strftime("%Y-%m-%d")
    (out / (old + ".db")).write_text("x")
    stop = {"now": False}
    assert X.main(env_root=tmp_path, client_factory=lambda e, f: FakeClient(e, f, [("tweet", TWEET)], stop),
                  stop=stop) == 0
    assert not (out / (old + ".db")).exists()
    monkeypatch.setenv("MEMECOIN_DESK_X_RETENTION_DAYS", "seven")
    assert X.main(env_root=tmp_path, client_factory=lambda *a: None) == 2
