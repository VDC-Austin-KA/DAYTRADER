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
          <td>${canTrade ? `<button class="btn sm" onclick='buyFromSignal(${JSON.stringify(s)})'>Buy 1</button>` : ""}</td>
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

async function buyFromSignal(s) {
  try {
    const r = await api("/api/trade", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: s.symbol, option_type: s.option_type,
        contract_symbol: s.contract_symbol, strike: s.strike,
        expiry: s.expiry, quantity: 1, price: s.option_price,
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

// Initial load.
checkDataSource();
loadSummary();
loadSignals();
loadModels();
loadTrades();
startTrainingPoll();  // surfaces any startup auto-training in progress
setInterval(loadSummary, 60000);
setInterval(checkDataSource, 60000);
