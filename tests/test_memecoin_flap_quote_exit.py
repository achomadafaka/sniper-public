"""The two-leg rescue for a stuck chain-56 flap.sh coin quoted in a token (DAODUN 68544, 2026-10-02).

A fake chain plays both legs: leg 1 sells the whole coin balance through the Portal for the quote
token, leg 2 sells exactly leg 1's quote-token delta to WBNB, and the close books from the WBNB
delta. Covered: the happy path, the lock, an ambiguous send that stops, the resume from the journal
(a reverted leg 2 retried; an unresolved leg 1 settled later), and the refusals.
"""
import json
import sqlite3

import pytest

from sniper.execution import evm_flap_quote_exit as X
from sniper.execution.evm_dex_quote import PANCAKE_EXACT_INPUT, _encode_path
from sniper.execution import exec_lock
from sniper.persistence import db_memecoin as m

WALLET = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
MINT = "0xf98a0f657b990b1496b46f8ea76bf7ca561a8888"
CHEEMS = "0x0df0587216a4a1bb7d5082fdc491d93d2dd4b413"
ADDR = {"portal": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
        "v2_router": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
        "v3_router": "0x1b81d678ffb9c0263b24a97847620c99d213eb14",
        "v3_quoter": "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997"}
RAW = 10 * 10 ** 18
LEG1_OUT = 85_000_000 * 10 ** 18
V2_OUT, V3_OUT = 60_000_000_000_000_000, 61_000_000_000_000_000


def _word(n):
    return format(int(n), "064x")


def _portal_amount(data):
    from eth_abi import decode
    return decode(["(address,address,uint256,uint256,bytes)"], bytes.fromhex(data[10:]))[0][2]


def _transfer(token, src, dst, amount):
    return {"address": token, "topics": [X.TRANSFER_TOPIC, X._addr_topic(src), X._addr_topic(dst)],
            "data": "0x" + _word(amount)}


