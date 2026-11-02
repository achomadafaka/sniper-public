"""evm-key-v1 on an ARMED J7 chain (Alpha, 2026-10-01): MEMECOIN_EVM_KEY_PROOF_CHAINS.

figaro is armed on 56 and holds direct proofs of 0x4403... on 4663 and 8453, but J7 never relayed a
56 buy of theirs, so no 56 claim ever started a proof and the 56 watcher never watched them. With 56
listed, the Ink rule (a direct proof elsewhere + a key account on the chain) proves the address on
56 too. Default empty: evm-key stays Ink-only, exactly as before.

Rule-5 map (2026-10-01, before the edit):
1. Money path: indirect. A newly proven address on an armed chain is watched, so its buys reach the
   on-chain lane's gates; nothing about admission or sizing changes.
2. Speed: none on the fill path; proofs are read at the watcher's roster refresh.
3. Concurrency: a settings read per call; no shared state.
4. Absent/unknown: an unreadable code (target or basis chain) is no proof; empty knob is today's.
5. Failures: a contract at the address on the chain refutes; a shared address is no basis.
6. Units: canonical chain id strings.
7. Sample: wallets with exactly one direct EVM proof address.
8. Rollback: empty the knob; evm-key rows on that chain stop counting at once and stay inert.
Never replaced by it: a wallet's CONFIRMED, REFUTED or shared row on the chain. A j7-crosschain-v1
row (also a dependent proof) counts as the chain's own row here and still triggers
existing_confirmed_other_address in verify_j7_addresses, unlike evm-key: today's behaviour, kept.
"""
import json
import sqlite3

import pytest

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av
from sniper.settings import memecoin as cfg

NOW = "2026-10-01T23:00:00+00:00"
W, OTHER = "uid-figaro", "uid-o"
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
KEY = "0x"                                          # an empty code: a key-controlled account
TX1, TX2 = "0x" + "1" * 64, "0x" + "2" * 64


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    return c


def _conn_at(path):
    """`_conn`'s tables on a file ledger, for the CLI."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    return c


def _proof(c, uid, chain, address=A, version=av.J7_PROOF_VERSION, status=av.CONFIRMED):
    detail = {"proof_version": version, "contradictions": 0,
              "event_evidence": [{"tx": TX1, "mint": "0xm", "verdict": av.BOUGHT},
                                 {"tx": TX2, "mint": "0xm", "verdict": av.BOUGHT}]}
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,address,"
              "verify_status,matches,contradictions,no_data,checked,verified_at,detail) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, uid, uid, chain, address, status, 2, 0, 0, 2, NOW, json.dumps(detail)))
    c.commit()


def _verify_and_save(c, chain="56", code=KEY):
    results = av.verify_evm_key_addresses(c, chain=chain, code_fn=lambda ch, a: code)
    expected = proof_snapshot(c)
    for r in results:
        r["detail"] = av._detail(r)
    save_verification_results(c, results, expected=expected, now=NOW)
    return {r["source_user_id"]: r for r in results}


@pytest.fixture
def on56(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("56",))


def test_the_knob_is_empty_by_default_and_parses_to_j7_chains_only():
    assert cfg.MEMECOIN_EVM_KEY_PROOF_CHAINS == ()
    assert cfg.evm_key_proof_chains("56, 4663,bogus,57073,solana") == ("56", "4663")
    assert av.evm_key_chains() == ("57073",)


def test_off_an_evm_key_row_on_56_is_never_current_and_verifying_56_is_refused():
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56", version=av.EVM_KEY_PROOF_VERSION)
    assert ("56", A) not in av.watcher_address_owners(c)
    with pytest.raises(ValueError):
        av.verify_evm_key_addresses(c, chain="56", code_fn=lambda ch, a: KEY)


def test_on_a_direct_proof_on_4663_proves_the_same_key_address_on_56(on56):
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "8453")
    out = _verify_and_save(c)
    assert out[W]["verify_status"] == av.CONFIRMED
    assert {b["chain"] for b in out[W]["crosschain_basis"]} == {"4663", "8453"}
    assert av.watcher_address_owners(c)[("56", A)][0] == W
    assert ("4663", A) in av.watcher_address_owners(c)        # the basis is untouched


@pytest.mark.parametrize("status", [av.CONFIRMED, av.REFUTED, av.SHARED])
def test_a_wallets_own_56_row_is_never_replaced(on56, status):
    """A direct 56 proof (of any address), a 56 refutation or a shared verdict stands: evm-key is a
    weaker, dependent proof and never overwrites what 56's own receipts said."""
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56", address=B, status=status)
    out = _verify_and_save(c)
    assert out[W]["skip_reason"] == "keeps_chain_row"
    row = c.execute("SELECT address, verify_status FROM axiom_verified_addresses WHERE source_user_id=? "
                    "AND network_id='56'", (W,)).fetchone()
    assert tuple(row) == (B, status)


