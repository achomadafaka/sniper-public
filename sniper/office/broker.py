"""Typed Office requests. Model text is data; the host owns capabilities."""
from dataclasses import dataclass
import math
import time


def _elapsed(start):
    end=time.monotonic()
    if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or end<start:
        return None
    return end-start


@dataclass(frozen=True)
class ToolRequest:
    actor_id: str
    tool: str
    args: dict
    decision_id: str | None
    cut: dict


def quote_external(text: str, source: str, received_at: float) -> dict:
    if (not isinstance(text, str) or not isinstance(source, str) or not source
            or type(received_at) not in (int, float) or not math.isfinite(received_at) or received_at < 0):
        raise ValueError("external_data_invalid")
    return {"type": "quoted_data", "text": text, "source": source, "received_at": received_at}


@dataclass(frozen=True)
class ToolResult:
    result: object
    units: dict
    network_id: str | None
    source_refs: list
    coverage: str
    continuation: object = None
    outcome: str = "success"


@dataclass(frozen=True)
class Tool:
    handler: object
    argument_types: dict
    definition_version: str
    authorize: object = None


@dataclass(frozen=True)
class Receipt:
    receipt_hash: str
    tool: str
    result: object
    source_refs: list
    clock_sample: dict
    receipt_cut: float
    slot_cut: int | None
    knowledge_cut: float
    coverage: str
    definition_version: str
    continuation: object
    outcome: str
    units: dict
    network_id: str | None


