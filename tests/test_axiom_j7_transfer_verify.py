"""J7 transfer proof for EVM wallet addresses (2026-09-27, Alpha GO).

The EVM on-chain watcher never made a detection: it watches only addresses with a current proof,
and no armed 4663 or BSC wallet had one (4663's two directory claims read 12 no_data each). Yet the
wallet's own J7 buy event names its address: `swap:raybot:<tx>:<address>:<mint>`. Over 7 days every
armed wallet with raybot buys showed exactly one address (44/44 on 4663, 36/36 on BSC), and on BSC
that address receives the coin in the transaction's own receipt while the transaction is sent by a
relayer through contract 0xccc88a9d... -- never by the wallet, which is why a sender-based check
finds nothing.

A J7 address is CONFIRMED only when at least 2 distinct buy receipts of the same armed wallet show
Transfer(to = that address) of the coin its event names, with no receipt contradicting it. One
address claimed by two wallets is shared and proves neither. The fixture is real: 9 BSC receipts of
3 armed wallets read from bsc-dataseed1.defibit.io (tests/fixtures/memecoin/j7_address_receipts_56.json).
"""
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/j7_address_receipts_56.json").read_text())
RELAYER = "0xccc88a9d"
NOW = "2026-09-27T23:50:00+00:00"


def _parts(fid):
    p = fid.split(":")
    return p[2], p[3].lower(), p[4].lower()


def _conn(events=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT, mint TEXT, side TEXT, network_id TEXT,
            created_at TEXT, signal_source TEXT);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    for e in (FX["events"] if events is None else events):
        c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,side,"
                  "network_id,created_at,signal_source) VALUES(?,?,?,?,?,?,?,?)",
                  (e["fomo_event_id"], e["source_user_id"], e["handle"], _parts(e["fomo_event_id"])[2],
                   "buy", "56", e["created_at"], "follow_activity"))
    c.commit()
    return c


def _receipt_fn(overrides=None):
    calls = []

    def read(chain, tx):
        calls.append((chain, tx))
        if overrides and tx in overrides:
            return overrides[tx]
        return FX["receipts"].get(tx)
    read.calls = calls
    return read


def _verify(conn, armed=None, receipt_fn=None, **kw):
    return av.verify_j7_addresses(conn, receipt_fn=receipt_fn or _receipt_fn(), chain="56",
                                  armed=set(FX["armed"] if armed is None else armed), **kw)


def _by_uid(results):
    return {r["source_user_id"]: r for r in results}


def test_the_relayer_sends_every_buy_and_the_wallet_receives_the_coin():
    """The fixture's own premise, on the real receipts: a from-based check could never match."""
    for e in FX["events"]:
        tx, addr, mint = _parts(e["fomo_event_id"])
        r = FX["receipts"][tx]
        assert r["to"].startswith(RELAYER) and r["from"] != addr
        assert any(l["address"] == mint and len(l["topics"]) == 3
                   and "0x" + l["topics"][2][-40:].lower() == addr for l in r["logs"])


def test_two_distinct_receipts_confirm_each_armed_wallets_address():
    results = _by_uid(_verify(_conn()))
    assert set(results) == set(FX["armed"])
    for uid, r in results.items():
        assert r["verify_status"] == av.CONFIRMED, r
        assert r["matches"] >= 2 and r["contradictions"] == 0 and r["skip_reason"] is None
        want = {_parts(e["fomo_event_id"])[1] for e in FX["events"] if e["source_user_id"] == uid}
        assert {r["address"]} == want


def test_a_confirmed_j7_proof_arms_the_watcher_after_the_write():
    conn = _conn()
    expected = proof_snapshot(conn)
    results = _verify(conn)
    for r in results:
        r["detail"] = av._detail(r)
    assert save_verification_results(conn, results, expected=expected, now=NOW) == 3
    owners = av.watcher_address_owners(conn)
    for r in results:
        assert owners[("56", r["address"])] == (r["source_user_id"], r["handle"])


def test_one_receipt_is_not_enough():
    uid = FX["armed"][0]
    first = next(e for e in FX["events"] if e["source_user_id"] == uid)
    r = _by_uid(_verify(_conn(events=[first]), armed=[uid]))[uid]
    assert r["verify_status"] == av.UNVERIFIED and r["matches"] == 1


