/* Caja de las 09:00: the dashboard. Plain JavaScript, no build step.
   Data: data/today.json, data/paths.json, data/private.json (encrypted), research/backtest.json. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SYMS = ["QQQ", "SPY", "DIA", "IWM"];
const PARTIAL_R = 1.7, PARTIAL_FRAC = 0.7, MAX_DAYS = 20;

const fmt = {
  px: (v) => v == null || isNaN(v) ? "-" : Number(v).toFixed(2),
  r: (v, d = 2) => v == null || isNaN(v) ? "-" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "R",
  pct: (v, d = 0) => v == null || isNaN(v) ? "-" : (100 * v).toFixed(d) + " %",
  money: (v) => v == null || isNaN(v) ? "-" : Number(v).toLocaleString("es-CL", { style: "currency", currency: "USD", maximumFractionDigits: 0 }),
  num: (v, d = 2) => v == null || isNaN(v) ? "-" : Number(v).toFixed(d),
};
const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  kids.forEach((k) => e.append(k));
  return e;
};

async function getJSON(url) {
  const r = await fetch(url + (url.includes("?") ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" });
  if (!r.ok) throw new Error(url + ": HTTP " + r.status);
  return r.json();
}

const state = { today: null, paths: null, priv: null, backtest: null, charts: {} };

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + name; });
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  if (name === "backtest" && state.backtest) renderBacktest(state.backtest);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
window.addEventListener("hashchange", () => showTab((location.hash || "#live").slice(1)));

/* ---------------------------------------------------------------- tooltip */
const tip = $("#tip");
function showTip(ev, html) {
  tip.replaceChildren();
  html.forEach((line) => tip.append(el("div", { text: line })));
  tip.hidden = false;
  const x = Math.min(window.innerWidth - tip.offsetWidth - 8, ev.clientX + 14);
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, ev.clientY - 10 - tip.offsetHeight) + "px";
}
const hideTip = () => { tip.hidden = true; };

