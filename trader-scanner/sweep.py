#!/usr/bin/env python3
"""Sweep scanner: liquidity sweep -> market structure shift (MSS) -> entry, on 30-minute bars.

A replica of the TradingView scanner the user trades with ("Trading Scanner": liquidity swing
length 8, internal structure length 3, max bars sweep->MSS 20, setup->entry 30, in trade 200,
take profit 2R, min stop 0.2 x ATR). Its source is closed; the rules below were fitted to three
of its charts (NOW, APP, SPCX, 30-minute bars to 2026-11-13) and reproduce its levels to the cent:

  1. Liquidity   the most recent confirmed swing high and swing low (a pivot with `swing_len`
                 bars on each side). Stops rest beyond them.
  2. Sweep       a bar that trades through the level and closes back inside: high above the swing
                 high and close below it (bearish, a short idea), or low under the swing low and
                 close above it (bullish). While one sweep waits, later sweeps are ignored; price
                 trading beyond the sweep bar's extreme cancels it (and that bar can sweep anew).
  3. MSS         the close breaks the internal structure against the sweep: under the last
                 confirmed internal swing low (pivot `internal_len`) as it stood on the sweep bar
                 (bearish), or over the internal swing high (bullish). At most `max_sweep_to_mss`
                 bars after the sweep.
  4. Entry       a limit at the near edge of the fair value gap (FVG) the break left behind: for a
                 short, the high of the third candle of the latest three-candle gap (low of the
                 first above the high of the third) between the sweep and the break. It has
                 `max_setup_to_entry` bars to fill. Price running to the target first does not
                 cancel it (the indicator keeps it).
  5. Stop        the sweep bar's extreme, at least `min_stop_atr` x ATR(14) from the
                 entry. Target `take_profit_r` x the risk. Out at the close after
                 `max_bars_in_trade` bars. In a bar that touches both, the stop counts.

Fitted, not known: where the entry goes when the break leaves no gap (here: wait up to 2 bars for
one, else drop the idea) and the Quality score (here: a transparent 0-100 of our own: displacement,
sweep depth, gap size, speed, risk size and trend, NOT the indicator's formula).

    python3 sweep.py scan                      # the table, like the indicator's
    python3 sweep.py scan --symbols NVDA,TSLA
    python3 sweep.py backtest                  # every trade the rules took in the bars on hand
    python3 sweep.py backtest --flat-eod       # day-trading variant: nothing held overnight

Symbols: --symbols, --watchlist FILE, config "sweep": {"symbols": [...]}, else
trader-scanner/sweep_watchlist.txt. Bars: Yahoo's 30-minute chart (60 days), or Alpaca's when its
keys are set. Regular session only, the bar in progress left out. Standard library only. It never
places an order: a scan is a list of charts that match a rule, not a list of stocks to buy.
"""

import argparse
import datetime
import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("trader_scan_for_sweep", os.path.join(HERE, "scan.py"))
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

DEFAULTS = {"swing_len": 8, "internal_len": 3, "max_sweep_to_mss": 20, "max_setup_to_entry": 30,
            "max_bars_in_trade": 200, "take_profit_r": 2.0, "min_stop_atr": 0.2, "atr_len": 14,
            "min_quality": 0, "fvg_wait": 2, "interval": "30m", "range": "60d"}
INTS = ("swing_len", "internal_len", "max_sweep_to_mss", "max_setup_to_entry", "max_bars_in_trade", "atr_len",
        "min_quality", "fvg_wait")
WATCHLIST = os.path.join(HERE, "sweep_watchlist.txt")
OUT = "trader/sweep.json"
BAR_MINUTES = {"30m": 30, "15m": 15, "1h": 60, "5m": 5}
NOTE = "Un escaneo es una lista de graficos que cumplen una regla, no una lista de acciones para comprar."


class SweepError(Exception):
    pass


# ---------------------------------------------------------------- bars