def test_an_unreadable_receipt_is_no_data_never_a_contradiction():
    uid = FX["armed"][0]
    txs = [_parts(e["fomo_event_id"])[0] for e in FX["events"] if e["source_user_id"] == uid]
    r = _by_uid(_verify(_conn(), armed=[uid], receipt_fn=_receipt_fn({t: None for t in txs[1:]})))[uid]
    assert r["verify_status"] == av.UNVERIFIED and r["no_data"] == len(txs) - 1
    assert r["contradictions"] == 0


def test_a_receipt_that_sends_the_coin_elsewhere_refutes():
    """Wallet A's event id rewritten to name wallet B's address: A's receipt pays A, not B."""
    a, b = FX["armed"][0], FX["armed"][1]
    b_addr = next(_parts(e["fomo_event_id"])[1] for e in FX["events"] if e["source_user_id"] == b)
    events = [dict(e, fomo_event_id=":".join(e["fomo_event_id"].split(":")[:3] + [b_addr]
                                               + e["fomo_event_id"].split(":")[4:]),
                   source_user_id="uid-x", handle="x")
              for e in FX["events"] if e["source_user_id"] == a]
    r = _by_uid(_verify(_conn(events=events), armed=["uid-x"]))["uid-x"]
    assert r["verify_status"] == av.REFUTED and r["contradictions"] >= 1


def test_one_address_claimed_by_two_wallets_refuses_both():
    a = FX["armed"][0]
    extra = [dict(e, fomo_event_id=e["fomo_event_id"][:-1] + ("0" if e["fomo_event_id"][-1] != "0" else "1"),
                  source_user_id="uid-other", handle="other")
             for e in FX["events"] if e["source_user_id"] == a][:1]
    results = _by_uid(_verify(_conn(events=FX["events"] + extra), armed=FX["armed"] + ["uid-other"]))
    assert results[a]["verify_status"] == av.SHARED
    assert results["uid-other"]["verify_status"] == av.SHARED
    assert all(results[u]["verify_status"] == av.CONFIRMED for u in FX["armed"][1:])


def test_a_claimant_that_is_not_armed_still_makes_the_address_shared():
    a = FX["armed"][0]
    extra = [dict(e, fomo_event_id=e["fomo_event_id"][:-1] + ("0" if e["fomo_event_id"][-1] != "0" else "1"),
                  source_user_id="uid-unarmed", handle="unarmed")
             for e in FX["events"] if e["source_user_id"] == a][:1]
    results = _by_uid(_verify(_conn(events=FX["events"] + extra)))
    assert results[a]["verify_status"] == av.SHARED and "uid-unarmed" not in results


def test_a_wallet_that_is_not_armed_is_not_verified():
    results = _verify(_conn(), armed=FX["armed"][:1])
    assert [r["source_user_id"] for r in results] == FX["armed"][:1]


def test_an_existing_confirmed_proof_of_another_address_is_not_replaced():
    conn = _conn()
    uid = FX["armed"][0]
    conn.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,"
                 "address,verify_status,matches,contradictions,no_data,checked,verified_at,detail) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, "h", "h", "56", "0x" + "ab" * 20, av.CONFIRMED, 3, 0, 0, 3, NOW, "{}"))
    r = _by_uid(_verify(conn))[uid]
    assert r["skip_reason"] == "existing_confirmed_other_address"


def test_a_wallet_whose_events_name_two_addresses_is_skipped():
    a = FX["armed"][0]
    evs = [e for e in FX["events"] if e["source_user_id"] == a]
    other = dict(evs[0], fomo_event_id=":".join(evs[0]["fomo_event_id"].split(":")[:3]
                                                 + ["0x" + "cd" * 20]
                                                 + evs[0]["fomo_event_id"].split(":")[4:]))
    other["fomo_event_id"] = other["fomo_event_id"][:-1] + "9"
    r = _by_uid(_verify(_conn(events=evs + [other]), armed=[a]))[a]
    assert r["skip_reason"] == "j7_multiple_addresses" and r["verify_status"] == av.UNVERIFIED


def test_a_j7_proof_without_two_bought_receipts_is_not_current():
    conn = _conn()
    expected = proof_snapshot(conn)
    results = _verify(conn)
    for r in results:
        r["detail"] = av._detail(r)
    save_verification_results(conn, results, expected=expected, now=NOW)
    uid = FX["armed"][0]
    row = conn.execute("SELECT detail FROM axiom_verified_addresses WHERE source_user_id=?",
                       (uid,)).fetchone()
    detail = json.loads(row[0])
    detail["event_evidence"] = [e for e in detail["event_evidence"] if e["verdict"] == av.BOUGHT][:1]
    conn.execute("UPDATE axiom_verified_addresses SET detail=? WHERE source_user_id=?",
                 (json.dumps(detail), uid))
    owners = av.watcher_address_owners(conn)
    assert ("56", results[0]["address"]) not in owners or owners[("56", results[0]["address"])][0] != uid


