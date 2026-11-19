/* Swing: the second dashboard. The swing setups' paper trades, from data/private_swing.json (encrypted with
   the same DASHBOARD_KEY as the box). Shares common.js with index.html. */
"use strict";

const SETUP = { earnings_gap: "Momentum de resultados", breakout20: "Ruptura de 20 días", pullback_ema20: "Pullback a la EMA20",
  rsi2: "RSI(2) sobrevendido", breakdown20: "Quiebre de 20 días", pullback_ema20_short: "Rebote a la EMA20 (corto)",
  rsi2_short: "RSI(2) sobrecomprado (corto)", manual: "Manual" };
const OUTCOME = { target: "objetivo", stop: "stop", time: "por tiempo", replaced: "reemplazada" };
const sw = { doc: null, pass: null };

function showTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + name; });
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

const setupName = (s) => SETUP[s] || s || "-";
const side = (s) => (s === "sell" ? "corto" : "largo");
const nyStamp = (iso) => (iso ? new Date(nyWall(new Date(iso)) * 1000).toISOString().slice(0, 16).replace("T", " ") : "-");

function render(d) {
  $("#trades-body").hidden = false;
  $("#lock").hidden = true;
  const min = Math.round((Date.now() - new Date(d.updated).getTime()) / 60000);
  $("#priv-updated").textContent = "Cuenta leída a las " + nyStamp(d.updated).slice(11) + " NY (" + (min < 1 ? "hace menos de 1 min" : "hace " + min + " min") + ")";
  $("#updated").textContent = "datos " + nyStamp(d.updated) + " NY";
  const T = d.trades || [];
  const open = T.filter((t) => t.status === "open" || t.status === "closing");
  const pending = T.filter((t) => t.status === "submitted");
  const closed = T.filter((t) => t.status === "closed" && t.R != null);
  const wins = closed.filter((t) => t.R > 0).length, totalR = closed.reduce((s, t) => s + t.R, 0);
  // what the study expects for the setups actually traded
  const st = closed.map((t) => (d.study || {})[t.setup]).filter(Boolean);
  const expR = st.length ? st.reduce((s, x) => s + x.R, 0) / st.length : null;
  const expW = st.length ? st.reduce((s, x) => s + x.win, 0) / st.length : null;
  const a = d.account || {}, day = a.equity != null && a.last_equity ? a.equity - a.last_equity : null;
  $("#sw-kpis").replaceChildren(
    kpi("Capital (paper)", fmt.money(a.equity), "la misma cuenta que la caja"),
    kpi("Hoy (toda la cuenta)", fmt.money(day), "", cls(day)),
    kpi("Abiertas", open.length + " de " + (d.max_positions || "-"), pending.length ? pending.length + (pending.length === 1 ? " entrada esperando" : " entradas esperando") : "sin entradas esperando"),
    kpi("Cerradas", String(closed.length), closed.length ? fmt.pct(wins / closed.length) + " ganadoras" + (expW != null ? " · estudio " + fmt.pct(expW) : "") : ""),
    kpi("R por operación", closed.length ? fmt.r(totalR / closed.length, 2) : "-", expR != null ? "estudio " + fmt.r(expR, 2) : "", cls(totalR)),
    kpi("R total", fmt.r(totalR, 2), closed.length && closed.length < 30 ? "menos de 30: muestra chica" : "", cls(totalR)));

  const ob = $("#sw-open tbody"); ob.replaceChildren();
  $("#sw-open-empty").hidden = open.length > 0;
  open.forEach((t) => ob.append(row([t.symbol, setupName(t.setup), side(t.side), fmt.px(t.entry_fill), fmt.px(t.stop), fmt.px(t.target),
    fmt.px(t.last), [fmt.r(t.unrealized_R, 2), cls(t.unrealized_R)], [fmt.money(t.pnl), cls(t.pnl)], t.time_stop_date || "-"])));
  const pb = $("#sw-pending tbody"); pb.replaceChildren();
  $("#sw-pending-empty").hidden = pending.length > 0;
  pending.forEach((t) => pb.append(row([t.symbol, setupName(t.setup), side(t.side), fmt.px(t.limit_price), fmt.px(t.stop), fmt.px(t.target), t.signal_date || "-"])));

  const cb = $("#sw-closed tbody"); cb.replaceChildren();
  $("#sw-closed-empty").hidden = closed.length > 0;
  const byExit = closed.slice().sort((x, y) => ((x.exit_at || "") < (y.exit_at || "") ? -1 : 1));
  byExit.slice().reverse().forEach((t) => cb.append(row([t.signal_date || "-", t.symbol, setupName(t.setup), side(t.side), fmt.px(t.entry_fill),
    fmt.px(t.exit_fill), OUTCOME[t.outcome] || t.outcome || "-", [fmt.r(t.R, 2), cls(t.R)]])));

  if (byExit.length) {
    let c = 0; const cum = byExit.map((t) => (c += t.R));
    lineChart($("#sw-r-chart"), [{ name: "R acumulado", color: css("--accent"), values: [0].concat(cum) }],
      { xlabels: ["inicio"].concat(byExit.map((t) => (t.exit_at || t.signal_date || "").slice(5, 10))), yfmt: (v) => fmt.r(v, 1), zero: true, label: "R acumulado de las operaciones swing cerradas" });
  } else $("#sw-r-chart").replaceChildren(el("p", { class: "muted", text: "Se dibuja con la primera operación cerrada." }));

  const setups = Array.from(new Set(T.map((t) => t.setup).filter(Boolean)));
  const tbl = $("#sw-setups"); tbl.replaceChildren();
  tbl.append(el("thead", {}, row(["Setup", "Cerradas", "Ganadoras", "Estudio", "R/op", "Estudio"])));
  const body = el("tbody");
  setups.forEach((s) => {
    const c = closed.filter((t) => t.setup === s), w = c.filter((t) => t.R > 0).length, r = c.length ? c.reduce((x, t) => x + t.R, 0) / c.length : null;
    const e = (d.study || {})[s];
    body.append(row([setupName(s), String(c.length), c.length ? fmt.pct(w / c.length) : "-", e ? fmt.pct(e.win) : "-",
      [r == null ? "-" : fmt.r(r, 2), cls(r)], e ? fmt.r(e.R, 2) : "-"]));
  });
  if (!setups.length) body.append(row(["Sin operaciones todavía", "", "", "", "", ""]));
  tbl.append(body);
}

