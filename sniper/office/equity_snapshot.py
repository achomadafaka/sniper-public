"""Authenticated immutable as-of Office equity. No current-ledger or live-price fallback."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import secrets
import tempfile
import time

from . import ledger as L, nominee as N, store as S

MAX_LEDGER_BYTES = 64 * 1024 * 1024
READ_BUDGET_S = 10.
BINDINGS = frozenset({'create_pool','curve_derived','curve_derived_unchecked','intake_canonical_pda'})


class SnapshotRefused(ValueError):
    def __init__(self, reason, summary=None):
        super().__init__(reason)
        self.summary = summary or dict(state='unknown', cash=None, known_liq_lower=None,
                                       unpriced_count=None, unpriced_qty=None, binding_kind_counts={})


def _finite(v):
    return type(v) in (int,float) and math.isfinite(v)


def validate_result(value, *, epoch_id, look_at):
    """Closed public schema; provenance authenticity is the host snapshotter's authority."""
    if not isinstance(value,dict) or set(value) != {'summary','proofs'}:
        raise ValueError('invalid_strict_equity')
    summary, proof = value['summary'], value['proofs']
    if (not isinstance(summary,dict) or set(summary) != {'state','cash','known_liq_lower','unpriced_count','unpriced_qty','binding_kind_counts'}
            or summary['state'] != 'known' or not _finite(summary['cash']) or not _finite(summary['known_liq_lower'])
            or type(summary['unpriced_count']) is not int or summary['unpriced_count'] != 0 or summary['unpriced_qty'] != '0'
            or not isinstance(summary['binding_kind_counts'],dict) or set(summary['binding_kind_counts'])-BINDINGS
            or any(type(n) is not int or n < 1 for n in summary['binding_kind_counts'].values())):
        raise ValueError('strict_equity_not_known')
    if (not isinstance(proof,dict) or set(proof) != {'epoch_id','as_of_cut','snapshot_sha256','sidecar_sha256','metadata_sha256','snapshot_receipt_id','snapshotter_identity'}
            or proof['epoch_id'] != epoch_id or not _finite(proof['as_of_cut']) or proof['as_of_cut'] != look_at
            or any(not isinstance(proof[key],str) or len(proof[key]) != 64 or any(c not in '0123456789abcdef' for c in proof[key]) for key in ('snapshot_sha256','sidecar_sha256','metadata_sha256'))
            or any(not isinstance(proof[key],str) or not proof[key] for key in ('snapshot_receipt_id','snapshotter_identity'))):
        raise ValueError('invalid_strict_equity_proofs')


