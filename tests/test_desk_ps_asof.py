"""PS as-of refresh: Gamma's cut recipe, typed refusals, no unknown-as-zero. Offline, local files only."""
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_ps_asof as A
from sniper.services import memecoin_desk_ps_metadata as MD
from sniper.services import memecoin_desk_pumpswap_candidates as P
from sniper.services import memecoin_desk_pumpswap as PS
from solders.pubkey import Pubkey
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_contract import rehash

MINT = U.b58encode(bytes([1]) * 32)
POOL = S.pumpswap_pool_address(MINT)
SIG = U.b58encode(bytes([3]) * 64)
SIG2 = U.b58encode(bytes([4]) * 64)
NOW = 1_800_000_000.0
DAY = "2027-01-15"                                                       # NOW's UTC day

def candidate():
    book = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    mapping = {"pool": POOL, "mint": MINT, "quote_mint": K.WSOL_MINT, "known_at": NOW - 600, "slot": 1, "canonical": True}
    meta = {"pool": POOL, "mint": MINT, "quote_mint": K.WSOL_MINT, "base_decimals": 6, "supply_atomic": "1000000000000000",
            "known_at": NOW - 500, "slot": 2, "source": "fixture"}

    def ev(q):
        return {"kind": "buy", "pool": POOL, "post_base_reserve_atomic": "100000000000000", "post_quote_reserve_atomic": q,
                "lp_fee_bps": 20, "protocol_fee_bps": 5, "creator_fee_bps": 5, "cashback_fee_bps": 0,
                "virtual_quote_atomic": "0", "virtual_quote_source": A.VQ_SOURCE}
    for q, slot, now in (("20000000000", 10, NOW - 400), ("40000000000", 11, NOW - 399)):
        c = book.on_swap(event=ev(q), mapping=mapping, metadata=meta, slot=slot, signature=SIG, ordinal=0, tx_index=None,
                         now=now, received_at=now, sol_usd=100, sol_usd_at=now - 1)
    assert c is not None
    return c


