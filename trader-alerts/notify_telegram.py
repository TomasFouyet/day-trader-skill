#!/usr/bin/env python3
"""Telegram notice for swing setups that passed trader-risk. Standard library only.

    python3 notify_telegram.py --dry-run         # print the messages, send nothing, remember nothing
    python3 notify_telegram.py                   # send one message per new setup that passed risk
    python3 notify_telegram.py --test            # one test message, to check the bot and the chat

Reads trader/state/swing_risk.json (trader-risk check.py --swing) and the
tickets in trader/swing_orders.json (trader-budget). One message per ticket
with a PASS from TODAY's risk check whose ticket is still exactly the one that
was checked (same fingerprint): symbol, entry, stop, target, suggested size,
time stop, the Time Machine's odds and verdict, the real history count and
the earnings warnings. A setup is sent once: the symbol, signal day and setup
are remembered in trader/state/telegram.json and never sent again.

Credentials come ONLY from the environment:
  TELEGRAM_BOT_TOKEN   the token @BotFather gives you
  TELEGRAM_CHAT_ID     your chat id (send /start to the bot, then see README)
They are never printed, written to a file or put in a message.

It sends messages. It never buys, sells or talks to a broker: the tickets are
for you to read, then buy by hand if you want. Not financial advice.
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot%s/sendMessage"
MAX_LEN = 4000
KEEP_DAYS = 60
UA = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"
FOOT = "Simulación, no predicción. No es asesoría financiera. Nada se operó: decides tú."


class NotifyError(Exception):
    pass


# ---------------------------------------------------------------- New York time (no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def parse_now(text):
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:   # New York wall clock
        for off in (-4, -5):
            utc = t - datetime.timedelta(hours=off)
            if eastern_offset(utc) == off:
                return utc
        return t + datetime.timedelta(hours=5)
    return (t - t.utcoffset()).replace(tzinfo=None)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise NotifyError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise NotifyError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)


def swing_fingerprint(t):
    """The same function as trader-risk check.py's swing_fingerprint: the exact ticket that was checked."""
    core = {k: t.get(k) for k in ("symbol", "side", "qty", "type", "entry", "limit_price", "stop", "target",
                                   "signal_date", "time_stop_date") if t.get(k) is not None}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def key_of(t):
    return "%s|%s|%s" % (str(t.get("symbol") or "").upper(), t.get("signal_date"), t.get("setup") or "")


# ---------------------------------------------------------------- the messages

def _m(x):
    return "${:,.2f}".format(float(x))