def session_bars(series, now_utc, minutes=30):
    """A Yahoo-shaped chart series -> regular-session bars oldest first, the bar in progress dropped.
    [{"t" (New York datetime), "day", "o", "h", "l", "c", "v"}]"""
    out = {}
    cols = [series.get(k) or [] for k in ("o", "h", "l", "c", "v")]
    now = scan.to_eastern(now_utc)
    for i, ts in enumerate(series.get("t") or []):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or c is None:
            continue
        t = scan.ts_to_eastern(ts)
        s = scan.session(t.date())
        if s is None or not (s[0] <= t.time() < s[1]):
            continue
        if t.minute % minutes and minutes < 60:
            continue                                   # an odd stamp: not a bar boundary
        if t + datetime.timedelta(minutes=minutes) > now:
            continue                                   # still forming
        o = c if o is None else o
        out[t] = {"t": t, "day": t.date().isoformat(), "o": float(o), "h": float(max(c, o) if h is None else h),
                  "l": float(min(c, o) if l is None else l), "c": float(c), "v": float(v or 0)}
    return [out[k] for k in sorted(out)]


def atr_series(bars, n=14):
    """Wilder's ATR (TradingView's ta.atr): None until n bars."""
    out, prev, trs = [None] * len(bars), None, []
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]),
                                                  abs(b["l"] - bars[i - 1]["c"]))
        if prev is None:
            trs.append(tr)
            if len(trs) == n:
                prev = sum(trs) / n
        else:
            prev = (prev * (n - 1) + tr) / n
        out[i] = prev
    return out


def pivot_confirmed(bars, i, n, high):
    """The pivot confirmed AT bar i (the pivot bar is i - n, n bars on each side), or None."""
    p = i - n
    if p - n < 0:
        return None
    key = "h" if high else "l"
    v = bars[p][key]
    left = [bars[k][key] for k in range(p - n, p)]
    right = [bars[k][key] for k in range(p + 1, i + 1)]
    if high:
        return v if all(x <= v for x in left) and all(x < v for x in right) else None
    return v if all(x >= v for x in left) and all(x > v for x in right) else None


def find_fvg(bars, lo, hi, direction):
    """The latest fair value gap whose three candles lie in bars[lo..hi] and that price has not
    come back to since: (near edge, far edge, index of the third candle) or None.
    direction -1 (short): low of the first candle above the high of the third."""
    for k in range(hi, lo + 1, -1):
        a, c = bars[k - 2], bars[k]
        if direction < 0 and a["l"] > c["h"]:
            near, far = c["h"], a["l"]
            if all(bars[j]["h"] < near for j in range(k + 1, hi + 1)) and bars[hi]["c"] < near:
                return near, far, k
        if direction > 0 and a["h"] < c["l"]:
            near, far = c["l"], a["h"]
            if all(bars[j]["l"] > near for j in range(k + 1, hi + 1)) and bars[hi]["c"] > near:
                return near, far, k
    return None


def quality(bars, atr, st, fvg, trend):
    """Our own 0-100 score of a setup (not the indicator's): how hard price left, how deep the sweep
    was, how big the gap is, how fast the break came, a sane risk and the trend behind it."""
    a = atr or 1e-9
    d = st["dir"]
    body = max(abs(bars[k]["c"] - bars[k]["o"]) for k in range(st["sweep_i"] + 1, st["mss_i"] + 1))
    depth = abs(st["extreme"] - st["level"])
    gap = abs(fvg[1] - fvg[0])
    speed = 1.0 - (st["mss_i"] - st["sweep_i"]) / float(max(1, st["max_wait"]))
    risk = abs(st["stop"] - st["entry"]) / a
    risk_ok = 1.0 if 0.5 <= risk <= 3.0 else (risk / 0.5 if risk < 0.5 else max(0.0, 1 - (risk - 3.0) / 3.0))
    with_trend = 0.5 if trend is None else (1.0 if (bars[st["mss_i"]]["c"] - trend) * d > 0 else 0.0)
    score = (25 * min(1.0, body / (2 * a)) + 20 * min(1.0, depth / a) + 15 * min(1.0, gap / a)
             + 15 * max(0.0, speed) + 10 * risk_ok + 15 * with_trend)
    return int(round(score))


