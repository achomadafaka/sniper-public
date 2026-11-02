from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


OPS = {"lt", "lte", "gt", "gte", "eq", "in", "between"}
NUMERIC_OPS = {"lt", "lte", "gt", "gte", "between"}
FIELD_TYPES = {
    "network": ("string", False),
    "market_cap": ("number", True),
    "size_usd": ("number", True),
    "was_curated": ("boolean", True),
    "was_real_approved": ("boolean", True),
    "distinct_buyer_count": ("integer", False),
    "window_s": ("integer", False),
    "thesis_stance": ("stance", True),
    "thesis_score": ("number", True),
    "source_lag_s": ("number", True),
    "token_age_s": ("number", True),
    "liquidity": ("number", True),
    "event_type": ("string", False),
}
STANCE_VALUES = {"winning", "underwater", "flat", "unknown"}


def _whole_positive(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be whole seconds")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be whole seconds") from None
    floor = 0 if allow_zero else 1
    if not math.isfinite(numeric) or numeric < floor or not numeric.is_integer():
        raise ValueError(f"{name} must be whole seconds")
    return int(numeric)


def _valid_scalar(value, kind, nullable):
    if value is None:
        return nullable
    if kind == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "stance":
        return isinstance(value, str) and value in STANCE_VALUES
    return False


@dataclass(frozen=True)
class Clause:
    field: str
    op: str
    value: Any
    kind: str
    nullable: bool

    def matches(self, features):
        actual = features.get(self.field)
        if actual is None:
            return ((self.op == "eq" and self.value is None)
                    or (self.op == "in" and None in self.value))
        if not _valid_scalar(actual, self.kind, self.nullable):
            return False
        if self.op == "eq":
            return actual == self.value
        if self.op == "in":
            return actual in self.value
        if self.op == "lt":
            return actual < self.value
        if self.op == "lte":
            return actual <= self.value
        if self.op == "gt":
            return actual > self.value
        if self.op == "gte":
            return actual >= self.value
        if self.op == "between":
            return self.value[0] <= actual <= self.value[1]
        return False


@dataclass(frozen=True)
class Predicate:
    clauses: tuple[Clause, ...]
    window_s: int
    freshness_s: int

    def matches(self, features):
        return all(clause.matches(features) for clause in self.clauses)


def compile_predicate(raw):
    if not isinstance(raw, dict) or set(raw) - {"clauses", "window_s", "freshness_s"}:
        raise ValueError("predicate must be an AND-only clause object")
    clauses_raw = raw.get("clauses")
    if not isinstance(clauses_raw, list) or not clauses_raw:
        raise ValueError("predicate requires at least one clause")
    window_s = _whole_positive(raw.get("window_s", 0), "window_s", allow_zero=True)
    freshness_s = _whole_positive(raw.get("freshness_s", 120), "freshness_s")
    clauses = []
    for item in clauses_raw:
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"}:
            raise ValueError("each predicate clause requires field, op, and value")
        field, op, value = item["field"], item["op"], item["value"]
        if field not in FIELD_TYPES:
            raise ValueError(f"unknown predicate field: {field}")
        if op not in OPS:
            raise ValueError(f"unsupported predicate operator: {op}")
        kind, nullable = FIELD_TYPES[field]
        if op in NUMERIC_OPS and kind not in {"number", "integer"}:
            raise ValueError(f"operator {op} requires a numeric field")
        if op in {"in", "between"}:
            if not isinstance(value, list) or not value or (op == "between" and len(value) != 2):
                raise ValueError(f"operator {op} requires a non-empty list")
            if not all(_valid_scalar(part, kind, nullable and op == "in") for part in value):
                raise ValueError(f"invalid value type for {field}")
            value = tuple(value)
        elif not _valid_scalar(value, kind, nullable and op == "eq"):
            raise ValueError(f"invalid value type for {field}")
        clauses.append(Clause(field, op, value, kind, nullable))
    return Predicate(tuple(clauses), window_s, freshness_s)
