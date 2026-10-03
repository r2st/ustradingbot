"""Public viral pages — free tools, blog, embed, sitemap, and robots.txt.

None of these routes require authentication so they can be indexed by
search engines and shared freely.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

router = APIRouter(tags=["viral"])

templates = None


def _get_templates():
    global templates
    if templates is None:
        from dashboard.app import templates as app_templates
        templates = app_templates
    return templates

# ---------------------------------------------------------------------------
# Blog articles
# ---------------------------------------------------------------------------

ARTICLES = [
    {
        "slug": "ai-trading-bots-retail-investing",
        "title": "How AI Trading Bots Are Changing Retail Investing",
        "excerpt": "Explore how artificial intelligence is leveling the playing field for retail traders with automated strategies and real-time risk management.",
        "date": "October 1, 2024",
        "read_time": "7 min read",
        "content_html": """
<p>The world of retail investing has undergone a dramatic transformation. What was once the exclusive domain of institutional traders with teams of analysts and proprietary algorithms is now accessible to individual investors through AI-powered trading bots.</p>

<h2>The Democratization of Algorithmic Trading</h2>

<p>Algorithmic trading has been used by hedge funds and investment banks for decades. These systems can analyze vast amounts of data, execute trades in milliseconds, and manage risk with mathematical precision. Until recently, building and running such systems required millions in infrastructure and a team of quantitative analysts.</p>

<p>Today, AI trading bots bring these same capabilities to retail investors. Modern platforms can scan thousands of stocks, identify patterns, and execute trades based on sophisticated strategies — all without requiring users to write a single line of code.</p>

<h2>Key Advantages of AI Trading Bots</h2>

<h3>Emotion-Free Decision Making</h3>
<p>One of the biggest challenges for retail traders is emotional decision-making. Fear and greed drive poor trading decisions — selling too early during a dip, holding too long during a rally, or revenge trading after a loss. AI bots follow their programmed strategies without emotional bias, executing trades based purely on data and predefined rules.</p>

<h3>24/7 Market Monitoring</h3>
<p>Markets don't sleep, and neither do trading bots. While a human trader might miss a key after-hours development or pre-market opportunity, an AI system continuously monitors price action, news feeds, and technical indicators. This constant vigilance ensures no opportunity goes unnoticed.</p>

<h3>Systematic Risk Management</h3>
<p>Professional traders live by their risk management rules. AI bots enforce these rules automatically: setting stop losses, calculating <a href="/tools/position-size-calculator">optimal position sizes</a>, and ensuring no single trade can devastate a portfolio. Tools like our free <a href="/tools/risk-reward-calculator">risk/reward calculator</a> help traders evaluate setups before committing capital.</p>

<h2>Types of AI Trading Strategies</h2>

<p>Modern AI trading bots employ various strategies, often combining multiple approaches:</p>

<ul>
<li><strong>Momentum Trading</strong> — Identifying stocks with strong directional movement and riding the trend until signals weaken</li>
<li><strong>Mean Reversion</strong> — Buying oversold stocks and selling overbought ones, betting that prices return to their average</li>
<li><strong>Earnings-Based</strong> — Trading around earnings announcements using historical patterns and sentiment analysis</li>
<li><strong>Technical Analysis</strong> — Using chart patterns, support/resistance levels, and indicators to time entries and exits</li>
</ul>

<h2>Risk Considerations</h2>

<p>While AI trading bots offer significant advantages, they are not without risks. Past performance does not guarantee future results. Markets can behave unpredictably, and even the most sophisticated algorithms can face losses during extreme volatility or black swan events.</p>

<p>Smart traders use tools like a <a href="/tools/profit-loss-calculator">P&L calculator</a> to understand their exposure and always define their maximum acceptable loss before entering any trade.</p>

<h2>The Future of Retail Trading</h2>

<p>As AI technology continues to advance, we can expect trading bots to become even more sophisticated. Natural language processing will enable better news analysis, reinforcement learning will improve strategy adaptation, and improved computing power will allow more complex real-time analysis.</p>

<p>The key for retail investors is to approach AI trading as a tool — one that amplifies good strategy and discipline rather than replacing the need for them. The most successful traders combine AI efficiency with human judgment about market conditions, risk tolerance, and investment goals.</p>
""",
    },
    {
        "slug": "risk-management-rules-traders",
        "title": "5 Risk Management Rules Every Trader Should Follow",
        "excerpt": "Learn the five essential risk management principles that separate profitable traders from those who blow up their accounts.",
        "date": "September 15, 2024",
        "read_time": "6 min read",
        "content_html": """
