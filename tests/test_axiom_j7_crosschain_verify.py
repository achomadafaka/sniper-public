"""J7 cross-chain proof, j7-crosschain-v1 (X1, Alpha GO 2026-09-29), and the no-demotion rule.

An EVM address holds across chains for one owner (an EOA shares its key; a same-address AA wallet
shares deployer and salt). Measured 2026-09-29: for every single-buy unproven armed wallet that held a
proof elsewhere, the address J7 names on the new chain equalled the proven one, 14 of 14.

A wallet W is proved on chain B for address A when W's J7 buy events on B name only A, W holds a
current DIRECT proof of A on another EVM chain, A is claimed by no other wallet, and no receipt read on
B contradicts it. The basis is checked when the proof is READ, so a retired basis retires the
cross-chain proof with it, and a cross-chain proof is never the basis of another.
"""
import json
import sqlite3

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.persistence.db_axiom_identity import proof_snapshot, save_verification_results
from sniper.services import axiom_address_verify as av

NOW = "2026-09-29T08:00:00+00:00"
W, OTHER = "uid-w", "uid-o"
A = "0x" + "a1" * 20
B_ADDR = "0x" + "b2" * 20
MINT = "0x" + "c3" * 20
TX1, TX2, TX3 = ("0x" + d * 64 for d in ("1", "2", "3"))
DESIGNATOR = "0xef0100" + "de" * 20            # EIP-7702: an EOA delegating to a contract


def _conn():
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
    return c


def _buy(c, uid, chain, tx, address=A, mint=MINT, at="2026-09-29T07:00:00+00:00"):
    c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,side,"
              "network_id,created_at,signal_source) VALUES(?,?,?,?,?,?,?,?)",
              ("swap:raybot:%s:%s:%s" % (tx, address, mint), uid, uid, mint, "buy", chain, at,
               "follow_activity"))
    c.commit()


def _proof(c, uid, chain, address=A, version=av.J7_PROOF_VERSION, status=av.CONFIRMED, **extra):
    """A stored proof row as the verifier writes it; a j7-transfer-v1 row carries its 2 receipts."""
    detail = {"proof_version": version, "contradictions": 0,
              "event_evidence": [{"tx": TX1, "mint": MINT, "verdict": av.BOUGHT},
                                 {"tx": TX2, "mint": MINT, "verdict": av.BOUGHT}]}
    detail.update(extra)
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,address,"
              "verify_status,matches,contradictions,no_data,checked,verified_at,detail) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, uid, uid, chain, address, status, 2, 0, 0, 2, NOW, json.dumps(detail)))
    c.commit()


def _receipt(pays=A, mint=MINT, ok=True):
    """A successful receipt; it pays `mint` to `pays`, or moves nothing when `pays` is None."""
    logs = [] if pays is None else [{"address": mint, "data": hex(10 ** 18), "topics": [
        av.TRANSFER_TOPIC, "0x" + "00" * 32, "0x" + "00" * 12 + pays[2:]]}]
    return {"status": "0x1" if ok else "0x0", "logs": logs}


def _verify(c, chain="4663", receipts=None, armed=(W,), code=DESIGNATOR):
    read = (lambda _chain, tx: (receipts or {}).get(tx))
    asked = []

    def code_fn(code_chain, address):
        asked.append((code_chain, address))
        return code
    out = {r["source_user_id"]: r for r in av.verify_j7_addresses(
        c, receipt_fn=read, chain=chain, armed=set(armed), code_fn=code_fn)}
    for r in out.values():
        r["code_asked"] = list(asked)
    return out


def _save(c, results):
    expected = proof_snapshot(c)
    for r in results.values():
        r["detail"] = av._detail(r)
    return save_verification_results(c, list(results.values()), expected=expected, now=NOW)


def test_one_j7_buy_naming_the_address_proven_elsewhere_confirms_cross_chain():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    r = _verify(c)[W]                                   # no receipt read: no RPC needed
    assert r["verify_status"] == av.CONFIRMED and r["proof_version"] == av.J7_CROSSCHAIN_PROOF_VERSION
    assert r["crosschain_basis"] == [{"chain": "56", "proof_version": av.J7_PROOF_VERSION}]
    assert r["contradictions"] == 0 and r["address"] == A
    assert r["code_asked"] == [("4663", A)] and r["chain_code"] == "7702"
    assert _save(c, {W: r}) == 1
    assert av.watcher_address_owners(c)[("4663", A)] == (W, W)


def test_a_contradicting_receipt_refutes_it():
    """Alpha's condition: the receipt of W's buy on B pays the coin somewhere else."""
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    r = _verify(c, receipts={TX3: _receipt(pays=B_ADDR)})[W]
    assert r["verify_status"] == av.REFUTED and r["contradictions"] == 1
    assert r["proof_version"] == av.J7_PROOF_VERSION
    _save(c, {W: r})
    assert ("4663", A) not in av.watcher_address_owners(c)


def test_a_later_contradiction_refutes_a_stored_cross_chain_proof():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    _save(c, _verify(c))
    assert ("4663", A) in av.watcher_address_owners(c)
    _save(c, _verify(c, receipts={TX3: _receipt(pays=None)}))
    assert ("4663", A) not in av.watcher_address_owners(c)


