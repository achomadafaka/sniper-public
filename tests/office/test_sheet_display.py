"""Display projection only: raw admission sheet and receipt remain byte-identical."""
import copy
import json
from types import SimpleNamespace
import pytest
from sniper.office import seat_tools as T


def sheet():
    chart={'bars':[{'at':i,'open':.000000123,'close':.000000124} for i in range(100)]}
    flow={'buy_n':0,'sell_n':2,'buy_usd':0.,'coverage':'partial'}
    buyers={'state':'unknown','reason':'no_source','actors':None}
    return dict(state='known',sheet=dict(chart=chart,flow_60s=flow,buyers=buyers,
        ps_facts={'chart':copy.deepcopy(chart),'flow_60s':copy.deepcopy(flow)},d_blocks={'buyers':copy.deepcopy(buyers)},
        safety={'top10_pct':0.,'creator_pct':None},safety_proof={'known_at':100.}),lenses={'ps_trending':{'state':'unknown','reason':'stale'}})


def call(raw):
    host=SimpleNamespace(paper=SimpleNamespace(read_sheet=lambda **kw:raw))
    out=T.SeatTools._sheet_read(host,'ticket')
    return json.loads(out['content'][0]['text']),out


def expand(value,root):
    if isinstance(value,dict):
        if set(value)=={'$ref'}:
            target=root
            for key in value['$ref'][2:].split('/'):target=target[key.replace('~1','/').replace('~0','~')]
            return copy.deepcopy(target)
        return {k:expand(v,root) for k,v in value.items()}
    if isinstance(value,list):return [expand(v,root) for v in value]
    return value


def test_duplicate_blocks_are_reversible_and_original_is_untouched():
    raw=sheet();before=json.dumps(raw,sort_keys=True)
    view,_=call(raw)
    assert view['sheet']['chart']=={'$ref':'#/sheet/ps_facts/chart'}
    assert view['sheet']['buyers']=={'$ref':'#/sheet/d_blocks/buyers'}
    assert expand(view['sheet'],view)==raw['sheet']
    assert json.dumps(raw,sort_keys=True)==before
    assert len(json.dumps(view))<len(before)*.8
    assert view['lenses']==raw['lenses']


def test_distinct_blocks_and_zero_vs_false_are_not_deduplicated():
    raw=sheet();raw['sheet']['chart']={'state':'unknown'}
    raw['sheet']['flow_60s']['buy_n']=False
    view,_=call(raw)
    assert view['sheet']['chart']=={'state':'unknown'}
    assert view['sheet']['flow_60s']['buy_n'] is False


@pytest.mark.parametrize('at,expected',[(100.,100.),(0.,0.),(None,None),(float('inf'),None),(True,None),(-1.,None),(float('nan'),None),(10**400,None)])
def test_holder_values_show_frozen_evidence_time_not_freshness(at,expected):
    raw=sheet();raw['sheet']['safety_proof']['known_at']=at
    view,_=call(raw);holders=view['holder_evidence']
    assert holders['top10_pct']['value']==0.
    assert holders['creator_pct']['value'] is None
    assert holders['top10_pct']['as_of']==expected
    assert holders['creator_pct']['observation_at'] is None
    assert 'frozen' in holders['note'] and 'not refreshed' in holders['note']
    assert 'admission evidence' in holders['note']
    assert holders['clock_basis']=='as_recorded_unix_seconds'


def test_missing_proof_is_unknown_and_never_uses_a_sheet_clock():
    raw=sheet();raw['sheet'].pop('safety_proof');raw['sheet']['clock']={'as_of':999.}
    view,_=call(raw)
    assert view['holder_evidence']['top10_pct']['as_of'] is None


def test_unknown_sheet_remains_unknown_without_display_facts():
    view,out=call({'state':'unknown','reason':'timeout','source_provenance':{'private':'x'}})
    assert view['state']=='unknown' and view['reason']=='timeout' and out['isError']
    assert 'holder_evidence' not in view


def test_compaction_precedes_existing_output_cap_and_retains_whole_chart():
    raw=sheet();chart={'bars':[{'at':i,'close':i/10000} for i in range(900)]}
    raw['sheet']['chart']=chart;raw['sheet']['ps_facts']['chart']=copy.deepcopy(chart)
    assert len(json.dumps(raw))>T.MAX_SHEET
    view,_=call(raw)
    assert view['state']=='known'
    assert expand(view['sheet'],view)==raw['sheet']
