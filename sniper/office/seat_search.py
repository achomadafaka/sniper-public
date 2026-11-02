"""Runs in the tool sandbox, stdlib only: one keyless web search through the seat proxy. Prints JSON. Results are hostile data.

Zero results require the engine's explicit empty marker; blocked, changed or incomplete pages read unknown."""
import base64
from html.parser import HTMLParser
import json
import sys
import socket
import urllib.error
import urllib.parse
import urllib.request

URL='https://html.duckduckgo.com/html/?q='
MAX_RESULTS=10
MAX_BYTES=1024*1024


class Results(HTMLParser):
    def __init__(self):
        super().__init__();self.items=[];self.field=None;self.depth=0;self.empty=False

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);classes=(attrs.get('class') or '').split()
        if 'no-results' in classes:self.empty=True
        if tag=='a' and 'result__a' in classes:
            self.items.append(dict(title='',url=_target(attrs.get('href') or ''),snippet=''));self.field='title'
        elif tag=='a' and 'result__snippet' in classes and self.items:self.field='snippet'

    def handle_endtag(self,tag):
        if tag=='a':self.field=None

    def handle_data(self,data):
        if self.field:self.items[-1][self.field]+=data


def _target(href):
    """The engine wraps each link in a redirect; the real URL is its uddg parameter."""
    parts=urllib.parse.urlsplit(href)
    wrapped=urllib.parse.parse_qs(parts.query).get('uddg')
    return wrapped[0] if wrapped else urllib.parse.urljoin('https://html.duckduckgo.com/',href)


def parse(body):
    if 'id="challenge-form"' in body or 'anomaly.js' in body:return dict(state='unknown',reason='challenge')
    parser=Results();parser.feed(body)
    items=[dict(title=i['title'].strip()[:300],url=i['url'][:2000],snippet=' '.join(i['snippet'].split())[:600]) for i in parser.items if i['url'].startswith('http')]
    if items:return dict(state='known',results=items[:MAX_RESULTS])
    if parser.empty:return dict(state='known',results=[])
    return dict(state='unknown',reason='result_markup_absent')


class BingResults(HTMLParser):
    def __init__(self):
        super().__init__();self.items=[];self.in_algo=0;self.field=None;self.tag=None

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);classes=(attrs.get('class') or '').split()
        if tag=='li' and 'b_algo' in classes:
            self.in_algo=1;self.items.append(dict(title='',url='',snippet=''))
        elif self.in_algo and tag=='h2':self.tag='h2'
        elif self.in_algo and tag=='a' and self.tag=='h2' and not self.items[-1]['url']:
            self.items[-1]['url']=_bing_target(attrs.get('href') or '');self.field='title'
        elif self.in_algo and tag=='p' and not self.items[-1]['snippet']:self.field='snippet'

    def handle_endtag(self,tag):
        if tag in ('a','p'):self.field=None
        if tag=='h2':self.tag=None
        if tag=='li':self.in_algo=0

    def handle_data(self,data):
        if self.field:self.items[-1][self.field]+=data


def _bing_target(href):
    """Bing wraps links as /ck/a?...&u=a1<base64url>. Anything else is the link itself."""
    wrapped=urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get('u')
    if wrapped and wrapped[0].startswith('a1'):
        try:return base64.urlsafe_b64decode(wrapped[0][2:]+'===').decode('utf-8')
        except ValueError:return ''
    return href


def parse_bing(body):
    parser=BingResults();parser.feed(body)
    items=[dict(title=' '.join(i['title'].split())[:300],url=i['url'][:2000],snippet=' '.join(i['snippet'].split())[:600]) for i in parser.items if i['url'].startswith('http')]
    return dict(state='known',results=items[:MAX_RESULTS]) if items else dict(state='unknown',reason='result_markup_absent')


TAVILY='https://api.tavily.com/search'


def _real_url(value):
    try:parts=urllib.parse.urlsplit(value) if isinstance(value,str) else None
    except ValueError:return False
    return bool(parts) and parts.scheme in ('http','https') and bool(parts.hostname)


def parse_tavily(body):
    """An empty list is a real no-hits answer. A non-empty list with nothing usable is a malformed answer, so the next engine gets its turn."""
    try:found=json.loads(body)['results']
    except (ValueError,KeyError,TypeError):return dict(state='unknown',reason='result_shape_unknown')
    if not isinstance(found,list):return dict(state='unknown',reason='result_shape_unknown')
    items=[dict(title=str(i.get('title',''))[:300],url=i['url'][:2000],snippet=' '.join(str(i.get('content','')).split())[:600]) for i in found if isinstance(i,dict) and _real_url(i.get('url'))]
    if found and not items:return dict(state='unknown',reason='result_shape_unknown')
    out=dict(state='known',results=items[:MAX_RESULTS])
    if len(items)<len(found):out['dropped_malformed']=len(found)-len(items)
    return out


# Order follows two control queries by Epsilon, 2026-10-30: Tavily relevant on both, Bing unrelated on both. `engine_prior` is that small sample, not a judgement of this answer.
# Each engine named here sees the seat's query text.
ENGINES=(('tavily',TAVILY,parse_tavily,'good_on_two_controls'),('duckduckgo',URL,parse,'untested'),('bing','https://www.bing.com/search?q=',parse_bing,'unrelated_on_two_controls'))


def _fetch(prefix,query):
    if prefix==TAVILY:
        request=urllib.request.Request(TAVILY,data=json.dumps(dict(query=query,max_results=MAX_RESULTS)).encode(),headers={'Content-Type':'application/json','X-Tavily-Access-Mode':'keyless'})
        with urllib.request.urlopen(request,timeout=20) as response:
            body=response.read(MAX_BYTES+1)
        return None if len(body)>MAX_BYTES else body.decode('utf-8','replace')
    request=urllib.request.Request(prefix+urllib.parse.quote_plus(query),headers={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0','Accept-Language':'en-US,en;q=0.9'})
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read(MAX_BYTES+1)
    return None if len(body)>MAX_BYTES else body.decode('utf-8','replace')


def search(query):
    """First engine with a real answer. Keyless engines challenge bots at will (DuckDuckGo did within an hour), so each failure is recorded, and all failing reads unknown."""
    tried={}
    for name,prefix,parser,quality in ENGINES:
        try:
            body=_fetch(prefix,query)
            out=dict(state='unknown',reason='response_too_large') if body is None else parser(body)
        except urllib.error.HTTPError as exc:
            refusal=exc.headers.get('X-Office-Refusal') if exc.headers else None
            # Only our proxy's fixed refusal vocabulary may become a reason.
            if refusal in ('budget_exhausted','search_paced','search_config_unknown','search_store_unknown','search_clock_unknown'):
                return dict(state='unknown',reason=refusal,failed=dict(tried,**{name:refusal}),**({'retry_after_s':5} if refusal=='search_paced' else {}))
            out=dict(state='unknown',reason='rate_limited' if exc.code==429 else 'http_%d'%exc.code)
        except (OSError,urllib.error.URLError) as exc:
            timeout=isinstance(exc,(TimeoutError,socket.timeout)) or isinstance(getattr(exc,'reason',None),(TimeoutError,socket.timeout))
            out=dict(state='unknown',reason='timeout' if timeout else 'transport_unknown')
        if out['state']=='known':return dict(out,engine=name,engine_prior=quality,failed=tried)
        tried[name]=out['reason']
    return dict(state='unknown',reason='all_engines_failed',failed=tried)


if __name__=='__main__':print(json.dumps(search(sys.argv[1])))
