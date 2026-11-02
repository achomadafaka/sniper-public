"""The CLI's rate-limit read, stdlib only, so it also runs inside a seat sandbox."""
import json
import os
import selectors
import subprocess
import time


def poll_codex(*, popen=subprocess.Popen, timeout_s=10):
    child=None
    try:
        child=popen(['codex','app-server','--listen','stdio://'],stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        def send(doc):
            child.stdin.write(json.dumps(doc).encode()+b'\n');child.stdin.flush()
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'office_quota_read','version':'1.0'}}})
        deadline=time.monotonic()+timeout_s
        pending=b'';read_bytes=0
        with selectors.DefaultSelector() as events:
            events.register(child.stdout,selectors.EVENT_READ)
            while time.monotonic()<deadline:
                if not events.select(min(.25,max(0,deadline-time.monotonic()))):continue
                data=os.read(child.stdout.fileno(),65536)
                if not data:return None,'transport'
                read_bytes+=len(data)
                if read_bytes>200000:return None,'invalid'
                pending+=data
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    doc=json.loads(line)
                    if not isinstance(doc,dict):return None,'invalid'
                    if doc.get('id')==1:
                        if 'error' in doc:return None,'unavailable'
                        send({'method':'initialized'})
                        send({'id':2,'method':'account/rateLimits/read','params':{'excludeResetCreditDetails':True}})
                    elif doc.get('id')==2:
                        result=doc.get('result')
                        return (result,None) if isinstance(result,dict) else (None,'unavailable')
        return None,'timeout'
    except (OSError,ValueError,TypeError):
        return None,'transport'
    finally:
        if child is not None:
            try:
                child.terminate();child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=2)
            except ProcessLookupError:
                pass
            finally:
                for stream in (child.stdin,child.stdout):
                    if stream is not None:stream.close()