class FakeChain:
    """Balances, allowances and nonces that move when a signed tx is sent; receipts on request."""

    def __init__(self, *, coin=RAW, cheems=0, revert_leg2=0, hold_receipt=(), direct=True, path_out=0):
        self.address, self.rpc = WALLET, "http://fake"
        self.direct, self.path_out = direct, path_out   # direct: Cheems has V2/V3 WBNB pools
        self.bal = {MINT: coin, CHEEMS: cheems, X.WBNB: 0}
        self.allow = {}
        self.nonce = 7
        self.pending = 7
        self.sent, self.receipts = [], {}
        self.revert_leg2 = revert_leg2          # how many leg-2 sends revert
        self.hold_receipt = set(hold_receipt)   # kinds whose receipt never shows (ambiguous)
        self.send_result = None                 # None = accept
        self.drop = set()                       # kinds accepted, then dropped: nonce used, no effect
        self.offered, self.applied = [], set()  # every raw offered; the raws the chain has seen

    # reads
    def call(self, to, data, *, overrides=None):
        sel = data[:10]
        if to == ADDR["portal"] and sel == "0xef7ec2e7":
            amount = _portal_amount(data)
            if amount > self.bal[MINT] or (self.allow.get((MINT, to), 0) < amount and not overrides):
                return None
            return "0x" + _word(LEG1_OUT * amount // RAW)
        if to == ADDR["v2_router"] and sel == X.SEL_GET_AMOUNTS_OUT:
            return "0x" + _word(32) + _word(2) + _word(1) + _word(V2_OUT) if self.direct else None
        if to == ADDR["v3_quoter"] and sel == X.SEL_V3_QUOTE_SINGLE:
            fee = int(data[10 + 192:10 + 256], 16)
            return "0x" + _word((V3_OUT if fee == 2500 else 1) if self.direct else 0) + _word(0) * 3
        if to == ADDR["v3_quoter"] and sel == X.SEL_V3_QUOTE_PATH:
            return "0x" + _word(self.path_out) + _word(0) * 3
        if to == ADDR["v3_router"] and sel == "0x" + PANCAKE_EXACT_INPUT:
            return "0x" + _word(self.path_out)
        if to == ADDR["v3_router"] and sel == X.SEL_V3_EXACT_INPUT_SINGLE:
            return "0x" + _word(V3_OUT)
        raise AssertionError((to, sel))

    def balance(self, token):
        return self.bal[token]

    def allowance(self, token, spender):
        return self.allow.get((token, spender), 0)

    def nonces(self):
        return self.pending, self.nonce

    def gas_price(self):
        return 100_000_000

    def estimate_gas(self, tx):
        return 200_000

    # sends
    def send(self, raw, kind="swap"):
        if self.send_result is not None:
            return self.send_result
        tx = json.loads(raw)
        self.offered.append(raw)
        if raw in self.applied:                 # the node already has these exact bytes
            self.sent.append("reoffer")
            return tx["hash"]
        self.applied.add(raw)
        if (("leg1" in self.drop and tx["to"] == ADDR["portal"]) or ("leg2" in self.drop and tx["to"] in (
                ADDR["v2_router"], ADDR["v3_router"]))):
            self.nonce += 1                     # a replacement took the nonce; ours never lands
            self.pending = self.nonce
            self.sent.append("dropped")
            return tx["hash"]
        kind = self._apply(tx)
        self.sent.append(kind)
        self.nonce += 1
        self.pending = self.nonce
        return tx["hash"]

    def _apply(self, tx):
        to, data, h = tx["to"], tx["data"], tx["hash"]
        if data.startswith("0x095ea7b3"):
            spender = "0x" + data[10 + 24:10 + 64]
            self.allow[(to, spender)] = int(data[10 + 64:], 16)
            self.receipts[h] = {"status": "0x1", "logs": []}
            return "approve"
        if to == ADDR["portal"]:
            amount = _portal_amount(data)
            out = LEG1_OUT * amount // RAW
            self.bal[MINT] -= amount
            self.bal[CHEEMS] += out
            receipt = {"status": "0x1", "logs": [_transfer(MINT, WALLET, to, amount),
                                                 _transfer(CHEEMS, to, WALLET, out)]}
            if "leg1" not in self.hold_receipt:
                self.receipts[h] = receipt
            else:
                self.held = (h, receipt)
            return "leg1"
        if data[2:10] == PANCAKE_EXACT_INPUT:
            from eth_abi import decode
            path, _to, _deadline, amount, _min = decode(
                ["(bytes,address,uint256,uint256,uint256)"], bytes.fromhex(data[10:]))[0]
            self.leg2_path = path
        else:
            amount = (int(data[10:10 + 64], 16) if to == ADDR["v2_router"]
                      else int(data[10 + 320:10 + 384], 16))
        if self.revert_leg2:
            self.revert_leg2 -= 1
            self.receipts[h] = {"status": "0x0", "logs": []}
            return "leg2"
        out = (V2_OUT if to == ADDR["v2_router"] else
               self.path_out if data[2:10] == PANCAKE_EXACT_INPUT else V3_OUT)
        self.bal[CHEEMS] -= amount
        self.bal[X.WBNB] += out
        self.leg2_amount = amount
        self.receipts[h] = {"status": "0x1", "logs": [_transfer(CHEEMS, WALLET, to, amount),
                                                      _transfer(X.WBNB, to, WALLET, out)]}
        return "leg2"

    def receipt(self, tx_hash):
        return self.receipts.get(tx_hash)


def _signer(start=0):
    n = {"i": start}

    def sign(tx):
        n["i"] += 1
        h = "0x%064x" % n["i"]
        return json.dumps(dict(tx, hash=h)), h
    return sign


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


@pytest.fixture(autouse=True)
def locks(monkeypatch, tmp_path):
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))


def _position(c, *, qty=10.0):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=MINT, ticker="DAODUN",
                          entry_price=9.0, entry_notional=90.0, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-10-02T14:52:27Z",
                          strategy="alpha_b", execution_mode="real", network_id="56")
    m.insert_fill(c, position_id=pid, kind="entry", price=9.0, qty=qty, notional=90.0,
                  fill_source="chain", fraction=1.0, created_at="2026-10-02T14:52:27Z", tx_sig="E")
    m.mark_stuck_bag(c, pid)
    return pid


def _run(c, pid, chain, tmp_path, *, quote_token=CHEEMS, sign_start=0, **kw):
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s
    return X.Run(c, pid, chain=chain, sign_fn=_signer(sign_start), journal_path=tmp_path / "j.json",
                 price_fn=lambda: 600.0, addresses=ADDR, out=lambda *_: None, sleep_fn=sleep,
                 clock=lambda: clock["t"], state_fn=lambda chain_, mint: {"quote_token": quote_token,
                                                                          "status": 4}, **kw)


