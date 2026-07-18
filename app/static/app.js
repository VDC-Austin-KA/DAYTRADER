// Dashboard logic for the ML Options Day Trader.
const $ = (sel) => document.querySelector(sel);
let priceChart = null;
let signalsCache = [];

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
      <td><button class="btn sm red" onclick="closePosition(${p.id})">Close</button></td>
    </tr>`).join("");
}

async function closePosition(id) {
  try {
    const r = await api("/api/close", {
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
          <td>${canTrade ? `<button class="btn sm" onclick='buyFromSignal(${JSON.stringify(s)})'>Buy</button>` : ""}</td>
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

// Contracts per order, taken from the header field. Clamped so a stray
// keystroke can't send a 10,000-lot to a live account.
function orderQty() {
  const raw = parseInt(($("#order-qty") || {}).value, 10);
  if (!Number.isFinite(raw) || raw < 1) return 1;
  return Math.min(raw, 100);
}

async function buyFromSignal(s) {
  try {
    const qty = orderQty();
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
        <td><button class="btn sm" onclick='buyOpportunity(${JSON.stringify(o)})'>Buy</button></td>
      </tr>`;
    }).join("");
  } catch (e) {
    meta.textContent = "";
    tb.innerHTML = `<tr><td colspan="12" class="neg">${e.message}</td></tr>`;
  }
}

let _liveTradeMode = false;

async function buyOpportunity(o) {
  const qty = orderQty();
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
      <button class="btn sm" onclick='buyOpportunity(${JSON.stringify({
        symbol: p.symbol, option_type: p.option_type, contract_symbol: p.contract_symbol,
        strike: p.strike, expiry: p.expiry, mid: p.mid, prob_profit: p.prob_profit,
      })})'>Buy (paper)</button>
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
    <td><button class="btn sm" onclick='buyOpportunity(${JSON.stringify(o)})'>Buy</button></td>
  </tr>`;
}

function drawMoversTable() {
  const filters = [...document.querySelectorAll("#movers-table .col-filter")]
    .map(inp => ({ key: inp.dataset.k, q: inp.value.trim().toLowerCase() }))
    .filter(f => f.q);
  let rows = moversRows.filter(o =>
    filters.every(f => String(f.key === 'whipsaw' ? (o.whipsaw ? 'yes' : 'no') : o[f.key])
      .toLowerCase().includes(f.q)));
  const { key, dir, numeric } = moversSort;
  rows = rows.slice().sort((a, b) => {
    const av = a[key], bv = b[key];
    return dir * (numeric ? (av - bv) : String(av).localeCompare(String(bv)));
  });
  $("#movers-table tbody").innerHTML = rows.length
    ? rows.map(moversRowHtml).join("")
    : `<tr><td colspan="14" class="muted">Nothing matches the filters.</td></tr>`;
}

function initMoversTable() {
  const head = document.querySelectorAll("#movers-table thead tr:first-child th");
  const filterRow = document.querySelector("#movers-table .filter-row");
  filterRow.innerHTML = "";
  head.forEach(th => {
    const td = document.createElement("th");
    if (th.dataset.k) {
      td.innerHTML = `<input class="col-filter" data-k="${th.dataset.k}" placeholder="filter" />`;
      th.classList.add("sortable");
      th.addEventListener("click", () => {
        const numeric = "num" in th.dataset;
        if (moversSort.key === th.dataset.k) moversSort.dir *= -1;
        else moversSort = { key: th.dataset.k, dir: numeric ? -1 : 1, numeric };
        drawMoversTable();
      });
    }
    filterRow.appendChild(td);
  });
  filterRow.addEventListener("input", drawMoversTable);
}

async function loadMovers(refresh = false) {
  const meta = $("#movers-meta");
  meta.innerHTML = `<span class="spinner"></span> Scanning the movers universe…`;
  try {
    const r = await api(`/api/movers${refresh ? "?refresh=true" : ""}`);
    moversRows = r.options;
    renderHeadlines(r.headlines);
    renderPlays(r.plays);
    drawMoversTable();
    const hot = r.readings.filter(x => x.surge >= 60).map(x => `${x.symbol} ${x.surge.toFixed(0)}`);
    meta.textContent = `${r.watchlist.length} tickers scanned · ${r.options.length} ranked contracts · ` +
      (hot.length ? `hot: ${hot.join(", ")}` : "no high-surge names right now") +
      ` · updated ${new Date(r.generated_at * 1000).toLocaleTimeString()}`;
  } catch (e) {
    meta.textContent = "";
    $("#movers-table tbody").innerHTML = `<tr><td colspan="14" class="neg">${e.message}</td></tr>`;
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