def _q(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


def _pc(x):
    return "%.1f %%" % (100.0 * float(x))


SETUP_ES = {"earnings_gap": "Momentum de resultados (salto fuerte tras el reporte)", "breakout20": "Ruptura del máximo de 20 días", "pullback_ema20": "Pullback a la EMA20 en tendencia alcista",
            "rsi2": "RSI(2) sobrevendido en tendencia alcista", "breakdown20": "Quiebre del mínimo de 20 días",
            "pullback_ema20_short": "Rebote a la EMA20 en tendencia bajista",
            "rsi2_short": "RSI(2) sobrecomprado en tendencia bajista", "manual": "Setup manual"}
VENUE_NAME = {"fintual": "Fintual", "bitunix": "Bitunix", "manual": "Tu app"}


GAP_STUDY = ("Estudio 2005-2026 (17.024 reportes del SEC): +0,27R por operación sobre entradas al azar, "
             "irregular por época (2020-22 perdió)")


def gap_lines(t):
    """earnings_gap: the jump and its SEC confirmation, and what the study found (an honest yardstick)."""
    if t.get("setup") != "earnings_gap":
        return []
    rx, er = t.get("reaction") or {}, t.get("earnings") or {}
    out = []
    if rx.get("reaction_pct") is not None:
        out.append("📈 Salto del %s: <b>%+.1f%%</b>%s con volumen <b>%sx</b> su promedio" % (
            _d(rx.get("reaction_date")), float(rx["reaction_pct"]),
            " sobre el S&amp;P 500" if rx.get("vs") == "SPY" else "", ("%.1f" % float(rx.get("volume_ratio") or 0))))
    if er.get("confirmed"):
        out.append("✅ Resultados confirmados: %s" % esc(er["confirmed"]))
    out.append("<i>%s</i>" % esc(GAP_STUDY))
    return out


def esc(x):
    return html.escape(str(x), quote=False)


def _d(iso):
    """2026-11-12 -> 12-11-2026."""
    s = str(iso or "")
    return "%s-%s-%s" % (s[8:10], s[5:7], s[:4]) if len(s) >= 10 else s


def _pct_move(a, b):
    return "%+.1f%%" % (100.0 * (float(b) / float(a) - 1))


def venue_line(v, primary):
    name = VENUE_NAME.get(v.get("venue"), str(v.get("venue")).capitalize())
    mark = " ⭐" if v.get("venue") == primary else ""
    if v.get("refusal"):
        return "▸ %s: <i>%s</i>" % (esc(name), esc(v["refusal"]))
    if not v.get("leveraged"):
        return "▸ %s 1x: <b>%s acciones</b> = <b>%s</b> (%s%% de tu cuenta, %s%% de tu capital ahí)%s" % (
            esc(name), _q(v["qty"]), _m(v.get("cost") or 0), v.get("margin_pct_of_account", "?"),
            v.get("margin_pct_of_venue"), mark)
    ok = "✅" if v.get("liquidation") is not None else ""
    return ("▸ %s <b>%dx</b>: <b>%s u.</b> · nocional %s · margen <b>%s</b> (%s%% de tu cuenta, %s%% de tu "
            "capital ahí) · liquidación ~%s %s%s" % (
                esc(name), int(v.get("leverage") or 1), _q(v["qty"]), _m(v["notional"]), _m(v["margin"]),
                v.get("margin_pct_of_account", "?"), v.get("margin_pct_of_venue"), _m(v["liquidation"]), ok, mark))


PM_ICON = {"ok": "\u2705", "warn": "\u26a0", "invalid": "\u274c", "unknown": "\u2754"}


def apply_premarket(t):
    """-> (ticket as seen from the pre-market price, the premarket block or None). A valid pre-market
    check moves the entry, the size, the max loss and the reward:risk; the stop and target stay."""
    pm = t.get("premarket")
    if not isinstance(pm, dict) or pm.get("status") not in ("ok", "warn") or pm.get("price") is None:
        return t, pm
    t2 = dict(t, entry=pm["price"], qty=pm.get("qty", t["qty"]), max_loss=pm.get("max_loss", t.get("max_loss")),
              reward_risk=pm.get("reward_risk", t.get("reward_risk")))
    t2["cost"] = round(float(t2["qty"]) * float(pm["price"]), 2)
    venues = {}
    for name, v in (t.get("venues") or {}).items():
        nv = dict(v)
        upd = (pm.get("venues") or {}).get(name)
        if upd:
            nv.update(upd)
            if not v.get("leveraged"):
                nv["cost"] = upd.get("notional", nv.get("cost"))
                nv["margin"] = nv["cost"]
            acct = float(t.get("account") or 0)
            if acct and nv.get("margin") is not None:
                nv["margin_pct_of_account"] = round(100.0 * float(nv["margin"]) / acct, 1)
            cap = float(v.get("capital") or 0)
            if cap and nv.get("margin") is not None:
                nv["margin_pct_of_venue"] = round(100.0 * float(nv["margin"]) / cap, 1)
        venues[name] = nv
    t2["venues"] = venues
    return t2, pm


def premarket_line(pm, short):
    if not pm:
        return None
    icon = PM_ICON.get(pm.get("status"), "")
    if pm.get("price") is None:
        return "\U0001F305 Pre-mercado: %s sin dato; vale el plan con el cierre de ayer" % icon
    word = {"ok": "sigue válido", "warn": "válido, con gap", "invalid": "NO entrar"}.get(pm["status"], "")
    return "\U0001F305 %s <b>%s</b> %s ET: <b>%s</b> (%+.1f%% vs cierre) %s %s" % (
        "Pre-mercado" if pm.get("phase") == "pre" else "Precio", "", pm.get("at") or "", _m(pm["price"]),
        float(pm.get("gap_pct") or 0), icon, word)


def message(t, result):
    t, pm = apply_premarket(t)
    short = str(t.get("side") or "buy") == "sell"
    sym = esc(t["symbol"])
    head = "%s <b>%s · %s</b>" % ("🔴" if short else "🟢", "SHORT" if short else "LONG", sym)
    if t.get("asset"):
        head = head.replace("<b>%s · " % ("SHORT" if short else "LONG"), "\U0001F6E2 <b>%s · " % ("SHORT" if short else "LONG"), 1)
        head += " · <b>%s</b>" % esc(t["asset"])
    if t.get("name"):
        head += " · %s" % esc(t["name"])
    rr = ("%.2f" % float(t.get("reward_risk") or 0)).rstrip("0").rstrip(".")
    lines = [head,
             "<i>%s · señal del %s (al cierre)</i>" % (esc(SETUP_ES.get(t.get("setup"), t.get("setup") or "")),
                                                         _d(t.get("signal_date"))),
             "",
             "📍 <b>Operación</b> (%s)" % ("vender en corto" if short else "comprar"),
             "<code>Entrada  %s</code>%s" % (_m(t["entry"]), (" pre-mercado (cierre %s)" % _m(pm["close"]))
                                             if pm and pm.get("status") in ("ok", "warn") else ""),
             "<code>Stop     %s  (%s)</code>" % (_m(t["stop"]), _pct_move(t["entry"], t["stop"])),
             ("<code>Objetivo %s  (%s) %sR</code> <i>lejano: la salida real es el stop o el tiempo</i>"
              if t.get("setup") == "earnings_gap" else "<code>Objetivo %s  (%s) %sR</code>") % (
                 _m(t["target"]), _pct_move(t["entry"], t["target"]), rr),
             "⏳ Cierre por tiempo: <b>%s</b> (%s días hábiles)" % (_d(t.get("time_stop_date")),
                                                                     t.get("time_stop_days") or "?"),
             ""]
    pl = premarket_line(pm, short)
    if pl:
        lines[3:3] = [pl.replace("<b></b> ", ""), ""]
        if pm.get("status") == "warn" and pm.get("note"):
            lines.insert(4, "<i>%s</i>" % esc(pm["note"]))
    gap = gap_lines(t)
    if gap:
        lines[2:2] = [""] + gap
    risk_usd = t.get("risk_usd") or t.get("max_loss")
    acct = t.get("account")
    lines.append("💰 <b>Tamaño</b> · riesgo máx. <b>%s</b>%s" % (
        _m(risk_usd or 0), (" (%.1f%% de %s)" % (100.0 * float(risk_usd) / float(acct), _m(acct))) if acct and risk_usd else ""))
    venues = t.get("venues") or {}
    if venues and (len(venues) > 1 or "manual" not in venues):
        for v in venues.values():
            lines.append(venue_line(v, t.get("venue")))
        lines.append("Pérdida si toca el stop: <b>%s</b>" % _m(t.get("max_loss") or 0))
    else:
        lines.append("▸ <b>%s acciones</b> (~%s) · pérdida si toca el stop <b>%s</b>" % (
            _q(t["qty"]), _m(t.get("cost") or float(t["qty"]) * float(t["entry"])), _m(t.get("max_loss") or 0)))
    m = t.get("timemachine") or {}
    lines.append("")
    if m.get("p_target") is not None and t.get("setup") == "earnings_gap":
        lines.append("🎲 <b>Time Machine</b> (%s días): toca el stop <b>%s</b> · sigue abierta al final <b>%s</b>" % (
            m.get("horizon_days") or t.get("time_stop_days"), _pc(m["p_stop"]), _pc(m["p_unresolved"])))
        lines.append("<i>La simulación no trae la deriva post-resultados: mide el riesgo, no la ventaja. "
                     "La ventaja es la del estudio.</i>")
    elif m.get("p_target") is not None:
        lines.append("🎲 <b>Time Machine</b> (%s trayectorias, %s días)" % (
            "{:,}".format(int(m.get("paths") or 0)).replace(",", "."), m.get("horizon_days") or t.get("time_stop_days")))
        lines.append("Objetivo <b>%s</b> · Stop <b>%s</b> · Abierta al final <b>%s</b>" % (
            _pc(m["p_target"]), _pc(m["p_stop"]), _pc(m["p_unresolved"])))
        hist = ("Historial real: %d casos%s" % (m["history_cases"], " (pocos)" if m.get("few_cases") else "")
                if m.get("history_cases") is not None else "")
        lines.append("Expectativa <b>%+.2fR</b>%s" % (float(m.get("expectancy_R") or 0), (" · " + hist) if hist else ""))
        wins = []
        if m.get("p_win") is not None:
            wins.append("<b>%s</b> en la simulación" % _pc(m["p_win"]))
        if m.get("history_win_rate") is not None and m.get("history_cases"):
            wins.append("<b>%s</b> en el historial real" % _pc(m["history_win_rate"]))
        if wins:
            lines.append("\U0001F3AF Winrate estimado: %s" % " · ".join(wins))
        if m.get("verdict"):
            v = m["verdict"].split(":")[0]
            if float(m.get("p_unresolved") or 0) > 0.6:
                v += "; la mayoría se decide al cierre por tiempo"
            lines.append("→ %s" % esc(v))
    else:
        lines.append("🎲 Time Machine: sin simulación para este ticket.")
    er = t.get("earnings") or {}
    lines.append("")
    if er.get("flag"):
        lines.append("📅 ⚠ <b>%s</b> (%s)" % (esc(er["flag"]), esc(_d(er.get("next")) or "fecha desconocida")))
    elif er.get("no_earnings"):
        lines.append("\U0001F4C5 Sin resultados trimestrales (materia prima)")
    elif er.get("next"):
        lines.append("📅 Resultados: ~%s (%s) ✅ fuera del horizonte" % (
            _d(er["next"]), "confirmada" if er.get("known") else "estimada"))
    else:
        lines.append("📅 Resultados: fecha desconocida, revísala")
    news = er.get("news") or []
    if news:
        lines += ["", "📰 <b>Noticias</b>"]
        for n in news[:3]:
            lines.append("• %s %s" % (esc(_d(n.get("time"))[:5]) if n.get("time") else "", esc(n["title"][:140])))
    other = [w for w in (result or {}).get("warnings") or []
             if not w.startswith("Noticia de resultados") and "estimacion" not in w and "menos de 30" not in w]
    if other:
        lines.append("")
        for w in other[:3]:
            lines.append("⚠ %s" % esc(w))
    lines += ["", "<i>%s</i>" % esc(FOOT)]
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 3] + "..."