def build(tmp, *, swaps=None, blocks=((20, NOW - 1.0),), gaps_ps=(), gaps=(), heartbeat=None, metadata="ok", meta_slot=15,
          meta_at=NOW - 30, maps=None):
    desk = tmp / "desk"
    (desk / "pumpswap").mkdir(parents=True)
    for folder in ("blocks", "gaps"):
        (desk / folder).mkdir()
    hb = {"ts": NOW - 0.5, "connected": True, "pumpswap_capture": True, "pumpswap_emissions": True, "ps_gap_pending": False,
          "ps_committed_through": NOW - 0.5, "sol_usd": 100.0, "sol_age_s": 2.0}
    hb.update(heartbeat or {})
    (desk / "universe.heartbeat").write_text(json.dumps(hb))
    c = sqlite3.connect(str(desk / "blocks" / (DAY + ".db")))
    c.execute("CREATE TABLE blocks(slot INTEGER PRIMARY KEY, block_time INTEGER, received_at REAL)")
    c.executemany("INSERT INTO blocks VALUES(?,?,?)", [(s, 0, r) for s, r in blocks])
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "gaps" / (DAY + ".db")))
    c.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    c.executemany("INSERT INTO gaps VALUES(?,?,?)", [(a, "x", 0) for a in gaps])
    c.commit()
    c.close()
    t = sqlite3.connect(str(desk / "pumpswap" / "tape.db"))
    t.execute("CREATE TABLE swaps_ps(pool TEXT, mint TEXT, quote_mint TEXT, slot INTEGER, signature TEXT, ordinal INTEGER, "
              "tx_index INTEGER, is_buy INTEGER, actor TEXT, coin_creator TEXT, base_atomic TEXT, quote_atomic TEXT, "
              "pre_base_reserve_atomic TEXT, pre_quote_reserve_atomic TEXT, post_base_reserve_atomic TEXT, "
              "post_quote_reserve_atomic TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, creator_fee_bps TEXT, "
              "cashback_fee_bps TEXT, buyback_fee_bps TEXT, event_time INTEGER, block_time INTEGER, received_at REAL, "
              "processed_at REAL, map_known_at REAL, map_status TEXT, payload_json TEXT, payload_b64 TEXT, network_id TEXT, "
              "venue TEXT, program TEXT, PRIMARY KEY(signature,ordinal))")
    t.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
    t.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    t.execute("CREATE TABLE pools(pool TEXT, mint TEXT, quote_mint TEXT, pool_creator TEXT, canonical INTEGER, "
              "slot INTEGER, signature TEXT, ordinal INTEGER, tx_index INTEGER, event_time INTEGER, block_time INTEGER, "
              "received_at REAL, processed_at REAL, known_at REAL, payload_json TEXT, payload_b64 TEXT, "
              "network_id TEXT, venue TEXT, program TEXT, PRIMARY KEY(signature,ordinal))")
    t.execute("CREATE INDEX pools_pool ON pools(pool)")
    t.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    for m in ([mapping()] if maps is None else maps):
        t.execute("INSERT INTO pools VALUES(" + ",".join("?" * 19) + ")", (
            m['pool'],m['mint'],m['quote_mint'],m['creator'],m['canonical'],m['slot'],m['signature'],
            m['ordinal'],m['tx_index'],0,0,m['received_at'],m['known_at'],m['known_at'],'{}','',
            'solana','pumpswap',PS.AMM_PROGRAM))
    t.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    t.executemany("INSERT INTO gaps_ps VALUES(?,?,?)", [(a, "x", 0) for a in gaps_ps])
    for sw in (swaps if swaps is not None else [swap()]):
        t.execute("INSERT INTO swaps_ps VALUES(" + ",".join("?" * 32) + ")",
                  (sw["pool"], sw["mint"], sw["quote_mint"], sw["slot"], sw["sig"], sw["ordinal"], sw["tx_index"], sw["is_buy"],
                   "a", "cc", "1", "1", "1", "1", sw["post_base"], sw["post_quote"], sw["lp"], sw["prot"], sw["crt"], sw["cashback"], "0",
                   0, 0, sw["received_at"], sw["received_at"], sw["map_known_at"], sw["map_status"], sw["payload"], "", "solana",
                   "pumpswap", "p"))
    t.commit()
    t.close()
    if metadata == "ok":
        m = MD.connect(desk / "pumpswap" / "metadata.db")
        m.execute("INSERT INTO mints VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                  (MINT, 6, "1000000000000000", "spl-token", None, None, meta_at, meta_slot, MD.SOURCE))
        m.commit()
        m.close()
    return desk


def mapping(**over):
    authority=Pubkey.find_program_address([b'pool-authority',bytes(Pubkey.from_string(MINT))],
                                        Pubkey.from_string(U.PUMP_PROGRAM))[0]
    row={'pool':POOL,'mint':MINT,'quote_mint':K.WSOL_MINT,'creator':str(authority),'canonical':1,
         'slot':1,'signature':U.b58encode(bytes([2])*64),'ordinal':0,'tx_index':1,
         'received_at':NOW-600,'known_at':NOW-600}
    row.update(over)
    return row


def vqp(ix, raw, **over):
    p = {"ix_name": ix, "virtual_quote_reserves": raw, "virtual_quote_atomic": str(raw), "virtual_quote_source": A.VQ_SOURCE}
    p.update(over)
    return json.dumps(p)


def swap(**kw):
    base = {"pool": POOL, "mint": MINT, "quote_mint": K.WSOL_MINT, "slot": 18, "sig": SIG, "ordinal": 0, "tx_index": 5,
            "is_buy": 1, "post_base": "100000000000000", "post_quote": "40000000000", "lp": "20", "prot": "5", "crt": "5", "cashback": "0",
            "received_at": NOW - 10.0, "map_known_at": NOW - 600, "map_status": "mapped",
            "payload": vqp("buy", 0)}
    base.update(kw)
    return base


def refresh(tmp, **kw):
    desk = build(tmp, **kw)
    return A.refresh(candidate(), desk_dir=desk, now=NOW)


def refuses(tmp, reason, **kw):
    with pytest.raises(StaleSnapshot) as exc:
        refresh(tmp, **kw)
    assert exc.value.reason == reason


def test_happy_path_flat_state_and_provenance(tmp_path):
    out, meta = refresh(tmp_path)
    state = out["snapshot"]["fields"]["pool_state"]["value"]
    assert state["reserve_known_at"] == NOW - 10.0 and state["virtual_quote_known_at"] == NOW - 10.0
    assert state["virtual_quote_slot"] == 18 and state["virtual_quote_source"] == A.VQ_SOURCE
    assert state["sol_usd"] == 100.0 and state["sol_age_s"] == pytest.approx(2.5)
    assert state["metadata_known_at"] == NOW - 30 and state["metadata_slot"] == 15 and "venue" not in state
    assert out["ticket_id"] == candidate()["ticket_id"] and out["venue"] == "pumpswap"
    assert all(out["snapshot"]["fields"][k]["known_at"] == NOW for k in ("pool_state","mcap_usd","liq_usd"))
    assert state["cut_slot"] == 21
    assert out["snapshot"]["fields"]["mcap_usd"]["value"] == 40000 and meta["supply_label"] == "supply_at_read"
    assert meta["cut_slot"] == 21


def test_original_candidate_not_edited(tmp_path):
    c = candidate()
    before = json.dumps(c, sort_keys=True)
    A.refresh(c, desk_dir=build(tmp_path), now=NOW)
    assert json.dumps(c, sort_keys=True) == before


def test_cut_slot_excludes_swap_at_or_after_cut(tmp_path):
    # newest known block slot 20 => cut 21: a swap in slot 21 (block not yet seen) is excluded, falls back to NO newer swap
    refuses(tmp_path, "no_swaps_before_cut", swaps=[swap(slot=21)])
    out, _ = refresh(tmp_path / "b", swaps=[swap(slot=20)])                      # slot == max block slot is usable
    assert out["snapshot"]["fields"]["pool_state"]["value"]["slot"] == 20


def test_received_at_equal_to_as_of_excluded(tmp_path):
    refuses(tmp_path, "no_swaps_before_cut", swaps=[swap(received_at=NOW)])


def test_block_received_at_as_of_does_not_count(tmp_path):
    refuses(tmp_path, "no_block_before_as_of", blocks=((20, NOW),))


def test_latest_sell_refuses_no_prior_buy_fallback(tmp_path):
    refuses(tmp_path, "latest_swap_not_buy", swaps=[swap(slot=17, sig=SIG), swap(slot=18, sig=SIG2, is_buy=0)])


def test_same_slot_ambiguity_refuses(tmp_path):
    refuses(tmp_path, "same_slot_ambiguous", swaps=[swap(tx_index=None)])
    refuses(tmp_path / "b", "same_slot_ambiguous", swaps=[swap(sig=SIG, tx_index=5), swap(sig=SIG2, tx_index=5)])
    refuses(tmp_path / "c", "same_slot_ambiguous", swaps=[swap(sig=SIG, tx_index=5), swap(sig=SIG2, tx_index=None)])


def test_later_event_same_transaction_is_the_tip_and_never_inherits(tmp_path):
    refuses(tmp_path, "virtual_quote_unproven",
            swaps=[swap(ordinal=0), swap(ordinal=1, payload=vqp("buy_v2", 0))])
    refuses(tmp_path / "b", "latest_swap_not_buy", swaps=[swap(ordinal=0), swap(ordinal=1, is_buy=0)])


def test_virtual_quote_needs_marker_atomic_equality_legacy_ix_range(tmp_path):
    bad = (vqp("buy_v2", 0), json.dumps({"ix_name": "buy"}), vqp("buy", -1), vqp("buy", 2**64),
           vqp("buy", "0"), vqp("buy", 5, virtual_quote_atomic="6"), vqp("buy", 5, virtual_quote_source="x"),
           json.dumps({"ix_name": "buy", "virtual_quote_reserves": 0}),            # raw only, no marker/atomic
           json.dumps({"ix_name": "buy", "virtual_quote_reserves": 5, "virtual_quote_atomic": "5"}), "not json", None)
    for i, payload in enumerate(bad):
        refuses(tmp_path / str(i), "virtual_quote_unproven", swaps=[swap(payload=payload)])
    out, _ = refresh(tmp_path / "ok", swaps=[swap(payload=vqp("buy_exact_quote_in", 2**64 - 1))])
    assert out["snapshot"]["fields"]["pool_state"]["value"]["virtual_quote_atomic"] == str(2**64 - 1)


def test_unknown_fee_and_reserve_refuse_not_zero(tmp_path):
    refuses(tmp_path, "fee_unknown", swaps=[swap(lp=None)])
    refuses(tmp_path / "b", "reserve_unknown", swaps=[swap(post_quote=None)])
    refuses(tmp_path / "c", "reserve_unknown", swaps=[swap(post_base="0")])


def test_reserve_age_bound(tmp_path):
    refuses(tmp_path, "reserve_stale", swaps=[swap(received_at=NOW - 61)], meta_at=NOW - 100)
    refresh(tmp_path / "b", swaps=[swap(received_at=NOW - 59)], meta_at=NOW - 100)


def test_heartbeat_rules(tmp_path):
    refuses(tmp_path / "1", "tape_stale", heartbeat={"ts": NOW - 6})
    refuses(tmp_path / "2", "tape_stale", heartbeat={"ts": NOW + 1})                  # future stamp
    refuses(tmp_path / "3", "tape_stale", heartbeat={"connected": False})
    refuses(tmp_path / "4", "ps_capture_off", heartbeat={"pumpswap_capture": False})
    refuses(tmp_path / "5", "ps_gap_pending", heartbeat={"ps_gap_pending": True})
    refuses(tmp_path / "6", "ps_gap_pending", heartbeat={"ps_gap_pending": None})
    refuses(tmp_path / "7", "sol_usd_stale", heartbeat={"sol_age_s": 119.9})          # 119.9 + 0.5 > 120
    refuses(tmp_path / "8", "sol_usd_stale", heartbeat={"sol_usd": 0})
    refuses(tmp_path / "9", "sol_usd_stale", heartbeat={"sol_usd": None})


def test_gap_after_cut_block_or_tip_refuses(tmp_path):
    refuses(tmp_path, "tape_gap_since_cut", gaps_ps=[NOW - 5])
    refuses(tmp_path / "b", "tape_gap_since_cut", gaps=[NOW - 5])
    refresh(tmp_path / "c", gaps_ps=[NOW - 100])                                       # gap before both anchors: fine


def test_metadata_unknown_cases_are_typed(tmp_path):
    refuses(tmp_path, "metadata_not_yet_known", metadata=None)
    refuses(tmp_path / "b", "metadata_not_yet_known", meta_at=NOW)                     # receipt == as_of excluded
    refuses(tmp_path / "c", "metadata_not_yet_known", meta_at=NOW + 5)                 # later row never leaks backwards
    refuses(tmp_path / "d", "metadata_not_yet_known", meta_slot=21)
    out,_=refresh(tmp_path / "e",meta_slot=19,meta_at=NOW-5)
    state=out['snapshot']['fields']['pool_state']['value']
    assert state['metadata_slot']==19 and state['metadata_known_at']==NOW-5
    assert state['reserve_known_at']==NOW-10 and state['cut_slot']==21


def test_unmapped_pool_and_wrong_mint_refuse(tmp_path):
    refuses(tmp_path, "unmapped_pool", swaps=[swap(map_status="unmapped")])
    refuses(tmp_path / "b", "unmapped_pool", swaps=[swap(mint=U.b58encode(bytes([9]) * 32))])
    refuses(tmp_path / "c", "unmapped_pool", swaps=[swap(map_known_at=NOW)])


def test_non_ps_and_early_clock_refuse(tmp_path):
    with pytest.raises(StaleSnapshot) as exc:
        A.refresh({"schema": "desk-candidate/v1"}, desk_dir=tmp_path, now=NOW)
    assert exc.value.reason == "not_a_ps_candidate"
    with pytest.raises(StaleSnapshot) as exc:
        A.refresh(candidate(), desk_dir=build(tmp_path), now=NOW - 1000)
    assert exc.value.reason == "clock_before_emission"


def test_read_only_no_writes(tmp_path):
    desk = build(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in desk.rglob("*") if p.is_file() and p.suffix == ".db"}
    A.refresh(candidate(), desk_dir=desk, now=NOW)
    assert before == {p: p.stat().st_mtime_ns for p in desk.rglob("*") if p.is_file() and p.suffix == ".db"}


def test_same_slot_ambiguity_outside_the_old_sixteen_row_page_refuses(tmp_path):
    many=[swap(sig=U.b58encode(bytes([i+10])*64),tx_index=i+2) for i in range(16)]
    refuses(tmp_path,'same_slot_ambiguous',swaps=many+[swap(sig=SIG2,tx_index=None)])
    hidden=[swap(sig=SIG,tx_index=1),swap(sig=SIG2,tx_index=1)]
    refuses(tmp_path/'b','same_slot_ambiguous',swaps=many+hidden)


def test_pool_maps_are_revalidated_at_the_cut(tmp_path):
    refuses(tmp_path/'missing','unmapped_pool',maps=[])
    refuses(tmp_path/'noncanonical','unmapped_pool',maps=[mapping(canonical=0)])
    refuses(tmp_path/'creator','unmapped_pool',maps=[mapping(creator=MINT)])
    late=mapping(known_at=NOW-5,received_at=NOW-20)
    refuses(tmp_path/'late','unmapped_pool',maps=[late],swaps=[swap(map_known_at=NOW-5)])
    other=U.b58encode(bytes([8])*32)
    conflict=mapping(mint=other,canonical=0,slot=19,signature=SIG2,known_at=NOW-2,received_at=NOW-2)
    refuses(tmp_path/'conflict','pool_map_conflict',maps=[mapping(),conflict])
    # A map not known before the decision cut cannot poison its earlier state.
    refresh(tmp_path/'future',maps=[mapping(),{**conflict,'known_at':NOW,'received_at':NOW}])


def test_bad_clock_and_bounds_are_typed(tmp_path):
    desk=build(tmp_path);c=candidate()
    for now in (None,True,float('inf'),10**1000):
        with pytest.raises(StaleSnapshot): A.refresh(c,desk_dir=desk,now=now)
    for key in ('reserve_max_age_s','sol_max_age_s'):
        for bound in (float('nan'),float('inf'),-1,True):
            with pytest.raises(StaleSnapshot,match='invalid_age_bound'):
                A.refresh(c,desk_dir=desk,now=NOW,**{key:bound})


def test_connect_failure_and_disappearing_metadata_are_typed(tmp_path,monkeypatch):
    desk=build(tmp_path);c=candidate()
    original=A._ro
    monkeypatch.setattr(A,'_ro',lambda p:None if str(p).endswith('metadata.db') else original(p))
    with pytest.raises(StaleSnapshot,match='metadata_not_yet_known'):A.refresh(c,desk_dir=desk,now=NOW)
    monkeypatch.setattr(A,'_ro',original)
    (desk/'pumpswap'/'metadata.db').unlink();(desk/'pumpswap'/'metadata.db').mkdir()
    with pytest.raises(StaleSnapshot,match='metadata_unreadable'):A.refresh(c,desk_dir=desk,now=NOW)


def test_giant_or_nonfinite_heartbeat_numbers_refuse(tmp_path):
    for n,(key,value) in enumerate((('sol_usd',10**1000),('sol_age_s',10**1000),('ts',10**1000),
                                  ('sol_usd',float('nan')),('sol_age_s',float('inf')))):
        with pytest.raises(StaleSnapshot):refresh(tmp_path/str(n),heartbeat={key:value})


def test_missing_or_partial_source_pool_state_refuses_before_io(tmp_path,monkeypatch):
    original=candidate()
    def no_io(*args):
        pytest.fail('incomplete source candidate reached disk reads')
    monkeypatch.setattr(A,'_heartbeat',no_io)
    for mode,reason in (('absent','unmapped_pool'),('null','unmapped_pool'),('partial','pool_state_incomplete')):
        c=deepcopy(original);fields=c['snapshot']['fields']
        if mode=='absent':
            del fields['pool_state']
        elif mode=='null':
            fields['pool_state'].update(value=None,completeness='missing')
        else:
            fields['pool_state'].update(completeness='partial')
            fields['pool_state']['value']['virtual_quote_atomic']=None
        c['snapshot']['missing']=sorted(k for k,v in fields.items()
                                         if v['value'] is None or v['completeness']!='complete')
        rehash(c)
        K.validate_candidate(c)
        before=deepcopy(c)
        with pytest.raises(StaleSnapshot,match=reason):
            A.refresh(c,desk_dir=tmp_path,now=NOW)
        assert c==before


@pytest.mark.parametrize('index',[0,-1])
def test_same_slot_map_needs_a_positive_distinct_signature_index(tmp_path,index):
    refuses(tmp_path,'unmapped_pool',maps=[mapping(slot=18,tx_index=index)])


def test_one_signature_cannot_claim_multiple_same_slot_indices(tmp_path):
    refuses(tmp_path,'same_slot_ambiguous',swaps=[swap(tx_index=5),swap(tx_index=6,ordinal=1)])


@pytest.mark.parametrize('change',[{'creator':MINT},{'canonical':0}])
def test_conflicting_creator_or_canonical_map_refuses(tmp_path,change):
    conflict=mapping(signature=SIG2,slot=19,received_at=NOW-2,known_at=NOW-2,**change)
    refuses(tmp_path,'pool_map_conflict',maps=[mapping(),conflict])


@pytest.mark.parametrize('watermark',[None,True,float('nan'),float('inf'),10**1000,NOW-.25,NOW-5.01],
                         ids=['null','bool','nan','infinite','overflow','after_heartbeat','stale'])
def test_unknown_future_or_stale_commit_watermark_refuses(tmp_path,watermark):
    refuses(tmp_path,'ps_commit_unproven',heartbeat={'ps_committed_through':watermark})


def test_missing_commit_watermark_refuses(tmp_path):
    desk=build(tmp_path)
    path=desk/'universe.heartbeat';hb=json.loads(path.read_text());del hb['ps_committed_through']
    path.write_text(json.dumps(hb))
    with pytest.raises(StaleSnapshot,match='ps_commit_unproven'):
        A.refresh(candidate(),desk_dir=desk,now=NOW)


@pytest.mark.parametrize('emission',[NOW-.25,NOW-.5],ids=['after_cut','equal_to_cut'])
def test_candidate_emission_cannot_follow_committed_tape_cut(tmp_path,emission):
    desk=build(tmp_path);c=candidate()
    c['emitted_at']=datetime.fromtimestamp(emission,timezone.utc).isoformat()
    c['snapshot']['emission_at']=emission;rehash(c);K.validate_candidate(c)
    original=deepcopy(c)
    if emission>NOW-.5:
        with pytest.raises(StaleSnapshot,match='candidate_after_tape_cut'):
            A.refresh(c,desk_dir=desk,now=NOW)
    else:
        out,meta=A.refresh(c,desk_dir=desk,now=NOW)
        assert meta['emission_at']==meta['receipt_cut']==emission
        assert out['ticket_id']==c['ticket_id']
    assert c==original


@pytest.mark.parametrize('offset',[0,.25])
def test_rows_at_or_after_commit_watermark_are_excluded(tmp_path,offset):
    cut=NOW-.5;received=cut+offset
    refuses(tmp_path/'blocks','no_block_before_as_of',blocks=((20,received),))
    refuses(tmp_path/'swaps','no_swaps_before_cut',swaps=[swap(received_at=received)])
    refuses(tmp_path/'maps','unmapped_pool',maps=[mapping(received_at=received)])
    # A later uncommitted row cannot poison the committed tip's ordering or pool identity.
    other=U.b58encode(bytes([8])*32)
    out,meta=refresh(tmp_path/'ok',swaps=[swap(),swap(sig=SIG2,tx_index=None,received_at=received)],
                     maps=[mapping(),mapping(mint=other,signature=SIG2,received_at=received,known_at=NOW-2)],
                     meta_slot=19,meta_at=NOW-.25)
    state=out['snapshot']['fields']['pool_state']['value']
    assert state['reserve_known_at']==NOW-10 and state['metadata_known_at']==NOW-.25
    assert meta['receipt_cut']==meta['committed_through']==cut
    assert meta['receipt_lag_s']==.5 and meta['lag_s']==399


def test_tape_queries_share_one_read_snapshot(tmp_path,monkeypatch):
    desk=build(tmp_path);path=desk/'pumpswap'/'tape.db'
    conn=sqlite3.connect(path);conn.execute('PRAGMA journal_mode=WAL');conn.close()
    original=A.PT.window_rows;changed=[]
    def interleave(conn,*args,**kwargs):
        rows=original(conn,*args,**kwargs)
        if kwargs.get('order','').startswith('slot DESC') and not changed:
            writer=sqlite3.connect(path)
            writer.execute('UPDATE pools SET canonical=0');writer.commit();writer.close();changed.append(True)
        return rows
    monkeypatch.setattr(A.PT,'window_rows',interleave)
    out,_=A.refresh(candidate(),desk_dir=desk,now=NOW)
    assert changed and out['snapshot']['fields']['pool_state']['value']['slot']==18


def test_gap_between_cut_block_and_tip_receipt_refuses(tmp_path):
    refuses(tmp_path,'tape_gap_since_cut',blocks=((20,NOW-20),),gaps_ps=[NOW-15])


def test_pool_map_known_after_swap_map_clock_refuses(tmp_path):
    refuses(tmp_path,'unmapped_pool',maps=[mapping(known_at=NOW-500)])


def test_nonzero_cashback_fee_refuses(tmp_path):
    refuses(tmp_path,'fee_unknown',swaps=[swap(cashback='1')])


def test_office_sheet_fee_basis_skips_an_exempt_tip_but_the_default_path_is_unchanged(tmp_path):
    (tmp_path/'a').mkdir();(tmp_path/'b').mkdir()
    swaps=[swap(slot=18,sig='old'+SIG[3:],tx_index=1,received_at=NOW-3.),swap(slot=19,lp='0',prot='0',crt='0',received_at=NOW-2.)]
    for sub,kw,want in (('a',dict(resolve_zero_fees=True),(20,5,5)),('b',{},(0,0,0))):
        d=build(tmp_path/sub,swaps=swaps)
        got,_=A.refresh(candidate(),desk_dir=d,now=NOW,**kw)
        s=got['snapshot']['fields']['pool_state']['value']
        assert (s['lp_fee_bps'],s['protocol_fee_bps'],s['creator_fee_bps'])==want and s.get('fee_basis')==('newest_nonzero' if kw else None)