class Broker:
    """Only the supervisor supplies Context and registry; neither is a model input.

    Mutation handlers must also fence their action at the ledger transaction boundary.
    This broker's generation checks cannot make two separate databases atomic.
    """
    def __init__(self, store, ledger, registry, *, generation_check):
        self.store, self.ledger = store, ledger
        import copy
        self.registry = copy.deepcopy(registry)
        self.generation_check = generation_check

    def execute(self, req, *, context):
        started=time.monotonic()
        handler_called=False
        handler_s=None
        import copy
        import hashlib
        from . import capabilities as C, store as S
        if self.store.in_transaction:
            raise RuntimeError("broker_requires_owned_transaction")
        req, context = copy.deepcopy(req), copy.deepcopy(context)
        reason = C.refusal(context, req)
        try:
            encoded_request = S.canonical_json(req.__dict__)
            if len(encoded_request) > 65536:
                raise ValueError("request_too_large")
            request_hash = hashlib.sha256(encoded_request).hexdigest()
        except (TypeError, ValueError, RecursionError):
            request_hash = None
            reason = reason or "request_invalid"
        tool = self.registry.get(req.tool) if isinstance(req.tool, str) else None
        try:
            if reason is None and self.generation_check(context) is not True:
                reason = "stale_generation"
        except Exception:
            reason = "generation_unknown"
        if reason is None:
            if (not isinstance(tool, Tool) or not tool.definition_version
                    or set(req.args) != set(tool.argument_types)
                    or any(type(req.args[k]) is not typ for k, typ in tool.argument_types.items())):
                reason = "args_invalid"
        if reason is None and req.tool.startswith("book.") and tool.authorize is None:
            reason = "ownership_unproven"
        if reason is None and tool.authorize is not None:
            try:
                if tool.authorize(req.args, context) is not True:
                    reason = "ownership_denied"
            except Exception:
                reason = "ownership_unknown"
        if reason:
            output = ToolResult({"reason": reason}, {}, None, [], "unknown", outcome="refused")
        else:
            try:
                args, trusted = copy.deepcopy(req.args), copy.deepcopy(context)
                handler_called=True
                handler_started=time.monotonic()
                try:
                    output = tool.handler(args, trusted)
                finally:
                    handler_s=_elapsed(handler_started)
                if (not isinstance(output, ToolResult) or output.outcome not in
                        {"success", "empty", "partial", "unknown", "stale", "timeout", "rate_limited", "refused"}
                        or not isinstance(output.units, dict) or not isinstance(output.source_refs, list)
                        or not isinstance(output.coverage, str)):
                    raise ValueError("result_invalid")
                S.canonical_json(output.__dict__)  # Reject non-JSON/non-finite values before persistence.
                output = copy.deepcopy(output)
                if req.tool == "ticket.propose" and output.outcome == "success":
                    if not isinstance(output.result, dict):
                        raise ValueError("ticket_invalid")
                    output.result["authority"] = "untrusted_office_request"
            except Exception:
                # Exception text can include credentials or private paths; persist only the typed boundary.
                output = ToolResult({"reason": "tool_failed"}, {}, None, [], "unknown", outcome="tool_error")
        payload = dict(output.__dict__, request_hash=request_hash, request_actor_id=req.actor_id, actor_id=context.actor_id, role=context.role,
                       book_id=context.book_id, generation=context.generation,
                       decision_id=context.decision_id, epoch_id=getattr(context,'epoch_id',None), cut=context.cut, tool=req.tool,
                       definition_version=tool.definition_version if isinstance(tool, Tool) else "unregistered")
        # Freeze caller/handler-owned objects through canonical bytes before writing/returning.
        import json
        payload = json.loads(S.canonical_json(payload))
        with S.write_transaction(self.store):
            if output.outcome not in {"refused", "tool_error"}:
                try:
                    current = self.generation_check(context) is True
                except Exception:
                    current = False
                if not current:
                    payload.update(result={"reason": "stale_generation"}, units={}, network_id=None,
                                   source_refs=[], coverage="unknown", continuation=None, outcome="refused")
            typed=[r for r in payload['source_refs'] if isinstance(r,dict) and ('receipt_id' in r or r.get('kind') in ('memo','decision','proposal'))]
            if typed and payload['outcome'] not in {'refused','tool_error'}:
                from .runtime_observations import validate_refs
                try:
                    validate_refs(self.store,typed,before=context.cut['knowledge_cut'])
                except ValueError:
                    payload.update(result={'reason':'handoff_reference_unknown'},source_refs=[],coverage='unknown',outcome='refused')
            # Passive execution evidence, separate from the frozen knowledge cut.
            # A receipt cannot include the duration of its own append/commit.
            payload['timing']={'basis':'monotonic','handler_called':handler_called,'handler_s':handler_s,
                'before_receipt_append_s':_elapsed(started),
                'coverage':'execute_entry_to_before_receipt_append_excludes_append_commit_return'}
            receipt_hash = S.append(self.store, "receipts", payload)
        cut = context.cut
        return Receipt(receipt_hash=receipt_hash, tool=req.tool, result=payload["result"],
                       source_refs=payload["source_refs"], clock_sample=cut.get("clock_sample", {}),
                       receipt_cut=cut.get("receipt_cut"), slot_cut=cut.get("slot_cut"),
                       knowledge_cut=cut.get("knowledge_cut"), coverage=payload["coverage"],
                       definition_version=payload["definition_version"], continuation=payload["continuation"],
                       outcome=payload["outcome"], units=payload["units"], network_id=payload["network_id"])


def check_claim(store, receipt_hash, field_path, value, unit, *, context, network_id):
    """A value is claimable only from an intact, visible receipt at this frozen cut."""
    import hashlib
    import json
    import sqlite3
    from . import store as S
    if not isinstance(unit, str) or not unit or not isinstance(field_path, str) or not field_path:
        return False
    try:
        row = store.execute("SELECT payload_json,prev_hash,row_hash FROM receipts WHERE row_hash=?",
                            (receipt_hash,)).fetchone()
        if row is None:
            return False
        payload, prev_hash, row_hash = row
        data = json.loads(payload)
        if hashlib.sha256(S.canonical_json(data) + prev_hash.encode()).hexdigest() != row_hash:
            return False
        if (data["actor_id"] != context.actor_id or data["book_id"] != context.book_id
                or data["decision_id"] != context.decision_id or data["cut"] != context.cut
                or data["outcome"] != "success" or data["network_id"] != network_id
                or data["units"].get(field_path) != unit):
            return False
        actual = data["result"]
        for part in field_path.split("."):
            actual = actual[int(part)] if isinstance(actual, list) else actual[part]
        # JSON equality must not let True pass as the numeric value 1.
        return S.canonical_json(actual) == S.canonical_json(value)
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return False