def test_an_unverified_row_on_56_does_not_block_the_proof(on56):
    """Live unverified rows exist on 56 and 4663 (Gamma, 2026-10-01): only a verdict stands."""
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56", address=B, status=av.UNVERIFIED)
    out = _verify_and_save(c)
    assert out[W]["verify_status"] == av.CONFIRMED and out[W]["skip_reason"] is None
    assert av.watcher_address_owners(c)[("56", A)][0] == W


def test_the_target_chain_is_never_its_own_basis(on56):
    c = _conn()
    _proof(c, W, "56", status=av.UNVERIFIED)          # nothing direct anywhere else
    assert W not in _verify_and_save(c)


def test_a_contract_at_the_address_on_56_refutes_and_nothing_is_watched(on56):
    c = _conn()
    _proof(c, W, "4663")
    out = _verify_and_save(c, code="0x6080604052")
    assert out[W]["verify_status"] == av.REFUTED
    assert ("56", A) not in av.watcher_address_owners(c)


def test_rollback_empties_the_knob_and_the_56_proof_stops_counting(monkeypatch, on56):
    c = _conn()
    _proof(c, W, "4663")
    _verify_and_save(c)
    assert ("56", A) in av.watcher_address_owners(c)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ())
    assert ("56", A) not in av.watcher_address_owners(c)


def test_a_later_j7_claim_of_another_address_on_56_is_still_verified(on56):
    """An evm-key row must not block 56's own evidence: J7 receipts for a different address on 56 are
    read (today an existing CONFIRMED row of another address skips them)."""
    c = _conn()
    _proof(c, W, "4663")
    _verify_and_save(c)
    c.execute("CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, source_user_id TEXT, source_handle TEXT, "
              "fomo_event_id TEXT UNIQUE, network_id TEXT, side TEXT, created_at TEXT)")
    for i, tx in enumerate((TX1, TX2)):
        c.execute("INSERT INTO memecoin_events (source_user_id, source_handle, fomo_event_id, network_id, side, "
                  "created_at) VALUES (?,?,?,?,?,?)", (W, "figaro", "swap:raybot:%s:%s:0x%s" % (
                      tx, B, ("c%d" % i) * 20), "56", "buy", NOW))
    out = {r["source_user_id"]: r for r in av.verify_j7_addresses(
        c, receipt_fn=lambda ch, tx: None, chain="56", armed={W})}
    assert out[W]["skip_reason"] != "existing_confirmed_other_address"
    assert out[W]["address"] == B


def test_the_cli_takes_56_only_when_listed(tmp_path, monkeypatch):
    from scripts.research import verify_axiom_addresses as cli
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ())
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "x.db"), "--evm-key", "--chain", "56"])


# --- research hosts and one key read on a proven chain (Alpha 2026-10-02) -------------------------
# Base had 9 armed wallets with no Base proof whose key address is directly proven on 4663/56, and
# figaro had no 56 proof. evm-key-v1 already proves them, but it never ran (no --evm-key cron), its
# basis reads used the CAPTURE hosts that rule 0724bc08 forbids for research, and it needed a key
# read on EVERY basis chain, so a wallet with 56 in its basis never proved while 56 has no research
# host. Now: research hosts named per chain, and one key read on a proven chain is enough.