# ---------------------------------------------------------------- the state machine

def run(bars, params=None, flat_eod=False):
    """Walk the bars once. -> {"trades": [...], "state": {...}} (the state after the last bar)."""
    p = dict(DEFAULTS, **(params or {}))
    n8, n3 = p["swing_len"], p["internal_len"]
    atr = atr_series(bars, p["atr_len"])
    closes_sum, trend = 0.0, [None] * len(bars)
    for i, b in enumerate(bars):                       # the 200-bar average, for the quality score only
        closes_sum += b["c"]
        if i >= 200:
            closes_sum -= bars[i - 200]["c"]
        if i >= 199:
            trend[i] = closes_sum / 200.0
    ph8 = pl8 = ph3 = pl3 = None
    st = {"stage": "scan"}
    trades, sweeps = [], 0

    def reset():
        st.clear()
        st["stage"] = "scan"

    def close_trade(i, px, outcome):
        risk = abs(st["stop"] - st["entry"])
        r = (px - st["entry"]) * st["dir"] / risk
        trades.append({"dir": st["dir"], "side": "buy" if st["dir"] > 0 else "sell", "sweep_i": st["sweep_i"],
                       "mss_i": st["mss_i"], "setup_i": st["setup_i"], "entry_i": st["entry_i"], "exit_i": i,
                       "entry": st["entry"], "stop": st["stop"], "target": st["target"], "exit": px,
                       "R": round(r, 3), "outcome": outcome, "quality": st["quality"],
                       "entry_time": bars[st["entry_i"]]["t"].strftime("%Y-%m-%d %H:%M"),
                       "exit_time": bars[i]["t"].strftime("%Y-%m-%d %H:%M"), "bars": i - st["entry_i"]})
        reset()

    def make_setup(i, fvg):
        d = st["dir"]
        entry = fvg[0]
        ext = st["extreme"]
        floor = p["min_stop_atr"] * (atr[i] or 0.0)
        stop = max(ext, entry + floor) if d < 0 else min(ext, entry - floor)
        if (stop - entry) * d >= 0:                     # the gap sits beyond the sweep: no trade
            return False
        st.update(entry=entry, stop=stop, extreme=ext, target=entry + d * p["take_profit_r"] * abs(entry - stop),
                  setup_i=i, max_wait=p["max_sweep_to_mss"], fvg=[fvg[0], fvg[1]])
        st["quality"] = quality(bars, atr[i], st, fvg, trend[i])
        if st["quality"] < p["min_quality"]:
            return False
        st["stage"] = "setup"
        return True

    for i, b in enumerate(bars):
        for n, high in ((n8, True), (n8, False), (n3, True), (n3, False)):
            v = pivot_confirmed(bars, i, n, high)
            if v is not None:
                if n == n8 and high:
                    ph8 = v
                elif n == n8:
                    pl8 = v
                elif high:
                    ph3 = v
                else:
                    pl3 = v
        last_of_day = i + 1 == len(bars) or bars[i + 1]["day"] != b["day"]
        stage = st["stage"]

        if stage == "open":
            d, stop, tgt = st["dir"], st["stop"], st["target"]
            if (b["o"] - stop) * d <= 0:                                   # gapped through the stop
                close_trade(i, b["o"], "stop")
            elif (b["l"] <= stop) if d > 0 else (b["h"] >= stop):
                close_trade(i, stop, "stop")
            elif (b["h"] >= tgt) if d > 0 else (b["l"] <= tgt):
                close_trade(i, tgt, "target")
            elif i - st["entry_i"] >= p["max_bars_in_trade"]:
                close_trade(i, b["c"], "time")
            elif flat_eod and last_of_day and i + 1 < len(bars):
                close_trade(i, b["c"], "eod")
            continue

        if stage == "setup":
            d, entry, stop = st["dir"], st["entry"], st["stop"]
            if i - st["setup_i"] > p["max_setup_to_entry"]:
                reset()
            elif (b["o"] - stop) * d <= 0:                                 # opened beyond the stop
                reset()
            elif (b["l"] <= entry) if d > 0 else (b["h"] >= entry):
                st.update(stage="open", entry_i=i)
                if (b["l"] <= stop) if d > 0 else (b["h"] >= stop):        # filled and stopped in one bar
                    close_trade(i, stop, "stop")
                elif flat_eod and last_of_day and i + 1 < len(bars):
                    close_trade(i, b["c"], "eod")
                continue
            elif flat_eod and last_of_day:
                reset()
            else:
                continue
            stage = st["stage"]

        if stage in ("swept", "mss") and ((b["h"] > st["extreme"]) if st["dir"] < 0 else (b["l"] < st["extreme"])):
            reset()                                                        # beyond the sweep: the idea is dead
            stage = "scan"

        if stage == "mss":                                                 # broke, waiting for the gap
            fvg = find_fvg(bars, st["sweep_i"], i, st["dir"])
            if fvg and make_setup(i, fvg):
                continue
            if i - st["mss_i"] >= p["fvg_wait"]:
                reset()
            else:
                continue
            stage = st["stage"]

        if stage == "swept":
            d = st["dir"]
            if i - st["sweep_i"] > p["max_sweep_to_mss"] or (flat_eod and b["day"] != bars[st["sweep_i"]]["day"]):
                reset()
            elif (b["c"] - st["mss_level"]) * d > 0:                       # the market structure shift
                st["mss_i"] = i
                fvg = find_fvg(bars, st["sweep_i"], i, d)
                if fvg and make_setup(i, fvg):
                    continue
                st["stage"] = "mss"
                continue
            else:
                continue                                                   # one sweep at a time

        # scanning: a sweep of the latest swing high or low
        up = ph8 is not None and b["h"] > ph8 and b["c"] < ph8 and pl3 is not None and pl3 < b["c"]
        dn = pl8 is not None and b["l"] < pl8 and b["c"] > pl8 and ph3 is not None and ph3 > b["c"]
        if up != dn:
            sweeps += 1
            st.clear()
            st.update(stage="swept", dir=-1 if up else 1, sweep_i=i, level=ph8 if up else pl8,
                      extreme=b["h"] if up else b["l"], mss_level=pl3 if up else ph3)

    last = len(bars) - 1
    state = dict(st)
    state["bars"] = None
    if st["stage"] == "swept":
        state["bars"] = last - st["sweep_i"]
    elif st["stage"] in ("mss", "setup"):
        state["bars"] = last - st.get("setup_i", st["mss_i"])
    elif st["stage"] == "open":
        risk = abs(st["stop"] - st["entry"])
        state["bars"] = last - st["entry_i"]
        state["R"] = round((bars[last]["c"] - st["entry"]) * st["dir"] / risk, 2)
    for k in ("sweep_i", "mss_i", "setup_i", "entry_i"):
        if k in state:
            state[k.replace("_i", "_time")] = bars[state[k]]["t"].strftime("%Y-%m-%d %H:%M")
    a = atr[last] if bars else None
    state.update(price=bars[last]["c"] if bars else None, atr=a, swing_high=ph8, swing_low=pl8,
                 as_of=bars[last]["t"].strftime("%Y-%m-%d %H:%M") if bars else None)
    if bars and a:
        near = [(abs(v - bars[last]["c"]) / a, name) for v, name in ((ph8, "up"), (pl8, "down")) if v is not None]
        if near:
            state["liq_atr"], state["liq_side"] = round(min(near)[0], 1), min(near)[1]
        if st["stage"] == "setup":
            state["to_entry_atr"] = round(abs(st["entry"] - bars[last]["c"]) / a, 1)
    return {"trades": trades, "state": state, "sweeps": sweeps, "bars": len(bars)}


