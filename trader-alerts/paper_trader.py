#!/usr/bin/env python3
"""Paper trading of the swing setups on Alpaca, to build a real track record. Standard library only.

    python3 paper_trader.py account                  # is the paper account reachable? (read only)
    python3 paper_trader.py sync                     # read fills, close time stops, update the journal
    python3 paper_trader.py submit --dry-run         # the orders it would place for today's setups
    python3 paper_trader.py submit                   # place them (needs PAPER_TRADING=1)
    python3 paper_trader.py report                   # the track record; --telegram sends it
    python3 paper_trader.py daily --telegram         # after the close: open trades, closed today, not filled

PAPER ONLY. Every request goes to https://paper-api.alpaca.markets, a simulated
account: no real money. The host is fixed in this file and anything else is
refused. It never loads trader-broker and never touches a live account. It
places orders only when PAPER_TRADING is 1 (or true / si / yes / on) in the environment, besides the
keys ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.

submit: each ticket that passed today's trader-risk check (trader/state/
swing_risk.json, same fingerprint) and is not "invalid" in the pre-market check
becomes ONE bracket order: a LIMIT entry at the ticket's entry (the pre-market
price when the pre-market check moved it), a take-profit limit at the target
and a stop at the stop, all good-till-cancelled. An entry that has not filled
by the close of its day is cancelled by the next sync ("limite no alcanzado"). Whole shares only
(Alpaca takes no fractions in a bracket): the ticket's size (the pre-market one
when there is one) rounded down; under 1 share is skipped and logged. A short
is a short sale of the stock (Alpaca allows it on easy-to-borrow names). Each
setup is ordered once (trader/state/paper.json).

sync: reads each order back. A filled entry opens the trade; a filled take-
profit or stop closes it as "target" or "stop". On the first run after the
ticket's time_stop_date with the trade still open, it cancels the two exit
orders and closes the position at the market (the next open: the Time
Machine closes at the time-stop day's close instead). Realized R = the move
from the real entry fill to the real exit fill, over the planned risk.

report: closed trades, real winrate and average R next to what the Time
Machine estimated for them, open trades, and the paper account's equity.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PAPER_URL = "https://paper-api.alpaca.markets"     # fixed: never a live account
STATE = "trader/state/paper.json"
UA = "day-trader-skill/2.0 (paper trading)"
HERE = os.path.dirname(os.path.abspath(__file__))


class PaperError(Exception):
    pass


# ---------------------------------------------------------------- time

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def to_eastern(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return utc + datetime.timedelta(hours=-4 if start <= utc < end else -5)


def parse_now(text):
    if not text:
        return utc_now()
    t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        for off in (4, 5):
            utc = t + datetime.timedelta(hours=off)
            if to_eastern(utc) == t:
                return utc
        return t + datetime.timedelta(hours=5)
    return (t - t.utcoffset()).replace(tzinfo=None)


# ---------------------------------------------------------------- the paper API

def _check_url(url):
    if not url.startswith(PAPER_URL + "/"):
        raise PaperError("refused: %s is not the paper trading API" % url.split("?")[0])


def _http(method, url, body=None, timeout=30):
    _check_url(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", ""),
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("message")
        except Exception:
            msg = None
        finally:
            e.close()
        raise PaperError("Alpaca paper answered HTTP %d%s" % (e.code, (": %s" % msg) if msg else ""))
    except (urllib.error.URLError, OSError) as e:
        raise PaperError("could not reach the Alpaca paper API: %s" % getattr(e, "reason", e))


class Paper(object):
    def __init__(self, http=None):
        if not (os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY")):
            raise PaperError("set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (paper account keys)")
        self.http = http or _http

    def call(self, method, path, body=None):
        return self.http(method, PAPER_URL + path, body)

    def account(self):
        return self.call("GET", "/v2/account")

    def order(self, order_id):
        return self.call("GET", "/v2/orders/%s?nested=true" % urllib.parse.quote(order_id))

    def by_client_id(self, cid):
        return self.call("GET", "/v2/orders:by_client_order_id?client_order_id=%s" % urllib.parse.quote(cid))

    def submit(self, body):
        return self.call("POST", "/v2/orders", body)

    def cancel(self, order_id):
        return self.call("DELETE", "/v2/orders/%s" % urllib.parse.quote(order_id))

    def close_position(self, sym):
        return self.call("DELETE", "/v2/positions/%s" % urllib.parse.quote(sym))

    def positions(self):
        return self.call("GET", "/v2/positions")


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)


def _notify():
    spec = importlib.util.spec_from_file_location("paper_notify", os.path.join(HERE, "notify_telegram.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- orders

def price_str(x):
    x = float(x)
    return "%.2f" % x if x >= 1 else "%.4f" % x


LIVE = ("submitted", "open", "closing")   # one trade a symbol at a time
MAX_POSITIONS = 6     # live paper trades at once (PAPER_MAX_POSITIONS overrides)
# Edge over random entries measured on real history (notify_telegram.STUDY): the only basis to say one
# setup has better odds than another. The Time Machine's probabilities did not predict outcomes.
EDGE_R = {"earnings_gap": 0.27}


def max_positions():
    try:
        return max(1, int(os.environ.get("PAPER_MAX_POSITIONS") or MAX_POSITIONS))
    except ValueError:
        return MAX_POSITIONS


def current_R(tr, positions):
    """Open R of a live trade at the latest price (0 for an entry that has not filled)."""
    p = positions.get(str(tr.get("symbol") or "").upper()) or {}
    cur, entry, risk = _f(p.get("current_price")), tr.get("entry_fill"), tr.get("risk_per_share")
    if tr.get("status") != "open" or cur is None or entry is None or not risk:
        return 0.0
    return ((cur - entry) if tr.get("side") != "sell" else (entry - cur)) / risk


def weakest(trades, positions):
    """The live trade with the least edge, then the worst open R: (key, trade) or (None, None)."""
    live = [(k, v) for k, v in trades.items() if v.get("status") in ("submitted", "open")]
    if not live:
        return None, None
    return min(live, key=lambda kv: (EDGE_R.get(kv[1].get("setup"), 0.0), current_R(kv[1], positions)))


def retire(api, tr, why):
    """Cancel an unfilled entry, or close an open trade at the market (its exit legs cancelled first)."""
    if tr["status"] == "submitted":
        api.cancel(tr["order_id"])
        tr.update(status="not_filled", why=why)
        return
    try:
        legs = (api.order(tr["order_id"]) or {}).get("legs") or []
    except PaperError:
        legs = []
    for l in legs:
        if l.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled"):
            try:
                api.cancel(l["id"])
            except PaperError:
                pass
    c = api.close_position(tr["symbol"])
    tr.update(status="closing", close_order_id=c.get("id"), close_reason="replaced", why=why)


def key_of(t):
    return "%s|%s|%s" % (str(t.get("symbol") or "").upper(), t.get("signal_date"), t.get("setup") or "")


def client_id(t):
    raw = "swing-%s-%s-%s" % (t["symbol"], str(t.get("signal_date") or "").replace("-", ""), t.get("setup") or "x")
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:48]


def planned(t):
    """A ticket -> (the bracket order body, or None, and why)."""
    pm = t.get("premarket") if isinstance(t.get("premarket"), dict) else {}
    if pm.get("status") == "invalid":
        return None, "invalido en el pre-mercado"
    qty = pm.get("qty") if pm.get("status") in ("ok", "warn") and pm.get("qty") is not None else t.get("qty")
    whole = int(float(qty or 0) + 1e-9)
    if whole < 1:
        return None, "menos de 1 accion entera (%s)" % qty
    short = str(t.get("side") or "buy") == "sell"
    entry = pm.get("price") if pm.get("status") in ("ok", "warn") and pm.get("price") is not None else t["entry"]
    body = {"symbol": str(t["symbol"]).upper(), "qty": str(whole), "side": "sell" if short else "buy",
            "type": "limit", "limit_price": price_str(entry), "time_in_force": "gtc", "order_class": "bracket",
            "take_profit": {"limit_price": price_str(t["target"])},
            "stop_loss": {"stop_price": price_str(t["stop"])},
            "client_order_id": client_id(t)}
    return body, None


def submit(api, tickets_doc, risk_doc, state, today, dry_run=False, now=None):
    """Pure but for api: place one bracket per new PASS ticket. -> [(symbol, what)]."""
    if not isinstance(risk_doc, dict) or risk_doc.get("date") != today:
        return [("*", "sin chequeo de risk de hoy: nada que operar")]
    notify = _notify()
    passed = {r.get("fingerprint") for r in risk_doc.get("results") or [] if r.get("verdict") == "PASS"}
    trades = state.setdefault("trades", {})
    out = []
    positions = None
    for t in (tickets_doc or {}).get("tickets") or []:
        if notify.swing_fingerprint(t) not in passed:
            continue
        k = key_of(t)
        if k in trades:
            out.append((t["symbol"], "ya operado"))
            continue
        sym = str(t.get("symbol") or "").upper()
        if any(str(v.get("symbol") or "").upper() == sym and v.get("status") in LIVE for v in trades.values()):
            out.append((t["symbol"], "omitido: ya hay una operacion viva en %s" % sym))
            continue
        body, why = planned(t)
        if body is None:
            trades[k] = {"symbol": t["symbol"], "status": "skipped", "why": why, "signal_date": t.get("signal_date")}
            out.append((t["symbol"], "omitido: " + why))
            continue
        if sum(1 for v in trades.values() if v.get("status") in LIVE) >= max_positions():
            # full: replace the weakest live trade only when this setup has a measured edge it lacks
            if positions is None:
                try:
                    positions = {str(p.get("symbol")).upper(): p for p in (api.positions() or [])} if api else {}
                except PaperError:
                    positions = {}
            wk, w = weakest(trades, positions)
            new_edge = EDGE_R.get(t.get("setup"), 0.0)
            if w is None or new_edge <= EDGE_R.get(w.get("setup"), 0.0):
                out.append((t["symbol"], "omitido: limite de %d operaciones vivas" % max_positions()))
                continue
            if dry_run:
                out.append((w["symbol"], "reemplazo (dry-run) por %s" % t["symbol"]))
            else:
                try:
                    retire(api, w, "reemplazada por %s (%s)" % (t["symbol"], t.get("setup")))
                    out.append((w["symbol"], "reemplazada por %s" % t["symbol"]))
                except PaperError as e:
                    out.append((t["symbol"], "omitido: no se pudo liberar %s (%s)" % (w["symbol"], e)))
                    continue
        if dry_run:
            out.append((t["symbol"], "orden paper (dry-run): %s %s %s limite %s, TP %s, SL %s" % (
                body["side"], body["qty"], body["symbol"], body["limit_price"], body["take_profit"]["limit_price"],
                body["stop_loss"]["stop_price"])))
            continue
        try:
            o = api.submit(body)
        except PaperError as e:
            trades[k] = {"symbol": t["symbol"], "status": "rejected", "why": str(e), "signal_date": t.get("signal_date")}
            out.append((t["symbol"], "rechazada: %s" % e))
            continue
        m = t.get("timemachine") or {}
        trades[k] = {"symbol": t["symbol"], "side": body["side"], "qty": int(body["qty"]), "setup": t.get("setup"),
                     "signal_date": t.get("signal_date"), "time_stop_date": t.get("time_stop_date"),
                     "plan_entry": t.get("entry"), "limit_price": float(body["limit_price"]),
                     "stop": float(t["stop"]), "target": float(t["target"]),
                     "risk_per_share": abs(float(t["entry"]) - float(t["stop"])),
                     "predicted": {"p_win": m.get("p_win"), "expectancy_R": m.get("expectancy_R"),
                                   "p_target": m.get("p_target"), "p_stop": m.get("p_stop")},
                     "order_id": o.get("id"), "client_order_id": body["client_order_id"], "status": "submitted",
                     "submitted_at": (now or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")}
        out.append((t["symbol"], "orden paper enviada"))
    return out


# ---------------------------------------------------------------- sync

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def realized_R(tr, exit_px):
    entry = tr.get("entry_fill")
    risk = tr.get("risk_per_share") or 0
    if entry is None or not risk or exit_px is None:
        return None
    move = (exit_px - entry) if tr.get("side") == "buy" else (entry - exit_px)
    return round(move / risk, 3)


def _et_day(stamp):
    try:
        utc = datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return to_eastern(utc).date().isoformat()


def sync(api, state, today, after_close=False):
    """Read fills back, cancel limit entries that did not fill on their day, close what passed its
    time stop. after_close: today's session is over, so today's unfilled entries are cancelled too.
    -> [(symbol, what)]."""
    out = []
    for k, tr in (state.get("trades") or {}).items():
        if tr.get("status") not in ("submitted", "open", "closing") or not tr.get("order_id"):
            continue
        try:
            o = api.order(tr["order_id"])
        except PaperError as e:
            out.append((tr["symbol"], "no se pudo leer: %s" % e))
            continue
        st = o.get("status")
        if tr["status"] == "submitted":
            if st == "filled":
                tr.update(status="open", entry_fill=_f(o.get("filled_avg_price")), entry_at=o.get("filled_at"))
                out.append((tr["symbol"], "entrada ejecutada"))
            elif st in ("canceled", "expired", "rejected"):
                tr.update(status="not_filled", why=st)
                out.append((tr["symbol"], "entrada no ejecutada (%s)" % st))
                continue
            else:
                day = _et_day(tr.get("submitted_at"))
                if st in ("new", "accepted", "pending_new", "held") and day and (today > day or after_close):
                    try:
                        api.cancel(tr["order_id"])
                        tr.update(status="not_filled", why="limite no alcanzado")
                        out.append((tr["symbol"], "entrada no ejecutada (limite no alcanzado, cancelada)"))
                    except PaperError as e:
                        out.append((tr["symbol"], "no se pudo cancelar la entrada: %s" % e))
                continue
        legs = o.get("legs") or []
        exit_leg = next((l for l in legs if l.get("status") == "filled"), None)
        if tr["status"] == "closing" and tr.get("close_order_id"):
            try:
                c = api.order(tr["close_order_id"])
            except PaperError:
                c = {}
            if c.get("status") == "filled":
                px = _f(c.get("filled_avg_price"))
                why = tr.get("close_reason") or "time"
                tr.update(status="closed", outcome=why, exit_fill=px, exit_at=c.get("filled_at"),
                          R=realized_R(tr, px))
                out.append((tr["symbol"], "cerrada por stop de tiempo" if why == "time" else "cerrada (reemplazada)"))
            continue
        if exit_leg:
            px = _f(exit_leg.get("filled_avg_price"))
            outcome = "target" if exit_leg.get("type") == "limit" else "stop"
            tr.update(status="closed", outcome=outcome, exit_fill=px, exit_at=exit_leg.get("filled_at"),
                      R=realized_R(tr, px))
            out.append((tr["symbol"], "cerrada en %s" % ("el objetivo" if outcome == "target" else "el stop")))
            continue
        ts = tr.get("time_stop_date")
        if tr["status"] == "open" and ts and today > ts:
            for l in legs:
                if l.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled"):
                    try:
                        api.cancel(l["id"])
                    except PaperError:
                        pass
            try:
                c = api.close_position(tr["symbol"])
                tr.update(status="closing", close_order_id=c.get("id"))
                out.append((tr["symbol"], "stop de tiempo: cierre a mercado enviado"))
            except PaperError as e:
                out.append((tr["symbol"], "stop de tiempo: no se pudo cerrar (%s)" % e))
    return out


# ---------------------------------------------------------------- leftover orders

OPEN_STATES = ("new", "accepted", "held", "pending_new", "partially_filled", "accepted_for_bidding",
               "pending_replace", "pending_cancel")


def origin(cid):
    cid = cid or ""
    return "swing" if cid.startswith("swing-") else "caja" if cid.startswith("orb-") else "otra"


def is_entry(o):
    """An order that opens a trade: a bracket/OTO parent, a swing entry, or a box entry (orb-...-in)."""
    cid = o.get("client_order_id") or ""
    return bool(o.get("legs")) or o.get("order_class") in ("bracket", "oto") or cid.startswith("swing-") or cid.endswith("-in")


def leftover_orders(orders, positions):
    """Open orders with nothing behind them. -> [(order, why, sure)].

    sure=True (safe to cancel): an exit that no longer has a position to close: a sell with no long
    position or a buy with no short position, when it is a leg of a filled entry, a box exit (orb-...),
    or any sell (a sell with no shares would open a short). A pending entry is not a leftover (sync cancels
    the stale ones); an unknown standalone buy is only reported."""
    qty = {}
    for p in positions or []:
        try:
            qty[str(p.get("symbol")).upper()] = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            pass
    out = []
    for o in orders or []:
        parent_open = o.get("status") in OPEN_STATES
        if parent_open and is_entry(o):
            continue                                       # a pending entry, its legs are held with it
        for x in ([o] if parent_open else []) + list(o.get("legs") or []):
            if x.get("status") not in OPEN_STATES:
                continue
            sym = str(x.get("symbol") or o.get("symbol") or "").upper()
            q = qty.get(sym, 0.0)
            side = x.get("side")
            if side == "sell" and q > 0 or side == "buy" and q < 0:
                continue                                   # closes an open position: fine
            leg = x is not o
            known = leg or origin(x.get("client_order_id")) == "caja"
            if side == "sell":
                out.append((x, "venta sin posicion comprada" + (" (pata de una entrada ya cerrada)" if leg else ""), True))
            elif known:
                out.append((x, "compra de cierre sin posicion vendida" + (" (pata)" if leg else ""), True))
            else:
                out.append((x, "compra suelta sin posicion (no es una salida conocida: no se toca)", False))
    return out


def orders_check(api, cancel=False):
    """-> (lines without quantities or prices, counts)."""
    orders = api.call("GET", "/v2/orders?status=open&limit=500&nested=true") or []
    positions = api.positions() or []
    found = leftover_orders(orders, positions)
    lines, done = [], 0
    for o, why, sure in found:
        what = "%s %s %s (%s, %s): %s" % (str(o.get("symbol") or "").upper(), o.get("side"), o.get("type"),
                                           origin(o.get("client_order_id")), o.get("status"), why)
        if cancel and sure:
            try:
                api.cancel(o["id"])
                done += 1
                what += " -> cancelada"
            except PaperError as e:
                what += " -> no se pudo cancelar (%s)" % e
        lines.append(what)
    n_open = sum(1 for o in orders for x in [o] + list(o.get("legs") or []) if x.get("status") in OPEN_STATES)
    return lines, {"open": n_open, "positions": len(positions), "leftover": len(found),
                   "sure": sum(1 for f in found if f[2]), "cancelled": done}


# ---------------------------------------------------------------- end of day

NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
EVENING_FROM = 16 * 60 + 5
EVENING_TO = 22 * 60


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def sessions_left(today, until):
    """Trading days after today up to and including `until` (the time-stop day)."""
    try:
        a, b = datetime.date.fromisoformat(today), datetime.date.fromisoformat(str(until)[:10])
    except ValueError:
        return None
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if trading_day(d):
            n += 1
    return n


def evening_check(now_utc, state, force=False):
    et = to_eastern(now_utc)
    today = et.date().isoformat()
    if force:
        return True, today
    if not trading_day(et.date()):
        return False, "hoy no hubo sesion en Nueva York"
    mins = et.hour * 60 + et.minute
    if not EVENING_FROM <= mins < EVENING_TO:
        return False, "en Nueva York son las %s: el resumen del dia va de 16:05 a 21:59" % et.strftime("%H:%M")
    if state.get("last_daily") == today:
        return False, "el resumen de hoy ya se envio"
    return True, today


def _m(x):
    return "${:,.2f}".format(float(x))


def _d(iso):
    s = str(iso or "")
    return "%s-%s" % (s[8:10], s[5:7]) if len(s) >= 10 else s


def daily_text(state, positions, today, equity=None):
    """The end-of-day message: open trades marked to the close, what closed and what did not fill today."""
    trades = list((state.get("trades") or {}).values())
    open_ = [t for t in trades if t.get("status") in ("open", "closing")]
    closed = [t for t in trades if t.get("status") == "closed" and _et_day(t.get("exit_at")) == today]
    unfilled = [t for t in trades if t.get("status") == "not_filled" and _et_day(t.get("submitted_at")) == today]
    lines = ["\U0001F319 <b>Cierre %s-%s</b> \u00b7 tus operaciones (paper, Alpaca)" % (today[8:10], today[5:7]), ""]
    total_pl = 0.0
    if open_:
        lines.append("<b>Abiertas (%d)</b>" % len(open_))
        for t in sorted(open_, key=lambda x: x["symbol"]):
            p = positions.get(t["symbol"]) or {}
            short = t.get("side") == "sell"
            cur = _f(p.get("current_price"))
            entry = t.get("entry_fill") or t.get("limit_price") or t.get("plan_entry")
            pl = _f(p.get("unrealized_pl"))
            if pl is not None:
                total_pl += pl
            head = "%s <b>%s</b> %s \u00b7 %s acc \u00b7 entrada %s" % (
                "\U0001F534" if short else "\U0001F7E2", t["symbol"], "SHORT" if short else "LONG", t.get("qty"),
                _m(entry) if entry else "?")
            if cur is not None and entry:
                move = (cur / float(entry) - 1) * 100
                r = ((entry - cur) if short else (cur - entry)) / (t.get("risk_per_share") or 1)
                head += " \u2192 <b>%s</b> (%+.1f%%) \u00b7 <b>%+.2fR</b>" % (_m(cur), move, r)
            if pl is not None:
                head += " \u00b7 %s%s" % ("+" if pl >= 0 else "-", _m(abs(pl)))
            lines.append(head)
            detail = "   Stop %s \u00b7 Objetivo %s" % (_m(t["stop"]), _m(t["target"]))
            if cur:
                detail = "   Stop %s (a %.1f%%) \u00b7 Objetivo %s (a %.1f%%)" % (
                    _m(t["stop"]), abs(t["stop"] / cur - 1) * 100, _m(t["target"]), abs(t["target"] / cur - 1) * 100)
            left = sessions_left(today, t.get("time_stop_date"))
            if t.get("time_stop_date"):
                detail += " \u00b7 salida por tiempo %s%s" % (_d(t["time_stop_date"]),
                                                             (" (%d días hábiles)" % left) if left is not None else "")
            lines.append(detail)
        lines.append("")
    else:
        lines += ["Sin operaciones abiertas.", ""]
    if closed:
        lines.append("<b>Cerradas hoy (%d)</b>" % len(closed))
        for t in closed:
            word = {"target": "\u2705 objetivo", "stop": "\u274c stop", "time": "\u23f3 por tiempo",
                    "replaced": "\U0001F501 reemplazada"}.get(t.get("outcome"), "")
            pl = None
            if t.get("exit_fill") is not None and t.get("entry_fill") is not None:
                pl = (t["exit_fill"] - t["entry_fill"]) * t.get("qty", 0) * (-1 if t.get("side") == "sell" else 1)
            lines.append("%s <b>%s</b> \u00b7 %s \u00b7 <b>%+.2fR</b>%s" % (
                "\u2022", t["symbol"], word, t.get("R") or 0, (" \u00b7 %s%s" % ("+" if pl >= 0 else "-", _m(abs(pl))))
                if pl is not None else ""))
        lines.append("")
    if unfilled:
        lines.append("<b>No entraron hoy</b> (el precio no llegó al límite): %s" % ", ".join(t["symbol"] for t in unfilled))
        lines.append("")
    if open_:
        lines.append("Resultado abierto: <b>%s%s</b>" % ("+" if total_pl >= 0 else "-", _m(abs(total_pl))))
    if equity is not None:
        lines.append("Patrimonio de la cuenta paper: <b>%s</b>" % _m(equity))
    lines += ["", "<i>Cuenta simulada: sin dinero real. No es asesoría financiera.</i>"]
    return "\n".join(lines)


def daily(api, state, now_utc, force=False):
    """-> (text or None, why). Syncs after the close, then builds the end-of-day message."""
    ok, today = evening_check(now_utc, state, force)
    if not ok:
        return None, today
    events = sync(api, state, today, after_close=True)
    try:
        positions = {str(p.get("symbol")).upper(): p for p in (api.positions() or [])}
    except PaperError:
        positions = {}
    try:
        equity = api.account().get("equity")
    except PaperError:
        equity = None
    state["last_daily"] = today
    return daily_text(state, positions, today, equity), events


# ---------------------------------------------------------------- report

STUDY = {"earnings_gap": (0.60, 0.52), "breakout20": (0.17, 0.47), "pullback_ema20": (0.17, 0.47),
         "rsi2": (0.17, 0.47)}    # (R per trade, winrate) on real history: notify_telegram.STUDY
SPY_PATH = "/v8/finance/chart/SPY?range=1y&interval=1d"


def spy_daily():
    """SPY's daily bars {New York date: (open, close)} for the last year from Alpaca's market data
    (trader-scanner/marketdata.py, the ALPACA_API_KEY_ID keys). {} without them: the report says so.
    Market data only: this file never talks to any trading host but the paper one."""
    text = None
    md = sys.modules.get("trader_marketdata")
    if md is None:
        try:
            spec = importlib.util.spec_from_file_location(
                "trader_marketdata", os.path.join(os.path.dirname(HERE), "trader-scanner", "marketdata.py"))
            md = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(md)
            sys.modules["trader_marketdata"] = md
        except Exception:
            md = None
    try:
        if md is not None and md.enabled():
            text = md.yahoo_like(SPY_PATH)
    except Exception:
        text = None
    if text is None:
        return {}
    return parse_spy(text)


def parse_spy(text):
    try:
        r = json.loads(text)["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
    except (ValueError, KeyError, TypeError, IndexError):
        return {}
    out = {}
    for i, ts in enumerate(r.get("timestamp") or []):
        o, c = q["open"][i], q["close"][i]
        if ts is None or c is None:
            continue
        d = to_eastern(datetime.datetime.utcfromtimestamp(int(ts))).date().isoformat()
        out[d] = (float(o if o is not None else c), float(c))
    return out


def vs_spy(closed, spy):
    """[(trade %, SPY %)] over the same days: SPY from the entry day's open to the exit day's close."""
    out = []
    for t in closed:
        d0, d1 = _et_day(t.get("entry_at")), _et_day(t.get("exit_at"))
        e, x = t.get("entry_fill"), t.get("exit_fill")
        if not (d0 and d1 and e and x) or d0 not in spy or d1 not in spy:
            continue
        move = (x / e - 1) * (-1 if t.get("side") == "sell" else 1)
        out.append((100 * move, 100 * (spy[d1][1] / spy[d0][0] - 1)))
    return out


def summary(state, equity=None, spy=None):
    trades = list((state.get("trades") or {}).values())
    closed = [t for t in trades if t.get("status") == "closed" and t.get("R") is not None]
    open_ = [t for t in trades if t.get("status") in ("submitted", "open", "closing")]
    lines = ["\U0001F4D2 <b>Paper trading (Alpaca, cuenta simulada)</b>", ""]
    if closed:
        wins = [t for t in closed if t["R"] > 0]
        avg = sum(t["R"] for t in closed) / len(closed)
        st = [STUDY[t["setup"]] for t in closed if t.get("setup") in STUDY]
        by = {o: sum(1 for t in closed if t.get("outcome") == o) for o in ("target", "stop", "time", "replaced")}
        lines.append("Cerradas: <b>%d</b> (%d objetivo, %d stop, %d por tiempo%s)" % (
            len(closed), by["target"], by["stop"], by["time"],
            (", %d reemplazadas" % by["replaced"]) if by["replaced"] else ""))
        lines.append("Winrate real: <b>%.0f%%</b>%s" % (100.0 * len(wins) / len(closed),
                     (" · estudio %.0f%%" % (100.0 * sum(w for _, w in st) / len(st))) if st else ""))
        lines.append("R promedio real: <b>%+.2fR</b>%s" % (avg, (" · estudio %+.2fR" % (sum(r for r, _ in st) / len(st)))
                                                          if st else ""))
        lines.append("Total: <b>%+.1fR</b>" % sum(t["R"] for t in closed))
        if spy is not None:
            pairs = vs_spy(closed, spy)
            if pairs:
                mt, ms = sum(a for a, _ in pairs) / len(pairs), sum(b for _, b in pairs) / len(pairs)
                beat = sum(1 for a, b in pairs if a > b)
                lines.append("\U0001F4C8 Contra el S&amp;P 500 (mismos días): operaciones <b>%+.2f%%</b> · SPY <b>%+.2f%%</b> "
                             "· diferencia <b>%+.2f pts</b> · le ganó en %d de %d" % (mt, ms, mt - ms, beat, len(pairs)))
            else:
                lines.append("\U0001F4C8 Contra el S&amp;P 500: sin precios de SPY para esas fechas.")
        if len(closed) < 30:
            lines.append("<i>Menos de 30 operaciones: todavía es una muestra chica.</i>")
    else:
        lines.append("Todavía no hay operaciones cerradas.")
    lines.append("Abiertas o pendientes: <b>%d</b> de %d%s" % (len(open_), max_positions(),
                                                               (" (%s)" % ", ".join(t["symbol"] for t in open_)) if open_ else ""))
    skipped = [t for t in trades if t.get("status") in ("skipped", "rejected", "not_filled")]
    if skipped:
        lines.append("Sin operar: %d (menos de 1 acción, rechazadas, no ejecutadas o reemplazadas)" % len(skipped))
    if equity is not None:
        lines.append("Patrimonio de la cuenta paper: <b>$%s</b>" % "{:,.2f}".format(float(equity)))
    lines += ["", "<i>Simulado: sin dinero real. No es asesoría financiera.</i>"]
    return "\n".join(lines)


def counts(res):
    """[(symbol, what)] -> '2 enviadas, 1 omitida' (no symbols: safe for public logs)."""
    kinds = (("orden paper enviada", "enviada(s)"), ("dry-run", "simulada(s) (dry-run)"), ("omitido", "omitida(s)"),
             ("rechazada", "rechazada(s)"), ("ya operado", "ya operada(s)"), ("entrada ejecutada", "entrada(s) ejecutada(s)"),
             ("no ejecutada", "entrada(s) no ejecutada(s)"), ("objetivo", "cerrada(s) en el objetivo"),
             ("el stop", "cerrada(s) en el stop"), ("stop de tiempo", "stop(s) de tiempo"), ("no se pudo", "error(es)"),
             ("sin chequeo", "sin chequeo de risk de hoy"), ("reemplaz", "reemplazada(s)"))
    n = {}
    why = (("ya hay una operacion viva", "omitida(s): ya abierta en esa accion"),
           ("limite de", "omitida(s): limite de operaciones"),
           ("menos de 1 accion", "omitida(s): menos de 1 accion entera"),
           ("pre-mercado", "omitida(s): invalida en el pre-mercado"))
    for _, what in res:
        label = next((lab for key, lab in why if key in what), None) or \
            next((lab for key, lab in kinds if key in what), "otro(s)")
        n[label] = n.get(label, 0) + 1
    return ", ".join("%d %s" % (v, k) for k, v in n.items())


def paper_on():
    return str(os.environ.get("PAPER_TRADING") or "").strip().lower() in ("1", "true", "si", "yes", "on")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper trading of the swing setups on Alpaca (simulated account).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("account", "sync", "submit", "report", "daily", "orders"):
        p = sub.add_parser(name)
        p.add_argument("--state", default=STATE)
        p.add_argument("--now")
        p.add_argument("--quiet", action="store_true", help="counts only (public CI logs)")
        if name == "submit":
            p.add_argument("--tickets", default="trader/swing_orders.json")
            p.add_argument("--risk", default="trader/state/swing_risk.json")
            p.add_argument("--dry-run", action="store_true")
        if name in ("report", "daily"):
            p.add_argument("--telegram", action="store_true")
        if name == "daily":
            p.add_argument("--force", action="store_true", help="send it now, whatever the clock says")
        if name == "orders":
            p.add_argument("--cancel-orphans", action="store_true",
                           help="cancel the open exit orders that have no position behind them")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        now = parse_now(a.now)
        today = to_eastern(now).date().isoformat()
        state = read_json(a.state, {}) or {}
        if a.cmd == "submit" and a.dry_run:
            api = None
        else:
            api = Paper()
        if a.cmd == "account":
            acc = api.account()
            print("Cuenta paper de Alpaca: estado %s%s." % (acc.get("status"),
                  "" if a.quiet else ", patrimonio $%s" % acc.get("equity")))
            return 0
        if a.cmd == "orders":
            cancel = a.cancel_orphans and paper_on()
            if a.cancel_orphans and not cancel:
                print("PAPER_TRADING no es 1: solo se listan, no se cancela nada.")
            lines, c = orders_check(api, cancel)
            print("Ordenes abiertas: %d | posiciones: %d | sin posicion detras: %d (seguras de cancelar %d) | "
                  "canceladas: %d" % (c["open"], c["positions"], c["leftover"], c["sure"], c["cancelled"]))
            for line in lines:
                print("  " + line)
            return 0
        if a.cmd == "daily":
            if not paper_on():
                print("PAPER_TRADING no esta activo: sin resumen del dia.")
                return 0
            text, events = daily(api, state, now, a.force)
            if text is None:
                print("Resumen del dia: no se envia (%s)." % events)
                return 0
            write_json(a.state, state)
            if a.telegram:
                n = _notify()
                tok, chat = n.credentials()
                n.send(tok, chat, text)
                print("Resumen del dia enviado a Telegram (%s)." % (counts(events) or "sin cambios"))
            else:
                print(text if not a.quiet else "Resumen del dia listo (%s)." % (counts(events) or "sin cambios"))
            return 0
        if a.cmd == "submit":
            if not a.dry_run and not paper_on():
                print("PAPER_TRADING no es 1: no se envian ordenes paper (usa --dry-run para verlas).")
                return 0
            res = submit(api, read_json(a.tickets, {}), read_json(a.risk, {}), state, today, a.dry_run, now)
        elif a.cmd == "sync":
            res = sync(api, state, today)
        else:
            equity = None
            try:
                equity = api.account().get("equity")
            except PaperError:
                pass
            text = summary(state, equity, spy_daily())
            if a.telegram:
                n = _notify()
                tok, chat = n.credentials()
                n.send(tok, chat, text)
                print("Reporte paper enviado a Telegram.")
            elif not a.quiet:
                print(text)
            return 0
        if not (a.cmd == "submit" and a.dry_run):
            state["last_%s" % a.cmd] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            write_json(a.state, state)
        if a.quiet:
            print("paper %s: %s." % (a.cmd, counts(res) or "nada que hacer"))
        else:
            for sym, what in res:
                print("%-6s %s" % (sym, what))
    except PaperError as e:
        print("paper_trader.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
