"""Read-only funded-contract reconciliation for deploy and runner startup."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math


_CONTRACT_FIELDS = (
    "funded_contract_version", "funded_contract_hash", "funded_contract_json",
)
_REQUIRED_CONTRACT_KEYS = {
    "contract_version", "identity", "source", "classification", "sizing",
    "policy", "expectation", "invalidations",
}


def _aware_utc(value, *, label):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be an aware ISO timestamp")
    return parsed.astimezone(timezone.utc)


def _table_rows(conn, table):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()
    if not exists:
        return []
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    return [dict(zip(columns, row)) for row in conn.execute(f"SELECT * FROM {table}")]


def _validate_json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("contract JSON contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("contract JSON object key is not a string")
            _validate_json_value(item)
        return
    raise ValueError(f"contract JSON contains unsupported {type(value).__name__}")


def _normalized_network(network_id, mint):
    network = str(network_id or "").strip().lower()
    if network == "1399811149":
        return "solana"
    if network:
        return network
    return "evm-unknown" if str(mint or "").strip().lower().startswith("0x") else "solana"


def _validate_contract_fields(row, *, label):
    values = tuple(row.get(field) for field in _CONTRACT_FIELDS)
    if all(value is None for value in values):
        raise ValueError("missing funded contract")
    if any(value is None for value in values):
        raise ValueError("funded contract fields are incomplete")
    version, expected_hash, payload_json = values
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("funded contract version is invalid")
    try:
        contract = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("funded contract JSON is invalid") from exc
    if not isinstance(contract, dict):
        raise ValueError("funded contract root is not an object")
    _validate_json_value(contract)
    canonical = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if canonical != payload_json or actual_hash != str(expected_hash).lower():
        raise ValueError("funded contract hash does not match canonical JSON")
    if contract.get("contract_version") != version:
        raise ValueError("funded contract version does not match JSON")
    missing = sorted(_REQUIRED_CONTRACT_KEYS - contract.keys())
    if missing:
        raise ValueError(f"funded contract schema is missing {','.join(missing)}")
    if not isinstance(contract.get("policy"), dict) or not contract["policy"].get("policy_id"):
        raise ValueError("funded contract policy is invalid")
    if not isinstance(contract.get("classification"), dict):
        raise ValueError("funded contract classification is invalid")
    if not isinstance(contract.get("sizing"), dict):
        raise ValueError("funded contract sizing is invalid")
    if not isinstance(contract.get("expectation"), dict):
        raise ValueError("funded contract expectation is invalid")
    if not isinstance(contract.get("invalidations"), list):
        raise ValueError("funded contract invalidations are invalid")
    identity = contract.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("funded contract identity is missing")
    if row.get("mint") is not None:
        if str(identity.get("mint")) != str(row.get("mint")):
            raise ValueError(f"funded contract identity does not match {label}")
        if _normalized_network(identity.get("network_id"), identity.get("mint")) != \
                _normalized_network(row.get("network_id"), row.get("mint")):
            raise ValueError(f"funded contract identity does not match {label}")
    return values


def _post_cutover(rows, *, time_field, cutoff, label, errors):
    selected = []
    for row in rows:
        raw = row.get(time_field)
        try:
            observed = _aware_utc(raw, label=f"{label} {time_field}")
        except ValueError as exc:
            if any(row.get(field) is not None for field in _CONTRACT_FIELDS):
                errors.append(f"{label}: {exc}")
            continue
        if observed >= cutoff:
            selected.append(row)
    return selected


_TERMINAL_PENDING_STATUSES = {"confirmed", "failed"}


def _row_is_terminal(kind, row):
    """A terminal row carries no live risk: its money outcome is already settled. A closed
    position and a consumed/failed reservation are audit debt when their contract is bad —
    a live (open / unresolved) row is exactly what the contract regime must protect."""
    if kind == "position":
        return not row.get("is_open")
    return str(row.get("status") or "pending") in _TERMINAL_PENDING_STATUSES


def reconcile_severity(conn, *, cutover_at: str, policy_parse=None) -> tuple[list[str], list[str]]:
    """(blocking, warnings) for post-cutover funded-contract violations.

    Blocking = live risk: an OPEN real position or an unresolved reservation whose contract is
    missing/malformed/mismatched. Warnings = terminal audit debt: the same violations on a
    CLOSED position, a confirmed/failed reservation, or an attempt journal row. Split added
    2026-07-21 after a closed manual position missing its contract bricked armed startup for
    26 minutes while guarding no live risk. The armed runner exits on blocking and pages+logs
    warnings; the DEPLOY validator treats both as fatal (deploys are attended — debt gets
    fixed deliberately, not carried).

    `policy_parse` is INJECTED, not imported, and that is deliberate. Everything else here checks
    that the contract BYTES are present, authentic and consistent across tables; none of it asks
    the only question the exit path cares about — can the sweep turn these bytes into rails? For
    four `out_of_scope` contracts it could not (`_validated_policy_data` accepted four coin bands
    while the writer legitimately writes five), so `frozen_policy_from_position` raised, the sweep
    logged and continued, and those positions rode with NO take-profit, NO stop and NO timecap past
    both gates. Position 44703 was found live 18 minutes past its frozen 780s cap; all four
    `out_of_scope` positions in history closed `manual` — the operator was the stop loss.

    Byte integrity and semantic parseability are DIFFERENT PROPERTIES. Pass
    `frozen_policy_from_position` to check the second. It is a parameter rather than an import
    because this module is loaded by the deploy validator against a RELEASE TREE and must stay
    dependency-light; importing the policy stack here would make the gate need the whole runtime to
    answer a question about bytes. Callers that decide money always pass it."""
    try:
        cutoff = _aware_utc(cutover_at, label="funded contract cutoff")
    except ValueError as exc:
        return ([str(exc)], [])

    blocking, warnings = [], []
    pending_rows = _table_rows(conn, "memecoin_pending_entries")
    position_rows = [
        row for row in _table_rows(conn, "memecoin_positions")
        if str(row.get("execution_mode") or "paper") == "real"
    ]
    attempt_rows = _table_rows(conn, "memecoin_entry_attempts")
    attempts = {row.get("attempt_id"): row for row in attempt_rows if row.get("attempt_id")}
    pending_by_attempt = {
        row.get("attempt_id"): row for row in pending_rows if row.get("attempt_id")
    }
    positions_by_attempt = {
        row.get("attempt_id"): row for row in position_rows if row.get("attempt_id")
    }

    # rows with an unparseable time field are UNKNOWN state -> fail closed (blocking)
    post_pending = _post_cutover(
        pending_rows, time_field="reserved_at", cutoff=cutoff,
        label="post-cutover pending", errors=blocking,
    )
    post_positions = _post_cutover(
        position_rows, time_field="opened_at", cutoff=cutoff,
        label="post-cutover position", errors=blocking,
    )
    post_attempts = _post_cutover(
        attempt_rows, time_field="decision_started_at", cutoff=cutoff,
        label="post-cutover attempt", errors=warnings,
    )

    for kind, rows in (("pending", post_pending), ("position", post_positions)):
        for row in rows:
            sink = warnings if _row_is_terminal(kind, row) else blocking
            label = f"post-cutover {kind} {row.get('id', '?')}"
            try:
                values = _validate_contract_fields(row, label=kind)
            except ValueError as exc:
                sink.append(f"{label}: {exc}")
                continue
            attempt_id = row.get("attempt_id")
            attempt = attempts.get(attempt_id)
            if not attempt_id or attempt is None:
                sink.append(f"{label}: funded contract is missing entry attempt identity")
                continue
            attempt_values = tuple(attempt.get(field) for field in _CONTRACT_FIELDS)
            if attempt_values != values:
                sink.append(f"{label}: funded contract does not match entry attempt")
                continue
            # THE GATE MUST RUN THE PARSE THE SWEEP RUNS (2026-08-26). Everything above checks that
            # the contract BYTES are present, authentic and consistent across tables. None of it
            # asks the only question the exit path cares about: can the sweep turn these bytes into
            # rails? For four `out_of_scope` contracts it could not — `_validated_policy_data`
            # accepted only four coin bands while the writer legitimately writes five — so
            # `frozen_policy_from_position` raised, the sweep logged and continued, and those
            # positions rode with NO take-profit, NO stop and NO timecap. Both gates passed them.
            # Position 44703 was found live, 18 minutes past its frozen 780s cap; all four
            # `out_of_scope` positions in history closed `manual`, i.e. the operator was the stop.
            #
            # Byte integrity and semantic parseability are DIFFERENT PROPERTIES, and checking the
            # first while the money path depends on the second is how a rail-less position stays
            # invisible. Same severity split as everything else here: a live row blocks, a terminal
            # row is audit debt.
            if policy_parse is not None:
                try:
                    policy_parse(dict(row))
                except ValueError as exc:
                    sink.append(
                        f"{label}: the exit sweep cannot parse this contract ({exc})")

    for attempt in post_attempts:
        values = tuple(attempt.get(field) for field in _CONTRACT_FIELDS)
        if all(value is None for value in values):
            continue
        label = f"post-cutover attempt {attempt.get('attempt_id', '?')}"
        try:
            _validate_contract_fields(attempt, label="attempt")
        except ValueError as exc:
            warnings.append(f"{label}: {exc}")
            continue
        attempt_id = attempt.get("attempt_id")
        # A REFUSED attempt never reached reserve_entry — the risk gate rejected it — so having no
        # reservation is the correct outcome, not audit debt. Every other terminal status happens
        # at or after the reservation, and each is 100% linked in the live ledger (filled 93/93,
        # safety_failed 81/81, aborted 33/33, route_failed 6/6, failed 2/2, anomaly 1/1), so this
        # exemption cannot hide the case the check exists for: a contract that reserved and then
        # vanished.
        #
        # It started mattering when MEMECOIN_REAL_ONE_POSITION_PER_COIN was turned off. With the
        # policy on, a second signal for a held coin died before a contract was built. With it off,
        # both racers build one and the loser is refused as "lost reservation race", which left a
        # contract with no reservation and blocked EVERY subsequent deploy.
        if str(attempt.get("terminal_status") or "").strip().lower() == "refused":
            continue
        if attempt_id not in pending_by_attempt and attempt_id not in positions_by_attempt:
            warnings.append(f"{label}: funded contract has no reservation or position")

    return (list(dict.fromkeys(blocking)), list(dict.fromkeys(warnings)))


def validate_funded_contract_reconciliation(conn, *, cutover_at: str) -> list[str]:
    """Deploy-facing strict view: every violation (blocking + terminal audit debt) is fatal.
    The armed runner uses reconcile_severity instead — it exits only on live-risk rows."""
    blocking, warnings = reconcile_severity(conn, cutover_at=cutover_at)
    return list(dict.fromkeys(blocking + warnings))
