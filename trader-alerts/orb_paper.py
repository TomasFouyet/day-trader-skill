#!/usr/bin/env python3
"""The 09:00 box breakout on Alpaca PAPER, every day. Standard library only.

    python3 trader-alerts/orb_paper.py status                 # box, signal, positions and orders now (read only)
    python3 trader-alerts/orb_paper.py run --dry-run          # what this pass would do, without orders
    python3 trader-alerts/orb_paper.py run --telegram         # one pass: places and manages orders
    python3 trader-alerts/orb_paper.py report --telegram      # open trades and today's events

PAPER ONLY. Orders go through paper_trader.Paper, which refuses any host but
https://paper-api.alpaca.markets (a simulated account, no real money). It never loads trader-broker. It places
orders only when PAPER_TRADING is 1 / true / si / yes / on, besides ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.
Market data comes from Alpaca's data API (trader-scanner/marketdata.py).

The strategy (STRATEGY below; the validation is in README section 13):
  1. The box: high and low of the 5 minute bars from 09:00 to 10:00 New York (pre-market included).
  2. From 10:00, the first 5 minute bar that CLOSES outside the box is the signal: above = long, below = short.
     Both sides, no moving-average filter. Only bars that close by 13:00. One trade a day per symbol, and none
     while a trade on that symbol is still open.
  3. Entry at the market right after that bar closes. Stop at the other edge of the box.
  4. At 1.7R, 70% of the position is sold (an OCO order: that limit or the stop). The other 30% has its own
     stop at the box edge, moved to the entry price (breakeven) once the 70% is out. It has no target.
  5. A trade still open 20 trading days after the entry is closed at the market (after 15:30).

Run it every 5 minutes on trading days from 09:55 to 16:05 New York (the workflow caja-diaria.yml, started by
cron-job.org or its own schedule). Each pass is idempotent: orders carry client ids orb-SYMBOL-YYYYMMDD-...
and the account is the record of what happened. A signal whose bar closed more than 10 minutes before the pass
is skipped for the day ("se paso la senal"): entering later would not be the tested trade.

Differences from the backtest: live bars come from IEX (Alpaca's free real-time feed; the backtest used the
consolidated tape), entries are market orders a minute or two after the close, and Alpaca triggers stops only
in regular hours.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = "trader/state/orb_paper.json"

STRATEGY = {
    "symbols": ["QQQ", "SPY", "DIA", "IWM"],
    "box_start": "09:00", "box_end": "10:00", "min_box_bars": 3,
    "last_entry": "12:55",            # the last bar that may signal starts at 12:55 and closes at 13:00
    "partial_r": 1.7, "partial_frac": 0.7,
    "max_days": 20,                   # trading days a trade may stay open
    "grace_minutes": 10,              # a signal older than this at the pass is skipped for the day
    "feed": "iex",
}


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


paper = _load("orb_paper_trader", "trader-alerts/paper_trader.py")
scan = _load("orb_paper_scan", "trader-scanner/scan.py")


def _hm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def env_float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def risk_pct():
    """% of the equity risked per trade (ORB_RISK_PCT, default 0.25: four symbols can be open at once)."""
    return env_float("ORB_RISK_PCT", 0.25)


def max_notional_pct():
    """Largest position, as % of the equity (ORB_MAX_NOTIONAL_PCT, default 50)."""
    return env_float("ORB_MAX_NOTIONAL_PCT", 50.0)


def symbols():
    raw = os.environ.get("ORB_SYMBOLS")
    return [s.strip().upper() for s in raw.split(",") if s.strip()] if raw else list(STRATEGY["symbols"])


# ---------------------------------------------------------------- bars and the box

def ny_bars(raw):
    """Alpaca bars (t in UTC) -> [{t: New York datetime of the bar start, o, h, l, c}], in time order."""
    out = []
    for b in raw or []:
        utc = datetime.datetime.strptime(str(b["t"])[:19], "%Y-%m-%dT%H:%M:%S")
        out.append({"t": scan.to_eastern(utc), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                    "c": float(b["c"])})
    out.sort(key=lambda b: b["t"])
    return out


def box_of(bars, day, s=STRATEGY):
    """(high, low, number of bars) of the box on `day`, or None if it has too few bars or is not over."""
    b0, b1 = _hm(s["box_start"]), _hm(s["box_end"])
    inside = [b for b in bars if b["t"].date() == day and b0 <= b["t"].time() < b1]
    if len(inside) < s["min_box_bars"]:
        return None
    return max(b["h"] for b in inside), min(b["l"] for b in inside), len(inside)


def first_signal(bars, day, box, now_et, since=None, s=STRATEGY):
    """The first completed bar of the day that closes outside the box, from the box end (or `since`) to the
    last entry bar. -> (bar, direction 1 / -1) or None."""
    hi, lo, _ = box
    start = _hm(s["box_end"])
    if since:                                       # the bar that was forming when the earlier trade closed counts
        start = max(start, (datetime.datetime.combine(day, since) - datetime.timedelta(minutes=5)).time())
    last = _hm(s["last_entry"])
    for b in bars:
        t = b["t"]
        if t.date() != day or t.time() < start or t.time() > last:
            continue
        if t + datetime.timedelta(minutes=5) > now_et:
            break                                   # still forming
        if b["c"] > hi:
            return b, 1
        if b["c"] < lo:
            return b, -1
    return None


def plan(direction, entry, box, equity, s=STRATEGY):
    """Order sizes and prices for a trade. -> dict, or None when the size rounds below 2 shares."""
    hi, lo, _ = box
    stop = lo if direction > 0 else hi
    risk = (entry - stop) * direction
    if risk <= 0:
        return None
    qty = int(equity * risk_pct() / 100.0 / risk)
    qty = min(qty, int(equity * max_notional_pct() / 100.0 / entry))
    if qty < 2:
        return None
    part = max(1, min(qty - 1, int(round(qty * s["partial_frac"]))))
    return {"dir": direction, "qty": qty, "partial_qty": part, "runner_qty": qty - part, "entry": entry,
            "stop": round(stop, 2), "partial_px": round(entry + direction * s["partial_r"] * risk, 2),
            "risk": risk}


# ---------------------------------------------------------------- the account

def cid(sym, day, part):
    return "orb-%s-%s-%s" % (sym, day.strftime("%Y%m%d"), part)


class Account(object):
    """The few PAPER calls this needs, on top of paper_trader.Paper (which refuses any other host)."""

    def __init__(self, api):
        self.api = api

    def equity(self):
        return float(self.api.account().get("equity") or 0)

    def positions(self):
        return {p["symbol"]: p for p in (self.api.positions() or [])}

    def get(self, client_id):
        try:
            return self.api.by_client_id(client_id)
        except paper.PaperError as e:
            if "404" in str(e):
                return None
            raise

    def orders(self, status="all", sym=None, limit=200):
        q = "status=%s&limit=%d&direction=desc&nested=true" % (status, limit)
        if sym:
            q += "&symbols=%s" % sym
        return self.api.call("GET", "/v2/orders?" + q) or []

    def submit(self, body):
        return self.api.submit(body)

    def replace(self, order_id, body):
        return self.api.call("PATCH", "/v2/orders/%s" % order_id, body)

    def cancel(self, order_id):
        return self.api.cancel(order_id)

    def close(self, sym):
        return self.api.close_position(sym)


def once(state, day, key, events, item):
    """Add an event only the first time this day (passes run every 5 minutes)."""
    said = state.setdefault("said", {}).setdefault(day.isoformat(), [])
    if key not in said:
        said.append(key)
        events.append(item)


def _filled(o):
    return o is not None and o.get("status") == "filled"


def entry_of(acct, sym):
    """The filled orb entry order behind the open position in sym (newest first), or None."""
    for o in acct.orders("closed", sym, 100):
        c = o.get("client_order_id") or ""
        if c.startswith("orb-%s-" % sym) and c.endswith("-in") and _filled(o):
            return o
    return None


def exit_bodies(sym, day, p, entry_px):
    """The OCO for the 70% (target at partial_r, stop at the box edge) and the stop for the other 30%."""
    side = "sell" if p["dir"] > 0 else "buy"
    risk = (entry_px - p["stop"]) * p["dir"]
    target = round(entry_px + p["dir"] * STRATEGY["partial_r"] * risk, 2)
    oco = {"symbol": sym, "qty": str(p["partial_qty"]), "side": side, "type": "limit", "time_in_force": "gtc",
           "order_class": "oco", "limit_price": "%.2f" % target, "take_profit": {"limit_price": "%.2f" % target},
           "stop_loss": {"stop_price": "%.2f" % p["stop"]}, "client_order_id": cid(sym, day, "tp")}
    runner = {"symbol": sym, "qty": str(p["runner_qty"]), "side": side, "type": "stop", "time_in_force": "gtc",
              "stop_price": "%.2f" % p["stop"], "client_order_id": cid(sym, day, "run")}
    return oco, runner, target


# ---------------------------------------------------------------- one pass

def trading_days_between(a, b):
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if scan.trading_day(d)[0]:
            n += 1
    return n


def manage(acct, sym, pos, now_et, events, dry_run, state, bars):
    """An open orb position: move the runner's stop to breakeven after the partial, repair missing exits,
    close it after max_days."""
    entry = entry_of(acct, sym)
    if entry is None:
        once(state, now_et.date(), "foreign-" + sym, events, (sym, "posicion abierta que no es de esta estrategia: no se toca"))
        return
    day = datetime.datetime.strptime(entry["client_order_id"].split("-")[2], "%Y%m%d").date()
    fill = float(entry.get("filled_avg_price") or 0)
    direction = 1 if entry.get("side") == "buy" else -1
    held = abs(float(pos.get("qty") or 0))
    open_orders = [o for o in acct.orders("open", sym, 100) if (o.get("client_order_id") or "").startswith(cid(sym, day, ""))]
    age = trading_days_between(day, now_et.date())
    if age >= STRATEGY["max_days"] and now_et.time() >= datetime.time(15, 30):
        events.append((sym, "%d dias abierta: se cierra a mercado" % age))
        if not dry_run:
            for o in open_orders:
                acct.cancel(o["id"])
            acct.close(sym)
        return
    tp = acct.get(cid(sym, day, "tp"))
    partial_done = tp is not None and _filled(tp)
    runner = [o for o in open_orders if (o.get("client_order_id") or "").startswith(cid(sym, day, "run"))]
    if partial_done:
        for o in runner:
            if abs(float(o.get("stop_price") or 0) - fill) > 0.005:
                events.append((sym, "parcial cobrado: stop del resto a la entrada %.2f" % fill))
                if not dry_run:
                    acct.replace(o["id"], {"stop_price": "%.2f" % fill, "client_order_id": cid(sym, day, "run-be")})
        if not runner:
            events.append((sym, "parcial cobrado y el resto sin stop: se pone en %.2f" % fill))
            if not dry_run:
                acct.submit({"symbol": sym, "qty": str(int(held)), "side": "sell" if direction > 0 else "buy",
                             "type": "stop", "time_in_force": "gtc", "stop_price": "%.2f" % fill,
                             "client_order_id": cid(sym, day, "run-be2")})
        return
    if not open_orders:
        # the exits never got placed (or were lost): put them back from the entry and the box
        stop = stop_of(state, entry, bars, day)
        if stop is None:
            once(state, now_et.date(), "nostop-" + sym, events, (sym, "sin ordenes de salida y sin stop conocido: revisar a mano"))
            return
        part = max(1, min(int(held) - 1, int(round(held * STRATEGY["partial_frac"]))))
        p = {"dir": direction, "partial_qty": part, "runner_qty": int(held) - part, "stop": stop}
        oco, run_body, target = exit_bodies(sym, day, p, fill)
        events.append((sym, "faltaban las salidas: stop %.2f, parcial %.2f" % (stop, target)))
        if not dry_run:
            acct.submit(oco)
            acct.submit(run_body)


def stop_of(state, entry, bars, day):
    """The box edge used as the stop: saved at entry time, or rebuilt from that day's box."""
    rec = (state.get("trades") or {}).get(entry.get("client_order_id") or "", {})
    if rec.get("stop") is not None:
        return rec["stop"]
    box = box_of(bars, day)
    if box is None:
        return None
    return round(box[1] if entry.get("side") == "buy" else box[0], 2)