<p>Ask any consistently profitable trader what their secret is, and the answer almost always comes back to risk management. It's not about finding the perfect entry or predicting market direction — it's about surviving long enough for your edge to play out.</p>

<p>Here are five risk management rules that every trader, from beginner to advanced, should follow religiously.</p>

<h2>1. Never Risk More Than 1-2% Per Trade</h2>

<p>This is the golden rule of trading risk management. On any single trade, you should never risk more than 1-2% of your total trading account. This means that even a string of losing trades won't devastate your portfolio.</p>

<p>Here's the math: with a $25,000 account and a 2% risk limit, your maximum loss per trade is $500. Even ten consecutive losing trades (which is statistically unlikely with a decent strategy) would only draw down your account by 20%.</p>

<p>Use our free <a href="/tools/position-size-calculator">position size calculator</a> to determine exactly how many shares to buy for any trade setup while staying within your risk limits.</p>

<h2>2. Always Define Your Stop Loss Before Entering</h2>

<p>Every trade should have a predetermined exit point. Before you enter a position, you should know exactly where you'll exit if the trade goes against you. This could be based on:</p>

<ul>
<li><strong>Technical levels</strong> — Below a key support level or moving average</li>
<li><strong>Volatility-based</strong> — A certain number of ATR (Average True Range) from your entry</li>
<li><strong>Percentage-based</strong> — A fixed percentage below your entry price</li>
<li><strong>Dollar amount</strong> — Your maximum acceptable loss in dollar terms</li>
</ul>

<p>The specific method matters less than the discipline of always having a stop. Trading without a stop loss is gambling, not trading.</p>

<h2>3. Maintain a Minimum 1:2 Risk/Reward Ratio</h2>

<p>Before entering any trade, calculate the ratio between what you stand to lose (risk) and what you stand to gain (reward). At minimum, aim for a 1:2 ratio — meaning your potential reward should be at least twice your potential risk.</p>

<p>Why? Because with a 1:2 ratio, you only need to be right 34% of the time to break even. With a 1:3 ratio, you only need 25% winners. This gives your strategy room to be wrong more often than it's right and still be profitable.</p>

<p>Try our <a href="/tools/risk-reward-calculator">risk/reward calculator</a> to evaluate any trade setup before committing capital.</p>

<h2>4. Limit Daily and Weekly Drawdowns</h2>

<p>Individual trade risk limits are necessary but not sufficient. You also need aggregate limits. A common framework:</p>

<ul>
<li><strong>Daily loss limit</strong> — Stop trading for the day after losing 3-5% of your account</li>
<li><strong>Weekly loss limit</strong> — Reduce position sizes or stop trading after a 5-8% weekly drawdown</li>
<li><strong>Monthly loss limit</strong> — Re-evaluate your strategy entirely if you're down 10-15% in a month</li>
</ul>

<p>These circuit breakers prevent the emotional spiral that often follows a losing streak. When you hit your limit, step away. The market will be there tomorrow.</p>

<h2>5. Track Every Trade and Review Your Performance</h2>

<p>You can't manage what you don't measure. Keep a detailed trading journal that records:</p>

<ul>
<li>Entry and exit prices, dates, and times</li>
<li>Position size and dollar risk</li>
<li>Your thesis for entering the trade</li>
<li>What happened and whether you followed your plan</li>
<li>P&amp;L for each trade (use our <a href="/tools/profit-loss-calculator">P&L calculator</a>)</li>
</ul>

<p>Review your journal weekly. Look for patterns in your winning and losing trades. Are you making more money on certain types of setups? Are you consistently losing on trades you took impulsively? The data will reveal where to focus your improvement.</p>

<h2>The Bottom Line</h2>

<p>Risk management isn't sexy. It won't give you a story about the time you made 500% on a single trade. But it will keep you in the game long enough for compounding to work its magic. The traders who survive and thrive are the ones who prioritize not losing over winning big.</p>

