"""One pacer per research key across PROCESSES (Alpha 2026-10-02): two processes never call inside
one gap, and a 429 seen by one pauses the others, including one already waiting for its slot."""
import multiprocessing as mp
import time

import pytest

from sniper.services.memecoin_research_pacer import SharedPacer

CTX = mp.get_context("spawn")


def _acquirer(path, gap, n, out, start_at):
    from sniper.services.memecoin_research_pacer import SharedPacer
    pacer = SharedPacer(path, gap_s=gap)
    while time.time() < start_at:
        time.sleep(0.001)
    for _ in range(n):
        slot = pacer.acquire()
        out.put((slot, time.time()))


def test_two_processes_never_get_slots_inside_one_gap(tmp_path):
    path, gap, n = str(tmp_path / "key.json"), 0.15, 6
    out = CTX.Queue()
    start = time.time() + 1.0
    procs = [CTX.Process(target=_acquirer, args=(path, gap, n, out, start)) for _ in range(2)]
    for p in procs:
        p.start()
    got = [out.get(timeout=20) for _ in range(2 * n)]
    for p in procs:
        p.join(10)
    slots = sorted(slot for slot, _called in got)
    assert all(b - a >= gap - 1e-9 for a, b in zip(slots, slots[1:])), slots
    assert all(called >= slot for slot, called in got)          # nobody calls before its slot


def _pauser(path, gap, armed, ready, out):
    from sniper.services.memecoin_research_pacer import SharedPacer
    pacer = SharedPacer(path, gap_s=gap)
    armed.set()
    ready.wait(20)
    out.put(pacer.pause(2.0))                                    # this process saw the 429


def _waiter(path, gap, armed, ready, out):
    from sniper.services.memecoin_research_pacer import SharedPacer
    pacer = SharedPacer(path, gap_s=gap)
    armed.wait(20)
    pacer._locked(lambda s: (None, dict(s, last=time.time() + 1.0)))   # its slot is 1 s out
    ready.set()
    out.put(("slot", pacer.acquire(), time.time()))


def test_a_429_in_one_process_pauses_another_already_waiting_for_its_slot(tmp_path):
    # The waiter reserves a slot 1 s out and sleeps; the other process sees a 429 and pauses 2 s.
    # The waiter must not call at its slot: it re-checks and waits the pause out.
    path, gap = str(tmp_path / "key.json"), 0.05
    out, armed, ready = CTX.Queue(), CTX.Event(), CTX.Event()
    pauser = CTX.Process(target=_pauser, args=(path, gap, armed, ready, out))
    waiter = CTX.Process(target=_waiter, args=(path, gap, armed, ready, out))
    pauser.start()
    waiter.start()
    got = [out.get(timeout=20) for _ in range(2)]
    for p in (waiter, pauser):
        p.join(10)
    until = next(x for x in got if not isinstance(x, tuple))
    (_kind, slot, called), = [x for x in got if isinstance(x, tuple)]
    assert slot >= until and called >= until, (slot, called, until)


def _one_acquire(path, out):
    from sniper.services.memecoin_research_pacer import SharedPacer
    out.put(("started", time.time()))
    SharedPacer(path, gap_s=0.01).acquire()
    out.put(("acquired", time.time()))


def test_a_reservation_waits_for_the_keys_lock_held_by_another_process(tmp_path):
    import fcntl
    import os
    path = str(tmp_path / "key.json")
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                           # another process mid-reservation
    out = CTX.Queue()
    proc = CTX.Process(target=_one_acquire, args=(path, out))
    proc.start()
    assert out.get(timeout=20)[0] == "started"
    time.sleep(0.5)
    released = time.time()
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    kind, at = out.get(timeout=20)
    proc.join(10)
    assert kind == "acquired" and at >= released


