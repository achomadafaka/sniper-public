"""Cross-process serialization of real-money execution, per (wallet, chain).

Real fills are priced by whole-wallet balance deltas (spent = balance_before - balance_after). If two
real trades on the same wallet+chain overlap — and they can, because the runner main thread, the
follow-activity thread, and the SEPARATE web-server process all execute against the same wallet with
no coordination — the delta is corrupted, realized_pnl is mis-signed, and the daily-loss breaker
(which reads realized_pnl) can fail OPEN. See the 2026-07-15 system review, CRITICAL-1.

A `threading.Lock` cannot fix this: the web dashboard is a different PROCESS. An advisory FILE lock
(`fcntl.flock`) is held by the OS across processes, so it serializes all three actors. The lock is
keyed per (wallet, chain): Base (8453) and Robinhood (4663) share one EVM ADDRESS but have
independent balances, so they must NOT block each other — different key, different lockfile.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)


class UnchangedWalletResult(dict):
    """Explicit proof from an executor's pre-mutation return site, never inferred from status.

    Only use before any approval, pre-transaction, signing or send. A refused retry after a mined
    transaction is an ordinary dict and still invalidates the wallet snapshot.
    """


def _default_lock_dir():
    from sniper.util.state_paths import state_path
    return str(state_path("data"))


def _lock_path(key, lock_dir=None):
    if lock_dir is None:
        lock_dir = _default_lock_dir()
    os.makedirs(lock_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:120]
    return os.path.join(lock_dir, "real_exec_%s.lock" % safe)


@contextlib.contextmanager
def wallet_exec_lock(key, *, lock_dir=None, blocking=True):
    """Hold an EXCLUSIVE cross-process lock for `key` (a wallet+chain identity) for the block's
    duration. Blocking: a concurrent holder (any thread OR any process) waits. Released on exit even
    if the body raises.

    A FRESH fd is opened per acquire (not reused): flock locks are associated with the open file
    DESCRIPTION, so two threads sharing one fd both 'hold' the same lock and don't mutually exclude.
    A distinct open per acquire makes it exclusive across threads too, not just across processes.
    Open/close cost is trivial next to a multi-second on-chain execution.

    NON-BLOCKING (`blocking=False`) yields True when the lock was taken and False when another
    holder has it, without waiting. Added 2026-08-26 for the rent sweep: maintenance work must never
    make a latency-critical entry queue behind it, and a sweep that skips this pass simply runs at
    the next one. Real EXECUTION must keep blocking=True — an entry that gives up its turn silently
    is a missed trade, not a deferred chore.
    """
    path = _lock_path(key, lock_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    held = False
    try:
        if blocking:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until exclusive; cross-thread + cross-process
            held = True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except OSError:
                held = False
        yield held
    finally:
        try:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def evm_exec_lock_key(chain, address):
    """THE per-(wallet, chain) execution lock key for an EVM wallet. Byte-exact, never case-folded:
    the EVM leg's executors and the 4663 fast lane must hold the same lock (Gamma MEDIUM)."""
    return "evm-%s-%s" % (chain, address)


def _generation_path(key, lock_dir=None):
    return _lock_path(key, lock_dir)[:-len(".lock")] + ".gen"


def touch_generation(key, *, lock_dir=None):
    """Stamp `key`'s execution generation: the file's mtime is when an execution on this wallet
    and chain last finished. The 4663 fast lane trusts a balance/nonce snapshot only when it was
    read after this stamp (spec 2026-10-01 fast lane §2.5). Called with the lock still held."""
    path = _generation_path(key, lock_dir)
    with open(path, "a"):
        pass
    os.utime(path, None)


# A STAMP THAT FAILED IS A GENERATION NOBODY KNOWS (Gamma MEDIUM). A one-way latch: set by any
# thread whose stamp raised, never cleared in-process, read by the 4663 fast lane, which then
# refuses every signal until a restart. Setting an Event is the only mutation; it is thread-safe.
_GENERATION_FAULT = threading.Event()
# The FIRST latch's cause (Alpha 2026-10-01): the lane's state row and its one alert name it.
_GENERATION_FAULT_REASON = [None]
_GENERATION_FAULT_LOCK = threading.Lock()