def random_trades(bars, params, n, seed, flat_eod=False):
    """A baseline with no signal: n random entries, each with a random direction, a stop 1.5 x ATR
    away and the same target, time limit and day rule. -> [R]"""
    p = dict(DEFAULTS, **(params or {}))
    atr = atr_series(bars, p["atr_len"])
    rng = random.Random(seed)
    out = []
    first = max(p["atr_len"] + 1, 30)
    if len(bars) - first < 40:
        return out
    for _ in range(n):
        i = rng.randrange(first, len(bars) - 2)
        d = rng.choice((1, -1))
        entry, risk = bars[i]["c"], 1.5 * atr[i]
        stop, tgt = entry - d * risk, entry + d * p["take_profit_r"] * risk
        r = None
        for j in range(i + 1, min(len(bars), i + 1 + p["max_bars_in_trade"])):
            b = bars[j]
            if (b["o"] - stop) * d <= 0:
                r = (b["o"] - entry) * d / risk
            elif (b["l"] <= stop) if d > 0 else (b["h"] >= stop):
                r = -1.0
            elif (b["h"] >= tgt) if d > 0 else (b["l"] <= tgt):
                r = p["take_profit_r"]
            elif flat_eod and (j + 1 == len(bars) or bars[j + 1]["day"] != b["day"]):
                r = (b["c"] - entry) * d / risk
            if r is not None:
                break
        if r is None:
            r = (bars[min(len(bars) - 1, i + p["max_bars_in_trade"])]["c"] - entry) * d / risk
        out.append(r)
    return out


