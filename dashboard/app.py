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
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.concurrency import run_in_threadpool

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so we can import config / signals / risk
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analytics.performance import analyze_journal
from config.settings import Settings, get_settings, momentum_weights, swing_weights
from config.universe import ALL_SYMBOLS, CA_WATCHLIST, US_WATCHLIST
from fastapi.templating import Jinja2Templates
from signals.signal_types import Grade

# ---------------------------------------------------------------------------
# App & templates
# ---------------------------------------------------------------------------
app = FastAPI(title="US Trading Bot Dashboard", version="0.1.0")

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

# ---------------------------------------------------------------------------
# Authentication — HTTP Basic Auth guarding every page except /health
# ---------------------------------------------------------------------------
# The implementation lives in dashboard.auth so the feature routers can share
# the exact same guard without importing this module (which would be circular).
from dashboard.auth import require_auth  # noqa: E402

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
        "total_symbols": len(ALL_SYMBOLS),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    mask = exits.dt.date == datetime.now().date()
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _user: str = Depends(require_auth)):
    """Render the main dashboard page (requires HTTP Basic Auth)."""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "status": _build_system_status(),
            "paper": _build_paper_trading(),
            "providers": _build_provider_status(),
            "engine": _build_engine_status(),
            "backtest_options": _build_backtest_options(),
            "help": _build_help(),
            "strategies": _build_strategies(),
            "strategy_comparison": _build_strategy_comparison(),
            "scores": _build_sample_scores(),
            "risk": _build_risk_rules(),
            "watchlist_us": US_WATCHLIST,
            "watchlist_ca": CA_WATCHLIST,
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


