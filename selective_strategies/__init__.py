"""
Highly Selective Strategies module.

Low-frequency, high-selectivity strategies that stack multiple independent
filters so each setup fires rarely but with cleaner statistical edge.
"""

from selective_strategies.config import SelectiveConfig, get_selective_config
from selective_strategies.scanner import run_selective_scan

__all__ = ["SelectiveConfig", "get_selective_config", "run_selective_scan"]