def _binary(root, ref, deadline):
    rel = PurePosixPath(ref['path'])
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise SnapshotRefused('snapshot_path_denied')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_size > MAX_LEDGER_BYTES:
                raise SnapshotRefused('snapshot_file_not_immutable_or_bounded')
            parts, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise SnapshotRefused('snapshot_read_budget')
                chunk = stream.read(min(1048576, MAX_LEDGER_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise SnapshotRefused('snapshot_too_large')
                parts.append(chunk)
        raw = b''.join(parts)
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise SnapshotRefused('snapshot_hash_mismatch')
        return raw
    finally:
        os.close(directory)


def _scan(conn, cut, epoch_id):
    # due_at is a scheduled future target, not an observed fill/exit time.
    fields = {'orders':('requested_at','filled_at','exit_requested_at','closed_at'),
              'marks':('at',), 'cashflows':('at','fx_at'), 'capital_flows':('at',),
              'books':('opened_at',), 'ledger_epochs':('opened_at',),
              'decision_receipts':('created_at',), 'outages':('from_at','to_at')}
    maxima, counts = {}, {}
    for table, columns in fields.items():
        counts[table] = conn.execute('SELECT count(*) FROM '+table).fetchone()[0]
        for column in columns:
            bad = conn.execute('SELECT 1 FROM '+table+' WHERE '+column+' IS NOT NULL AND '
                '(typeof('+column+") NOT IN ('integer','real') OR "+column+'<0 OR '+column+'>?) LIMIT 1', (cut,)).fetchone()
            if bad:
                raise SnapshotRefused('snapshot_activity_after_cut_or_clock_unknown')
        row = conn.execute('SELECT '+','.join('max('+column+')' for column in columns)+' FROM '+table).fetchone()
        maxima[table] = max((v for v in row if v is not None), default=None)
    books = [r[0] for r in conn.execute("SELECT book_id FROM books WHERE epoch_id=? AND kind='evaluated' ORDER BY book_id", (epoch_id,))]
    if not books:
        raise SnapshotRefused('snapshot_evaluated_book_missing')
    return dict(user_version=conn.execute('PRAGMA user_version').fetchone()[0], books=books,
                order_count=counts['orders'], mark_count=counts['marks'], cashflow_count=counts['cashflows'],
                max_order_time=maxima['orders'], max_cashflow_at=conn.execute('SELECT max(at) FROM cashflows').fetchone()[0], max_mark_at=maxima['marks'])


def read_equity_snapshot(root, ref, *, epoch_id, look_at, authenticate_snapshot, runtime_dir):
    """Consume a private, re-hashed read-only copy, with no source path/WAL dependency."""
    private = None
    conn = None
    try:
        if not _finite(look_at) or look_at < 0 or not callable(authenticate_snapshot):
            raise SnapshotRefused('snapshot_auth_or_cut_unknown')
        root = Path(root).resolve(strict=True)
        deadline = time.monotonic() + READ_BUDGET_S
        raw_meta = N._read(root, ref, deadline)
        doc = json.loads(raw_meta)
        if (doc.get('schema') != 'office-equity-snapshot-v1' or doc.get('epoch_id') != epoch_id
                or not _finite(doc.get('as_of_cut')) or doc['as_of_cut'] != look_at
                or not _finite(doc.get('taken_at')) or doc['taken_at'] < look_at):
            raise SnapshotRefused('snapshot_cut_mismatch')
        receipt = doc['receipt']
        body = copy.deepcopy(doc)
        stated_meta_hash = body['receipt'].pop('metadata_sha256')
        metadata_hash = hashlib.sha256(S.canonical_json(body)).hexdigest()
        if stated_meta_hash != metadata_hash:
            raise SnapshotRefused('snapshot_metadata_hash_mismatch')
        if any(not isinstance(receipt.get(key),str) or not receipt[key] for key in ('id','written_by','written_by_identity')):
            raise SnapshotRefused('snapshot_auth_unknown')
        try:
            authenticated = authenticate_snapshot(copy.deepcopy(receipt), metadata_hash) is True
        except Exception:
            authenticated = False
        if not authenticated:
            raise SnapshotRefused('snapshot_auth_unknown')
        if (not isinstance(doc.get('clock_sample_id'),str) or not doc['clock_sample_id']
                or not _finite(doc.get('clock_offset_s')) or not _finite(doc.get('clock_sampled_at_host'))
                or not 0 <= doc['taken_at'] + doc['clock_offset_s'] - doc['clock_sampled_at_host'] <= L.CLOCK_MAX_AGE_S):
            raise SnapshotRefused('snapshot_clock_unknown')
        fx = doc.get('fx')
        if (not isinstance(fx,dict) or not _finite(fx.get('sol_usd')) or fx['sol_usd'] <= 0
                or not _finite(fx.get('sol_usd_at')) or not _finite(fx.get('max_age_s'))
                or not 0 < fx['max_age_s'] <= 120. or not 0 <= look_at - fx['sol_usd_at'] <= fx['max_age_s']
                or any(not isinstance(fx.get(key),str) or not fx[key] for key in ('source','source_ref'))):
            raise SnapshotRefused('snapshot_fx_unknown_or_stale')
        ledger = doc['ledger']
        raw = _binary(root, ledger, deadline)
        runtime = Path(runtime_dir).absolute()
        if runtime.is_symlink():
            raise SnapshotRefused('snapshot_runtime_denied')
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = runtime.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SnapshotRefused('snapshot_runtime_not_private')
        private = tempfile.TemporaryDirectory(prefix='equity-', dir=runtime)
        copy_path = Path(private.name)/(secrets.token_hex(12)+'.db')
        fd = os.open(copy_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if hashlib.sha256(copy_path.read_bytes()).hexdigest() != ledger['sha256']:
            raise SnapshotRefused('snapshot_private_copy_hash_mismatch')
        conn = sqlite3.connect(copy_path.as_uri()+'?mode=ro&immutable=1', uri=True, timeout=.1)
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('PRAGMA query_only=ON')
        actual = _scan(conn, look_at, epoch_id)
        if (not set(actual) <= set(ledger)
                or any(ledger[key] != value for key,value in actual.items())
                or any(type(ledger[key]) is not int for key in ('user_version','order_count','mark_count','cashflow_count'))
                or any(ledger[key] is not None and not _finite(ledger[key]) for key in ('max_order_time','max_cashflow_at','max_mark_at'))):
            raise SnapshotRefused('snapshot_counts_or_times_mismatch')
        equity = L.equity(conn, epoch_id=epoch_id, kind='evaluated', now=look_at,
                           sol_usd=fx['sol_usd'], sol_at=fx['sol_usd_at'], mark_max_age_s=60., require_proven=True)
        summary = dict(state=equity.state, cash=equity.cash, known_liq_lower=equity.known_liq_lower,
                       unpriced_count=equity.unpriced_count, unpriced_qty=str(equity.unpriced_qty),
                       binding_kind_counts=dict(equity.binding_kind_counts))
        if equity.state != 'known' or equity.unpriced_count != 0 or equity.known_liq_lower is None:
            summary.update(state='unknown', known_liq_lower=None)
            raise SnapshotRefused('strict_equity_unknown', summary)
        result = dict(summary=summary, proofs=dict(epoch_id=epoch_id, as_of_cut=look_at,
                    snapshot_sha256=ledger['sha256'], sidecar_sha256=ref['sha256'], metadata_sha256=metadata_hash,
                    snapshot_receipt_id=receipt['id'], snapshotter_identity=receipt['written_by_identity']))
        validate_result(result, epoch_id=epoch_id, look_at=look_at)
        return result
    except SnapshotRefused:
        raise
    except N.Invalid as exc:
        reason = 'snapshot_read_budget' if str(exc) == 'nominee_resolution_budget' else 'snapshot_sidecar_pin_invalid'
        raise SnapshotRefused(reason) from None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
        raise SnapshotRefused('snapshot_unreadable_or_invalid') from None
    finally:
        if conn is not None:
            conn.close()
        if private is not None:
            private.cleanup()