@app.get("/health")
async def health():
    """Simple health-check endpoint (also reports the trading mode)."""
    settings = get_settings()
    return {
        "status": "ok",
        "trading_mode": settings.TRADING_MODE,
        "broker": settings.BROKER,
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Mode + paper-trading API
# ---------------------------------------------------------------------------


@app.get("/api/mode")
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


@app.post("/api/mode/switch")
async def api_mode_switch(request: Request, _user: str = Depends(require_auth)):
    """Switch paper ⇄ live (admin password required to go live).

    Persists the new broker to ``.env`` and requests an engine restart.  The
    settings cache is cleared so the dashboard immediately reflects the new
    mode, and a best-effort mode-switch alert is dispatched.
    """
    from dashboard.mode_control import switch_mode

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    target = str(body.get("target", "")).strip()
    admin_password = str(body.get("admin_password", ""))

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
            pass

    return {
        "ok": result.ok,
        "mode": result.mode,
        "message": result.message,
        "restart_requested": result.restart_requested,
    }


# ---------------------------------------------------------------------------
# Market-data provider selection API
# ---------------------------------------------------------------------------


@app.get("/api/providers")
async def api_providers(_user: str = Depends(require_auth)):
    """List market-data providers with active/connected status (no secrets)."""
    from dashboard.provider_control import provider_status

    return provider_status(get_settings())


@app.post("/api/providers/select")
async def api_provider_select(request: Request, _user: str = Depends(require_auth)):
    """Switch the active market-data provider (persists to .env + restarts)."""
    from dashboard.provider_control import switch_provider

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    result = switch_provider(str(body.get("provider", "")), get_settings())
    if result.ok and hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    return {
        "ok": result.ok,
        "message": result.message,
        "active": result.active,
        "restart_requested": result.restart_requested,
    }


@app.post("/api/providers/keys")
async def api_provider_keys(request: Request, _user: str = Depends(require_auth)):
    """Save provider API keys to .env (auto-selects Alpaca when keys complete)."""
    from dashboard.provider_control import save_api_keys

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
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


@app.get("/api/paper/summary")
async def api_paper_summary(_user: str = Depends(require_auth)):
    """Paper account balance, realized/today P&L, and headline stats."""
    paper = _build_paper_trading()
    return {k: v for k, v in paper.items() if k not in ("positions", "recent_trades")}


@app.get("/api/paper/positions")
async def api_paper_positions(_user: str = Depends(require_auth)):
    """Current open paper positions."""
    paper = _build_paper_trading()
    return {"open_count": paper["open_count"], "positions": paper["positions"]}


@app.get("/api/paper/trades")
async def api_paper_trades(_user: str = Depends(require_auth)):
    """Recent completed paper trades."""
    return {"trades": _build_paper_trading()["recent_trades"]}


# ---------------------------------------------------------------------------
# Performance analytics API
# ---------------------------------------------------------------------------


def _analytics_report():
    """Build a :class:`PerformanceReport` from the live trade journal."""
    settings = get_settings()
    csv_path = Path(settings.DATA_DIR) / "trades.csv"
    return analyze_journal(csv_path, settings.TOTAL_CAPITAL)


@app.get("/api/analytics/summary")
async def analytics_summary(_user: str = Depends(require_auth)):
    """Portfolio-wide performance metrics (win rate, profit factor, Sharpe…)."""
    return _analytics_report().summary


@app.get("/api/analytics/by-strategy")
async def analytics_by_strategy(_user: str = Depends(require_auth)):
    """Per-strategy performance breakdown."""
    return {"by_strategy": _analytics_report().by_strategy}


@app.get("/api/analytics/by-symbol")
async def analytics_by_symbol(_user: str = Depends(require_auth)):
    """Per-symbol performance breakdown."""
    return {"by_symbol": _analytics_report().by_symbol}


@app.get("/api/analytics/equity-curve")
async def analytics_equity_curve(_user: str = Depends(require_auth)):
    """Cumulative equity curve, one point per completed trade."""
    return {"equity_curve": _analytics_report().equity_curve}


@app.get("/api/analytics/trades")
async def analytics_trades(_user: str = Depends(require_auth)):
    """Most recent completed trades."""
    return {"trades": _analytics_report().recent_trades}


@app.get("/api/analytics/report")
async def analytics_report(_user: str = Depends(require_auth)):
    """Full analytics payload (summary + breakdowns + curve + recent trades)."""
    return _analytics_report().to_dict()


# ---------------------------------------------------------------------------
# Risk dashboard API
# ---------------------------------------------------------------------------


def _risk_report():
    """Build a :class:`RiskReport` from the live book and journal."""
    from analytics.risk_dashboard import build_risk_report

    settings = get_settings()
    return build_risk_report(
        settings.DATA_DIR,
        dict(settings.CAPITAL_BY_CURRENCY),
        settings.TOTAL_CAPITAL,
    )


@app.get("/api/risk/report")
async def risk_report(_user: str = Depends(require_auth)):
    """Full portfolio risk payload (exposure, sectors, correlation, drawdown, P&L)."""
    return _risk_report().to_dict()


@app.get("/api/risk/exposure")
async def risk_exposure(_user: str = Depends(require_auth)):
    """Real-time portfolio exposure per currency and overall."""
    return _risk_report().exposure


@app.get("/api/risk/sectors")
async def risk_sectors(_user: str = Depends(require_auth)):
    """Sector / industry concentration of the open book."""
    return {"sector_concentration": _risk_report().sector_concentration}


@app.get("/api/risk/correlations")
async def risk_correlations(_user: str = Depends(require_auth)):
    """Pairwise correlation between open positions."""
    report = _risk_report()
    return {
        "correlations": report.correlations,
        "max_correlation": report.max_correlation,
    }


@app.get("/api/risk/drawdown")
async def risk_drawdown(_user: str = Depends(require_auth)):
    """Current and maximum drawdown tracking."""
    return _risk_report().drawdown


@app.get("/api/risk/pnl-breakdown")
async def risk_pnl_breakdown(_user: str = Depends(require_auth)):
    """Daily / weekly / monthly realised-P&L breakdown."""
    return _risk_report().pnl_breakdown


# ---------------------------------------------------------------------------
# Engine control API — start/stop/restart + live status + logs (admin-gated)
# ---------------------------------------------------------------------------


@app.get("/api/engine/status")
async def engine_status_api(_user: str = Depends(require_auth)):
    """Live engine status: systemd state + activity heartbeat.

    The systemd lookup shells out to ``systemctl show``; run it off the event
    loop so a slow probe never blocks other dashboard requests.
    """
    from dashboard.engine_control import engine_status

    return await run_in_threadpool(engine_status, get_settings())


@app.post("/api/engine/control")
async def engine_control_api(request: Request, _user: str = Depends(require_auth)):
    """Start / stop / restart the trading engine (admin password required)."""
    from dashboard.engine_control import control_engine

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    action = str(body.get("action", ""))
    admin_password = str(body.get("admin_password", ""))

    result = await run_in_threadpool(
        control_engine, action, admin_password, get_settings()
    )
    return result


@app.get("/api/engine/logs")
async def engine_logs_api(lines: int = 200, _user: str = Depends(require_auth)):
    """Return the last *lines* of engine logs from journald."""
    from dashboard.engine_control import engine_logs

    return await run_in_threadpool(engine_logs, lines)


# ---------------------------------------------------------------------------
# Backtesting API — run backtests from the UI in a background thread
# ---------------------------------------------------------------------------


@app.get("/api/backtest/options")
async def backtest_options_api(_user: str = Depends(require_auth)):
    """Symbol / strategy / date choices for the backtest form."""
    from dashboard.backtest_control import options

    return options()


@app.post("/api/backtest/run")
async def backtest_run_api(request: Request, _user: str = Depends(require_auth)):
    """Validate parameters and kick off a background backtest run."""
    from dashboard.backtest_control import start_backtest

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return start_backtest(body)


@app.get("/api/backtest/status/{job_id}")
async def backtest_status_api(job_id: str, _user: str = Depends(require_auth)):
    """Poll a backtest job: state, message, and results when complete."""
    from dashboard.backtest_control import get_job

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job id.")
    return job