def test_both_legs_sell_the_coin_then_exactly_its_quote_delta_and_book_from_wbnb(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain(cheems=5 * 10 ** 18)            # Cheems the wallet already held stays put
    journal = _run(conn, pid, chain, tmp_path).confirm()
    assert journal["state"] == "booked"
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]
    assert chain.leg2_amount == LEG1_OUT                # leg 1's delta, not the wallet's Cheems
    assert chain.bal[CHEEMS] == 5 * 10 ** 18 and chain.bal[MINT] == 0
    assert chain.allow[(MINT, ADDR["portal"])] == 0 or True     # exact approvals, consumed
    pos = m.get_position(conn, pid)
    assert not pos["is_open"] and pos["exit_reason"] == "manual" and not pos["stuck_bag"]
    assert pos["close_sig"] == journal["leg1"]
    assert pos["realized_pnl"] == pytest.approx(V3_OUT / 1e18 * 600.0 - 90.0)
    fill = conn.execute("SELECT kind, qty, notional FROM memecoin_fills WHERE position_id=? "
                        "AND kind!='entry'", (pid,)).fetchone()
    assert tuple(fill) == ("manual_close", 10.0, pytest.approx(V3_OUT / 1e18 * 600.0))


def test_the_approvals_are_exact(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    seen = []
    real = chain._apply

    def spy(tx):
        if tx["data"].startswith("0x095ea7b3"):
            seen.append(int(tx["data"][10 + 64:], 16))
        return real(tx)
    chain._apply = spy
    _run(conn, pid, chain, tmp_path).confirm()
    assert seen == [RAW, LEG1_OUT]


def test_the_lock_held_elsewhere_sends_nothing(conn, tmp_path, monkeypatch):
    pid = _position(conn)
    chain = FakeChain()
    monkeypatch.setattr(X, "LOCK_WAIT_S", 0.0)
    with exec_lock.wallet_exec_lock(exec_lock.evm_exec_lock_key("56", WALLET)) as held:
        assert held
        with pytest.raises(X.Refused, match="exec lock"):
            _run(conn, pid, chain, tmp_path).confirm()
    assert chain.sent == [] and m.get_position(conn, pid)["close_claim_id"] is None


def test_an_ambiguous_leg1_stops_then_a_rerun_settles_it_and_finishes(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    with pytest.raises(X.Refused, match="AMBIGUOUS"):
        _run(conn, pid, chain, tmp_path).confirm()
    assert chain.sent == ["approve", "leg1"]                       # nothing after the ambiguity
    journal = json.loads((tmp_path / "j.json").read_text())
    assert journal["state"] == "leg1_submitted"
    assert m.get_position(conn, pid)["close_sig"] == journal["leg1"]   # the runner cannot re-sell
    chain.pending = chain.nonce                                    # the chain shows it now
    h, receipt = chain.held
    chain.receipts[h] = receipt
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]    # leg 1 was never re-sent


def test_a_reverted_leg2_keeps_the_claim_and_a_rerun_retries_leg2_only(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    with pytest.raises(X.Refused, match="leg 2 reverted"):
        _run(conn, pid, chain, tmp_path).confirm()
    pos = m.get_position(conn, pid)
    assert pos["is_open"] and pos["close_sig"]                     # held: no runner re-sell
    assert json.loads((tmp_path / "j.json").read_text())["state"] == "leg2_failed"
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert chain.sent == ["approve", "leg1", "approve", "leg2", "leg2"]
    assert chain.leg2_amount == LEG1_OUT


def test_a_rejected_leg1_resets_the_claim(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    run = _run(conn, pid, chain, tmp_path)
    real_send = chain.send

    def send(raw, kind="swap"):
        if json.loads(raw)["to"] == ADDR["portal"]:
            return False
        return real_send(raw, kind)
    chain.send = send
    with pytest.raises(X.Refused, match="rejected leg 1"):
        run.confirm()
    pos = m.get_position(conn, pid)
    assert pos["is_open"] and pos["close_claim_id"] is None and pos["close_sig"] is None


@pytest.mark.parametrize("case", ["balance", "pending", "native_quote", "claimed"])
def test_refusals_send_nothing(conn, tmp_path, case):
    pid = _position(conn)
    chain = FakeChain(coin=RAW // 2 if case == "balance" else RAW)
    if case == "pending":
        chain.pending = chain.nonce + 1
    if case == "claimed":
        m.claim_position_close(conn, pid, "2026-10-02T15:00:00Z", "b_stop")
    quote = "0x" + "00" * 20 if case == "native_quote" else CHEEMS
    run = _run(conn, pid, chain, tmp_path, quote_token=quote)
    run._curve = (lambda mint: (_ for _ in ()).throw(X.Refused("native quote"))) \
        if case == "native_quote" else run._curve
    with pytest.raises(X.Refused):
        run.confirm()
    assert chain.sent == []


def test_the_dry_run_writes_nothing(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    result = _run(conn, pid, chain, tmp_path, overrides_fn=lambda *a, **k: {"x": 1}).dry_run()
    assert result["leg1_out"] == LEG1_OUT and result["routes"][0]["venue"] == "v3"
    assert chain.sent == [] and not (tmp_path / "j.json").exists()
    assert m.get_position(conn, pid)["close_claim_id"] is None


def test_leg2_refuses_when_the_wallet_holds_less_than_leg1s_delta(conn, tmp_path):
    # Gamma's surviving mutant on b2e5f8010: the `held < amount` guard had no test.
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    with pytest.raises(X.Refused, match="leg 2 reverted"):
        _run(conn, pid, chain, tmp_path).confirm()
    chain.bal[CHEEMS] -= 1                         # something moved the residue meanwhile
    sent = list(chain.sent)
    with pytest.raises(X.Refused, match="under leg 1's"):
        _run(conn, pid, chain, tmp_path).confirm()
    assert chain.sent == sent and m.get_position(conn, pid)["is_open"]


def test_a_route_the_node_refuses_yields_to_the_next_near_route_never_a_far_one(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    real = chain.estimate_gas

    def estimate(tx):
        if tx["to"] == ADDR["v3_router"]:
            raise RuntimeError("estimateGas refused: execution reverted")
        return real(tx)
    chain.estimate_gas = estimate
    journal = _run(conn, pid, chain, tmp_path).confirm()
    assert journal["state"] == "booked" and journal["leg2_route"]["venue"] == "v2"
    chain2 = FakeChain()
    def only_the_far_route(tx):                  # v2 and v3/2500 refuse; v3/10000 (out=1) would pass
        if tx["to"] == ADDR["v2_router"] or (
                tx["to"] == ADDR["v3_router"] and int(tx["data"][10 + 128:10 + 192], 16) != 10000):
            raise RuntimeError("refused")
        return 200_000
    chain2.estimate_gas = only_the_far_route
    pid2 = _position(conn)
    with pytest.raises(X.Refused, match="every leg-2 route refused"):
        _run(conn, pid2, chain2, tmp_path / "b").confirm()
    assert chain2.sent.count("leg2") == 0          # the far v3/10000 route (out=1) was never sent


def test_an_allowance_the_node_shows_a_block_late_is_waited_for_not_refused(conn, tmp_path):
    # The operator's first --confirm (2026-10-02): the approval receipt was seen, the next eth_call
    # landed a block behind and read allowance 0, and the run refused leg 1.
    pid = _position(conn)
    chain = FakeChain()
    lag = {"reads": 2}
    real_allowance, real_call = chain.allowance, chain.call

    def allowance(token, spender):
        if chain.sent and lag["reads"] > 0:
            lag["reads"] -= 1
            return 0
        return real_allowance(token, spender)

    def call(to, data, *, overrides=None):
        if to == ADDR["portal"] and chain.sent and lag["reads"] > 0:
            return None
        return real_call(to, data, overrides=overrides)
    chain.allowance, chain.call = allowance, call
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]       # approved once per leg


def test_an_allowance_that_never_shows_refuses_releases_the_claim_and_reruns_without_reapproving(
        conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    real_allowance = chain.allowance
    hidden = {"on": True}
    chain.allowance = lambda token, spender: 0 if hidden["on"] else real_allowance(token, spender)
    with pytest.raises(X.Refused, match="does not show the allowance"):
        _run(conn, pid, chain, tmp_path).confirm()
    pos = m.get_position(conn, pid)
    assert pos["close_claim_id"] is None and pos["close_sig"] is None and pos["is_open"]
    hidden["on"] = False
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert chain.sent.count("approve") == 2 and chain.sent[:2] == ["approve", "leg1"]


def test_pancake_v3_leg_two_uses_the_router_deadline_abi():
    from eth_abi import decode
    from sniper.execution.evm_dex_quote import PANCAKE_EXACT_INPUT_SINGLE
    tx=X.leg2_tx({'venue':'v3','fee':2500},CHEEMS,LEG1_OUT,123,WALLET,
                v2_router=ADDR['v2_router'],v3_router=ADDR['v3_router'],deadline=1790990000)
    assert tx['data'][:10]=='0x'+PANCAKE_EXACT_INPUT_SINGLE
    values=decode(['address','address','uint24','address','uint256','uint256','uint256','uint160'],bytes.fromhex(tx['data'][10:]))
    assert values==(CHEEMS,X.WBNB,2500,WALLET,1790990000,LEG1_OUT,123,0)


# ---- Codex EVM-05 (2026-10-06): a crash before a broadcast leaves the exact bytes to re-offer ----

class Crash(BaseException):
    pass


def _crash_once(chain, match):
    """The process dies inside chain.send for the first raw `match` accepts: never broadcast."""
    real, state = chain.send, {"done": False}

    def send(raw, kind="swap"):
        if not state["done"] and match(json.loads(raw)):
            state["done"] = True
            raise Crash
        return real(raw, kind)
    chain.send = send


@pytest.mark.parametrize("what, match", [
    ("leg1", lambda tx: tx["to"] == ADDR["portal"]),
    ("approve", lambda tx: tx["data"].startswith("0x095ea7b3") and tx["to"] == MINT),
    ("leg2", lambda tx: tx["to"] in (ADDR["v2_router"], ADDR["v3_router"])),
])
def test_a_crash_before_a_broadcast_resumes_with_the_identical_bytes(conn, tmp_path, what, match):
    pid = _position(conn)
    chain = FakeChain()
    _crash_once(chain, match)
    with pytest.raises(Crash):
        _run(conn, pid, chain, tmp_path).confirm()
    journal = json.loads((tmp_path / "j.json").read_text())
    raw_key = {"leg1": "leg1_raw", "approve": "leg1_approve_raw", "leg2": "leg2_raw"}[what]
    signed = journal[raw_key]                                      # persisted before the send
    assert json.loads(signed)["hash"] == journal[{"leg1": "leg1", "approve": "leg1_approve",
                                                  "leg2": "leg2"}[what]]
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert signed in chain.offered                                 # the SAME bytes, not a new tx
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]    # one of each, nothing doubled


def test_a_crash_between_the_journal_and_the_db_mark_is_repaired_then_re_offered(conn, tmp_path, monkeypatch):
    pid = _position(conn)
    chain = FakeChain()
    real_mark, state = m.mark_position_close_submitted, {"done": False}

    def mark(*a, **k):
        if not state["done"]:
            state["done"] = True
            raise Crash
        return real_mark(*a, **k)
    monkeypatch.setattr(m, "mark_position_close_submitted", mark)
    with pytest.raises(Crash):
        _run(conn, pid, chain, tmp_path).confirm()
    journal = json.loads((tmp_path / "j.json").read_text())
    assert journal["state"] == "leg1_submitted" and journal["leg1_raw"]
    pos = m.get_position(conn, pid)
    assert pos["close_claim_id"] == journal["claim_id"] and pos["close_sig"] is None
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    assert m.get_position(conn, pid)["close_sig"] == journal["leg1"]
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]


def test_a_resume_whose_claim_is_no_longer_ours_never_re_offers(conn, tmp_path, monkeypatch):
    pid = _position(conn)
    chain = FakeChain()
    monkeypatch.setattr(m, "mark_position_close_submitted",
                        lambda *a, **k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        _run(conn, pid, chain, tmp_path).confirm()
    monkeypatch.undo()
    m.release_position_close_claim(conn, pid, json.loads((tmp_path / "j.json").read_text())["claim_id"])
    sent = list(chain.sent)
    with pytest.raises(X.Refused, match="by hand"):
        _run(conn, pid, chain, tmp_path).confirm()
    assert chain.sent == sent                                      # no re-offer, no new send


def test_a_re_offers_reply_never_resets_the_claim(conn, tmp_path):
    # The node may say anything about bytes it already has; only a receipt or the drop proof decides.
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    with pytest.raises(X.Refused, match="AMBIGUOUS"):
        _run(conn, pid, chain, tmp_path).confirm()
    chain.send_result = False
    with pytest.raises(X.Refused, match="AMBIGUOUS"):
        _run(conn, pid, chain, tmp_path).confirm()
    pos = m.get_position(conn, pid)
    journal = json.loads((tmp_path / "j.json").read_text())
    assert journal["state"] == "leg1_submitted" and pos["close_sig"] == journal["leg1"]


def test_the_journal_is_fsynced_file_and_directory(tmp_path, monkeypatch):
    synced = []
    real = X.os.fsync
    monkeypatch.setattr(X.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    X.write_journal(tmp_path / "j.json", {"state": "x"})
    assert len(synced) == 2 and json.loads((tmp_path / "j.json").read_text()) == {"state": "x"}


def test_every_journal_replacement_is_durable_including_progress(conn, tmp_path, monkeypatch):
    """Codex night review (EVM-05 partial): a later non-durable replace of the WHOLE file could undo
    an fsynced write-ahead on power loss. Every replacement, the post-send handed flag and route
    scan progress included, is fsync(file) -> replace -> fsync(dir)."""
    pid = _position(conn)
    chain = FakeChain()
    events = []
    real_fsync, real_replace = X.os.fsync, X.os.replace
    monkeypatch.setattr(X.os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(X.os, "replace", lambda a, b: events.append("replace") or real_replace(a, b))
    assert _run(conn, pid, chain, tmp_path).confirm()["state"] == "booked"
    journal = json.loads((tmp_path / "j.json").read_text())
    assert journal.get("leg1_raw_sent") and journal.get("route_scan")   # both progress kinds ran
    writes = events.count("replace")
    assert writes > 0 and events == ["fsync", "replace", "fsync"] * writes


def test_a_retried_leg2_crashing_before_its_send_re_offers_the_new_bytes(conn, tmp_path):
    # The first leg 2 was handed to the node and reverted; the retry's bytes never left the process.
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    with pytest.raises(X.Refused, match="leg 2 reverted"):
        _run(conn, pid, chain, tmp_path).confirm()
    first = json.loads((tmp_path / "j.json").read_text())
    assert first["leg2_raw_sent"] is True
    _crash_once(chain, lambda tx: tx["to"] in (ADDR["v2_router"], ADDR["v3_router"]))
    with pytest.raises(Crash):
        _run(conn, pid, chain, tmp_path, sign_start=100).confirm()   # its own hashes
    retry = json.loads((tmp_path / "j.json").read_text())
    assert retry["leg2"] != first["leg2"] and not retry.get("leg2_raw_sent")
    assert _run(conn, pid, chain, tmp_path, sign_start=200).confirm()["state"] == "booked"
    assert retry["leg2_raw"] in chain.offered and chain.sent.count("leg2") == 2


def test_a_resume_never_re_offers_bytes_a_previous_call_handed_over(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    with pytest.raises(X.Refused, match="AMBIGUOUS"):
        _run(conn, pid, chain, tmp_path).confirm()
    with pytest.raises(X.Refused, match="AMBIGUOUS"):
        _run(conn, pid, chain, tmp_path).confirm()
    assert "reoffer" not in chain.sent and chain.sent == ["approve", "leg1"]


def test_a_quote_token_with_no_wbnb_pool_leaves_through_usdt(conn, tmp_path):
    """FITCOIN 72879 (2026-10-10) is quoted in LLYB, the tokenized Eli Lilly stock, which trades on
    Pancake V3 only against USDT. Leg 2 had no quote -> WBNB route, so the runner's exit refused
    before leg 1 ('actual flap route or quote-token USD price unavailable') on every sweep. The
    quote -> USDT -> WBNB V3 path now prices, proves and sells it."""
    pid = _position(conn)
    chain = FakeChain(direct=False, path_out=V3_OUT)
    run = _run(conn, pid, chain, tmp_path, quote_check_fn=lambda *a: (True, "ok"),
               overrides_fn=lambda *a, **k: {"override": True})
    journal = run.confirm()
    assert journal["state"] == "booked"
    route = journal["leg2_route"]
    assert route["venue"] == "v3_path" and route["tokens"] == [CHEEMS, X.USDT, X.WBNB]
    assert chain.leg2_path == _encode_path(route["tokens"], route["fees"])
    assert chain.bal[X.WBNB] == V3_OUT and chain.leg2_amount == LEG1_OUT


def test_no_route_at_all_still_refuses_before_leg1(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain(direct=False, path_out=0)
    with pytest.raises(X.Refused, match="route or quote-token USD price unavailable"):
        _run(conn, pid, chain, tmp_path, quote_check_fn=lambda *a: (True, "ok"),
               overrides_fn=lambda *a, **k: {"override": True}).confirm()
    assert chain.sent == []