DESIGNATOR = "0xef0100" + "de" * 20
CONTRACT = "0x6080604052"


@pytest.fixture
def on8453(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("8453",))


def _codes(by_chain):
    asked = []

    def code_fn(chain, address):
        asked.append(chain)
        return by_chain.get(chain, av.NO_HOST)          # a chain left out has no host: unread
    return code_fn, asked


def test_one_key_read_on_a_proven_chain_proves_and_an_unread_chain_is_recorded(on8453):
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56")
    code_fn, asked = _codes({"8453": KEY, "4663": DESIGNATOR})        # no 56 host: unread
    (r,) = av.verify_evm_key_addresses(c, chain="8453", code_fn=code_fn)
    assert r["verify_status"] == av.CONFIRMED and r["basis_code"] == {"4663": "7702", "56": "unread"}
    assert json.loads(av._detail(r))["basis_code"]["56"] == "unread"
    assert sorted(asked) == ["4663", "56", "8453"]


@pytest.mark.parametrize("codes", [{"8453": KEY, "4663": DESIGNATOR, "56": CONTRACT},     # a basis contract
                                   {"8453": KEY, "56": CONTRACT},                           # and nothing else read
                                   {"8453": KEY}])                                           # no basis read at all
def test_a_contract_on_any_read_chain_or_no_basis_read_proves_nothing(on8453, codes):
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56")
    (r,) = av.verify_evm_key_addresses(c, chain="8453", code_fn=_codes(codes)[0])
    assert r["verify_status"] != av.CONFIRMED