def generation_fault():
    return _GENERATION_FAULT.is_set()


def generation_fault_reason():
    """Why the fault latched first, or None while it has not."""
    return _GENERATION_FAULT_REASON[0]


def latch_generation_fault(reason):
    """Latch the fault. The first cause is kept and logged ONCE at ERROR with its traceback (the
    caller is inside its `except`); a later failure only adds a debug line, never a flood."""
    with _GENERATION_FAULT_LOCK:
        first = _GENERATION_FAULT_REASON[0] is None
        if first:
            _GENERATION_FAULT_REASON[0] = str(reason)[:300]
        _GENERATION_FAULT.set()
    if first:
        logger.error("GENERATION STAMP FAULT: the 4663 fast lane is OFF until restart (%s)", reason,
                     exc_info=True)
    else:
        logger.debug("generation stamp fault again (latched already): %s", reason)


def chain_generation_key(chain):
    """The per-chain broadcast generation: stamped by every raw send on the chain, whatever path
    sent it (executors, the pre-approval daemon, gas top-ups, operator bridge scripts)."""
    return "evm-chain-%s" % chain


def _raw_chain_id(raw_hex):
    import rlp
    raw = bytes.fromhex(str(raw_hex)[2:] if str(raw_hex).startswith("0x") else str(raw_hex))
    if raw and raw[0] >= 0xC0:                              # legacy: EIP-155 v = 35 + 2 * chainId
        v = int.from_bytes(rlp.decode(raw)[6], "big")
        if v < 35:
            raise ValueError("pre-EIP-155 transaction carries no chain id")
        return (v - 35) // 2
    if raw and raw[0] in (1, 2, 3, 4):
        return int.from_bytes(rlp.decode(raw[1:])[0], "big")
    raise ValueError("not a signed EVM transaction")


def stamp_send(raw_hex, *, lock_dir=None, intent=None):
    """Stamp the generation of the chain this signed transaction belongs to. Called AFTER every
    broadcast attempt, whatever its outcome. Never raises: a failure latches the fault.

    `intent` is the token `intent_send` returned for this post. Its record is removed only after the
    stamp succeeded: a failed stamp leaves it, so the send stays fenced for every process."""
    try:
        touch_generation(chain_generation_key(_raw_chain_id(raw_hex)), lock_dir=lock_dir)
    except Exception as exc:
        latch_generation_fault("stamp_send: %s: %s" % (type(exc).__name__, exc))
        return
    if intent is None:
        return
    path, transport, intent_us = intent
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass                                  # the sweep resolved it on chain proof first
    except Exception as exc:
        latch_generation_fault("intent unlink: %s: %s" % (type(exc).__name__, exc))
        return
    logger.warning("exec.intent chain=%s transport=%s intent_us=%.1f",
                   os.path.basename(os.path.dirname(path)), transport, intent_us)


# ------------------------------------------------------------------------------------------------
# EVM-08 write-ahead intents (spec 2026-10-07-evm-generation-intent-fence-design.md, revision 2).
# One record per network post in <lock dir>/exec_intents/<chain>/, written before the post and
# removed only by that post's own completion (stamp_send). The fast lanes refuse while any record
# exists on the chain; a record left by a failed stamp or a crash is resolved only by the sweep, on
# chain proof.
# ------------------------------------------------------------------------------------------------

class IntentUnwritable(RuntimeError):
    """The write-ahead intent could not be recorded. The post must not be sent."""


_INTENT_COUNTER = itertools.count()


def intents_enabled():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", False))


def _intent_root(lock_dir=None):
    return os.path.join(lock_dir if lock_dir is not None else _default_lock_dir(), "exec_intents")


def standing_intent_lock_dir(*, lock_dir=None):
    """Receipt-lived approval identities, separate from the entry lane's short send fence."""
    return os.path.join(lock_dir if lock_dir is not None else _default_lock_dir(),
                        "standing_allowance")


def _hex(raw_hex):
    raw = str(raw_hex)
    return (raw[2:] if raw[:2].lower() == "0x" else raw).lower()


