"""Configuration package for the US Trading Bot."""

from config.settings import (
    Settings,
    get_settings,
    momentum_weights,
    swing_weights,
    weights_for_strategy,
)

__all__ = [
    "Settings",
    "get_settings",
    "momentum_weights",
    "swing_weights",
    "weights_for_strategy",
]
