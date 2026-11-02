"""Crew-owned capabilities; persona text cannot grant them."""
from dataclasses import dataclass
import copy
import math
import json

CAPS = {
    "scout": frozenset({"sheet.read", "intake.read", "browse", "ticket.propose",
                        "desk.near_pass_list", "desk.near_pass_sheet"}),
    "analyst": frozenset({"sheet.read", "ticket.propose"}),
    "judge": frozenset({"decision.bundle", "judge.decide"}),
    "risk": frozenset({"decision.bundle", "book.read_order", "book.read_position"}),
    "trader": frozenset({"decision.bundle", "book.read_order", "book.read_position", "book.submit"}),
    "head": frozenset({"org.read", "org.propose", "ticket.propose", "ticket.approve"}),
    "postmortem": frozenset({"grade.read", "lesson.propose"}),
    "ops": frozenset({"health.read", "ticket.propose"}),
    "quant": frozenset({"research.submit", "research.result", "ticket.propose"}),
}


# Written work grants no execution, identity or policy authority.
for _role in ('head','scout','judge'):
    CAPS[_role]=CAPS[_role] | frozenset({'msg.send','task.set','task.update'})

# These tools read non-slot data. New PS tools must not inherit an unknown slot.
OFFCHAIN_TOOLS = frozenset({"msg.send", "task.set", "task.update", "browse", "intake.read", "ticket.propose", "ticket.approve", "org.read",
                           "org.propose", "health.read", "research.submit", "research.result",
                           "grade.read", "lesson.propose", "judge.decide", "book.read_order", "book.read_position",
                           # near-pass Scout feed: the list reads persisted tickets (no slot); the sheet's cut is each
                           # ticket's own persisted market cut (receipt_cut, cut_slot), never the caller's context cut
                           "desk.near_pass_list", "desk.near_pass_sheet"})


@dataclass(frozen=True)
class Context:
    actor_id: str
    role: str
    book_id: str
    generation: int
    decision_id: str | None
    cut: dict

    def __post_init__(self):
        object.__setattr__(self, "cut", copy.deepcopy(self.cut))


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def cut_refusal(cut):
    """Shared live/replay corrected-UTC cut validation; no per-tool permission changes."""
    if (not isinstance(cut, dict) or cut.get("basis") != "corrected_utc" or not all(_finite(cut.get(k)) for k in ("receipt_cut", "knowledge_cut"))
            or "slot_cut" not in cut
            or (cut["slot_cut"] is not None and (type(cut["slot_cut"]) is not int or cut["slot_cut"] < 0))
            or not isinstance(cut.get("clock_sample"), dict)
            or not _finite(cut["clock_sample"].get("corrected_utc"))
            or type(cut["clock_sample"].get("offset_s")) not in (int, float)
            or not math.isfinite(cut["clock_sample"]["offset_s"])
            or max(cut["receipt_cut"], cut["knowledge_cut"]) > cut["clock_sample"]["corrected_utc"]):
        return "cut_invalid"
    # Native tape receipt clocks are host time: corrected UTC = host - frozen offset.
    if "host_receipt_cut" in cut and (not _finite(cut["host_receipt_cut"])
            or not math.isclose(cut["host_receipt_cut"] - cut["clock_sample"]["offset_s"],
                                cut["receipt_cut"], rel_tol=0., abs_tol=1e-6)):
        return "cut_invalid"
    return None


def refusal(context, request):
    if not isinstance(request.tool, str) or not isinstance(request.actor_id, str):
        return "request_invalid"
    if request.actor_id != context.actor_id:
        return "actor_mismatch"
    if request.decision_id != context.decision_id:
        return "decision_mismatch"
    cut = context.cut
    reason = cut_refusal(cut)
    if reason:
        return reason
    try:
        same = json.dumps(request.cut, sort_keys=True, allow_nan=False) == json.dumps(cut, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        same = False
    if not same:
        return "cut_mismatch"
    if request.tool not in CAPS.get(context.role, frozenset()):
        return "tool_denied"
    if cut["slot_cut"] is None and request.tool not in OFFCHAIN_TOOLS:
        return "slot_unknown"
    if not isinstance(request.args, dict):
        return "args_invalid"
    if request.tool.startswith("book.") and request.args.get("book_id") != context.book_id:
        return "book_mismatch"
    return None