def intent_send(raw_hex, transport, *, max_block=None, lock_dir=None, required=False):
    """Write this post's intent record, immediately before its network I/O. Returns the completion
    token for `stamp_send(raw, intent=token)`, or None with MEMECOIN_EXEC_INTENT_STAMP off.
    `required=True` retains mandatory custody identities independently of that optional knob.

    Raises IntentUnwritable, after latching the local fault, when the record cannot be written;
    each sender maps that to its own definite "not sent". No fsync (spec section 4, item 5)."""
    if not required and not intents_enabled():
        return None
    started = time.perf_counter()
    path = None
    try:
        chain = _raw_chain_id(raw_hex)
        d = os.path.join(_intent_root(lock_dir), str(chain))
        name = "%s.%d.%d.%d" % (hashlib.sha256(_hex(raw_hex).encode()).hexdigest()[:32],
                                os.getpid(), threading.get_ident(), next(_INTENT_COUNTER))
        path = os.path.join(d, name)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(path, flags, 0o644)
        except FileNotFoundError:
            os.makedirs(d, exist_ok=True)
            fd = os.open(path, flags, 0o644)
        try:
            line = (json.dumps({
                "raw": "0x" + _hex(raw_hex), "transport": transport,
                "max_block": None if max_block is None else int(max_block),
                "written_at": time.time()}) + "\n").encode()
            written = os.write(fd, line)
            if written != len(line):                  # a short write is a failed write (Gamma)
                raise OSError("short intent write: %d of %d bytes" % (written, len(line)))
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(path)                       # nothing was sent: a torn record would fence
            raise
        os.close(fd)
    except Exception as exc:
        latch_generation_fault("intent: %s: %s" % (type(exc).__name__, exc))
        raise IntentUnwritable("%s: %s" % (type(exc).__name__, exc)) from exc
    return path, transport, (time.perf_counter() - started) * 1e6


def intents_outstanding(chain, *, lock_dir=None, required=False):
    """False when no post is open on the chain (or the knob is off), True when one is in flight or
    unrecorded, None when the directory cannot be read (latched, as an unreadable .gen is).
    `required=True` reads mandatory identities even when optional post stamping is disabled."""
    if not required and not intents_enabled():
        return False
    try:
        with os.scandir(os.path.join(_intent_root(lock_dir), str(chain))) as entries:
            return any(True for _entry in entries)
    except FileNotFoundError:
        return False
    except OSError as exc:
        latch_generation_fault("intent read %s: %s: %s" % (chain, type(exc).__name__, exc))
        return None


def _decode_signed(raw_hex):
    """(sender, nonce, tx hash) from signed bytes. Raises on anything it cannot read."""
    import rlp
    from eth_account import Account
    from eth_utils import keccak
    raw = bytes.fromhex(_hex(raw_hex))
    if raw and raw[0] >= 0xC0:
        nonce = int.from_bytes(rlp.decode(raw)[0], "big")
    elif raw and raw[0] in (1, 2):
        nonce = int.from_bytes(rlp.decode(raw[1:])[1], "big")
    else:
        raise ValueError("unsupported transaction type")
    return Account.recover_transaction(raw), nonce, "0x" + keccak(raw).hex()


def _custody_post(url, method, params, timeout_s=5.0):
    import urllib.request
    from sniper.execution.evm_wallet import _UA      # Cloudflare-fronted RPCs (chain 4663) 403 'error code: 1010' on Python-urllib
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        return json.loads(r.read() or b"{}")


def _custody_int(post, url, method, params):
    answer = post(url, method, params)
    return int(str(answer["result"]), 16)            # an error reply has no result: raises


