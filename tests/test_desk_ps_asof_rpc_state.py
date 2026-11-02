"""Office sheet fallback: when the tape tip cannot prove price (latest swap a sell, vq unproven, reserve stale) an exact
on-chain pool read (pumpswap_sell.read_pool_state shape) may supply reserves + virtual_quote; provenance is labelled."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_asof as A
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_ps_evidence import fixture, NOW


def sell_tip(tmp_path):
    desk, c = fixture(tmp_path)
    conn = sqlite3.connect(desk / "pumpswap/tape.db")
    conn.execute("UPDATE swaps_ps SET is_buy=0")
    conn.commit(); conn.close()
    return desk, c


def rpc_state(c, **over):
    v = c["snapshot"]["fields"]["pool_state"]["value"]
    s = dict(base_reserve=int(v["base_reserve_atomic"]), quote_reserve=int(v["quote_reserve_atomic"]),
             virtual_quote=int(v["virtual_quote_atomic"]), slot=int(v["slot"]) + 5, read_at=NOW - .6)
    s.update(over)
    return s


def go(c, desk, **kw):
    return A.refresh(c, desk_dir=desk, now=NOW - .5, **kw)


def test_a_sell_tip_is_still_refused_without_the_fallback(tmp_path):
    desk, c = sell_tip(tmp_path)
    with pytest.raises(StaleSnapshot, match="latest_swap_not_buy"):
        go(c, desk)


def test_the_exact_read_prices_a_sell_tip_and_labels_its_source(tmp_path):
    desk, c = sell_tip(tmp_path)
    out, _ = go(c, desk, rpc_state=rpc_state(c))
    st = out["snapshot"]["fields"]["pool_state"]["value"]
    assert st["virtual_quote_source"] == "pumpswap_rpc:pool_state" and st["slot"] == rpc_state(c)["slot"]
    assert out["snapshot"]["fields"]["mcap_usd"]["value"] > 0


@pytest.mark.parametrize("bad", [dict(read_at=NOW - 500), dict(read_at=NOW + 5), dict(slot=1), dict(base_reserve=0),
                                 dict(quote_reserve=True), dict(virtual_quote=-1), dict(slot=None)])
def test_an_unusable_read_is_refused_not_trusted(tmp_path, bad):
    desk, c = sell_tip(tmp_path)
    with pytest.raises(StaleSnapshot, match="rpc_state_unusable"):
        go(c, desk, rpc_state=rpc_state(c, **bad))


def test_a_buy_tip_never_uses_the_read(tmp_path):
    desk, c = fixture(tmp_path)
    out, _ = go(c, desk, rpc_state=rpc_state(c, base_reserve=1))
    st = out["snapshot"]["fields"]["pool_state"]["value"]
    assert st["virtual_quote_source"] == A.VQ_SOURCE and st["base_reserve_atomic"] != "1"


def test_a_tape_gap_before_the_read_does_not_block_an_exact_read(tmp_path):
    desk, c = sell_tip(tmp_path)
    conn = sqlite3.connect(desk / "pumpswap/tape.db")
    conn.execute("INSERT INTO gaps_ps VALUES(?,?,?)", (NOW - 0.8, "x", 0))        # after the cut block, before the read
    conn.commit(); conn.close()
    with pytest.raises(StaleSnapshot, match="latest_swap_not_buy"):
        go(c, desk)
    out, _ = go(c, desk, rpc_state=rpc_state(c))                                   # the read is newer than the gap
    assert out["snapshot"]["fields"]["pool_state"]["value"]["virtual_quote_source"] == "pumpswap_rpc:pool_state"


def test_gap_only_retry_uses_exact_read_even_when_buy_tip_is_healthy(tmp_path):
    desk,c=fixture(tmp_path)
    with sqlite3.connect(desk/'pumpswap/tape.db') as conn:
        conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-.8,'worker_start',0))
    with pytest.raises(StaleSnapshot,match='tape_gap_since_cut'):
        go(c,desk)
    out,_=go(c,desk,rpc_state=rpc_state(c))
    assert out['snapshot']['fields']['pool_state']['value']['virtual_quote_source']==A.RPC_SOURCE


@pytest.mark.parametrize('sell',[False,True])
def test_post_read_gap_still_refuses_for_both_buy_and_sell_tips(tmp_path,sell):
    desk,c=sell_tip(tmp_path) if sell else fixture(tmp_path)
    with sqlite3.connect(desk/'pumpswap/tape.db') as conn:
        conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-.55,'worker_start',0))
    with pytest.raises(StaleSnapshot,match='tape_gap_since_cut'):
        go(c,desk,rpc_state=rpc_state(c))


def test_pending_loss_at_read_time_still_refuses(tmp_path):
    import json
    from test_desk_ps_loss_integration import provision
    desk,c=fixture(tmp_path);provision(desk)
    path=desk/'universe.heartbeat';beat=json.loads(path.read_text())
    beat.update(ps_gap_pending=True,ps_pending_gaps={'decode_loss':NOW-.6});path.write_text(json.dumps(beat))
    with pytest.raises(StaleSnapshot,match='tape_gap_since_cut') as failure:
        go(c,desk,rpc_state=rpc_state(c),integrity=True)
    assert failure.value.pending_gaps=={'decode_loss':NOW-.6}
