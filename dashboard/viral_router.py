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
        "slug": "advanced-stock-screening-strategies-nse-bse",
        "title": "Advanced Stock Screening Strategies for NSE and BSE: Finding Multibaggers in India",
        "excerpt": "Learn advanced stock screening techniques for the Indian market — combining fundamental quality filters, technical momentum signals, and promoter activity data to find high-potential multibagger stocks on NSE and BSE.",
        "date": "October 10, 2026",
        "iso_date": "2026-10-10",
        "read_time": "10 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is a multibagger stock and how do I screen for one in India?","acceptedAnswer":{"@type":"Answer","text":"A multibagger is a stock that returns several times its purchase price. To screen for potential multibaggers on NSE and BSE, look for companies with consistent revenue growth above 20% for three or more years, expanding operating margins, ROE above 20%, low debt-to-equity ratio, promoter holding above 50% with no pledging, and a market cap under ₹10,000 crore. Use Screener.in to build a custom query combining these filters. Remember that past multibaggers are easy to identify — finding future ones requires patience, conviction, and thorough due diligence beyond the screener output."}},{"@type":"Question","name":"How do I combine fundamental and technical screening for Indian stocks?","acceptedAnswer":{"@type":"Answer","text":"The most effective approach is a two-stage workflow. First, use a fundamental screener like Screener.in to identify financially healthy companies — filter for ROE above 15%, debt-to-equity below 1, positive free cash flow growth, and revenue growth above 10%. Then run that shortlist through a technical screener like Chartink to find stocks with bullish setups — price above 200-day moving average, RSI between 40 and 65, and volume above the 20-day average. This combined approach ensures you buy fundamentally strong stocks at technically favourable entry points."}},{"@type":"Question","name":"What is promoter holding analysis and why is it important for Indian stock screening?","acceptedAnswer":{"@type":"Answer","text":"Promoter holding refers to the percentage of shares owned by the company founders and controlling shareholders. In India, SEBI requires disclosure of promoter shareholding every quarter. High promoter holding (above 50%) with increasing trends signals management confidence. Conversely, declining promoter holding or pledged shares can indicate financial stress. Screener.in and Trendlyne both track promoter holding changes over time, making it easy to filter for companies where promoters are buying rather than selling."}},{"@type":"Question","name":"Can I screen for smallcap multibaggers without taking excessive risk?","acceptedAnswer":{"@type":"Answer","text":"Yes, by adding risk filters to your screen. Screen for small-cap companies (market cap ₹500-5,000 crore) with low debt (debt-to-equity below 0.3), positive free cash flow, consistent dividend payments (signals real cash generation), institutional holding above 10% (validation by professional investors), and average daily trading volume above 1 lakh shares (ensures liquidity for exit). These filters remove the riskiest small-caps while retaining companies with genuine growth potential."}},{"@type":"Question","name":"How often should I run my stock screening filters on NSE and BSE?","acceptedAnswer":{"@type":"Answer","text":"Run fundamental screens quarterly — after each earnings season when new financial data becomes available (typically May, August, November, and February for Indian companies). Run technical screens weekly for swing trading setups, or daily for short-term momentum trades. Avoid running screens during market hours for investment decisions, as intraday price fluctuations can create misleading signals. End-of-day data provides cleaner results for most screening strategies."}}]}',
        "content_html": """
<p>Basic stock screening — filtering by P/E ratio and market cap — is where every investor starts. But the Indian market rewards those who go deeper. With over 5,000 stocks listed across NSE and BSE, the difference between finding a solid blue-chip and identifying a future multibagger often comes down to the sophistication of your screening approach.</p>

<p>This guide covers advanced screening strategies that go beyond simple ratio filters, combining fundamental quality, growth momentum, promoter behaviour, and technical timing to build a systematic stock selection process for Indian equities.</p>

<h2>Beyond Basic Filters: Multi-Layer Screening</h2>

<p>A basic screen might filter for P/E below 20 and ROE above 15. While useful, this approach returns dozens of results across unrelated sectors and market caps. Advanced screening adds layers that progressively narrow the universe to truly exceptional candidates.</p>

<h3>Layer 1: Financial Quality Gate</h3>
<p>The first layer eliminates companies with weak financials:</p>
<ul>
<li><strong>Operating Cash Flow / Net Profit > 0.8</strong> — Ensures reported profits are backed by actual cash. Companies with high accrual earnings (profit on paper but no cash coming in) are higher risk</li>
<li><strong>Interest Coverage Ratio > 3</strong> — The company earns at least three times its interest payments, providing a safety margin against rising rates</li>
<li><strong>Consecutive years of positive free cash flow > 3</strong> — Eliminates companies that are consistently burning cash despite reporting profits</li>
<li><strong>Current ratio > 1.3</strong> — Adequate short-term liquidity to meet obligations without stress</li>
</ul>

<h3>Layer 2: Growth Consistency Filter</h3>
<p>Multibaggers are not built on one good quarter. Look for sustained growth:</p>
<ul>
<li><strong>Revenue CAGR (3-year) > 15%</strong> — Consistent top-line expansion, not a one-time spike</li>
<li><strong>EPS CAGR (3-year) > 18%</strong> — Earnings growing faster than revenue indicates improving margins</li>
<li><strong>No quarter with revenue decline > 10% YoY in last 8 quarters</strong> — Stability of growth trajectory matters</li>
</ul>
<p>On Screener.in, you can write custom queries to filter for these growth metrics. Trendlyne's Durability Score also captures consistency of earnings, providing a single number that aggregates multiple stability factors.</p>

<h3>Layer 3: Promoter Behaviour Analysis</h3>
<p>In the Indian market, promoter behaviour is a uniquely powerful signal. SEBI mandates quarterly disclosure of promoter shareholding, giving investors transparent data:</p>
<ul>
<li><strong>Promoter holding > 50%</strong> — Skin in the game; promoters who own a majority stake are aligned with minority shareholders</li>
<li><strong>Promoter holding increasing over 4 quarters</strong> — Promoters buying from the open market is a bullish signal; they have inside knowledge of the business trajectory</li>
<li><strong>Zero pledged shares</strong> — Pledged promoter shares can trigger forced selling during market declines, creating a downward spiral</li>
<li><strong>No reduction in promoter holding > 2% in any quarter</strong> — Large sell-downs often precede negative news</li>
</ul>
<p>Screener.in tracks promoter holding history in its shareholding section. Trendlyne offers a dedicated promoter activity filter in its Super Screener.</p>

<h2>Sector-Specific Screening Adjustments</h2>

<p>A universal screen across all sectors produces misleading comparisons. Each sector has structural differences that require adjusted parameters:</p>

<h3>Banking and Financial Services</h3>
<ul>
<li>Replace debt-to-equity with <strong>Capital Adequacy Ratio (CAR) > 14%</strong></li>
<li>Use <strong>Net Interest Margin (NIM) > 3%</strong> instead of operating margins</li>
<li>Screen for <strong>Gross NPA < 3%</strong> and <strong>Net NPA < 1%</strong> for asset quality</li>
<li><strong>Return on Assets (ROA) > 1%</strong> is excellent for banks</li>
</ul>

<h3>IT and Technology</h3>
<ul>
<li>Focus on <strong>revenue per employee growth</strong> as a productivity measure</li>
<li><strong>Dollar revenue growth > 10%</strong> captures actual business growth independent of currency movements</li>
<li>High debt-to-equity is unusual here; any significant debt is a red flag</li>
</ul>

<h3>Pharma and Healthcare</h3>
<ul>
<li>Screen for <strong>R&D spending as percentage of revenue > 5%</strong> — investment in the pipeline</li>
<li>Check <strong>ANDA filings and FDA inspection history</strong> — not screenable numerically, but critical qualitative checks</li>
<li><strong>US revenue as percentage of total > 30%</strong> filters for companies with global scale</li>
</ul>

<h2>Technical Timing Layer</h2>

<p>Once you have a fundamentally strong shortlist, add a technical layer to time your entries. The best fundamental stock bought at the wrong technical level can underperform for months. Use Chartink for Indian-specific technical screening:</p>

<h3>Breakout Accumulation Screen</h3>
<ul>
<li><strong>Price within 5% of 52-week high</strong> — Stocks near highs with strong fundamentals tend to break out further</li>
<li><strong>Volume > 1.5x 20-day SMA volume</strong> — Confirms institutional accumulation</li>
<li><strong>RSI between 55 and 70</strong> — Trending with momentum but not overextended</li>
<li><strong>ADX > 25</strong> — Confirms a trending environment rather than a choppy range</li>
</ul>

<h3>Pullback Entry Screen</h3>
<ul>
<li><strong>Price above 200-day MA</strong> — Long-term uptrend intact</li>
<li><strong>Price pulled back to 50-day MA (within 2%)</strong> — Short-term pullback offering entry in the trend</li>
<li><strong>RSI between 40 and 55</strong> — Not oversold enough to signal trend breakdown, but cooled off enough for a fresh entry</li>
</ul>

<h2>Institutional Activity Signals</h2>

<p>Tracking what institutional investors — mutual funds, FIIs, and DIIs — are doing adds a powerful validation layer:</p>
<ul>
<li><strong>Increasing mutual fund holding over 2+ quarters</strong> — Professional fund managers are accumulating</li>
<li><strong>FII holding increasing while DII holding is stable</strong> — Foreign institutional interest often precedes re-rating</li>
<li><strong>Bulk deal or block deal activity</strong> — Large transactions by institutional players signal conviction</li>
</ul>
<p>Trendlyne and StockEdge both track institutional shareholding changes and bulk/block deals. These data points are not available in basic screeners but are crucial for identifying stocks under accumulation.</p>

<h2>Building a Screening Watchlist Workflow</h2>

<ol>
<li><strong>Quarterly fundamental screen</strong> — Run after each results season. Apply the 3-layer financial quality, growth, and promoter filters. Save results as your "Universe" watchlist</li>
<li><strong>Weekly technical scan</strong> — Run the breakout or pullback screens on your Universe watchlist to identify timely entries</li>
<li><strong>Risk assessment</strong> — Use a <a href="/tools/position-size-calculator">position size calculator</a> to determine allocation based on your stop loss distance</li>
<li><strong>Entry execution</strong> — Define entry, stop loss, and target. Evaluate with a <a href="/tools/risk-reward-calculator">risk/reward calculator</a> before committing capital</li>
<li><strong>Monthly review</strong> — Check if any holdings have deteriorated on the fundamental filters; exit positions that no longer meet criteria</li>
</ol>

<h2>Tools for Advanced Screening in India</h2>

<ul>
<li><strong>Screener.in</strong> — Custom queries for fundamentals, 10-year financial history, peer comparison</li>
<li><strong>Chartink</strong> — Real-time technical scans, candlestick pattern detection, custom scan builder</li>
<li><strong>Trendlyne</strong> — Super Screener with 130+ filters, institutional activity tracking, DVM scores</li>
<li><strong>Tijori Finance</strong> — Sector-level analysis, industry comparison, capital allocation tracking</li>
<li><strong>StockEdge</strong> — Mobile-first screening, SAST deal tracking, pre-built analytical scans</li>
</ul>

<h2>Common Advanced Screening Pitfalls</h2>

<ul>
<li><strong>Survivorship bias</strong> — When you study past multibaggers, you only see the winners. Many stocks met the same criteria and still failed. Always size positions assuming any individual pick can underperform</li>
<li><strong>Over-optimising filters</strong> — Tweaking parameters to match past winners creates a screen that worked historically but has no predictive power. Use simple, robust criteria with clear logical rationale</li>
<li><strong>Ignoring valuation</strong> — A company can be excellent and still overvalued. Add a valuation check (PEG ratio, EV/EBITDA relative to historical median) as a final sanity filter</li>
<li><strong>Screening without thesis</strong> — Run screens to test a specific investment thesis, not to randomly discover stocks. The thesis guides your filter choices and keeps your portfolio coherent</li>
</ul>

<h2>The Bottom Line</h2>

<p>Advanced stock screening in India combines financial quality filters, growth consistency checks, promoter behaviour analysis, institutional activity tracking, and technical timing into a systematic, repeatable process. No single screen will find every winner, but a disciplined multi-layer approach dramatically improves your odds of identifying high-quality growth stocks before they become crowded trades. Build your screening workflow, run it consistently, and let the data — not tips or rumours — guide your stock selection.</p>
""",
    },
    {
        "slug": "sebi-fno-regulations-2026-derivatives-traders",
        "title": "SEBI F&O Regulations 2026: Complete Guide to New Futures and Options Rules for Indian Traders",
        "excerpt": "A comprehensive breakdown of SEBI's latest F&O regulations including increased lot sizes, expiry day margin rules, weekly expiry rationalisation, and how these changes impact retail derivatives traders in India.",
        "date": "October 9, 2026",
        "iso_date": "2026-10-09",
        "read_time": "11 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What are the new SEBI F&O rules for 2026?","acceptedAnswer":{"@type":"Answer","text":"SEBI introduced several major changes to F&O trading rules: minimum contract size increased to ₹15 lakh (from ₹5-10 lakh), additional margin of 2% on expiry day for short options, rationalisation of weekly expiry contracts to one per exchange, upfront collection of option premium from buyers, and mandatory risk disclosures showing that approximately 90% of individual F&O traders incur net losses. These changes aim to reduce speculative retail participation and improve market stability."}},{"@type":"Question","name":"Why did SEBI increase F&O lot sizes in India?","acceptedAnswer":{"@type":"Answer","text":"SEBI increased minimum contract sizes to ₹15 lakh to discourage small retail traders from speculative F&O trading. SEBI studies found that approximately 9 out of 10 individual traders in the F&O segment made net losses over a 3-year period, with average losses of approximately ₹2 lakh per person. By raising the capital requirement, SEBI aims to ensure that only traders with sufficient capital and risk capacity participate in derivatives trading."}},{"@type":"Question","name":"How do SEBI expiry day margin rules affect option sellers?","acceptedAnswer":{"@type":"Answer","text":"SEBI mandates an additional margin of 2% of the contract value on expiry day for short option positions. This means if you are selling Nifty options on expiry day and the contract value is ₹15 lakh, you need an additional ₹30,000 in margin on top of the regular SPAN plus exposure margin. This rule specifically targets the popular strategy of selling options on expiry day to collect premium decay, making it more capital-intensive and discouraging over-leveraged expiry day speculation."}},{"@type":"Question","name":"What is SEBI weekly expiry rationalisation?","acceptedAnswer":{"@type":"Answer","text":"SEBI directed exchanges to limit weekly expiry contracts to one benchmark index per exchange. Before this change, NSE offered weekly expiries for Nifty, Bank Nifty, and FinNifty, while BSE had Sensex and Bankex. After rationalisation, each exchange can have weekly expiry for only one index. NSE chose Nifty, BSE chose Sensex. Monthly expiries continue for all index derivatives. This reduces speculative trading volume on multiple weekly expiry days and concentrates liquidity."}},{"@type":"Question","name":"How much capital do I need for F&O trading in India after SEBI changes?","acceptedAnswer":{"@type":"Answer","text":"After SEBI increased the minimum contract size to ₹15 lakh, you need significantly more capital for F&O trading. For buying options, you need the full premium amount (typically ₹5,000 to ₹50,000 per lot depending on the strike and expiry). For selling options, you need SPAN plus exposure margin, which ranges from ₹1 lakh to ₹2 lakh per lot for index options. For futures, margins are typically 10-15% of contract value, so approximately ₹1.5 to ₹2.25 lakh per lot. A practical minimum capital for active F&O trading is ₹5 lakh to ₹10 lakh to manage risk properly."}}]}',
        "content_html": """
<p>The Securities and Exchange Board of India (SEBI) has reshaped the derivatives landscape for retail traders with a series of regulatory changes that took effect from late 2024 through 2025. These rules — covering contract sizes, margin requirements, expiry day norms, and weekly expiry rationalisation — directly impact how you trade futures and options on NSE and BSE. If you trade derivatives in India, understanding these regulations is essential for adapting your strategy and managing your capital efficiently.</p>

<p>This guide breaks down every major SEBI F&O regulation change, explains the reasoning behind each, and provides practical guidance on what it means for your trading.</p>

<h2>The Catalyst: SEBI's Retail Loss Study</h2>

<p>SEBI's regulatory overhaul was driven by a landmark study on retail participation in F&O markets. The findings were sobering:</p>
<ul>
<li>Approximately <strong>89% of individual traders in the F&O segment incurred net losses</strong> over a three-year period (FY22-FY24)</li>
<li>The average loss per individual trader was approximately <strong>₹2 lakh</strong></li>
<li>Loss-making traders paid an average of <strong>₹28,000 in transaction costs</strong> (brokerage, STT, exchange fees), compounding their losses</li>
<li>Only about <strong>11% of individual traders were net profitable</strong>, and even among them, the median profit was modest</li>
</ul>
<p>This data gave SEBI the mandate to introduce measures aimed at protecting retail investors from disproportionate losses in the derivatives segment.</p>

<h2>Increased Minimum Contract Size</h2>

<p>SEBI raised the minimum notional value of index derivative contracts to <strong>₹15 lakh</strong>, up from the previous range of ₹5-10 lakh. This was implemented through lot size adjustments:</p>

<h3>Impact on Traders</h3>
<ul>
<li><strong>Higher capital requirements</strong> — Option buyers now pay higher premiums per lot; option sellers need more margin</li>
<li><strong>Reduced over-trading</strong> — Higher contract values make it more expensive to take multiple speculative positions</li>
<li><strong>Better risk awareness</strong> — Traders are forced to think more carefully about each position when the minimum stake is higher</li>
</ul>

<p>For a Nifty futures contract at approximately 25,000 levels, the new lot size means each lot represents roughly ₹15-17 lakh in notional value. Margin requirements for a single Nifty futures lot are approximately ₹1.5-2 lakh.</p>

<h2>Expiry Day Margin Surcharge</h2>

<p>One of the most impactful changes for active options traders is the <strong>additional 2% margin on expiry day for short option positions</strong>. This specifically targets the popular strategy of selling options on the day they expire to capture rapid time decay (theta).</p>

<h3>How It Works</h3>
<ul>
<li>On the expiry day of any index options contract, an additional Extreme Loss Margin (ELM) of 2% of the contract value is added</li>
<li>For a Nifty short option position with ₹15 lakh contract value, this means approximately ₹30,000 in additional margin</li>
<li>This margin applies from the start of the expiry day, not just at the end</li>
<li>The additional margin is released on the next trading day</li>
</ul>

<h3>Strategic Implications</h3>
<p>If you sell options on expiry day, you need to account for this additional margin in your capital planning. The strategy of selling far out-of-the-money options on expiry day for small premiums becomes less attractive when the additional margin requirement is factored in — the return on capital employed drops significantly.</p>

<h2>Weekly Expiry Rationalisation</h2>

<p>SEBI directed each exchange to limit weekly expiry contracts to <strong>one benchmark index</strong>:</p>
<ul>
<li><strong>NSE</strong> retained weekly expiry for Nifty 50 (Tuesdays)</li>
<li><strong>BSE</strong> retained weekly expiry for Sensex (Fridays)</li>
<li>Bank Nifty, FinNifty (NSE), and Bankex (BSE) weekly contracts were discontinued; monthly contracts continue</li>
</ul>

<h3>Why This Matters</h3>
<p>Before rationalisation, four to five weekly expiries across NSE and BSE created what SEBI described as a "casino-like" environment. Traders could sell options expiring the same day on multiple indices throughout the week. By consolidating weekly expiries, SEBI reduced the total number of expiry days per month from roughly eight to two, significantly curbing short-term speculative volume.</p>

<p>For traders who relied on Bank Nifty weekly options, this change requires a fundamental strategy adjustment. Monthly Bank Nifty options have wider bid-ask spreads and behave differently from weekly contracts due to longer time to expiry.</p>

<h2>Upfront Premium Collection for Option Buyers</h2>

<p>SEBI mandated that brokers collect the <strong>full option premium upfront</strong> from buyers, rather than allowing them to buy options using intraday margin leverage. Previously, some brokers allowed option buying with reduced margins similar to intraday equity trades.</p>

<h3>Impact</h3>
<ul>
<li>If a Nifty call option costs ₹200 and the lot size is 75 shares, you need ₹15,000 (200 × 75) in your account to buy one lot</li>
<li>No more buying options with 2x or 3x leverage on the premium amount</li>
<li>This reduces the speculative frenzy of buying cheap far OTM options with minimal capital</li>
</ul>

<h2>Mandatory Risk Disclosure and Loss Reporting</h2>

<p>SEBI now requires brokers to display a <strong>risk disclosure on their F&O trading screens</strong> informing traders that approximately 9 out of 10 individual F&O traders incur net losses. Additionally:</p>
<ul>
<li>Brokers must show each client their <strong>individual profit and loss statement</strong> for F&O trading on a monthly basis</li>
<li>The P&L statement must include all transaction costs (brokerage, STT, stamp duty, exchange fees, GST)</li>
<li>First-time F&O traders must complete a <strong>risk assessment</strong> before being allowed to trade derivatives</li>
</ul>

<h2>Increased Securities Transaction Tax (STT) on Options</h2>

<p>While STT is governed by the Finance Act (not SEBI directly), the government increased STT on options selling from 0.0625% to <strong>0.1% of the option premium</strong> on the sell side. This change was coordinated with SEBI's broader effort to make speculative options trading more expensive:</p>
<ul>
<li>A trader selling options worth ₹1,00,000 in premium now pays ₹100 in STT (up from ₹62.50)</li>
<li>For high-frequency options sellers executing hundreds of trades, this adds up to a meaningful cost increase</li>
<li>Combined with higher margins and lot sizes, the total cost of running options-selling strategies has increased substantially</li>
</ul>

<h2>Position Limits and Monitoring</h2>

<p>SEBI enforces position limits to prevent excessive concentration in derivative contracts:</p>
<ul>
<li><strong>Client-level limits</strong> — Individual traders face position limits based on the total open interest in a contract. For index options, the client-level limit is typically 15% of total market-wide open interest</li>
<li><strong>Enhanced surveillance</strong> — SEBI and exchanges monitor positions in real time. Breaches of position limits attract penalties and forced position reduction</li>
<li><strong>Concentration margin</strong> — If a single client or group holds a disproportionate share of open interest in a contract, additional margins may be levied</li>
</ul>

<h2>Practical Adaptation Strategies for Retail F&O Traders</h2>

<h3>Capital Planning</h3>
<p>With higher lot sizes and margin requirements, proper capital allocation is critical:</p>
<ul>
<li>Maintain at least <strong>2x the margin requirement</strong> for your positions to avoid margin calls during volatility spikes</li>
<li>Use a <a href="/tools/position-size-calculator">position size calculator</a> to ensure no single F&O position risks more than 2% of your trading capital</li>
<li>Keep at least <strong>30% of your trading capital in cash</strong> as a buffer for adverse margin movements</li>
</ul>

<h3>Strategy Shifts</h3>
<ul>
<li><strong>From weekly to monthly</strong> — Shift strategies from weekly options (now limited) to monthly options with appropriate adjustments for longer time decay</li>
<li><strong>From naked selling to spreads</strong> — Credit spreads (bull put, bear call) reduce margin requirements and defined risk, making them more capital-efficient under the new regime</li>
<li><strong>Fewer, higher-quality trades</strong> — Higher costs per trade mean you need a stronger edge to remain profitable; eliminate marginal setups</li>
</ul>

<h3>Risk Management Under New Rules</h3>
<ul>
<li>Always calculate your maximum loss before entering an F&O position using a <a href="/tools/risk-reward-calculator">risk/reward calculator</a></li>
<li>Factor in <strong>all transaction costs</strong> (STT, exchange fees, GST, brokerage) when evaluating strategy profitability</li>
<li>Set a <strong>monthly loss limit</strong> — if your F&O P&L hits -5% of capital, stop trading and review your strategy</li>
</ul>

<h2>Staying Compliant: Checklist for Retail F&O Traders</h2>

<ol>
<li>Verify your broker collects upfront margins as per SEBI norms — using a non-compliant broker exposes you to regulatory risk</li>
<li>Check your monthly F&O profit and loss statement from your broker — including all transaction costs</li>
<li>Ensure your positions are within client-level position limits</li>
<li>Complete the mandatory risk assessment if you are a new F&O trader</li>
<li>Understand the tax treatment — F&O profits and losses are classified as non-speculative business income and require ITR-3 filing</li>
</ol>

<h2>The Bottom Line</h2>

<p>SEBI's F&O regulatory changes represent a deliberate effort to make derivatives trading less accessible to undercapitalised speculators and more structured for serious traders. While the higher costs and capital requirements may feel restrictive, they create a more sustainable trading environment. Adapt by focusing on capital efficiency, using defined-risk strategies like spreads, and treating transaction costs as a first-order consideration in your strategy design. The traders who thrive under the new rules will be those who treat F&O trading as a disciplined business, not a lottery.</p>
""",
    },
    {
        "slug": "best-intraday-trading-indicators-nse-vwap-ema-rsi",
        "title": "Best Intraday Trading Indicators for NSE: VWAP, EMA, and RSI Strategies Explained",
        "excerpt": "A hands-on guide to the most effective intraday trading indicators for NSE stocks — learn how to use VWAP, EMA crossovers, RSI, and volume analysis with specific setups and rules for the Indian market.",
        "date": "October 8, 2026",
        "iso_date": "2026-10-08",
        "read_time": "10 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is VWAP and how is it used in intraday trading on NSE?","acceptedAnswer":{"@type":"Answer","text":"VWAP (Volume Weighted Average Price) is the average price of a stock weighted by the volume traded at each price level throughout the trading day. It resets every day. On NSE, institutional traders use VWAP as a benchmark — buying below VWAP and selling above it. For intraday traders, VWAP acts as a dynamic support and resistance level: when price is above VWAP, the bias is bullish; when below, bearish. The most reliable intraday strategy is trading pullbacks to VWAP in the direction of the prevailing trend, entering on the first or second touch with a stop loss just beyond VWAP."}},{"@type":"Question","name":"Which EMA settings work best for intraday trading in India?","acceptedAnswer":{"@type":"Answer","text":"The most widely used EMA settings for intraday trading on NSE are the 9-period EMA (fast) and 20-period EMA (slow) on 5-minute or 15-minute charts. The 9 EMA crossing above the 20 EMA generates a buy signal, and crossing below generates a sell signal. For confirmation, add a 50-period EMA as a trend filter — only take buy signals when price is above the 50 EMA and sell signals when below. These settings balance responsiveness with reliability and work well on liquid Nifty 50 and Bank Nifty stocks."}},{"@type":"Question","name":"How do I use RSI for intraday trading without false signals?","acceptedAnswer":{"@type":"Answer","text":"To reduce false signals with RSI in intraday trading, use a 14-period RSI on 15-minute charts and do not trade RSI in isolation. Instead, use it as a confirmation filter: take a long trade only when RSI is between 40 and 65 (confirming upward momentum without being overbought), and a short trade when RSI is between 35 and 60 (confirming downward momentum without being oversold). Avoid trading when RSI is in extreme zones (above 75 or below 25) — these indicate the move may be exhausted. Combining RSI with VWAP direction gives the most reliable signals for NSE intraday trading."}},{"@type":"Question","name":"How important is volume analysis for intraday trading on NSE?","acceptedAnswer":{"@type":"Answer","text":"Volume is critical for confirming intraday price moves on NSE. A breakout above a resistance level is far more likely to sustain when volume is at least 1.5 to 2 times the 20-period volume average. Low-volume breakouts are commonly traps. For NSE stocks, the first 15 minutes after market open (9:15-9:30 AM) typically show the highest volume. Declining volume during a trend indicates weakening momentum and a potential reversal. Volume analysis is most useful as a confirmation tool — validate every entry signal with a volume check before committing capital."}},{"@type":"Question","name":"Can I combine VWAP, EMA, and RSI in a single intraday strategy?","acceptedAnswer":{"@type":"Answer","text":"Yes, and this combination is one of the most robust approaches for intraday trading on NSE. The framework: use VWAP for directional bias (price above VWAP means look for longs only), 9/20 EMA crossover for entry timing, and RSI as a momentum filter (only take longs when RSI is between 40 and 65). This three-layer confirmation reduces false signals significantly. Apply this on a 15-minute chart for Nifty 50 stocks with average daily volume above 5 lakh shares. Set stop loss at the swing low below your entry and target a 1:2 risk-reward ratio."}}]}',
        "content_html": """
<p>Technical indicators are the bread and butter of intraday trading. But with hundreds of indicators available on every charting platform, beginners often fall into the trap of loading their charts with so many overlays that they cannot make a clear decision. The reality is that successful intraday traders on NSE typically use just two or three indicators — and they use them well.</p>

<p>This guide focuses on the three most effective indicators for intraday trading on the Indian market — VWAP, EMA, and RSI — with specific setups, rules, and practical examples tailored for NSE stocks.</p>

<h2>Why These Three Indicators?</h2>

<p>Every indicator answers one of three questions: What is the trend? How strong is the momentum? Is the price at fair value? VWAP answers the fair value question, EMA answers the trend question, and RSI answers the momentum question. Together, they cover all three dimensions without redundancy.</p>

<p>Using multiple indicators that answer the same question — for example, RSI, Stochastic, and CCI (all momentum indicators) — creates confusion because they give conflicting signals during transitions. One from each category is the optimal setup.</p>

<h2>VWAP: The Institutional Benchmark</h2>

<h3>What VWAP Tells You</h3>
<p>Volume Weighted Average Price is calculated by dividing the cumulative total of price multiplied by volume by the cumulative total of volume. In simple terms, it is the average price paid by all traders throughout the day, weighted by volume. VWAP resets at the start of each trading day.</p>

<p>On NSE, institutional traders — mutual funds, FIIs, DIIs — benchmark their execution against VWAP. When a fund manager buys below VWAP, it is considered a good fill. This institutional behaviour creates a natural dynamic:</p>
<ul>
<li>When price is <strong>above VWAP</strong>, buyers are in control, and the stock is trading at a premium to the day's average</li>
<li>When price is <strong>below VWAP</strong>, sellers dominate, and the stock trades at a discount</li>
<li><strong>VWAP itself acts as a magnet</strong> — price tends to return to VWAP during the trading day, especially during low-volume periods</li>
</ul>

<h3>VWAP Trading Rules for NSE</h3>
<ol>
<li><strong>Directional bias</strong>: If price opens and stays above VWAP, look for long setups only. If below, shorts only. Do not fight the VWAP direction</li>
<li><strong>Pullback entries</strong>: The highest-probability trade is a pullback to VWAP in the direction of the trend. If price is trending above VWAP and pulls back to touch VWAP, look for a bullish reversal candle at that level to go long</li>
<li><strong>First and second touch</strong>: The first or second touch of VWAP during a trend is more reliable than the third or fourth. Multiple touches weaken the level, increasing the chance of a breakdown</li>
<li><strong>Stop loss</strong>: Place it a few points beyond VWAP on the opposite side of your trade direction</li>
</ol>

<p>VWAP is available on TradingView, Kite (Zerodha), and most Indian charting platforms. Use it on 5-minute or 15-minute candles for the best results.</p>

<h2>EMA: Trend Direction and Entry Timing</h2>

<h3>Why EMA Over SMA</h3>
<p>The Exponential Moving Average gives more weight to recent prices, making it more responsive to current price action than the Simple Moving Average. For intraday trading where speed matters, EMA reacts faster to trend changes without the lag that makes SMA sluggish on short timeframes.</p>

<h3>The 9/20 EMA Setup</h3>
<p>The 9-period and 20-period EMA combination is the most popular for intraday trading on NSE. Here is how to use it:</p>

<ul>
<li><strong>Buy signal</strong>: 9-EMA crosses above 20-EMA on a 5-minute or 15-minute chart. Enter the trade when the crossover candle closes above both EMAs</li>
<li><strong>Sell signal</strong>: 9-EMA crosses below 20-EMA. Enter short when the crossover candle closes below both EMAs</li>
<li><strong>Trend filter</strong>: Add a 50-period EMA. Only take long signals when price is above the 50-EMA; only take short signals when price is below it. This single addition eliminates most counter-trend false signals</li>
</ul>

<h3>EMA as Dynamic Support and Resistance</h3>
<p>During a strong intraday trend, the 9-EMA often acts as dynamic support (in an uptrend) or resistance (in a downtrend). Price bounces off the 9-EMA repeatedly during trending moves. When price breaks through the 9-EMA and pulls back to the 20-EMA, the trend is weakening. When it breaks the 20-EMA, the trend may be reversing.</p>

<h3>Avoiding EMA Whipsaws</h3>
<p>EMA crossovers produce false signals in choppy, sideways markets. To avoid these:</p>
<ul>
<li><strong>Wait for separation</strong> — After a crossover, wait for the 9-EMA and 20-EMA to separate by at least a few points before entering. Immediate re-crosses indicate choppiness</li>
<li><strong>Check the angle</strong> — A steep angle on both EMAs indicates a strong trend. Flat EMAs mean range-bound conditions; avoid trading crossovers when EMAs are flat</li>
<li><strong>Confirm with volume</strong> — A crossover accompanied by volume above the 20-bar average is more reliable than a low-volume crossover</li>
</ul>

<h2>RSI: Momentum Confirmation</h2>

<h3>RSI for Intraday Context</h3>
<p>The Relative Strength Index measures the speed and magnitude of recent price changes on a scale of 0 to 100. The standard 14-period RSI on a 15-minute chart is the most effective setting for intraday trading on NSE.</p>

<p>Common mistake: treating RSI 70 as an automatic sell signal and RSI 30 as an automatic buy signal. In strong trends, RSI can stay overbought or oversold for extended periods. Instead, use RSI as a <strong>confirmation filter</strong>:</p>

<h3>RSI Confirmation Rules</h3>
<ul>
<li><strong>For long entries</strong>: RSI should be between 40 and 65. Below 40 may indicate the stock is in a downtrend. Above 65 means the move may already be extended</li>
<li><strong>For short entries</strong>: RSI should be between 35 and 60. Above 60 suggests upward momentum is still present. Below 35 may indicate the stock is already oversold</li>
<li><strong>RSI divergence</strong>: When price makes a new intraday high but RSI makes a lower high, momentum is fading — a warning to take profits on longs or prepare for a reversal. The reverse applies for bearish divergences</li>
</ul>

<h3>RSI and VWAP Confluence</h3>
<p>The most powerful intraday signal occurs when RSI and VWAP align. For example: price is above VWAP (bullish bias), pulls back to touch VWAP, and RSI is between 40 and 50 (momentum has cooled but is not bearish). This confluence gives a high-probability long entry with a clear stop loss below VWAP.</p>

<h2>The Combined Framework: VWAP + EMA + RSI</h2>

<p>Here is the complete three-indicator framework for intraday trading on NSE, designed to minimise false signals while capturing genuine moves:</p>

<h3>Setup</h3>
<ul>
<li>Chart: 15-minute candles on any Nifty 50 or Nifty Next 50 stock with daily volume above 5 lakh shares</li>
<li>Indicators: VWAP, 9-EMA, 20-EMA, 14-period RSI</li>
</ul>

<h3>Long Entry Rules</h3>
<ol>
<li>Price is above VWAP (directional bias is bullish)</li>
<li>9-EMA is above 20-EMA, or a fresh bullish crossover has occurred</li>
<li>RSI is between 40 and 65</li>
<li>Volume on the entry candle is above the 20-bar average</li>
<li>Enter at the close of the candle that meets all four conditions</li>
</ol>

<h3>Short Entry Rules</h3>
<ol>
<li>Price is below VWAP (directional bias is bearish)</li>
<li>9-EMA is below 20-EMA, or a fresh bearish crossover has occurred</li>
<li>RSI is between 35 and 60</li>
<li>Volume on the entry candle is above the 20-bar average</li>
<li>Enter at the close of the candle that meets all four conditions</li>
</ol>

<h3>Exit Rules</h3>
<ul>
<li><strong>Stop loss</strong>: Below the most recent swing low (for longs) or above the most recent swing high (for shorts). Alternatively, use the opposite side of VWAP with a small buffer</li>
<li><strong>Target</strong>: Use a minimum 1:2 risk/reward ratio. If your stop is 10 points from entry, your target should be at least 20 points. Evaluate with a <a href="/tools/risk-reward-calculator">risk/reward calculator</a></li>
<li><strong>Trailing stop</strong>: Once the trade moves 1:1 in your favour, trail the stop to the 9-EMA on the 15-minute chart</li>
<li><strong>Time stop</strong>: If the trade has not reached 1:1 risk/reward within 90 minutes, exit at market — the setup has failed to follow through</li>
</ul>

<h2>Position Sizing for Intraday Trades</h2>

<p>Your position size should be determined by your stop loss distance and maximum acceptable risk — never by the leverage your broker offers. Use the <a href="/tools/position-size-calculator">position size calculator</a> with these inputs:</p>
<ul>
<li><strong>Account size</strong>: Your total intraday trading capital</li>
<li><strong>Risk per trade</strong>: 1% for beginners, up to 2% for experienced traders</li>
<li><strong>Entry price</strong>: Your planned entry</li>
<li><strong>Stop loss</strong>: The level determined by your indicator setup</li>
</ul>

<h2>Common Indicator Mistakes in Indian Intraday Trading</h2>

<ul>
<li><strong>Too many indicators</strong> — Three indicators (one each for trend, momentum, and fair value) is the maximum. Adding more creates analysis paralysis and conflicting signals</li>
<li><strong>Using daily indicator values for intraday trades</strong> — Make sure your indicators are calculated on intraday timeframes (5m, 15m). A daily RSI of 50 tells you nothing about the next 30 minutes</li>
<li><strong>Ignoring the first 15 minutes</strong> — Indicator readings during the first 15 minutes after the 9:15 AM NSE open are unreliable due to opening volatility. Wait for the opening range to establish before reading indicators</li>
<li><strong>Trading indicators during lunch hours</strong> — Between 11:30 AM and 1:30 PM, volume drops and indicators produce noisy, unreliable signals. Reduce position sizes or avoid trading during this window</li>
<li><strong>Not accounting for transaction costs</strong> — STT, exchange fees, GST, and brokerage on intraday trades reduce your net P&L. A trade with a 10-point target and 5-point stop looks like 1:2 risk/reward but may be closer to 1:1.5 after costs</li>
</ul>

<h2>The Bottom Line</h2>

<p>VWAP, EMA, and RSI — used together as a three-layer confirmation framework — provide everything an intraday trader needs to make systematic, data-driven trading decisions on NSE. The key is discipline: take only trades that pass all three filters, size your positions based on risk (not leverage), and exit when your rules say to exit. Master this framework with one stock for a month before expanding to multiple names. Consistency in execution beats complexity in indicators, every time.</p>
""",
    },
    {
        "slug": "best-stock-screener-india-nse-bse",
        "title": "Best Stock Screener for India: How to Filter NSE & BSE Stocks Like a Pro",
        "excerpt": "A detailed comparison of the best stock screeners for Indian markets, with practical screening strategies for NSE and BSE stocks using fundamental and technical filters.",
        "date": "October 10, 2026",
        "iso_date": "2026-10-10",
        "read_time": "9 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is the best free stock screener for Indian markets?","acceptedAnswer":{"@type":"Answer","text":"Screener.in is widely considered the best free fundamental stock screener for Indian markets. It covers all NSE and BSE listed companies and supports a custom query language for filtering by financial ratios, growth metrics, and valuation parameters. For technical screening, Chartink is the top free choice with real-time scan capabilities during market hours."}},{"@type":"Question","name":"How do I screen stocks on NSE and BSE?","acceptedAnswer":{"@type":"Answer","text":"To screen stocks on NSE and BSE, use a screener like Screener.in or Chartink. Define your criteria — for example, market cap above ₹5,000 crore, ROE above 15%, debt-to-equity below 1, and positive free cash flow. Apply the filters and review the shortlist. Then validate candidates with technical analysis using charts on TradingView or Chartink before making investment decisions."}},{"@type":"Question","name":"Can I screen for stocks based on SEBI mutual fund categories?","acceptedAnswer":{"@type":"Answer","text":"Yes. SEBI classifies stocks into large-cap (top 100 by market cap), mid-cap (101-250), and small-cap (251 onwards). Most Indian screeners let you filter by market capitalization ranges that correspond to these SEBI categories. Screener.in and Trendlyne both support market cap filters that align with SEBI classifications."}},{"@type":"Question","name":"What fundamental ratios should I use to screen Indian stocks?","acceptedAnswer":{"@type":"Answer","text":"Key fundamental ratios for screening Indian stocks include: P/E ratio (compare with sector average), Return on Equity above 15%, Debt-to-Equity below 1 (except banks and infrastructure), Promoter Holding above 50% with no pledging, positive and growing Free Cash Flow, and Sales Growth above 10% year-over-year. Always screen within a sector for meaningful comparisons."}},{"@type":"Question","name":"Is Chartink better than Screener.in?","acceptedAnswer":{"@type":"Answer","text":"They serve different purposes. Screener.in excels at fundamental analysis — financial ratios, quarterly results, and long-term performance metrics. Chartink specialises in technical analysis — candlestick patterns, indicator-based scans, and real-time alerts. The best approach is to use both: filter fundamentally strong companies on Screener.in, then check technical entry timing on Chartink."}}]}',
        "content_html": """
<p>With over 5,000 companies listed across the National Stock Exchange (NSE) and Bombay Stock Exchange (BSE), finding the right stocks to invest in without a screener is like searching for a needle in a haystack. A stock screener lets you set precise filters — market capitalisation, P/E ratio, return on equity, promoter holding, technical indicators — and instantly narrows the universe to a shortlist that fits your strategy.</p>

<p>This guide compares the best stock screeners available for Indian investors in 2026, walks you through practical screening strategies, and shows you how to build a repeatable workflow for finding high-quality stocks on NSE and BSE.</p>

<h2>Why You Need a Stock Screener for Indian Markets</h2>

<p>The Indian equity market is one of the most diverse in the world. You have blue-chip Nifty 50 companies trading alongside thousands of small-caps and micro-caps. Quarterly results, corporate actions, promoter transactions, and SEBI circulars create a constant flow of data that no human can process manually.</p>

<p>A screener automates the first stage of your research. Instead of reading through hundreds of annual reports, you define what "good" looks like — say, ROE above 15%, debt-to-equity below 0.5, and sales growth above 10% — and the screener delivers a focused list in seconds. You spend your time analysing companies, not finding them.</p>

<h2>Top Stock Screeners for Indian Investors</h2>

<h3>1. Screener.in — Best for Fundamental Analysis</h3>

<p>Screener.in remains the gold standard for fundamental screening in India. Its custom query language is powerful yet intuitive — you can write queries like <code>Market Capitalization > 10000 AND Return on equity > 15 AND Debt to equity < 1</code>. The platform pulls data directly from BSE and NSE filings, covers every listed company, and provides 10-year financial history for each stock.</p>

<p>Strengths: clean interface, custom queries, peer comparison, export to Excel, free for most features. The premium tier adds watchlist alerts and concall transcripts.</p>

<h3>2. Chartink — Best for Technical Scans</h3>

<p>If your strategy relies on technical analysis, Chartink is indispensable. It lets you build scans using candlestick patterns (hammer, engulfing, doji), indicator conditions (RSI below 30, MACD crossover), volume filters (volume 2x above 20-day SMA), and price action triggers (52-week high breakout). Scans run in real time during market hours, and you can set alerts that notify you when a scan triggers.</p>

<p>Strengths: real-time scanning, massive library of pre-built scans, custom scan builder, intraday and end-of-day modes.</p>

<h3>3. Trendlyne — Best for Combined Analysis</h3>

<p>Trendlyne blends fundamental data, technical signals, and analyst consensus into a single platform. Its "Super Screener" supports 130+ filters spanning financials, technicals, and forecasts. Unique features include Durability Score (financial stability), Momentum Score (price trend strength), and Valuation Score. The Forecaster section aggregates analyst target prices and recommendations.</p>

<h3>4. Tijori Finance — Best for Sector Research</h3>

<p>Tijori Finance stands out for its sector-level analysis. It groups companies by industry and lets you compare margins, growth rates, valuations, and capital allocation across peers. If you prefer a top-down approach — picking the sector first and then finding the best company within it — Tijori is designed for your workflow.</p>

<h3>5. StockEdge — Best Mobile Screener</h3>

<p>For traders who prefer screening on mobile, StockEdge offers a well-designed app with pre-built scans, technical and fundamental filters, and daily updates on insider trades, bulk deals, and SAST disclosures. The free tier is functional; premium adds advanced scans and strategy back-testing.</p>

<h2>Practical Screening Strategies for Indian Stocks</h2>

<h3>Quality Value Screen</h3>
<p>Find undervalued companies with strong fundamentals:</p>
<ul>
<li>ROE > 15% consistently for 3 years</li>
<li>Debt to Equity < 0.5</li>
<li>P/E ratio below sector median</li>
<li>Promoter holding > 50%, no pledging</li>
<li>Positive free cash flow for 3 consecutive years</li>
</ul>

<h3>Growth Momentum Screen</h3>
<p>Identify fast-growing companies with price momentum:</p>
<ul>
<li>Revenue growth > 20% YoY for 2 years</li>
<li>EPS growth > 15% YoY</li>
<li>Price above 200-day moving average</li>
<li>RSI between 50 and 70 (trending but not overbought)</li>
<li>Market cap > ₹2,000 crore (excludes illiquid micro-caps)</li>
</ul>

<h3>Dividend Income Screen</h3>
<p>Build a portfolio of reliable dividend payers:</p>
<ul>
<li>Dividend yield > 2%</li>
<li>Dividend payout ratio between 20% and 60%</li>
<li>No dividend cut in the last 5 years</li>
<li>ROE > 12%</li>
<li>Debt to Equity < 1</li>
</ul>

<h2>SEBI Categories and Market Cap Filters</h2>

<p>SEBI mandates specific market capitalisation categories for mutual fund classification:</p>
<ul>
<li><strong>Large Cap</strong> — Top 100 companies by full market capitalisation</li>
<li><strong>Mid Cap</strong> — Companies ranked 101 to 250</li>
<li><strong>Small Cap</strong> — Companies ranked 251 and below</li>
</ul>

<p>Most screeners let you filter by these ranges. Screening within a SEBI category ensures you compare like with like — a small-cap's P/E should be compared to other small-caps, not to Nifty 50 giants.</p>

<h2>Building Your Screening Workflow</h2>

<ol>
<li><strong>Fundamental filter</strong> — Use Screener.in to find financially healthy companies matching your criteria</li>
<li><strong>Technical timing</strong> — Run the shortlist through Chartink to identify stocks with bullish chart setups</li>
<li><strong>Sector context</strong> — Check Tijori or Trendlyne for peer comparison within the sector</li>
<li><strong>Risk sizing</strong> — Use a <a href="/tools/position-size-calculator">position size calculator</a> to determine appropriate allocation</li>
<li><strong>Entry planning</strong> — Define entry, stop loss, and target with a <a href="/tools/risk-reward-calculator">risk/reward calculator</a></li>
</ol>

<h2>Common Screening Mistakes to Avoid</h2>

<ul>
<li><strong>Too many filters</strong> — Start with 4-5 key criteria; over-filtering returns zero results</li>
<li><strong>Ignoring sector context</strong> — A bank's debt-to-equity is structurally different from an FMCG company's</li>
<li><strong>Screening across all market caps</strong> — Always filter within a market cap range for meaningful comparisons</li>
<li><strong>Treating screener output as buy signals</strong> — A screener finds candidates; due diligence confirms them</li>
<li><strong>Not re-running screens regularly</strong> — Markets change; re-run your screens weekly or after quarterly results</li>
</ul>

<h2>The Bottom Line</h2>

<p>A stock screener is the most efficient first step in any investment process. It replaces hours of manual research with seconds of systematic filtering. Start with Screener.in for fundamentals and Chartink for technicals — both are free and cover the entire Indian market. Build a consistent screening routine, validate with your own research, and let your strategy guide your decisions.</p>
""",
    },
    {
        "slug": "sebi-regulations-retail-traders-india-2026",
        "title": "SEBI Regulations Every Retail Trader in India Must Know in 2026",
        "excerpt": "A practical guide to SEBI rules that directly affect retail traders and investors — from margin requirements and peak margin norms to insider trading laws and investor protection measures.",
        "date": "October 8, 2026",
        "iso_date": "2026-10-08",
        "read_time": "10 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is SEBI and what does it do?","acceptedAnswer":{"@type":"Answer","text":"SEBI (Securities and Exchange Board of India) is the statutory regulatory authority for the Indian securities market, established under the SEBI Act of 1992. It regulates stock exchanges (NSE, BSE), brokers, mutual funds, portfolio managers, and other market participants. Its primary objectives are protecting investor interests, promoting market development, and regulating the securities market."}},{"@type":"Question","name":"What are SEBI peak margin rules for retail traders?","acceptedAnswer":{"@type":"Answer","text":"SEBI peak margin norms require brokers to collect upfront margins from traders before executing trades. For intraday equity trades, the minimum margin is typically 20% of the trade value (5x leverage). For F&O trades, SEBI-mandated SPAN plus exposure margins apply. Brokers must report peak margin utilisation to the exchange, and shortfalls attract penalties. These rules were fully implemented from September 2021 onwards."}},{"@type":"Question","name":"Can retail traders do intraday trading under SEBI rules?","acceptedAnswer":{"@type":"Answer","text":"Yes, retail traders can do intraday trading under SEBI rules. However, SEBI has progressively tightened intraday leverage through peak margin norms. Brokers can offer a maximum of approximately 5x leverage for intraday equity trades (20% margin requirement). For F&O, the leverage depends on the contract-specific SPAN margin set by the exchange. Traders must also comply with position limits and are subject to Securities Transaction Tax (STT) on sell-side intraday transactions."}},{"@type":"Question","name":"What is SEBI circular on stock broker segregation of client funds?","acceptedAnswer":{"@type":"Answer","text":"SEBI mandates strict segregation of client funds from broker proprietary funds. Brokers must maintain separate bank accounts and demat accounts for client securities. They cannot use one client funds to meet another clients obligations. SEBI also requires running account settlement — brokers must return unused client funds quarterly (or monthly if the client opts in). These rules protect retail investors if a broker faces financial difficulty."}},{"@type":"Question","name":"How does SEBI protect retail investors from fraud?","acceptedAnswer":{"@type":"Answer","text":"SEBI protects retail investors through multiple mechanisms: mandatory KYC verification, SCORES (SEBI Complaints Redress System) for complaint resolution, prohibition of insider trading under PIT Regulations, surveillance systems to detect market manipulation, mandatory risk disclosures by brokers, investor education programs, and the Investor Protection Fund maintained by stock exchanges. SEBI also conducts regular inspections of brokers and intermediaries."}}]}',
        "content_html": """
<p>The Securities and Exchange Board of India (SEBI) regulates every aspect of the Indian securities market — from how brokers handle your money to what leverage you can use for intraday trades. As a retail trader or investor, understanding SEBI regulations is not optional. These rules directly affect your trading costs, available leverage, tax obligations, and the protections you have if something goes wrong.</p>

<p>This guide covers the SEBI regulations that matter most to retail participants in 2026, explained in plain language with practical implications for your trading.</p>

<h2>What Is SEBI and Why Does It Matter?</h2>

<p>SEBI was established under the SEBI Act of 1992 as the statutory regulator for the Indian securities market. It oversees stock exchanges (NSE, BSE, MCX), brokers, mutual funds, portfolio managers, credit rating agencies, and other market intermediaries. SEBI's three core objectives are:</p>

<ul>
<li><strong>Investor protection</strong> — Ensuring fair treatment and preventing fraud</li>
<li><strong>Market development</strong> — Promoting efficient and transparent markets</li>
<li><strong>Market regulation</strong> — Creating and enforcing rules for all participants</li>
</ul>

<p>Every rule SEBI introduces affects how you trade, what fees you pay, and how your money is safeguarded. Ignoring these regulations can result in penalties, frozen accounts, or legal liability.</p>

<h2>Margin Requirements and Leverage Rules</h2>

<h3>Peak Margin Norms</h3>

<p>SEBI's peak margin framework, fully effective since September 2021, fundamentally changed how leverage works for retail traders. Under these norms:</p>

<ul>
<li>Brokers must collect upfront margins before executing trades</li>
<li>Exchanges check margin utilisation at multiple random snapshots during the trading day</li>
<li>The highest margin utilisation (peak margin) is reported; shortfalls attract penalties</li>
<li>For intraday equity trades, the effective margin requirement is approximately 20%, translating to roughly 5x leverage</li>
</ul>

<p>Before these norms, some brokers offered 20x or even 40x intraday leverage. The reduction was designed to protect retail traders from catastrophic losses during volatile sessions.</p>

<h3>F&O Margin Requirements</h3>

<p>For futures and options trading, SEBI mandates SPAN (Standard Portfolio Analysis of Risk) plus exposure margins. These vary by contract and are recalculated daily. Recent SEBI changes also require option sellers to maintain margins that account for extreme market moves, significantly increasing capital requirements for short option strategies.</p>

<p>SEBI also increased the minimum contract size for index derivatives to ₹15 lakh (from ₹5-10 lakh previously), making F&O trading more capital-intensive for retail participants.</p>

<h2>Client Fund Segregation</h2>

<p>One of SEBI's most important investor protection measures is the mandatory segregation of client funds:</p>

<ul>
<li><strong>Separate bank accounts</strong> — Broker must maintain distinct accounts for client funds and proprietary funds</li>
<li><strong>Separate demat accounts</strong> — Client securities are held separately from broker securities</li>
<li><strong>No co-mingling</strong> — Brokers cannot use one client's funds for another client's obligations</li>
<li><strong>Running account settlement</strong> — Unused client funds must be returned quarterly (or monthly if the client opts for it)</li>
</ul>

<p>These rules exist because of past incidents where broker defaults led to retail investor losses. If your broker has excess funds in your trading account, they are legally required to return them periodically.</p>

<h2>Insider Trading Regulations (PIT Regulations)</h2>

<p>SEBI's Prohibition of Insider Trading (PIT) Regulations affect retail investors more than most realise:</p>

<ul>
<li><strong>Unpublished Price Sensitive Information (UPSI)</strong> — If you receive non-public information about a company (from a friend who works there, a tip on social media from an insider), trading on it is illegal</li>
<li><strong>Connected persons</strong> — Family members and close associates of company insiders are presumed to have access to UPSI</li>
<li><strong>Penalties</strong> — Insider trading penalties can reach ₹25 crore or three times the profit made, whichever is higher, plus potential imprisonment</li>
</ul>

<p>The practical takeaway: if someone gives you a "sure shot tip" claiming inside knowledge, trading on it is a criminal offence under Indian law.</p>

<h2>Investor Protection Mechanisms</h2>

<h3>SCORES — SEBI Complaints Redress System</h3>
<p>If you have a complaint against your broker, mutual fund, or any SEBI-registered intermediary, SCORES is the official platform. File your complaint at scores.sebi.gov.in. SEBI mandates that intermediaries resolve complaints within 30 days. Unresolved complaints are escalated by SEBI directly.</p>

<h3>Investor Protection Fund (IPF)</h3>
<p>Both NSE and BSE maintain Investor Protection Funds. If a broker defaults and client assets cannot be recovered through normal channels, the IPF provides compensation to affected investors up to a maximum limit (currently ₹25 lakh per investor per broker default at NSE).</p>

<h3>Basic Services Demat Account (BSDA)</h3>
<p>For small investors, SEBI mandates a Basic Services Demat Account with reduced annual maintenance charges. If your demat holding is below ₹10 lakh, you are eligible for lower charges — ask your depository participant about BSDA eligibility.</p>

<h2>Trading Rules That Affect Your Strategy</h2>

<h3>T+1 Settlement</h3>
<p>India moved to T+1 (trade date plus one business day) settlement for equity trades. When you buy shares, they are credited to your demat account the next business day. When you sell, proceeds are available the next day. This is among the fastest settlement cycles globally and reduces counterparty risk.</p>

<h3>Circuit Limits</h3>
<p>SEBI and stock exchanges impose circuit limits (price bands) on individual stocks — typically 2%, 5%, 10%, or 20% depending on the stock's volatility history. Index-level circuit breakers halt trading across the market at 10%, 15%, and 20% declines from the previous close. These limits prevent flash crashes and give the market time to absorb information.</p>

<h3>Short Selling Rules</h3>
<p>Retail investors can short sell in the Indian market, but must disclose the short position at the time of placing the order. Naked short selling (selling without borrowing the shares) is not permitted for delivery-based trades — you must either square off intraday or arrange for borrowing through the Securities Lending and Borrowing Mechanism (SLBM).</p>

<h2>Tax Implications Linked to SEBI Rules</h2>

<p>While taxation is governed by the Income Tax Act, SEBI's market structure directly affects your tax obligations:</p>

<ul>
<li><strong>Securities Transaction Tax (STT)</strong> — Levied on all exchange-traded transactions; rates differ for delivery, intraday, futures, and options</li>
<li><strong>Classification of income</strong> — SEBI's framework influences whether your trading income is classified as business income or capital gains</li>
<li><strong>Reporting requirements</strong> — Brokers report your transaction data to tax authorities; ensure your ITR matches broker statements</li>
</ul>

<h2>Recent SEBI Changes Retail Traders Should Know</h2>

<ul>
<li><strong>Increased F&O lot sizes</strong> — Minimum contract value raised to ₹15 lakh, increasing capital requirements</li>
<li><strong>Expiry day restrictions</strong> — SEBI has implemented additional margin requirements on expiry days to curb speculative activity</li>
<li><strong>Mutual fund fee transparency</strong> — Total Expense Ratio (TER) caps reduced, benefiting SIP investors</li>
<li><strong>Nomination requirements</strong> — SEBI mandates nomination for all demat and trading accounts; accounts without nomination face restrictions</li>
<li><strong>KYC centralisation</strong> — KRA (KYC Registration Agency) process streamlined for faster account opening</li>
</ul>

<h2>How to Stay Updated on SEBI Regulations</h2>

<ol>
<li><strong>SEBI website</strong> — sebi.gov.in publishes all circulars, orders, and consultation papers</li>
<li><strong>Exchange circulars</strong> — NSE and BSE issue implementation circulars after SEBI directives</li>
<li><strong>Broker notifications</strong> — Reputable brokers communicate regulatory changes through email and app notifications</li>
<li><strong>Financial news</strong> — Follow Finshots, ET Markets, or Moneycontrol for plain-language regulatory updates</li>
</ol>

<h2>The Bottom Line</h2>

<p>SEBI regulations exist to make the Indian market safer and more transparent for retail participants. While some rules — like reduced leverage — may feel restrictive, they protect you from the catastrophic losses that wiped out traders in less regulated environments. Understand the rules, trade within them, and use the investor protection mechanisms available to you. A well-regulated market is ultimately a better market for long-term wealth creation.</p>
""",
    },
    {
        "slug": "intraday-trading-tips-strategies-india",
        "title": "Intraday Trading Tips and Strategies for Indian Stock Market Beginners",
        "excerpt": "Practical intraday trading strategies for NSE and BSE, covering entry timing, indicator setups, risk management, and common mistakes that beginners make in the Indian market.",
        "date": "October 6, 2026",
        "iso_date": "2026-10-06",
        "read_time": "11 min read",
        "faq_json_ld": '{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is intraday trading in the Indian stock market?","acceptedAnswer":{"@type":"Answer","text":"Intraday trading means buying and selling stocks within the same trading day on NSE or BSE. All positions are squared off before market close at 3:30 PM IST. If you do not close your position, your broker will auto-square it off. Intraday trades are marked as MIS (Margin Intraday Square-off) on most Indian brokers. Profits from intraday trading are classified as speculative business income under Indian tax law."}},{"@type":"Question","name":"What is the best time for intraday trading in India?","acceptedAnswer":{"@type":"Answer","text":"The best times for intraday trading on NSE/BSE are the first hour (9:15 AM to 10:15 AM) when volatility and volume are highest due to overnight gap reactions and fresh order flow, and the last hour (2:30 PM to 3:30 PM) when institutional positioning creates clear directional moves. The period between 11:30 AM and 1:30 PM is typically low-volume and choppy — experienced traders often avoid initiating new positions during this window."}},{"@type":"Question","name":"How much money do I need to start intraday trading in India?","acceptedAnswer":{"@type":"Answer","text":"Under SEBI peak margin norms, intraday equity trading requires approximately 20% margin (roughly 5x leverage). To trade a stock priced at ₹1,000 per share with 100 shares (₹1,00,000 trade value), you need approximately ₹20,000 in margin. However, a practical starting capital for intraday trading is ₹50,000 to ₹1,00,000 — this gives you enough room to manage risk properly with the 1-2% per-trade risk rule."}},{"@type":"Question","name":"What indicators are best for intraday trading in India?","acceptedAnswer":{"@type":"Answer","text":"The most effective indicators for intraday trading on NSE/BSE are: VWAP (Volume Weighted Average Price) for identifying fair value and trend direction, 9-EMA and 20-EMA for short-term trend following, RSI (14-period) for overbought/oversold conditions, and volume bars for confirming breakouts. Use 5-minute or 15-minute candle timeframes. Avoid using more than 3 indicators simultaneously — they create conflicting signals."}},{"@type":"Question","name":"Is intraday trading profitable in India?","acceptedAnswer":{"@type":"Answer","text":"SEBI data shows that approximately 90% of individual intraday traders in the F&O segment incur net losses. However, the remaining 10% who are consistently profitable share common traits: strict risk management (never risking more than 1-2% per trade), a tested strategy with a defined edge, emotional discipline, and low trading costs. Intraday trading can be profitable but requires significant skill development, practice, and capital management."}}]}',
        "content_html": """
<p>Intraday trading — buying and selling stocks within the same trading session — is one of the most popular forms of trading on the NSE and BSE. The appeal is obvious: you can profit from small price movements without overnight risk, and leverage amplifies your returns. But the reality is that most beginners lose money because they trade without a strategy, ignore risk management, or let emotions drive their decisions.</p>

<p>This guide provides practical, actionable intraday trading strategies tailored for the Indian market, covering everything from the best times to trade to specific indicator setups and risk management rules.</p>

<h2>Understanding Intraday Trading in India</h2>

<p>When you place an intraday trade on an Indian broker (Zerodha, Groww, Angel One, or any other SEBI-registered broker), you select MIS (Margin Intraday Square-off) as the product type. This tells the broker that you intend to close the position before the market closes at 3:30 PM IST. If you don't, the broker auto-squares off your position — usually around 3:15-3:20 PM.</p>

<p>Under SEBI's peak margin norms, the maximum intraday leverage for equity trades is approximately 5x (20% margin requirement). For a ₹1,00,000 trade, you need ₹20,000 in your trading account. This is a significant reduction from the 20-40x leverage that was available before SEBI tightened the rules.</p>

<h2>Best Times for Intraday Trading on NSE/BSE</h2>

<h3>Opening Hour: 9:15 AM to 10:15 AM</h3>
<p>The first hour is the most volatile and liquid period of the trading day. Overnight news, global market cues (US markets close at 7:00 AM IST), and pre-market order imbalances create sharp moves at the open. This is when most professional intraday traders make the bulk of their daily P&L.</p>

<p>Strategy for the opening hour: wait for the first 15 minutes to establish the opening range (OR), then trade breakouts above or below this range with confirmation from volume.</p>

<h3>Mid-Day: 11:30 AM to 1:30 PM</h3>
<p>This is typically a low-volume, choppy period. Spreads widen, false breakouts increase, and trending moves stall. Most experienced intraday traders reduce activity or avoid trading entirely during this window. If you have open positions, this is a good time to manage them rather than initiate new ones.</p>

<h3>Closing Hour: 2:30 PM to 3:30 PM</h3>
<p>Volume picks up again as institutional traders and mutual funds execute their orders. The last hour often produces strong directional moves, especially on expiry days. However, avoid holding positions into the last 10 minutes — auto-square-off mechanisms can cause unfavourable fills.</p>

<h2>Intraday Trading Strategies for Indian Markets</h2>

<h3>1. VWAP Pullback Strategy</h3>

<p>VWAP (Volume Weighted Average Price) is the most important indicator for intraday trading. It represents the average price weighted by volume — essentially, the "fair value" for the day.</p>

<ul>
<li><strong>Bullish setup</strong>: Price is above VWAP. Wait for a pullback to touch or slightly dip below VWAP, then enter long when a bullish candle closes above VWAP with increasing volume.</li>
<li><strong>Bearish setup</strong>: Price is below VWAP. Wait for a rally back to VWAP, then enter short when a bearish candle closes below VWAP.</li>
<li><strong>Stop loss</strong>: Place it a few points beyond the pullback's extreme.</li>
<li><strong>Target</strong>: The day's high (for longs) or low (for shorts), or a 1:2 risk-reward ratio.</li>
</ul>

<h3>2. Opening Range Breakout (ORB)</h3>

<p>One of the most reliable intraday strategies, particularly effective on Nifty 50 and Bank Nifty stocks:</p>

<ol>
<li>Mark the high and low of the first 15 or 30 minutes after market open (the opening range)</li>
<li>If price breaks above the opening range high with volume, go long</li>
<li>If price breaks below the opening range low with volume, go short</li>
<li>Stop loss: the opposite end of the opening range</li>
<li>Target: the width of the opening range projected from the breakout point (measured move)</li>
</ol>

<p>Use the <a href="/tools/risk-reward-calculator">risk/reward calculator</a> to evaluate each ORB setup before entering.</p>

<h3>3. EMA Crossover with RSI Filter</h3>

<p>This strategy combines trend direction (moving averages) with momentum confirmation (RSI):</p>

<ul>
<li>Use 9-EMA and 20-EMA on a 5-minute or 15-minute chart</li>
<li><strong>Buy signal</strong>: 9-EMA crosses above 20-EMA AND RSI is above 50 (but below 70)</li>
<li><strong>Sell signal</strong>: 9-EMA crosses below 20-EMA AND RSI is below 50 (but above 30)</li>
<li>Avoid taking crossover signals when RSI is in extreme zones (above 70 or below 30) — the move may already be exhausted</li>
</ul>

<h3>4. Support/Resistance Breakout with Volume</h3>

<p>Identify key support and resistance levels from the previous day's chart or from the current day's price action:</p>

<ul>
<li>Previous day's high, low, and close are natural reference points</li>
<li>Intraday pivot points (calculated from previous day's HLC) serve as support/resistance</li>
<li>Enter only when price breaks the level with volume at least 1.5x the 20-bar average</li>
<li>Low-volume breakouts are traps — avoid them</li>
</ul>

<h2>Risk Management for Intraday Trading</h2>

<p>Risk management is the difference between traders who survive and those who blow up their accounts. Follow these rules without exception:</p>

<h3>The 1% Rule</h3>
<p>Never risk more than 1% of your trading capital on a single intraday trade. With a ₹1,00,000 account, your maximum loss per trade is ₹1,000. Use our <a href="/tools/position-size-calculator">position size calculator</a> to calculate the exact number of shares based on your entry price and stop loss.</p>

<h3>Daily Loss Limit</h3>
<p>Set a maximum daily loss limit — typically 2-3% of your capital. If you hit this limit, stop trading for the day. Continuing to trade after hitting your loss limit leads to revenge trading and compounding losses.</p>

<h3>Position Sizing</h3>
<p>Your position size should be determined by your stop loss distance and risk per trade — never by how much leverage your broker offers. The formula:</p>

<p><strong>Shares = (Account Risk Amount) ÷ (Entry Price – Stop Loss Price)</strong></p>

<p>If your account is ₹1,00,000, risk per trade is 1% (₹1,000), entry is ₹500, and stop loss is ₹490, you buy 100 shares (₹1,000 ÷ ₹10 = 100). Not 200, not 500 — exactly 100.</p>

<h2>Choosing Stocks for Intraday Trading</h2>

<p>Not every stock is suitable for intraday trading. Look for:</p>

<ul>
<li><strong>High liquidity</strong> — Average daily volume above 5 lakh shares; Nifty 50 and Nifty Next 50 components are ideal</li>
<li><strong>Adequate volatility</strong> — Average daily range of 1.5-3%; too little movement means no opportunity, too much means hard-to-manage risk</li>
<li><strong>Tight bid-ask spread</strong> — Wider spreads eat into your profits; stick to heavily traded names</li>
<li><strong>News catalyst</strong> — Stocks with earnings, upgrades/downgrades, or sector news tend to move more directionally</li>
</ul>

<h2>Common Mistakes Indian Intraday Traders Make</h2>

<ul>
<li><strong>Trading without a stop loss</strong> — "It will come back" is the most expensive sentence in trading; always define your exit before entry</li>
<li><strong>Averaging down on losing positions</strong> — Adding to a losing intraday trade doubles your risk; cut the loss and move on</li>
<li><strong>Overtrading</strong> — Taking 15-20 trades per day generates commissions and STT that erode your edge; 2-5 quality trades are enough</li>
<li><strong>Ignoring brokerage and tax costs</strong> — STT, stamp duty, GST, exchange fees, and brokerage can total 0.05-0.1% per trade; factor these into your strategy</li>
<li><strong>Trading during low-volume hours</strong> — The mid-day lull produces choppy, unpredictable price action; avoid it</li>
<li><strong>Not maintaining a trading journal</strong> — Track every trade using a <a href="/tools/profit-loss-calculator">P&L calculator</a> and review weekly</li>
</ul>

<h2>Tax Implications of Intraday Trading in India</h2>

<p>Intraday trading profits are classified as <strong>speculative business income</strong> under Indian income tax law. Key points:</p>

<ul>
<li>Taxed at your applicable income tax slab rate</li>
<li>Speculative losses can only be set off against speculative gains (not against salary or other income)</li>
<li>Speculative losses can be carried forward for 4 assessment years</li>
<li>Audit under Section 44AD/44AB may apply if turnover exceeds ₹2 crore (turnover = absolute sum of profits and losses)</li>
<li>Maintain proper books of accounts; your broker's contract notes serve as primary records</li>
</ul>

<h2>Getting Started: A Practical Checklist</h2>

<ol>
<li>Open a trading account with a SEBI-registered discount broker (lower brokerage preserves your edge)</li>
<li>Fund your account with capital you can afford to lose — start with ₹50,000 to ₹1,00,000</li>
<li>Paper trade for at least 2-4 weeks using one strategy from this guide</li>
<li>Track results religiously — win rate, average win/loss, and maximum drawdown</li>
<li>Go live with minimal position sizes; scale up only after 50+ live trades show consistent results</li>
<li>Review your journal weekly and refine your strategy based on data, not feelings</li>
</ol>

<h2>The Bottom Line</h2>

<p>Intraday trading in the Indian stock market offers real opportunity, but it demands discipline, preparation, and continuous learning. Start with one strategy, master it on paper, manage your risk ruthlessly, and treat it as a skill to develop over months — not a shortcut to quick profits. The traders who succeed are the ones who respect the process.</p>
""",
    },
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
  <url><loc>https://trade.doaide.com/blog/advanced-stock-screening-strategies-nse-bse</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/sebi-fno-regulations-2026-derivatives-traders</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/best-intraday-trading-indicators-nse-vwap-ema-rsi</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/best-stock-screener-india-nse-bse</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/sebi-regulations-retail-traders-india-2026</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://trade.doaide.com/blog/intraday-trading-tips-strategies-india</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
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