def _load_intents(d, now):
    out = []
    with os.scandir(d) as entries:
        for entry in entries:
            rec, torn, mtime = None, False, None
            try:
                mtime = entry.stat().st_mtime
                with open(entry.path) as fh:
                    text = fh.read()
            except FileNotFoundError:
                continue                              # completed while we listed
            except OSError:
                text = None                           # unreadable: unknown, never "torn"
            if text is not None:
                try:
                    rec = json.loads(text) if text.endswith("\n") else None
                except ValueError:
                    rec = None
                torn = not isinstance(rec, dict)      # a whole record is one JSON object + "\n"
            out.append({"path": entry.path, "name": entry.name,
                        "digest": entry.name.split(".")[0], "age": now - (mtime or now),
                        "mtime": mtime, "rec": rec if isinstance(rec, dict) else None,
                        "torn": torn})
    return out


def _boot_time():
    """This boot's start (epoch s, /proc/stat btime), or None when it cannot be read."""
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _never_posted(r, boot_time):
    """A torn record proves its post never ran: the writer posts only after the whole line was
    written and closed (Gamma MEDIUM). Two guards: the record was made in THIS boot (a power loss
    without fsync can empty the record of a post that left the host), and its writer is gone (a
    live writer may still be inside its write)."""
    if not r["torn"] or boot_time is None or r["mtime"] is None or r["mtime"] < boot_time:
        return False
    try:
        pid = int(r["name"].split(".")[1])
    except (IndexError, ValueError):
        return False
    return not _pid_alive(pid)


def _resolve(chain, r, basis, tx_hash, lock_dir):
    """Stamp the chain .gen, then remove the record. False (record kept) when the stamp fails."""
    try:
        touch_generation(chain_generation_key(chain), lock_dir=lock_dir)
    except Exception as exc:
        latch_generation_fault("intent sweep stamp: %s: %s" % (type(exc).__name__, exc))
        return False                                  # never unlink before the .gen moved
    with contextlib.suppress(FileNotFoundError):
        os.unlink(r["path"])
    logger.warning("generation fence resolved chain=%s tx=%s after %.0f s basis=%s",
                   chain, tx_hash, r["age"], basis)
    return True


def sweep_intents(*, post=None, rpc_url_for=None, now=None, lock_dir=None, alert_fn=None,
                  alerted=None, min_age_s=None, alert_after_s=None, boot_time=None, required=False,
                  alert_scope="fast lanes"):
    """Resolve intent records at least W old, on chain proof only (spec section 6), and alert once
    per record fenced past the alert bound. Returns [(chain, tx hash, basis)] resolved.

    never_posted: a torn record (empty, or not one whole JSON line) made in this boot whose writer
    process is gone; no network read. Proofs read on the chain's custody RPC after its eth_chainId
    matched the directory's chain:
    - nonce_consumed: latest nonce of the sender is past the record's nonce (any transport);
    - bundle_expired: every open record of these bytes is a bundle with max_block, and the head is
      past the largest max_block + 2.
    Any failed or malformed read resolves nothing. Run by the runner's sweep thread only.
    `required=True` recovers mandatory identities even when optional post stamping is disabled."""
    from sniper.settings import memecoin as cfg
    if not required and not intents_enabled():
        return []
    post = post or _custody_post
    if rpc_url_for is None:
        rpc_url_for = cfg.evm_rpc_for_chain
    now = time.time() if now is None else now
    min_age_s = cfg.MEMECOIN_EXEC_INTENT_RESOLVE_AFTER_S if min_age_s is None else min_age_s
    alert_after_s = (cfg.MEMECOIN_EXEC_INTENT_ALERT_AFTER_S if alert_after_s is None
                     else alert_after_s)
    boot_time = _boot_time() if boot_time is None else boot_time
    root = _intent_root(lock_dir)
    try:
        chains = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    resolved = []
    for chain in chains:
        try:
            records = _load_intents(os.path.join(root, chain), now)
        except OSError:
            logger.exception("intent sweep: chain %s directory unreadable", chain)
            continue
        old = [r for r in records if r["age"] >= min_age_s]
        if not old:
            continue
        done = set()
        for r in old:                                 # identity first: the alert names it
            if r["torn"]:
                r["why"] = "torn record (writer alive, or made before this boot)"
                if _never_posted(r, boot_time) and _resolve(chain, r, "never_posted", "unknown",
                                                           lock_dir):
                    done.add(r["path"])
                    resolved.append((chain, "unknown", "never_posted"))
                continue
            try:
                r["sender"], r["nonce"], r["tx"] = _decode_signed(r["rec"]["raw"])
                r["why"] = "nonce not consumed"
            except Exception as exc:
                r["why"] = ("unreadable record" if r["rec"] is None
                            else "undecodable record: %s" % type(exc).__name__)
        pending = [r for r in old if "tx" in r]
        if not pending:                               # nothing a chain read could resolve
            _alert_fenced(chain, old, done, alert_after_s, alerted, alert_fn, scope=alert_scope)
            continue
        url = rpc_url_for(chain)
        try:
            if not url or _custody_int(post, url, "eth_chainId", []) != int(chain):
                raise ValueError("custody RPC missing or on another chain")
            head = None
            for r in pending:
                try:
                    sender, nonce, tx_hash = r["sender"], r["nonce"], r["tx"]
                    basis = None
                    if _custody_int(post, url, "eth_getTransactionCount",
                                    [sender, "latest"]) > nonce:
                        basis = "nonce_consumed"
                    else:
                        same = [x["rec"] for x in records if x["digest"] == r["digest"]]
                        if all(x and x.get("transport") == "bundle" and x.get("max_block") is not None
                               for x in same):
                            if head is None:
                                head = _custody_int(post, url, "eth_blockNumber", [])
                            if head > max(int(x["max_block"]) for x in same) + 2:
                                basis = "bundle_expired"
                except Exception as exc:
                    r["why"] = "custody read failed: %s" % type(exc).__name__
                    logger.debug("intent sweep: %s unresolved", r["path"], exc_info=True)
                    continue
                if basis is None:
                    continue
                if _resolve(chain, r, basis, tx_hash, lock_dir):
                    done.add(r["path"])
                    resolved.append((chain, tx_hash, basis))
        except Exception as exc:
            for r in pending:
                r["why"] = "custody read failed: %s" % (
                    "no custody RPC" if not url else type(exc).__name__)
            logger.warning("intent sweep: chain %s unresolved: %s: %s", chain,
                           type(exc).__name__, exc)
        _alert_fenced(chain, old, done, alert_after_s, alerted, alert_fn, scope=alert_scope)
    return resolved