def test_a_state_from_the_future_never_blocks_for_long(tmp_path):
    path = str(tmp_path / "key.json")
    with open(path, "w") as f:
        f.write('{"last": 9e12, "paused_until": 9e12}')         # a clock step back, or garbage
    slept = []
    now = [1000.0]
    pacer = SharedPacer(path, gap_s=1.1, clock=lambda: now[0], sleep=lambda s: (
        slept.append(s), now.__setitem__(0, now[0] + s)))
    assert pacer.acquire() == 1000.0 and slept == []          # discarded, not waited out


@pytest.mark.parametrize("garbage", ["", "{", "[]", '{"last": "x"}'])
def test_a_corrupt_state_file_is_a_fresh_pacer(tmp_path, garbage):
    path = str(tmp_path / "key.json")
    with open(path, "w") as f:
        f.write(garbage)
    now = [1000.0]
    pacer = SharedPacer(path, gap_s=1.1, clock=lambda: now[0], sleep=lambda s: None)
    assert pacer.acquire() == 1000.0 and pacer.acquire() == 1001.1


def test_the_pacer_is_keyed_by_the_research_keys_id_never_its_url(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path))
    pacer = SharedPacer.for_url("https://mainnet.helius-rpc.com/?api-key=SECRETKEY123")
    assert "SECRETKEY123" not in pacer.path and pacer.path.startswith(str(tmp_path))
    assert SharedPacer.for_url("https://mainnet.helius-rpc.com/?api-key=SECRETKEY123").path == pacer.path
    assert SharedPacer.for_url("https://mainnet.helius-rpc.com/?api-key=OTHERKEY").path != pacer.path


def test_the_default_directory_is_per_user_never_a_checkout_or_release(tmp_path, monkeypatch):
    import os
    monkeypatch.delenv("MEMECOIN_RESEARCH_PACER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / "a-release"))
    pacer = SharedPacer.for_url("https://mainnet.helius-rpc.com/?api-key=K")
    assert os.path.dirname(pacer.path) == str(tmp_path / ".cache" / "sniper" / "research_pacer")


# ---- the readers share it: the slot-end shadow and funded_exit_truth (the wallet graph's reader) ----

class FakePacer:
    def __init__(self):
        self.acquired, self.paused = 0, []

    def acquire(self):
        self.acquired += 1

    def pause(self, seconds):
        self.paused.append(seconds)


def _http_error(code, body=b"Too Many Requests"):
    import io
    import urllib.error
    return urllib.error.HTTPError("https://k", code, "x", {}, io.BytesIO(body))


def test_the_slot_shadow_reader_paces_every_call_and_shares_a_429(monkeypatch):
    import json as _json
    import urllib.error
    import urllib.request
    from sniper.services import memecoin_slot_end_shadow as shadow
    pacer = FakePacer()
    rpc = shadow.ResearchRpc("https://k", pacer=pacer)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"result": 7}).encode()
    answers = [Resp(), _http_error(429)]

    def urlopen(req, timeout=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert rpc("getSlot", []) == 7
    with pytest.raises(urllib.error.HTTPError):
        rpc("getSlot", [])
    assert pacer.acquired == 2 and pacer.paused == [shadow.PAUSE_ON_429_S]


def test_the_shadow_reader_from_env_carries_the_keys_shared_pacer(tmp_path, monkeypatch):
    from sniper.services import memecoin_slot_end_shadow as shadow
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path))
    rpc, why = shadow.rpc_from_env({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://r.test/?api-key=K"})
    assert why is None and isinstance(rpc._pacer, SharedPacer)
    assert rpc._pacer.path == SharedPacer.for_url("https://r.test/?api-key=K").path


def test_funded_exit_truth_paces_each_attempt_and_pauses_everyone_on_a_rate_429(monkeypatch):
    import json as _json
    import sys
    import urllib.request
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
    import funded_exit_truth as fet
    pacer = FakePacer()
    monkeypatch.setattr(fet, "_URL", "https://k")
    monkeypatch.setattr(fet, "_PACER", pacer)
    answers = [_http_error(429), _json.dumps({"result": 3}).encode()]

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

    def urlopen(req, timeout=None):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return Resp(a)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert fet.rpc("getSlot", []) == 3
    assert pacer.acquired == 2 and pacer.paused == [6]


# ---- Gamma LOWs on f873eac80 ----

def test_a_shorter_pause_never_cuts_a_longer_one(tmp_path):
    now = [1000.0]
    pacer = SharedPacer(str(tmp_path / "key.json"), clock=lambda: now[0], sleep=lambda s: None)
    pacer.pause(60)                                          # the shadow's 429
    pacer.pause(6)                                           # then a script's rate 429
    assert pacer.paused_until() == 1060.0


def test_a_pause_past_the_cap_is_held_at_the_cap_never_dropped_as_garbage(tmp_path):
    now = [1000.0]
    slept = []
    pacer = SharedPacer(str(tmp_path / "key.json"), clock=lambda: now[0],
                        sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    pacer.pause(1000)
    assert pacer.paused_until() == 1600.0
    assert pacer.acquire() == 1600.0 and sum(slept) == 600.0


def test_every_keyed_drpc_url_takes_the_one_account_pacer_and_a_limit_pauses_everyone(tmp_path,
                                                                                     monkeypatch):
    """Gamma probe 2026-10-02: the free dRPC account allows ~120 requests per rolling 60 s
    ACCOUNT-WIDE, every chain counted; its limit answer is HTTP 429 code 15, gone in ~50 s. Every
    keyed URL, whatever its network, takes ONE pacer at <= 1.5/s; a limit answer pauses it 60 s;
    a keyless dRPC host and any other host pass untouched."""
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path))
    took, paused = [], []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append((self.path, self.gap_s)))
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: paused.append((self.path, s)))
    answers = iter([(None, "HTTP Error 429: Too Many Requests"), ("0x1", None), ("0x2", None),
                    ("0x3", None), (None, {"code": 15, "message": ""}),
                    (None, {"code": -32005, "message": "free plan rate limit"}),
                    (None, "timed out"), (None, "HTTP Error 403: Forbidden")])
    call = rp.drpc_paced(lambda url, method, params: next(answers))
    k56 = "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET"
    k4663 = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    for url in (k56, k4663, "https://robinhood.drpc.org", "https://1rpc.io/bnb", k56, k4663, k56,
                k4663):
        call(url, "eth_chainId", [])
    assert len(took) == 6 and len({path for path, _gap in took}) == 1, took
    assert {gap for _path, gap in took} == {1 / 1.5}
    assert "SECRET" not in took[0][0]
    assert paused == [(took[0][0], 60.0)] * 3, "429, code 15, a limit message; not a timeout or a 403"


