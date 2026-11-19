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
  if (name === "trades" && state.priv) renderPrivate(state.priv);
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
    else if (d.box.forming) st.append(el("span", { class: "muted", text: "Caja formándose hasta las 10:00." }));
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
    if (ch.fittedFor !== today.session) { ch.chart.timeScale().fitContent(); ch.fittedFor = today.session; }
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
function ageText(iso) {
  if (!iso) return "";
  const t = new Date(iso), min = Math.round((Date.now() - t.getTime()) / 60000);
  const ny = new Date(nyWall(t) * 1000).toISOString().slice(11, 16);
  return "Cuenta leída a las " + ny + " NY (" + (min < 1 ? "hace menos de 1 min" : "hace " + min + " min") + ")";
}
function privateFreshness() {
  if (!state.priv) return;
  $("#priv-updated").textContent = ageText(state.priv.updated) + (live.on && live.last ? " · precios y R en vivo" : " · se revisa cada minuto");
}
function renderPrivate(p) {
  $("#trades-body").hidden = false;
  $("#lock").hidden = true;
  privateFreshness();
  const a = p.account || {};
  const closed = p.trades.filter((t) => !t.open && t.realized_R != null);
  // R already in the pocket: closed trades and the parts of open ones (the 70% at 1.7R)
  const banked = p.trades.filter((t) => t.realized_R != null && (t.exits || []).length);
  const totalR = banked.reduce((s, t) => s + t.realized_R, 0);
  const totalPnl = banked.reduce((s, t) => s + (t.realized_pnl || 0), 0);
  const wins = closed.filter((t) => t.realized_R > 0).length;
  const dayChg = a.equity != null && a.last_equity ? a.equity - a.last_equity : null;
  const k = $("#acct-kpis");
  k.replaceChildren(
    kpi("Capital (paper)", fmt.money(a.equity), "efectivo " + fmt.money(a.cash)),
    kpi("Hoy", fmt.money(dayChg), "", cls(dayChg)),
    kpi("Abiertas", String(p.trades.filter((t) => t.open).length), "operaciones de la caja"),
    kpi("Cerradas", String(closed.length), closed.length ? fmt.pct(wins / closed.length) + " ganadoras" : ""),
    kpi("R cobrado", fmt.r(totalR, 2), (banked.length ? fmt.money(totalPnl) + " en " + banked.length + (banked.length === 1 ? " operación" : " operaciones") : "nada cobrado todavía") +
      (closed.length ? " · " + fmt.r(closed.reduce((s, t) => s + t.realized_R, 0) / closed.length, 3) + " por cerrada" : ""), cls(totalR)));
  const ob = $("#open-table tbody"); ob.replaceChildren();
  const open = p.trades.filter((t) => t.open);
  $("#open-empty").hidden = open.length > 0;
  const today = (state.today && state.today.session) || new Date().toISOString().slice(0, 10);
  open.forEach((t) => {
    const days = businessDaysBetween(t.day, today);
    if (live.on && live.last[t.symbol] != null) {          // connected: the price and R of this second
      t.last = live.last[t.symbol];
      if (t.stop && t.entry !== t.stop) t.unrealized_R = (t.last - t.entry) * t.dir / Math.abs(t.entry - t.stop);
    }
    const btn = el("button", { class: "btn small", type: "button", text: "Simular" });
    btn.addEventListener("click", () => {
      fillSim({ sym: t.symbol, dir: t.dir, entry: t.entry, stop: t.stop, now: t.last, days, partial: t.partial_done });
      showTab("sim"); runSim();
    });
    const got = (t.exits || []).length ? [fmt.r(t.realized_R, 2), cls(t.realized_R)] : "-";
    ob.append(row([t.symbol, t.side, fmt.px(t.entry), fmt.px(t.stop), fmt.px(t.last),
      [fmt.r(t.unrealized_R), cls(t.unrealized_R)], t.partial_done ? "cobrado" : "pendiente", got, String(days), btn]));
  });
  // every exit that filled, open trades included
  const KIND = { tp: "70 % a 1,7R", "tp-leg": "stop (70 %)", run: "stop (30 %)", "run-be": "breakeven (30 %)",
    "run-be2": "breakeven (30 %)" };
  const exits = [];
  p.trades.forEach((t) => (t.exits || []).forEach((x) => {
    const risk = t.stop && t.entry ? Math.abs(t.entry - t.stop) : null, part = t.qty ? x.qty / t.qty : null;
    const pnl = x.price != null ? (x.price - t.entry) * t.dir * x.qty : null;
    exits.push({ time: x.time || "", sym: t.symbol, side: t.side, kind: KIND[x.kind] || (x.kind === "close" ? "cierre" : x.kind || "salida"),
      part, price: x.price, pnl, r: risk && part != null && x.price != null ? (x.price - t.entry) * t.dir / risk * part : null });
  }));
  exits.sort((a, b) => (a.time < b.time ? 1 : -1));
  const eb = $("#exits-table tbody"); eb.replaceChildren();
  $("#exits-empty").hidden = exits.length > 0;
  exits.forEach((x) => eb.append(row([x.time ? new Date(nyWall(new Date(x.time)) * 1000).toISOString().slice(0, 16).replace("T", " ") : "-",
    x.sym, x.side, x.kind, x.part != null ? fmt.pct(x.part) : "-", fmt.px(x.price), [fmt.money(x.pnl), cls(x.pnl)], [fmt.r(x.r, 2), cls(x.r)]])));
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
$("#priv-refresh").addEventListener("click", async () => {
  if (!state.pass) return;
  $("#priv-updated").textContent = "Leyendo...";
  await unlock(state.pass, true);
  privateFreshness();
});
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

/* ---------------------------------------------------------------- live link to Alpaca (browser only) */
// With the user's PAPER keys the page talks straight to Alpaca: the IEX market data stream (1 minute bars and
// trades, folded into 5 minute candles here), the paper account (REST every 10 s) and its trade updates
// stream. The keys stay in this browser (sessionStorage, or localStorage if asked) and go only to Alpaca.
const ALPACA = { data: "https://data.alpaca.markets", stream: "wss://stream.data.alpaca.markets/v2/iex",
  paper: "https://paper-api.alpaca.markets", trades: "wss://paper-api.alpaca.markets/stream" };
const live = { on: false, keys: null, bars: {}, last: {}, ws: null, tws: null, timer: null, poll: null, dirty: false,
  session: null, fills: [], stops: {}, retry: 0 };
const NYF = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hourCycle: "h23", year: "numeric",
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
function nyWall(date) {
  // New York wall-clock time as a Unix epoch (the charts' clock, like today.json)
  const p = {}; NYF.formatToParts(date).forEach((x) => { p[x.type] = x.value; });
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) / 1000;
}
const wallDay = (w) => new Date(w * 1000).toISOString().slice(0, 10);
const wallMin = (w) => Math.floor((w % 86400) / 60);
function foldBar(sym, w, o, h, l, c) {
  const k = Math.floor(w / 300) * 300, m = live.bars[sym] || (live.bars[sym] = new Map());
  const b = m.get(k);
  if (!b) m.set(k, [k, o, h, l, c]);
  else { b[2] = Math.max(b[2], h); b[3] = Math.min(b[3], l); b[4] = c; }
}
function boxAndSignal(bars, nowW) {
  // same rules as the bot (orb_paper.box_of / first_signal): box 09:00-10:00 with at least 3 bars, the first
  // completed bar from 10:00 to 12:55 that closes outside it
  const inside = bars.filter((b) => wallMin(b[0]) >= 540 && wallMin(b[0]) < 600);
  if (inside.length < 3) return { box: null, signal: null };
  const hi = Math.max(...inside.map((b) => b[2])), lo = Math.min(...inside.map((b) => b[3]));
  const box = { high: hi, low: lo, bars: inside.length, forming: wallMin(nowW) < 600 };
  let signal = null;
  for (const b of bars) {
    const m = wallMin(b[0]);
    if (m < 600 || m > 775) continue;
    if (b[0] + 300 > nowW) break;
    const dir = b[4] > hi ? 1 : b[4] < lo ? -1 : 0;
    if (dir) {
      const stop = dir > 0 ? lo : hi;
      signal = { time: b[0] + 300, dir, close: b[4], stop, partial: Math.round((b[4] + dir * PARTIAL_R * Math.abs(b[4] - stop)) * 100) / 100 };
      break;
    }
  }
  return { box, signal };
}
function liveModel() {
  const nowW = nyWall(new Date()), day = wallDay(nowW), wd = new Date(nowW * 1000).getUTCDay(), m = wallMin(nowW);
  const model = { session: day, now_ny: new Date(nowW * 1000).toISOString().slice(0, 19).replace("T", " "),
    market_open: wd > 0 && wd < 6 && m >= 570 && m < 960, symbols: {}, live: true };
  SYMS.forEach((sym) => {
    const bars = Array.from((live.bars[sym] || new Map()).values()).filter((b) => wallDay(b[0]) === day).sort((a, b) => a[0] - b[0]);
    const { box, signal } = boxAndSignal(bars, nowW);
    model.symbols[sym] = { bars, box, signal, last: live.last[sym] != null ? live.last[sym] : bars.length ? bars[bars.length - 1][4] : null };
  });
  return model;
}
function liveRender() {
  if (!live.on) return;
  live.dirty = false;
  state.today = liveModel();
  renderLive(state.today);
  $("#updated").textContent = "en vivo " + state.today.now_ny.slice(11) + " NY";
  renderPositions();
  if (state.priv && !$("#tab-trades").hidden) renderPrivate(state.priv);
}
const scheduleRender = () => { if (!live.dirty) { live.dirty = true; setTimeout(liveRender, 1000); } };
function setStatus(text, kind) {
  const p = $("#live-status"); p.textContent = text; p.className = "pill " + (kind || "");
}
function seedFromToday(today) {
  // the published bars (up to ~5 minutes old) are the start; the stream adds to them
  if (!today || !today.symbols) return;
  SYMS.forEach((sym) => (today.symbols[sym] && today.symbols[sym].bars || []).forEach((b) => foldBar(sym, b[0], b[1], b[2], b[3], b[4])));
}
async function alpaca(base, path) {
  const r = await fetch(base + path, { headers: { "APCA-API-KEY-ID": live.keys.id, "APCA-API-SECRET-KEY": live.keys.secret } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
async function backfill() {
  // today's 5 minute bars from 08:30 New York, so the chart has no gap before the stream started
  const nowW = nyWall(new Date()), start = new Date((Math.floor(nowW / 86400) * 86400 + 510 * 60) * 1000);
  const offset = (nowW - Date.now() / 1000);                 // wall clock minus UTC, in seconds
  const iso = new Date(start.getTime() - offset * 1000).toISOString();
  try {
    const d = await alpaca(ALPACA.data, "/v2/stocks/bars?symbols=" + SYMS.join(",") + "&timeframe=5Min&feed=iex&limit=1000&start=" + encodeURIComponent(iso));
    Object.entries(d.bars || {}).forEach(([sym, bs]) => bs.forEach((b) => foldBar(sym, nyWall(new Date(b.t)), b.o, b.h, b.l, b.c)));
    return true;
  } catch (e) { return false; }
}
function openMarketStream() {
  const ws = live.ws = new WebSocket(ALPACA.stream);
  ws.onopen = () => ws.send(JSON.stringify({ action: "auth", key: live.keys.id, secret: live.keys.secret }));
  ws.onmessage = async (ev) => {
    const text = typeof ev.data === "string" ? ev.data : await new Response(ev.data).text();
    let msgs; try { msgs = JSON.parse(text); } catch (e) { return; }
    (Array.isArray(msgs) ? msgs : [msgs]).forEach((m) => {
      if (m.T === "success" && m.msg === "authenticated") {
        ws.send(JSON.stringify({ action: "subscribe", bars: SYMS, updatedBars: SYMS, trades: SYMS }));
        live.retry = 0;
        setStatus("en vivo", "live");
      } else if (m.T === "error") {
        $("#live-msg").textContent = m.code === 406 ? "Alpaca permite una sola conexión de datos a la vez en el plan gratis: cierra la otra pestaña o programa que la use."
          : m.code === 402 || m.code === 401 ? "Alpaca rechazó las claves (¿son las de la cuenta paper?)." : "Alpaca: " + (m.msg || m.code);
        if (m.code === 402 || m.code === 401) disconnect(true);
      } else if ((m.T === "b" || m.T === "u") && SYMS.includes(m.S)) {
        foldBar(m.S, nyWall(new Date(m.t)), m.o, m.h, m.l, m.c); live.last[m.S] = m.c; scheduleRender();
      } else if (m.T === "t" && SYMS.includes(m.S)) {
        const w = nyWall(new Date(m.t)); foldBar(m.S, w, m.p, m.p, m.p, m.p); live.last[m.S] = m.p; scheduleRender();
      }
    });
  };
  ws.onclose = () => {
    if (!live.on || live.ws !== ws) return;
    setStatus("reconectando", "warn");
    live.retry = Math.min(live.retry + 1, 6);
    setTimeout(() => { if (live.on) openMarketStream(); }, 1000 * Math.pow(2, live.retry));
  };
}
function openTradeStream() {
  const ws = live.tws = new WebSocket(ALPACA.trades);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ action: "auth", key: live.keys.id, secret: live.keys.secret }));
  ws.onmessage = async (ev) => {
    const text = typeof ev.data === "string" ? ev.data : new TextDecoder().decode(ev.data instanceof ArrayBuffer ? ev.data : await new Response(ev.data).arrayBuffer());
    let m; try { m = JSON.parse(text); } catch (e) { return; }
    if (m.stream === "authorization" && m.data && m.data.status === "authorized") ws.send(JSON.stringify({ action: "listen", data: { streams: ["trade_updates"] } }));
    if (m.stream === "trade_updates" && m.data && m.data.order) {
      const o = m.data.order, ev2 = m.data.event;
      if (["fill", "partial_fill", "canceled", "rejected", "expired"].includes(ev2)) {
        const t = new Date(m.data.timestamp || Date.now());
        live.fills.unshift(new Date(nyWall(t) * 1000).toISOString().slice(11, 19) + " · " + o.symbol + " " +
          ({ fill: "ejecutada", partial_fill: "ejecución parcial", canceled: "cancelada", rejected: "rechazada", expired: "vencida" })[ev2] +
          ": " + (o.side === "buy" ? "compra" : "venta") + " " + (m.data.qty || o.filled_qty || o.qty || "") + " " + o.type +
          (m.data.price ? " a " + fmt.px(+m.data.price) : ""));
        live.fills = live.fills.slice(0, 30);
        pollAccount();
      }
    }
  };
  ws.onclose = () => { if (live.on && live.tws === ws) setTimeout(() => { if (live.on) openTradeStream(); }, 5000); };
}
function stopOf(sym, entry, dir, orders) {
  // the trade's original stop: today's signal, the decrypted record, or the open stop order on the closing side
  // (not a runner already moved to breakeven)
  if (state.priv) { const t = state.priv.trades.find((x) => x.open && x.symbol === sym); if (t && t.stop) return t.stop; }
  for (const o of orders) for (const x of [o].concat(o.legs || [])) {
    const side = dir > 0 ? "sell" : "buy", cid = x.client_order_id || o.client_order_id || "";
    if ((x.symbol || o.symbol) === sym && x.type === "stop" && x.side === side && x.stop_price && !/run-be/.test(cid)
        && Math.abs(+x.stop_price - entry) > entry * 1e-4) return +x.stop_price;
  }
  const d = state.today && state.today.symbols && state.today.symbols[sym];
  if (d && d.signal && d.signal.dir === dir) return d.signal.stop;
  return null;
}
async function pollAccount() {
  if (!live.on) return;
  try {
    const [acct, pos, orders] = await Promise.all([alpaca(ALPACA.paper, "/v2/account"), alpaca(ALPACA.paper, "/v2/positions"),
      alpaca(ALPACA.paper, "/v2/orders?status=open&limit=500&nested=true")]);
    live.acct = acct; live.positions = pos; live.orders = orders;
    $("#acct-live").hidden = false;
    $("#acct-live-time").textContent = "actualizada " + new Date(nyWall(new Date()) * 1000).toISOString().slice(11, 19) + " NY";
    renderPositions();
  } catch (e) {
    $("#live-msg").textContent = "No se pudo leer la cuenta paper desde el navegador (" + e.message + "). Las velas en vivo siguen; la cuenta se ve en Mis operaciones.";
  }
}
function renderPositions() {
  if (!live.acct) return;
  const a = live.acct, eq = +a.equity, day = a.last_equity ? eq - +a.last_equity : null;
  const pos = live.positions || [];
  let upl = 0;
  const rows = pos.map((p) => {
    const q = +p.qty, dir = q > 0 ? 1 : -1, entry = +p.avg_entry_price;
    const now = live.last[p.symbol] != null ? live.last[p.symbol] : +p.current_price;
    const pnl = (now - entry) * q; upl += pnl;
    const stop = stopOf(p.symbol, entry, dir, live.orders || []);
    const r = stop != null && stop !== entry ? (now - entry) * dir / Math.abs(entry - stop) : null;
    const btn = SYMS.includes(p.symbol) && stop != null ? el("button", { class: "btn small", type: "button", text: "Simular" }) : "";
    if (btn) btn.addEventListener("click", () => {
      const t = state.priv && state.priv.trades.find((x) => x.open && x.symbol === p.symbol);
      fillSim({ sym: p.symbol, dir, entry, stop, now, days: t ? businessDaysBetween(t.day, state.today.session) : 0, partial: t ? t.partial_done : false });
      showTab("sim"); runSim();
    });
    return row([p.symbol, dir > 0 ? "largo" : "corto", String(Math.abs(q)), fmt.px(entry), fmt.px(now), [fmt.money(pnl), cls(pnl)],
      r == null ? "-" : [fmt.r(r, 2), cls(r)], btn]);
  });
  $("#acct-live-kpis").replaceChildren(kpi("Capital (paper)", fmt.money(eq), "efectivo " + fmt.money(+a.cash)),
    kpi("Hoy", fmt.money(day), "", cls(day)), kpi("Abiertas", String(pos.length), "todas las de la cuenta"),
    kpi("P&L abierto", fmt.money(upl), "con el último precio", cls(upl)));
  $("#pos-live tbody").replaceChildren(...rows);
  $("#pos-live-empty").hidden = pos.length > 0;
  const f = $("#fills-live");
  f.replaceChildren(...(live.fills.length ? live.fills.map((t) => el("li", { text: t })) : [el("li", { class: "muted", text: "Sin ejecuciones desde que te conectaste." })]));
}
function loadKeys() {
  for (const store of [sessionStorage, localStorage]) {
    try { const k = JSON.parse(store.getItem("alpaca-paper-keys") || "null"); if (k && k.id && k.secret) return k; } catch (e) { /* storage blocked */ }
  }
  return null;
}
async function connect(keys, remember) {
  if (!/^PK/.test(keys.id)) { $("#live-msg").textContent = "Esa no parece una clave paper (las de paper empiezan con PK). Por seguridad este panel solo usa la cuenta paper."; return; }
  disconnect(false);
  live.on = true; live.keys = keys; live.bars = {}; live.last = {}; live.fills = [];
  try {
    sessionStorage.setItem("alpaca-paper-keys", JSON.stringify(keys));
    if (remember) localStorage.setItem("alpaca-paper-keys", JSON.stringify(keys)); else localStorage.removeItem("alpaca-paper-keys");
  } catch (e) { /* storage blocked: works for this visit only */ }
  $("#live-form").hidden = true; $("#live-on").hidden = false; $("#live-msg").textContent = "";
  $("#live-info").textContent = "Conectado con la clave " + keys.id.slice(0, 4) + "…" + keys.id.slice(-3) + (remember ? " (recordada en este navegador)" : " (solo esta pestaña)");
  setStatus("conectando", "warn");
  seedFromToday(state.today && !state.today.live ? state.today : null);
  const filled = await backfill();
  if (!filled) $("#live-msg").textContent = "Sin historial de Alpaca desde el navegador: se parte de los datos publicados y se completa en vivo.";
  liveRender();
  openMarketStream();
  openTradeStream();
  pollAccount();
  live.poll = setInterval(pollAccount, 10000);
  live.timer = setInterval(scheduleRender, 15000);   // close forming bars and move the clock even when nothing trades
}
function disconnect(forget) {
  live.on = false;
  [live.ws, live.tws].forEach((w) => { if (w) try { w.close(); } catch (e) { /* closed */ } });
  live.ws = live.tws = null;
  clearInterval(live.poll); clearInterval(live.timer);
  live.acct = null;
  if (forget) try { sessionStorage.removeItem("alpaca-paper-keys"); localStorage.removeItem("alpaca-paper-keys"); } catch (e) { /* ignore */ }
  $("#live-form").hidden = false; $("#live-on").hidden = true; $("#acct-live").hidden = true;
  setStatus("cada 5 min", "");
}
$("#live-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  connect({ id: $("#lk-id").value.trim(), secret: $("#lk-secret").value.trim() }, $("#lk-remember").checked);
  $("#lk-secret").value = "";
});
$("#live-off").addEventListener("click", () => { disconnect(true); refreshLive(); });

/* ---------------------------------------------------------------- boot */
async function refreshLive() {
  if (live.on) return;                       // the stream is fresher than the published file
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
  const keys = loadKeys();
  if (keys) connect(keys, (() => { try { return !!localStorage.getItem("alpaca-paper-keys"); } catch (e) { return false; } })());
  setInterval(async () => {
    await refreshLive();
    if (state.pass) unlock(state.pass, true);
  }, 60000);
  setInterval(privateFreshness, 20000);
  // a phone puts background tabs to sleep: catch up as soon as the page is visible again
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    refreshLive();
    if (state.pass) unlock(state.pass, true);
  });
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());
boot();
