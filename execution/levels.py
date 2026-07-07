"""
Multi-level exit ladders for manual trades (feature 2, extended).

A manual trade can carry several stop-loss levels and several profit-target
levels, each exiting a slice of the position (e.g. stop 1 at -3% for 33%,
stop 2 at -5% for 33%, stop 3 at -8% for the rest; targets mirrored on the
upside).  This module holds the *pure* building blocks — parsing, validation,
and share splitting — shared by the manual-trade entry point and the brokers,
so they are unit-testable without a broker or network.

Levels are also the designed extension point for future *dynamic* stops: a
level is a plain ``(kind, price, quantity)`` record, so a later dynamic-stop
engine can re-price untriggered stop levels (see
:func:`reprice_stop_levels`) without changing the trigger/fill machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

#: Hard cap on levels per ladder — keeps forms/state sane.
MAX_LEVELS_PER_LADDER = 5

#: Supported trade sides.  ``buy`` opens a long, ``sell`` opens a short.
SIDES = ("long", "short")


class LevelError(ValueError):
    """Raised when an exit-level specification is invalid."""


@dataclass
class ExitLevel:
    """One rung of an exit ladder.

    Attributes:
        kind: ``"stop"`` or ``"target"``.
        price: Trigger price.
        quantity: Shares to exit when this level triggers.
        triggered: Whether this level has already fired.
    """

    kind: str
    price: float
    quantity: int
    triggered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "price": self.price,
            "quantity": self.quantity,
            "triggered": self.triggered,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExitLevel":
        return cls(
            kind=str(d.get("kind", "stop")),
            price=float(d.get("price", 0.0) or 0.0),
            quantity=int(d.get("quantity", 0) or 0),
            triggered=bool(d.get("triggered", False)),
        )


def normalize_side(side: Any) -> str:
    """Map a UI/API side value to ``"long"`` / ``"short"``.

    Accepts ``buy``/``long`` and ``sell``/``short`` (any case).
    """
    s = str(side or "buy").strip().lower()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    raise LevelError(f"side must be 'buy' or 'sell', got {side!r}.")


def parse_level_specs(
    raw: Any,
    kind: str,
    side: str,
    entry_price: float,
) -> List[Dict[str, float]]:
    """Parse raw level specs into ``[{"price": p, "pct": pct}, ...]``.

    Each raw item may specify the trigger as an absolute ``price`` or as a
    ``percent`` move from entry (e.g. ``-3`` for a 3% stop on a long; the sign
    is normalised from the level *kind* and *side*, so ``3`` and ``-3`` mean
    the same stop distance).  ``pct`` is the percentage of the position to
    exit at the level (0 < pct <= 100); when omitted the ladder is split
    evenly, with the final level absorbing any remainder.

    Raises:
        LevelError: on malformed input, wrong-side prices, or duplicates.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise LevelError(f"{kind}s must be a list of levels.")
    if len(raw) > MAX_LEVELS_PER_LADDER:
        raise LevelError(
            f"At most {MAX_LEVELS_PER_LADDER} {kind} levels are supported."
        )

    parsed: List[Dict[str, float]] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise LevelError(f"{kind} level {i} must be an object.")
        price = _level_price(item, kind, side, entry_price, i)
        pct = item.get("pct", None)
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                raise LevelError(f"{kind} level {i}: pct must be a number.") from None
            if not (0.0 < pct <= 100.0):
                raise LevelError(
                    f"{kind} level {i}: pct must be in (0, 100], got {pct}."
                )
        parsed.append({"price": price, "pct": pct})

    _validate_prices([p["price"] for p in parsed], kind, side, entry_price)

    total_pct = sum(p["pct"] for p in parsed if p["pct"] is not None)
    if total_pct > 100.0 + 1e-6:
        raise LevelError(
            f"{kind} level percentages sum to {total_pct:g}% (must be <= 100%)."
        )

    # Sort nearest-to-entry first: that is the order levels trigger in as
    # price moves away from entry.
    reverse = (kind == "stop") == (side == "long")
    parsed.sort(key=lambda p: p["price"], reverse=reverse)
    return parsed


def _level_price(
    item: Dict[str, Any], kind: str, side: str, entry_price: float, i: int
) -> float:
    """Resolve one level's trigger price from ``price`` or ``percent``."""
    raw_price = item.get("price", None)
    raw_percent = item.get("percent", None)
    if raw_price not in (None, ""):
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            raise LevelError(f"{kind} level {i}: price must be a number.") from None
    elif raw_percent not in (None, ""):
        try:
            magnitude = abs(float(raw_percent)) / 100.0
        except (TypeError, ValueError):
            raise LevelError(f"{kind} level {i}: percent must be a number.") from None
        # Stops sit on the losing side of entry, targets on the winning side.
        losing = kind == "stop"
        below_entry = losing if side == "long" else not losing
        factor = 1.0 - magnitude if below_entry else 1.0 + magnitude
        price = entry_price * factor
    else:
        raise LevelError(f"{kind} level {i}: give either a price or a percent.")
    if price <= 0:
        raise LevelError(f"{kind} level {i}: price must be positive.")
    return round(price, 4)