def wait_fill(acct, client_id, tries=10, pause=1.0, sleep=time.sleep):
    for _ in range(tries):
        o = acct.get(client_id)
        if _filled(o):
            return o
        if o is not None and o.get("status") in ("canceled", "expired", "rejected"):
            return o
        sleep(pause)
    return acct.get(client_id)


def enter(acct, sym, now_et, bars, positions, equity, events, state, dry_run, sleep=time.sleep):
    day = now_et.date()
    if sym in positions:
        return
    if acct.get(cid(sym, day, "in")) is not None:
        return                                                     # already traded today
    box = box_of(bars, day)
    if box is None:
        if now_et.time() >= _hm(STRATEGY["box_end"]):
            once(state, day, "box-" + sym, events, (sym, "caja con menos de %d velas: hoy no opera" % STRATEGY["min_box_bars"]))
        return
    since = _since(state, sym, day)
    sig = first_signal(bars, day, box, now_et, since)
    if sig is None:
        return
    bar, direction = sig
    closed_at = bar["t"] + datetime.timedelta(minutes=5)
    if now_et - closed_at > datetime.timedelta(minutes=STRATEGY["grace_minutes"]):
        once(state, day, "missed-" + sym, events, (sym, "se paso la senal de las %s: hoy no entra" % closed_at.strftime("%H:%M")))
        return
    p = plan(direction, bar["c"], box, equity)
    if p is None:
        once(state, day, "small-" + sym, events, (sym, "tamano menor a 2 acciones con el riesgo configurado: no entra"))
        return
    side = "buy" if direction > 0 else "sell"
    label = "largo" if direction > 0 else "corto"
    events.append((sym, "%s %d a mercado (cierre %.2f fuera de la caja %.2f-%.2f); stop %.2f, %d%% a %.2f" % (
        label, p["qty"], bar["c"], box[1], box[0], p["stop"], round(100 * STRATEGY["partial_frac"]), p["partial_px"])))
    if dry_run:
        return
    in_id = cid(sym, day, "in")
    state.setdefault("trades", {})[in_id] = {"stop": p["stop"], "box": [box[0], box[1]], "signal": bar["c"],
                                             "dir": direction, "qty": p["qty"], "at": now_et.isoformat()}
    paper.write_json(STATE, state)
    acct.submit({"symbol": sym, "qty": str(p["qty"]), "side": side, "type": "market", "time_in_force": "day",
                 "client_order_id": in_id})
    o = wait_fill(acct, in_id, sleep=sleep)
    if not _filled(o):
        events.append((sym, "la entrada no se lleno todavia: la proxima pasada pone las salidas"))
        return
    fill = float(o.get("filled_avg_price") or bar["c"])
    oco, run_body, target = exit_bodies(sym, day, p, fill)
    acct.submit(oco)
    acct.submit(run_body)
    state["trades"][in_id]["fill"] = fill
    events.append((sym, "entro a %.2f; salidas puestas (parcial %.2f, stop %.2f)" % (fill, target, p["stop"])))


