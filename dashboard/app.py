"""
FastAPI web dashboard for the US Trading Bot.

Provides a read-only browser view of system configuration, strategy
parameters, demo signal scoring, and risk management rules.

All pages are protected by HTTP Basic Auth (credentials come from
``DASHBOARD_USERNAME`` / ``DASHBOARD_PASSWORD``); only ``/health`` is public
so external monitors can probe liveness.  Bind to localhost by default and put
a TLS-terminating reverse proxy in front for remote access.

Start with::

    uvicorn dashboard.app:app --host 127.0.0.1 --port 8501 --reload
"""

from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.concurrency import run_in_threadpool

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so we can import config / signals / risk
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics.performance import analyze_journal
from config.settings import EASTERN, Settings, get_settings, momentum_weights, swing_weights
from config.etf_universe import ALL_ETFS
from config.universe import ALL_SYMBOLS, CA_WATCHLIST, US_WATCHLIST
import structlog
from fastapi.templating import Jinja2Templates
from signals.signal_types import Grade

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# App & templates
# ---------------------------------------------------------------------------
from dashboard.middleware import OPENAPI_TAGS  # noqa: E402
from dashboard.middleware import install as _install_middleware  # noqa: E402

app = FastAPI(
    title="US Trading Bot Dashboard",
    version="0.1.0",
    description=(
        "Control panel and JSON API for the US/CA equity trading bot: paper/live "
        "mode, manual trades, engine control, analytics, risk, backtests, and the "
        "AI analyst & memory layer. All endpoints except `/health` require HTTP "
        "Basic auth; money-moving actions additionally require the admin password."
    ),
    openapi_tags=OPENAPI_TAGS,
    # B1 — the interactive docs (Swagger UI / ReDoc) and the raw OpenAPI schema
    # leak the full API surface, so the built-in *unauthenticated* routes are
    # disabled here and re-served below behind the same HTTP Basic auth guard as
    # every other page.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Cross-cutting middleware + error handling (security headers, request logging
# with a correlation id, and a consistent global exception envelope).  Stack
# traces are only echoed to the client on an interactive TTY (local dev); in
# production the client gets a generic message and the detail is logged.
_install_middleware(app, debug=sys.stderr.isatty())

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# Self-hosted static assets (Chart.js for the monitoring charts — no CDN so
# the dashboard works on a locked-down server).
from fastapi.staticfiles import StaticFiles  # noqa: E402

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# Authentication — HTTP Basic Auth guarding every page except /health
# ---------------------------------------------------------------------------
# The implementation lives in dashboard.auth so the feature routers can share
# the exact same guard without importing this module (which would be circular).
from dashboard.auth import require_auth  # noqa: E402
from dashboard.http_util import parse_json_body  # noqa: E402
from dashboard.rate_limit import rate_limit  # noqa: E402
from dashboard.schemas import (  # noqa: E402
    BacktestRunRequest,
    EngineControlRequest,
    ModeSwitchRequest,
    ProviderKeysRequest,
    ProviderSelectRequest,
)


# ---------------------------------------------------------------------------
# B1 — auth-gated API documentation
# ---------------------------------------------------------------------------
# The default Swagger UI / ReDoc / openapi.json routes were disabled on the app
# above (docs_url=None, …); re-serve them here behind the same require_auth
# guard so the interactive docs and the machine-readable schema are only
# reachable with valid dashboard credentials.
from fastapi.openapi.docs import (  # noqa: E402
    get_redoc_html,
    get_swagger_ui_html,
)
from fastapi.openapi.utils import get_openapi  # noqa: E402


@app.get("/openapi.json", include_in_schema=False)
async def _openapi(_user: str = Depends(require_auth)) -> Dict[str, Any]:
    """The OpenAPI schema — auth-gated (was publicly reachable before B1)."""
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )


@app.get("/docs", include_in_schema=False)
async def _swagger_ui(_user: str = Depends(require_auth)) -> HTMLResponse:
    """Swagger UI — auth-gated.  Fetches /openapi.json with the same creds."""
    return get_swagger_ui_html(
        openapi_url="/openapi.json", title=f"{app.title} — API docs"
    )


@app.get("/redoc", include_in_schema=False)
async def _redoc(_user: str = Depends(require_auth)) -> HTMLResponse:
    """ReDoc — auth-gated."""
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} — API docs")


# ---------------------------------------------------------------------------
# Helpers — build data dicts consumed by the template
# ---------------------------------------------------------------------------

