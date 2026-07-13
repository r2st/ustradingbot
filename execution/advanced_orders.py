"""
Pure helpers for advanced order execution.

These functions carry no broker or network state so they can be unit-tested in
isolation and shared identically by the :class:`PaperBroker` and
:class:`IBKRBroker`:

* :func:`compute_scale_in_tranches` — split a target position into N tranches
  at successively lower limit prices.
* :func:`partial_take_split` — split a quantity into a "take" slice and a
  "runner" slice for partial profit-taking.
* :func:`partial_take_price` — the first-target price at R multiples of risk.
* :func:`expiry_at` / :func:`is_expired` — limit-order time-in-force maths.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from config.settings import EASTERN


def compute_scale_in_tranches(
    entry_price: float,
    total_quantity: int,
    n_tranches: int,
    step_pct: float,
) -> List[Tuple[float, int]]:
    """Split an entry into *n_tranches* ``(limit_price, quantity)`` tranches.

    The first tranche rests at *entry_price*; each subsequent tranche is
    ``step_pct`` lower than the previous one, so a pullback improves the
    average fill.  Quantity is divided as evenly as possible with any remainder
    added to the first (best-priced) tranche.

    Returns an empty list when the inputs cannot produce at least one share.
    Degrades to a single full-size tranche when *n_tranches* <= 1.
    """
    total_quantity = int(total_quantity)
    if total_quantity <= 0 or entry_price <= 0:
        return []
    n = max(1, int(n_tranches))
    if n == 1:
        return [(round(entry_price, 4), total_quantity)]

    # Cannot split fewer shares than tranches — fall back to a single tranche.
    if total_quantity < n:
        return [(round(entry_price, 4), total_quantity)]

    base_qty = total_quantity // n
    remainder = total_quantity - base_qty * n

    tranches: List[Tuple[float, int]] = []
    for i in range(n):
        price = entry_price * (1.0 - step_pct * i)
        qty = base_qty + (remainder if i == 0 else 0)
        tranches.append((round(price, 4), int(qty)))
    return tranches


def partial_take_split(quantity: int, take_pct: float) -> Tuple[int, int]:
    """Split *quantity* into ``(take_qty, runner_qty)`` for partial profit-taking.

    *take_qty* is ``round(quantity * take_pct)`` clamped to ``[0, quantity]``.
    When the split would leave nothing to run (or nothing to take) it returns
    ``(0, quantity)`` so the caller keeps the position whole rather than closing
    it via the partial path.
    """
    quantity = int(quantity)
    if quantity <= 1 or not (0.0 < take_pct < 1.0):
        return 0, quantity
    take_qty = int(round(quantity * take_pct))
    take_qty = max(1, min(take_qty, quantity - 1))
    return take_qty, quantity - take_qty


def partial_take_price(
    entry_price: float,
    stop_price: float,
    target_r: float,
) -> Optional[float]:
    """Return the first-target price at *target_r* times the initial risk.

    ``entry + target_r * (entry - stop)``.  Returns ``None`` when the risk is
    non-positive (a malformed stop) so the caller skips partial-taking.
    """
    risk = entry_price - stop_price
    if risk <= 0:
        return None
    return round(entry_price + target_r * risk, 4)


def expiry_at(placed_at: datetime, expiry_hours: float) -> datetime:
    """Return the absolute expiry time for a resting order."""
    return placed_at + timedelta(hours=float(expiry_hours))


def is_expired(
    placed_at: Any,
    expiry_hours: float,
    now: Optional[datetime] = None,
) -> bool:
    """Return whether a resting order placed at *placed_at* has expired.

    *placed_at* may be a ``datetime`` or an ISO string.  An unparseable value
    is treated as *not* expired (fail-safe — never silently drop an order we
    cannot time).
    """
    now = now or datetime.now(tz=EASTERN)
    if isinstance(placed_at, datetime):
        placed = placed_at
    else:
        try:
            placed = datetime.fromisoformat(str(placed_at))
        except (ValueError, TypeError):
            return False
    if expiry_hours <= 0:
        return False
    # Either side may be tz-naive: ``placed_at`` from a bare ``datetime.now()``
    # or an ISO string persisted before timezones were tracked, and a caller may
    # pass a naive ``now``.  Assume naive timestamps are Eastern so the two are
    # always compared on the same footing and the subtraction never raises.
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    return now >= expiry_at(placed, expiry_hours)