def _since(state, sym, day):
    """A trade from an earlier day that closed during today's session: only bars after it may signal (the
    backtest's one-position-at-a-time rule)."""
    t = (state.get("closed_today") or {}).get("%s-%s" % (sym, day.isoformat()))
    return _hm(t) if t else None


def track_open(state, positions, now_et):
    """Note the time a position seen open on an earlier pass is gone (closed by its stop or target)."""
    was = state.get("open") or {}
    for sym in symbols():
        if was.get(sym) and sym not in positions:
            key = "%s-%s" % (sym, now_et.date().isoformat())
            state.setdefault("closed_today", {}).setdefault(key, now_et.strftime("%H:%M"))
    state["open"] = {sym: True for sym in symbols() if sym in positions}


def run_pass(acct, now_utc, bars_by_sym, dry_run=False, state=None, sleep=time.sleep):
    """One pass over every symbol. -> list of (symbol, event text)."""
    state = state if state is not None else (paper.read_json(STATE, {}) or {})
    now_et = scan.to_eastern(now_utc)
    events = []
    ok, why = scan.trading_day(now_et.date())
    if not ok:
        return [("-", "mercado cerrado (%s)" % why)]
    positions = acct.positions()
    track_open(state, positions, now_et)
    for sym, pos in positions.items():
        if sym in symbols():
            manage(acct, sym, pos, now_et, events, dry_run, state, bars_by_sym.get(sym) or [])
    if _hm(STRATEGY["box_end"]) <= now_et.time() <= datetime.time(13, 15):
        equity = acct.equity()
        for sym in symbols():
            enter(acct, sym, now_et, bars_by_sym.get(sym) or [], positions, equity, events, state, dry_run, sleep)
    prune(state, now_et)
    if not dry_run:
        paper.write_json(STATE, state)
    return events