async function unlock(pass, quiet) {
  const msg = $("#lock-msg");
  try {
    msg.textContent = "Descifrando...";
    sw.doc = await decryptDoc(await getJSON("data/private_swing.json"), pass);
    msg.textContent = "";
    sw.pass = pass;
    if ($("#remember").checked) try { sessionStorage.setItem("dash-pass", pass); } catch (e) { /* storage blocked */ }
    render(sw.doc);
  } catch (e) {
    if (!quiet) msg.textContent = /404/.test(e.message) ? "Todavía no hay datos del swing publicados (la próxima publicación del panel los agrega)." : e.message === "Frase incorrecta" ? "Frase incorrecta." : "No se pudo leer: " + e.message;
  }
}
$("#unlock-form").addEventListener("submit", (ev) => { ev.preventDefault(); unlock($("#pass").value); });
$("#priv-refresh").addEventListener("click", () => { if (sw.pass) { $("#priv-updated").textContent = "Leyendo..."; unlock(sw.pass, true); } });
$("#relock").addEventListener("click", () => {
  sw.doc = null; sw.pass = null;
  try { sessionStorage.removeItem("dash-pass"); } catch (e) { /* ignore */ }
  $("#trades-body").hidden = true; $("#lock").hidden = false; $("#pass").value = "";
});

(function boot() {
  showTab((location.hash || "#trades").slice(1));
  let saved = null;
  try { saved = sessionStorage.getItem("dash-pass"); } catch (e) { /* ignore */ }
  if (saved) { $("#remember").checked = true; unlock(saved, true); }
  setInterval(() => { if (sw.pass) unlock(sw.pass, true); }, 60000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && sw.pass) unlock(sw.pass, true); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());
})();