def test_the_http_seam_form_pauses_the_account_on_a_429_status_a_raised_429_or_a_limit_reply(
        tmp_path, monkeypatch):
    import urllib.error
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path))
    took, paused = [], []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append(self.path))
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: paused.append(s))
    keyed = "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET"
    answers = [(429, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "x"}}, {}),
               (200, {"jsonrpc": "2.0", "error": {"code": 15, "message": ""}}, {}),
               urllib.error.HTTPError(keyed, 429, "Too Many Requests", {}, None),
               (200, {"jsonrpc": "2.0", "result": "0x38"}, {}),
               (200, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "execution reverted"}}, {}),
               urllib.error.HTTPError(keyed, 403, "Forbidden", {}, None)]

    def http(method, url, headers=None, body=None, timeout=10):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    paced = rp.drpc_paced_http(http)
    for _ in range(6):
        try:
            paced("POST", keyed, body="{}")
        except urllib.error.HTTPError:
            pass
    assert len(took) == 6 and paused == [60.0] * 3, (took, paused)
    answers.append((429, {}, {}))
    paced("POST", "https://robinhood.drpc.org", body="{}")      # keyless: its own budget, unpaced
    assert len(took) == 6 and len(paused) == 3


def test_json_batch_reader_shares_account_pacing_and_pauses_on_a_limited_reply(monkeypatch):
    from sniper.services import memecoin_research_pacer as rp
    called, paused = [], []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: called.append(self.path))
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, seconds: paused.append(seconds))
    url = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    reply = [{"id": 1, "result": "0x1"}, {"id": 2, "error": {"code": 15}}]
    assert rp.drpc_paced_json(lambda target, **kw: reply)(url, body=[]) is reply
    assert len(called) == 1 and paused == [60.0]


