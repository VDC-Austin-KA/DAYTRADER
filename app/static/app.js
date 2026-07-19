// Dashboard logic for the ML Options Day Trader.
const $ = (sel) => document.querySelector(sel);
let priceChart = null;
let signalsCache = [];
// Live US buying power, refreshed with the account strip. Sizing caps
// are derived from this so the UI cannot offer a size you can't fund.
let _buyingPower = 0;
const BP_USABLE = 2 / 3;   // leave headroom for manual trades

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.borderColor = isError ? "var(--red)" : "var(--border)";
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 3500);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

function fmtMoney(n) {
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function pnlClass(n) { return n > 0 ? "pos" : n < 0 ? "neg" : ""; }

async function loadSummary() {
  const data = await api("/api/portfolio");
  const s = data.summary;
  $("#summary-cards").innerHTML = `
    <div class="card"><div class="label">Equity</div><div class="value">${fmtMoney(s.equity)}</div></div>
    <div class="card"><div class="label">Cash</div><div class="value">${fmtMoney(s.cash)}</div></div>
    <div class="card"><div class="label">Open Value</div><div class="value">${fmtMoney(s.market_value)}</div></div>
    <div class="card"><div class="label">Unrealized P&L</div><div class="value ${pnlClass(s.unrealized_pnl)}">${fmtMoney(s.unrealized_pnl)}</div></div>
    <div class="card"><div class="label">Total Return</div><div class="value ${pnlClass(s.total_return_pct)}">${s.total_return_pct}%</div></div>
    <div class="card"><div class="label">Open Positions</div><div class="value">${s.open_positions}</div></div>`;
  renderPositions(data.positions);
}

function renderPositions(positions) {
  const tb = $("#positions-table tbody");
  const panel = $("#positions-panel");
  // Pinned panel only exists while there is something to close, so a flat
  // account doesn't carry a permanent empty box at the top of the page.
  if (panel) panel.hidden = !positions.length;
  if (!positions.length) {
    tb.innerHTML = `<tr><td colspan="9" class="muted">No open positions.</td></tr>`;
    return;
  }
  tb.innerHTML = positions.map(p => `
    <tr>
      <td>${p.symbol}</td>
      <td><span class="pill ${p.option_type === 'call' ? 'bullish' : 'bearish'}">${p.option_type}</span></td>
      <td>$${p.strike}</td><td>${p.expiry}</td><td>${p.quantity}</td>
      <td>$${p.entry_price.toFixed(2)}</td><td>$${p.current_price.toFixed(2)}</td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</td>
      <td class="pos-actions">
        <button class="btn sm red" onclick="closePosition(${p.id})">Close</button>
        <button class="btn sm ghost" title="Close this and open the opposite side, same size"
                onclick='flipPosition(${p.id}, ${JSON.stringify(p).replace(/'/g, "&apos;")})'>⇄ Flip</button>
      </td>
    </tr>`).join("");
}

async function closePosition(id, quiet = false) {
  try {
    const r = await api("/api/close", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_id: id }),
    });
    if (quiet) return r;           // bulk close reports once at the end
    toast(r.message);
    loadSummary(); loadTrades();
  } catch (e) {
    if (quiet) throw e;
    toast(e.message, true);
  }
}

// Reverse a position in one click: close this side, open the other at the
// same strike (or nearest listed) and the same size. Two real orders in
// live mode, so it always confirms.
async function flipPosition(id, p) {
  const to = p.option_type === "call" ? "PUT" : "CALL";
  if (!confirm(
    `Flip ${p.symbol} ${p.option_type.toUpperCase()} $${p.strike} → ${to} $${p.strike}, ` +
    `same size (${p.quantity}).` +
    (_liveTradeMode
      ? "\n\nThis sends TWO REAL orders to your moomoo account: a sell to close and a buy to open."
      : "")
  )) return;
  try {
    const r = await api("/api/flip", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position_id: id }),
    });
    toast(r.message);
    loadSummary(); loadTrades();
  } catch (e) { toast(e.message, true); }
}

async function loadSignals() {
  $("#signals-status").textContent = "loading…";
  try {
    signalsCache = await api("/api/signals");
    const tb = $("#signals-table tbody");
    if (!signalsCache.length) {
      tb.innerHTML = `<tr><td colspan="8" class="muted">No signals yet. Click "Train models" then "Refresh signals".</td></tr>`;
    } else {
      tb.innerHTML = signalsCache.map(s => {
        const conf = (Math.abs(s.probability - 0.5) * 200).toFixed(0);
        const contract = s.contract_symbol
          ? `${s.option_type.toUpperCase()} $${s.strike}`
          : "—";
        const canTrade = s.contract_symbol && s.option_type;
        return `<tr>
          <td><a href="#" onclick="selectSymbol('${s.symbol}');return false;">${s.symbol}</a></td>
          <td><span class="pill ${s.direction}">${s.direction}</span></td>
          <td>${conf}%</td>
          <td>${contract}</td>
          <td>${s.dte || "—"}</td>
          <td>${s.option_price ? "$" + s.option_price.toFixed(2) : "—"}</td>
          <td>${s.breakeven ? "$" + s.breakeven : "—"}</td>
          <td>${canTrade ? buyCell(s, "buyFromSignal") : ""}</td>
        </tr>`;
      }).join("");
    }
    $("#signals-status").textContent = `${signalsCache.length} symbols`;
    populateChartSelect();
  } catch (e) {
    $("#signals-status").textContent = "error";
    toast(e.message, true);
  }
}

