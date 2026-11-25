#!/usr/bin/env python3
"""The data behind the dashboard (site/, published on GitHub Pages). Standard library only. READ ONLY.

    python3 trader-alerts/dashboard_data.py --out site/data            # in Actions, with the Alpaca keys
    python3 trader-alerts/dashboard_data.py --out site/data --offline DIR   # from saved Yahoo charts (preview)

It writes:
  today.json      the 5 minute bars of the last session for QQQ, SPY, DIA and IWM, the 09:00-10:00 box and
                  the first close outside it (public market data: no account data).
  history.json    the 5 minute bars (09:00-16:00) of the last 20 sessions, to draw a whole trade (public).
  paths.json      the bars of the last ~25 sessions, as moves relative to the previous close, for the trade
                  simulator (also public market data).
  private.json    the paper account: equity, positions, the box strategy's trades and today's events,
                  ENCRYPTED with the passphrase in DASHBOARD_KEY. The repository and its site are public, so
                  account data never goes out in clear (the README's privacy rule). Without DASHBOARD_KEY
                  the file is not written.

The encryption, standard library only: PBKDF2-HMAC-SHA256 (210000 rounds, random salt) makes 64 bytes; the
first 32 key an HMAC-SHA256 counter-mode keystream (block i = HMAC(k1, nonce || i)), the last 32 an
HMAC-SHA256 tag over salt, nonce and ciphertext (encrypt-then-MAC). site/app.js undoes it with WebCrypto.
The ciphertext is public: use a long random passphrase (20+ characters).

It never places, changes or cancels an order: only GET requests, through paper_trader.Paper.
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ROUNDS = 210000


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


orb = _load("dash_orb_paper", "trader-alerts/orb_paper.py")
paper, scan = orb.paper, orb.scan


# ---------------------------------------------------------------- encryption

def _keystream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt(obj, passphrase, rounds=ROUNDS):
    plain = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, rounds, 64)
    ct = bytes(a ^ b for a, b in zip(plain, _keystream(k[:32], nonce, len(plain))))
    tag = hmac.new(k[32:], salt + nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "pbkdf2-sha256", "rounds": rounds, "salt": b64(salt), "nonce": b64(nonce),
            "ct": b64(ct), "tag": b64(tag)}


def decrypt(doc, passphrase):
    d = lambda k: base64.b64decode(doc[k])
    salt, nonce, ct, tag = d("salt"), d("nonce"), d("ct"), d("tag")
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, int(doc["rounds"]), 64)
    if not hmac.compare_digest(hmac.new(k[32:], salt + nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("wrong passphrase or damaged file")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _keystream(k[:32], nonce, len(ct)))).decode("utf-8"))


# ---------------------------------------------------------------- market data (public)

def _wall(t):
    """A New York wall-clock datetime -> seconds as if it were UTC (charts then show New York time)."""
    return int((t - datetime.datetime(1970, 1, 1)).total_seconds())


def last_session(now_et):
    d = now_et.date()
    if now_et.time() < datetime.time(9, 0) or not scan.trading_day(d)[0]:
        d -= datetime.timedelta(days=1)
        while not scan.trading_day(d)[0]:
            d -= datetime.timedelta(days=1)
    return d


def today_doc(bars_by_sym, now_utc):
    now_et = scan.to_eastern(now_utc)
    day = last_session(now_et)
    open_now = scan.trading_day(now_et.date())[0] and datetime.time(9, 30) <= now_et.time() < datetime.time(16, 0)
    out = {"updated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "now_ny": now_et.strftime("%Y-%m-%d %H:%M"),
           "session": day.isoformat(), "market_open": bool(open_now), "strategy": orb.STRATEGY, "symbols": {}}
    ref = now_et if day == now_et.date() else datetime.datetime.combine(day, datetime.time(20, 0))
    for sym, bars in bars_by_sym.items():
        mine = [b for b in bars if b["t"].date() == day]
        box = orb.box_of(mine, day)
        sig = orb.first_signal(mine, day, box, ref) if box else None
        out["symbols"][sym] = {
            "bars": [[_wall(b["t"]), b["o"], b["h"], b["l"], b["c"]] for b in mine],
            "box": {"high": box[0], "low": box[1], "bars": box[2]} if box else None,
            "signal": {"time": _wall(sig[0]["t"] + datetime.timedelta(minutes=5)), "dir": sig[1], "close": sig[0]["c"],
                       "stop": box[1] if sig[1] > 0 else box[0],
                       "partial": round(sig[0]["c"] + sig[1] * 1.7 * abs(sig[0]["c"] - (box[1] if sig[1] > 0 else box[0])), 2)}
            if sig else None,
            "last": mine[-1]["c"] if mine else None}
    return out


def history_doc(bars_by_sym, now_utc, sessions=20):
    """The 5 minute bars from 09:00 to 16:00 New York of the last sessions, to draw a whole trade (public)."""
    out = {"updated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "symbols": {}}
    for sym, bars in bars_by_sym.items():
        rth = [b for b in bars if datetime.time(9, 0) <= b["t"].time() < datetime.time(16, 0)]
        keep = set(sorted({b["t"].date() for b in rth})[-sessions:])
        out["symbols"][sym] = [[_wall(b["t"]), round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2)]
                               for b in rth if b["t"].date() in keep]
    return out


def paths_doc(bars_by_sym, sessions=25):
    """Per symbol, the last sessions as lists of [high, low, close] relative to the previous close (x1e5)."""
    out = {"note": "5 minute bars, pre-market and after-hours included; each value is (price / previous close - 1) x 100000",
           "symbols": {}}
    for sym, bars in bars_by_sym.items():
        days = {}
        prev = None
        for b in bars:
            if prev:
                rel = lambda x: int(round((x / prev - 1) * 1e5))
                days.setdefault(b["t"].date().isoformat(), []).append([rel(b["h"]), rel(b["l"]), rel(b["c"])])
            prev = b["c"]
        keep = sorted(days)[-sessions:]
        out["symbols"][sym] = [{"day": d, "bars": days[d]} for d in keep]
    return out


def fetch(now_utc, days=40, offline=None):
    """5 minute bars, consolidated tape (15 minutes late) for the history and IEX for the last minutes."""
    syms = orb.symbols()
    if offline:
        out = {}
        for s in syms:
            series = scan.parse_chart(scan._read_maybe_gz(os.path.join(offline, s + ".json")), s)
            out[s] = [{"t": scan.ts_to_eastern(t), "o": o, "h": h, "l": l, "c": c}
                      for t, o, h, l, c in zip(series["t"], series["o"], series["h"], series["l"], series["c"])
                      if None not in (o, h, l, c)]
        return out
    md = scan._marketdata()
    start = md._iso(now_utc - datetime.timedelta(days=days))
    hist = md.multi_bars(syms, "5Min", start, md._iso(now_utc - datetime.timedelta(minutes=16)), "sip")
    live = md.multi_bars(syms, "5Min", md._iso(now_utc - datetime.timedelta(minutes=40)), md._iso(now_utc), "iex")
    out = {}
    for s in syms:
        bars = orb.ny_bars(hist.get(s))
        last = bars[-1]["t"] if bars else None
        bars += [b for b in orb.ny_bars(live.get(s)) if last is None or b["t"] > last]
        out[s] = bars
    return out


# ---------------------------------------------------------------- the account (private)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# the box trades rebuilt from the paper orders live in orb_paper (the Telegram close report uses them too)
trades_from_orders = orb.trades_from_orders


def private_doc(acct, now_utc, state=None):
    api = acct.api
    account = api.account() or {}
    positions = api.positions() or []
    orders = acct.orders("all", None, 500)
    try:
        hist = api.call("GET", "/v2/account/portfolio/history?period=3M&timeframe=1D") or {}
    except paper.PaperError:
        hist = {}
    trades = trades_from_orders(orders)
    pos = {p["symbol"]: p for p in positions}
    for t in trades:
        p = pos.get(t["symbol"]) if t["open"] else None
        if p and t["entry"] and t["stop"]:
            last = _f(p.get("current_price"))
            t["last"] = last
            t["unrealized_R"] = round((last - t["entry"]) * t["dir"] / abs(t["entry"] - t["stop"]), 3)
            t["open_qty"] = abs(_f(p.get("qty")) or 0)
    said = (state or {}).get("said") or {}
    return {
        "updated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account": {"equity": _f(account.get("equity")), "cash": _f(account.get("cash")),
                    "last_equity": _f(account.get("last_equity")), "buying_power": _f(account.get("buying_power"))},
        "positions": [{"symbol": p.get("symbol"), "qty": _f(p.get("qty")), "side": p.get("side"),
                       "entry": _f(p.get("avg_entry_price")), "last": _f(p.get("current_price")),
                       "pnl": _f(p.get("unrealized_pl"))} for p in positions],
        "trades": trades,
        "equity_history": {"t": hist.get("timestamp") or [], "equity": hist.get("equity") or []},
        "notes": {d: v for d, v in said.items()},
    }


SWING_FIELDS = ("symbol", "side", "qty", "setup", "signal_date", "time_stop_date", "limit_price", "entry_fill", "entry_at",
                "stop", "target", "risk_per_share", "status", "outcome", "exit_fill", "exit_at", "R", "why", "predicted")


def live_swing(api, t):
    """What Alpaca says now about a swing trade the saved state may not know yet (the bot syncs it twice a
    day): an entry that filled, or an exit leg that filled. Read only. -> a copy of the trade."""
    t = dict(t)
    if t.get("status") not in ("submitted", "open", "closing") or not t.get("order_id"):
        return t
    try:
        o = api.order(t["order_id"])
    except (paper.PaperError, OSError, ValueError, AttributeError):
        return t
    if t["status"] == "submitted":
        if o.get("status") == "filled":
            t.update(status="open", entry_fill=_f(o.get("filled_avg_price")), entry_at=o.get("filled_at"), live=True)
        elif o.get("status") in ("canceled", "expired", "rejected"):
            t.update(status="not_filled", why=o.get("status"), live=True)
        else:
            return t
    leg = next((x for x in o.get("legs") or [] if x.get("status") == "filled"), None)
    if t.get("status") in ("open", "closing") and leg is not None:
        px = _f(leg.get("filled_avg_price"))
        t.update(status="closed", outcome="target" if leg.get("type") == "limit" else "stop", exit_fill=px,
                 exit_at=leg.get("filled_at"), R=paper.realized_R(t, px) if px else None, live=True)
    return t


def swing_doc(api, now_utc, state):
    """The swing setups' paper trades (trader-alerts/paper_trader.py state) with the live prices of the open
    ones, the study's expectations and the record. Never in clear: main() encrypts it."""
    account = api.account() or {}
    positions = {str(p.get("symbol") or "").upper(): p for p in (api.positions() or [])}
    trades = []
    for t in ((state or {}).get("trades") or {}).values():
        t = live_swing(api, t)
        row = {k: t.get(k) for k in SWING_FIELDS + ("live",) if t.get(k) is not None}
        if t.get("status") in ("open", "closing"):
            p = positions.get(str(t.get("symbol") or "").upper())
            entry, risk = _f(t.get("entry_fill")), _f(t.get("risk_per_share"))
            if p is not None:
                last = _f(p.get("current_price"))
                row["last"] = last
                row["pnl"] = _f(p.get("unrealized_pl"))
                if entry and risk and last:
                    d = 1 if t.get("side") == "buy" else -1
                    row["unrealized_R"] = round((last - entry) * d / risk, 3)
        trades.append(row)
    trades.sort(key=lambda r: (r.get("signal_date") or "", r.get("symbol") or ""))
    closed = [t for t in trades if t.get("status") == "closed" and t.get("R") is not None]
    return {
        "updated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account": {"equity": _f(account.get("equity")), "cash": _f(account.get("cash")),
                    "last_equity": _f(account.get("last_equity"))},
        "trades": trades,
        "study": {k: {"R": v[0], "win": v[1]} for k, v in paper.STUDY.items()},
        "max_positions": paper.max_positions(),
        "record": {"closed": len(closed), "R": round(sum(t["R"] for t in closed), 3),
                   "wins": sum(1 for t in closed if t["R"] > 0)},
        "last_sync": (state or {}).get("last_sync"), "last_submit": (state or {}).get("last_submit"),
    }


