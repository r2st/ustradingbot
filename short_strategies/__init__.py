"""
Short Selling Strategy Module.

An independent signal-provider package: eleven short-side strategies emit
standardized :class:`~short_strategies.common.signal.ShortSignal` objects,
which are filtered through the shared risk gates and converted to core
``signals.signal_types.Signal`` instances (``direction="short"``) so they
flow through the existing engine pipeline exactly like long signals.

Public API::

    from short_strategies import run_short_scan, get_short_config
"""

from short_strategies.common.config import get_short_config
from short_strategies.scanner import run_short_scan

__all__ = ["run_short_scan", "get_short_config"]
