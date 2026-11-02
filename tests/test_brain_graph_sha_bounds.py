from urllib.parse import parse_qs
import pytest

from bot.brain_graph.store import RestStore


def test_batch_commit_entity_expansion_is_one_capped_read_with_unknown_tail():
    token='ab12cd34'
    calls=[]
    def request(method,path,body,headers,timeout):
        calls.append(path)
        if path.startswith('brain_entities?'):return [dict(id='entity',name_lc=token+'e'*32)]
        if path.startswith('rpc/brain_facts_for_entities?'):
            query=parse_qs(path.split('?',1)[1])
            if 'id' in query:return []
            return [dict(id='f%04d'%i,entity_ids=['entity'],evidence=[],recorded_at='') for i in range(int(query['limit'][0]))]
        return []
    result=RestStore(request=request).facts_by_shas([token],page=2)[token]
    assert len(calls)==3
    assert len(result['facts'])==2 and result['complete'] is False


def test_single_sha_entity_expansion_is_one_capped_read():
    token='ab12cd34'+'e'*32
    calls=[]
    def request(method,path,body,headers,timeout):
        calls.append(path)
        if path.startswith('brain_entities?'):return [dict(id='entity',name_lc=token)]
        if path.startswith('rpc/brain_facts_for_entities?'):
            query=parse_qs(path.split('?',1)[1])
            if 'id' in query:return []
            return [dict(id='f%04d'%i,entity_ids=['entity'],evidence=[],recorded_at='') for i in range(int(query['limit'][0]))]
        return []
    result=RestStore(request=request).facts_by_sha(token,limit=2)
    assert len(calls)<=8
    assert sum(p.startswith('rpc/brain_facts_for_entities?') for p in calls)==1
    assert len(result)==2


def test_batch_page_never_exceeds_the_configured_gateway_cap():
    token='ab12cd34';limits=[]
    def request(method,path,body,headers,timeout):
        if path.startswith('brain_facts?'):
            limit=int(parse_qs(path.split('?',1)[1])['limit'][0]);limits.append(limit)
            return [dict(id=str(i),entity_ids=[],evidence=[dict(type='sha',ref=token)],recorded_at='') for i in range(min(limit,1000))]
        return []
    result=RestStore(request=request).facts_by_shas([token],per_limit=2000,page=2000)[token]
    assert result['complete'] is False and limits==[1000]


@pytest.mark.parametrize('page',[0,-1,False,1.5])
def test_batch_page_must_be_a_positive_integer(page):
    with pytest.raises(ValueError):RestStore(request=lambda *a,**k:pytest.fail('invalid page made a request')).facts_by_shas(['ab12cd34'],page=page)
