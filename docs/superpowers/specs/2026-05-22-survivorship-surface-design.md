# Spec — Survivorship Surface (`survivorship_surface`)

**Date:** 2026-05-22
**Status:** design — operator pre-scoped, ready for implementation
**Origin:** Item 6 of the analyzer "shortsightedness" backlog. Every wallet
the analyzer sees was handed to it because it *won* — leaderboards and
top-PnL lists surface survivors. A wallet that ran the same strategy and
busted is invisible. Operator scoping (verbatim): *"Survivorship is mostly
an honesty caveat until we have loser cohorts … add it as a report/display
caveat later, self-contained."* This surface is that caveat.

## 1. Problem

A full survivorship correction needs a **loser cohort** — wallets that ran
a strategy class and failed — and a base rate across all who tried it. We
do not have that and cannot get it cheaply (discovery is winner-biased).
What we CAN do, honestly and self-contained, is **flag the discovery
basis**: when the analyzer knows a wallet was found via a winner-ranked
list, say so plainly, so no downstream reader treats a positive read as
unconditional. The surface corrects nothing — it discloses.

## 2. Scope

One display-only surface, `survivorship_surface`, on `ProtocolReport`.
Pure function of the existing `triage_context` (the G3 discovery-context
field). **Out of scope:** any actual survivorship correction, loser-cohort
construction, base-rate estimation, or verdict/gate coupling. This is a
disclosure caveat, nothing more.

## 3. Surface schema

`report.survivorship_surface: dict | None` — `None` only on the
pre-surface backward-compat path. When produced, a dict:

| Key | Type | Meaning |
|---|---|---|
| `surface_version` | int | schema version (1) |
| `discovery_source` | str \| None | `triage_context["source"]` verbatim, or `None` when no triage context was supplied |
| `survivor_sampled` | bool \| None | `True` — discovery source is a winner-ranked list (survivorship bias present); `None` — no triage context, or source not recognized as a winner-list (bias status unknown, not asserted clean) |
| `caveat` | str \| None | the honesty caveat string when `survivor_sampled` is `True`; else `None` |
| `note` | str | the always-present structural statement (§5) |

## 4. Producer algorithm — `compute_survivorship_surface()`

`sniper/analysis/wallet_protocol/survivorship.py`:

```
compute_survivorship_surface(triage_context: dict | None) -> dict
```

Pure; no I/O; deterministic.

- `discovery_source` = `str(triage_context["source"])` when `triage_context`
  is a dict with a non-empty `source`; else `None`.
- `survivor_sampled` = `True` when `discovery_source` lowercased contains any
  winner-list marker: `leaderboard`, `top_pnl`, `top-pnl`, `winner`,
  `top_wallet`. Otherwise `None` — the surface never asserts `False`
  ("verified unbiased"), because discovery neutrality cannot be confirmed
  from a source string alone.
- `caveat` — when `survivor_sampled` is `True`:
  `"SURVIVORSHIP_BIAS: discovered via <source> — a winner-ranked list.
  Wallets that ran a similar strategy and lost are not observed; the
  strategy class's base rate is unknown. Treat any positive read as
  conditioned on survival."` Else `None`.

## 5. The `note` (always present)

`"Survivorship surface is display-only — it does not change verdict, gate,
edge type, copyability note, or quality flags. It flags only HOW the wallet
was discovered; it cannot correct for survivorship, which would require a
loser cohort (out of scope)."`

## 6. Hard wall — non-negotiable

Identical to the sibling surfaces (`capital_risk`, `sizing_outcome`,
`null_baseline`):
- **Display-only.** Read ONLY by the report renderer. MUST NOT feed
  `overall_verdict`, `gate_result`, `primary_edge_type`,
  `copyability_note`, branch/hypothesis logic, or the Strategy Pattern
  Audit. (This is the operator's explicit constraint — a survivorship
  signal that touched verdict semantics would contaminate calibration.)
- **No `data_quality_flags` append.** The caveat lives in the surface's
  own `caveat` field.
- **No live I/O.** Pure function of the already-supplied `triage_context`.
- `types.py` carries the field with the standard hard-wall comment.

## 7. Wiring

- `types.py` — `survivorship_surface: dict | None = None` on
  `ProtocolReport`, hard-wall comment.
- `analyze_wallet.py` — one call alongside the `null_baseline_surface`
  call, fed the `triage_context` argument.
- `report.py` — a `## Survivorship` markdown block.

## 8. Acceptance tests — `tests/test_wallet_protocol_survivorship_surface.py`

1. **Leaderboard source flagged** — `triage_context={"source":
   "polymarket_public_leaderboard"}` → `survivor_sampled is True`,
   `discovery_source` echoes the source, `caveat` is a non-empty string
   mentioning the source.
2. **Other winner-list markers** — `top_pnl`, `winner`, `top_wallet` each
   → `survivor_sampled is True`.
3. **No triage context** — `triage_context=None` → `survivor_sampled is
   None`, `discovery_source is None`, `caveat is None`.
4. **Unrecognized source** — `triage_context={"source": "manual_add"}` →
   `survivor_sampled is None` (never `False`), `caveat is None`,
   `discovery_source == "manual_add"`.
5. **`note` always present** — non-empty `note` in every case above.
6. **Hard wall** — a garbage-stub producer leaves verdict / gate /
   primary_edge_type / copyability_note / `data_quality_flags`
   byte-identical (mutation-proof).
7. Field present + dict-shaped on the live path; `None` only pre-surface.
8. Replay determinism — same input → byte-identical surface JSON.
9. Backward-compat — a `ProtocolReport` built without the field → `None`.
10. Scope-guard field-set tests (`test_wallet_protocol_honesty_floor.py`,
    `test_wallet_protocol_tag_grain_rescue_floor.py`) updated to include
    `survivorship_surface`.

## 9. Out of scope

Loser-cohort construction · base-rate estimation · any actual
survivorship correction · verdict / gate / Strategy-Pattern-Audit
coupling. This surface discloses the discovery basis and stops there.