def _validate_prices(
    prices: List[float], kind: str, side: str, entry_price: float
) -> None:
    """Enforce that every level sits on the correct side of entry, no dupes."""
    if len(set(prices)) != len(prices):
        raise LevelError(f"Duplicate {kind} level prices.")
    for price in prices:
        if side == "long":
            ok = price < entry_price if kind == "stop" else price > entry_price
            expect = "below" if kind == "stop" else "above"
        else:
            ok = price > entry_price if kind == "stop" else price < entry_price
            expect = "above" if kind == "stop" else "below"
        if not ok:
            raise LevelError(
                f"Every {kind} level must be {expect} the entry price for a "
                f"{'buy' if side == 'long' else 'sell'} trade "
                f"(got {price} vs entry {entry_price})."
            )


def split_shares(quantity: int, pcts: List[Optional[float]]) -> List[int]:
    """Split *quantity* shares across ladder slices.

    Slices with an explicit pct get ``floor(quantity * pct/100)``; slices
    without one share the unallocated percentage evenly.  The final slice
    absorbs all rounding/remainder so the ladder always covers the full
    position.  Slices that round to zero shares stay zero (their allocation
    flows into the final slice via the remainder).
    """
    quantity = int(quantity)
    n = len(pcts)
    if quantity <= 0 or n == 0:
        return [0] * n

    explicit = sum(p for p in pcts if p is not None)
    implicit_n = sum(1 for p in pcts if p is None)
    implicit_pct = max(0.0, 100.0 - explicit) / implicit_n if implicit_n else 0.0
    resolved = [p if p is not None else implicit_pct for p in pcts]

    shares = [int(quantity * p / 100.0) for p in resolved[:-1]]
    shares.append(max(0, quantity - sum(shares)))
    return shares


def build_levels(
    side: str,
    entry_price: float,
    quantity: int,
    stop_specs: Any,
    target_specs: Any,
) -> List[ExitLevel]:
    """Build the full exit ladder for a manual trade.

    Parses + validates both ladders, splits the position quantity across each
    ladder independently (stops cover 100% of the position, targets cover
    100% of the position — whichever side triggers first reduces the live
    quantity, and later levels exit at most what remains).

    Raises:
        LevelError: when either ladder is missing or invalid.
    """
    side = normalize_side(side)
    if entry_price <= 0:
        raise LevelError("entry_price must be positive.")
    if quantity <= 0:
        raise LevelError("quantity must be a positive integer.")

    stops = parse_level_specs(stop_specs, "stop", side, entry_price)
    targets = parse_level_specs(target_specs, "target", side, entry_price)
    if not stops:
        raise LevelError("At least one stop level is required.")
    if not targets:
        raise LevelError("At least one target level is required.")

    levels: List[ExitLevel] = []
    for kind, specs in (("stop", stops), ("target", targets)):
        qtys = split_shares(quantity, [s["pct"] for s in specs])
        for spec, qty in zip(specs, qtys):
            levels.append(ExitLevel(kind=kind, price=spec["price"], quantity=qty))
    return levels


def reprice_stop_levels(
    levels: List[ExitLevel], new_prices: Dict[int, float]
) -> List[ExitLevel]:
    """Return a copy of *levels* with untriggered stops re-priced.

    Extension hook for the future dynamic-stop engine: ``new_prices`` maps the
    index of a stop level (within the untriggered stop sub-list, 0-based) to
    its new trigger price.  Triggered levels and targets are never touched.
    """
    out: List[ExitLevel] = []
    stop_i = 0
    for lvl in levels:
        if lvl.kind == "stop" and not lvl.triggered:
            price = new_prices.get(stop_i, lvl.price)
            out.append(ExitLevel(lvl.kind, round(price, 4), lvl.quantity, lvl.triggered))
            stop_i += 1
        else:
            out.append(ExitLevel(lvl.kind, lvl.price, lvl.quantity, lvl.triggered))
    return out


def nearest_price(levels: List[ExitLevel], kind: str, side: str) -> float:
    """Return the nearest-to-entry untriggered level price for *kind*.

    Used to populate the legacy single ``stop_price`` / ``target_price``
    fields (journal rows, position views) for a laddered position.
    """
    prices = [l.price for l in levels if l.kind == kind and not l.triggered]
    if not prices:
        return 0.0
    if kind == "stop":
        return max(prices) if side == "long" else min(prices)
    return min(prices) if side == "long" else max(prices)
