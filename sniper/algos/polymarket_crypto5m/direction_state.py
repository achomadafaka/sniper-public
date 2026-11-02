"""Memory/confirmation layer for the crypto5m scalper — the missing 'distinguish a real direction from a
market micro-adjustment'.

`direction.directional_thesis` gives an INSTANTANEOUS read each tick; it has no memory, so the scalper used
to commit and un-commit on noise. DirectionState sits between the thesis and the scalper and confirms a
direction only when it is REAL: the read sign has PERSISTED across ticks (memory), a short and a longer
horizon AGREE (a trend shows at multiple scales; a wiggle at one), the tape ORDER FLOW agrees, and the read
clears the conviction bar. Pure + unit-tested; the runner holds one instance and calls .update() each tick.
"""
from __future__ import annotations

DEFAULT_PERSIST_TICKS = 3          # consecutive same-sign updates (~6s at a 2s poll) = memory
DEFAULT_PERSIST_WINDOW_S = 12      # only remember this far back (bounds the history)
DEFAULT_ENTER_CONVICTION = 0.50    # strength floor (matches the old scalp_trader gate)


def _sign(side):
    return 1 if side == "Up" else (-1 if side == "Down" else 0)


def _agree(side_sign, *vals):
    """True iff every PRESENT value shares the side's sign (None values are ignored, but at least one must
    be present)."""
    present = [v for v in vals if v is not None]
    return bool(present) and all((v > 0) == (side_sign > 0) for v in present)


class DirectionState:
    def __init__(self, persist_ticks=DEFAULT_PERSIST_TICKS, persist_window_s=DEFAULT_PERSIST_WINDOW_S,
                 enter_conviction=DEFAULT_ENTER_CONVICTION):
        self.persist_ticks = persist_ticks
        self.persist_window_s = persist_window_s
        self.enter_conviction = enter_conviction
        self._hist = []                                  # list of (ts, side) most-recent last

    def _run_length(self, side):
        """How many consecutive most-recent updates share `side` (the memory depth of the current read)."""
        if not side:
            return 0
        n = 0
        for _, s in reversed(self._hist):
            if s == side:
                n += 1
            else:
                break
        return n

    def update(self, thesis, desk, now) -> dict:
        side = thesis.get("side")
        self._hist.append((now, side))
        self._hist = [(t, s) for (t, s) in self._hist if t >= now - self.persist_window_s]
        sign = _sign(side)
        persistence = self._run_length(side)
        horizons_agree = _agree(sign, desk.get("ret_15s"), desk.get("ret_30s")) if side else False
        of_vote = (thesis.get("votes") or {}).get("order_flow")
        flow_agree = of_vote is not None and of_vote == sign and sign != 0
        conviction = float(thesis.get("conviction") or 0.0)
        confirmed = bool(side and persistence >= self.persist_ticks and horizons_agree
                         and flow_agree and conviction >= self.enter_conviction)
        return {"confirmed": confirmed, "confirmed_side": side if confirmed else None,
                "read_side": side, "persistence_ticks": persistence, "horizons_agree": horizons_agree,
                "flow_agree": flow_agree, "conviction": conviction}