def test_the_receipt_reader_asks_the_chains_own_public_rpc_for_receipts_only():
    seen = []

    def call(url, method, params):
        seen.append((url, method))
        return FX["receipts"][params[0]], None
    tx = next(iter(FX["receipts"]))
    read = av.evm_receipt_reader(call_fn=call)
    assert read("56", tx)["status"] == FX["receipts"][tx]["status"]
    assert seen == [(av.EVM_RPC["56"], "eth_getTransactionReceipt")]
    assert av.evm_receipt_reader(call_fn=lambda *a: (None, "boom"))("56", tx) is None


def test_the_verifier_command_confirms_and_writes_with_j7(tmp_path, monkeypatch, capsys):
    from scripts.research import verify_axiom_addresses as cli
    path = tmp_path / "ledger.db"
    src = _conn()
    src.executescript("""CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_user_id TEXT, handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
        paused INTEGER DEFAULT 0, real_chains TEXT);""")
    for uid in FX["armed"]:
        src.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) "
                    "VALUES(?,?,1,'56')", (uid, uid))
    src.commit()
    dst = sqlite3.connect(path)
    dst.executescript("\n".join(src.iterdump()))
    dst.close()
    asked = []
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://custody-56.example")   # the scheduled-run guard
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append((url, method)) or FX["receipts"].get(params[0]), None))
    cli.main(["--db", str(path), "--confirm", "--j7", "--chain", "56",
              "--rpc-url", "https://bsc-dataseed1.defibit.io"])
    report = json.loads(capsys.readouterr().out)
    assert report["written"] == 3
    assert {u for u, _m in asked} == {"https://bsc-dataseed1.defibit.io"}
    assert {m for _u, m in asked} == {"eth_getTransactionReceipt"}
    conn = sqlite3.connect(path)
    assert {r[0] for r in conn.execute("SELECT verify_status FROM axiom_verified_addresses")} == {
        av.CONFIRMED}


def test_a_receipt_paying_the_address_another_coin_refutes():
    """The event names coin X; the real receipt pays the address coin Y. Not this buy's proof."""
    a = FX["armed"][0]
    events = [dict(e, fomo_event_id=":".join(e["fomo_event_id"].split(":")[:4] + ["0x" + "ee" * 20]))
              for e in FX["events"] if e["source_user_id"] == a]
    r = _by_uid(_verify(_conn(events=events), armed=[a]))[a]
    assert r["verify_status"] == av.REFUTED and r["matches"] == 0


def test_a_buy_of_the_quote_stablecoin_is_not_evidence():
    """0xLingard's newest J7 'buy' names BSC USDC: a swap INTO the quote asset (the wallet sends its
    coin, through an ERC-4337 bundler). It proves nothing and refutes nothing; the 3 coin buys confirm."""
    evs = FX["quote_asset_buy_events"]
    uid = evs[0]["source_user_id"]
    r = _by_uid(_verify(_conn(events=evs), armed=[uid]))[uid]
    assert r["verify_status"] == av.CONFIRMED and r["contradictions"] == 0
    assert all(e["mint"] != "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d" for e in r["event_evidence"])


# --- armed on ANY EVM chain (Alpha, operator yes 2026-10-02) ---------------------------------------
# LevisNFT, martinzz, 397397, wwayfboss, yansanity were proven only on 56. J7 shows their own 4663
# swaps (LevisNFT: 16 at 0xfc77861d), but the --j7 run proved only wallets ARMED on its chain, so they
# never got a 4663 proof, and evm-key had no 4663 basis to prove them on Base. A proof records an
# address and arms nothing; only the candidate set widens.

def _topic(address):
    return "0x" + "0" * 24 + address[2:]