/* ---------------------------------------------------------------- SVG charts */
const NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
function niceTicks(lo, hi, n = 5) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step = Math.pow(10, Math.floor(Math.log10((hi - lo) / n)));
  const err = (hi - lo) / n / step;
  const s = step * (err >= 7.5 ? 10 : err >= 3 ? 5 : err >= 1.5 ? 2 : 1);
  const ticks = [];
  for (let v = Math.floor(lo / s + 1e-9) * s; v <= Math.ceil(hi / s - 1e-9) * s + 1e-9; v += s) ticks.push(+v.toFixed(10));
  return ticks;
}
function measure(svgEl, texts) {
  let w = 0;
  texts.forEach((t) => { const n = svg("text", {}, t); svgEl.append(n); w = Math.max(w, n.getComputedTextLength()); n.remove(); });
  return w;
}
/* lines: [{name, color, values: [y...], dash, width}], x: labels (same length) */
function lineChart(box, lines, opts = {}) {
  box.replaceChildren();
  const W = Math.max(300, box.clientWidth || 600), H = opts.height || 240;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
  box.append(s);
  const all = lines.flatMap((l) => l.values.filter((v) => v != null && !isNaN(v)));
  if (!all.length) { s.append(svg("text", { x: 10, y: 20 }, "Sin datos")); return; }
  let lo = Math.min(...all, opts.zero ? 0 : Infinity), hi = Math.max(...all, opts.zero ? 0 : -Infinity);
  const ticks = niceTicks(lo, hi);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const yfmt = opts.yfmt || ((v) => v);
  const ml = measure(s, ticks.map(yfmt)) + 12, mr = 10, mt = 10, mb = 24;
  const n = Math.max(...lines.map((l) => l.values.length));
  const x = (i) => ml + (n <= 1 ? 0 : i / (n - 1)) * (W - ml - mr);
  const y = (v) => mt + (1 - (v - lo) / (hi - lo || 1)) * (H - mt - mb);
  ticks.forEach((t) => {
    s.append(svg("line", { x1: ml, x2: W - mr, y1: y(t), y2: y(t), stroke: t === 0 && opts.zero ? css("--muted") : css("--grid"), "stroke-width": 1 }));
    s.append(svg("text", { x: ml - 6, y: y(t) + 4, "text-anchor": "end" }, yfmt(t)));
  });
  if (opts.xlabels) {
    const lab = opts.xlabels, every = Math.max(1, Math.ceil(lab.length / Math.max(2, Math.floor((W - ml) / 90))));
    lab.forEach((l, i) => {
      const last = i === lab.length - 1;
      if (!l || !(last || (i % every === 0 && lab.length - 1 - i >= every * 0.7))) return;
      s.append(svg("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : last ? "end" : "middle" }, l));
    });
  }
  if (opts.band) {
    const [loV, hiV] = opts.band;
    let d = "";
    hiV.forEach((v, i) => { d += (i ? "L" : "M") + x(i) + " " + y(v); });
    for (let i = loV.length - 1; i >= 0; i--) d += "L" + x(i) + " " + y(loV[i]);
    s.append(svg("path", { d: d + "Z", fill: opts.bandColor || css("--box"), stroke: "none" }));
  }
  lines.forEach((l) => {
    let d = "", pen = false;
    l.values.forEach((v, i) => { if (v == null || isNaN(v)) { pen = false; return; } d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); pen = true; });
    s.append(svg("path", { d, fill: "none", stroke: l.color, "stroke-width": l.width || 2, "stroke-dasharray": l.dash ? "5 4" : "none", "stroke-linejoin": "round" }));
  });
  const cross = svg("line", { y1: mt, y2: H - mb, stroke: css("--muted"), "stroke-width": 1, opacity: 0 });
  s.append(cross);
  const hit = svg("rect", { x: ml, y: mt, width: W - ml - mr, height: H - mt - mb, fill: "transparent" });
  hit.addEventListener("pointermove", (ev) => {
    const r = s.getBoundingClientRect();
    const px = (ev.clientX - r.left) * W / r.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((px - ml) / (W - ml - mr) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 0.5);
    const head = opts.xlabels ? opts.xlabels[i] : String(i);
    showTip(ev, [head].concat(lines.filter((l) => l.values[i] != null).map((l) => l.name + ": " + yfmt(l.values[i]))));
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  s.append(hit);
}
function barChart(box, labels, values, opts = {}) {
  box.replaceChildren();
  const W = Math.max(300, box.clientWidth || 500), H = opts.height || 220;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" });
  box.append(s);
  const vals = values.map((v) => v == null ? 0 : v);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const ticks = niceTicks(lo, hi, 4);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const yfmt = opts.yfmt || ((v) => v);
  const ml = measure(s, ticks.map(yfmt)) + 12, mr = 8, mt = 16, mb = 24;
  const y = (v) => mt + (1 - (v - lo) / (hi - lo || 1)) * (H - mt - mb);
  const bw = (W - ml - mr) / labels.length;
  ticks.forEach((t) => {
    s.append(svg("line", { x1: ml, x2: W - mr, y1: y(t), y2: y(t), stroke: t === 0 ? css("--muted") : css("--grid") }));
    s.append(svg("text", { x: ml - 6, y: y(t) + 4, "text-anchor": "end" }, yfmt(t)));
  });
  labels.forEach((l, i) => {
    const v = values[i];
    const x0 = ml + i * bw + bw * 0.18, w = bw * 0.64;
    if (!opts.xEvery || i % opts.xEvery === 0) s.append(svg("text", { x: ml + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }, l));
    if (v == null) return;
    const color = opts.colors ? opts.colors[i] : v >= 0 ? css("--long") : css("--short");
    const r = svg("rect", { x: x0, width: w, y: Math.min(y(v), y(0)), height: Math.max(1, Math.abs(y(v) - y(0))), fill: color, rx: 2 });
    r.addEventListener("pointermove", (ev) => showTip(ev, [l + ": " + yfmt(v)]));
    r.addEventListener("pointerleave", hideTip);
    s.append(r);
    if (!opts.minLabel || Math.abs(v) >= opts.minLabel) s.append(svg("text", { x: x0 + w / 2, y: v >= 0 ? y(v) - 4 : y(v) + 12, "text-anchor": "middle" }, yfmt(v)));
  });
}

/* ---------------------------------------------------------------- live */
function chartColors() {
  return { bg: css("--panel"), ink: css("--muted"), grid: css("--grid"), up: css("--long"), down: css("--short"), accent: css("--accent") };
}
function renderLive(today) {
  const grid = $("#live-grid");
  $("#session-day").textContent = today.session;
  const pill = $("#market-pill");
  pill.textContent = today.market_open ? "mercado abierto" : "mercado cerrado";
  pill.className = "pill " + (today.market_open ? "open" : "closed");
  $("#updated").textContent = "datos " + today.now_ny + " NY";
  const c = chartColors();
  SYMS.forEach((sym) => {
    const d = today.symbols[sym];
    if (!d) return;
    let card = $("#card-" + sym);
    if (!card) {
      card = el("div", { class: "card sym-card", id: "card-" + sym },
        el("div", { class: "sym-head" }, el("span", { class: "name", text: sym }), el("span", { class: "price mono" })),
        el("div", { class: "sym-state" }), el("div", { class: "candles", role: "img", "aria-label": "Velas de 5 minutos de " + sym }),
        el("div", { class: "levels" }));
      grid.append(card);
    }
    $(".price", card).textContent = fmt.px(d.last);
    const st = $(".sym-state", card);
    st.replaceChildren();
    if (!d.box) st.append(el("span", { class: "muted", text: "La caja todavía no está completa (termina a las 10:00)." }));
    else if (!d.signal) st.append(el("span", { class: "muted", text: "Caja lista. Sin cierre fuera de la caja todavía." }));
    else {
      const long = d.signal.dir > 0;
      const t = new Date(d.signal.time * 1000).toISOString().slice(11, 16);
      st.append(el("span", { class: "chip " + (long ? "long" : "short"), text: long ? "señal larga" : "señal corta" }),
        document.createTextNode(" cierre " + fmt.px(d.signal.close) + " a las " + t + " · stop " + fmt.px(d.signal.stop) + " · 70 % a " + fmt.px(d.signal.partial)));
    }
    const lv = $(".levels", card);
    lv.replaceChildren();
    if (d.box) lv.append(el("span", { text: "caja " + fmt.px(d.box.low) + " - " + fmt.px(d.box.high) }), el("span", { text: d.box.bars + " velas" }),
      el("span", { text: "alto " + fmt.num((d.box.high - d.box.low) / d.box.low * 100, 2) + " %" }));
    const holder = $(".candles", card);
    let ch = state.charts[sym];
    if (!ch && window.LightweightCharts) {
      const chart = LightweightCharts.createChart(holder, {
        autoSize: true, layout: { background: { color: c.bg }, textColor: c.ink, fontFamily: "IBM Plex Mono, monospace" },
        grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } }, rightPriceScale: { borderColor: c.grid },
        timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false }, crosshair: { mode: 0 } });
      const series = chart.addCandlestickSeries({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down, borderVisible: false });
      ch = state.charts[sym] = { chart, series, lines: [], box: holder };
    }
    if (!ch) return;
    ch.series.setData(d.bars.map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] })));
    ch.lines.forEach((l) => ch.series.removePriceLine(l));
    ch.lines = [];
    const add = (price, color, title, style = 2) => ch.lines.push(ch.series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
    if (d.box) { add(d.box.high, c.accent, "caja"); add(d.box.low, c.accent, "caja"); }
    if (d.signal) { add(d.signal.partial, c.up, "70 %", 0); add(d.signal.stop, c.down, "stop", 0); }
    ch.series.setMarkers(d.signal ? [{ time: d.signal.time - 300, position: d.signal.dir > 0 ? "belowBar" : "aboveBar",
      color: d.signal.dir > 0 ? c.up : c.down, shape: d.signal.dir > 0 ? "arrowUp" : "arrowDown", text: d.signal.dir > 0 ? "largo" : "corto" }] : []);
    ch.chart.timeScale().fitContent();
  });
}