# ---------------------------------------------------------------- data, config

def sweep_params(cfg):
    sw = (cfg or {}).get("sweep")
    sw = {} if sw is None else sw
    if not isinstance(sw, dict):
        raise SweepError('"sweep" en la config tiene que ser un objeto {...}')
    p = dict(DEFAULTS)
    for k, v in sw.items():
        if k == "symbols":
            continue
        if k not in DEFAULTS:
            raise SweepError("sweep: clave desconocida %r (conocidas: %s)" % (k, ", ".join(sorted(DEFAULTS))))
        try:
            p[k] = v if k in ("interval", "range") else (int(v) if k in INTS else float(v))
        except (TypeError, ValueError):
            raise SweepError("sweep.%s tiene que ser un numero" % k)
    if p["interval"] not in BAR_MINUTES:
        raise SweepError("sweep.interval es uno de %s" % ", ".join(sorted(BAR_MINUTES)))
    for k in ("swing_len", "internal_len", "max_sweep_to_mss", "max_setup_to_entry", "max_bars_in_trade", "atr_len"):
        if p[k] < 1:
            raise SweepError("sweep.%s tiene que ser 1 o mas" % k)
    if p["take_profit_r"] <= 0 or p["min_stop_atr"] < 0:
        raise SweepError("sweep.take_profit_r va sobre 0 y sweep.min_stop_atr no puede ser negativo")
    return p


def read_symbols(a, cfg):
    if a.symbols:
        syms = [s for s in a.symbols.replace(",", " ").split() if s]
    elif a.watchlist:
        syms = scan.symbols_from_file(a.watchlist)
    elif isinstance((cfg.get("sweep") or {}).get("symbols"), list):
        syms = [str(s) for s in cfg["sweep"]["symbols"]]
    elif os.path.exists(WATCHLIST):
        syms = scan.symbols_from_file(WATCHLIST)
    else:
        raise SweepError("sin simbolos: usa --symbols, --watchlist o sweep.symbols en la config")
    out = []
    for s in syms:
        s = s.strip().upper().split(":")[-1]               # NASDAQ:NVDA -> NVDA
        if not scan.TICKER.match(s):
            raise SweepError("%r no es un ticker de accion (las cripto no pasan por este script)" % s)
        if s not in out:
            out.append(s)
    return out