def _rh_ledger(path, armed_chain):
    w, a = "uid-levis", "0x" + "fc" * 20
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT, mint TEXT, side TEXT, network_id TEXT,
            created_at TEXT, signal_source TEXT);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
        CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, source_user_id TEXT,
            handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0, real_chains TEXT);""")
    c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) VALUES(?,?,1,?)",
              (w, "LevisNFT", armed_chain))
    receipts = {}
    for i in (1, 2):
        tx, mint = "0x" + str(i) * 64, "0x" + ("a%d" % i) * 20
        c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,side,network_id,"
                  "created_at,signal_source) VALUES(?,?,?,?,?,?,?,?)",
                  ("swap:raybot:%s:%s:%s" % (tx, a, mint), w, "LevisNFT", mint, "buy", "4663", NOW,
                   "follow_activity"))
        receipts[tx] = {"status": "0x1", "logs": [{"address": mint, "data": "0x" + "0" * 63 + "5",
                        "topics": [av.TRANSFER_TOPIC, _topic("0x" + "99" * 20), _topic(a)]}]}
    c.commit()
    c.close()
    return w, a, receipts


@pytest.mark.parametrize("armed_chain", ["56", "8453"])
def test_a_wallet_armed_elsewhere_gets_a_4663_proof_and_then_a_base_evm_key_proof(
        tmp_path, monkeypatch, capsys, armed_chain):
    from scripts.research import verify_axiom_addresses as cli
    from sniper.settings import memecoin as cfg
    path = tmp_path / "ledger.db"
    w, a, receipts = _rh_ledger(path, armed_chain)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://custody-8453.example")
    ids = {"https://base.research.example": hex(8453), "https://rh.research.example": hex(4663)}

    def http(url, method, params):
        if method == "eth_chainId":
            return ids[url], None
        if method == "eth_getCode":
            return "0x", None
        return receipts.get(params[0]), None
    monkeypatch.setattr(av, "_http_call", http)
    cli.main(["--db", str(path), "--confirm", "--j7", "--chain", "4663", "--rpc-url", "https://rh.reads.example"])
    assert json.loads(capsys.readouterr().out)["written"] == 0       # flag off: today's armed-here set
    cli.main(["--db", str(path), "--confirm", "--j7", "--chain", "4663", "--any-evm-armed",
              "--code-url", "4663=https://rh.research.example"])
    assert json.loads(capsys.readouterr().out)["written"] == 1
    c = sqlite3.connect(path)
    assert av.watcher_address_owners(c)[("4663", a)][0] == w            # proven; arming untouched
    assert c.execute("SELECT real_chains FROM memecoin_wallets").fetchone()[0] == armed_chain
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("8453",))
    cli.main(["--db", str(path), "--confirm", "--evm-key", "--chain", "8453", "--summary",
              "--code-url", "8453=https://base.research.example", "--code-url", "4663=https://rh.research.example"])
    assert json.loads(capsys.readouterr().out)["status"] == {"axiom_confirmed/evm-key-v1": 1}
    assert av.watcher_address_owners(sqlite3.connect(path))[("8453", a)][0] == w


def test_a_wallet_armed_nowhere_or_only_on_solana_is_not_a_candidate(tmp_path, monkeypatch, capsys):
    from scripts.research import verify_axiom_addresses as cli
    path = tmp_path / "ledger.db"
    _w, _a, receipts = _rh_ledger(path, "solana")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (receipts.get(params[0]), None))
    cli.main(["--db", str(path), "--confirm", "--j7", "--chain", "4663", "--rpc-url", "https://rh.reads.example",
              "--any-evm-armed"])
    assert json.loads(capsys.readouterr().out)["written"] == 0


def test_wallets_armed_elsewhere_are_capped_per_run_and_dropped_once_proven(tmp_path, monkeypatch, capsys):
    """Alpha: a backlog of wallets armed elsewhere must never burst the shared capture host."""
    from scripts.research import verify_axiom_addresses as cli
    path = tmp_path / "ledger.db"
    _w, _a, receipts = _rh_ledger(path, "56")
    c = sqlite3.connect(path)
    for i in range(7):                                   # 7 more wallets armed on 56, one 4663 buy each
        uid, addr = "uid-x%d" % i, "0x" + ("%02d" % (10 + i)) * 20
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) VALUES(?,?,1,'56')",
                  (uid, uid))
        c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,side,network_id,"
                  "created_at,signal_source) VALUES(?,?,?,?,?,?,?,?)",
                  ("swap:raybot:0x%s:%s:0x%s" % (("%x" % (i + 3)) * 64, addr, "b%d" % i * 20), uid, uid,
                   "0x" + "b%d" % i * 20, "buy", "4663", NOW, "follow_activity"))
    c.commit()
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (receipts.get(params[0]), None))
    monkeypatch.setattr(cli, "J7_EXTRA_PER_RUN", 5)
    cli.main(["--db", str(path), "--j7", "--verify", "--chain", "4663", "--rpc-url", "https://rh.reads.example",
              "--any-evm-armed"])
    first = json.loads(capsys.readouterr().out)["results"]
    assert len(first) == 5 and "uid-levis" in {r["source_user_id"] for r in first}
    cli.main(["--db", str(path), "--j7", "--confirm", "--chain", "4663", "--rpc-url", "https://rh.reads.example",
              "--any-evm-armed"])
    capsys.readouterr()
    cli.main(["--db", str(path), "--j7", "--verify", "--chain", "4663", "--rpc-url", "https://rh.reads.example",
              "--any-evm-armed"])
    second = {r["source_user_id"] for r in json.loads(capsys.readouterr().out)["results"]}
    assert "uid-levis" not in second and len(second) == 5             # proven: no longer read; cap holds


def test_j7_reads_receipts_only_on_the_named_research_host_after_its_chain_id(tmp_path, monkeypatch, capsys):
    """Alpha 2026-10-02: the 4663 --j7 run moves to the research QuickNode key; the capture hosts are
    already rate-limiting. --code-url gives --j7 the evm-key host guard and the eth_chainId check."""
    from scripts.research import verify_axiom_addresses as cli
    path = tmp_path / "ledger.db"
    _w, _a, receipts = _rh_ledger(path, "4663")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_4663", "https://robinhood.capture.example")
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://rh.quiknode.example/key1/")
    asked = []

    def http(url, method, params):
        asked.append((url, method))
        return (hex(4663), None) if method == "eth_chainId" else (receipts.get(params[0]), None)
    monkeypatch.setattr(av, "_http_call", http)
    cli.main(["--db", str(path), "--confirm", "--j7", "--chain", "4663", "--summary",
              "--code-url", "4663=MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"])
    out = capsys.readouterr().out
    assert json.loads(out)["written"] == 1 and "key1" not in out
    assert {u for u, _m in asked} == {"https://rh.quiknode.example/key1/"}
    assert asked[0] == ("https://rh.quiknode.example/key1/", "eth_chainId")
    with pytest.raises(SystemExit):                                  # a capture host is refused for --j7 too
        cli.main(["--db", str(path), "--verify", "--j7", "--chain", "4663",
                  "--code-url", "4663=https://other.capture.example"])


def test_the_cap_counts_only_claimants_and_a_shared_wallet_does_not_come_back(tmp_path, monkeypatch, capsys):
    """Gamma MEDIUM on 47b67156c: 3 no-claim wallets sorting first must not eat the cap, and a wallet
    that cannot prove (SHARED) must not hold a slot every run."""
    from scripts.research import verify_axiom_addresses as cli
    path = tmp_path / "ledger.db"
    _w, _a, receipts = _rh_ledger(path, "56")
    c = sqlite3.connect(path)
    for i in range(3):                                   # armed on 56, no 4663 claim, uid sorts first
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) VALUES(?,?,1,'56')",
                  ("uid-a%d" % i, "noclaim%d" % i))
    shared = "0x" + "5e" * 20
    for i in range(4):                                   # 4 claimants; x0 and x1 claim one address: SHARED
        uid = "uid-x%d" % i
        addr = shared if i < 2 else "0x" + ("%02d" % (20 + i)) * 20
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) VALUES(?,?,1,'56')",
                  (uid, uid))
        c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,side,network_id,"
                  "created_at,signal_source) VALUES(?,?,?,?,?,?,?,?)",
                  ("swap:raybot:0x%s:%s:0x%s" % (("%x" % (i + 3)) * 64, addr, "c%d" % i * 20), uid, uid,
                   "0x" + "c%d" % i * 20, "buy", "4663", NOW, "follow_activity"))
    c.commit()
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (receipts.get(params[0]), None))
    monkeypatch.setattr(cli, "J7_EXTRA_PER_RUN", 5)
    run = ["--db", str(path), "--j7", "--chain", "4663", "--rpc-url", "https://rh.reads.example", "--any-evm-armed"]
    cli.main(run + ["--confirm"])
    capsys.readouterr()
    cli.main(run + ["--verify"])
    second = {r["source_user_id"] for r in json.loads(capsys.readouterr().out)["results"]}
    assert second == {"uid-x2", "uid-x3"}               # levis proven, x0/x1 shared, no-claim wallets never