def summary(items, today, invalid=()):
    """One short message: today's setups ranked by the Time Machine's expectancy, with the pre-market
    check of each, and the ones the pre-market price already ruled out."""
    longs = sum(1 for _, t, _ in items if t.get("side") != "sell")
    lines = ["📊 <b>Swing %s</b>: %d setup(s) para operar (%d long, %d short)" % (
        _d(today), len(items), longs, len(items) - longs), ""]
    for n, (_, t, _) in enumerate(items, 1):
        m = t.get("timemachine") or {}
        exp = ("%+.2fR" % float(m["expectancy_R"])) if m.get("expectancy_R") is not None else "s/d"
        if m.get("p_win") is not None:
            exp += " · win %.0f%%" % (100.0 * float(m["p_win"]))
        pm = t.get("premarket") or {}
        gap = (" · pre %+.1f%% %s" % (float(pm["gap_pct"]), PM_ICON.get(pm.get("status"), ""))
               if pm.get("gap_pct") is not None else (" · pre %s" % PM_ICON["unknown"] if pm else ""))
        lines.append("%d. %s <b>%s</b> %s · exp. %s · %s%s" % (
            n, "🔴" if t.get("side") == "sell" else "🟢", esc(t["symbol"]),
            "SHORT" if t.get("side") == "sell" else "LONG", exp, esc(t.get("setup") or ""), gap))
    if invalid:
        lines += ["", "❌ <b>Descartados en el pre-mercado</b>"]
        for _, t, _ in invalid:
            pm = t.get("premarket") or {}
            lines.append("• <b>%s</b>: %s" % (esc(t["symbol"]), esc(pm.get("note") or "")))
    lines += ["", "<i>Ordenados por expectativa del Time Machine. El detalle de cada uno va abajo.</i>"]
    return "\n".join(lines)