def load(a, symbols, p, now):
    """-> ({SYM: bars}, {SYM: why it failed})"""
    data, failed = {}, {}
    if a.offline:
        series, failed = scan.load_offline(a.offline)
    else:
        series = {}
        for k, sym in enumerate(symbols):
            if k and a.pause:
                scan.SLEEP(a.pause)
            try:
                series[sym] = scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, p["range"], p["interval"])), sym)
            except scan.ScanError as e:
                failed[sym] = str(e)
    for sym in symbols:
        if sym in series:
            bars = session_bars(series[sym], now, BAR_MINUTES[p["interval"]])
            if len(bars) < 60:
                failed[sym] = "menos de 60 velas"
            else:
                data[sym] = bars
        elif sym not in failed:
            failed[sym] = "sin datos"
    return data, failed


# ---------------------------------------------------------------- scan

ARROW = {1: "▲", -1: "▼"}
ORDER = {"open": 0, "setup": 1, "mss": 2, "swept": 3, "scan": 4}


def _px(x):
    return "-" if x is None else ("%.2f" % x if x >= 1 else "%.4f" % x)


def stage_label(s):
    st = s["stage"]
    if st == "scan":
        return "escaneando"
    arrow = ARROW[s["dir"]]
    return {"swept": "%s BARRIDO · esperando MSS", "mss": "%s MSS · esperando FVG",
            "setup": "%s SETUP · esperando entrada", "open": "%s ABIERTA"}[st] % arrow


def row(sym, s):
    last = ""
    if s["stage"] == "open":
        last = "%+.1f R" % s["R"]
    elif s["stage"] == "setup":
        last = "a %s ATR de la entrada" % s.get("to_entry_atr", "?")
    elif s.get("liq_atr") is not None:
        last = "liq %s %s ATR" % (ARROW[1 if s["liq_side"] == "up" else -1], s["liq_atr"])
    has = s["stage"] in ("setup", "open")
    return "%-6s %-30s %4s %4s %9s %9s / %-9s %s" % (
        sym, stage_label(s), "" if s.get("bars") is None else s["bars"], ("Q%d" % s["quality"]) if has else "",
        _px(s["entry"]) if has else "", _px(s["stop"]) if has else "", _px(s["target"]) if has else "", last)


def cmd_scan(a):
    now = scan.parse_now(a.now)
    cfg = scan.read_config(a.config)
    p = sweep_params(cfg)
    symbols = read_symbols(a, cfg)
    data, failed = load(a, symbols, p, now)
    rows = []
    for sym in symbols:
        if sym in data:
            res = run(data[sym], p, a.flat_eod)
            s = res["state"]
            s["symbol"] = sym
            rows.append(s)
    rows.sort(key=lambda s: (ORDER[s["stage"]], -(s.get("quality") or 0), s["symbol"]))
    doc = {"created_by": "trader-scanner sweep", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "interval": p["interval"], "params": {k: p[k] for k in DEFAULTS if k not in ("interval", "range")},
           "flat_eod": bool(a.flat_eod), "failed": failed, "note": NOTE,
           "rows": [{k: v for k, v in s.items() if not k.endswith("_i")} for s in rows]}
    if not a.no_write:
        scan.write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    n = {k: sum(1 for s in rows if s["stage"] == k) for k in ORDER}
    asof = max((s["as_of"] for s in rows), default="nada")
    print("SWEEP SCANNER (%s) · %d abiertas · %d setups · %d barridos · velas hasta %s NY" % (
        p["interval"], n["open"], n["setup"], n["swept"] + n["mss"], asof))
    print("%-6s %-30s %4s %4s %9s %9s / %-9s %s" % ("Accion", "Etapa", "Velas", "Cal.", "Entrada", "Stop", "Objetivo",
                                                   "A la entrada · R · liquidez"))
    for s in rows:
        print(row(s["symbol"], s))
    for sym, why in sorted(failed.items()):
        print("%-6s sin datos: %s" % (sym, why))
    print(NOTE + " Calidad (Cal.) es una nota propia, no la del indicador. Nada se coloca.")
    return 0


# ---------------------------------------------------------------- backtest

def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "win": None, "avg": None, "total": 0.0, "se": None}
    avg = sum(rs) / n
    var = sum((x - avg) ** 2 for x in rs) / n
    return {"n": n, "win": sum(1 for x in rs if x > 0) / float(n), "avg": avg, "total": sum(rs),
            "se": (var / n) ** 0.5}