/* ---------------------------------------------------------------- private (decrypt) */
const b64 = (s) => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));
async function decryptDoc(doc, pass) {
  const enc = new TextEncoder();
  const salt = b64(doc.salt), nonce = b64(doc.nonce), ct = b64(doc.ct), tag = b64(doc.tag);
  const base = await crypto.subtle.importKey("raw", enc.encode(pass), "PBKDF2", false, ["deriveBits"]);
  const bits = new Uint8Array(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: doc.rounds }, base, 512));
  const hkey = (raw) => crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const k1 = await hkey(bits.slice(0, 32)), k2 = await hkey(bits.slice(32));
  const macIn = new Uint8Array(salt.length + nonce.length + ct.length);
  macIn.set(salt); macIn.set(nonce, salt.length); macIn.set(ct, salt.length + nonce.length);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", k2, macIn));
  if (mac.length !== tag.length || mac.some((b, i) => b !== tag[i])) throw new Error("Frase incorrecta");
  const blocks = Math.ceil(ct.length / 32), jobs = [];
  for (let i = 0; i < blocks; i++) {
    const m = new Uint8Array(nonce.length + 4);
    m.set(nonce); new DataView(m.buffer).setUint32(nonce.length, i);
    jobs.push(crypto.subtle.sign("HMAC", k1, m));
  }
  const ks = (await Promise.all(jobs)).map((b) => new Uint8Array(b));
  const out = new Uint8Array(ct.length);
  for (let i = 0; i < ct.length; i++) out[i] = ct[i] ^ ks[i >> 5][i & 31];
  return JSON.parse(new TextDecoder().decode(out));
}
function businessDaysBetween(a, b) {
  let n = 0;
  const d = new Date(a + "T12:00:00Z"), end = new Date(b + "T12:00:00Z");
  while (d < end) { d.setUTCDate(d.getUTCDate() + 1); const w = d.getUTCDay(); if (w !== 0 && w !== 6) n++; }
  return n;
}
function renderPrivate(p) {
  $("#trades-body").hidden = false;
  $("#lock").hidden = true;
  const a = p.account || {};
  const closed = p.trades.filter((t) => !t.open && t.realized_R != null);
  const totalR = closed.reduce((s, t) => s + t.realized_R, 0);
  const wins = closed.filter((t) => t.realized_R > 0).length;
  const dayChg = a.equity != null && a.last_equity ? a.equity - a.last_equity : null;
  const k = $("#acct-kpis");
  k.replaceChildren(
    kpi("Capital (paper)", fmt.money(a.equity), "efectivo " + fmt.money(a.cash)),
    kpi("Hoy", fmt.money(dayChg), "", cls(dayChg)),
    kpi("Abiertas", String(p.trades.filter((t) => t.open).length), "operaciones de la caja"),
    kpi("Cerradas", String(closed.length), closed.length ? fmt.pct(wins / closed.length) + " ganadoras" : ""),
    kpi("R total", fmt.r(totalR, 2), closed.length ? fmt.r(totalR / closed.length, 3) + " por operación" : "", cls(totalR)));
  const ob = $("#open-table tbody"); ob.replaceChildren();
  const open = p.trades.filter((t) => t.open);
  $("#open-empty").hidden = open.length > 0;
  const today = (state.today && state.today.session) || new Date().toISOString().slice(0, 10);
  open.forEach((t) => {
    const days = businessDaysBetween(t.day, today);
    const btn = el("button", { class: "btn small", type: "button", text: "Simular" });
    btn.addEventListener("click", () => {
      fillSim({ sym: t.symbol, dir: t.dir, entry: t.entry, stop: t.stop, now: t.last, days, partial: t.partial_done });
      showTab("sim"); runSim();
    });
    ob.append(row([t.symbol, t.side, fmt.px(t.entry), fmt.px(t.stop), fmt.px(t.last),
      [fmt.r(t.unrealized_R), cls(t.unrealized_R)], t.partial_done ? "cobrado" : "pendiente", String(days), btn]));
  });
  const cb = $("#closed-table tbody"); cb.replaceChildren();
  $("#closed-empty").hidden = closed.length > 0;
  closed.slice().reverse().forEach((t) => cb.append(row([t.day, t.symbol, t.side, fmt.px(t.entry), fmt.px(t.stop),
    t.partial_done ? "sí" : "no", [fmt.money(t.realized_pnl), cls(t.realized_pnl)], [fmt.r(t.realized_R, 2), cls(t.realized_R)]])));
  const eh = p.equity_history || {};
  if ((eh.equity || []).length) lineChart($("#equity-chart"), [{ name: "capital", color: css("--accent"), values: eh.equity }],
    { xlabels: eh.t.map((s) => new Date(s * 1000).toISOString().slice(5, 10)), yfmt: (v) => "$" + (v / 1000).toFixed(1) + "k", label: "Capital de la cuenta paper" });
  else $("#equity-chart").replaceChildren(el("p", { class: "muted", text: "Sin historial todavía." }));
  if (closed.length) {
    let c = 0; const cum = closed.map((t) => (c += t.realized_R));
    lineChart($("#r-chart"), [{ name: "R acumulado", color: css("--accent"), values: [0].concat(cum) }],
      { xlabels: ["inicio"].concat(closed.map((t) => t.day.slice(5))), yfmt: (v) => fmt.r(v, 1), zero: true, label: "R acumulado de las operaciones cerradas" });
  } else $("#r-chart").replaceChildren(el("p", { class: "muted", text: "Se dibuja con la primera operación cerrada." }));
  const notes = $("#notes"); notes.replaceChildren();
  const days = Object.keys(p.notes || {}).sort().reverse().slice(0, 5);
  if (!days.length) notes.append(el("li", { class: "muted", text: "Sin avisos." }));
  days.forEach((d) => p.notes[d].forEach((n) => notes.append(el("li", { text: d + " · " + n }))));
}
function kpi(k, v, n, c = "") {
  return el("div", { class: "kpi" }, el("span", { class: "k", text: k }), el("span", { class: "v " + c, text: v }), el("span", { class: "n", text: n || "" }));
}
function row(cells) {
  const tr = el("tr");
  cells.forEach((c) => {
    const td = el("td");
    if (c instanceof Node) td.append(c);
    else if (Array.isArray(c)) { td.textContent = c[0]; td.className = c[1]; }
    else td.textContent = c;
    tr.append(td);
  });
  return tr;
}
async function unlock(pass, quiet) {
  const msg = $("#lock-msg");
  try {
    msg.textContent = "Descifrando...";
    const doc = await getJSON("data/private.json");
    state.priv = await decryptDoc(doc, pass);
    msg.textContent = "";
    if ($("#remember").checked) try { sessionStorage.setItem("dash-pass", pass); } catch (e) { /* storage blocked */ }
    state.pass = pass;
    renderPrivate(state.priv);
  } catch (e) {
    if (!quiet) msg.textContent = /404/.test(e.message) ? "Todavía no hay datos de la cuenta publicados (falta el secreto DASHBOARD_KEY o la primera pasada)." : e.message === "Frase incorrecta" ? "Frase incorrecta." : "No se pudo leer: " + e.message;
  }
}
$("#unlock-form").addEventListener("submit", (ev) => { ev.preventDefault(); unlock($("#pass").value); });
$("#relock").addEventListener("click", () => {
  state.priv = null; state.pass = null;
  try { sessionStorage.removeItem("dash-pass"); } catch (e) { /* ignore */ }
  $("#trades-body").hidden = true; $("#lock").hidden = false; $("#pass").value = "";
});

