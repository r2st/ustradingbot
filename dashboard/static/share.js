/* WhatsApp sharing helpers shared by the public pages and the dashboard.
 *
 * Everything is built client-side from data already on the page, so sharing
 * never calls the API.  Amounts are opt-in: portfolio summaries default to
 * percentages only, so a forwarded message doesn't leak account size.
 */
(function (global) {
  "use strict";

  var SITE = "https://trade.doaide.com";

  function signed(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    var v = Number(n).toFixed(digits === undefined ? 2 : digits);
    return (n > 0 ? "+" : "") + v;
  }

  function money(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    var sign = n < 0 ? "-" : "";
    return sign + "$" + Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }

  function waUrl(text) {
    return "https://wa.me/?text=" + encodeURIComponent(text);
  }

  function open(text) {
    global.open(waUrl(text), "_blank", "noopener");
  }

  /* summary: {account_equity, day_pnl, day_pnl_pct, holdings:[{symbol, weight_pct, unrealized_pct}]} */
  function portfolioText(summary, opts) {
    opts = opts || {};
    var lines = ["📊 My portfolio today — DoAide Trade"];
    if (opts.includeAmounts) {
      lines.push("Value: " + money(summary.account_equity));
      lines.push("Day's P&L: " + money(summary.day_pnl) + " (" + signed(summary.day_pnl_pct) + "%)");
    } else {
      lines.push("Day's P&L: " + signed(summary.day_pnl_pct) + "%");
    }
    var top = (summary.holdings || []).slice(0, 5);
    if (top.length) {
      lines.push("", "Top holdings:");
      top.forEach(function (h) {
        var bits = [h.symbol];
        if (h.weight_pct !== null && h.weight_pct !== undefined) bits.push(Number(h.weight_pct).toFixed(1) + "% of book");
        if (h.unrealized_pct !== null && h.unrealized_pct !== undefined) bits.push(signed(h.unrealized_pct) + "%");
        lines.push("• " + bits.join(" · "));
      });
    }
    lines.push("", "Track yours free: " + SITE);
    return lines.join("\n");
  }

  /* rows: [{symbol, price, change_pct}] */
  function watchlistText(title, rows, link) {
    var lines = ["👀 " + title + " — DoAide Trade"];
    rows.slice(0, 15).forEach(function (r) {
      var price = (r.price === null || r.price === undefined) ? "—" : "$" + Number(r.price).toFixed(2);
      var chg = (r.change_pct === null || r.change_pct === undefined) ? "" : " (" + signed(r.change_pct) + "%)";
      lines.push("• " + r.symbol + " " + price + chg);
    });
    if (rows.length > 15) lines.push("…and " + (rows.length - 15) + " more");
    lines.push("", link || SITE + "/screener");
    return lines.join("\n");
  }

  global.DoAideShare = {
    waUrl: waUrl,
    open: open,
    portfolioText: portfolioText,
    watchlistText: watchlistText,
  };
})(window);