def _alert_fenced(chain, old, done, alert_after_s, alerted, alert_fn, *, scope="fast lanes"):
    """One alert per record fenced past the bound, naming why it is still unresolved."""
    for r in old:
        if r["path"] in done or r["age"] < alert_after_s:
            continue
        if alerted is not None:
            if r["path"] in alerted:
                continue
            alerted.add(r["path"])
        text = ("EVM-08 fence: the chain %s %s have been off %.0f min; tx %s sender %s "
                "nonce %s unresolved: %s. A consumed nonce resolves it (the next ordinary send on "
                "the chain); or check the wallet and clear it." % (
                    chain, scope, r["age"] / 60.0, r.get("tx", "unknown"), r.get("sender", "unknown"),
                    r.get("nonce", "unknown"), r.get("why", "unknown")))
        logger.warning(text)
        if alert_fn is not None:
            try:
                alert_fn(text)
            except Exception:
                logger.exception("intent sweep: alert failed")


def run_intent_sweep_forever(*, alert_fn=None, period_s=None, sleep=time.sleep):
    """The runner's single sweep owner, on its own daemon thread. Never raises."""
    from sniper.settings import memecoin as cfg
    alerted = set()
    while True:
        try:
            sweep_intents(alert_fn=alert_fn, alerted=alerted)
        except Exception:
            logger.exception("intent sweep failed (keeps running)")
        try:
            sweep_intents(alert_fn=alert_fn, alerted=alerted,
                          lock_dir=standing_intent_lock_dir(), required=True,
                          alert_scope="holdings sells")
        except Exception:
            logger.exception("standing approval intent sweep failed (keeps running)")
        sleep(cfg.MEMECOIN_EXEC_INTENT_SWEEP_PERIOD_S if period_s is None else period_s)