def rank(items):
    def key(x):
        m = x[1].get("timemachine") or {}
        return -(float(m["expectancy_R"]) if m.get("expectancy_R") is not None else -99.0)
    return sorted(items, key=key)


def pending(risk_doc, tickets_doc, state, today, any_date=False, resend=False):
    """-> ([(key, ticket, result)], skipped [(symbol, why)]). Pure."""
    skipped = []
    if not isinstance(risk_doc, dict) or risk_doc.get("mode") != "swing":
        raise NotifyError("the risk file is not a swing check (run trader-risk check.py --swing)")
    if risk_doc.get("date") != today and not any_date:
        return [], [("*", "the risk check is from %s, not today (%s): nothing sent" % (risk_doc.get("date"), today))]
    tickets = (tickets_doc or {}).get("tickets") or []
    by_fp = {swing_fingerprint(t): t for t in tickets}
    sent = (state or {}).get("sent") or {}
    out = []
    for r in risk_doc.get("results") or []:
        if r.get("verdict") != "PASS":
            continue
        t = by_fp.get(r.get("fingerprint"))
        if t is None:
            skipped.append((r.get("symbol"), "the ticket changed after the risk check: run check.py --swing again"))
            continue
        k = key_of(t)
        if k in sent and not resend:
            skipped.append((t["symbol"], "already sent on %s" % sent[k].get("at", "?")[:10]))
            continue
        out.append((k, t, r))
    return out, skipped