def test_json_reader_pauses_when_its_typed_failure_wraps_http_429(monkeypatch):
    import urllib.error
    from sniper.services import memecoin_research_pacer as rp
    from sniper.services.memecoin_relay_initiation import ProofError
    paused = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, seconds: paused.append(seconds))
    url = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    def fetch(target, **kwargs):
        raise ProofError("origin_rate_limit") from urllib.error.HTTPError(target, 429, "limited", {}, None)
    with pytest.raises(ProofError):
        rp.drpc_paced_json(fetch)(url)
    assert paused == [60.0]


def _direct_account_pacer(code):
    """Recognize a called canonical account pacer, never a source-text mention."""
    import ast
    for function in ast.walk(ast.parse(code)):
        if not isinstance(function,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
        names=set()
        for node in ast.walk(function):
            if not isinstance(node,ast.Assign) or not isinstance(node.value,ast.Call):continue
            call=node.value
            if (isinstance(call.func,ast.Attribute) and call.func.attr=='named'
                and isinstance(call.func.value,ast.Name) and call.func.value.id=='SharedPacer'
                and call.args and isinstance(call.args[0],ast.Constant) and call.args[0].value=='drpc-keyed'
                and any(k.arg=='gap_s' and ast.unparse(k.value) in ('1 / 1.5','DRPC_GAP_S') for k in call.keywords)):
                names.update(t.id for t in node.targets if isinstance(t,ast.Name))
        if any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
               and node.func.attr=='acquire' and isinstance(node.func.value,ast.Name)
               and node.func.value.id in names for node in ast.walk(function)):return True
    return False