def test_a_failed_read_on_a_hosted_basis_chain_proves_nothing_this_run(monkeypatch):
    """Gamma M1: a read that FAILED (None: timeout, error, exhausted budget) is not 'no host'. On
    394ce5523 the failed 8453 read was skipped as unread and the 4663 key read confirmed."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PROOF_CHAINS", ("56",))
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "8453")
    (r,) = av.verify_evm_key_addresses(c, chain="56", code_fn=_codes({"56": KEY, "4663": KEY, "8453": None})[0])
    assert r["verify_status"] != av.CONFIRMED and r["basis_code"] == {"4663": "empty", "8453": "failed"}


def test_an_armed_wallet_proven_on_4663_only_becomes_watched_on_8453_and_a_contract_does_not(on8453):
    from sniper.persistence import db_memecoin as m
    for code, watched in ((KEY, True), (CONTRACT, False)):
        c = _conn()
        m.ensure_memecoin_tables(c)
        m.add_wallet(c, W, "w", "")
        assert m.set_wallet_chain_armed(c, W, "8453", True, force=True)["ok"]
        _proof(c, W, "4663")
        results = av.verify_evm_key_addresses(c, chain="8453", code_fn=_codes({"8453": code, "4663": KEY})[0])
        expected = proof_snapshot(c)
        for r in results:
            r["detail"] = av._detail(r)
        save_verification_results(c, results, expected=expected, now=NOW)
        assert (A in av.evm_watched_owners(c, "8453")) is watched


def _cli_env(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://custody-8453.example")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody-4663.example")
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_4663", "https://robinhood.capture-reads.example")
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_8453", "https://base.capture-host.example")
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://research.quiknode.example/key1/")


@pytest.mark.parametrize("value, why", [
    ("https://base.capture-host.example", "MEMECOIN_CAPTURE_EVM_READ_URL_8453"),  # a CAPTURE host
    ("https://other.capture-reads.example/x", "capture or default-RPC domain"),  # same domain, other host
    ("https://mainnet.base.org", "read-pool member"),                           # EVM_RPC's default
    ("https://custody-4663.example", "MEMECOIN_EVM_RPC_URL_4663"),              # another env var's URL
    ("MEMECOIN_NOT_SET_ANYWHERE", "is not set")])
def test_a_code_read_host_is_a_research_host_only(tmp_path, monkeypatch, value, why):
    from scripts.research import verify_axiom_addresses as cli
    _cli_env(monkeypatch)
    monkeypatch.setattr(av, "_http_call", lambda *a: pytest.fail("no read before the guard"))
    url, refusal = cli.code_read_url(value)
    assert url is None and why in refusal and "key1" not in refusal
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "x.db"), "--evm-key", "--chain", "8453", "--verify",
                  "--code-url", "8453=" + value])


def test_the_cron_shape_reads_only_the_named_research_hosts(tmp_path, monkeypatch, capsys, on8453):
    """Target 8453 on tenderly, basis 4663 on the research key (an env name, never printed), 56 unread;
    no CAPTURE host is ever asked."""
    from scripts.research import verify_axiom_addresses as cli
    _cli_env(monkeypatch)
    path = tmp_path / "ledger.db"
    c = _conn_at(path)
    _proof(c, W, "4663")
    _proof(c, W, "56")
    c.commit()
    c.close()
    asked = []
    ids = {"https://base.gateway.tenderly.co": hex(8453), "https://research.quiknode.example/key1/": hex(4663)}
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        (ids[url], None) if method == "eth_chainId" else (asked.append(url) or KEY, None)))
    cli.main(["--db", str(path), "--confirm", "--evm-key", "--chain", "8453", "--summary",
              "--code-url", "8453=https://base.gateway.tenderly.co",
              "--code-url", "4663=MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"])
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary["written"] == 1 and summary["status"] == {"axiom_confirmed/evm-key-v1": 1}
    assert asked == ["https://base.gateway.tenderly.co", "https://research.quiknode.example/key1/"]
    assert "key1" not in out
    c = sqlite3.connect(path)
    assert av.watcher_address_owners(c)[("8453", A)][0] == W


def test_a_keyed_drpc_basis_host_passes_beside_keyless_drpc_capture_and_every_read_is_paced(
        tmp_path, monkeypatch, capsys, on8453):
    """The operator's dRPC key as the 56 research host (Alpha 2026-10-02): it passes the guard beside
    the keyless dRPC capture host (a separate budget, Gamma probe), and every read on it, the
    eth_chainId check included, takes the account's shared pacer."""
    from scripts.research import verify_axiom_addresses as cli
    from sniper.services import memecoin_research_pacer as rp
    _cli_env(monkeypatch)
    keyed = "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET"
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_4663", "https://robinhood.drpc.org")
    monkeypatch.setenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_56", keyed)
    monkeypatch.setenv("MEMECOIN_RESEARCH_DRPC_RPC_URL_4663", keyed.replace("bsc", "robinhood"))
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path / "pacer"))
    paced = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: paced.append(self.path))
    path = tmp_path / "ledger.db"
    c = _conn_at(path)
    _proof(c, W, "56")
    c.commit()
    c.close()
    asked = []
    ids = {"https://base.gateway.tenderly.co": hex(8453), keyed: hex(56)}
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append(url) or ((ids[url], None) if method == "eth_chainId" else (KEY, None))))
    cli.main(["--db", str(path), "--verify", "--evm-key", "--chain", "8453", "--summary",
              "--code-url", "8453=https://base.gateway.tenderly.co",
              "--code-url", "56=MEMECOIN_RESEARCH_DRPC_RPC_URL_56"])
    out = capsys.readouterr().out
    assert asked.count(keyed) >= 2 and len(paced) == asked.count(keyed), (asked, paced)
    assert "SECRET" not in out


def test_a_host_named_for_the_wrong_chain_is_refused_before_any_read(tmp_path, monkeypatch, on8453):
    """Gamma M2: `--code-url 8453=<the 4663 research key>` passed the host guard and read 4663 state
    as 8453, where the address answers "0x", a key read. eth_chainId first; a mismatch refuses."""
    from scripts.research import verify_axiom_addresses as cli
    _cli_env(monkeypatch)
    asked = []
    monkeypatch.setattr(av, "_http_call", lambda url, method, params: (
        asked.append(method) or (hex(4663), None)))
    with pytest.raises(SystemExit):
        cli.main(["--db", str(tmp_path / "x.db"), "--verify", "--evm-key", "--chain", "8453",
                  "--code-url", "8453=MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"])
    assert asked == ["eth_chainId"]