<p>Start by calculating your risk on every trade. Use the tools available to you. And remember: the goal isn't to maximize any single trade — it's to maximize the long-term growth of your account.</p>
""",
    },
    {
        "slug": "algorithmic-trading-beginners-guide",
        "title": "The Complete Guide to Algorithmic Trading for Beginners",
        "excerpt": "Everything you need to know about getting started with algorithmic trading, from basic concepts to choosing your first strategy.",
        "date": "September 1, 2024",
        "read_time": "9 min read",
        "content_html": """
<p>Algorithmic trading — using computer programs to execute trades based on predefined rules — has grown from a niche practice of Wall Street quants to a mainstream approach used by traders of all levels. If you're curious about getting started, this guide covers the fundamentals.</p>

<h2>What Is Algorithmic Trading?</h2>

<p>At its core, algorithmic trading (also called algo trading, automated trading, or systematic trading) means using a computer program to follow a defined set of instructions for placing trades. These instructions can be based on timing, price, quantity, mathematical models, or any combination of factors.</p>

<p>The key difference from discretionary trading is consistency. A human trader might change their mind, hesitate, or let emotions influence their decisions. An algorithm does exactly what it's programmed to do, every time.</p>

<h2>Core Components of an Algo Trading System</h2>

<h3>1. Market Data</h3>
<p>Every trading algorithm needs data to make decisions. This includes real-time price quotes, historical price data, volume information, and potentially alternative data like news sentiment, social media trends, or economic indicators.</p>

<h3>2. Signal Generation</h3>
<p>This is the brain of the system — the logic that decides when to buy and sell. Signals can be based on:</p>
<ul>
<li><strong>Technical indicators</strong> — Moving averages, RSI, MACD, Bollinger Bands</li>
<li><strong>Statistical models</strong> — Mean reversion, correlation analysis, regression</li>
<li><strong>Machine learning</strong> — Pattern recognition, classification, prediction</li>
<li><strong>Fundamental data</strong> — Earnings, revenue growth, valuation metrics</li>
</ul>

<h3>3. Risk Management</h3>
<p>Perhaps the most important component. Risk management rules determine how much to trade and when to exit. Key concepts include <a href="/tools/position-size-calculator">position sizing</a>, stop loss placement, portfolio allocation, and maximum drawdown limits.</p>

<h3>4. Execution</h3>
<p>Once a signal is generated and risk checks pass, the system needs to execute the trade through a broker API. Good execution minimizes slippage (the difference between expected and actual fill price) and handles edge cases like partial fills.</p>

<h2>Popular Beginner Strategies</h2>

<h3>Moving Average Crossover</h3>
<p>One of the simplest and most well-known strategies. Buy when a short-term moving average crosses above a long-term one (golden cross), and sell when it crosses below (death cross). While simple, this strategy captures major trends and avoids most sideways chop.</p>

<h3>RSI Mean Reversion</h3>
<p>The Relative Strength Index (RSI) measures whether a stock is overbought or oversold. A basic strategy buys when RSI drops below 30 (oversold) and sells when it rises above 70 (overbought). This works best in range-bound markets.</p>

<h3>Breakout Trading</h3>
<p>Buy when price breaks above resistance or short when it breaks below support. The key is confirming breakouts with volume and managing false breakouts with tight stops. Calculate your risk for each breakout attempt using a <a href="/tools/risk-reward-calculator">risk/reward calculator</a>.</p>

<h2>Backtesting: Testing Your Strategy</h2>

<p>Before risking real money, always backtest your strategy against historical data. Backtesting simulates how your strategy would have performed in the past. Key metrics to evaluate:</p>

<ul>
<li><strong>Win rate</strong> — Percentage of profitable trades</li>
<li><strong>Average win vs. average loss</strong> — Your risk/reward in practice</li>
<li><strong>Maximum drawdown</strong> — Largest peak-to-trough decline</li>
<li><strong>Sharpe ratio</strong> — Risk-adjusted return (higher is better)</li>
<li><strong>Profit factor</strong> — Gross profit divided by gross loss</li>
</ul>

<p>A strategy that looks great on paper might fail in live markets due to overfitting, transaction costs, or changing market conditions. Use out-of-sample testing and walk-forward analysis to validate robustness.</p>

<h2>Paper Trading: The Bridge to Live</h2>

<p>After backtesting, paper trade (simulate with real-time data but no real money) for at least 2-4 weeks. This verifies that your system works with live data feeds, handles market hours correctly, and executes as expected. Use a <a href="/tools/profit-loss-calculator">P&L calculator</a> to track your simulated results.</p>