def exec_generation(key, *, lock_dir=None):
    """The last execution's end (host-clock epoch seconds), 0.0 when none was ever stamped, or None
    when the stamp exists but cannot be read. Only a missing file means "never": an EACCES or EIO is
    a generation nobody knows, so it latches the same fault a failed stamp does (Codex EVM-08)."""
    try:
        return os.stat(_generation_path(key, lock_dir)).st_mtime
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        latch_generation_fault("generation read %s: %s: %s" % (key, type(exc).__name__, exc))
        return None


def serialize_executor(fn, key, *, lock_dir=None, release_unsigned_close_on_error=False,
                       withdrawal_action=None, holding_entry_chain=None, maintenance_chain=None):
    """Wrap a real executor (execute_fn / exit_fn / derisk_fn) so every call holds the per-(wallet,
    chain) lock across its ENTIRE quote->send->confirm->balance-delta window. Transparent to args and
    return value. Explicit EVM chains fence unknown maintenance; only entry wrappers may release
    an unsigned reservation. This is the single chokepoint every actor's real execution passes through."""
    if maintenance_chain is None:
        maintenance_chain = holding_entry_chain
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with wallet_exec_lock(key, lock_dir=lock_dir):
            if holding_entry_chain is not None or maintenance_chain is not None:
                from sniper.persistence import db_memecoin as m
                conn = args[0] if args else kwargs["conn"]
                reason = None
                if holding_entry_chain is not None and m.unresolved_holding_sell_count(
                        conn, holding_entry_chain):
                    reason = "holding_unresolved"
                elif maintenance_chain is not None and m.has_unresolved_evm_gas_topup(
                        conn, maintenance_chain):
                    reason = "gas_unresolved"
                if reason:
                    if holding_entry_chain is not None:
                        pid = args[1] if len(args) > 1 else kwargs["pid"]
                        m.release_unsigned_blocked_entry(conn, pid, reason)
                    return UnchangedWalletResult(status="deferred", reason=reason)
            # Both venue exit executors use this wrapper. Re-read after taking the wallet lock:
            # a sweep or source-sell may have selected the position before the operator paused it.
            # Manual close remains available from Long Hold without a displayed price.
            if release_unsigned_close_on_error and len(args) >= 3 and args[2] != "manual":
                from sniper.persistence import db_memecoin as m
                current = m.get_position(args[0], args[1]["id"])
                if current and current.get("tracking_paused_at"):
                    return {"status": "tracking_paused", "position_id": current["id"]}
            if withdrawal_action:
                from sniper.persistence import db_memecoin as m
                conn = args[0] if args else kwargs["conn"]
                if m.unresolved_withdraws(conn, "solana"):
                    if withdrawal_action == "entry":
                        pid = args[1] if len(args) > 1 else kwargs["pid"]
                        m.release_withdraw_blocked_entry(conn, pid)
                    return {"status": "deferred", "reason": "withdrawal_unresolved"}
            before = None
            if release_unsigned_close_on_error and len(args) >= 2:
                from sniper.persistence import db_memecoin as m
                before = m.get_position(args[0], args[1]["id"])
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception:
                if before and not before.get("close_claim_id"):
                    try:
                        current = m.get_position(args[0], args[1]["id"])
                        if current and current.get("close_claim_id"):
                            if m.release_position_close_claim(
                                    args[0], current["id"], current["close_claim_id"]):
                                logger.warning(
                                    "unsigned close claim released after executor exception pos=%s",
                                    current["id"])
                    except Exception:
                        logger.exception("unsigned close claim cleanup failed")
                raise
            finally:
                # A proved read-only refusal must not poison the parallel fast lane's snapshot.
                # Exceptions, ordinary results and every broadcast retain conservative stamping.
                if not isinstance(result, UnchangedWalletResult):
                    try:
                        touch_generation(key, lock_dir=lock_dir)
                    except Exception as exc:
                        latch_generation_fault("execution stamp %s: %s: %s"
                                               % (key, type(exc).__name__, exc))
    # Recovery must acquire this exact same lock before changing executor-owned claims.
    wrapped.recovery_lock = lambda: wallet_exec_lock(key, lock_dir=lock_dir, blocking=False)
    return wrapped
