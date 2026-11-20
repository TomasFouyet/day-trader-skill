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


# Entries rest until 13:00 New York. The last signal bar (12:55-13:00) closes at 13:00, so the cut falls a
# moment later: its order gets its try, and whatever still waits is cancelled for the day.
ENTRY_CANCEL = datetime.time(13, 2, 30)
ENTRY_CANCEL_TEXT = "13:00"


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


def max_slip_r():
    """How far past the breakout bar's close the entry may fill, in R (ORB_MAX_SLIP_R, default 0.15). The entry
    is a limit order at that price, immediate-or-cancel: never worse, and no entry if the price already ran."""
    return env_float("ORB_MAX_SLIP_R", 0.15)


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


def _got(o):
    """Shares an order actually got (an immediate-or-cancel entry can fill in part, then show "canceled")."""
    try:
        return abs(float((o or {}).get("filled_qty") or 0))
    except (TypeError, ValueError):
        return 0.0


def entry_of(acct, sym):
    """The filled orb entry order behind the open position in sym (newest first), or None."""
    for o in acct.orders("closed", sym, 100):
        c = o.get("client_order_id") or ""
        if c.startswith("orb-%s-" % sym) and c.endswith("-in") and (_filled(o) or _got(o) > 0):
            return o
    return None


def exit_bodies(sym, day, p, entry_px):
    """The OCO for the 70% (target at partial_r, stop at the box edge) and the stop for the other 30%.

    The target is measured from the close of the breakout bar (p["partial_px"]), like the backtest and the
    Pine strategy: a market order filled a little later does not move it. Without that close (exits put back
    for a trade with no saved record) it is measured from the fill."""
    side = "sell" if p["dir"] > 0 else "buy"
    if p.get("partial_px"):
        target = round(p["partial_px"], 2)
    else:
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
    mine = [o for o in acct.orders("open", sym, 100) if (o.get("client_order_id") or "").startswith(cid(sym, day, ""))]
    for o in [o for o in mine if (o.get("client_order_id") or "").endswith("-in")]:
        if not dry_run:                                   # a resting entry filled in part: keep what it got
            try:
                acct.cancel(o["id"])
            except paper.PaperError:
                pass
    open_orders = [o for o in mine if not (o.get("client_order_id") or "").endswith("-in")]
    age = trading_days_between(day, now_et.date())
    if age >= STRATEGY["max_days"] and now_et.time() >= datetime.time(15, 30):
        events.append((sym, "%d dias abierta: se cierra a mercado" % age))
        if not dry_run:
            for o in open_orders:
                acct.cancel(o["id"])
            acct.close(sym)
        return
    tp = acct.get(cid(sym, day, "tp"))
    # the 70% is in when its order filled, or when the position is smaller than the entry (an order replaced
    # by hand gets a new id, but the shares tell)
    try:
        bought = abs(float(entry.get("filled_qty") or 0))
    except (TypeError, ValueError):
        bought = 0.0
    partial_done = (tp is not None and _filled(tp)) or (bought > 0 and held < bought)
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
        rec = (state.get("trades") or {}).get(entry.get("client_order_id") or "", {})
        if rec.get("signal") is not None and rec.get("stop") is not None:
            p["partial_px"] = rec["signal"] + direction * STRATEGY["partial_r"] * abs(rec["signal"] - rec["stop"])
        oco, run_body, target = exit_bodies(sym, day, p, fill)
        if rec.get("limit") is not None and rec.get("fill") is None:
            rec["fill"] = fill
            events.append((sym, "se lleno la orden limite: entro %d a %.2f; salidas puestas (parcial %.2f, stop %.2f)" % (
                int(held), fill, target, stop)))
        else:
            events.append((sym, "faltaban las salidas: stop %.2f, parcial %.2f" % (stop, target)))
        if not dry_run:
            acct.submit(oco)
            if p["runner_qty"] > 0:
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
    since = _since(state, sym, day, acct)
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
                                             "bar": bar["t"].strftime("%H:%M"),
                                             "dir": direction, "qty": p["qty"], "at": now_et.isoformat()}
    paper.write_json(STATE, state)
    # a limit at the bar's close plus a small allowance, immediate-or-cancel: the fill is never further from
    # the close than max_slip_r(), so the 1.7R target stays where the rule puts it
    cap = round(bar["c"] + direction * max_slip_r() * p["risk"], 2)
    state["trades"][in_id]["limit"] = cap
    acct.submit({"symbol": sym, "qty": str(p["qty"]), "side": side, "type": "limit", "limit_price": "%.2f" % cap,
                 "time_in_force": "day", "client_order_id": in_id})
    o = wait_fill(acct, in_id, tries=3, sleep=sleep)
    got = int(_got(o))
    if got < 1:
        if o is not None and o.get("status") in ("canceled", "expired", "rejected"):
            events.append((sym, "la orden de entrada fue %s" % o.get("status")))
        else:
            events.append((sym, "el precio ya paso %.2f (cierre %.2f + %.2fR): la orden limite queda esperando hasta "
                                "las %s; si se llena, se ponen las salidas" % (cap, bar["c"], max_slip_r(),
                                                                              ENTRY_CANCEL_TEXT)))
        return
    if not _filled(o):                                    # filled in part: keep those shares, drop the rest
        try:
            acct.cancel(o["id"])
        except paper.PaperError:
            pass
        o = acct.get(in_id) or o
        got = int(_got(o))
    if got < p["qty"]:                                    # the exits follow the shares it got
        part = max(1, min(got - 1, int(round(got * STRATEGY["partial_frac"])))) if got > 1 else 1
        p = dict(p, qty=got, partial_qty=part, runner_qty=got - part)
        state["trades"][in_id]["qty"] = got
    fill = float(o.get("filled_avg_price") or bar["c"])
    oco, run_body, target = exit_bodies(sym, day, p, fill)
    acct.submit(oco)
    if p["runner_qty"] > 0:
        acct.submit(run_body)
    state["trades"][in_id]["fill"] = fill
    events.append((sym, "entro a %.2f; salidas puestas (parcial %.2f, stop %.2f)" % (fill, target, p["stop"])))