<h2>Common Mistakes to Avoid</h2>

<ul>
<li><strong>Overfitting</strong> — Optimizing parameters to perfectly match historical data creates a system that fails on new data</li>
<li><strong>Ignoring transaction costs</strong> — Commissions, slippage, and market impact can turn a profitable backtest into a losing strategy</li>
<li><strong>No risk management</strong> — Even the best signal is worthless without proper position sizing and stop losses</li>
<li><strong>Curve fitting</strong> — Adding rules to fix specific losing trades in backtests, rather than improving the core strategy</li>
<li><strong>Starting too big</strong> — Begin with small position sizes and scale up only after proving consistency</li>
</ul>

<h2>Getting Started</h2>

<p>You don't need to build everything from scratch. Many platforms offer tools that let you define strategies without deep programming knowledge. The important thing is to start with a clear understanding of the fundamentals:</p>

<ol>
<li>Learn the basics of technical analysis and market mechanics</li>
<li>Choose a simple strategy and understand its logic thoroughly</li>
<li>Backtest rigorously with realistic assumptions</li>
<li>Paper trade for several weeks before going live</li>
<li>Start small and scale up as you build confidence</li>
</ol>

<p>The journey from manual trading to algorithmic trading is a marathon, not a sprint. Focus on building a solid foundation, and the results will follow.</p>
""",
    },
]

# ---------------------------------------------------------------------------
# Tool pages
# ---------------------------------------------------------------------------


@router.get("/tools", response_class=HTMLResponse)
async def tools_index(request: Request):
    return _get_templates().TemplateResponse(request, "viral/tools_index.html")


@router.get("/tools/position-size-calculator", response_class=HTMLResponse)
async def position_size_calculator(request: Request):
    return _get_templates().TemplateResponse(request, "viral/position_size.html")


@router.get("/tools/profit-loss-calculator", response_class=HTMLResponse)
async def profit_loss_calculator(request: Request):
    return _get_templates().TemplateResponse(request, "viral/profit_loss.html")


@router.get("/tools/risk-reward-calculator", response_class=HTMLResponse)
async def risk_reward_calculator(request: Request):
    return _get_templates().TemplateResponse(request, "viral/risk_reward.html")


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------


@router.get("/embed", response_class=HTMLResponse)
async def embed_page(request: Request):
    return _get_templates().TemplateResponse(request, "viral/embed.html")


# ---------------------------------------------------------------------------
# Blog
# ---------------------------------------------------------------------------


@router.get("/blog", response_class=HTMLResponse)
async def blog_index(request: Request):
    return _get_templates().TemplateResponse(
        request, "viral/blog_index.html", context={"articles": ARTICLES}
    )


@router.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_post(request: Request, slug: str):
    article = next((a for a in ARTICLES if a["slug"] == slug), None)
    if not article:
        return HTMLResponse(
            content="<h1>Article Not Found</h1><p><a href='/blog'>Back to Blog</a></p>",
            status_code=404,
        )
    return _get_templates().TemplateResponse(
        request, "viral/blog_post.html", context={"article": article}
    )


# ---------------------------------------------------------------------------
# SEO: sitemap & robots
# ---------------------------------------------------------------------------

_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://trade.doaide.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
  <url><loc>https://trade.doaide.com/tools</loc><changefreq>monthly</changefreq><priority>0.9</priority></url>
  <url><loc>https://trade.doaide.com/tools/position-size-calculator</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
  <url><loc>https://trade.doaide.com/tools/profit-loss-calculator</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
  <url><loc>https://trade.doaide.com/tools/risk-reward-calculator</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
  <url><loc>https://trade.doaide.com/embed</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>
  <url><loc>https://trade.doaide.com/blog</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>https://trade.doaide.com/blog/ai-trading-bots-retail-investing</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/risk-management-rules-traders</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/algorithmic-trading-beginners-guide</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
</urlset>"""


@router.get("/sitemap.xml", response_class=Response)
async def sitemap():
    return Response(content=_SITEMAP_XML.strip(), media_type="application/xml")


_ROBOTS_TXT = """User-agent: *
Allow: /
Allow: /tools/
Allow: /blog/
Allow: /embed

Sitemap: https://trade.doaide.com/sitemap.xml
"""


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return _ROBOTS_TXT.strip()
