"""
FastAPI web dashboard for the US Trading Bot.

Provides a read-only browser view of system configuration, strategy
parameters, demo signal scoring, and risk management rules.

Start with::

    uvicorn dashboard.app:app --host 0.0.0.0 --port 8501 --reload
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so we can import config / signals / risk
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import Settings, momentum_weights, swing_weights
from config.universe import ALL_SYMBOLS, CA_WATCHLIST, US_WATCHLIST
from signals.signal_types import Grade

# ---------------------------------------------------------------------------
# App & templates
# ---------------------------------------------------------------------------
app = FastAPI(title="US Trading Bot Dashboard", version="0.1.0")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# ---------------------------------------------------------------------------
# Helpers — build data dicts consumed by the template
# ---------------------------------------------------------------------------

def _build_system_status() -> Dict[str, Any]:
    """Gather system status and configuration summary."""
    settings = Settings()
    return {
        "bot_version": "0.1.0",
        "mode": "Paper Trading" if settings.IS_PAPER_TRADING else "LIVE",
        "ibkr_host": settings.IBKR_HOST,
        "ibkr_port": settings.IBKR_PORT,
        "ibkr_client_id": settings.IBKR_CLIENT_ID,
        "total_capital": f"${settings.TOTAL_CAPITAL:,.0f}",
        "usd_capital": f"${settings.CAPITAL_BY_CURRENCY.get('USD', 0):,.0f}",
        "cad_capital": f"${settings.CAPITAL_BY_CURRENCY.get('CAD', 0):,.0f}",
        "scan_interval": f"{settings.SCAN_INTERVAL_MINUTES} min",
        "log_level": settings.LOG_LEVEL,
        "data_dir": str(settings.DATA_DIR),
        "market_hours": (
            f"{settings.MARKET_OPEN_HOUR}:{settings.MARKET_OPEN_MINUTE:02d} – "
            f"{settings.MARKET_CLOSE_HOUR}:{settings.MARKET_CLOSE_MINUTE:02d} ET"
        ),
        "us_symbols": len(US_WATCHLIST),
        "ca_symbols": len(CA_WATCHLIST),
        "total_symbols": len(ALL_SYMBOLS),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _build_strategies() -> List[Dict[str, Any]]:
    """Return strategy descriptions and weight configurations."""
    settings = Settings()
    return [
        {
            "name": "Momentum",
            "family": "momentum",
            "description": (
                "Trend-following strategy targeting stocks with strong upward "
                "momentum across multiple timeframes. Looks for EMA alignment, "
                "rising MACD, healthy volume, and bullish Ripster cloud structure."
            ),
            "max_positions": settings.MAX_MOMENTUM_POSITIONS,
            "weights": momentum_weights,
            "size_modifier": "100%",
        },
        {
            "name": "VCP Breakout",
            "family": "momentum",
            "description": (
                "Volatility Contraction Pattern — identifies stocks forming "
                "tightening consolidation with decreasing volume, ready for "
                "a breakout. Uses momentum-family weights."
            ),
            "max_positions": settings.MAX_MOMENTUM_POSITIONS,
            "weights": momentum_weights,
            "size_modifier": "100% (shared cap with Momentum)",
        },
        {
            "name": "PEAD",
            "family": "pead",
            "description": (
                "Post-Earnings Announcement Drift — captures the tendency for "
                "stocks to continue drifting in the direction of an earnings "
                "surprise. Uses momentum-family weights."
            ),
            "max_positions": settings.MAX_PEAD_POSITIONS,
            "weights": momentum_weights,
            "size_modifier": "100%",
        },
        {
            "name": "Swing",
            "family": "swing",
            "description": (
                "Mean-to-trend swing trading targeting oversold bounces in "
                "uptrending stocks. Heavier RSI and EMA weighting to catch "
                "pullback entries near support."
            ),
            "max_positions": settings.MAX_SWING_POSITIONS,
            "weights": swing_weights,
            "size_modifier": "100%",
        },
        {
            "name": "Mean Reversion",
            "family": "mean_reversion",
            "description": (
                "Counter-trend strategy betting on reversals from extreme "
                "oversold conditions. Half position size, max 1 concurrent "
                "position. Uses swing-family weights."
            ),
            "max_positions": 1,
            "weights": swing_weights,
            "size_modifier": "50%",
        },
    ]


def _build_sample_scores() -> List[Dict[str, Any]]:
    """Generate sample scored signals for the demo view."""
    samples = [
        {
            "symbol": "NVDA",
            "strategy": "momentum",
            "entry": 142.50,
            "stop": 136.25,
            "target": 153.75,
            "rsi_score": 0.82,
            "macd_score": 0.90,
            "ema_score": 0.95,
            "volume_score": 0.78,
            "ripster_score": 0.85,
        },
        {
            "symbol": "AAPL",
            "strategy": "swing",
            "entry": 198.30,
            "stop": 193.10,
            "target": 207.64,
            "rsi_score": 0.70,
            "macd_score": 0.65,
            "ema_score": 0.75,
            "volume_score": 0.60,
            "ripster_score": 0.68,
        },
        {
            "symbol": "SHOP.TO",
            "strategy": "swing",
            "entry": 105.20,
            "stop": 100.80,
            "target": 113.12,
            "rsi_score": 0.55,
            "macd_score": 0.50,
            "ema_score": 0.62,
            "volume_score": 0.48,
            "ripster_score": 0.52,
        },
        {
            "symbol": "META",
            "strategy": "momentum",
            "entry": 510.75,
            "stop": 498.20,
            "target": 533.34,
            "rsi_score": 0.88,
            "macd_score": 0.92,
            "ema_score": 0.90,
            "volume_score": 0.85,
            "ripster_score": 0.80,
        },
        {
            "symbol": "TSLA",
            "strategy": "vcp_breakout",
            "entry": 255.40,
            "stop": 248.60,
            "target": 267.64,
            "rsi_score": 0.45,
            "macd_score": 0.40,
            "ema_score": 0.50,
            "volume_score": 0.35,
            "ripster_score": 0.30,
        },
        {
            "symbol": "AMD",
            "strategy": "mean_reversion",
            "entry": 164.80,
            "stop": 160.50,
            "target": 172.54,
            "rsi_score": 0.30,
            "macd_score": 0.25,
            "ema_score": 0.35,
            "volume_score": 0.40,
            "ripster_score": 0.28,
        },
    ]

    results = []
    for s in samples:
        # Determine weights
        strategy = s["strategy"].lower()
        if strategy in ("momentum", "vcp_breakout", "pead"):
            w = momentum_weights
        else:
            w = swing_weights

        combined = (
            w["rsi"] * s["rsi_score"]
            + w["macd"] * s["macd_score"]
            + w["ema"] * s["ema_score"]
            + w["volume"] * s["volume_score"]
            + w["ripster"] * s["ripster_score"]
        )
        combined = round(min(max(combined, 0.0), 1.0), 4)
        grade = Grade.from_score(combined)

        risk = s["entry"] - s["stop"]
        reward = s["target"] - s["entry"]
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        results.append(
            {
                **s,
                "combined_score": combined,
                "grade": grade.value,
                "grade_class": (
                    "grade-a" if grade == Grade.A else
                    "grade-b" if grade == Grade.B else
                    "grade-c" if grade == Grade.C else
                    "grade-f"
                ),
                "risk_reward": rr,
                "risk_per_share": round(risk, 2),
            }
        )

    results.sort(key=lambda x: x["combined_score"], reverse=True)
    return results


def _build_risk_rules() -> Dict[str, Any]:
    """Collect risk management parameters."""
    settings = Settings()
    return {
        "max_position_size_pct": f"{settings.MAX_POSITION_SIZE_PCT * 100:.1f}%",
        "max_open_positions": settings.MAX_OPEN_POSITIONS,
        "daily_loss_limit_pct": f"{settings.DAILY_LOSS_LIMIT_PCT * 100:.1f}%",
        "daily_loss_limit_usd": f"${settings.TOTAL_CAPITAL * settings.DAILY_LOSS_LIMIT_PCT:,.0f}",
        "atr_stop_multiplier": f"{settings.ATR_STOP_MULTIPLIER}x ATR(14)",
        "risk_reward_min": f"{settings.RISK_REWARD_MIN}:1",
        "hold_max_days": settings.HOLD_MAX_DAYS,
        "reentry_cooldown": f"{settings.REENTRY_COOLDOWN_MINUTES} min",
        "long_cooldown": "24 hours (stop hit / setup broken)",
        "order_cutoff": f"{settings.ORDER_CUTOFF_MINUTES_BEFORE_CLOSE} min before close",
        "ghost_max_defer": f"{settings.GHOST_POSITION_MAX_DEFER_HOURS} hours",
        "partial_take_trail": "Enabled" if settings.ENABLE_PARTIAL_TAKE_TRAIL else "Disabled",
        "min_atr_pct": f"{settings.MIN_ATR_PCT * 100:.1f}%",
        "grade_thresholds": {
            "A": f">= {settings.GRADE_A_THRESHOLD} (100% size)",
            "B": f">= {settings.GRADE_B_THRESHOLD} (75% size)",
            "C": f">= {settings.GRADE_C_THRESHOLD} (skip)",
            "F": f"< {settings.GRADE_C_THRESHOLD} (block)",
        },
        "hard_vetoes": [
            "Bear Regime — price below EMA-200",
            "OBV Divergence — price up but OBV down over 10 days",
            "Bearish Volume Surge — down day with volume > 1.5x average",
            "Ripster Cross Below — price below both EMA clouds",
            f"Low ATR — ATR% < {settings.MIN_ATR_PCT * 100:.1f}%",
        ],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the main dashboard page."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "status": _build_system_status(),
            "strategies": _build_strategies(),
            "scores": _build_sample_scores(),
            "risk": _build_risk_rules(),
            "watchlist_us": US_WATCHLIST,
            "watchlist_ca": CA_WATCHLIST,
        },
    )


@app.get("/health")
async def health():
    """Simple health-check endpoint."""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