// Contracts per order. Every Buy button has its own qty box beside it, so
// size is set where the trade is taken rather than in a header field that
// may be scrolled off screen. Falls back to the header default.
// Clamped so a stray keystroke can't send a 10,000-lot to a live account.
function clampQty(raw) {
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(n, 100);
}

function orderQty(el) {
  // el = the Buy button; its row-local input is the authority.
  if (el) {
    const box = el.parentElement && el.parentElement.querySelector(".row-qty");
    if (box) {
      const cap = parseInt(box.dataset.cap, 10);
      const n = clampQty(box.value);
      return Number.isFinite(cap) ? Math.min(n, cap) : n;
    }
  }
  return clampQty(($("#order-qty") || {}).value);
}

// Most contracts affordable at `price`, using two thirds of buying power.
// The remaining third is deliberately left free so manual trades in the
// moomoo app are never blocked by what this dashboard has committed.
function maxAffordable(price) {
  if (!_buyingPower || !price || price <= 0) return 100;
  return Math.max(0, Math.floor((_buyingPower * BP_USABLE) / (price * 100)));
}

// Markup for a qty box + Buy button pair, used by every table.
function buyCell(payload, fnName = "buyOpportunity", label = "Buy") {
  const json = JSON.stringify(payload).replace(/'/g, "&apos;");
  const price = payload.mid ?? payload.option_price ?? 0;
  const cap = Math.min(100, maxAffordable(price));
  if (cap < 1) {
    return `<span class="muted" title="Buying power $${_buyingPower.toFixed(2)}">`
      + `too rich</span>`;
  }
  const start = Math.min(clampQty(($("#order-qty") || {}).value), cap);
  return `<div class="buy-cell">
    <input class="row-qty" type="number" min="1" max="${cap}" step="1"
           value="${start}" data-cap="${cap}"
           title="Max ${cap} at $${price.toFixed(2)} (2/3 of buying power)"
           onclick="event.stopPropagation()">
    <button class="btn sm" onclick='${fnName}(${json}, this)'>${label}</button>
  </div>`;
}

async function buyFromSignal(s, el) {
  try {
    const qty = orderQty(el);
    if (_liveTradeMode && !confirm(
      `LIVE order to your moomoo account:\nBuy ${qty} ${s.symbol} ${s.option_type} $${s.strike} @ ~$${(s.option_price ?? 0).toFixed(2)}.\nProceed?`
    )) return;
    const r = await api("/api/trade", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: s.symbol, option_type: s.option_type,
        contract_symbol: s.contract_symbol, strike: s.strike,
        expiry: s.expiry, quantity: qty, price: s.option_price,
        note: "from signal",
      }),
    });
    toast(r.message);
    loadSummary(); loadTrades();
  } catch (e) { toast(e.message, true); }
}

function populateChartSelect() {
  const sel = $("#chart-symbol");
  const symbols = signalsCache.length ? signalsCache.map(s => s.symbol) : WATCHLIST;
  sel.innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join("");
  if (symbols.length) selectSymbol(symbols[0]);
}

async function selectSymbol(symbol) {
  $("#chart-symbol").value = symbol;
  try {
    const data = await api(`/api/history/${symbol}?days=120`);
    const ctx = $("#price-chart");
    if (priceChart) priceChart.destroy();
    priceChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: data.dates,
        datasets: [{
          label: `${symbol} close`, data: data.close,
          borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,.12)",
          fill: true, tension: 0.2, pointRadius: 0, borderWidth: 2,
        }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8a99ad", maxTicksLimit: 6 }, grid: { display: false } },
          y: { ticks: { color: "#8a99ad" }, grid: { color: "#243044" } },
        },
      },
    });
    const sig = signalsCache.find(s => s.symbol === symbol);
    $("#signal-detail").innerHTML = sig
      ? `<strong>${symbol}</strong> — ${sig.rationale}`
      : `No signal cached for ${symbol}.`;
  } catch (e) { toast(e.message, true); }
}

