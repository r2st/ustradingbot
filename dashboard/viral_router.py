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
        "slug": "stock-screening-tools-india",
        "title": "Best Stock Screening Tools for Indian Investors in 2026",
        "excerpt": "A comprehensive guide to free and paid stock screeners tailored for NSE and BSE, helping Indian investors filter opportunities using fundamental and technical criteria.",
        "date": "October 5, 2026",
        "read_time": "8 min read",
        "content_html": """
<p>Finding the right stocks in the Indian market can feel overwhelming. With over 5,000 companies listed on the NSE and BSE, manually sifting through quarterly results and price charts is not practical. Stock screeners solve this problem by letting you filter the entire market down to a shortlist that matches your investing criteria.</p>

<h2>What Is a Stock Screener?</h2>

<p>A stock screener is a tool that filters stocks based on criteria you define — market cap, P/E ratio, dividend yield, revenue growth, RSI, moving averages, and dozens of other parameters. Instead of reviewing thousands of companies, you get a targeted list of candidates that match your strategy.</p>

<h2>Top Free Stock Screeners for Indian Markets</h2>

<h3>1. Screener.in</h3>
<p>The most popular fundamental screener for Indian stocks. Its query language lets you write filters like <code>Market Capitalization > 10000 AND Return on equity > 15</code>. The free tier covers most use cases, and the interface is clean and fast. Best for value investors and fundamental analysts.</p>

<h3>2. Chartink</h3>
<p>Where Screener.in focuses on fundamentals, Chartink is the go-to for technical scans on Indian equities. You can build scans using candlestick patterns, volume breakouts, moving average crossovers, and custom indicator combinations. It runs scans across NSE and BSE in real time during market hours.</p>

<h3>3. Trendlyne</h3>
<p>Combines fundamental data, technicals, and analyst consensus into one platform. The "Super Screener" lets you mix financial ratios with technical signals. Their momentum scores and durability scores add a layer of analysis that raw screeners don't provide.</p>

<h3>4. Tijori Finance</h3>
<p>A newer entrant that stands out for its sector-level data and peer comparison tools. Particularly useful for analyzing companies within a specific industry — comparing margins, growth rates, and valuations across competitors.</p>

<h2>Key Screening Criteria for Indian Stocks</h2>

<h3>Fundamental Filters</h3>
<ul>
<li><strong>P/E Ratio</strong> — Compare against sector average; a P/E below the sector median may signal undervaluation</li>
<li><strong>Return on Equity (ROE)</strong> — Consistently above 15% indicates efficient capital allocation</li>
<li><strong>Debt to Equity</strong> — Below 1 for most sectors; infrastructure and banking are exceptions</li>
<li><strong>Promoter Holding</strong> — Above 50% with no recent pledging is a positive signal unique to Indian markets</li>
<li><strong>Free Cash Flow</strong> — Positive and growing FCF separates real earnings from accounting earnings</li>
</ul>

<h3>Technical Filters</h3>
<ul>
<li><strong>RSI</strong> — Below 30 for oversold opportunities, above 70 for momentum plays</li>
<li><strong>Volume Breakout</strong> — Today's volume exceeding 2x the 20-day average often precedes big moves</li>
<li><strong>Moving Average Crossover</strong> — 50-day crossing above 200-day (golden cross) signals trend reversal</li>
<li><strong>52-Week High/Low Proximity</strong> — Stocks near 52-week highs with strong fundamentals often continue higher</li>
</ul>

<h2>Building a Screening Workflow</h2>

<p>The most effective approach combines multiple screeners in a workflow:</p>

<ol>
<li><strong>Fundamental filter first</strong> — Use Screener.in to find financially sound companies (ROE > 15%, debt-to-equity < 1, positive FCF growth)</li>
<li><strong>Technical confirmation</strong> — Run the shortlist through Chartink to find ones with bullish technical setups</li>
<li><strong>Risk assessment</strong> — Use a <a href="/tools/position-size-calculator">position size calculator</a> to determine appropriate allocation</li>
<li><strong>Entry planning</strong> — Define entry, stop loss, and target using a <a href="/tools/risk-reward-calculator">risk/reward calculator</a></li>
</ol>

<h2>Common Mistakes to Avoid</h2>

<ul>
<li><strong>Over-screening</strong> — Adding too many filters leaves you with zero results; start with 3-4 key criteria</li>
<li><strong>Ignoring sector context</strong> — A banking stock's metrics look very different from an IT company's; screen within sectors</li>
<li><strong>Screening without a thesis</strong> — Know what you're looking for before you screen; the tool amplifies your strategy, not replaces it</li>
<li><strong>Skipping the qualitative check</strong> — A screener can't tell you about management quality, regulatory risk, or competitive moats; always read the annual report</li>
</ul>

<h2>The Bottom Line</h2>

<p>Stock screeners are the first step in a disciplined investment process, not the last. They help you find candidates efficiently, but the real work — understanding the business, assessing risk, and timing your entry — still requires human judgment. Start with the free tools listed above, build a consistent screening routine, and let your strategy, not the screener, drive your decisions.</p>
""",
    },
    {
        "slug": "technical-analysis-beginners-guide",
        "title": "Technical Analysis for Beginners: Charts, Patterns, and Indicators Explained",
        "excerpt": "A plain-English introduction to reading stock charts, recognizing common patterns, and using popular indicators to time your trades.",
        "date": "September 20, 2026",
        "read_time": "10 min read",
        "content_html": """
<p>Technical analysis is the study of price and volume data to forecast future price movements. Unlike fundamental analysis, which looks at a company's financials and business model, technical analysis focuses entirely on the chart — the visual record of every buy and sell decision made by the market.</p>

<h2>Why Technical Analysis Matters</h2>

<p>Price charts encode the collective psychology of every market participant — institutional funds, retail traders, algorithms, and market makers. Patterns repeat because human behavior repeats: fear, greed, hope, and panic show up as recognizable shapes on charts. Technical analysis gives you a framework to read these patterns and act on them systematically.</p>

<h2>Reading a Price Chart</h2>

<h3>Candlestick Charts</h3>
<p>The most common chart type. Each candlestick shows four prices for a given time period: open, high, low, and close. A green (or hollow) candle means the close was above the open (bullish). A red (or filled) candle means the close was below the open (bearish). The thin lines extending from the body (wicks or shadows) show the high and low extremes.</p>

<h3>Timeframes</h3>
<p>Charts can be viewed at different timeframes — 1-minute, 5-minute, hourly, daily, weekly, or monthly. Day traders focus on intraday charts (1m to 15m). Swing traders use daily and 4-hour charts. Long-term investors look at weekly and monthly charts. The same stock can look bullish on a daily chart and bearish on a weekly chart, so always check multiple timeframes.</p>

<h2>Support and Resistance</h2>

<p>The two most important concepts in technical analysis:</p>

<ul>
<li><strong>Support</strong> — A price level where buying pressure consistently prevents the price from falling further. Think of it as a floor.</li>
<li><strong>Resistance</strong> — A price level where selling pressure consistently prevents the price from rising further. Think of it as a ceiling.</li>
</ul>

<p>When price breaks through resistance, that level often becomes new support (and vice versa). These flips are called role reversals and are some of the highest-probability trade setups.</p>

<h2>Essential Chart Patterns</h2>

<h3>Trend Patterns</h3>
<ul>
<li><strong>Higher Highs and Higher Lows</strong> — Uptrend; the path of least resistance is up</li>
<li><strong>Lower Highs and Lower Lows</strong> — Downtrend; the path of least resistance is down</li>
<li><strong>Consolidation</strong> — Price moves sideways in a range; energy builds for the next move</li>
</ul>

<h3>Reversal Patterns</h3>
<ul>
<li><strong>Head and Shoulders</strong> — Three peaks, middle tallest; signals a trend reversal from up to down</li>
<li><strong>Double Bottom</strong> — Price bounces twice off the same support; signals reversal from down to up</li>
<li><strong>Engulfing Candle</strong> — A large candle completely engulfs the previous one; shows a shift in momentum</li>
</ul>

<h3>Continuation Patterns</h3>
<ul>
<li><strong>Flag / Pennant</strong> — Brief consolidation after a sharp move; price typically continues in the same direction</li>
<li><strong>Triangle</strong> — Converging trendlines showing tightening range; breakout direction is the trade</li>
</ul>

<h2>Key Technical Indicators</h2>

<h3>Moving Averages (MA)</h3>
<p>The average closing price over a period (e.g., 50-day or 200-day). Price above the moving average suggests an uptrend; below suggests a downtrend. The 50-day crossing above the 200-day (golden cross) is a widely watched bullish signal.</p>

<h3>Relative Strength Index (RSI)</h3>
<p>Measures the speed and magnitude of recent price changes on a 0-100 scale. Above 70 is considered overbought; below 30 is oversold. RSI divergences — when RSI makes a new low but price doesn't — are powerful early reversal signals.</p>

<h3>MACD (Moving Average Convergence Divergence)</h3>
<p>Shows the relationship between two moving averages. When the MACD line crosses above the signal line, it's a bullish signal. When it crosses below, it's bearish. The histogram visualizes the distance between the two lines.</p>

<h3>Volume</h3>
<p>Often overlooked but critical. A price breakout on high volume is far more reliable than one on low volume. Volume confirms price action — it tells you whether the move has conviction behind it.</p>

<h2>Putting It All Together</h2>

<p>Technical analysis works best when you combine multiple signals:</p>

<ol>
<li>Identify the trend using moving averages and price structure</li>
<li>Find key support and resistance levels</li>
<li>Wait for a pattern or setup at those levels</li>
<li>Confirm with indicators (RSI, MACD) and volume</li>
<li>Calculate your <a href="/tools/risk-reward-calculator">risk/reward ratio</a> before entering</li>
<li>Size your position using a <a href="/tools/position-size-calculator">position size calculator</a></li>
</ol>

<h2>Common Beginner Mistakes</h2>

<ul>
<li><strong>Using too many indicators</strong> — Three indicators that all say the same thing adds noise, not clarity; pick one from each category (trend, momentum, volume)</li>
<li><strong>Ignoring the higher timeframe</strong> — A buy signal on the 5-minute chart means nothing if the daily chart is in a strong downtrend</li>
<li><strong>Treating indicators as predictions</strong> — Indicators describe what HAS happened; they don't predict what WILL happen; use them as confirmation, not fortune-telling</li>
<li><strong>Skipping risk management</strong> — The best chart setup is worthless without a stop loss and proper position sizing</li>
</ul>

<h2>Start Practicing</h2>

<p>The best way to learn technical analysis is to practice on real charts without risking real money. Open a charting platform, pick a stock, and start identifying support, resistance, and patterns. Track your paper trades using our <a href="/tools/profit-loss-calculator">P&L calculator</a> and review your accuracy weekly. With consistency, you'll develop an intuition for reading charts that no textbook can teach.</p>
""",
    },
    {
        "slug": "best-free-trading-tools-2026",
        "title": "Best Free Trading Tools in 2026: Calculators, Screeners, and Analytics",
        "excerpt": "A curated roundup of the best free tools available to traders in 2026 — from position sizing calculators to portfolio analytics and market screeners.",
        "date": "September 1, 2026",
        "read_time": "7 min read",
        "content_html": """
<p>You don't need expensive software to trade well. The best traders succeed not because they have the most tools, but because they use the right ones consistently. Here's a curated list of the best free trading tools available in 2026, organized by what they help you do.</p>

<h2>Risk and Position Sizing</h2>

<h3>DoAide Trade Position Size Calculator</h3>
<p>Before every trade, you need to know how many shares to buy. Our free <a href="/tools/position-size-calculator">position size calculator</a> takes your account size, risk percentage, entry price, and stop loss to compute the exact position size. It enforces the golden rule of never risking more than 1-2% per trade — the single most important habit for long-term survival.</p>

<h3>DoAide Trade Risk/Reward Calculator</h3>
<p>Is this trade worth taking? The <a href="/tools/risk-reward-calculator">risk/reward calculator</a> shows you the ratio between potential loss and potential gain before you commit capital. If the reward isn't at least twice the risk, the math is working against you.</p>

<h3>DoAide Trade P&L Calculator</h3>
<p>Track your results with the <a href="/tools/profit-loss-calculator">profit/loss calculator</a>. Enter your entry and exit prices, position size, and it computes your net P&L including fees. Essential for maintaining an accurate trading journal.</p>

<h2>Stock Screening</h2>

<h3>Screener.in (India)</h3>
<p>The best free fundamental screener for NSE/BSE stocks. Filter by any financial metric — P/E, ROE, debt ratios, revenue growth — using a simple query language. Indispensable for Indian market investors doing bottom-up stock picking.</p>

<h3>Chartink (India)</h3>
<p>Real-time technical scans for Indian equities. Build custom scans using candlestick patterns, indicator conditions, and volume filters. The alert system notifies you when a scan triggers during market hours.</p>

<h3>Finviz (US)</h3>
<p>The standard free screener for US stocks. Its visual heat map gives you an instant read on which sectors are moving. The screener supports fundamental, technical, and descriptive filters with a clean, no-nonsense interface.</p>

<h2>Charting and Analysis</h2>

<h3>TradingView (Free Tier)</h3>
<p>The most popular charting platform worldwide. The free tier gives you access to real-time data, 100+ technical indicators, and a global community sharing ideas. Limitations: you can save up to two chart layouts and use up to three indicators per chart. For most traders, this is enough.</p>

<h3>Investing.com</h3>
<p>Free real-time quotes, charts, and an economic calendar that tracks every major data release globally. The economic calendar is particularly valuable for macro-aware traders who want to avoid entering positions before major announcements.</p>

<h2>Portfolio and Analytics</h2>

<h3>Google Sheets + Google Finance</h3>
<p>The <code>GOOGLEFINANCE</code> function pulls real-time and historical stock data directly into a spreadsheet. Combined with basic formulas, you can build a custom portfolio tracker, return calculator, and allocation monitor for free. No app subscription required.</p>

<h3>Zerodha Console (India)</h3>
<p>If you trade on Zerodha, Console provides detailed trade analytics, P&L reports, and tax statements at no additional cost. The "Tradebook" export is useful for importing trades into your own analysis tools.</p>

<h2>News and Sentiment</h2>

<h3>Pulse by Zerodha</h3>
<p>Aggregates financial news from major Indian sources in one feed. No ads, no clickbait — just headlines relevant to the markets. The widget integration within Kite makes it easy to check news for a stock you're analyzing.</p>

<h3>Finshots</h3>
<p>A free daily newsletter that explains one financial concept or market event in plain English. Not a trading tool per se, but consistently reading it builds the market literacy that makes your trading decisions better informed.</p>

<h2>How to Build Your Trading Toolkit</h2>

<p>Don't try to use everything at once. Start with three essentials:</p>

<ol>
<li><strong>A screener</strong> — to find trade candidates (Screener.in or Chartink for India, Finviz for US)</li>
<li><strong>A charting platform</strong> — to analyze candidates (TradingView free tier)</li>
<li><strong>A risk calculator</strong> — to size every trade properly (<a href="/tools/position-size-calculator">position size calculator</a>)</li>
</ol>

<p>Add more tools only when you have a specific need for them. The goal is a lean, focused workflow — not a dashboard full of apps you never open.</p>

<h2>The Takeaway</h2>

<p>The barrier to well-equipped trading has never been lower. Every tool listed here is free and accessible from a browser or phone. What separates successful traders from the rest is not the tools they use, but the discipline with which they use them. Pick your stack, build a routine, and stick to it.</p>
""",
    },
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
  <url><loc>https://trade.doaide.com/blog/stock-screening-tools-india</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/technical-analysis-beginners-guide</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/best-free-trading-tools-2026</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
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