def _line(name, s):
    if not s["n"]:
        return "%-22s   0 operaciones" % name
    return "%-22s %4d ops  gana %3.0f%%  promedio %+.2fR (+-%.2f)  total %+6.1fR" % (
        name, s["n"], 100 * s["win"], s["avg"], s["se"], s["total"])


def cmd_backtest(a):
    now = scan.parse_now(a.now)
    cfg = scan.read_config(a.config)
    p = sweep_params(cfg)
    symbols = read_symbols(a, cfg)
    data, failed = load(a, symbols, p, now)
    per, all_t, rnd = {}, [], []
    for sym in symbols:
        if sym not in data:
            continue
        res = run(data[sym], p, a.flat_eod)
        per[sym] = res
        for t in res["trades"]:
            all_t.append(dict(t, symbol=sym))
        rnd += random_trades(data[sym], p, a.random, a.seed + len(rnd), a.flat_eod)
    doc = {"params": p, "flat_eod": bool(a.flat_eod), "symbols": len(per), "failed": failed,
           "all": stats([t["R"] for t in all_t]), "long": stats([t["R"] for t in all_t if t["dir"] > 0]),
           "short": stats([t["R"] for t in all_t if t["dir"] < 0]), "random": stats(rnd),
           "by_symbol": {s: stats([t["R"] for t in r["trades"]]) for s, r in per.items()},
           "outcomes": {o: sum(1 for t in all_t if t["outcome"] == o) for o in ("target", "stop", "time", "eod")},
           "trades": all_t}
    if a.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0
    days = sorted({b["day"] for bars in data.values() for b in bars})
    print("Backtest del sweep scanner (%s), %d acciones, %s a %s (%d sesiones)%s." % (
        p["interval"], len(per), days[0] if days else "?", days[-1] if days else "?", len(days),
        ", sin dejar nada abierto de un dia a otro" if a.flat_eod else ""))
    print(_line("Todas", doc["all"]))
    print(_line("  long", doc["long"]))
    print(_line("  short", doc["short"]))
    o = doc["outcomes"]
    print("  salidas: %d objetivo, %d stop, %d por tiempo%s" % (o["target"], o["stop"], o["time"],
                                                               (", %d al cierre del dia" % o["eod"]) if a.flat_eod else ""))
    print(_line("Al azar (sin senal)", doc["random"]))
    print()
    for sym in sorted(per, key=lambda s: -(doc["by_symbol"][s]["total"])):
        print(_line(sym, doc["by_symbol"][sym]))
    for sym, why in sorted(failed.items()):
        print("%-6s sin datos: %s" % (sym, why))
    print("\nPocas semanas de velas: una muestra chica. Sin comisiones ni deslizamiento. Si una vela toca stop y "
          "objetivo, cuenta el stop. No es una prediccion.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sweep scanner: liquidity sweep -> MSS -> FVG entry on 30-minute bars.")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("scan", "backtest"):
        q = sub.add_parser(name)
        q.add_argument("--symbols", help="AAPL,NVDA,...")
        q.add_argument("--watchlist", help="a file, one ticker a line")
        q.add_argument("--config", default="trader/config.json")
        q.add_argument("--offline", metavar="DIR", help="saved Yahoo chart JSON files, SYM.json(.gz)")
        q.add_argument("--now", help="pretend it is this time (New York), e.g. '2026-11-13 16:05'")
        q.add_argument("--pause", type=float, default=0.3)
        q.add_argument("--flat-eod", action="store_true", help="day trading: nothing is held overnight")
        q.add_argument("--json", action="store_true")
        if name == "scan":
            q.add_argument("--out", default=OUT)
            q.add_argument("--no-write", action="store_true")
        else:
            q.add_argument("--random", type=int, default=200, help="random baseline entries per symbol")
            q.add_argument("--seed", type=int, default=19)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return cmd_scan(a) if a.cmd == "scan" else cmd_backtest(a)
    except (SweepError, scan.ScanError) as e:
        print("sweep.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
