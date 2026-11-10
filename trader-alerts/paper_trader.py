#!/usr/bin/env python3
"""Paper trading of the swing setups on Alpaca, to build a real track record. Standard library only.

    python3 paper_trader.py account                  # is the paper account reachable? (read only)
    python3 paper_trader.py sync                     # read fills, close time stops, update the journal
    python3 paper_trader.py submit --dry-run         # the orders it would place for today's setups
    python3 paper_trader.py submit                   # place them (needs PAPER_TRADING=1)
    python3 paper_trader.py report                   # the track record; --telegram sends it

PAPER ONLY. Every request goes to https://paper-api.alpaca.markets, a simulated
account: no real money. The host is fixed in this file and anything else is
refused. It never loads trader-broker and never touches a live account. It
places orders only when PAPER_TRADING=1 is in the environment, besides the
keys ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY.

submit: each ticket that passed today's trader-risk check (trader/state/
swing_risk.json, same fingerprint) and is not "invalid" in the pre-market check
becomes ONE bracket order: market entry at the open, a take-profit limit at the
target and a stop at the stop, all good-till-cancelled. Whole shares only
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
    body = {"symbol": str(t["symbol"]).upper(), "qty": str(whole), "side": "sell" if short else "buy",
            "type": "market", "time_in_force": "gtc", "order_class": "bracket",
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
    for t in (tickets_doc or {}).get("tickets") or []:
        if notify.swing_fingerprint(t) not in passed:
            continue
        k = key_of(t)
        if k in trades:
            out.append((t["symbol"], "ya operado"))
            continue
        body, why = planned(t)
        if body is None:
            trades[k] = {"symbol": t["symbol"], "status": "skipped", "why": why, "signal_date": t.get("signal_date")}
            out.append((t["symbol"], "omitido: " + why))
            continue
        if dry_run:
            out.append((t["symbol"], "orden paper (dry-run): %s %s %s, TP %s, SL %s" % (
                body["side"], body["qty"], body["symbol"], body["take_profit"]["limit_price"],
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
                     "plan_entry": t.get("entry"), "stop": float(t["stop"]), "target": float(t["target"]),
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


def sync(api, state, today):
    """Read fills back, close what passed its time stop. -> [(symbol, what)]."""
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
                tr.update(status="closed", outcome="time", exit_fill=px, exit_at=c.get("filled_at"),
                          R=realized_R(tr, px))
                out.append((tr["symbol"], "cerrada por stop de tiempo"))
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


# ---------------------------------------------------------------- report

def summary(state, equity=None):
    trades = list((state.get("trades") or {}).values())
    closed = [t for t in trades if t.get("status") == "closed" and t.get("R") is not None]
    open_ = [t for t in trades if t.get("status") in ("submitted", "open", "closing")]
    lines = ["\U0001F4D2 <b>Paper trading (Alpaca, cuenta simulada)</b>", ""]
    if closed:
        wins = [t for t in closed if t["R"] > 0]
        avg = sum(t["R"] for t in closed) / len(closed)
        pw = [t["predicted"]["p_win"] for t in closed if (t.get("predicted") or {}).get("p_win") is not None]
        pe = [t["predicted"]["expectancy_R"] for t in closed
              if (t.get("predicted") or {}).get("expectancy_R") is not None]
        by = {o: sum(1 for t in closed if t.get("outcome") == o) for o in ("target", "stop", "time")}
        lines.append("Cerradas: <b>%d</b> (%d objetivo, %d stop, %d por tiempo)" % (
            len(closed), by["target"], by["stop"], by["time"]))
        lines.append("Winrate real: <b>%.0f%%</b>%s" % (100.0 * len(wins) / len(closed),
                     (" · estimado %.0f%%" % (100.0 * sum(pw) / len(pw))) if pw else ""))
        lines.append("R promedio real: <b>%+.2fR</b>%s" % (avg, (" · expectativa estimada %+.2fR" % (sum(pe) / len(pe)))
                                                          if pe else ""))
        lines.append("Total: <b>%+.1fR</b>" % sum(t["R"] for t in closed))
        if len(closed) < 30:
            lines.append("<i>Menos de 30 operaciones: todavía es una muestra chica.</i>")
    else:
        lines.append("Todavía no hay operaciones cerradas.")
    lines.append("Abiertas o pendientes: <b>%d</b>%s" % (len(open_), (" (%s)" % ", ".join(t["symbol"] for t in open_))
                                                          if open_ else ""))
    skipped = [t for t in trades if t.get("status") in ("skipped", "rejected", "not_filled")]
    if skipped:
        lines.append("Sin operar: %d (menos de 1 acción, rechazadas o no ejecutadas)" % len(skipped))
    if equity is not None:
        lines.append("Patrimonio de la cuenta paper: <b>$%s</b>" % "{:,.2f}".format(float(equity)))
    lines += ["", "<i>Simulado: sin dinero real. No es asesoría financiera.</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper trading of the swing setups on Alpaca (simulated account).")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("account", "sync", "submit", "report"):
        p = sub.add_parser(name)
        p.add_argument("--state", default=STATE)
        p.add_argument("--now")
        p.add_argument("--quiet", action="store_true", help="counts only (public CI logs)")
        if name == "submit":
            p.add_argument("--tickets", default="trader/swing_orders.json")
            p.add_argument("--risk", default="trader/state/swing_risk.json")
            p.add_argument("--dry-run", action="store_true")
        if name == "report":
            p.add_argument("--telegram", action="store_true")
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
        if a.cmd == "submit":
            if not a.dry_run and os.environ.get("PAPER_TRADING") != "1":
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
            text = summary(state, equity)
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
            print("paper %s: %d accion(es)." % (a.cmd, len(res)))
        else:
            for sym, what in res:
                print("%-6s %s" % (sym, what))
    except PaperError as e:
        print("paper_trader.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