async function loadModels() {
  const rows = await api("/api/models");
  const tb = $("#models-table tbody");
  tb.innerHTML = rows.length
    ? rows.map(r => `<tr><td>${r.symbol}</td><td>${(r.accuracy*100).toFixed(1)}%</td>
        <td>${r.roc_auc.toFixed(3)}</td><td>${r.n_samples}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No models trained yet.</td></tr>`;
}

async function loadTrades() {
  const rows = await api("/api/trades");
  const tb = $("#trades-table tbody");
  tb.innerHTML = rows.length
    ? rows.map(t => `<tr>
        <td>${new Date(t.timestamp).toLocaleString()}</td>
        <td>${t.symbol}</td>
        <td><span class="pill ${t.side === 'buy' ? 'bullish' : 'neutral'}">${t.side}</span></td>
        <td>${t.option_type}</td><td>${t.quantity}</td><td>$${t.price.toFixed(2)}</td>
        <td class="${pnlClass(t.realized_pnl)}">${t.realized_pnl ? fmtMoney(t.realized_pnl) : "—"}</td>
      </tr>`).join("")
    : `<tr><td colspan="7" class="muted">No trades yet.</td></tr>`;
}

// --- Opportunity scanner ---
function popColor(p) {
  if (p >= 0.45) return "var(--green)";
  if (p >= 0.25) return "var(--yellow)";
  return "var(--red)";
}

async function runScan() {
  const symbol = ($("#scan-symbol").value || "SPY").trim().toUpperCase();
  const side = $("#scan-side").value;
  const dte = $("#scan-dte").value;
  const premium = $("#scan-premium").value;
  const cost = $("#scan-cost").value;
  const meta = $("#scan-meta");
  const tb = $("#scan-table tbody");
  meta.innerHTML = `<span class="spinner"></span> Scanning ${symbol}…`;
  tb.innerHTML = "";
  try {
    const q = `max_dte=${dte}&max_premium=${premium}&max_cost=${cost}&side=${side}`;
    const r = await api(`/api/opportunities/${symbol}?${q}`);
    const modelNote = r.model_trained
      ? `model lean included (p=${r.model_prob})`
      : `⚠️ no ML model trained for ${symbol} — ranking on probability of profit only. Train it for a directional edge.`;
    meta.innerHTML = `${symbol} @ $${r.underlying} · ${r.count} contracts under the filters · showing top ${r.opportunities.length} · ${modelNote}`;
    if (!r.opportunities.length) {
      tb.innerHTML = `<tr><td colspan="12" class="muted">No contracts matched. Try raising Max DTE or Max cost.</td></tr>`;
      return;
    }
    tb.innerHTML = r.opportunities.map(o => {
      const pop = (o.prob_profit * 100).toFixed(1);
      const succ = (o.success * 100).toFixed(1);
      const pot = (o.potential_return * 100).toFixed(0);
      return `<tr>
        <td><span class="pill ${o.option_type === 'call' ? 'bullish' : 'bearish'}">${o.option_type}</span></td>
        <td>$${o.strike}</td><td>${o.expiry}</td><td>${o.dte}</td>
        <td>$${o.cost.toFixed(0)}</td>
        <td style="color:${popColor(o.prob_profit)}">${pop}%</td>
        <td>${succ}%</td>
        <td class="pos">+${pot}%</td>
        <td>${o.breakeven_move_pct}%</td>
        <td>${(o.iv * 100).toFixed(0)}%</td>
        <td>${o.score.toFixed(2)}</td>
        <td>${buyCell(o)}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    meta.textContent = "";
    tb.innerHTML = `<tr><td colspan="12" class="neg">${e.message}</td></tr>`;
  }
}

let _liveTradeMode = false;

async function buyOpportunity(o, el) {
  const qty = orderQty(el);
  if (_liveTradeMode && !confirm(
    `LIVE order to your moomoo account:\nBuy ${qty} ${o.symbol} ${o.option_type} $${o.strike} @ ~$${(o.mid ?? 0).toFixed(2)}` +
    `\nEst. cost: $${((o.mid ?? 0) * qty * 100).toFixed(2)}\nProceed?`
  )) return;
  try {
    const r = await api("/api/trade", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: o.symbol, option_type: o.option_type,
        contract_symbol: o.contract_symbol, strike: o.strike,
        expiry: o.expiry, quantity: qty, price: o.mid,
        note: `scanner POP ${(o.prob_profit * 100).toFixed(0)}%`,
      }),
    });
    toast(r.message);
    loadSummary(); loadTrades();
  } catch (e) { toast(e.message, true); }
}

$("#scan-btn").addEventListener("click", runScan);
$("#scan-symbol").addEventListener("keydown", (e) => { if (e.key === "Enter") runScan(); });

// --- Trade-mode indicator (paper vs live moomoo) ---
async function showTradeMode() {
  try {
    const c = await api("/api/config");
    let el = $("#trade-mode-pill");
    if (!el) {
      el = document.createElement("span");
      el.id = "trade-mode-pill";
      el.className = "pill";
      const actions = document.querySelector(".header-actions");
      if (actions) actions.prepend(el);
    }
    const live = c.dashboard_trade_mode === "moomoo";
    _liveTradeMode = live;
    el.textContent = live ? "● LIVE moomoo" : "○ paper";
    el.className = "pill " + (live ? "bearish" : "neutral");
    el.title = live
      ? "Buy/Close route to your moomoo account via OpenD"
      : "Trades simulated locally. Set DASHBOARD_TRADE_MODE=moomoo to route to your account.";
    const prov = document.querySelector(".header-actions");
    if (prov && c.in_open_window) el.textContent += " · open-window";
  } catch (e) { /* ignore */ }
}

// --- Data-source status banner ---
async function checkDataSource() {
  const banner = $("#datasource-banner");
  try {
    const s = await api("/api/datasource");
    if (s.configured && s.ok) {
      banner.classList.add("hidden");
      return true;
    }
    banner.classList.remove("hidden");
    if (!s.configured) {
      banner.className = "banner warn";
      banner.innerHTML = `🔌 <strong>No market-data source connected.</strong>
        ${s.message} Set the <code>TRADIER_TOKEN</code> service variable in Railway
        (free token from <a href="https://developer.tradier.com" target="_blank">developer.tradier.com</a>),
        then redeploy. Until then there's no data to train on or trade.`;
    } else {
      banner.className = "banner error";
      banner.innerHTML = `⚠️ <strong>Data source error.</strong> ${s.message}`;
    }
    return false;
  } catch (e) {
    banner.classList.remove("hidden");
    banner.className = "banner error";
    banner.textContent = "Could not check data source: " + e.message;
    return false;
  }
}

// --- Training progress polling ---
let trainPoll = null;
function showTraining(st) {
  const b = $("#training-banner");
  if (st.running) {
    b.classList.remove("hidden");
    b.className = "banner info";
    b.innerHTML = `<span class="spinner"></span> Training models —
      ${st.done}/${st.total} done${st.current ? " (" + st.current + ")" : ""}…
      downloading history &amp; fitting a model per symbol.`;
  } else if (st.finished_at && st.total) {
    const trained = (st.last_results || []).filter(r => r.status === "trained").length;
    b.classList.remove("hidden");
    b.className = "banner ok";
    b.innerHTML = `✅ Trained ${trained}/${st.total} models. Refreshing signals…`;
    setTimeout(() => b.classList.add("hidden"), 6000);
  } else {
    b.classList.add("hidden");
  }
}
async function pollTraining() {
  try {
    const st = await api("/api/train/status");
    showTraining(st);
    if (st.running) return;
  } catch (e) { /* ignore */ }
  if (trainPoll) { clearInterval(trainPoll); trainPoll = null; }
  loadModels(); loadSignals(); loadSummary();
}
function startTrainingPoll() {
  if (trainPoll) clearInterval(trainPoll);
  pollTraining();
  trainPoll = setInterval(pollTraining, 2500);
}

$("#train-btn").addEventListener("click", async () => {
  if (!(await checkDataSource())) {
    toast("Connect a data source first (set TRADIER_TOKEN).", true);
    return;
  }
  try {
    const r = await api("/api/train", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast(`Training ${r.count} symbols — this can take a minute…`);
    startTrainingPoll();
  } catch (e) { toast(e.message, true); }
});

$("#refresh-btn").addEventListener("click", async () => {
  toast("Refreshing signals…");
  try {
    await api("/api/signals/refresh", { method: "POST" });
    loadSignals(); loadSummary();
    toast("Signals refreshed.");
  } catch (e) { toast(e.message, true); }
});

$("#chart-symbol").addEventListener("change", (e) => selectSymbol(e.target.value));

// Panic button: flatten everything. Always confirms -- in live mode this
// sends real sell orders, and it is the one control you may reach for in a
// hurry, which is exactly when an accidental click is most likely.
$("#close-all-btn").addEventListener("click", async () => {
  const rows = document.querySelectorAll("#positions-table tbody tr td:first-child");
  const n = $("#positions-panel").hidden ? 0 : rows.length;
  if (!n) return;
  if (!confirm(
    `Close ALL ${n} open position(s)?` +
    (_liveTradeMode ? "\n\nThis sends REAL sell orders to your moomoo account." : "")
  )) return;
  const data = await api("/api/portfolio");
  for (const p of data.positions) {
    try { await closePosition(p.id, true); } catch (e) { /* keep going */ }
  }
  toast(`Closed ${data.positions.length} position(s).`);
  loadSummary(); loadTrades();
});

// --- Market Movers: universe scan, plays, headlines, filterable table ---
let moversRows = [];
let moversSort = { key: "blended_score", dir: -1, numeric: true };

function renderHeadlines(items) {
  const bar = $("#headline-bar");
  if (!items || !items.length) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  // Duplicate content so the marquee loop is seamless.
  const html = items.map(h => `<span class="headline-item">${h}</span>`).join("");
  $("#headline-items").innerHTML = html + html;
}

function renderPlays(plays) {
  const row = $("#plays-row");
  if (!plays.length) {
    row.innerHTML = `<div class="muted">No names above the surge threshold right now — the universe is quiet.</div>`;
    return;
  }
  row.innerHTML = plays.map(p => `
    <div class="play-card ${p.whipsaw ? 'whipsaw' : ''}">
      <div class="play-head">
        <strong>${p.symbol}</strong>
        <span class="pill ${p.direction === 'up' ? 'bullish' : p.direction === 'down' ? 'bearish' : 'neutral'}">${p.direction}</span>
        <span class="surge-badge">surge ${p.surge.toFixed(0)}</span>
        ${p.whipsaw ? '<span class="pill neutral">whipsaw</span>' : ''}
      </div>
      <div class="play-line">🎯 ${p.entry}</div>
      <div class="play-line muted">↔️ POP ${(p.prob_profit * 100).toFixed(0)}% · potential +${(p.potential_return * 100).toFixed(0)}% · ${p.exit_plan}</div>
      ${p.wing_plan ? `<div class="play-line wing">🪽 ${p.wing_plan}</div>` : ''}
      ${buyCell({
        symbol: p.symbol, option_type: p.option_type, contract_symbol: p.contract_symbol,
        strike: p.strike, expiry: p.expiry, mid: p.mid, prob_profit: p.prob_profit,
      })}
    </div>`).join("");
}

function moversRowHtml(o) {
  return `<tr>
    <td>${o.symbol}</td>
    <td><span class="pill ${o.option_type === 'call' ? 'bullish' : 'bearish'}">${o.option_type}</span></td>
    <td>$${o.strike}</td><td>${o.expiry}</td><td>${o.dte}</td>
    <td>$${o.cost.toFixed(0)}</td>
    <td style="color:${popColor(o.prob_profit)}">${(o.prob_profit * 100).toFixed(1)}%</td>
    <td>${(o.success * 100).toFixed(1)}%</td>
    <td class="pos">+${(o.potential_return * 100).toFixed(0)}%</td>
    <td><span class="surge-badge ${o.surge >= 70 ? 'hot' : ''}">${o.surge.toFixed(0)}</span></td>
    <td>${o.direction}</td>
    <td>${o.whipsaw ? 'yes' : ''}</td>
    <td>${o.blended_score.toFixed(3)}</td>
    <td class="px-contract" title="mid $${(o.mid ?? 0).toFixed(2)} x 100">$${((o.mid ?? 0) * 100).toFixed(0)}</td>
    <td>${buyCell(o)}</td>
  </tr>`;
}

// Column filters. Text substring boxes were useless on numeric columns
// (typing "5" matched 5, 15, 0.05...). Categorical columns now get a
// dropdown of the values actually present; numeric columns get a min/max
// pair, so "POP >= 40 and cost <= 100" is expressible.
const MOVERS_NUMERIC = new Set([
  "strike", "dte", "cost", "prob_profit", "success",
  "potential_return", "surge", "blended_score",
]);
// Columns whose stored value is a fraction but displayed as a percent --
// filter input is in the DISPLAYED units, which is what the user sees.
const MOVERS_PCT = new Set(["prob_profit", "success", "potential_return"]);

function moversFilterValue(o, key) {
  if (key === "whipsaw") return o.whipsaw ? "yes" : "no";
  const v = o[key];
  return MOVERS_PCT.has(key) ? v * 100 : v;
}

function moversPasses(o) {
  for (const el of document.querySelectorAll("#movers-table .col-filter")) {
    const key = el.dataset.k;
    const raw = el.value.trim();
    if (!raw) continue;
    const val = moversFilterValue(o, key);
    if (el.dataset.range) {
      const n = parseFloat(raw);
      if (!Number.isFinite(n)) continue;
      if (el.dataset.range === "min" && !(Number(val) >= n)) return false;
      if (el.dataset.range === "max" && !(Number(val) <= n)) return false;
    } else if (String(val).toLowerCase() !== raw.toLowerCase()) {
      return false;
    }
  }
  return true;
}

function drawMoversTable() {
  let rows = moversRows.filter(moversPasses);
  const { key, dir, numeric } = moversSort;
  if (key) {
    rows = rows.slice().sort((a, b) => {
      const av = a[key], bv = b[key];
      const c = numeric ? (av - bv) : String(av).localeCompare(String(bv));
      return dir === "asc" ? c : -c;
    });
  }
  $("#movers-table tbody").innerHTML = rows.length
    ? rows.map(moversRowHtml).join("")
    : `<tr><td colspan="15" class="muted">Nothing matches the filters.</td></tr>`;
  const meta = $("#movers-filter-count");
  if (meta) meta.textContent = `${rows.length} / ${moversRows.length} shown`;
}

function initMoversTable() {
  const head = document.querySelectorAll("#movers-table thead tr:first-child th");
  const filterRow = document.querySelector("#movers-table .filter-row");
  filterRow.innerHTML = "";
  head.forEach(th => {
    const td = document.createElement("th");
    const key = th.dataset.k;
    if (key) {
      if (MOVERS_NUMERIC.has(key)) {
        td.innerHTML =
          `<div class="rangebox">
             <input class="col-filter" data-k="${key}" data-range="min"
                    type="number" step="any" placeholder="min">
             <input class="col-filter" data-k="${key}" data-range="max"
                    type="number" step="any" placeholder="max">
           </div>`;
      } else {
        td.innerHTML = `<select class="col-filter" data-k="${key}">
                          <option value="">all</option>
                        </select>`;
      }
      th.classList.add("sortable");
      th.addEventListener("click", () => {
        const numeric = th.dataset.num !== undefined;
        moversSort = moversSort.key === key
          ? { key, dir: moversSort.dir === "asc" ? "desc" : "asc", numeric }
          : { key, dir: "desc", numeric };
        drawMoversTable();
      });
    }
    filterRow.appendChild(td);
  });
  filterRow.addEventListener("input", drawMoversTable);
  filterRow.addEventListener("change", drawMoversTable);
  const reset = $("#movers-filter-reset");
  if (reset) reset.addEventListener("click", () => {
    document.querySelectorAll("#movers-table .col-filter")
      .forEach(el => { el.value = ""; });
    drawMoversTable();
  });
}

// Populate dropdowns from the values actually present in the current scan.
function refreshMoversFilterOptions() {
  // loadMovers can resolve before initMoversTable has built the row, in
  // which case there are no selects to fill. Build it first.
  if (!document.querySelector("#movers-table .col-filter")) initMoversTable();
  document.querySelectorAll("#movers-table select.col-filter").forEach(sel => {
    const key = sel.dataset.k;
    const raw = [...new Set(moversRows.map(o => moversFilterValue(o, key)))];
    const seen = raw
      .sort((a, b) => {
        const na = parseFloat(a), nb = parseFloat(b);
        if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
        return String(a).localeCompare(String(b));
      })
      .map(String);
    const keep = sel.value;
    sel.innerHTML = `<option value="">all</option>` +
      seen.map(v => `<option value="${v}">${v}</option>`).join("");
    if (seen.includes(keep)) sel.value = keep;
  });
}

async function loadMovers(refresh = false) {
  const meta = $("#movers-meta");
  meta.innerHTML = `<span class="spinner"></span> Scanning the movers universe…`;
  try {
    const r = await api(`/api/movers${refresh ? "?refresh=true" : ""}`);
    moversRows = r.options;
    refreshMoversFilterOptions();
    renderHeadlines(r.headlines);
    renderPlays(r.plays);
    drawMoversTable();
    const hot = r.readings.filter(x => x.surge >= 60).map(x => `${x.symbol} ${x.surge.toFixed(0)}`);
    meta.textContent = `${r.watchlist.length} tickers scanned · ${r.options.length} ranked contracts · ` +
      (hot.length ? `hot: ${hot.join(", ")}` : "no high-surge names right now") +
      ` · updated ${new Date(r.generated_at * 1000).toLocaleTimeString()}`;
  } catch (e) {
    meta.textContent = "";
    $("#movers-table tbody").innerHTML = `<tr><td colspan="15" class="neg">${e.message}</td></tr>`;
  }
}

$("#movers-refresh").addEventListener("click", () => loadMovers(true));
initMoversTable();

// Initial load.
showTradeMode();
checkDataSource();
loadSummary();
loadSignals();
loadModels();
loadTrades();
loadMovers();
startTrainingPoll();  // surfaces any startup auto-training in progress
setInterval(loadSummary, 60000);
setInterval(checkDataSource, 60000);
setInterval(loadMovers, 150000);  // movers rescan every 2.5 min (server caches 2 min)

// ---------------------------------------------------------------------------
// Live broker state: real balances, real positions, real working orders.
// Distinct from the paper simulator, which keeps its own $100k fantasy.
// ---------------------------------------------------------------------------

function fmtCcy(n, ccy) {
  const v = Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "-" : ""}${v} ${ccy || ""}`.trim();
}

async function loadAccount() {
  try {
    const a = await api("/api/account");
    _buyingPower = a.ok ? (a.us_buying_power || 0) : 0;
    const strip = $("#account-strip");
    if (!a.ok) {
      strip.innerHTML = `<div class="acct-item warn">Broker account unavailable — ${a.message || "gateway down"}</div>`;
      return;
    }
    // US buying power is what a US options order can actually draw on; the
    // base-currency total is shown separately so the two are never conflated.
    strip.innerHTML = `
      <div class="acct-item"><span>Buying power (US)</span><b>$${a.us_buying_power.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</b></div>
      <div class="acct-item"><span>US cash</span><b>$${a.us_cash.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</b></div>
      <div class="acct-item"><span>Total assets</span><b>${fmtCcy(a.total_assets, a.currency)}</b></div>
      <div class="acct-item"><span>Positions value</span><b>${fmtCcy(a.market_value, a.currency)}</b></div>
      <div class="acct-item"><span>Unrealized</span><b class="${pnlClass(a.unrealized_pl)}">${fmtCcy(a.unrealized_pl, a.currency)}</b></div>
      <div class="acct-item"><span>Realized</span><b class="${pnlClass(a.realized_pl)}">${fmtCcy(a.realized_pl, a.currency)}</b></div>
      <div class="acct-item"><span>Mode</span><b class="${a.env === "REAL" ? "neg" : ""}">${a.env}</b></div>`;
  } catch (e) {
    $("#account-strip").innerHTML =
      `<div class="acct-item warn">Account error — ${e.message}</div>`;
  }
}

async function loadBrokerPositions() {
  try {
    const rows = await api("/api/broker/positions");
    const panel = $("#broker-positions-panel");
    panel.hidden = !rows.length;
    if (!rows.length) return;
    $("#broker-positions-table tbody").innerHTML = rows.map(p => `
      <tr>
        <td title="${p.name}">${p.code}</td>
        <td>${p.qty}</td>
        <td>$${p.cost_price.toFixed(2)}</td>
        <td>$${p.current_price.toFixed(2)}</td>
        <td>${fmtCcy(p.market_value, p.currency)}</td>
        <td class="${pnlClass(p.pl_val)}">${fmtCcy(p.pl_val, p.currency)}</td>
        <td class="${pnlClass(p.pl_ratio)}">${p.pl_ratio.toFixed(2)}%</td>
        <td><button class="btn sm red" onclick="closeBrokerPosition('${p.code}', ${p.can_sell_qty}, ${p.current_price})">Close</button></td>
      </tr>`).join("");
  } catch (e) { /* panel stays hidden */ }
}

async function closeBrokerPosition(code, qty, price) {
  if (!qty) { toast("Nothing sellable in that position.", true); return; }
  if (!confirm(`Sell ${qty} of ${code} at ~$${price.toFixed(2)}?\n\nThis is a REAL order.`)) return;
  try {
    const r = await api("/api/trade/close_broker", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, qty, price }),
    });
    toast(r.message);
    loadBrokerPositions(); loadBrokerOrders(); loadAccount();
  } catch (e) { toast(e.message, true); }
}

async function loadBrokerOrders() {
  try {
    const rows = await api("/api/broker/orders");
    const panel = $("#broker-orders-panel");
    panel.hidden = !rows.length;
    if (!rows.length) return;
    $("#broker-orders-table tbody").innerHTML = rows.map(o => `
      <tr>
        <td title="${o.name}">${o.code}</td>
        <td><span class="pill ${/BUY/i.test(o.side) ? "bullish" : "bearish"}">${o.side}</span></td>
        <td>${o.qty}</td>
        <td>$${o.price.toFixed(2)}</td>
        <td>${o.dealt_qty}${o.dealt_avg_price ? ` @ $${o.dealt_avg_price.toFixed(2)}` : ""}</td>
        <td>${o.status}${o.err ? ` <span class="muted" title="${o.err}">⚠</span>` : ""}</td>
        <td class="pos-actions">
          ${o.cancellable ? `
            <button class="btn sm ghost" onclick='amendOrder(${JSON.stringify(o).replace(/'/g, "&apos;")})'>Edit</button>
            <button class="btn sm red" onclick="cancelOrder('${o.order_id}')">Cancel</button>` : ""}
        </td>
      </tr>`).join("");
  } catch (e) { /* panel stays hidden */ }
}

async function cancelOrder(id) {
  if (!confirm("Cancel this working order?")) return;
  try {
    const r = await api("/api/broker/orders/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_id: id }),
    });
    toast(r.message);
    loadBrokerOrders(); loadAccount();
  } catch (e) { toast(e.message, true); }
}

async function amendOrder(o) {
  const price = prompt(`New limit price for ${o.code}:`, o.price.toFixed(2));
  if (price === null) return;
  const qty = prompt(`New quantity for ${o.code}:`, o.qty);
  if (qty === null) return;
  try {
    const r = await api("/api/broker/orders/amend", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        order_id: o.order_id, qty: parseFloat(qty), price: parseFloat(price) }),
    });
    toast(r.message);
    loadBrokerOrders(); loadAccount();
  } catch (e) { toast(e.message, true); }
}

function refreshBroker() {
  loadAccount(); loadBrokerPositions(); loadBrokerOrders();
}

$("#orders-refresh").addEventListener("click", refreshBroker);
$("#broker-flatten").addEventListener("click", async () => {
  const rows = await api("/api/broker/positions");
  if (!rows.length) return;
  if (!confirm(`Close ALL ${rows.length} live position(s)?\n\nThis sends REAL sell orders.`)) return;
  for (const p of rows) {
    if (!p.can_sell_qty) continue;
    try {
      await api("/api/trade/close_broker", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: p.code, qty: p.can_sell_qty, price: p.current_price }),
      });
    } catch (e) { /* keep going; one failure must not strand the rest */ }
  }
  toast("Flatten sent.");
  refreshBroker();
});

refreshBroker();
setInterval(refreshBroker, 15000);

// ---------------------------------------------------------------------------
// Trade notifications, in-page only. An autonomous daemon placing real
// orders must never do it invisibly, and the alert must look the same
// through the Railway /go link on a phone as it does on the desktop.
// ---------------------------------------------------------------------------
let _lastNotifId = 0;

// In-page only: no OS notification permission prompt, nothing that depends
// on the browser being trusted or the tab being desktop. That means alerts
// render identically on a phone hitting the Railway /go link as they do
// locally -- the banner IS the notification.
function showTradeAlert(e) {
  const bar = $("#trade-alerts");
  if (!bar) return;
  const cls = e.kind === "entry" ? "alert-entry"
            : e.kind === "exit" ? "alert-exit" : "alert-warn";
  const when = new Date(e.ts).toLocaleTimeString();
  const el = document.createElement("div");
  el.className = `trade-alert ${cls}`;
  el.innerHTML = `<b>${e.title}</b><span>${e.detail || ""}</span>
    <em>${when}</em>
    <button onclick="this.parentElement.remove()">✕</button>`;
  bar.prepend(el);
  while (bar.children.length > 8) bar.lastChild.remove();
  toast(e.title);
}

async function pollNotifications() {
  try {
    const r = await api(`/api/notifications?after=${_lastNotifId}`);
    for (const e of r.events || []) showTradeAlert(e);
    _lastNotifId = r.latest ?? _lastNotifId;
  } catch (e) { /* transient; next poll retries */ }
}

// Show the recent backlog on load so a phone opened mid-session still sees
// what the daemon has been doing, then follow live.
api("/api/notifications?after=0")
  .then(r => {
    (r.events || []).slice(-5).forEach(showTradeAlert);
    _lastNotifId = r.latest || 0;
  })
  .catch(() => {});
setInterval(pollNotifications, 3000);

// ---------------------------------------------------------------------------
// TradingView Advanced Chart. Their widget already provides extended-hours
// sessions and RSI / Awesome Oscillator as native studies, so embedding it
// beats reimplementing those in Chart.js -- and it brings drawing tools and
// the full indicator library for free.
// ---------------------------------------------------------------------------
function loadTradingView() {
  const container = $("#tv-chart-container");
  if (!container) return;
  const symbol = ($("#tv-symbol").value || "SPY").trim().toUpperCase();
  const interval = $("#tv-interval").value;
  const extended = $("#tv-extended").checked;

  container.innerHTML = "";
  const script = document.createElement("script");
  script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
  script.async = true;
  script.innerHTML = JSON.stringify({
    autosize: true,
    symbol,
    interval,
    timezone: "America/Chicago",
    theme: "dark",
    style: "1",                    // candles
    locale: "en",
    // "extended" includes premarket and after-hours; "regular" is RTH only.
    session: extended ? "extended" : "regular",
    withdateranges: true,
    hide_side_toolbar: false,
    allow_symbol_change: true,
    studies: [
      "Volume@tv-basicstudies",
      "RSI@tv-basicstudies",
      "AwesomeOscillator@tv-basicstudies",
    ],
    support_host: "https://www.tradingview.com",
  });
  container.appendChild(script);
}

["#tv-symbol", "#tv-interval", "#tv-extended"].forEach(sel => {
  const el = $(sel);
  if (el) el.addEventListener("change", loadTradingView);
});
loadTradingView();

// ---------------------------------------------------------------------------
// Mobile: tabs, column toggles, density. A portrait iPhone cannot show a
// 15-column table and five panels at once, so the page becomes one view at
// a time with the columns the user actually wants.
// ---------------------------------------------------------------------------
const VIEW_KEY = "dt.view", COLS_KEY = "dt.cols", DENSE_KEY = "dt.dense";

function showView(name) {
  document.querySelectorAll("[data-view]").forEach(el => {
    el.hidden = el.dataset.view !== name;
  });
  document.querySelectorAll("#tabbar .tab[data-tab]").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  localStorage.setItem(VIEW_KEY, name);
  // Panels without a data-view (grid extras) belong to Models.
  document.querySelectorAll("main > .grid").forEach(g => {
    g.hidden = name !== "models";
  });
  if (name === "news") loadNews();
}

document.querySelectorAll("#tabbar .tab[data-tab]").forEach(b =>
  b.addEventListener("click", () => showView(b.dataset.tab)));

// --- Column visibility (movers table) -------------------------------------
function hiddenCols() {
  try { return new Set(JSON.parse(localStorage.getItem(COLS_KEY) || "[]")); }
  catch { return new Set(); }
}

function applyCols() {
  const hide = hiddenCols();
  const ths = [...document.querySelectorAll("#movers-table thead tr:first-child th")];
  ths.forEach((th, i) => {
    const off = hide.has(th.dataset.k || String(i));
    th.style.display = off ? "none" : "";
    document.querySelectorAll("#movers-table tr").forEach(tr => {
      const cell = tr.children[i];
      if (cell) cell.style.display = off ? "none" : "";
    });
  });
}

function buildColsPanel() {
  const panel = $("#cols-panel");
  const ths = [...document.querySelectorAll("#movers-table thead tr:first-child th")];
  const hide = hiddenCols();
  panel.innerHTML = ths.map((th, i) => {
    const key = th.dataset.k || String(i);
    const label = th.textContent.trim() || "Buy";
    return `<label><input type="checkbox" data-col="${key}"
      ${hide.has(key) ? "" : "checked"}> ${label}</label>`;
  }).join("");
  panel.addEventListener("change", e => {
    if (!e.target.dataset.col) return;
    const h = hiddenCols();
    e.target.checked ? h.delete(e.target.dataset.col) : h.add(e.target.dataset.col);
    localStorage.setItem(COLS_KEY, JSON.stringify([...h]));
    applyCols();
  });
}

$("#cols-btn").addEventListener("click", () => {
  const p = $("#cols-panel");
  if (!p.innerHTML) buildColsPanel();
  p.hidden = !p.hidden;
});

// --- Density ---------------------------------------------------------------
function applyDensity() {
  document.body.classList.toggle("dense", localStorage.getItem(DENSE_KEY) === "1");
}
$("#density-btn").addEventListener("click", () => {
  localStorage.setItem(DENSE_KEY,
    localStorage.getItem(DENSE_KEY) === "1" ? "0" : "1");
  applyDensity();
});

// --- News / calendar -------------------------------------------------------
async function loadNews(refresh = false) {
  try {
    const r = await api(`/api/news?limit=30${refresh ? "&refresh=true" : ""}`);
    $("#news-list").innerHTML = (r.items || []).map(n => {
      const cls = n.impact >= 3 ? "hi" : n.impact >= 2 ? "mid" : "";
      return `<a class="news-item ${cls}" href="${n.link}" target="_blank" rel="noopener">
        <span class="news-src">${n.source}</span>
        <span class="news-title">${n.title}</span>
        ${n.impact ? `<span class="news-impact">${n.impact.toFixed(0)}</span>` : ""}
      </a>`;
    }).join("") || "No headlines.";
  } catch (e) { $("#news-list").textContent = e.message; }
  try {
    const c = await api("/api/calendar?days=21");
    $("#calendar-table tbody").innerHTML = (c.events || []).map(e => `
      <tr><td>${e.date}</td><td>${e.time_et}</td><td>${e.event}</td>
        <td>${e.expected ?? "—"}</td><td>${e.actual ?? "—"}</td>
        <td>${e.previous ?? "—"}</td></tr>`).join("");
    $("#calendar-note").textContent = c.note || "";
  } catch (e) { /* leave empty */ }
}
$("#news-refresh").addEventListener("click", () => loadNews(true));

applyDensity();
showView(localStorage.getItem(VIEW_KEY) || "trade");
const _origDraw = drawMoversTable;
drawMoversTable = function () { _origDraw.apply(this, arguments); applyCols(); };