def _build_system_status() -> Dict[str, Any]:
    """Gather system status and configuration summary."""
    settings = get_settings()
    return {
        "bot_version": "0.1.0",
        "mode": "Paper Trading" if not settings.IS_LIVE_TRADING else "Live Trading",
        "trading_mode": settings.TRADING_MODE,
        "is_live": settings.IS_LIVE_TRADING,
        "broker": settings.BROKER,
        "paper_backend": settings.paper_broker_label,
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
        "etf_symbols": len(ALL_ETFS),
        "total_symbols": len(set(ALL_SYMBOLS) | set(ALL_ETFS)),
        "timestamp": datetime.now(tz=EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def _build_strategies() -> List[Dict[str, Any]]:
    """Return strategy descriptions and weight configurations."""
    settings = get_settings()
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
        # ── Highly Selective strategies ──────────────────────────────────
        {
            "name": "RSI-2 Reversal",
            "family": "selective",
            "description": (
                "Mean reversion at structural support/resistance when RSI(2) "
                "hits an extreme (<5 or >95) with volume confirmation. "
                "Requires trend filter + oscillator extreme + level "
                "confluence + volume — all four rarely align."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": swing_weights,
            "size_modifier": "75% (selective risk budget)",
        },
        {
            "name": "Triple-Timeframe Breakout",
            "family": "selective",
            "description": (
                "Breakout requiring agreement across daily trend, 4H "
                "consolidation structure, and 1H trigger bar — filters "
                "for breakouts backed by genuine institutional participation. "
                "Includes macro event blackout."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": momentum_weights,
            "size_modifier": "75% (selective risk budget)",
        },
        {
            "name": "BB Climax Reversal",
            "family": "selective",
            "description": (
                "Bollinger Band touch + volume/volatility climax (top 5% "
                "volume) + reversal candle pattern + multi-day acceleration. "
                "Designed to catch capitulation bottoms, not routine pullbacks."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": swing_weights,
            "size_modifier": "75% (selective risk budget)",
        },
        {
            "name": "Post-Earnings Drift",
            "family": "selective",
            "description": (
                "PEAD with volume-confirmed follow-through: large gap, "
                "holds above open, 2x+ volume, strong close. Only fires "
                "~4x/year per name (earnings season)."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": momentum_weights,
            "size_modifier": "75% (selective risk budget)",
        },
        {
            "name": "Gap-Fill Fade",
            "family": "selective",
            "description": (
                "Statistical gap fade on small-to-moderate gaps (0.3–1.0%) "
                "with no macro event and indecisive opening candle. "
                "Targets the prior close (gap fill)."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": swing_weights,
            "size_modifier": "75% (selective risk budget)",
        },
        {
            "name": "Turnaround Tuesday",
            "family": "selective",
            "description": (
                "Day-of-week seasonal: long at Monday's close when Monday "
                "drops ≥1% from Friday with low IBS. Published calendar "
                "anomaly — fires at most weekly."
            ),
            "max_positions": settings.MAX_SELECTIVE_POSITIONS,
            "weights": swing_weights,
            "size_modifier": "75% (selective risk budget)",
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
    settings = get_settings()
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
# Paper trading — account, positions, and recent trades
# ---------------------------------------------------------------------------


def _load_open_positions(data_dir: Path) -> List[Dict[str, Any]]:
    """Read the persisted open positions (paper or live) from JSON."""
    path = data_dir / "open_positions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.values()) if isinstance(data, dict) else []


def _paper_today_pnl(data_dir: Path) -> float:
    """Sum today's net P&L from completed journal trades."""
    from analytics.performance import load_completed_trades

    df = load_completed_trades(data_dir / "trades.csv")
    if df.empty or "exit_time" not in df.columns or "pnl_net" not in df.columns:
        return 0.0
    exits = pd.to_datetime(df["exit_time"], errors="coerce")
    mask = exits.dt.date == datetime.now(tz=EASTERN).date()
    return float(pd.to_numeric(df.loc[mask, "pnl_net"], errors="coerce").fillna(0).sum())


def _num(value: Any) -> Optional[float]:
    """Coerce a journal cell to float, or ``None`` when blank/unparseable."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _slim_trades(rows: List[Dict[str, Any]], limit: int = 15) -> List[Dict[str, Any]]:
    """Reduce full journal rows to the fields the history table needs."""
    slim = []
    for r in list(reversed(rows))[:limit]:  # most recent first
        slim.append(
            {
                "symbol": r.get("symbol"),
                "strategy": r.get("strategy"),
                "entry_price": _num(r.get("entry_fill_price")),
                "exit_price": _num(r.get("exit_price")),
                "exit_reason": r.get("exit_reason"),
                "pnl_net": _num(r.get("pnl_net")),
                "r_multiple": _num(r.get("r_multiple")),
                "exit_time": r.get("exit_time"),
            }
        )
    return slim


def _build_paper_trading() -> Dict[str, Any]:
    """Gather the paper-trading account view: balance, positions, history."""
    settings = get_settings()
    data_dir = Path(settings.DATA_DIR)

    report = analyze_journal(data_dir / "trades.csv", settings.TOTAL_CAPITAL)
    summary = report.summary

    positions_raw = _load_open_positions(data_dir)
    committed: Dict[str, float] = {}
    positions: List[Dict[str, Any]] = []
    for p in positions_raw:
        try:
            entry = float(p.get("entry_price", 0) or 0)
            qty = int(float(p.get("quantity", 0) or 0))
        except (ValueError, TypeError):
            continue
        currency = str(p.get("currency", "USD")).upper()
        cost = entry * qty
        committed[currency] = committed.get(currency, 0.0) + cost
        positions.append(
            {
                "symbol": p.get("symbol", ""),
                "strategy": p.get("strategy", ""),
                "grade": p.get("grade", ""),
                "quantity": qty,
                "entry_price": round(entry, 2),
                "stop_price": round(float(p.get("stop_price", 0) or 0), 2),
                "target_price": round(float(p.get("target_price", 0) or 0), 2),
                "currency": currency,
                "cost_basis": round(cost, 2),
                "risk_amount": round(float(p.get("risk_amount", 0) or 0), 2),
                "entry_time": str(p.get("entry_time", ""))[:19].replace("T", " "),
            }
        )
    positions.sort(key=lambda x: x["symbol"])

    balances = []
    for currency, allocated in settings.CAPITAL_BY_CURRENCY.items():
        used = committed.get(currency.upper(), 0.0)
        balances.append(
            {
                "currency": currency.upper(),
                "allocated": round(allocated, 2),
                "committed": round(used, 2),
                "available": round(allocated - used, 2),
            }
        )

    realized = float(summary.get("total_pnl", 0.0) or 0.0)
    return {
        "mode": settings.TRADING_MODE,
        "is_live": settings.IS_LIVE_TRADING,
        "broker": settings.BROKER,
        "paper_backend": settings.paper_broker_label,
        "starting_capital": round(settings.TOTAL_CAPITAL, 2),
        "realized_pnl": round(realized, 2),
        "today_pnl": round(_paper_today_pnl(data_dir), 2),
        "account_equity": round(settings.TOTAL_CAPITAL + realized, 2),
        "open_count": len(positions),
        "total_trades": summary.get("total_trades", 0),
        "win_rate": summary.get("win_rate", 0.0),
        "profit_factor": summary.get("profit_factor"),
        "balances": balances,
        "positions": positions,
        "recent_trades": _slim_trades(report.recent_trades),
    }


def _build_help() -> Dict[str, Any]:
    """Comprehensive how-to content for the dashboard help section."""
    settings = get_settings()
    return {
        "is_live": settings.IS_LIVE_TRADING,
        "broker": settings.BROKER,
        "getting_started": [
            "Paper trading is the DEFAULT — no brokerage account or API keys needed.",
            "Start the bot:  python engine.py  (it runs the built-in simulated broker).",
            "Open this dashboard:  uvicorn dashboard.app:app --port 8501",
            "The engine only trades during US market hours (09:30–16:00 ET, weekdays).",
            "Watch the Paper Trading section fill with positions and P&L as it runs.",
            "Every fill is simulated with realistic slippage & commissions — no real money moves.",
        ],
        "reading_dashboard": [
            "Account Equity = starting capital + realised P&L from closed trades.",
            "Today's P&L sums the net P&L of trades that closed today.",
            "Open Positions lists each live lot: entry, stop, target, cost basis, and grade.",
            "Balances show committed vs. available capital per currency (USD / CAD).",
            "The Risk Dashboard shows gross exposure, sector concentration, position "
            "correlation, drawdown, and daily/weekly/monthly P&L.",
            "Strategy Performance Comparison breaks results down per strategy so you can "
            "see which edges are actually working.",
        ],
        "switch_to_live": [
            "Use the ‘Switch to Live’ button at the top — it pops a confirmation dialog "
            "warning that REAL money is at risk.",
            "Going live requires the admin password (DASHBOARD_PASSWORD) — de-risking "
            "back to paper never needs one.",
            "On confirm, the bot writes BROKER=ibkr / IBKR_PORT=7496 to .env and restarts "
            "the engine automatically; the banner turns red and reads LIVE.",
            "Live trading needs a funded Interactive Brokers account with TWS/Gateway "
            "running. IBKR_PORT=7497 is IBKR's paper gateway — still paper.",
        ],
        "switch_to_paper": [
            "Click ‘Switch to Paper’ (no password needed) — it sets BROKER=paper and "
            "restarts. You can also just set BROKER=paper in .env and restart.",
        ],
        "market_data": [
            "Pick your data source in the Market Data Provider section — Yahoo Finance, "
            "Alpaca, or Polygon.io — no .env editing required.",
            "Yahoo Finance is the free default and needs no key.",
            "Alpaca adds realtime websocket streaming for lower-latency exits; enter your "
            "key + secret and the provider auto-switches to Alpaca.",
            "Polygon.io covers US equities via its REST API; paste your key and select it.",
            "Each provider card shows Connected or Needs API key. Switching restarts the engine.",
        ],
        "api_keys": [
            "Alpaca: sign up at https://app.alpaca.markets, then Home → API Keys → "
            "Generate. The market-data keys work for both paper and live.",
            "Polygon.io: sign up at https://polygon.io, then Dashboard → API Keys. A free "
            "tier is available (rate-limited).",
            "Keys are written only to your local .env and never leave your machine.",
        ],
        "backtester": [
            "Run a historical backtest:  python -m backtest  (see backtest/__main__.py "
            "for symbol/date/strategy flags).",
            "The backtester reuses the exact fill model (slippage + commission) and the "
            "same analytics as live paper trading, so results are comparable.",
            "Use it to validate a strategy over past data before committing paper capital.",
        ],
        "signals": [
            "Each cycle the screener scores every symbol across 5 strategies "
            "(VCP, PEAD, momentum, swing, mean-reversion) using 5 weighted indicators "
            "(RSI, MACD, EMA structure, volume, Ripster clouds).",
            "Scores map to grades: A ≥ 0.78 (full size), B ≥ 0.65 (75% size), C/F are skipped.",
            "Multi-timeframe: a daily signal is only taken when the weekly trend agrees.",
            "Signals then pass the 9-gate entry pipeline: (1) risk pre-check "
            "(held/cooldown/daily-loss/limits/stop/target/RR), (2) strategy capacity, "
            "(3) pending-order guard, (4) AI news veto, (5) position sizing, "
            "(6) freshness (age + price drift), (7) cash availability, (8) bracket "
            "placement, (9) journaling & registration.",
        ],
        "evaluation_tips": [
            "Let paper trading run for weeks, not days — you want dozens of closed trades "
            "before judging an edge.",
            "Check Profit Factor (> 1.5 is healthy) and Expectancy (positive) per strategy, "
            "not just total P&L.",
            "Watch max drawdown and position correlation — a book of correlated names is "
            "really one bet.",
            "Confirm the win rate and average R-multiple are stable across different market "
            "conditions before going live.",
            "Start live with reduced capital and the same settings you validated on paper.",
        ],
    }


# Anchor of the in-page documentation section every "?" help link points at.
HELP_SECTION_ANCHOR = "nav-help"


def _build_section_guides() -> List[Dict[str, Any]]:
    """Per-section documentation shown as subtitles and in the Help reference.

    Each entry is the single source of truth for a dashboard section's one-line
    subtitle (``summary``) *and* its detailed how-to card (rendered inside the
    Help section at ``#help-<key>``). ``key`` matches the section's ``nav-<key>``
    anchor so the header's "?" link can deep-link to the matching card.

    Fields:
      * ``key``        — matches the ``nav-<key>`` section id (and ``help-<key>`` card)
      * ``title``      — human label (mirrors the section's ``<h2>``)
      * ``summary``    — one line shown under the heading; keep it scannable
      * ``quickstart`` — optional one-liner for experienced users, rendered as a
                         callout at the top of the card
      * ``what``       — 2-3 sentences on what the section is for, why it matters
                         and when to use it
      * ``how``        — ordered how-to / what-each-control-means bullets
      * ``examples``   — optional worked "to do X, …" examples
      * ``mistakes``   — optional common-mistake / watch-out bullets
      * ``tips``       — optional best-practice bullets
      * ``doc``        — optional {"label", "url"} "Learn more" link
    """
    return [
        {
            "key": "provider",
            "title": "Market Data Provider",
            "summary": "Choose where live price data comes from and store each provider's API key.",
            "quickstart": "Stay on Yahoo Finance (free, no key) for paper trading. Want lower-latency "
                          "realtime exits? Paste your Alpaca key/secret under API Keys and Save — the "
                          "active provider switches to Alpaca automatically.",
            "what": "This section picks the single source that both the engine and the dashboard use for "
                    "every quote and every history request, and it lets you store each provider's API key "
                    "without hand-editing files. It matters because the data feed drives every signal, stop "
                    "and exit — a bad or throttled feed means missed or mispriced trades. Visit it when you "
                    "first set up, when a provider starts rate-limiting you, or when you want realtime "
                    "streaming instead of Yahoo's delayed snapshots.",
            "how": [
                "“Active provider” at the top shows which feed is live right now.",
                "“Select provider” dropdown lists every source; the label shows “— needs API key” for any "
                "provider you haven't configured and “(active)” next to the current one.",
                "Click Apply after choosing — the engine restarts on the new feed within a few seconds; a "
                "confirmation line appears just below the dropdown.",
                "Yahoo Finance is the free default and needs no key. Alpaca and Polygon.io each need an API key.",
                "The provider cards below show a green “Connected” pill or an amber “Needs API key” pill, plus "
                "a “Get a key:” link to the provider's signup page.",
                "Under API Keys, each provider has its own labelled fields (e.g. Alpaca key + secret). Fields "
                "already stored show a “saved” pill and a “•••••••• (leave blank to keep)” placeholder.",
                "Type the key(s) and click “Save <provider> keys”. Saving Alpaca keys also flips the active "
                "provider to Alpaca. Keys are written only to your local .env and never leave this machine.",
            ],
            "examples": [
                "To add Alpaca streaming: open API Keys → Alpaca, paste your API key and secret, click "
                "“Save Alpaca keys”. The card turns green and the active provider becomes Alpaca.",
                "To rotate a key without changing providers: leave the other fields blank (the "
                "“leave blank to keep” placeholder), fill only the one field, and Save.",
            ],
            "mistakes": [
                "Selecting a provider whose card still says “Needs API key” and clicking Apply — the feed "
                "will fail. Save the key first.",
                "Expecting Yahoo Finance to give tick-level realtime data — it returns delayed snapshots, "
                "fine for paper trading but not for latency-sensitive live exits.",
            ],
            "tips": [
                "Alpaca adds realtime websocket streaming for lower-latency exits.",
                "Use the “Get a key” link on each card to reach the provider's signup page.",
            ],
        },
        {
            "key": "account",
            "title": "Trading Account",
            "summary": "Your live equity, today's P&L, cash balances and every open position.",
            "quickstart": "The stat cards up top are your scoreboard; the Open Positions list below is your "
                          "live book. Use the ▦ Cards / ☰ Table toggle to switch views, and the Why? / TA "
                          "Chart / Stop buttons on each position to inspect or close it.",
            "what": "This is the at-a-glance state of the account the engine is trading — paper by default, "
                    "live once you switch broker from the header. It matters because it's the single place "
                    "that ties equity, realised and unrealised P&L, cash by currency and open risk together "
                    "in real time. Check it first thing each session and whenever a trade opens or closes.",
            "how": [
                "Stat cards read left to right: Starting Capital, Account Equity (starting + realised P&L), "
                "Realized P&L, Today's P&L, Unrealized P&L (open positions marked to live price), Today "
                "Total (open + closed), Open Positions, Closed Trades, Win Rate and Profit Factor.",
                "Today's P&L and the Unrealized/Today-Total cards update in real time over a websocket; the "
                "small timestamp in the header shows the last live update, and a mini intraday P&L chart "
                "plots the day's path.",
                "“Cash Balances” breaks down Allocated / Committed / Available capital per currency (USD and CAD).",
                "Open Positions shows each live lot. Cards view gives a stop ◄─●─► target progress bar; Table "
                "view adds columns for Qty, Entry, Stop, Target, Cost Basis and Opened time.",
                "Use the ▦ Cards / ☰ Table toggle (top-right of the positions block) to switch layouts — your "
                "choice is remembered in a cookie across reloads.",
                "Each position has three buttons: “Why?” opens the AI rationale for the entry, “TA Chart” "
                "shows the annotated technical chart, and the red “Stop” closes the position at market now "
                "(admin password required).",
                "“Recent Trade History” at the bottom lists the latest closed trades with entry, exit, exit "
                "reason, net P&L and R-multiple.",
            ],
            "examples": [
                "To review why a position was opened: find it in Open Positions and click “Why?” — the AI's "
                "entry rationale (grade, indicators, setup) pops up.",
                "To flatten a runaway position immediately: click the red “Stop” on its row, enter the admin "
                "password, and it closes at market and moves to Recent Trade History.",
            ],
            "mistakes": [
                "Confusing Realized P&L (closed trades only) with Unrealized P&L (open, marked-to-market) — "
                "Today Total combines both.",
                "Clicking the red “Stop” expecting it to just cancel the order — it closes the whole "
                "position at market. Use it only when you really mean to exit now.",
            ],
            "tips": [
                "A red banner and “LIVE” label mean real money is at risk; paper mode is labelled and safe.",
            ],
        },
        {
            "key": "strategy",
            "title": "Strategy Performance Comparison",
            "summary": "Win rate, profit factor and expectancy broken down per strategy.",
            "quickstart": "Scan the table for the strategy with the highest Profit Factor and positive "
                          "Expectancy over a decent Trades count — that's your working edge. The bar chart "
                          "below ranks strategies by total P&L at a glance.",
            "what": "This section shows which of the bot's edges are actually working by putting every "
                    "strategy's closed-trade statistics side by side. It matters because total account P&L "
                    "hides the fact that one strong strategy can be subsidising several losing ones. Use it "
                    "when deciding which strategies to keep enabled in Engine Trade Selection.",
            "how": [
                "Each row is one strategy (VCP, PEAD, momentum, swing, mean-reversion, sector_rotation, …).",
                "Trades is the closed-trade sample size — treat rows with only a handful of trades as noise.",
                "Win Rate is the share of trades that closed green; Avg Win and Avg Loss are the mean "
                "dollar outcome of each.",
                "Profit Factor = gross wins ÷ gross losses; above 1.5 is healthy, below 1.0 loses money.",
                "Expectancy is the average R-multiple per trade; positive means a real, repeatable edge.",
                "Total P&L (last column) and the coloured bar chart underneath rank strategies by absolute "
                "dollars earned or lost.",
            ],
            "examples": [
                "To decide what to trade next month: sort your eye down the Profit Factor column, keep "
                "strategies above ~1.5 with 20+ trades, and consider disabling anything below 1.0.",
                "A strategy showing 40% Win Rate but Profit Factor 2.0 is a winner — its big wins outweigh "
                "its frequent small losses; don't disable it for the low win rate alone.",
            ],
            "mistakes": [
                "Judging a strategy on 3-5 trades — small samples swing wildly and mean nothing.",
                "Chasing win rate instead of expectancy: a 70%-win strategy with tiny wins and huge losses "
                "still bleeds money.",
            ],
            "tips": [
                "Judge a strategy on dozens of closed trades, not a handful — small samples mislead.",
            ],
        },
        {
            "key": "backtest",
            "title": "Backtesting",
            "summary": "Replay a strategy over historical data before risking any capital.",
            "quickstart": "Pick symbols (list or free-text), set a date range and strategies, choose a "
                          "minimum grade and starting capital, then click Run Backtest. Read the equity "
                          "curve, trades table and log; tick winning rows to push them into Engine Trade "
                          "Selection.",
            "what": "Backtesting replays the chosen strategies over historical daily bars using the exact "
                    "same fill model (slippage + commission) the live and paper engine uses, so the results "
                    "are directly comparable. It matters because it's how you validate an idea before "
                    "risking capital or committing the engine to it. Run it whenever you change a strategy, "
                    "add symbols, or want evidence before pinning a trade selection.",
            "how": [
                "“Symbols” is a multi-select list (Ctrl/⌘-click for several) capped at the shown maximum; "
                "“Or type symbols” takes a comma/space-separated list (e.g. AAPL, MSFT, NVDA) merged with it.",
                "“Start date” and “End date” bound the replay window; the defaults cover a recent span.",
                "“Strategies” is a set of checkboxes — tick one or more. PEAD fetches live earnings data so "
                "it runs noticeably slower than the others.",
                "“Minimum grade” (A / B / C and better) filters which setups are allowed to trade, mirroring "
                "the engine's grade gate.",
                "“Starting capital ($)” sets the simulated account (minimum 1, steps of 100); use a realistic "
                "figure so position sizing matches how you'd really trade.",
                "Click Run Backtest — it runs in the background so the dashboard stays responsive; a status "
                "line shows progress.",
                "Results show metric cards, an equity curve, a per-trade table (entry/exit dates and prices, "
                "stop, target, reason, qty, net P&L, R) and a chronological Backtest log.",
                "The log toolbar filters events: All / Signals / Entries / Exits / Rejections — use "
                "Rejections to see why candidate signals were skipped.",
                "Each results row can be ticked to add that symbol to Engine Trade Selection (see the "
                "Backtest performance table there).",
                "“Walk-Forward” (further down this section) repeatedly optimizes on an in-sample window and "
                "validates on the following out-of-sample window, rolling forward by a step — a large gap "
                "between in- and out-of-sample results is the classic overfitting tell.",
            ],
            "examples": [
                "To test a momentum play on big tech: type “AAPL, MSFT, NVDA, AMZN, META”, set a 1-2 year "
                "range, tick the momentum strategy, leave grade at B and better, Run, then read Profit "
                "Factor and max drawdown from the metric cards.",
                "To sanity-check a single name: leave the list empty, type just “NVDA”, tick every strategy, "
                "and see which strategy produced the best R on it.",
                "To check a strategy is not overfit: set in-sample and out-of-sample month lengths under "
                "Walk-Forward, Run it, and confirm the out-of-sample equity curve tracks the in-sample one.",
            ],
            "mistakes": [
                "Testing only a bull-market window — a strategy that only works in an uptrend is fragile. "
                "Include a choppy or down period.",
                "Reading a backtest with very few resulting trades as proof of an edge; widen the symbols or "
                "date range for a meaningful sample.",
                "Setting an unrealistic Starting capital, which distorts position sizes and makes the P&L "
                "figures meaningless for your real account.",
            ],
            "tips": [
                "Test across different market conditions — a strategy that only works in a bull run is fragile.",
            ],
        },
        {
            "key": "selection",
            "title": "Engine Trade Selection",
            "summary": "Pin the engine to specific symbols, strategies and a minimum setup grade.",
            "quickstart": "Flip Status on, set a minimum grade, optionally tick strategies and add symbols "
                          "(empty = no restriction), enter the admin password and Save selection. Leave "
                          "Status off to let the engine scan everything automatically.",
            "what": "This section constrains what the automated engine is allowed to trade — pinning it to "
                    "the symbols, strategies and minimum setup grade you choose, typically right after a "
                    "backtest proves what works. It matters because it turns a validated backtest into the "
                    "engine's live mandate instead of letting it scan the whole watchlist. Use it when you "
                    "want the bot to focus, and turn it off to return to fully automatic scanning.",
            "how": [
                "“Status” is a toggle. On = the selection is active and the engine only trades what you "
                "picked; off = “Selection inactive — engine trades everything” (fully automatic).",
                "“Minimum grade” dropdown: A only (strongest setups), B or better (default), or C or better "
                "(permissive) — it's the floor a setup must clear to be traded.",
                "“Strategies” is a checkbox list — none checked means all strategies are allowed; tick some "
                "to restrict to only those.",
                "“Symbols”: type a ticker in “Add symbol” and click Add to build a chip list — an empty list "
                "means every watchlist symbol is allowed.",
                "“Backtest performance” table shows per-symbol results from your most recent backtest run; "
                "tick a row's checkbox to add that symbol straight into the selection above.",
                "Enter the admin password (required) and click Save selection. Changes take effect on the "
                "engine's next scan cycle.",
            ],
            "examples": [
                "To trade only your three best backtested names with the VCP strategy at grade A: turn "
                "Status on, set Minimum grade to “A only”, tick VCP, add the three symbols, Save.",
                "To temporarily stop the bot from opening anything new: leave Status on with a symbol list "
                "and set the grade to “A only” so almost nothing qualifies — or just Stop the engine.",
            ],
            "mistakes": [
                "Setting Status on but leaving both Strategies and Symbols empty, then wondering why nothing "
                "narrowed — empty means “no restriction”, so it behaves like automatic scanning at your "
                "chosen grade.",
                "Forgetting the admin password field — the save silently fails without it.",
                "Assuming changes are instant; they apply on the next scan cycle, not the moment you Save.",
            ],
            "tips": [
                "Grades map from score: A ≥ 0.78 (full size), B ≥ 0.65 (75% size); C/F are normally skipped.",
            ],
        },
        {
            "key": "history",
            "title": "Trade History & Analytics",
            "summary": "Every closed trade with filters and aggregate performance analytics.",
            "quickstart": "Use the filter row (Strategy, Symbol, Exit reason, From/To dates) then Apply to "
                          "narrow the table; click column headers to sort; page with ◀ Prev / Next ▶; and "
                          "read the stat cards for aggregate hold times and streaks.",
            "what": "This is the full, filterable record of every trade the engine has closed, plus the "
                    "analytics computed from that record. It matters because reviewing real closed trades — "
                    "not just the equity number — is how you find what's working and what to stop doing. Use "
                    "it for periodic reviews and to answer questions like “how did my gap-fill trades do?”",
            "how": [
                "Stat cards up top: Avg Hold, Median Hold, Best Trade, Worst Trade and Current Streak "
                "(consecutive wins or losses).",
                "Filter row: Strategy dropdown, Symbol text box, Exit reason dropdown, and From / To date "
                "pickers. Click Apply to run the filter; the table and (where shown) stats update to the "
                "filtered set.",
                "“Export CSV” downloads exactly the current view for spreadsheet analysis.",
                "The table columns (Symbol, Strategy, Grade, Entry, Exit, Hold, P&L $, P&L %, R, Exit "
                "reason, Closed) — several headers are clickable to sort; the arrow shows the sort column.",
                "Use ◀ Prev / Next ▶ to page through long histories; the page indicator sits between them.",
                "“Rolling Win Rate” chart at the bottom has a window dropdown (10 / 20 / 50 trades) — pick "
                "the smoothing window to see how your hit rate is trending.",
            ],
            "examples": [
                "To review every mean-reversion trade on NVDA last quarter: set Strategy = mean-reversion, "
                "Symbol = NVDA, From/To to the quarter, Apply, then sort by R to find the outliers.",
                "To check if you're on a losing streak: read the Current Streak card, then set the Rolling "
                "Win Rate window to 10 trades to see the recent trend.",
            ],
            "mistakes": [
                "Changing a filter but forgetting to click Apply — the table won't update until you do.",
                "Reading the Rolling Win Rate on a tiny window (10) as a verdict; it's meant to show the "
                "trend, not a stable statistic.",
            ],
            "tips": [
                "Export the same data as CSV or PDF from the API, Export & Accounts section.",
            ],
        },
        {
            "key": "charts",
            "title": "Performance Charts",
            "summary": "Equity curve, drawdown and P&L visualised over time.",
            "quickstart": "Four charts render automatically from your closed trades: Equity Curve, Periodic "
                          "Returns (switch Daily/Weekly/Monthly), Drawdown Over Time and Rolling Win Rate. "
                          "Nothing to configure — just read them.",
            "what": "This section turns the trade history into charts so trends, drawdowns and consistency "
                    "are obvious at a glance instead of buried in a table. It matters because the shape of "
                    "the equity curve — smooth vs. jagged — tells you more about an edge's durability than "
                    "the final number. Use it after a batch of trades closes to gauge how healthy the run is.",
            "how": [
                "Equity Curve tracks account value trade by trade — you want a steady rising line, not a "
                "spike-and-crash.",
                "Periodic Returns has a frequency dropdown (Daily / Weekly / Monthly) — switch it to see "
                "whether green periods outnumber red at each timescale.",
                "Drawdown Over Time shows how far equity has fallen from its running peak; the depth and "
                "duration of the troughs are your key risk view.",
                "Rolling Win Rate (20 trades) plots your recent hit rate so you can see it drifting up or down.",
                "If there are no closed trades yet, the section shows an empty note — charts populate after "
                "the first completed trade.",
            ],
            "examples": [
                "To judge a strategy's risk: look at Drawdown Over Time — two shallow 5% dips are far "
                "healthier than one 30% cliff, even at the same final equity.",
                "To spot seasonality: switch Periodic Returns to Monthly and see if certain months are "
                "consistently red.",
            ],
            "mistakes": [
                "Fixating on the final equity value while ignoring a deep drawdown that would have been hard "
                "to sit through live.",
                "Expecting charts before any trade has closed — they only appear once there's realised data.",
            ],
            "tips": [
                "A smooth rising curve with shallow drawdowns matters more than a high final number.",
            ],
        },
        {
            "key": "activity",
            "title": "Engine Activity",
            "summary": "A cycle-by-cycle log of scans, gate rejections, trades and exits.",
            "quickstart": "Read the Recent Cycles table to see scans, rejections and trades per cycle, then "
                          "use the Event Feed filter chips (All / Trades / Rejections / Exits / Errors) and "
                          "the Symbol box to find exactly why a name did or didn't trade.",
            "what": "This section is a cycle-by-cycle trace of every decision the engine made — scans run, "
                    "each gate rejection with its reason (including AI-veto reasoning), trades placed and "
                    "exits taken. It matters because it's the single best answer to “why didn't the bot "
                    "trade today?” Check it whenever the engine's behaviour surprises you, before you touch "
                    "any settings.",
            "how": [
                "“Recent Cycles” table: one row per scan with Signals (candidates found), Rejected by gate, "
                "Placed (trades opened), Exits and Elapsed (cycle duration).",
                "The Event Feed below is the detailed stream. Filter chips restrict it: All, Trades, "
                "Rejections, Exits, Errors.",
                "The “Symbol” box filters the feed to one ticker as you type — handy for tracing a single "
                "name across cycles.",
                "Each rejection entry names the gate that blocked the signal and the reason (e.g. grade too "
                "low, RSI overbought, AI veto, risk cap reached).",
                "A “new” badge flags fresh events since you last looked.",
            ],
            "examples": [
                "The bot didn't buy NVDA even though you expected it to: type NVDA in the Symbol box, click "
                "the Rejections chip, and read the exact gate that blocked it.",
                "Nothing traded all morning: check Recent Cycles — if Signals is 0, no setups qualified; if "
                "Signals is high but Placed is 0, read the Rejections to see which gate is too tight.",
            ],
            "mistakes": [
                "Loosening strategy or risk settings before reading the rejection reasons here — you may be "
                "fixing the wrong thing.",
                "Assuming an empty feed means a bug; outside market hours (09:30-16:00 ET, weekdays) the "
                "engine doesn't run cycles.",
            ],
            "tips": [
                "If nothing is trading, read the rejection reasons here before changing settings.",
            ],
        },
        {
            "key": "alerts",
            "title": "Alerts & Notifications",
            "summary": "Configure Telegram, email and push channels and test they work.",
            "quickstart": "Click “Send test” on each channel card (Telegram / Email / Push) to confirm "
                          "delivery, then set per-event rules in the Rules table (which events fire, on "
                          "which channels, at what threshold) and click Save rules.",
            "what": "This section controls where the bot sends trade, exit and risk notifications and lets "
                    "you fire a test message per channel. It matters because a live bot you're not watching "
                    "is only as safe as its alerting — you want to hear about fills, stops and risk breaches "
                    "immediately. Set it up once, then revisit whenever you change credentials.",
            "how": [
                "“Channels” shows three cards — Telegram, Email and Push (PWA) — each with a status line and "
                "a “Send test” button that fires a real message so you can confirm it arrives.",
                "Channel credentials live in .env / Settings; an unconfigured channel is marked as such and "
                "its test will fail until you add them.",
                "“Rules” table: one row per event, with Enabled plus Telegram / Email / Push checkboxes to "
                "pick channels, and a Threshold field where relevant.",
                "Thresholds are numbers: drawdown and daily-loss are fractions (e.g. 0.05 = 5%), proximity "
                "is a percentage. They decide when a threshold-based alert fires.",
                "Click Save rules to apply — changes take effect on the engine's next dispatch, no restart "
                "needed.",
                "“Alert History” at the bottom lists what was actually sent; the Type dropdown filters it.",
            ],
            "examples": [
                "To get a Telegram ping only on real trouble: in the Rules table enable the drawdown and "
                "daily-loss rows, tick only Telegram, set the drawdown threshold to 0.05, Save.",
                "After adding email SMTP credentials: click “Send test” on the Email card — a green result "
                "confirms the credentials work before you rely on them.",
            ],
            "mistakes": [
                "Enabling an event but leaving every channel checkbox unticked — the rule is on but has "
                "nowhere to send.",
                "Entering a drawdown threshold as 5 instead of 0.05 — thresholds are fractions, so 5 means "
                "500% and never fires.",
                "Trusting alerts you never tested; always Send test after changing credentials.",
            ],
            "tips": [
                "Send a test after changing credentials so you know alerts will arrive during market hours.",
            ],
        },
        {
            "key": "memory",
            "title": "AI Memory & Learning",
            "summary": "The plain-English lessons the bot writes for itself after each trade.",
            "quickstart": "Read-only. The stat cards and binding chips summarise what the bot has learned; "
                          "the Learnings table, Reflections timeline and Guard Decisions log show the actual "
                          "lessons and the entries they blocked or demoted.",
            "what": "This is a read-only window into the reflection engine's memory: after every trade "
                    "closes it writes one plain-English lesson, and before new entries the similar-setup and "
                    "learnings guards consult those lessons. It matters because it's how the bot stops "
                    "repeating losing setups — and it lets you audit that reasoning. Check it to understand "
                    "why a plausible signal was blocked or down-sized.",
            "how": [
                "Stat cards: Total Lessons, Active Bindings (lessons currently strong enough to act), Guard "
                "Decisions (times a guard intervened) and Guard Hit Rate.",
                "The binding chips show how many lessons sit at each level: avoid (rejects the trade), "
                "require_confirm (restricts to grade A), prefer (annotates), observe (non-binding note).",
                "“Learnings” table lists every lesson with its Binding level, Confidence and Support "
                "(number of similar trades backing it). A lesson only binds once enough similar trades "
                "support it and confidence clears the threshold.",
                "“Trade Reflections Timeline” shows each closing trade and the lesson it produced, newest "
                "first, with the outcome and pattern tags.",
                "“Guard Decisions” log shows recent entry signals a guard acted on — a “block” skipped the "
                "trade, a “demote” would have required grade A — with the signal's grade, RSI and volume "
                "and the reason. Only rejections are logged; accepted signals pass silently.",
            ],
            "examples": [
                "A grade-B signal on a name you expected to trade got skipped: find it in Guard Decisions, "
                "read the reason, and trace back to the underlying lesson in the Learnings table.",
                "To gauge how much the memory is steering the bot: compare Guard Decisions to total trades "
                "and watch the binding-chip counts grow over weeks.",
            ],
            "mistakes": [
                "Expecting rich memory on day one — lessons only accumulate as trades close, so an empty "
                "table early on is normal, not a fault.",
                "Trying to edit lessons here — the section is read-only by design; the engine manages it.",
            ],
            "tips": [
                "Memory grows more useful over time — the longer the bot runs, the more it has learned.",
            ],
        },
        {
            "key": "risk",
            "title": "Risk Dashboard",
            "summary": "Gross exposure, sector concentration, correlation, drawdown and rolling P&L.",
            "quickstart": "Read the four top cards (Gross Exposure, Current & Max Drawdown, Top "
                          "Correlation) and the Daily Loss Budget bar first. Green bar = room to trade; "
                          "amber/red = you're near or over today's loss limit. Everything else is detail.",
            "what": "This is the portfolio-level risk picture — how much capital is at stake, how much you "
                    "could lose if every stop hit, and how concentrated or correlated the open book is. It "
                    "matters because position count hides real risk: five correlated tech names are one big "
                    "bet, not five small ones. Check it before adding exposure and whenever drawdown deepens.",
            "how": [
                "Top cards: Gross Exposure (total capital committed), Current Drawdown, Max Drawdown and "
                "Top Correlation (the most-correlated open pair).",
                "“Open Risk” shows $ At Risk to Stops (what you'd lose if every open stop hit), % of "
                "Capital at Risk, and Daily Loss Budget Used with a coloured progress bar (green → amber → "
                "red as you approach the limit). The pill toggles between “cost basis” and “marked to "
                "market” depending on whether live prices are available.",
                "Sector Concentration and the Sector/Industry Heat Map flag too much weight in one sector.",
                "Position Correlations and the Correlation Matrix (green = positively correlated, red = "
                "inversely) warn when several names really move together.",
                "P&L Breakdown gives Today / This Week / This Month; Exposure by Currency shows committed vs "
                "allocated per USD/CAD.",
            ],
            "examples": [
                "Before opening a sixth tech position: check the Correlation Matrix — if the new name is "
                "0.8+ correlated with names you already hold, you're doubling one bet, not diversifying.",
                "To know when to stop for the day: watch the Daily Loss Budget bar — when it turns red "
                "you've used your loss budget and the engine will stop opening new risk.",
            ],
            "mistakes": [
                "Judging risk by number of positions instead of correlation and sector concentration.",
                "Reading “$ At Risk to Stops” as a guaranteed loss — it's the worst case if every stop "
                "fills exactly, before any gap risk.",
            ],
            "tips": [
                "A book of highly correlated positions carries far more risk than the position count suggests.",
            ],
        },
        {
            "key": "engine",
            "title": "Engine Control",
            "summary": "Start, stop and monitor the automated trading engine.",
            "quickstart": "Enter the admin password, then use Start / Stop / Restart. Watch the state badge "
                          "and the phase/last-scan/next-scan cards for health, and load the engine logs "
                          "below (with optional Auto-tail) to see what it's doing.",
            "what": "This is the on/off switch and live status for the background engine that scans, scores "
                    "and places trades. It matters because it's how you take manual control — pausing the "
                    "bot before news, restarting it after a config change, or confirming it's actually "
                    "running. Use it whenever you need to change or verify the engine's run state.",
            "how": [
                "The state badge (with coloured dot) shows running / stopped / stale; a “stale” hint appears "
                "if it hasn't checked in recently.",
                "Status cards: Current phase, Market (open/closed), Last scan, Next scan, Open positions and "
                "Process (PID).",
                "Enter the admin password (DASHBOARD_PASSWORD) in the field — it's required for Start, Stop "
                "and Restart.",
                "Buttons: Start (launch the engine), Stop (halt scanning), Restart (stop then start, e.g. "
                "after a settings change), and Refresh status (re-poll without changing anything — no "
                "password needed).",
                "It only trades during US market hours (09:30-16:00 ET, weekdays); outside those hours it "
                "idles even when running.",
                "“Engine logs” loads recent output — pick 100 / 200 / 500 lines, click Refresh, or tick "
                "Auto-tail to keep it live-updating.",
            ],
            "examples": [
                "After changing risk settings: enter the password and click Restart so the engine picks up "
                "the new config on a clean cycle.",
                "To confirm the bot is alive without changing anything: click Refresh status and read the "
                "state badge and Last scan time.",
            ],
            "mistakes": [
                "Clicking Start/Stop without the admin password — the action is rejected.",
                "Assuming a stopped engine also closed your positions — Stop only halts scanning; open "
                "positions stay live and must be managed from the Trading Account section.",
                "Panicking that nothing trades after hours — the engine only acts during market hours.",
            ],
            "tips": [
                "Stopping the engine leaves open positions untouched — manage those from the account section.",
            ],
        },
        {
            "key": "status",
            "title": "System Status & Configuration",
            "summary": "Broker mode, versions, configuration and the current watchlists.",
            "quickstart": "This is a read-out of the running configuration — broker mode, IBKR connection, "
                          "capital split, scan interval, universe size and the live watchlists. To actually "
                          "switch between paper and live, use the “Switch to Live / Switch to Paper” button "
                          "in the page header.",
            "what": "This section is the system's overall configuration and health at a glance: what broker "
                    "and mode are active, how capital is split across USD/CAD, the scan interval and market "
                    "hours, the universe size, and the current US / Canadian / ETF watchlists. It matters "
                    "because it's the fastest way to confirm the bot is wired up the way you think. Check it "
                    "after any deploy or config change.",
            "how": [
                "The key/value grid lists Bot Version, Trading Mode (paper shows “(simulated)”), Broker, "
                "IBKR Connection (host:port) and Client ID, Total / USD / CAD capital, Scan Interval, "
                "Market Hours, Universe Size (US / CA / ETF breakdown) and Log Level.",
                "Below the grid, the US, Canadian and ETF watchlists are shown as pill lists so you can see "
                "exactly what the engine scans.",
                "This section is read-only. The paper↔live switch is the header button: “Switch to Live” "
                "pops a confirmation warning that REAL money is at risk and requires the admin password; "
                "“Switch to Paper” sets BROKER=paper and restarts the engine.",
                "Live trading needs a funded Interactive Brokers account with TWS or IB Gateway running and "
                "reachable at the IBKR host:port shown here.",
            ],
            "examples": [
                "After a deploy, confirm you're still in paper: check Trading Mode shows “Paper "
                "(simulated)” and the header badge is the paper colour before doing anything else.",
                "To verify the live gateway is targeted correctly: read IBKR Connection — port 7497 is the "
                "paper gateway (still simulated), 7496 is live.",
            ],
            "mistakes": [
                "Hunting for a live/paper switch inside this section — it isn't here; use the header button.",
                "Seeing IBKR_PORT 7497 and assuming it means live — that's the paper gateway; only 7496 is "
                "real-money.",
            ],
            "tips": [
                "IBKR_PORT=7497 is IBKR's paper gateway — still paper. 7496 is live.",
            ],
        },
        {
            "key": "watchlist",
            "title": "Watchlist Management",
            "summary": "Curate the named symbol lists the engine scans.",
            "quickstart": "Type a ticker, pick a list in “To list”, click Add. Create new lists with “New "
                          "list name” → Create list. The Watchlist Monitor below shows live price, day "
                          "change and each symbol's status in the latest scan.",
            "what": "This section organises symbols into named lists — the pool the scanner and engine "
                    "consider each cycle when no explicit trade selection is active. It matters because a "
                    "focused, liquid watchlist produces cleaner signals than a sprawling one, and disabling "
                    "a list parks it without deleting it. Use it to keep the engine pointed at names you "
                    "actually want it trading.",
            "how": [
                "“Add symbol”: type a ticker (e.g. NVDA), choose the target list in the “To list” dropdown, "
                "and click Add.",
                "“New list name” + Create list makes a fresh named list (e.g. “AI Leaders”) you can then "
                "add symbols to.",
                "The lists below let you remove symbols and enable/disable each list — the engine scans "
                "every enabled list; a disabled list is kept but skipped.",
                "These symbols feed the Pre-Market Scanner, the Earnings Calendar and automatic scanning; "
                "changes apply on the engine's next cycle.",
                "“Watchlist Monitor” shows, per symbol: which Lists it's in, live Price, Day %, and its "
                "Status in the latest scan (signal / near entry / held / rejected / excluded) with a Detail "
                "column — a preview of what the bot might do next.",
            ],
            "examples": [
                "To build a themed list: type “AI Leaders” into New list name → Create list, then add NVDA, "
                "AMD, AVGO, SMCI one at a time via Add symbol → To list = AI Leaders.",
                "To pause a list without losing it: disable it in the lists panel — the engine stops "
                "scanning it but the symbols stay saved.",
            ],
            "mistakes": [
                "Piling hundreds of illiquid names in — it dilutes scan quality and slows cycles. Keep it "
                "tight and liquid.",
                "Adding a symbol but leaving the wrong list selected in “To list”, so it lands in a list the "
                "engine isn't scanning.",
            ],
            "tips": [
                "Keep the watchlist focused — a smaller, liquid list produces cleaner signals than a huge one.",
            ],
        },
        {
            "key": "universe",
            "title": "Universe Browser",
            "summary": "Browse the full tradable-symbol database and promote names to the watchlist.",
            "quickstart": "If it says “not initialized”, click Seed Now once. Then search/filter the table, "
                          "tick symbols and use the bulk bar to add them to a watchlist. The Tiered Scanning "
                          "panel and sub-tabs (Index Membership / Scan Pool / Auto-Promotions) show how the "
                          "engine prioritises names.",
            "what": "This is a searchable catalogue of every stock and ETF the bot knows about, built from a "
                    "seedable universe database, plus the tiered-scanning model that decides how often each "
                    "name is evaluated. It matters because it's how you discover new candidates beyond your "
                    "watchlist and understand why some names are scanned every cycle and others only weekly. "
                    "Use it to find and promote names, or to inspect index membership.",
            "how": [
                "If the universe is empty, an “Universe not initialized” panel appears — click Seed Now to "
                "download the full symbol list (this takes a while the first time; a progress bar tracks it).",
                "Once seeded, a stats bar shows totals by exchange (NYSE / NASDAQ / AMEX / TSX) and last-"
                "updated time.",
                "Search by ticker or company name; the Filters row narrows by Exchange, Sector, Asset Type "
                "(Stocks / ETFs), and minimum Price / Volume / Market Cap.",
                "Tick rows (or “Select All on Page”) to reveal the bulk bar, choose a watchlist in its "
                "dropdown, and click Add to promote them all at once. Table headers sort; pager moves "
                "between pages.",
                "“Tiered Scanning” explains the index-based model: Tier 1 Active Trading (scanned every "
                "cycle), Tier 2 Scan Pool (top S&P 500 by volume×market-cap, scanned daily), Tier 3 "
                "Universe (full S&P 500 ∪ NASDAQ-100, swept weekly).",
                "Sub-tabs: Index Membership (filter S&P 500 / NASDAQ-100 / Both), Scan Pool (the ranked "
                "Tier-2 list with a Liquidity Score), and Auto-Promotions (symbols bumped to Tier 1 after "
                "firing a signal, until their TTL lapses).",
                "The Quick-Add Templates seed a watchlist from a preset (S&P 500 Large Cap, Tech Large Cap, "
                "Canadian Energy, All ETFs, or a chosen Sector). “Rebuild Universe Database” re-fetches "
                "everything from source.",
            ],
            "examples": [
                "To find liquid healthcare stocks over $20: set Sector = Healthcare, Asset Type = Stocks, "
                "Min Price = 20, sort by Avg Volume, tick the top names and bulk-add them to a watchlist.",
                "To seed a starter watchlist fast: click the “Tech Large Cap” quick-add template instead of "
                "adding names one by one.",
            ],
            "mistakes": [
                "Clicking “Rebuild Universe Database” casually — it's a heavy full re-fetch; only do it when "
                "the data is genuinely stale.",
                "Filtering so tightly (high min market cap + volume + price) that the table comes back empty "
                "and assuming the universe is broken.",
            ],
            "tips": [
                "Seeding can take a while the first time — it downloads the full symbol list.",
            ],
        },
        {
            "key": "trade",
            "title": "Manual Trade Entry",
            "summary": "Place a discretionary order outside the automated engine.",
            "quickstart": "Enter Symbol, Side, Quantity and Entry price; add one or more stop-loss and "
                          "profit-target levels (absolute price OR Δ% from entry, plus % of position); "
                          "enter the admin password and click Place trade.",
            "what": "This section lets you enter a trade by hand — buy (long) or sell (short) any symbol, "
                    "with laddered stop-loss and profit-target levels for partial exits. It matters as an "
                    "override: for testing, or to act on a setup the engine didn't take, while still using "
                    "the same fill model, journalling and risk accounting as automated trades. Use it "
                    "sparingly and deliberately — it's real (paper or live) exposure.",
            "how": [
                "Top row: Symbol (e.g. AAPL), Side (Buy = long, Sell = short), Quantity (shares, min 1), "
                "and Entry price.",
                "“Stop-loss levels” and “Profit-target levels” are ladders. Each level takes a Price OR a "
                "Δ% from entry (fill either, not both) and a “% of position” that slice exits.",
                "Click “+ Add stop level” / “+ Add target level” for partial exits (e.g. stop 1 at −3% for "
                "33%, stop 2 at −5% for 33%, stop 3 at −8% for the rest). The last level in each ladder "
                "takes whatever remains.",
                "Enter the admin password (required) and click Place trade.",
                "Multi-level ladders and short orders run on the paper broker; a simple long (one stop + one "
                "target) also works on IBKR.",
                "Once filled, the position appears in the Trading Account and Risk views like any other and "
                "is journalled for analytics.",
            ],
            "examples": [
                "Scale out of a long: Buy 300 AAPL at 190, target 1 = +5% for 33%, target 2 = +10% for "
                "33%, target 3 = (leave last) for the rest; stop = −4% for 100%.",
                "A quick short: Side = Sell, 100 shares, one stop at +5% (Δ%) for 100% and one target at "
                "−8% for 100%.",
            ],
            "mistakes": [
                "Filling both Price and Δ% on the same level — give one or the other.",
                "Letting the ladder's slice percentages exceed 100%, or forgetting the last level should "
                "cover the remainder.",
                "Forgetting a manual trade still counts toward risk limits and analytics — size it like a "
                "real position.",
            ],
            "tips": [
                "Manual trades still count toward risk limits and analytics — size them accordingly.",
            ],
        },
        {
            "key": "scanner",
            "title": "Pre-Market Scanner",
            "summary": "Scan the watchlist for opening gaps and unusual volume.",
            "quickstart": "Click “Run scan”. The table ranks watchlist symbols by opening Gap and Volume "
                          "multiple, and flags which setups each mover matches in the Signals column.",
            "what": "This is a quick pre-open scan that surfaces the watchlist symbols moving the most and "
                    "trading on abnormal volume. It matters because gaps and volume spikes are where the "
                    "day's opportunities and risks concentrate — it's your morning shortlist. Run it shortly "
                    "before the open, when pre-market prints are most informative.",
            "how": [
                "Click “Run scan” to score every watchlist symbol against the previous close.",
                "Columns: Prev (previous close), Last (latest price), Gap (% move from the previous close), "
                "Vol × (today's volume vs. its average) and Signals.",
                "The Signals column flags which of the bot's setups the mover currently matches.",
                "It only covers your watchlist, so curate that list (Watchlist Management) for the scan to be "
                "useful.",
            ],
            "examples": [
                "Before the open, Run scan and sort your attention to rows with a large Gap and Vol × above "
                "~2 — those are the names most likely to trigger a setup at the bell.",
                "A symbol showing +6% Gap but Vol × near 1 is a thin, unconfirmed move — treat it more "
                "cautiously than the same gap on 3× volume.",
            ],
            "mistakes": [
                "Running it mid-afternoon and expecting “pre-market” meaning — the gap/volume read is most "
                "meaningful near the open.",
                "Acting on a big gap with weak volume; low Vol × means the move isn't backed by "
                "participation.",
            ],
            "tips": [
                "Run it shortly before the open — gaps and volume are most meaningful pre-market.",
            ],
        },
        {
            "key": "earnings",
            "title": "Earnings Calendar",
            "summary": "Upcoming earnings dates, plus who's reporting today with beat/miss and the move.",
            "quickstart": "Click “Load earnings” for upcoming report dates per watchlist symbol. Click "
                          "“Load today” to see who reports today with EPS beat/miss, surprise % and the "
                          "pre/post-market move.",
            "what": "This section shows when each watchlist name reports earnings and, for today, how those "
                    "reports came in. It matters because earnings are scheduled volatility events — holding "
                    "into one is an event bet, and the reaction after a report is itself a tradable edge. "
                    "Use it to avoid unwanted event risk or to plan around it.",
            "how": [
                "“Upcoming”: click Load earnings to fetch report dates (via yfinance); the table shows each "
                "Symbol, its Earnings date and Days away (a countdown).",
                "“Reporting today”: click Load today to see who reports today with Session (pre/after "
                "market), Est EPS, Actual, Surprise %, the Pre/Post move and Sector. A contagion box "
                "highlights knock-on effects on related names.",
                "A live pre/post-market move needs extended-hours data enabled on your provider.",
                "Cross-reference before entering — a position held into earnings carries event risk the "
                "engine's normal stops can't contain across a gap.",
            ],
            "examples": [
                "Before letting a swing trade run over the weekend: Load earnings and check Days away — if "
                "the name reports Monday, you may want to exit or size down first.",
                "To fish for PEAD setups: Load today, sort by Surprise %, and look at how the biggest "
                "beats/misses are moving pre/post-market.",
            ],
            "mistakes": [
                "Holding a position into an earnings date you didn't check — a gap can blow through your "
                "stop.",
                "Expecting a live Pre/Post move without extended-hours data enabled — the column will be "
                "blank.",
            ],
            "tips": [
                "Many strategies avoid holding through earnings; the PEAD strategy deliberately trades the reaction after.",
            ],
        },
        {
            "key": "sectors",
            "title": "Sector Rotation & Breadth",
            "summary": "The 11 SPDR sector ETFs ranked by relative strength versus SPY.",
            "quickstart": "Click “Load sectors”. Leaders (outperforming SPY and above their 50-DMA) are the "
                          "rotation longs; the breadth box tells you how broad — and trustworthy — the "
                          "current move is.",
            "what": "This section ranks the 11 SPDR sector ETFs by relative strength versus SPY and "
                    "summarises market breadth. It matters because money rotates between sectors, and being "
                    "long the leaders while breadth is healthy is the core of the sector_rotation strategy. "
                    "Use it to see where strength is concentrated and whether the broad market confirms it.",
            "how": [
                "Click Load sectors to rank each sector ETF; columns show Return, vs SPY (relative "
                "strength), Above 50-DMA, a composite Score and a Leader flag.",
                "Leaders — outperforming SPY and trading above their 50-day average — are the candidates the "
                "sector_rotation strategy goes long.",
                "The breadth box summarises how broad participation is; strong breadth backs a rotation "
                "signal, weak/narrow breadth is a caution flag.",
            ],
            "examples": [
                "To align with rotation: Load sectors, note the top 2-3 Leaders (e.g. XLK, XLE), and favour "
                "longs in those sectors while avoiding the laggards at the bottom.",
                "If only one sector is green and breadth is weak, treat a “leader” cautiously — the move "
                "isn't broadly supported.",
            ],
            "mistakes": [
                "Chasing a leader while breadth is deteriorating — narrow leadership often precedes a "
                "pullback.",
                "Reading raw Return alone; “vs SPY” and Above-50-DMA are what define a true relative-"
                "strength leader.",
            ],
            "tips": [
                "Rotating into leading sectors and out of laggards is the core idea — confirm with breadth.",
            ],
        },
        {
            "key": "montecarlo",
            "title": "Monte Carlo & Market Intelligence",
            "summary": "Regime detection, adaptive thresholds and Monte Carlo equity projections.",
            "quickstart": "The left column auto-loads Market Regime and Adaptive Thresholds. For the "
                          "projection, set Runs and Horizon (trades) and click Run — the fan chart and "
                          "cards show the range of likely equity outcomes.",
            "what": "This section holds the higher-level market-intelligence tools: what regime the market "
                    "is in, how the engine is auto-tuning its gates to that regime, and a probabilistic "
                    "projection of future equity. It matters because it frames expectations — the same edge "
                    "behaves differently in a trending vs. choppy market, and the projection shows a range, "
                    "not a promise. Use it to set realistic expectations and sanity-check risk.",
            "how": [
                "“Market Regime” classifies current conditions (e.g. trending, choppy, volatile) — it "
                "loads automatically.",
                "“Adaptive Thresholds” shows how the engine is auto-tuning its gates to the current regime.",
                "“Monte Carlo Projection”: set Runs (number of simulated paths — default 1000, min 100, "
                "steps of 100) and Horizon (how many future trades to simulate — default 50, min 5, steps "
                "of 5), then click Run.",
                "It resamples your historical trade outcomes across many random orderings; the fan chart "
                "shows the spread of equity paths and the cards summarise the likely range (and downside "
                "risk).",
                "More Runs = a smoother, more stable distribution but a slightly slower computation; a "
                "longer Horizon projects further out with wider uncertainty.",
            ],
            "examples": [
                "To gauge realistic downside: run 5000 runs over a 50-trade horizon and read the worst-case "
                "band of the fan — that's roughly how bad a run of bad luck could look.",
                "Before pushing size, check Market Regime: if it says “choppy/volatile”, expect the "
                "engine's adaptive thresholds to tighten and fewer trades to qualify.",
            ],
            "mistakes": [
                "Treating the projection as a forecast of one path — it's a distribution of possibilities "
                "built from past trades, and assumes the future resembles the past.",
                "Running it with only a handful of historical trades — the resampled paths just echo that "
                "tiny sample and mean little.",
            ],
            "tips": [
                "Treat the projection as a distribution of possibilities, not a forecast of one path.",
            ],
        },
        {
            "key": "notes",
            "title": "Trade Journal Notes",
            "summary": "Attach searchable notes and tags to trades by trade ID.",
            "quickstart": "Enter a Trade ID (from the history table), write a Note, add comma-separated "
                          "Tags, and Save. Later, use Search text / Filter tag (or click a tag in the "
                          "cloud) to find notes.",
            "what": "This is a searchable journal that attaches free-form notes and tags to specific trades "
                    "by their ID. It matters because the qualitative context — why you took or skipped a "
                    "trade, what you saw, what you'd do differently — is exactly what the numbers don't "
                    "capture, and it's one of the fastest ways to improve. Use it right after a trade while "
                    "the reasoning is fresh.",
            "how": [
                "“Trade ID” references a trade from the Trade History table (e.g. 12); “Note” is your "
                "free-form text; “Tags (comma-sep)” are labels like “mistake, gap-up”. Click Save.",
                "To review: type a keyword in “Search text” and/or a tag in “Filter tag”, then click Search "
                "— matching notes list in the table (Trade, Note, Tags, Updated).",
                "A tag cloud shows your most-used tags; click one to filter to it quickly.",
                "Notes persist so you can build a personal record of lessons over time.",
            ],
            "examples": [
                "After a stopped-out trade: find its ID in Trade History, note “entered late, chased the "
                "gap”, tag it “mistake, chased”, and Save — then periodically Search “chased” to see the "
                "pattern.",
                "To tag your best setups: add the tag “A+setup” to trades that worked, then Filter tag = "
                "A+setup to study what they had in common.",
            ],
            "mistakes": [
                "Guessing a Trade ID — use the exact ID shown in the Trade History table or the note won't "
                "attach to the right trade.",
                "Never revisiting your notes; the value is in periodically searching them for recurring "
                "mistakes.",
            ],
            "tips": [
                "Journaling why you took (or skipped) a trade is one of the fastest ways to improve.",
            ],
        },
        {
            "key": "api",
            "title": "API, Export & Accounts",
            "summary": "Download CSV/PDF reports, manage REST API keys and browser notifications.",
            "quickstart": "Download Trades/Analytics as CSV or PDF from the Export bar. Create a REST API "
                          "key (Key name → Create key) for scripts. Enable browser push and manage it from "
                          "the 🔔 bell in the header.",
            "what": "This section bundles the dashboard's data-export, API-access and notification-"
                    "administration tools. It matters because it's how you get data out (for spreadsheets "
                    "or your own analysis), automate against the bot programmatically, and control real-time "
                    "browser alerts. Use it for reporting, integrations, and setting up notifications.",
            "how": [
                "“Export Data”: four one-click downloads — Trades CSV, Trades PDF, Analytics CSV, Analytics "
                "PDF.",
                "“Notifications”: real-time browser push for trades executed, stops hit, targets reached and "
                "AI alerts. Click “Enable notifications” to grant permission, “Open notifications” to review "
                "recent ones; the 🔔 bell in the header is where you enable and pick which categories notify "
                "you.",
                "“REST API Keys”: type a Key name (e.g. my-script) and Create key — the raw key is shown "
                "once, so copy it immediately. Authenticate calls with the header "
                "Authorization: Bearer <key>; docs live at /api/v1.",
                "“User Accounts”: multi-user support is off by default; set MULTI_USER_ENABLED=true to allow "
                "separate accounts with their own strategies, capital and watchlists.",
            ],
            "examples": [
                "To analyse performance in a spreadsheet: click “Analytics CSV”, open it in Excel/Sheets, "
                "and pivot by strategy or month.",
                "To pull live positions from a script: Create key, copy it, then call the API with "
                "`Authorization: Bearer <key>` per the /api/v1 docs.",
            ],
            "mistakes": [
                "Navigating away after creating an API key without copying it — the raw key is shown only "
                "once and can't be retrieved later.",
                "Expecting push notifications to work without clicking “Enable notifications” and granting "
                "the browser permission first.",
            ],
            "tips": [
                "Use the CSV exports to analyse performance in a spreadsheet or notebook.",
            ],
        },
        {
            "key": "tax",
            "title": "Tax Center",
            "summary": "Realized gains, tax-loss-harvest candidates and IRS 8949 / Schedule D exports.",
            "quickstart": "Pick a tax year, review realized gains and wash-sale flags, then download the "
                          "IRS 8949 CSV or Schedule D CSV for your filing.",
            "what": "This section turns your trade journal into a US-style realized-gains report using "
                    "FIFO cost basis. It splits short- vs long-term gains, flags wash sales, and surfaces "
                    "open positions you could harvest for a loss. Use it around quarterly estimates and "
                    "at year end.",
            "how": [
                "“Tax year” scopes every table to one year; leave it on “All years” for the full picture.",
                "The stat tiles show total realized gain, the short/long split, proceeds, wash-sale count "
                "and the total harvestable loss.",
                "“Realized Gains & Losses” lists each closed lot with its holding period and wash-sale flag.",
                "“Tax-Loss Harvest Candidates” lists open losers; a red “risk” badge means selling now would "
                "trip the 30-day wash-sale rule — the earliest clean rebuy date is shown.",
                "“⬇ 8949 CSV” downloads IRS Form 8949 rows; “⬇ Schedule D CSV” downloads the short/long "
                "summary.",
            ],
            "examples": [
                "To file for last year: set “Tax year” to that year, then click “⬇ 8949 CSV” and "
                "“⬇ Schedule D CSV” — both scope to the selected year.",
                "To find a year-end write-off: read the “Total harvestable loss” tile, then pick a "
                "candidate whose wash-sale badge reads “clear”.",
            ],
            "mistakes": [
                "Treating these numbers as filed tax advice — they are computed from the journal and should "
                "be confirmed with a professional.",
                "Harvesting a position flagged as wash-sale risk before its earliest clean rebuy date.",
            ],
            "tips": [
                "Harvest losses before year end to offset realized gains, respecting the wash-sale window.",
            ],
        },
        {
            "key": "rebalance",
            "title": "Rebalancing",
            "summary": "Current vs target allocation, per-bucket drift, and a suggested trim/add plan.",
            "quickstart": "Set target weights per bucket, read the drift bars, then Preview the suggested "
                          "trades. Apply is gated behind the admin password and REBALANCE_AUTO.",
            "what": "This section compares your live allocation (by sector, strategy or asset type) against "
                    "operator-defined targets and flags buckets that have drifted past the threshold. It "
                    "matters because drift quietly changes your risk profile. Use it periodically to keep "
                    "the book aligned with your plan.",
            "how": [
                "The paired donuts show current vs target allocation side by side.",
                "Each drift bar shows how far a bucket is over (red) or under (blue) its target.",
                "“Preview” lists the suggested trims and adds without placing any orders.",
                "“Apply” asks for the admin password and only returns a trim plan — orders are still placed "
                "deliberately via the manual-trade path.",
            ],
            "examples": [
                "To check if you have drifted: open the section and read the drift bars — any bar past the "
                "threshold line is a bucket worth rebalancing.",
                "To see the trades a rebalance would suggest: click “Preview” and read the trim/add list; "
                "no orders are placed.",
            ],
            "mistakes": [
                "Expecting Apply to place live orders automatically — it never does; it returns a plan.",
                "Rebalancing on tiny drift and churning commissions; respect the drift threshold.",
            ],
            "tips": [
                "Rebalance on a fixed cadence (e.g. monthly) rather than reacting to every wiggle.",
            ],
        },
        {
            "key": "dividends",
            "title": "Dividends",
            "summary": "Upcoming ex-dividend dates, trailing income, and yield-on-cost per position.",
            "quickstart": "Scan upcoming ex-div dates, check the trailing monthly income chart, and read "
                          "yield-on-cost per holding.",
            "what": "This section tracks the income side of total return: which holdings pay, when they go "
                    "ex-dividend, how much you have accrued, and each position's yield on its cost basis. "
                    "Use it to plan around ex-dates and to understand income contribution.",
            "how": [
                "The total-income tile sums accrued dividend income across the open book.",
                "“Upcoming Ex-Dividend” lists the next ex-dates from each holding's dividend history.",
                "The bar chart shows trailing monthly dividend income.",
                "“Yield on Cost” divides trailing income by each position's cost basis.",
            ],
            "examples": [
                "To avoid a surprise around an ex-date: scan “Upcoming Ex-Dividend” for the next date on a "
                "holding you plan to trade this week.",
                "To spot your best income compounder: sort your attention by the “Yield on Cost” column and "
                "find the highest figure.",
            ],
            "mistakes": [
                "Reading the price drop on an ex-dividend date as a loss — it reflects the dividend paid out, "
                "not a real drawdown.",
                "Comparing raw yield across holdings without cost basis — yield-on-cost is the personalised "
                "figure that matters for your book.",
            ],
            "tips": [
                "Yield-on-cost rises over time as a holding grows its dividend — a sign of a compounding payer.",
            ],
        },
        {
            "key": "economic",
            "title": "Economic Calendar",
            "summary": "This-week macro events (FOMC, CPI, NFP, GDP) with importance and blackout status.",
            "quickstart": "Check whether a macro blackout is active, then scan the week's events; filter by "
                          "importance to focus on the market-movers.",
            "what": "This section lists scheduled macro releases that move the whole market and shows whether "
                    "the engine is currently in a macro blackout window (when it avoids new entries). Use it "
                    "to anticipate volatility around FOMC, CPI, NFP and GDP.",
            "how": [
                "The blackout indicator turns amber when now falls inside an event's blackout window.",
                "The table lists each event's date/time (ET), importance and type.",
                "Use the importance filter to hide low-impact events.",
            ],
            "examples": [
                "To understand why the engine stopped opening trades: check whether the blackout indicator "
                "is amber — a macro window may be suppressing new entries.",
                "To plan around the week's volatility: set the importance filter to high and read the ET "
                "times for FOMC, CPI or NFP.",
            ],
            "mistakes": [
                "Assuming the calendar times are in your local zone — they are shown in US Eastern (ET).",
                "Opening a large new position minutes before a high-importance release and getting gapped "
                "through your stop.",
            ],
            "tips": [
                "High-importance events (FOMC, CPI, NFP) are the ones most likely to gap your stops.",
            ],
        },
        {
            "key": "webhooks",
            "title": "Webhooks",
            "summary": "Inbound webhook status, HMAC secret, veto rules and recent deliveries (TradingView).",
            "quickstart": "Confirm webhooks are enabled, copy the endpoint URL and HMAC secret, then point a "
                          "TradingView alert at it. Manage entry vetoes and review recent deliveries here.",
            "what": "This section manages the inbound webhook that lets external systems (e.g. TradingView "
                    "alerts) submit trades or veto new entries. It matters for automation and for cutting off "
                    "a symbol quickly. Use it to wire up alerts and audit what has been received.",
            "how": [
                "The status pill shows whether WEBHOOKS_ENABLED is on; trades require WEBHOOK_ALLOW_TRADES too.",
                "Copy the endpoint URL and the HMAC secret (masked; reveal to copy) into your alert.",
                "“Veto Rules” blocks new entries for a symbol; add or delete rules inline.",
                "“Recent Deliveries” logs the last inbound webhook calls with their outcome.",
            ],
            "examples": [
                "To wire up a TradingView alert: reveal and copy the endpoint URL and HMAC secret, then paste "
                "them into the alert's webhook settings.",
                "To halt new entries on a symbol without touching the strategy: add a Veto Rule for that "
                "symbol under “Veto Rules”.",
            ],
            "mistakes": [
                "Sending trades while WEBHOOK_ALLOW_TRADES is off — they are rejected.",
                "Posting without the HMAC signature when a secret is configured.",
            ],
            "tips": [
                "Rotate the HMAC secret if it may have leaked; update your alert at the same time.",
            ],
        },
        {
            "key": "indicator-alerts",
            "title": "Indicator Alerts",
            "summary": "Custom RSI / moving-average / volume / drawdown alert rules with a manual check.",
            "quickstart": "Build a rule (symbol × indicator × condition × threshold), arm it, and use “Check "
                          "Now” to evaluate every rule immediately.",
            "what": "This section lets you define technical and portfolio alert rules beyond simple price "
                    "levels — RSI crosses, golden/death moving-average crosses, volume spikes, drawdown and "
                    "daily-loss limits. Use it to get notified when a condition you care about fires.",
            "how": [
                "Pick a rule type, fill its parameters, and “Add rule” to arm it.",
                "The rules table lists every rule; toggle Active to arm/disarm (re-arming clears a trigger).",
                "“Check Now” evaluates all armed rules and reports which fired.",
                "The history feed shows recently triggered rules.",
            ],
            "examples": [
                "To watch for an oversold bounce: add an RSI rule on your symbol with condition “below” and "
                "threshold 30, then Add rule to arm it.",
                "To verify a rule works before relying on it: click “Check Now” and confirm the rule appears "
                "in the fired list when its condition is met.",
            ],
            "mistakes": [
                "Leaving a rule disarmed (Active off) and expecting it to notify you — only armed rules fire.",
                "Setting a volume-spike threshold so low that every normal session triggers it.",
            ],
            "tips": [
                "An RSI-below-30 cross flags oversold; a golden cross (50/200) flags a longer-term uptrend.",
            ],
        },
        {
            "key": "attribution",
            "title": "Performance Attribution",
            "summary": "P&L attributed to sectors and the market factor (beta / alpha decomposition).",
            "quickstart": "Read the sector waterfall to see which sectors drove P&L, then the market-factor "
                          "split into systematic (beta) and specific (alpha) return.",
            "what": "This section explains where returns came from: a sector-by-sector waterfall of realized "
                    "P&L and a market-factor decomposition (how much of the return was market beta vs "
                    "strategy-specific alpha). Use it to understand whether you were paid for skill or beta.",
            "how": [
                "The waterfall shows each sector's contribution to total P&L, largest first.",
                "The market-factor tiles show portfolio beta, alpha, and the systematic/specific P&L split.",
                "Positive alpha means return beyond what market exposure alone would explain.",
            ],
            "examples": [
                "To find your biggest P&L driver: read the top bar of the sector waterfall — it is the "
                "sector that contributed the most.",
                "To judge whether returns were skill or the market: compare the alpha tile against beta — "
                "meaningful positive alpha points to strategy edge.",
            ],
            "mistakes": [
                "Reading one strong month of alpha as durable skill — attribution needs a run of periods to "
                "be meaningful.",
                "Ignoring beta when the whole market rallied — a rising tide can flatter a high-beta book.",
            ],
            "tips": [
                "High beta with low alpha means the market did the work, not the strategy.",
            ],
        },
        {
            "key": "excursion",
            "title": "MAE / MFE Excursion",
            "summary": "How much heat each trade took (MAE) and how far it ran (MFE), in R-multiples — with stop/target advisories.",
            "quickstart": "Read the two histograms: the MAE chart shows how deep trades dipped against you "
                          "before closing; the MFE chart shows how far they ran in your favour. The advisories "
                          "up top translate those distributions into stop/target actions.",
            "what": "Maximum Adverse Excursion (MAE) is the worst a trade ever looked; Maximum Favourable "
                    "Excursion (MFE) is the best it ever looked — both measured from entry and expressed in "
                    "R-multiples of the trade's initial risk so a $2 stock and a $200 stock compare directly. "
                    "Together they reveal whether your stops and targets are well-placed: tight stops that eject "
                    "eventual winners, or conservative targets that cap runners, both show up here before they "
                    "show up in the P&L.",
            "how": [
                "The stat cards summarise the distributions: median and 90th-percentile MAE, median MFE, and "
                "the winners' median MAE (how much heat your winners typically take).",
                "The MAE histogram buckets trades by how far they fell against entry. A stop at 1.0R sits at "
                "the right edge — a big cluster there means stops are frequently threatened.",
                "The MFE histogram buckets trades by peak unrealised profit. A long right tail past your "
                "target R means trades routinely run well beyond where you exit.",
                "The advisory banner flags 'stops too tight', 'winners reversing into stops', or 'targets too "
                "conservative' once there are enough closed trades to be meaningful.",
            ],
            "examples": [
                "To decide whether to widen stops: check the winners' median MAE — if your winners routinely "
                "dip to 0.8-0.9R before working, a stop at 1.0R is barely holding them.",
                "To decide whether to extend targets: compare the MFE histogram's tail against your target R "
                "— a fat tail well beyond it means you're leaving money on the table.",
            ],
            "mistakes": [
                "Acting on a handful of trades — the advisories stay silent until there's a real sample, and "
                "you should too.",
                "Widening stops without checking risk — a wider stop means smaller size for the same dollar "
                "risk, or more risk for the same size.",
            ],
            "tips": [
                "MAE/MFE are captured intraday for every open position, so they reflect true peaks, not just "
                "end-of-day marks.",
                "The autotune loop reads the same signals — these advisories also surface in the engine log.",
            ],
        },
        {
            "key": "statements",
            "title": "Statements",
            "summary": "Generate and download monthly or quarterly performance statements.",
            "quickstart": "Choose monthly or quarterly, preview the statement, and download the PDF.",
            "what": "This section produces a periodic performance statement (the same one the scheduler can "
                    "email) with headline figures and line items. Use it for record-keeping or to share a "
                    "clean summary for a period.",
            "how": [
                "Pick the period (monthly or quarterly).",
                "The preview shows the statement subject, body and line items.",
                "“Download PDF” saves the rendered statement.",
            ],
            "examples": [
                "To archive last month's performance: select “monthly”, preview it, then “Download PDF” for "
                "your records.",
                "To share a clean quarter summary: select “quarterly” and download the PDF — it mirrors the "
                "statement the scheduler emails.",
            ],
            "mistakes": [
                "Expecting a downloaded statement to update itself later — it is a snapshot of the period at "
                "download time.",
                "Relying on the manual download for recurring records instead of enabling the scheduled "
                "email statements.",
            ],
            "tips": [
                "Quarterly statements smooth out month-to-month noise for a cleaner trend.",
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["System"])
async def dashboard(request: Request, _user: str = Depends(require_auth)):
    """Render the main dashboard page (requires HTTP Basic Auth)."""
    # Open-positions view preference (cards vs. table). Cards are the default;
    # the client persists the choice in the ``ustb_pos_view`` cookie so the
    # first server-rendered paint already matches what the user last picked.
    positions_view = "table" if request.cookies.get("ustb_pos_view") == "table" else "cards"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        # no-store: the page embeds live state and its JS/UI changes on every
        # deploy — a heuristically-cached copy kept showing pre-deploy UI.
        headers={"Cache-Control": "no-store"},
        context={
            "positions_view": positions_view,
            "status": _build_system_status(),
            "paper": _build_paper_trading(),
            "providers": _build_provider_status(),
            "engine": _build_engine_status(),
            "backtest_options": _build_backtest_options(),
            "help": _build_help(),
            "guides": _build_section_guides(),
            "guides_map": {g["key"]: g for g in _build_section_guides()},
            "help_anchor": HELP_SECTION_ANCHOR,
            "strategies": _build_strategies(),
            "strategy_comparison": _build_strategy_comparison(),
            "scores": _build_sample_scores(),
            "risk": _build_risk_rules(),
            # Drives the header's sign-out control — pointless (and confusing)
            # to offer when the dashboard is running without auth.
            "auth_enabled": bool(get_settings().DASHBOARD_AUTH_ENABLED),
            "watchlist_us": US_WATCHLIST,
            "watchlist_ca": CA_WATCHLIST,
            "watchlist_etf": ALL_ETFS,
        },
    )


def _build_strategy_comparison() -> List[Dict[str, Any]]:
    """Per-strategy performance rows (win rate, avg win/loss, profit factor)."""
    return _analytics_report().by_strategy


def _build_provider_status() -> Dict[str, Any]:
    """Market-data provider status for the dashboard selector."""
    from dashboard.provider_control import provider_status

    return provider_status(get_settings())


def _build_engine_status() -> Dict[str, Any]:
    """Engine service status + activity heartbeat for the control panel."""
    from dashboard.engine_control import engine_status

    return engine_status(get_settings())


def _build_backtest_options() -> Dict[str, Any]:
    """Symbol / strategy / date choices for the backtest form."""
    from dashboard.backtest_control import options

    return options()


# ---------------------------------------------------------------------------
# Branded sign-in (replaces the browser's native Basic-auth dialog)
# ---------------------------------------------------------------------------
# HTTP Basic still works everywhere — API clients, curl and the existing tests
# are unaffected.  These routes add a session-cookie path so a browser gets a
# real login screen instead of the OS credential prompt, which was the first
# thing a new user ever saw.  See dashboard/session.py for the token format.


def _login_page(request: Request, *, error: str = "", username: str = "", status_code: int = 200):
    """Render the branded sign-in page."""
    return templates.TemplateResponse(
        request,
        "login.html",
        context={"error": error, "username": username},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/login", response_class=HTMLResponse, tags=["System"], include_in_schema=False)
async def login_page(request: Request):
    """Public sign-in page.  Already-valid sessions are sent straight to the app."""
    from dashboard.session import COOKIE_NAME, verify_token

    settings = get_settings()
    if not settings.DASHBOARD_AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    token = request.cookies.get(COOKIE_NAME, "")
    if token and verify_token(token, settings.DASHBOARD_PASSWORD):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return _login_page(request)


@app.post("/login", tags=["System"], include_in_schema=False)
async def login_submit(request: Request):
    """Validate credentials and issue a session cookie.

    Accepts a form post (the login page) or a JSON body (programmatic clients).
    Credentials are checked exactly as :func:`dashboard.auth.require_auth` checks
    them — same constant-time comparison, same brute-force lockout — so this
    route cannot become a weaker way in.  Missing or invalid credentials answer
    ``401`` rather than a validation error, which keeps the "every mutating
    endpoint rejects anonymous callers" guarantee intact.
    """
    from dashboard.rate_limit import LoginGuard, client_key
    from dashboard.session import COOKIE_NAME, DEFAULT_TTL_SECONDS, issue_token

    settings = get_settings()
    wants_html = "text/html" in (request.headers.get("accept") or "")

    if not settings.DASHBOARD_AUTH_ENABLED:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    ckey = client_key(request)
    LoginGuard.check_locked(ckey)

    # Body may be form-encoded (the login page) or JSON (a script).  The
    # urlencoded case is parsed directly rather than via request.form(), which
    # needs the optional python-multipart package — not worth a new production
    # dependency for a two-field sign-in form.  A malformed or absent body
    # simply leaves the credentials empty, which is handled as a failed login.
    username = password = ""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    raw = await request.body()
    if content_type == "application/x-www-form-urlencoded":
        from urllib.parse import parse_qs

        try:
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            username = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
        except (UnicodeDecodeError, ValueError):
            pass
    elif content_type == "application/json":
        try:
            body = json.loads(raw or b"{}")
            if isinstance(body, dict):
                username = str(body.get("username", "") or "")
                password = str(body.get("password", "") or "")
        except (ValueError, UnicodeDecodeError):
            pass

    expected_user = settings.DASHBOARD_USERNAME
    expected_pass = settings.DASHBOARD_PASSWORD
    if not expected_pass:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard auth is enabled but DASHBOARD_PASSWORD is not set.",
        )

    ok = bool(username) and bool(password) and (
        secrets.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
        and secrets.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    )
    if not ok:
        LoginGuard.record_failure(ckey)
        log.warning("auth.login_failed", username=username or None,
                    ip=request.headers.get("x-forwarded-for", "") or
                       (request.client.host if request.client else "unknown"))
        if wants_html:
            return _login_page(
                request,
                error="Incorrect username or password.",
                username=username,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    LoginGuard.record_success(ckey)
    token = issue_token(expected_user, expected_pass)
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=DEFAULT_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        # Only mark Secure when the request actually arrived over TLS, so the
        # cookie still works for a plain-HTTP LAN/dev deployment.
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@app.post("/logout", tags=["System"], include_in_schema=False)
async def logout(request: Request, _user: str = Depends(require_auth)):
    """Clear the session cookie.  Auth-guarded so the anonymous sweep sees 401."""
    from dashboard.session import COOKIE_NAME

    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/health", tags=["System"])
async def health():
    """Liveness probe — "is the process up?" (also reports the trading mode).

    Deliberately unconditional and dependency-free so an orchestrator's liveness
    check never restarts the container over a *dependency* being down.  Use
    ``/readyz`` for the "can it actually trade?" readiness question.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "trading_mode": settings.TRADING_MODE,
        "broker": settings.BROKER,
        "timestamp": datetime.now(tz=EASTERN).isoformat(),
    }


@app.get("/readyz", tags=["System"])
async def readyz():
    """Readiness probe — pings broker/provider + reports engine-loop liveness.

    Returns 200 only when the market-data provider is credentialed, the broker
    is reachable, and the engine heartbeat (if present) is fresh; otherwise 503
    with the per-check detail so a monitor goes red when the bot is silently
    disconnected from the market (audit B-3).
    """
    from dashboard.ops import readiness_report

    report = await run_in_threadpool(readiness_report, get_settings())
    status_code = 200 if report["ready"] else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=report)


@app.get("/version", tags=["System"])
async def version():
    """Build/release identity (git SHA + version) baked in at deploy time."""
    from dashboard.ops import version_info

    return version_info(app.version)


@app.get("/metrics", tags=["System"])
async def metrics_endpoint():
    """Prometheus-format application metrics (audit B-3).

    Counters (requests, errors, manual trades, AI/LLM calls), gauges (engine
    liveness, open positions, last-cycle age), and latency histograms — a
    lightweight hand-rolled set with no ``prometheus_client`` dependency.
    """
    from dashboard import metrics as _metrics
    from dashboard.ops import refresh_engine_gauges

    refresh_engine_gauges(get_settings())
    return PlainTextResponse(
        _metrics.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Mode + paper-trading API
# ---------------------------------------------------------------------------


@app.get("/api/mode", tags=["System"])
async def api_mode():
    """Report the current trading mode (public — safe, read-only)."""
    settings = get_settings()
    return {
        "trading_mode": settings.TRADING_MODE,
        "is_live": settings.IS_LIVE_TRADING,
        "is_paper": not settings.IS_LIVE_TRADING,
        "broker": settings.BROKER,
        "paper_backend": settings.paper_broker_label,
    }


@app.post(
    "/api/mode/switch",
    tags=["Trading"],
    dependencies=[Depends(rate_limit("mode_switch", control=True))],
)
async def api_mode_switch(
    payload: ModeSwitchRequest, _user: str = Depends(require_auth)
):
    """Switch paper ⇄ live (admin password required to go live).

    Persists the new broker to ``.env`` and requests an engine restart.  The
    settings cache is cleared so the dashboard immediately reflects the new
    mode, and a best-effort mode-switch alert is dispatched.  Rate-limited.
    """
    from dashboard.mode_control import switch_mode

    target = payload.target.strip()
    admin_password = payload.admin_password

    settings = get_settings()
    old_mode = settings.TRADING_MODE
    result = switch_mode(target, admin_password, settings)

    if result.ok:
        # Reflect the new mode immediately for subsequent dashboard reads.
        if hasattr(get_settings, "cache_clear"):
            get_settings.cache_clear()
        try:
            from agent.alerts import AlertManager

            await AlertManager(get_settings()).notify_mode_switch(
                old_mode, result.mode, actor=_user
            )
        except Exception:  # noqa: BLE001 -- alerting must never fail the switch
            # The mode switch itself succeeded; only the notification failed.
            # Log it so a broken alert channel is diagnosable (B-9).
            log.warning("mode_switch.alert_failed", exc_info=True)

    return {
        "ok": result.ok,
        "mode": result.mode,
        "message": result.message,
        "restart_requested": result.restart_requested,
    }


# ---------------------------------------------------------------------------
# Market-data provider selection API
# ---------------------------------------------------------------------------


@app.get("/api/providers", tags=["Market Data"])
async def api_providers(_user: str = Depends(require_auth)):
    """List market-data providers with active/connected status (no secrets)."""
    from dashboard.provider_control import provider_status

    return provider_status(get_settings())


@app.post(
    "/api/providers/select",
    tags=["Market Data"],
    dependencies=[Depends(rate_limit("provider_select", control=True))],
)
async def api_provider_select(
    payload: ProviderSelectRequest, _user: str = Depends(require_auth)
):
    """Switch the active market-data provider (persists to .env + restarts)."""
    from dashboard.provider_control import switch_provider

    result = switch_provider(payload.provider, get_settings())
    if result.ok and hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    return {
        "ok": result.ok,
        "message": result.message,
        "active": result.active,
        "restart_requested": result.restart_requested,
    }


@app.post(
    "/api/providers/keys",
    tags=["Market Data"],
    dependencies=[Depends(rate_limit("provider_keys", control=True))],
)
async def api_provider_keys(
    payload: ProviderKeysRequest, _user: str = Depends(require_auth)
):
    """Save provider API keys to .env (auto-selects Alpaca when keys complete)."""
    from dashboard.provider_control import save_api_keys

    body = payload.model_dump(exclude_none=True)
    keys = body.get("keys", body)  # accept {"keys": {...}} or a flat dict
    if not isinstance(keys, dict):
        keys = {}
    result = save_api_keys(
        {str(k): str(v) for k, v in keys.items()}, get_settings()
    )
    if result.ok and hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    return {
        "ok": result.ok,
        "message": result.message,
        "active": result.active,
        "restart_requested": result.restart_requested,
    }


@app.get("/api/paper/summary", tags=["Analytics"])
async def api_paper_summary(_user: str = Depends(require_auth)):
    """Paper account balance, realized/today P&L, and headline stats."""
    paper = _build_paper_trading()
    return {k: v for k, v in paper.items() if k not in ("positions", "recent_trades")}


@app.get("/api/paper/positions", tags=["Analytics"])
async def api_paper_positions(_user: str = Depends(require_auth)):
    """Current open paper positions."""
    paper = _build_paper_trading()
    return {"open_count": paper["open_count"], "positions": paper["positions"]}


@app.get("/api/paper/trades", tags=["Analytics"])
async def api_paper_trades(_user: str = Depends(require_auth)):
    """Recent completed paper trades."""
    return {"trades": _build_paper_trading()["recent_trades"]}


@app.get("/api/paper/account", tags=["Analytics"])
async def api_paper_account(_user: str = Depends(require_auth)):
    """Full account snapshot: summary stats, balances, positions, trades.

    One call powering the dashboard's in-place refresh of the whole
    paper-trading section (kept live alongside the engine status poll so the
    position list can never drift out of sync with the engine panel's count).
    """
    return _build_paper_trading()


# ---------------------------------------------------------------------------
# Performance analytics API
# ---------------------------------------------------------------------------


def _analytics_report():
    """Build a :class:`PerformanceReport` from the live trade journal."""
    settings = get_settings()
    csv_path = Path(settings.DATA_DIR) / "trades.csv"
    return analyze_journal(csv_path, settings.TOTAL_CAPITAL)


@app.get("/api/analytics/summary", tags=["Analytics"])
async def analytics_summary(_user: str = Depends(require_auth)):
    """Portfolio-wide performance metrics (win rate, profit factor, Sharpe…)."""
    return _analytics_report().summary


@app.get("/api/analytics/by-strategy", tags=["Analytics"])
async def analytics_by_strategy(_user: str = Depends(require_auth)):
    """Per-strategy performance breakdown."""
    return {"by_strategy": _analytics_report().by_strategy}


@app.get("/api/analytics/by-symbol", tags=["Analytics"])
async def analytics_by_symbol(_user: str = Depends(require_auth)):
    """Per-symbol performance breakdown."""
    return {"by_symbol": _analytics_report().by_symbol}


@app.get("/api/analytics/equity-curve", tags=["Analytics"])
async def analytics_equity_curve(
    granularity: str = "trade",
    benchmark: str = "",
    _user: str = Depends(require_auth),
):
    """Cumulative equity curve with per-point drawdown.

    ``granularity=trade`` (default) returns one point per completed trade;
    ``granularity=daily`` buckets closed trades by exit date so a large
    journal doesn't render hundreds of x-axis points (monitoring F7).

    ``benchmark=SPY`` adds a ``benchmark`` value on each point: the benchmark
    (e.g. SPY) normalized to the curve's starting equity, for an overlay.
    """
    curve = _analytics_report().equity_curve

    if granularity == "daily" and curve:
        by_day: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for point in curve:
            day = str(point.get("date", ""))[:10]
            if day not in by_day:
                order.append(day)
            # Last trade of the day wins (equity is cumulative).
            prev = by_day.get(day, {})
            by_day[day] = {
                "date": day,
                "equity": point.get("equity"),
                "trade_pnl": round(
                    float(prev.get("trade_pnl", 0.0) or 0.0)
                    + float(point.get("trade_pnl", 0.0) or 0.0), 2),
            }
        curve = [by_day[d] for d in order]

    # Per-point drawdown from the running peak, shared by all F7 charts.
    peak = None
    out = []
    for point in curve:
        eq = float(point.get("equity", 0.0) or 0.0)
        peak = eq if peak is None or eq > peak else peak
        dd = (peak - eq) / peak if peak and peak > 0 else 0.0
        enriched = dict(point)
        enriched["drawdown_pct"] = round(dd, 4)
        out.append(enriched)

    bench_symbol = (benchmark or "").strip().upper()
    if bench_symbol and out:
        try:
            from data.fetcher import fetch_ohlcv

            df = await run_in_threadpool(fetch_ohlcv, bench_symbol, "2y", "1d")
            if df is not None and "Close" in getattr(df, "columns", []):
                closes = {
                    str(idx)[:10]: float(val)
                    for idx, val in df["Close"].items()
                    if val == val  # drop NaN
                }
                from analytics.performance import benchmark_curve

                start_eq = float(out[0].get("equity", 0.0) or 0.0)
                dates = [str(p.get("date", ""))[:10] for p in out]
                series = benchmark_curve(dates, start_eq, closes)
                for point, bval in zip(out, series):
                    point["benchmark"] = bval
        except Exception:  # noqa: BLE001 -- overlay is best-effort, never fatal
            bench_symbol = ""

    return {
        "equity_curve": out,
        "granularity": granularity,
        "benchmark": bench_symbol or None,
    }


@app.get("/api/analytics/trades", tags=["Analytics"])
async def analytics_trades(_user: str = Depends(require_auth)):
    """Most recent completed trades."""
    return {"trades": _analytics_report().recent_trades}


@app.get("/api/analytics/report", tags=["Analytics"])
async def analytics_report(_user: str = Depends(require_auth)):
    """Full analytics payload (summary + breakdowns + curve + recent trades)."""
    return _analytics_report().to_dict()


# ---------------------------------------------------------------------------
# Risk dashboard API
# ---------------------------------------------------------------------------


def _risk_report():
    """Build a :class:`RiskReport` from the live book and journal.

    Live quotes are passed in best-effort (monitoring F5): with quotes the
    exposure/open-risk figures are marked to market; without them the report
    still renders, valued at cost.
    """
    from analytics.risk_dashboard import build_risk_report

    settings = get_settings()
    prices: Dict[str, float] = {}
    try:
        from dashboard import quotes as _quotes

        positions = _load_open_positions(Path(settings.DATA_DIR))
        symbols = [str(p.get("symbol", "")) for p in positions if p.get("symbol")]
        if symbols:
            for sym, q in _quotes.get_quotes(symbols).items():
                if q.get("price") is not None:
                    prices[sym] = float(q["price"])
    except Exception:  # noqa: BLE001 -- quotes are an enhancement, never a dependency
        prices = {}
    return build_risk_report(
        settings.DATA_DIR,
        dict(settings.CAPITAL_BY_CURRENCY),
        settings.TOTAL_CAPITAL,
        prices=prices or None,
        daily_loss_limit_pct=settings.DAILY_LOSS_LIMIT_PCT,
        var_confidence=settings.VAR_CONFIDENCE,
        var_horizon_days=settings.VAR_HORIZON_DAYS,
    )


@app.get("/api/risk/report", tags=["Analytics"])
async def risk_report(_user: str = Depends(require_auth)):
    """Full portfolio risk payload (exposure, sectors, correlation, drawdown, P&L)."""
    return _risk_report().to_dict()


@app.get("/api/risk/exposure", tags=["Analytics"])
async def risk_exposure(_user: str = Depends(require_auth)):
    """Real-time portfolio exposure per currency and overall."""
    return _risk_report().exposure


@app.get("/api/risk/sectors", tags=["Analytics"])
async def risk_sectors(_user: str = Depends(require_auth)):
    """Sector / industry concentration of the open book."""
    return {"sector_concentration": _risk_report().sector_concentration}


@app.get("/api/attribution", tags=["Analytics"])
async def attribution_api(_user: str = Depends(require_auth)):
    """Performance attribution: P&L by sector, by strategy, and market-factor."""
    from analytics.attribution import build_attribution_report

    settings = get_settings()
    return await run_in_threadpool(
        build_attribution_report, str(settings.DATA_DIR), settings.TOTAL_CAPITAL
    )


@app.get("/api/statement", tags=["Analytics"])
async def statement_api(
    period: str = "monthly", format: str = "json",
    _user: str = Depends(require_auth),
):
    """Generate a monthly/quarterly statement; ``?format=pdf`` downloads the PDF."""
    from automation.statements import build_statement

    settings = get_settings()
    statement = await run_in_threadpool(build_statement, settings, period)
    if format.lower() == "pdf" and statement.pdf:
        from fastapi.responses import Response

        return Response(
            content=statement.pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition":
                f'attachment; filename="statement_{period}.pdf"'
            },
        )
    return {"subject": statement.subject, "body": statement.body,
            "period": statement.period, "lines": statement.lines}


@app.get("/api/dividends", tags=["Analytics"])
async def dividends_api(_user: str = Depends(require_auth)):
    """Accrued dividend income per open position + total-return summary."""
    from analytics.dividends import fetch_dividends, portfolio_dividend_income

    settings = get_settings()

    def _build():
        positions = _load_open_positions(Path(settings.DATA_DIR))
        divs = {}
        for p in positions:
            sym = str(p.get("symbol", ""))
            if sym:
                divs[sym] = fetch_dividends(sym)
        income = portfolio_dividend_income(positions, divs)
        # Realised capital P&L (this year) for a total-return figure.
        realized = 0.0
        try:
            from analytics.performance import load_completed_trades

            trades = load_completed_trades(Path(settings.DATA_DIR) / "trades.csv")
            if trades is not None and "pnl_net" in getattr(trades, "columns", []):
                realized = float(trades["pnl_net"].sum())
        except Exception:  # noqa: BLE001
            realized = 0.0
        from analytics.dividends import (
            monthly_dividend_income,
            total_return,
            upcoming_ex_dividends,
        )

        # Per-position yield-on-cost = accrued income / cost basis.
        cost_by_symbol = {
            str(p.get("symbol", "")): float(p.get("cost_basis", 0) or 0)
            for p in positions
        }
        for row in income.get("by_position", []):
            cost = cost_by_symbol.get(row["symbol"], 0.0)
            row["cost_basis"] = round(cost, 2)
            row["yield_on_cost_pct"] = (
                round(row["dividend_income"] / cost * 100, 2) if cost > 0 else None
            )

        return {
            "dividend_income": income,
            "total_return": total_return(realized, income["total"]),
            "upcoming": upcoming_ex_dividends(positions, divs),
            "monthly": monthly_dividend_income(positions, divs, months=12),
        }

    return await run_in_threadpool(_build)


@app.get("/api/economic-calendar", tags=["Analytics"])
async def economic_calendar_api(
    days: int = 21, _user: str = Depends(require_auth)
):
    """Upcoming macro events (FOMC/CPI/NFP/GDP) and the active blackout state."""
    from data.economic_calendar import active_blackout, upcoming_events

    settings = get_settings()
    days = max(1, min(int(days), 120))
    event_types = list(getattr(settings, "MACRO_BLACKOUT_EVENT_TYPES", []) or [])
    active = active_blackout(
        hours_before=settings.MACRO_BLACKOUT_HOURS_BEFORE,
        hours_after=settings.MACRO_BLACKOUT_HOURS_AFTER,
        data_dir=settings.DATA_DIR,
        event_types=event_types or None,
    )
    return {
        "events": upcoming_events(days=days, data_dir=settings.DATA_DIR),
        "blackout_active": active.to_dict() if active else None,
        "filter_mode": settings.MACRO_FILTER_MODE,
        "hours_before": settings.MACRO_BLACKOUT_HOURS_BEFORE,
        "hours_after": settings.MACRO_BLACKOUT_HOURS_AFTER,
    }


@app.get("/api/risk/var", tags=["Analytics"])
async def risk_var(
    confidence: float = 0.95, _user: str = Depends(require_auth)
):
    """Portfolio Value-at-Risk / CVaR (parametric + historical) and the
    configured hard-limit thresholds.

    ``?confidence=0.95|0.99`` re-derives VaR at 1- and 10-day horizons plus a
    return-distribution histogram from the portfolio's aligned return series,
    alongside the report's portfolio beta.
    """
    settings = get_settings()
    conf = 0.99 if float(confidence) >= 0.975 else 0.95
    report = _risk_report()
    returns = (report.var_cvar or {}).get("returns") or []
    from risk.limits import var_summary

    summary = var_summary(returns, conf)
    beta = (report.beta or {}).get("portfolio_beta")
    return {
        "var_cvar": report.var_cvar,
        "summary": summary,
        "beta": beta,
        "confidence": conf,
        "limits": {
            "max_sector_concentration_pct": settings.MAX_SECTOR_CONCENTRATION_PCT,
            "enforce_sector_limit": settings.ENFORCE_SECTOR_LIMIT,
            "max_position_correlation": settings.MAX_POSITION_CORRELATION,
            "enforce_correlation_limit": settings.ENFORCE_CORRELATION_LIMIT,
            "portfolio_var_limit_pct": settings.PORTFOLIO_VAR_LIMIT_PCT,
            "enforce_portfolio_var_limit": settings.ENFORCE_PORTFOLIO_VAR_LIMIT,
        },
    }


@app.get("/api/risk/correlations", tags=["Analytics"])
async def risk_correlations(_user: str = Depends(require_auth)):
    """Pairwise correlation between open positions."""
    report = _risk_report()
    return {
        "correlations": report.correlations,
        "max_correlation": report.max_correlation,
    }


@app.get("/api/risk/drawdown", tags=["Analytics"])
async def risk_drawdown(_user: str = Depends(require_auth)):
    """Current and maximum drawdown tracking."""
    return _risk_report().drawdown


@app.get("/api/risk/pnl-breakdown", tags=["Analytics"])
async def risk_pnl_breakdown(_user: str = Depends(require_auth)):
    """Daily / weekly / monthly realised-P&L breakdown."""
    return _risk_report().pnl_breakdown


# ---------------------------------------------------------------------------
# Engine control API — start/stop/restart + live status + logs (admin-gated)
# ---------------------------------------------------------------------------


@app.get("/api/engine/status", tags=["Trading"])
async def engine_status_api(_user: str = Depends(require_auth)):
    """Live engine status: systemd state + activity heartbeat.

    The systemd lookup shells out to ``systemctl show``; run it off the event
    loop so a slow probe never blocks other dashboard requests.
    """
    from dashboard.engine_control import engine_status

    return await run_in_threadpool(engine_status, get_settings())


@app.get("/api/engine/restart-status", tags=["Trading"])
async def engine_restart_status_api(_user: str = Depends(require_auth)):
    """Whether a requested engine restart is still pending or was acked (B-6).

    After a mode/provider/key change drops the restart sentinel, the client can
    poll this to confirm the running engine actually consumed it (``pending``
    flips false and ``acked_at`` is set) rather than the signal silently
    no-op-ing and leaving the UI claiming a change that never took effect.
    """
    from dashboard.mode_control import restart_status

    return restart_status(get_settings().DATA_DIR)


@app.post(
    "/api/engine/control",
    tags=["Trading"],
    dependencies=[Depends(rate_limit("engine_control", control=True))],
)
async def engine_control_api(
    payload: EngineControlRequest, _user: str = Depends(require_auth)
):
    """Start / stop / restart the trading engine (admin password required).

    Rate-limited per client IP.
    """
    from dashboard.engine_control import control_engine

    result = await run_in_threadpool(
        control_engine, payload.action, payload.admin_password, get_settings()
    )
    return result


@app.get("/api/engine/logs", tags=["Trading"])
async def engine_logs_api(lines: int = 200, _user: str = Depends(require_auth)):
    """Return the last *lines* of engine logs from journald."""
    from dashboard.engine_control import engine_logs

    return await run_in_threadpool(engine_logs, lines)


# ---------------------------------------------------------------------------
# Backtesting API — run backtests from the UI in a background thread
# ---------------------------------------------------------------------------


@app.get("/api/backtest/options", tags=["Backtesting"])
async def backtest_options_api(_user: str = Depends(require_auth)):
    """Symbol / strategy / date choices for the backtest form."""
    from dashboard.backtest_control import options

    return options()


@app.post(
    "/api/backtest/run",
    tags=["Backtesting"],
    dependencies=[Depends(rate_limit("backtest_run", control=True))],
)
async def backtest_run_api(
    payload: BacktestRunRequest, _user: str = Depends(require_auth)
):
    """Validate parameters and kick off a background backtest run.

    Rate-limited per client IP.
    """
    from dashboard.backtest_control import start_backtest

    return start_backtest(payload.model_dump(exclude_none=True))


@app.post(
    "/api/backtest/walk-forward",
    tags=["Backtesting"],
    dependencies=[Depends(rate_limit("backtest_run", control=True))],
)
async def walk_forward_run_api(
    payload: BacktestRunRequest, _user: str = Depends(require_auth)
):
    """Kick off a background walk-forward optimization run.

    Accepts the backtest fields plus ``train_months``, ``test_months``,
    ``step_months``, ``objective`` and an optional ``param_grid`` for the sweep.
    Poll ``/api/backtest/status/{job_id}`` for the result (same registry).
    """
    from dashboard.backtest_control import start_walk_forward

    return start_walk_forward(payload.model_dump(exclude_none=True))


@app.get("/api/backtest/latest", tags=["Backtesting"])
async def backtest_latest_api(_user: str = Depends(require_auth)):
    """The most recently started backtest job, so the UI can resume after
    a page reload instead of losing the run/results."""
    from dashboard.backtest_control import latest_job

    job = latest_job()
    return {"ok": True, "job": job}


@app.get("/api/backtest/status/{job_id}", tags=["Backtesting"])
async def backtest_status_api(job_id: str, _user: str = Depends(require_auth)):
    """Poll a backtest job: state, message, and results when complete."""
    from dashboard.backtest_control import get_job

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job id.")
    return job


# ---------------------------------------------------------------------------
# Feature routers — each new dashboard feature ships as its own APIRouter that
# shares the HTTP Basic auth dependency (dashboard.auth.require_auth).
# ---------------------------------------------------------------------------

from dashboard.universe_router import router as _universe_router  # noqa: E402
from dashboard.watchlist_router import router as _watchlist_router  # noqa: E402
from dashboard.notes_router import router as _notes_router  # noqa: E402
from dashboard.manual_trade_router import router as _manual_trade_router  # noqa: E402
from dashboard.trade_selection_router import router as _trade_selection_router  # noqa: E402
from dashboard.insights_router import router as _insights_router  # noqa: E402
from dashboard.export_router import router as _export_router  # noqa: E402
from dashboard.users_router import router as _users_router  # noqa: E402
from dashboard.push_router import router as _push_router  # noqa: E402
from dashboard.api_v1 import router as _api_v1_router  # noqa: E402
from dashboard.live_router import router as _live_router  # noqa: E402
from dashboard.history_router import router as _history_router  # noqa: E402
from dashboard.activity_router import router as _activity_router  # noqa: E402
from dashboard.alerts_router import router as _alerts_router  # noqa: E402
from dashboard.rationale_router import router as _rationale_router  # noqa: E402
from dashboard.ai_router import router as _ai_router  # noqa: E402
from dashboard.ta_router import router as _ta_router  # noqa: E402
from dashboard.positions_router import router as _positions_router  # noqa: E402
from dashboard.earnings_router import router as _earnings_router  # noqa: E402
from dashboard.memory_router import router as _memory_router  # noqa: E402
from dashboard.ws_pnl import router as _ws_pnl_router  # noqa: E402
from dashboard.tax_router import router as _tax_router  # noqa: E402
from dashboard.price_alerts_router import router as _price_alerts_router  # noqa: E402
from dashboard.webhook_router import router as _webhook_router  # noqa: E402
from dashboard.indicator_alerts_router import router as _indicator_alerts_router  # noqa: E402
from dashboard.rebalance_router import router as _rebalance_router  # noqa: E402

for _r in (
    _universe_router,
    _watchlist_router,
    _notes_router,
    _manual_trade_router,
    _trade_selection_router,
    _insights_router,
    _export_router,
    _users_router,
    _push_router,
    _api_v1_router,
    _live_router,
    _history_router,
    _activity_router,
    _alerts_router,
    _rationale_router,
    _ai_router,
    _ta_router,
    _positions_router,
    _earnings_router,
    _memory_router,
    _ws_pnl_router,
    _tax_router,
    _price_alerts_router,
    _webhook_router,
    _indicator_alerts_router,
    _rebalance_router,
):
    app.include_router(_r)


@app.on_event("startup")
async def _startup_rate_limit_backend() -> None:
    """Wire the shared limiter store and warn on unsafe multi-worker setups (B-4)."""
    from dashboard.rate_limit import configure_backend, warn_if_multiworker

    configure_backend(get_settings())
    warn_if_multiworker(get_settings())
