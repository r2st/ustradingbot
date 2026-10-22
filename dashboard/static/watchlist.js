/* Local watchlist stored in the browser (localStorage) — no account needed. */
window.DoAideWatchlist = (function () {
  var KEY = 'doaide_watchlist';

  function read() {
    try {
      var raw = JSON.parse(localStorage.getItem(KEY));
      return Array.isArray(raw) ? raw.filter(function (s) { return typeof s === 'string'; }) : [];
    } catch (e) { return []; }
  }

  function write(symbols) {
    try { localStorage.setItem(KEY, JSON.stringify(symbols)); } catch (e) { /* private mode */ }
  }

  function has(symbol) { return read().indexOf(symbol) !== -1; }

  function toggle(symbol) {
    var list = read();
    var i = list.indexOf(symbol);
    if (i === -1) list.push(symbol); else list.splice(i, 1);
    write(list);
    return i === -1;
  }

  /* A star toggle button for one symbol. */
  function starHtml(symbol) {
    var on = has(symbol);
    return '<button type="button" class="v-star' + (on ? ' v-star-on' : '') +
      '" data-watch="' + symbol + '" aria-pressed="' + on +
      '" aria-label="' + (on ? 'Remove ' : 'Add ') + symbol + (on ? ' from' : ' to') +
      ' watchlist" title="Watchlist">' + (on ? '★' : '☆') + '</button>';
  }

  /* Delegated click handling for every [data-watch] button under root. */
  function bind(root, onChange) {
    (root || document).addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-watch]') : null;
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      var sym = btn.getAttribute('data-watch');
      var on = toggle(sym);
      btn.textContent = on ? '★' : '☆';
      btn.classList.toggle('v-star-on', on);
      btn.setAttribute('aria-pressed', String(on));
      if (onChange) onChange(sym, on);
    });
  }

  return { list: read, has: has, toggle: toggle, starHtml: starHtml, bind: bind };
})();
