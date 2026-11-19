/* Shared by both dashboards (index.html: the 09:00 box, swing.html: the swing setups): helpers, SVG charts,
   the decryption of the account files. Plain JavaScript, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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

const NYF = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hourCycle: "h23", year: "numeric",
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
function nyWall(date) {
  // New York wall-clock time as a Unix epoch (the charts' clock, like today.json)
  const p = {}; NYF.formatToParts(date).forEach((x) => { p[x.type] = x.value; });
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) / 1000;
}
