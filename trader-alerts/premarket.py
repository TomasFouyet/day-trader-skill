#!/usr/bin/env python3
"""Pre-market check of the swing tickets, before the US open. Standard library only.

    python3 premarket.py                     # annotate trader/swing_orders.json with the pre-market price
    python3 premarket.py --gap-warn-pct 4 --json
    python3 premarket.py --offline DIR --now "2026-11-09 09:05"   # <SYM>.json(.gz) charts, no network

The swing setups come from yesterday's close. Between that close and today's
open the price can gap: through the stop (the setup failed), toward the
target (less left to win, a bigger risk per share) or just far. For each
ticket this reads the latest pre-market price (Yahoo v8 chart, range=1d,
interval=1m, includePrePost=true: free, unofficial, delayed, thin) and sets
ticket["premarket"]:

  invalid   the price is already at or beyond the stop, at or beyond the
            target, or the reward:risk from that price is under min_reward_risk
  warn      valid, but it gapped more than --gap-warn-pct (3 %, or
            swing.gap_warn_pct) from yesterday's close
  ok        valid
  unknown   no pre-market trade in the data: the plan stays as it was

For a valid ticket it works out the trade from the pre-market price: the risk
per share (to the same stop), the reward:risk (to the same target) and the
size, cut so the loss at the stop stays inside the ticket's max loss. It never
sizes UP. Leveraged venues are scaled the same way, with the liquidation price
moved with the price. The stop, target and time stop do not move.

After 9:30 New York it reads the latest regular-session price instead, and says so.
It never places an order. Not financial advice.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
HOSTS = ("query2", "query1")
GAP_WARN_PCT = 3.0
OPEN_MIN = 9 * 60 + 30


class PremarketError(Exception):
    pass


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
    if t.tzinfo is None:
        for off in (-4, -5):
            utc = t - datetime.timedelta(hours=off)
            if eastern_offset(utc) == off:
                return utc
        return t + datetime.timedelta(hours=5)
    return (t - t.utcoffset()).replace(tzinfo=None)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _marketdata():
    """trader-scanner/marketdata.py (Alpaca, when its keys are set), one shared instance per process."""
    m = sys.modules.get("trader_marketdata")
    if m is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-scanner",
                            "marketdata.py")
        if not os.path.exists(path):
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("trader_marketdata", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules["trader_marketdata"] = m
        spec.loader.exec_module(m)
    return m


def _alpaca(path_or_url):
    md = _marketdata()
    return md.yahoo_like(path_or_url) if md is not None and md.enabled() else None


def fetch(sym):
    path = "/v8/finance/chart/%s?range=1d&interval=1m&includePrePost=true" % sym.upper().replace(".", "-")
    alp = _alpaca(path)
    if alp is not None:
        return alp
    last = None
    for host in HOSTS:
        try:
            return _get("https://%s.finance.yahoo.com%s" % (host, path))
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            e.close()
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
        time.sleep(0.5)
    raise PremarketError("Yahoo no respondio para %s (%s)" % (sym, last))


def read_offline(folder, sym):
    for name in ("%s.json" % sym.upper(), "%s.json.gz" % sym.upper()):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8") as fh:
                return fh.read()
    raise PremarketError("no hay %s.json(.gz) en %s" % (sym.upper(), folder))


def latest_price(text, sym, now_utc):
    """-> (price, New York time 'HH:MM', 'pre' or 'regular') of the latest 1-minute bar today, or Nones."""
    try:
        r = json.loads(text)["chart"]["result"][0]
    except (ValueError, KeyError, TypeError, IndexError):
        raise PremarketError("Yahoo devolvio algo inesperado para %s" % sym)
    if str((r.get("meta") or {}).get("symbol") or sym).upper() != sym.upper().replace(".", "-"):
        raise PremarketError("el archivo es de %s, no de %s" % (r["meta"].get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    today = to_eastern(now_utc).date()
    best = None
    for i, ts in enumerate(r.get("timestamp") or []):
        c = closes[i] if i < len(closes) else None
        if ts is None or c is None:
            continue
        utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts))
        if utc > now_utc:
            continue
        et = to_eastern(utc)
        if et.date() != today:
            continue
        best = (float(c), et)
    if not best:
        return None, None, None
    p, et = best
    return p, et.strftime("%H:%M"), ("pre" if et.hour * 60 + et.minute < OPEN_MIN else "regular")


def assess(t, price, at, phase, gap_warn_pct=GAP_WARN_PCT, min_rr=1.0):
    """Pure: a swing ticket + the latest price -> the "premarket" block."""
    short = str(t.get("side") or "buy") == "sell"
    close, stop, target = float(t["entry"]), float(t["stop"]), float(t["target"])
    out = {"price": None, "at": at, "phase": phase, "close": close, "status": "unknown"}
    if price is None:
        out["note"] = "sin operaciones de pre-mercado: vale el plan con el cierre de ayer"
        return out
    gap = 100.0 * (price / close - 1)
    out.update(price=round(price, 2), gap_pct=round(gap, 2))
    beyond_stop = price >= stop if short else price <= stop
    beyond_target = price <= target if short else price >= target
    risk = abs(price - stop)
    rr = abs(target - price) / risk if risk > 0 else 0.0
    out["reward_risk"] = round(rr, 2)
    if beyond_stop:
        out.update(status="invalid", note="el precio ya paso el stop (%.2f): el setup fallo" % stop)
        return out
    if beyond_target:
        out.update(status="invalid", note="el precio ya llego al objetivo (%.2f): no queda recorrido" % target)
        return out
    if min_rr and rr < float(min_rr) - 1e-9:
        out.update(status="invalid", note="desde %.2f la recompensa:riesgo es %.2f, bajo tu minimo %s"
                                        % (price, rr, ("%g" % float(min_rr))))
        return out
    max_loss = float(t.get("max_loss") or 0)
    qty = float(t["qty"])
    old_risk = abs(close - stop)
    factor = min(1.0, old_risk / risk) if risk > 0 else 1.0       # never size up
    out.update(risk_per_share=round(risk, 2), factor=round(factor, 4))
    out["qty"] = _scale_qty(qty, factor, t)
    out["max_loss"] = round(out["qty"] * risk, 2)
    if max_loss and out["max_loss"] > max_loss + 0.01:
        out["qty"] = _scale_qty(qty, max_loss / (qty * risk), t)
        out["max_loss"] = round(out["qty"] * risk, 2)
    venues = {}
    for name, v in (t.get("venues") or {}).items():
        if v.get("refusal") or not v.get("qty"):
            continue
        q0 = float(v["qty"])
        step = 0.001 if v.get("leveraged") else (1 if q0.is_integer() else 0.0001)
        vq = _round_down(q0 * factor, step)
        nv = {"qty": vq, "notional": round(vq * price, 2)}
        if v.get("leveraged"):
            lev = float(v.get("leverage") or 1)
            nv["margin"] = round(nv["notional"] / lev, 2)
            if v.get("liquidation"):
                liq = float(v["liquidation"]) * price / close
                nv["liquidation"] = round(liq, 2)
                if (short and liq <= stop) or (not short and liq >= stop):
                    nv["warning"] = "la liquidacion queda antes del stop: baja el apalancamiento"
        venues[name] = nv
    out["venues"] = venues
    if abs(gap) > gap_warn_pct:
        out.update(status="warn", note="gap de %+.1f%% desde el cierre de ayer: el tamano se ajusto" % gap)
    else:
        out.update(status="ok", note="sigue valido")
    return out


def _fractional(t):
    q = t.get("qty")
    return isinstance(q, float) and not float(q).is_integer()


def _round_down(x, step):
    return round(math.floor(x / step + 1e-9) * step, 6) if step < 1 else int(math.floor(x + 1e-9))


def _scale_qty(qty, factor, t):
    step = 0.0001 if _fractional(t) else (0.001 if t.get("leverage", 1) and float(t.get("leverage") or 1) > 1 else 1)
    return _round_down(qty * factor, step)


def run(tickets, now_utc, getter, gap_warn_pct=GAP_WARN_PCT, min_rr=1.0):
    out = []
    for t in tickets:
        sym = t["symbol"]
        try:
            price, at, phase = latest_price(getter(sym), sym, now_utc)
            block = assess(t, price, at, phase, gap_warn_pct, min_rr)
        except PremarketError as e:
            block = {"status": "unknown", "note": "sin dato de pre-mercado (%s): vale el plan" % e,
                     "close": t.get("entry")}
        block["checked_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        t["premarket"] = block
        out.append((sym, block))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pre-market check of the swing tickets (gap, validity, size).")
    ap.add_argument("--tickets", default="trader/swing_orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--gap-warn-pct", type=float)
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not os.path.exists(a.tickets):
            raise PremarketError("no hay %s (corre trader-budget primero)" % a.tickets)
        with open(a.tickets, encoding="utf-8") as fh:
            doc = json.load(fh)
        cfg = {}
        if a.config and os.path.exists(a.config):
            with open(a.config, encoding="utf-8") as fh:
                cfg = json.load(fh)
        sw = cfg.get("swing") if isinstance(cfg.get("swing"), dict) else {}
        gap = a.gap_warn_pct if a.gap_warn_pct is not None else float(sw.get("gap_warn_pct", GAP_WARN_PCT))
        now = parse_now(a.now)
        getter = (lambda s: read_offline(a.offline, s)) if a.offline else fetch
        res = run(doc.get("tickets") or [], now, getter, gap, cfg.get("min_reward_risk", 1.0))
        if not a.no_write:
            with open(a.tickets + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
                fh.write("\n")
            os.replace(a.tickets + ".tmp", a.tickets)
    except (PremarketError, ValueError) as e:
        print("premarket.py: %s" % e, file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(dict(res), indent=2))
    else:
        for sym, b in res:
            px = ("%.2f (%s ET, %s, %+.1f%%)" % (b["price"], b["at"], "pre-mercado" if b["phase"] == "pre" else "sesion",
                                               b["gap_pct"])) if b.get("price") is not None else "sin dato"
            print("%-6s %-8s %s: %s" % (sym, b["status"].upper(), px, b.get("note", "")))
        print("No se coloco nada. Pre-mercado de Yahoo: gratis, atrasado y con poco volumen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