def _since(state, sym, day, acct=None):
    """A trade from an earlier day that closed during today's session: only bars after it may signal (the
    backtest's one-position-at-a-time rule). Two sources, the later wins: the pass that first saw the
    position gone (saved state), and Alpaca's own fill time of that closing order, which does not depend on
    the saved state (a lost cache cannot make an earlier breakout look tradable)."""
    t = (state.get("closed_today") or {}).get("%s-%s" % (sym, day.isoformat()))
    since = _hm(t) if t else None
    if acct is not None:
        f = closed_today_at(acct, sym, day)
        if f is not None and (since is None or f > since):
            since = f
    return since


def closed_today_at(acct, sym, day):
    """New York time of the latest fill today of an order in sym that is not today's entry: the stop, the
    partial or the day-limit close of a trade from an earlier day. -> time or None."""
    today = cid(sym, day, "")
    last = None
    for o in acct.orders("closed", sym, 100):
        for x in [o] + list(o.get("legs") or []):
            if not _filled(x) or (x.get("client_order_id") or "").startswith(today) or not x.get("filled_at"):
                continue
            try:
                at = scan.to_eastern(datetime.datetime.strptime(x["filled_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            if at.date() == day and (last is None or at > last):
                last = at
    return last.time().replace(second=0, microsecond=0) if last else None


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
    gone = [sym for sym in symbols() if (state.get("open") or {}).get(sym) and sym not in positions]
    track_open(state, positions, now_et)
    for sym in gone:
        closed_event(acct, sym, now_et, events, state)
    for sym, pos in positions.items():
        if sym in symbols():
            manage(acct, sym, pos, now_et, events, dry_run, state, bars_by_sym.get(sym) or [])
    if now_et.time() >= ENTRY_CANCEL:
        cancel_resting_entries(acct, now_et, events, state, dry_run)
    if _hm(STRATEGY["box_end"]) <= now_et.time() < ENTRY_CANCEL:
        equity = acct.equity()
        for sym in symbols():
            enter(acct, sym, now_et, bars_by_sym.get(sym) or [], positions, equity, events, state, dry_run, sleep)
    prune(state, now_et)
    if not dry_run:
        paper.write_json(STATE, state)
    return events


def resting_entries(acct, day):
    """Today's box entry orders still waiting (a limit that did not fill at once)."""
    tag = "-%s-in" % day.strftime("%Y%m%d")
    return [o for o in acct.orders("open", None, 100) if (o.get("client_order_id") or "").startswith("orb-")
            and (o.get("client_order_id") or "").endswith(tag)]


def cancel_resting_entries(acct, now_et, events, state, dry_run):
    for o in resting_entries(acct, now_et.date()):
        sym = (o.get("client_order_id") or "").split("-")[1]
        if not dry_run:
            try:
                acct.cancel(o["id"])
            except paper.PaperError:
                continue
        once(state, now_et.date(), "cancel-in-" + sym, events,
             (sym, "la orden limite de entrada no se lleno antes de las %s: cancelada, hoy no opera" % ENTRY_CANCEL_TEXT))


def closed_event(acct, sym, now_et, events, state):
    """A box trade that was open on the last pass and is gone now: say how it ended (the rest at breakeven,
    a stop, the day-20 close) with its R and dollars, once."""
    try:
        trades = trades_from_orders(acct.orders("closed", sym, 100))
    except paper.PaperError:
        trades = []
    done = [t for t in trades if t["symbol"] == sym and not t["open"]]
    if not done:
        once(state, now_et.date(), "closed-" + sym, events, (sym, "posicion cerrada"))
        return
    t = max(done, key=lambda x: (x["day"], x["entry_time"] or ""))
    risk = abs(t["entry"] - t["stop"]) if t.get("entry") and t.get("stop") else None
    parts = []
    for x in sorted(t.get("exits") or [], key=lambda x: x.get("time") or ""):
        part = (x.get("qty") or 0) / t["qty"] if t.get("qty") else 0
        r = (x["price"] - t["entry"]) * t["dir"] / risk * part if risk and x.get("price") is not None else None
        if r is not None and abs(r) < 0.005:
            r = 0.0                                            # breakeven: no "-0.00R"
        parts.append("%s %s" % (EXIT_ES.get(x.get("kind"), "salida"), ("%+.2fR" % r) if r is not None else ""))
    text = "operacion %s del %s cerrada: %s \u00b7 total %s (%s)" % (
        t["side"], t["day"], ", ".join(parts) or "salida",
        ("%+.2fR" % t["realized_R"]) if t.get("realized_R") is not None else "-", _usd(t.get("realized_pnl") or 0))
    once(state, now_et.date(), "closed-" + t["id"], events, (sym, text))


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

def _utc_of(stamp):
    try:
        return datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def audit_rows(acct, now_utc, bars_by_sym, day=None):
    """Each box entry of `day` against the rule: entry at the close of the breakout bar, stop at the other box
    edge, 70% at 1.7R. -> list of dicts in R and seconds (no prices or sizes: the CI log is public)."""
    day = day or scan.to_eastern(now_utc).date()
    out = []
    for sym in symbols():
        o = acct.get(cid(sym, day, "in"))
        if not _filled(o):
            continue
        bars = bars_by_sym.get(sym) or []
        box = box_of(bars, day)
        end = scan.to_eastern(_utc_of(o.get("filled_at")) or now_utc)
        sig = first_signal(bars, day, box, end + datetime.timedelta(minutes=1)) if box else None
        fill = float(o.get("filled_avg_price") or 0)
        d = 1 if o.get("side") == "buy" else -1
        row = {"symbol": sym, "dir": d}
        if sig and box:
            close = sig[0]["c"]
            edge = box[1] if d > 0 else box[0]
            risk = abs(close - edge)
            closed_at = sig[0]["t"] + datetime.timedelta(minutes=5)
            row.update(bar=sig[0]["t"].strftime("%H:%M"), delay_s=int((end - closed_at).total_seconds()),
                       slip_R=(close - fill) * d / risk if risk else None, risk_ratio=abs(fill - edge) / risk if risk else None)
        tp = acct.get(cid(sym, day, "tp"))
        if tp is not None:
            row["tp_status"] = tp.get("status")
        filled_at = scan.to_eastern(_utc_of(o.get("filled_at")) or now_utc)
        rec = (paper.read_json(STATE, {}) or {}).get("trades", {}).get(cid(sym, day, "in"), {})
        stop0 = rec.get("stop") or ((box[1] if d > 0 else box[0]) if box else None)
        after = [b for b in bars if b["t"] + datetime.timedelta(minutes=5) > filled_at]
        if after and stop0 is not None and fill != stop0:
            best = max(b["h"] for b in after) if d > 0 else min(b["l"] for b in after)
            row["best_R_from_fill"] = (best - fill) * d / abs(fill - stop0)
            if tp is not None and tp.get("limit_price"):
                row["touched_tp"] = (best - float(tp["limit_price"])) * d >= 0
        if tp is not None:
            lim = float(tp.get("limit_price") or 0)
            stp = None
            for leg in tp.get("legs") or []:
                if leg.get("stop_price"):
                    stp = float(leg["stop_price"])
            if stp is None:
                rec = (paper.read_json(STATE, {}) or {}).get("trades", {}).get(cid(sym, day, "in"), {})
                stp = rec.get("stop")
            if stp is not None and fill != stp:
                row["tp_R_from_fill"] = (lim - fill) * d / abs(fill - stp)
                if sig and box:
                    row["stop_is_box_edge"] = abs(stp - (box[1] if d > 0 else box[0])) < 0.011
                    close = sig[0]["c"]
                    row["tp_R_from_close"] = (lim - close) * d / abs(close - stp) if close != stp else None
        out.append(row)
    return out


def audit_text(rows):
    if not rows:
        return "Sin entradas de la caja ese dia."
    lines = ["Entradas de la caja contra la regla (entrar al cierre de la vela de ruptura, stop en el otro borde, 70% a 1,7R):"]
    for r in rows:
        f = lambda k, fmt: (fmt % r[k]) if r.get(k) is not None else "-"
        lines.append("%s %s: vela %s, ejecutada %s s despues de su cierre; precio %s R respecto del cierre "
                     "(negativo = peor); riesgo real %s veces el de la regla; 70%% a %s R desde la ejecucion "
                     "(%s R desde el cierre de la vela); stop en el borde de la caja: %s" % (
                         r["symbol"], "largo" if r["dir"] > 0 else "corto", r.get("bar", "-"), f("delay_s", "%d"),
                         f("slip_R", "%+.2f"), f("risk_ratio", "%.2f"), f("tp_R_from_fill", "%.2f"),
                         f("tp_R_from_close", "%.2f"), {True: "si", False: "NO"}.get(r.get("stop_is_box_edge"), "-")))
        if r.get("best_R_from_fill") is not None:
            lines.append("    desde la entrada llego hasta %+.2f R (velas IEX); toco el precio del 70%%: %s; orden del 70%%: %s" % (
                r["best_R_from_fill"], {True: "si", False: "no"}.get(r.get("touched_tp"), "-"), r.get("tp_status", "-")))
    return "\n".join(lines)


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


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def trades_from_orders(orders):
    """The box strategy's trades, rebuilt from the paper orders (client ids orb-SYMBOL-YYYYMMDD-part)."""
    by = {}
    for o in orders:
        c = o.get("client_order_id") or ""
        if not c.startswith("orb-"):
            continue
        parts = c.split("-")
        if len(parts) < 4:
            continue
        key = "-".join(parts[:3])
        by.setdefault(key, []).append(o)
        for leg in o.get("legs") or []:
            by[key].append(dict(leg, client_order_id=c + "-leg"))
    trades = []
    for key, os_ in sorted(by.items()):
        _, sym, day = key.split("-")
        entry = next((o for o in os_ if (o.get("client_order_id") or "").endswith("-in")
                      and (o.get("status") == "filled" or (_f(o.get("filled_qty")) or 0) > 0)), None)
        if entry is None:
            continue
        side = 1 if entry.get("side") == "buy" else -1
        px, qty = _f(entry.get("filled_avg_price")), _f(entry.get("filled_qty")) or _f(entry.get("qty"))
        stops = [_f(o.get("stop_price")) for o in os_ if o.get("type") in ("stop",) and _f(o.get("stop_price"))]
        stop = None
        for s_ in stops:
            if px and abs(s_ - px) > 0.005:
                stop = s_
                break
        exits = [o for o in os_ if o is not entry and o.get("status") == "filled"]
        out_qty = sum(_f(o.get("filled_qty")) or 0 for o in exits)
        pnl = sum(((_f(o.get("filled_avg_price")) or 0) - px) * side * (_f(o.get("filled_qty")) or 0) for o in exits)
        risk = abs(px - stop) * qty if (stop and px and qty) else None
        partial = any((o.get("client_order_id") or "").endswith("-tp") and o.get("status") == "filled" for o in os_)
        trades.append({
            "id": key, "symbol": sym, "day": "%s-%s-%s" % (day[:4], day[4:6], day[6:]), "side": "largo" if side > 0 else "corto",
            "dir": side, "qty": qty, "entry": px, "stop": stop, "entry_time": entry.get("filled_at"),
            "partial_done": partial, "closed_qty": out_qty, "open": out_qty < (qty or 0) - 1e-9,
            "realized_pnl": round(pnl, 2), "realized_R": round(pnl / risk, 3) if risk else None,
            "exits": [{"time": o.get("filled_at"), "qty": _f(o.get("filled_qty")), "price": _f(o.get("filled_avg_price")),
                       "kind": (o.get("client_order_id") or "").split("-", 3)[-1]} for o in exits]})
    return trades


EXIT_ES = {"tp": "\u2705 70% a 1,7R", "tp-leg": "\u274c stop (70%)", "run": "\u274c stop (30%)",
           "run-be": "\u2796 breakeven (30%)", "run-be2": "\u2796 breakeven (30%)"}


def _usd(v):
    return ("+" if v >= 0 else "-") + "$" + "{:,.0f}".format(abs(v))


def _ny_day(stamp):
    t = _utc_of(stamp)
    return scan.to_eastern(t).date() if t else None


def report_text(acct, now_utc):
    """The box at the close: today's entries and exits, the open trades with what they banked and their stop
    now, R and dollars. For Telegram (the swing summary is paper_trader.py's)."""
    now_et = scan.to_eastern(now_utc)
    day = now_et.date()
    trades = trades_from_orders(acct.orders("all", None, 500))
    positions = acct.positions()
    open_orders = acct.orders("open", None, 200)
    lines = ["\U0001F319 <b>Caja 09:00 \u00b7 cierre %s</b> (paper, Alpaca)" % day.strftime("%d-%m"), ""]
    fresh = [t for t in trades if t["day"] == day.isoformat()]
    if fresh:
        lines.append("<b>Entradas de hoy (%d)</b>" % len(fresh))
        for t in fresh:
            lines.append("%s %s %s \u00b7 entrada $%.2f \u00b7 stop $%.2f" % (
                "\U0001F7E2" if t["dir"] > 0 else "\U0001F534", t["symbol"], t["side"].upper(), t["entry"] or 0, t["stop"] or 0))
    else:
        lines.append("Hoy no hubo entradas.")
    banked_R = banked_usd = 0.0
    outs = []
    for t in trades:
        risk = abs(t["entry"] - t["stop"]) if t.get("entry") and t.get("stop") else None
        for x in t.get("exits") or []:
            if _ny_day(x.get("time")) != day or x.get("price") is None:
                continue
            part = (x.get("qty") or 0) / t["qty"] if t.get("qty") else 0
            r = (x["price"] - t["entry"]) * t["dir"] / risk * part if risk else None
            usd = (x["price"] - t["entry"]) * t["dir"] * (x.get("qty") or 0)
            banked_R += r or 0
            banked_usd += usd
            outs.append("\u2022 %s \u00b7 %s \u00b7 %s \u00b7 %s" % (
                t["symbol"], EXIT_ES.get(x.get("kind"), "salida"), ("%+.2fR" % r) if r is not None else "-", _usd(usd)))
    if outs:
        lines += ["", "<b>Salidas de hoy (%d)</b>" % len(outs)] + outs
    live = [t for t in trades if t["open"] and t["symbol"] in positions]
    if live:
        lines += ["", "<b>Abiertas (%d)</b>" % len(live)]
        open_R = open_usd = 0.0
        for t in live:
            p = positions[t["symbol"]]
            last = _f(p.get("current_price")) or t["entry"]
            risk = abs(t["entry"] - t["stop"]) if t.get("stop") else None
            left = abs(_f(p.get("qty")) or 0)
            r_now = (last - t["entry"]) * t["dir"] / risk if risk else None
            usd = (last - t["entry"]) * t["dir"] * left
            open_usd += usd
            stop_now = None
            for o in open_orders:
                for x in [o] + list(o.get("legs") or []):
                    if (x.get("symbol") or o.get("symbol")) == t["symbol"] and x.get("type") == "stop" and x.get("stop_price"):
                        stop_now = _f(x["stop_price"])
            got = t.get("realized_R")
            age = trading_days_between(datetime.date.fromisoformat(t["day"]), day)
            lines.append("%s %s %s \u00b7 entrada $%.2f \u2192 $%.2f \u00b7 %s ahora \u00b7 %s" % (
                "\U0001F7E2" if t["dir"] > 0 else "\U0001F534", t["symbol"], t["side"].upper(), t["entry"], last,
                ("%+.2fR" % r_now) if r_now is not None else "-", _usd(usd)))
            lines.append("   %s \u00b7 stop %s \u00b7 d\u00eda %d de %d" % (
                ("cobrado %+.2fR (70%%)" % got) if t.get("partial_done") and got is not None else "70% pendiente",
                ("$%.2f%s" % (stop_now, " (breakeven)" if abs(stop_now - t["entry"]) < 0.011 else "")) if stop_now else "-",
                age, STRATEGY["max_days"]))
        lines += ["", "Resultado abierto: %s" % _usd(open_usd)]
    else:
        lines += ["", "Sin operaciones abiertas de la caja."]
    if outs:
        lines.append("Cobrado hoy: <b>%+.2fR</b> (%s)" % (banked_R, _usd(banked_usd)))
    lines += ["", "<i>Cuenta simulada: sin dinero real. No es asesor\u00eda financiera.</i>"]
    return "\n".join(lines)


def close_report_once(acct, now_utc, send):
    """After 16:00 New York, the close report once a day (the passes run every 5 minutes)."""
    now_et = scan.to_eastern(now_utc)
    if now_et.time() < datetime.time(16, 0) or not scan.trading_day(now_et.date())[0]:
        return False
    state = paper.read_json(STATE, {}) or {}
    said = state.setdefault("said", {}).setdefault(now_et.date().isoformat(), [])
    if "close-report" in said:
        return False
    send(report_text(acct, now_utc))
    said.append("close-report")
    paper.write_json(STATE, state)
    return True

def _bar_ready(bars_by_sym, now_et):
    """Alpaca already has the 5 minute bar that just closed, for every symbol with bars today."""
    start = (now_et.replace(second=0, microsecond=0) - datetime.timedelta(minutes=now_et.minute % 5 + 5))
    for bars in bars_by_sym.values():
        if bars and bars[-1]["t"].date() == now_et.date() and not any(b["t"] == start for b in bars[-3:]):
            return False
    return True


def _one_pass(acct, now, fetch, dry, send, sleep=time.sleep):
    try:
        events = run_pass(acct, now, fetch(now), dry_run=dry, sleep=sleep)
        if events:
            text = events_text(now, events)
            if send and not dry:
                send(text)
                print(log_line(events))
            else:
                print(text)
    except (paper.PaperError, OSError, ValueError) as e:
        print("Pasada con error (sigue vigilando): %s" % e, file=sys.stderr)


def watch(acct, until, dry, send=None, clock=None, fetch=None, sleep=time.sleep, offset=2, retries=15, quick=10):
    """Stay up through the entry window: one pass a few seconds after each 5 minute bar closes (instead of
    the next scheduled pass, a minute or two later), until `until` New York time. -> passes made."""
    clock, fetch = clock or paper.utc_now, fetch or fetch_bars
    end, n = _hm(until), 0
    ok, why = scan.trading_day(scan.to_eastern(clock()).date())
    if not ok:
        print("Mercado cerrado (%s): sin vigia." % why)
        return 0
    while True:
        et = scan.to_eastern(clock())
        if et.time() >= end:
            return n
        if n:
            nxt = et.replace(second=0, microsecond=0) + datetime.timedelta(minutes=5 - et.minute % 5, seconds=offset)
            if nxt.time() >= end:
                return n
            gap = (nxt - et).total_seconds()
            waiting = False
            if gap > quick + 2 and et.time() < ENTRY_CANCEL:
                try:
                    waiting = bool(resting_entries(acct, et.date()))
                except paper.PaperError:
                    waiting = False
            if waiting:                                     # a limit entry is resting: check it again soon
                sleep(quick)
                _one_pass(acct, clock(), fetch, dry, send, sleep)
                n += 1
                continue
            sleep(max(0.0, gap))
        try:
            now = clock()
            bars = fetch(now)
            for _ in range(retries):                    # the bar that just closed may take a few seconds to appear
                if _bar_ready(bars, scan.to_eastern(now)):
                    break
                sleep(1)
                now = clock()
                bars = fetch(now)
            events = run_pass(acct, now, bars, dry_run=dry, sleep=sleep)
            if events:
                text = events_text(now, events)
                if send and not dry:
                    send(text)
                    print(log_line(events))
                else:
                    print(text)
        except (paper.PaperError, OSError, ValueError) as e:
            print("Pasada con error (sigue vigilando): %s" % e, file=sys.stderr)
        n += 1


def log_line(events):
    """What the public CI log may show of a pass: the symbols and how many messages, nothing else."""
    return "%d aviso(s) enviados a Telegram: %s" % (len(events), ", ".join(sorted({e[0] for e in events})))


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(sin TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: no se envia)")
        return
    paper._notify().send(token, chat, text)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="The 09:00 box breakout on Alpaca PAPER.")
    ap.add_argument("cmd", choices=("status", "run", "report", "audit"))
    ap.add_argument("--dry-run", action="store_true", help="say what it would do, place nothing")
    ap.add_argument("--telegram", action="store_true", help="send the events (run) or the report to Telegram")
    ap.add_argument("--now", help="New York time for tests, e.g. '2026-11-18 10:06'")
    ap.add_argument("--watch-until", metavar="HH:MM",
                    help="run: stay up until this New York time, one pass a few seconds after each 5 minute bar closes")
    a = ap.parse_args(argv)
    now_utc = scan.parse_now(a.now) if a.now else paper.utc_now()
    now_et = scan.to_eastern(now_utc)
    try:
        acct = Account(paper.Paper())
        if a.cmd == "report":
            text = report_text(acct, now_utc)
            if a.telegram:
                telegram(text)
                print("Resumen de la caja enviado a Telegram.")
            else:
                print(text)
            return 0
        if not (datetime.time(9, 55) <= now_et.time() <= datetime.time(16, 10)) and a.cmd == "run" and not a.now:
            print("Fuera del horario de la estrategia (%s NY): nada que hacer." % now_et.strftime("%H:%M"))
            return 0
        bars = fetch_bars(now_utc)
        if a.cmd == "audit":
            print(audit_text(audit_rows(acct, now_utc, bars)))
            return 0
        if a.cmd == "status":
            print(status_text(acct, now_utc, bars))
            return 0
        dry = a.dry_run or not paper.paper_on()
        if dry and not a.dry_run:
            print("PAPER_TRADING no es 1: se muestra lo que haria, sin ordenes.")
        if a.cmd == "run" and a.watch_until and not a.now:
            n = watch(acct, a.watch_until, dry, telegram if a.telegram else None)
            print("Vigia terminado a las %s NY: %d pasadas." % (scan.to_eastern(paper.utc_now()).strftime("%H:%M"), n))
            return 0
        events = run_pass(acct, now_utc, bars, dry_run=dry)
        text = events_text(now_utc, events) if events else ""
        if a.telegram and events and not dry:
            telegram(text)
            print(log_line(events))
        else:
            print(text or "Sin novedades.")
        if a.telegram and not dry and close_report_once(acct, now_utc, telegram):
            print("Resumen de cierre de la caja enviado a Telegram.")
        return 0
    except paper.PaperError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