def prune(state, now_et):
    """Keep two weeks of daily notes."""
    cut = (now_et.date() - datetime.timedelta(days=14)).isoformat()
    state["said"] = {d: v for d, v in (state.get("said") or {}).items() if d >= cut}
    state["closed_today"] = {k: v for k, v in (state.get("closed_today") or {}).items() if k[-10:] >= cut}


def fetch_bars(now_utc, http=None):
    md = scan._marketdata()
    if md is None or not md.enabled():
        raise paper.PaperError("set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY for the market data")
    day = scan.to_eastern(now_utc).date()
    start = scan.eastern_to_utc(datetime.datetime.combine(day, datetime.time(8, 30)))
    raw = md.multi_bars(symbols(), "5Min", md._iso(start), md._iso(now_utc), STRATEGY["feed"], http=http)
    return {s: ny_bars(raw.get(s)) for s in symbols()}


# ---------------------------------------------------------------- texts

def status_text(acct, now_utc, bars_by_sym):
    now_et = scan.to_eastern(now_utc)
    day = now_et.date()
    lines = ["<b>Caja 09:00-10:00 (paper)</b> %s %s NY" % (day.isoformat(), now_et.strftime("%H:%M"))]
    positions = acct.positions()
    for sym in symbols():
        bars = bars_by_sym.get(sym) or []
        box = box_of(bars, day)
        if box is None:
            lines.append("%s: caja sin completar" % sym)
            continue
        sig = first_signal(bars, day, box, now_et)
        txt = "%s: caja %.2f-%.2f (%d velas)" % (sym, box[1], box[0], box[2])
        if sig:
            txt += ", senal %s a las %s (cierre %.2f)" % ("larga" if sig[1] > 0 else "corta",
                                                         (sig[0]["t"] + datetime.timedelta(minutes=5)).strftime("%H:%M"),
                                                         sig[0]["c"])
        if sym in positions:
            p = positions[sym]
            txt += "; abierta %s %s, P&L %.2f" % (p.get("side"), p.get("qty"), float(p.get("unrealized_pl") or 0))
        lines.append(txt)
    return "\n".join(lines)