# ---------------------------------------------------------------- Telegram

def _post(url, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sleep(s):
    time.sleep(s)


def credentials():
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", tok), ("TELEGRAM_CHAT_ID", chat)) if not v]
    if missing:
        raise NotifyError("set %s in the environment (never in a file): see the README" % " and ".join(missing))
    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", tok):
        raise NotifyError("TELEGRAM_BOT_TOKEN does not look like a bot token (digits:letters from @BotFather)")
    if chat == tok.split(":")[0]:
        raise NotifyError("TELEGRAM_CHAT_ID is the bot's own id (the number before ':' in the token). Use YOUR "
                          "chat id: send /start to the bot, open https://api.telegram.org/bot<TOKEN>/getUpdates "
                          "and copy the number in \"chat\":{\"id\": ...}")
    return tok, chat


def send(token, chat, text):
    """-> message_id. One retry after Telegram's retry_after on a 429. The token never shows in errors."""
    url = API % token
    for attempt in (0, 1):
        try:
            res = _post(url, {"chat_id": chat, "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": True})
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                body = {}
            finally:
                e.close()
            wait = ((body.get("parameters") or {}).get("retry_after"))
            if e.code == 429 and attempt == 0 and wait:
                _sleep(min(float(wait), 60))
                continue
            why = body.get("description") or "no reason"
            if e.code == 403 and "bot" in why.lower():
                why += (" (TELEGRAM_CHAT_ID must be YOUR chat id, not the bot's; and send /start to the bot first)")
            elif e.code == 400 and "chat not found" in why.lower():
                why += " (send /start to the bot, then take the chat id from getUpdates)"
            raise NotifyError("Telegram answered HTTP %d: %s" % (e.code, why))
        except (urllib.error.URLError, OSError) as e:
            raise NotifyError("could not reach api.telegram.org: %s" % getattr(e, "reason", e))
        if not res.get("ok"):
            raise NotifyError("Telegram said no: %s" % res.get("description"))
        return (res.get("result") or {}).get("message_id")
    raise NotifyError("Telegram kept rate limiting")


def prune(state, now_utc):
    cut = (now_utc - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    state["sent"] = {k: v for k, v in (state.get("sent") or {}).items() if str(v.get("at", ""))[:10] >= cut}
    return state


def main(argv=None):
    ap = argparse.ArgumentParser(description="Telegram notice for swing setups that passed trader-risk.")
    ap.add_argument("--risk", default="trader/state/swing_risk.json")
    ap.add_argument("--tickets", default="trader/swing_orders.json")
    ap.add_argument("--state", default="trader/state/telegram.json")
    ap.add_argument("--dry-run", action="store_true", help="print the messages; send and remember nothing")
    ap.add_argument("--test", action="store_true", help="send one test message and stop")
    ap.add_argument("--heartbeat", action="store_true", help="also send a short note when nothing new passed")
    ap.add_argument("--any-date", action="store_true", help="use a risk check from another day (testing)")
    ap.add_argument("--resend", action="store_true", help="send today's setups again even if they went out before")
    ap.add_argument("--now", help="pretend it is this time (New York, or ISO with Z)")
    ap.add_argument("--quiet", action="store_true", help="print counts only (for public CI logs)")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.test:
            text = ("✅ Prueba de day-trader-skill (%s UTC): el bot y el chat funcionan. Los avisos de setups "
                    "swing llegarán aquí." % now.strftime("%Y-%m-%d %H:%M"))
            if a.dry_run:
                print(text)
                return 0
            tok, chat = credentials()
            send(tok, chat, text)
            print("Test message sent.")
            return 0
        today = to_eastern(now).date().isoformat()
        risk_doc = read_json(a.risk, "swing risk check (run trader-risk check.py --swing)")
        tickets = read_json(a.tickets, "swing tickets (run trader-budget)")
        state = read_json(a.state, "state", required=False) or {}
        todo, skipped = pending(risk_doc, tickets, state, today, a.any_date, a.resend)
        tok = chat = None
        if not a.dry_run and (todo or a.heartbeat):
            tok, chat = credentials()
        sent = 0
        invalid = [x for x in todo if (x[1].get("premarket") or {}).get("status") == "invalid"]
        todo = rank([x for x in todo if x not in invalid])
        if len(todo) >= 2 or invalid:
            text = summary(todo, today, invalid)
            if a.dry_run:
                if not a.quiet:
                    print(text + "\n")
            else:
                send(tok, chat, text)
        for k, t, r in todo:
            text = message(t, r)
            if a.dry_run:
                if not a.quiet:
                    print(text + "\n")
                continue
            mid = send(tok, chat, text)
            state.setdefault("sent", {})[k] = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "message_id": mid}
            write_json(a.state, prune(state, now))      # after each one: a crash never resends what went out
            sent += 1
        for k, t, r in invalid:      # ruled out today: remember it, so it is not sent tomorrow either
            if not a.dry_run:
                state.setdefault("sent", {})[k] = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "invalid": True}
                write_json(a.state, prune(state, now))
        if a.heartbeat and not todo and not invalid:
            text = "Sin setups swing nuevos que pasen risk hoy (%s)." % today
            if a.dry_run:
                print(text)
            else:
                send(tok, chat, text)
        for sym, why in skipped:
            if not a.quiet:
                print("skip %s: %s" % (sym, why))
        print("%s %d message(s); %d skipped." % ("Would send" if a.dry_run else "Sent", len(todo) if a.dry_run else sent,
                                               len(skipped)))
    except NotifyError as e:
        print("notify_telegram.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