# ---------------------------------------------------------------- main

def write(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the dashboard's data (read only).")
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--offline", metavar="DIR", help="saved Yahoo charts SYMBOL.json instead of Alpaca (no account)")
    ap.add_argument("--now", help="New York time, e.g. '2026-11-18 11:00'")
    a = ap.parse_args(argv)
    now_utc = scan.parse_now(a.now) if a.now else paper.utc_now()
    os.makedirs(a.out, exist_ok=True)
    bars = fetch(now_utc, offline=a.offline)
    write(os.path.join(a.out, "today.json"), today_doc(bars, now_utc))
    write(os.path.join(a.out, "paths.json"), paths_doc(bars))
    write(os.path.join(a.out, "history.json"), history_doc(bars, now_utc))
    key = os.environ.get("DASHBOARD_KEY") or ""
    priv = os.path.join(a.out, "private.json")
    if a.offline:
        print("offline: sin datos de la cuenta")
    elif len(key) < 12:
        print("DASHBOARD_KEY falta o es muy corta (12+ caracteres): la parte privada no se publica")
        if os.path.exists(priv):
            os.remove(priv)
    else:
        acct = orb.Account(paper.Paper())
        write(priv, encrypt(private_doc(acct, now_utc, paper.read_json(orb.STATE, {})), key))
        write(os.path.join(a.out, "private_swing.json"), encrypt(swing_doc(acct.api, now_utc, paper.read_json(paper.STATE, {})), key))
        print("private.json y private_swing.json cifrados")
    print("datos en %s (%s)" % (a.out, now_utc.strftime("%Y-%m-%d %H:%M UTC")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