def events_text(now_utc, events):
    now_et = scan.to_eastern(now_utc)
    return "<b>Caja 09:00 (paper)</b> %s NY\n%s" % (now_et.strftime("%Y-%m-%d %H:%M"),
                                                   "\n".join("%s: %s" % e for e in events))


def report_text(acct, now_utc):
    now_et = scan.to_eastern(now_utc)
    lines = ["<b>Caja 09:00 (paper), cierre del %s</b>" % now_et.date().isoformat()]
    positions = acct.positions()
    any_open = False
    for sym in symbols():
        if sym not in positions:
            continue
        entry = entry_of(acct, sym)
        if entry is None:
            continue
        any_open = True
        p = positions[sym]
        st = paper.read_json(STATE, {}) or {}
        rec = (st.get("trades") or {}).get(entry.get("client_order_id"), {})
        fill = float(entry.get("filled_avg_price") or 0)
        last = float(p.get("current_price") or fill)
        stop = rec.get("stop")
        r = ((last - fill) * (1 if entry.get("side") == "buy" else -1) / abs(fill - stop)) if stop else None
        lines.append("%s %s %s desde %.2f, ahora %.2f%s" % (sym, "largo" if entry.get("side") == "buy" else "corto",
                                                            p.get("qty"), fill, last, (", %+.2fR" % r) if r is not None else ""))
    if not any_open:
        lines.append("Sin operaciones abiertas.")
    return "\n".join(lines)


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(sin TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: no se envia)")
        return
    paper._notify().send(token, chat, text)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="The 09:00 box breakout on Alpaca PAPER.")
    ap.add_argument("cmd", choices=("status", "run", "report"))
    ap.add_argument("--dry-run", action="store_true", help="say what it would do, place nothing")
    ap.add_argument("--telegram", action="store_true", help="send the events (run) or the report to Telegram")
    ap.add_argument("--now", help="New York time for tests, e.g. '2026-11-18 10:06'")
    a = ap.parse_args(argv)
    now_utc = scan.parse_now(a.now) if a.now else paper.utc_now()
    now_et = scan.to_eastern(now_utc)
    try:
        acct = Account(paper.Paper())
        if a.cmd == "report":
            text = report_text(acct, now_utc)
            print(text)
            if a.telegram:
                telegram(text)
            return 0
        if not (datetime.time(9, 55) <= now_et.time() <= datetime.time(16, 10)) and a.cmd == "run" and not a.now:
            print("Fuera del horario de la estrategia (%s NY): nada que hacer." % now_et.strftime("%H:%M"))
            return 0
        bars = fetch_bars(now_utc)
        if a.cmd == "status":
            print(status_text(acct, now_utc, bars))
            return 0
        dry = a.dry_run or not paper.paper_on()
        if dry and not a.dry_run:
            print("PAPER_TRADING no es 1: se muestra lo que haria, sin ordenes.")
        events = run_pass(acct, now_utc, bars, dry_run=dry)
        text = events_text(now_utc, events) if events else ""
        print(text or "Sin novedades.")
        if a.telegram and events and not dry:
            telegram(text)
        return 0
    except paper.PaperError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