def test_every_reader_of_a_drpc_research_url_goes_through_the_account_pacer():
    """Gamma LOW on 24df31a2b: pacing is opt-in per caller, so a reader of a
    MEMECOIN_RESEARCH_DRPC_RPC_URL_* that skips drpc_paced spends the shared window unseen. Every
    reader must use the wrapper or the called canonical account pacer. The latter
    is also checked behaviorally across chain URLs in the universe tests."""
    from pathlib import Path
    import ast
    def code_only(path):
        tree = ast.parse(path.read_text(errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
        return ast.unparse(tree)
    root = Path(__file__).resolve().parents[1]
    sources = {p: code_only(p) for base in ("sniper", "scripts")
               for p in sorted((root / base).rglob("*.py"))}
    readers = [p for p, code in sources.items() if "MEMECOIN_RESEARCH_DRPC_RPC_URL" in code]
    assert readers, "the exit follower reads MEMECOIN_RESEARCH_DRPC_RPC_URL_56"
    unpaced = [str(p.relative_to(root)) for p in readers
               if "drpc_paced" not in sources[p] and not _direct_account_pacer(sources[p])]
    assert unpaced == [], unpaced


def test_direct_account_pacer_guard_rejects_wrong_key_uncalled_and_literal_only():
    good="def paced():\n    pacer = SharedPacer.named('drpc-keyed', gap_s=1/1.5)\n    def send():\n        pacer.acquire()\n    return send\n"
    assert _direct_account_pacer(good)
    assert not _direct_account_pacer(good.replace('drpc-keyed','per-chain-key'))
    assert not _direct_account_pacer(good.replace('pacer.acquire()','pass'))
    assert not _direct_account_pacer(repr(good))


def test_only_the_settings_name_the_hop_seed_host_and_setup_backfill_reads_56_paced(monkeypatch):
    """Alpha 2026-10-02: no script reaches https://1rpc.io (quota-dead: -32001 on every call); the
    BSC tape's hop seed moved to the keyed dRPC account the same day. setup_backfill reads 56 on that
    account through its pacer, and 4663 on the QuickNode research key unpaced."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    literal = re.compile(r"[\"']https?://1rpc\.io")
    named = sorted(str(p.relative_to(root)) for base in ("sniper", "scripts")
                   for p in (root / base).rglob("*.py") if literal.search(p.read_text(errors="replace")))
    assert named == [], named
    import sniper.services.memecoin_research_pacer as rp
    from scripts.research import setup_backfill as sb
    assert sb.HOSTS["56"][0] == "MEMECOIN_RESEARCH_DRPC_RPC_URL_56"
    took = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append(self.path))

    class Reply:
        def __init__(self, out):
            self.out = out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self.out).encode()
    import json
    import sqlite3
    monkeypatch.setattr(sb.urllib.request, "urlopen",
                        lambda req, timeout=30: Reply({"jsonrpc": "2.0", "id": 1, "result": "0x38"}))
    db = sqlite3.connect(":memory:")
    db.executescript(sb.DDL)
    keyed = sb.Rpc("56", "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET", 0.0, db)
    assert keyed("eth_chainId", []) == "0x38" and len(took) == 1
    plain = sb.Rpc("4663", "https://x.quiknode.pro/k/", 0.0, db)
    assert plain("eth_chainId", []) == "0x38" and len(took) == 1


def test_a_late_wake_never_releases_two_calls_inside_one_gap(tmp_path):
    # Codex W12: a caller reserved 1001.1, woke 100 s late, and the next caller reserved "now" and was
    # released at the same instant: actual gap 0.0 s against 1.1 s configured.
    from sniper.services.memecoin_research_pacer import SharedPacer
    now = [1000.0]
    sends = []

    def oversleep(seconds):
        now[0] += seconds + 100.0

    pacer = SharedPacer(tmp_path / "key.json", gap_s=1.1, clock=lambda: now[0], sleep=oversleep)
    for _ in range(4):
        pacer.acquire()
        sends.append(now[0])
    assert all(b - a >= 1.1 - 1e-9 for a, b in zip(sends, sends[1:])), sends


def test_two_callers_sharing_a_file_are_granted_a_gap_apart_after_one_overslept(tmp_path):
    from sniper.services.memecoin_research_pacer import SharedPacer
    now = [1000.0]
    path = tmp_path / "key.json"
    late = SharedPacer(path, gap_s=1.1, clock=lambda: now[0], sleep=lambda s: None)
    other = SharedPacer(path, gap_s=1.1, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    late.acquire()                                     # prior grant at 1000.0
    reserved = {}

    def late_sleep(seconds):                          # while `late` oversleeps, `other` is released
        now[0] += seconds + 100.0
        if "other_sent" not in reserved:
            other.acquire()
            reserved["other_sent"] = now[0]
    late.sleep = late_sleep
    late.acquire()
    assert now[0] - reserved["other_sent"] >= 1.1 - 1e-9


@pytest.mark.parametrize("answer", [
    {"code": -32000, "message": "rate-limited due to unusually high traffic"},
    RuntimeError("{'code': -32000, 'message': 'rate-limited due to unusually high traffic'}")])
def test_drpc_s_hyphenated_rate_limited_answer_pauses_the_account(tmp_path, monkeypatch, answer):
    """Gamma 2026-10-14: dRPC answered -32000 "rate-limited due to unusually high traffic" on 56 and
    the shared pacer did not pause the seeds on it ("rate-limited" is not "rate limit")."""
    import sniper.services.memecoin_research_pacer as rp
    monkeypatch.setenv("MEMECOIN_RESEARCH_PACER_DIR", str(tmp_path))
    paused = []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    monkeypatch.setattr(rp.SharedPacer, "pause", lambda self, s: paused.append(s))
    assert rp._drpc_limit(answer)
    rp.drpc_paced(lambda url, method, params: (None, answer))(
        "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET", "eth_getLogs", [])
    assert paused == [60.0]
    assert not rp._drpc_limit({"code": -32000, "message": "execution reverted"})