/* ---------------------------------------------------------------- simulator */
function fillSim(t) {
  $("#s-sym").value = t.sym; $("#s-side").value = String(t.dir);
  $("#s-entry").value = fmt.num(t.entry); $("#s-stop").value = fmt.num(t.stop); $("#s-now").value = fmt.num(t.now);
  $("#s-days").value = t.days || 0; $("#s-partial").value = t.partial ? "1" : "0";
}
$("#s-fill").addEventListener("click", () => {
  const sym = $("#s-sym").value, d = state.today && state.today.symbols[sym];
  if (!d || !d.signal) { $("#sim-msg").textContent = "Hoy no hay señal en " + sym + "."; return; }
  fillSim({ sym, dir: d.signal.dir, entry: d.signal.close, stop: d.signal.stop, now: d.last, days: 0, partial: false });
  $("#sim-msg").textContent = "";
});
$("#sim-form").addEventListener("submit", (ev) => { ev.preventDefault(); runSim(); });

function quantile(sorted, q) { return sorted[Math.min(sorted.length - 1, Math.max(0, Math.floor(q * (sorted.length - 1))))]; }
function simulate(p, days, runs) {
  const dir = p.dir, risk = Math.abs(p.entry - p.stop);
  const R = (x) => (x - p.entry) * dir / risk;
  const P = p.entry + dir * PARTIAL_R * risk;
  const daysLeft = Math.max(0, MAX_DAYS - p.days);
  const ckEvery = 12, maxCk = (daysLeft + 1) * 17;
  const fan = Array.from({ length: maxCk }, () => new Float32Array(runs));
  const finals = new Float64Array(runs);
  let hitPartial = 0, stopped = 0;
  for (let k = 0; k < runs; k++) {
    let price = p.now, done = p.partial, realized = done ? PARTIAL_FRAC * PARTIAL_R : 0, frac = done ? 1 - PARTIAL_FRAC : 1;
    let stop = done ? p.entry : p.stop, crossed = 0, ck = 0, step = 0, out = null, partialNow = false;
    let dayIdx = Math.floor(Math.random() * days.length), j = Math.floor(Math.random() * days[dayIdx].bars.length);
    const val = () => realized + frac * R(price);
    while (out === null) {
      if (j >= days[dayIdx].bars.length) {
        crossed++;
        if (crossed > daysLeft) break;
        dayIdx = Math.floor(Math.random() * days.length); j = 0;
        if (!days[dayIdx].bars.length) continue;
      }
      const b = days[dayIdx].bars[j++];
      const hi = price * (1 + b[0] / 1e5), lo = price * (1 + b[1] / 1e5), cl = price * (1 + b[2] / 1e5);
      if (dir > 0) {
        if (lo <= stop) { const px = hi < stop ? hi : stop; out = realized + frac * R(px); stopped++; break; }
        if (!done && hi >= P) { done = true; partialNow = true; realized += PARTIAL_FRAC * PARTIAL_R; frac = 1 - PARTIAL_FRAC; stop = p.entry; }
      } else {
        if (hi >= stop) { const px = lo > stop ? lo : stop; out = realized + frac * R(px); stopped++; break; }
        if (!done && lo <= P) { done = true; partialNow = true; realized += PARTIAL_FRAC * PARTIAL_R; frac = 1 - PARTIAL_FRAC; stop = p.entry; }
      }
      price = cl;
      if (++step % ckEvery === 0 && ck < maxCk) fan[ck++][k] = val();
    }
    const fin = out === null ? val() : out;
    finals[k] = fin;
    for (; ck < maxCk; ck++) fan[ck][k] = fin;
    if (partialNow) hitPartial++;
  }
  const barsPerDay = days.reduce((s, d) => s + d.bars.length, 0) / days.length || 190;
  return { finals, fan, hitPartial: hitPartial / runs, stopped: stopped / runs, daysLeft, barsPerDay };
}
function runSim() {
  const msg = $("#sim-msg");
  const p = { sym: $("#s-sym").value, dir: +$("#s-side").value, entry: +$("#s-entry").value, stop: +$("#s-stop").value,
    now: +$("#s-now").value, days: +$("#s-days").value || 0, partial: $("#s-partial").value === "1" };
  const runs = +$("#s-runs").value;
  if (!(p.entry > 0 && p.stop > 0 && p.now > 0) || p.entry === p.stop) { msg.textContent = "Completa entrada, stop y precio ahora."; return; }
  if ((p.entry - p.stop) * p.dir <= 0) { msg.textContent = "El stop tiene que quedar del otro lado: abajo de la entrada en un largo, arriba en un corto."; return; }
  const days = state.paths && state.paths.symbols[p.sym];
  if (!days || !days.length) { msg.textContent = "Faltan los datos de " + p.sym + "."; return; }
  const R = (x) => (x - p.entry) * p.dir / Math.abs(p.entry - p.stop);
  const rNow = R(p.now);
  if (!p.partial && rNow <= -1) { msg.textContent = "Con ese precio el stop ya habría saltado."; return; }
  msg.textContent = "Simulando " + runs + " caminos...";
  setTimeout(() => {
    const res = simulate(p, days, runs);
    msg.textContent = "";
    const closeNow = p.partial ? PARTIAL_FRAC * PARTIAL_R + (1 - PARTIAL_FRAC) * rNow : rNow;
    const sorted = Array.from(res.finals).sort((a, b) => a - b);
    const mean = sorted.reduce((s, v) => s + v, 0) / sorted.length;
    const worse = sorted.filter((v) => v < closeNow).length / sorted.length;
    $("#sim-kpis").replaceChildren(
      kpi("Si cierras ahora", fmt.r(closeNow), p.partial ? "70 % ya cobrado + 30 % al precio de ahora" : "a " + fmt.px(p.now), cls(closeNow)),
      kpi("Si mantienes (promedio)", fmt.r(mean), "mediana " + fmt.r(quantile(sorted, 0.5)), cls(mean - closeNow)),
      kpi("Mantener termina peor", fmt.pct(worse), "de los caminos"),
      kpi("Llega al parcial", p.partial ? "ya cobrado" : fmt.pct(res.hitPartial), "1,7R"),
      kpi("Rango probable", fmt.r(quantile(sorted, 0.1), 1) + " a " + fmt.r(quantile(sorted, 0.9), 1), "percentiles 10 y 90"));
    const pct = (qs) => res.fan.map((col) => { const a = Array.from(col).sort((x, y) => x - y); return quantile(a, qs); });
    lineChart($("#sim-fan"), [
      { name: "p90", color: css("--long"), values: pct(0.9), width: 1, dash: true },
      { name: "mediana", color: css("--accent"), values: pct(0.5), width: 2.5 },
      { name: "p10", color: css("--short"), values: pct(0.1), width: 1, dash: true },
      { name: "cerrar ahora", color: css("--muted"), values: res.fan.map(() => closeNow), width: 1 }],
      { band: [pct(0.25), pct(0.75)], xlabels: res.fan.map((_, i) => "día " + Math.floor(i * 12 / res.barsPerDay)), yfmt: (v) => fmt.r(v, 1), zero: true, label: "Camino del R en los caminos simulados" });
    const qlo = quantile(sorted, 0.005), qhi = quantile(sorted, 0.995);
    const width = [0.25, 0.5, 1, 2].find((w) => (qhi - qlo) / w <= 16) || 2;
    const lo = Math.floor(qlo / width) * width, hi = Math.ceil(qhi / width) * width + width;
    const bins = [], labels = [], mids = [];
    for (let b = lo; b < hi - 1e-9; b += width) {
      const top = b + width >= hi - 1e-9 ? Infinity : b + width, bot = b <= lo + 1e-9 ? -Infinity : b;
      bins.push(sorted.filter((v) => v >= bot && v < top).length / sorted.length);
      labels.push(fmt.num(b, width < 1 ? 2 : 0)); mids.push(b + width / 2);
    }
    barChart($("#sim-hist"), labels, bins, { yfmt: (v) => fmt.pct(v), minLabel: 0.03, xEvery: Math.ceil(labels.length / 10),
      colors: mids.map((m) => (m < closeNow ? css("--short") : css("--long"))), label: "Distribución del R final" });
    const better = mean > closeNow + 0.05, worseMean = mean < closeNow - 0.05;
    $("#sim-read").textContent = (better ? "En promedio, mantener deja más que cerrar ahora" : worseMean ? "En promedio, cerrar ahora deja más que mantener" : "En promedio, mantener y cerrar dejan casi lo mismo") +
      " (" + fmt.r(mean) + " contra " + fmt.r(closeNow) + "), pero en el " + fmt.pct(worse) + " de los caminos mantener termina peor. " +
      "Los caminos re-muestrean " + days.length + " días reales recientes de " + p.sym + ": muestran el rango posible, no el precio de mañana. Las reglas de la estrategia son mantener.";
  }, 20);
}