def test_a_proof_of_a_different_address_elsewhere_is_no_basis():
    c = _conn()
    _proof(c, W, "56", address=B_ADDR)
    _buy(c, W, "4663", TX3)
    assert _verify(c)[W]["verify_status"] == av.UNVERIFIED


def test_an_address_another_wallet_holds_anywhere_is_no_basis():
    c = _conn()
    _proof(c, W, "56")
    _proof(c, OTHER, "8453")                           # the same address, another owner
    _buy(c, W, "4663", TX3)
    assert _verify(c)[W]["verify_status"] != av.CONFIRMED


def test_a_retired_basis_retires_the_cross_chain_proof_when_read():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    _save(c, _verify(c))
    c.execute("UPDATE axiom_verified_addresses SET verify_status=? WHERE network_id='56'", (av.REFUTED,))
    c.commit()
    assert ("4663", A) not in av.watcher_address_owners(c)


def test_a_cross_chain_proof_is_never_the_basis_of_another():
    c = _conn()
    _proof(c, W, "4663", version=av.J7_CROSSCHAIN_PROOF_VERSION)
    _proof(c, W, "8453", version=av.J7_CROSSCHAIN_PROOF_VERSION)
    assert not {k for k in av.watcher_address_owners(c) if k[1] == A}
    _buy(c, W, "56", TX3)
    assert _verify(c, chain="56")[W]["verify_status"] == av.UNVERIFIED


def test_two_paid_receipts_stay_the_direct_j7_proof():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX1)
    _buy(c, W, "4663", TX2)
    r = _verify(c, receipts={TX1: _receipt(), TX2: _receipt()})[W]
    assert r["verify_status"] == av.CONFIRMED and r["proof_version"] == av.J7_PROOF_VERSION


def test_unreadable_receipts_never_demote_a_current_proof():
    """P1 runs this hourly on a public host. A 429 or an exhausted budget reads NO_DATA, and an
    UNVERIFIED result used to overwrite the CONFIRMED row: the watcher dropped a proven wallet."""
    c = _conn()
    _proof(c, W, "4663")
    _buy(c, W, "4663", TX1)
    _buy(c, W, "4663", TX2)
    r = _verify(c, receipts={})[W]
    assert r["verify_status"] == av.UNVERIFIED and r["skip_reason"] == "keeps_current_proof"
    assert _save(c, {W: r}) == 0
    assert av.watcher_address_owners(c)[("4663", A)] == (W, W)


def test_a_contradiction_still_refutes_a_current_proof():
    c = _conn()
    _proof(c, W, "4663")
    _buy(c, W, "4663", TX1)
    _save(c, _verify(c, receipts={TX1: _receipt(pays=None)}))
    assert ("4663", A) not in av.watcher_address_owners(c)


def test_a_current_direct_proof_here_is_never_replaced_by_a_cross_chain_one():
    """Dry run on the ledger, 2026-09-29: with no receipt readable, 33 of 44 4663 wallets would have
    been rewritten from their direct proof to the weaker cross-chain one, which a retired basis on
    another chain would then take away."""
    c = _conn()
    _proof(c, W, "4663")
    _proof(c, W, "56")
    _buy(c, W, "4663", TX1)
    r = _verify(c, receipts={})[W]
    assert r["skip_reason"] == "keeps_current_proof" and r["proof_version"] == av.J7_PROOF_VERSION
    assert _save(c, {W: r}) == 0


def test_an_empty_account_on_this_chain_confirms():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    r = _verify(c, code="0x")[W]
    assert r["verify_status"] == av.CONFIRMED and r["chain_code"] == "empty"


def test_a_contract_at_the_address_on_this_chain_is_no_cross_chain_proof():
    """Gamma LOW-1 (Alpha 2026-09-29): an AA contract deployed at the same address on B by another
    controller (the CREATE-nonce collision class) would otherwise pass on J7's naming alone."""
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    for code in ("0x6080604052", "0xef0100" + "de" * 19, "0xef0100" + "de" * 21, "0xef0200" + "de" * 20):
        assert _verify(c, code=code)[W]["verify_status"] == av.UNVERIFIED, code


def test_an_unreadable_code_is_no_cross_chain_proof():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    for code in (None, ""):
        assert _verify(c, code=code)[W]["verify_status"] == av.UNVERIFIED, repr(code)


def test_without_a_code_reader_nothing_is_confirmed_cross_chain():
    c = _conn()
    _proof(c, W, "56")
    _buy(c, W, "4663", TX3)
    r = av.verify_j7_addresses(c, receipt_fn=lambda *_a: None, chain="4663", armed={W})[0]
    assert r["verify_status"] == av.UNVERIFIED


def test_the_code_reader_asks_the_receipt_host_at_latest():
    seen = []

    def call(url, method, params):
        seen.append((url, method, params))
        return DESIGNATOR, None
    read = av.evm_code_reader(call_fn=call, urls={"4663": "https://reads.example"})
    assert read("4663", A) == DESIGNATOR
    assert seen == [("https://reads.example", "eth_getCode", [A, "latest"])]
    assert av.evm_code_reader(call_fn=lambda *a: (None, "HTTP 429"))("4663", A) is None