/* ---------------------------------------------------------------- backtest */
function table(t, head, rows, hlIndex) {
  t.replaceChildren(el("thead", {}, row(head)));
  const b = el("tbody");
  rows.forEach((r, i) => { const tr = row(r); if (i === hlIndex) tr.className = "hl"; b.append(tr); });
  t.append(b);
}
function renderBacktest(bt) {
  const c = bt.chosen, m = bt.meta;
  $("#bt-meta").textContent = m.symbols.join(", ") + ", " + m.from + " a " + m.to + ". Costo " + fmt.num(m.cost_pct, 2) + " % por lado. Elegida: " + c.label + ".";
  $("#bt-versions").textContent = m.versions; $("#bt-luck").textContent = fmt.num(m.luck_sharpe, 2);
  $("#bt-kpis").replaceChildren(
    kpi("R por operación", fmt.r(c.mean, 3), "± " + fmt.num(c.se, 3) + " · " + c.n + " operaciones", cls(c.mean)),
    kpi("Sharpe anual", fmt.num(c.sharpe, 2), "1ª mitad " + fmt.r(c.first, 3) + " · 2ª " + fmt.r(c.second, 3)),
    kpi("Peor caída", fmt.num(c.dd, 0) + "R", "con 0,25 % por operación ≈ " + fmt.num(c.dd * 0.25, 0) + " %"),
    kpi("Contra el azar", "p = " + fmt.num(c.p_random, 3), "azar " + fmt.r(c.random_median, 3)),
    kpi("Años en pérdida", fmt.pct(c.mc.y1.loss), "quinquenios " + fmt.pct(c.mc.y5.loss)),
    kpi("Deflated Sharpe", fmt.num(c.dsr, 2), "se pide 0,95", c.dsr >= 0.95 ? "pos" : "warn"));
  const others = bt.equity.others.filter((o) => !/09:00-09:30/.test(o.name)).slice(0, 3);
  const pal = [css("--muted"), css("--warn"), css("--long")];
  const lines = [{ name: "La elegida", color: css("--accent"), values: bt.equity.chosen, width: 2.5 },
    { name: "Entradas al azar", color: css("--muted"), values: bt.equity.random, dash: true, width: 1.5 }]
    .concat(others.map((o, i) => ({ name: o.name.replace(/^(Mejor #\d+|Extra|Estrategia \d): /, ""), color: pal[i], values: o.equity, width: 1 })));
  $("#bt-legend").replaceChildren(...lines.map((l) => el("span", {}, (() => { const i = el("i"); i.style.background = l.color; return i; })(), document.createTextNode(l.name))));
  lineChart($("#bt-equity"), lines, { xlabels: bt.equity.dates, yfmt: (v) => fmt.r(v, 0), zero: true, height: 320, label: "Resultado acumulado de la estrategia" });
  barChart($("#bt-years"), c.years.years, c.years.r, { yfmt: (v) => fmt.r(v, 2), label: "R por operación por año" });
  lineChart($("#bt-costs"), [{ name: "R por operación", color: css("--accent"), values: c.costs.r }],
    { xlabels: c.costs.levels.map((l) => fmt.num(l, 3) + "%"), yfmt: (v) => fmt.r(v, 2), zero: true, label: "R por operación según el costo" });
  const mini = (r) => [fmt.r(r.mean, 3), fmt.num(r.sharpe, 2), fmt.num(r.dd, 0) + "R", fmt.r(r.second, 3)];
  table($("#bt-boxes"), ["Caja", "R/op", "Sharpe", "Caída", "2ª mitad"], bt.boxes.map((r) => [r.box].concat(mini(r))), 1);
  table($("#bt-runner"), ["Resto", "R/op", "Sharpe", "Caída", "2ª mitad"], bt.runner.map((r) => [r.runner].concat(mini(r))), 0);
  table($("#bt-partial"), ["", "R/op", "Sharpe", "Caída", "2ª mitad"], bt.partial.map((r) => [r.partial].concat(mini(r))), 1);
  const top = bt.top.filter((r, i) => !bt.top.slice(0, i).some((o) => o.n === r.n && o.mean === r.mean && o.second === r.second));
  table($("#bt-top"), ["Versión", "n", "R/op", "±", "Sharpe", "Caída", "1ª mitad", "2ª mitad"],
    top.map((r) => [r.label, String(r.n), [fmt.r(r.mean, 3), cls(r.mean)], fmt.num(r.se, 3), fmt.num(r.sharpe, 2), fmt.num(r.dd, 0) + "R", fmt.r(r.first, 3), fmt.r(r.second, 3)]),
    top.findIndex((r) => r.n === c.n && r.mean === c.mean));
}

/* ---------------------------------------------------------------- boot */
async function refreshLive() {
  try { state.today = await getJSON("data/today.json"); renderLive(state.today); }
  catch (e) { $("#updated").textContent = "sin datos en vivo todavía"; }
}
async function boot() {
  showTab((location.hash || "#live").slice(1));
  await refreshLive();
  getJSON("data/paths.json").then((d) => { state.paths = d; }).catch(() => {});
  getJSON("research/backtest.json").then((d) => { state.backtest = d; if (!$("#tab-backtest").hidden) renderBacktest(d); }).catch(() => {});
  let saved = null;
  try { saved = sessionStorage.getItem("dash-pass"); } catch (e) { /* ignore */ }
  if (saved) { $("#remember").checked = true; unlock(saved, true); }
  setInterval(async () => {
    await refreshLive();
    if (state.pass) unlock(state.pass, true);
  }, 60000);
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());
boot();
