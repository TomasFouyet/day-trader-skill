#!/usr/bin/env python3
"""Research: the "estrategia de aperturas" (box strategy) shown by Zcoin on YouTube, against the chosen box.

Its rules, as the video states them (5 minute bars, S&P 500 and Nasdaq):
    box         08:30-10:00 New York (an hour before the open to 30 minutes after; the macro data at 08:30)
    signal      the first 5 minute bar that CLOSES outside the box
    entry       a limit order at the broken edge of the box (the retest); the break and the fill must both
                happen before 12:00 New York, otherwise there is no trade that day
    stop        the other edge of the box
    exits       50% at 1R and the stop of the rest to the entry (breakeven); then 25% at the first RSI(14)
                divergence on 5 minutes and 25% at the first divergence on 1 hour (discretionary in the
                video: here a fixed rule, so only an approximation)

The divergence rule used here: a pivot high (higher than the 2 bars on each side) above the previous pivot high
of the last 60 bars while RSI(14) at it is lower (a long's exit; the mirror for a short), taken at the close of
the bar that confirms the pivot, only for pivots after the entry. The 1 hour bars are the 5 minute bars grouped
by clock hour (extended hours included, like a chart with ETH). Each divergence takes half of what was left
after the first target.

Also: his tentative box-size filter (no trade on a box wider than a % of the price, or than a share of the daily
ATR(14) of the previous days) and our first target (70% at 1.7R) on his box and entry.

Same conventions as trader-scanner/orb.py: one position at a time per index, one try a day, the stop is checked
before a target in the same bar, a gap past the stop fills at the open, each trade sized to 1R and net of a
round-trip cost.

    python3 trader-analyst/orb_zcoin.py --symbols QQQ,SPY,IWM,DIA --days 1827

Aggregates only. Research, never orders.
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


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


optimize = _module("trader_orb_optimize_z", os.path.join(HERE, "orb_optimize.py"))
validate, perf, orb = optimize.validate, optimize.perf, optimize.orb

BASE = ("10:00", "both", "none", "12:55", 1.7, 0.7, 20)

OURS = {"box_start": "09:00", "last_sig": "12:55", "entry": "close", "deadline": "13:00",
        "p1_r": 1.7, "p1_frac": 0.7, "rest": "hold", "hold": 20}
ZC = {"box_start": "08:30", "last_sig": "11:55", "entry": "retest", "deadline": "12:00",
      "p1_r": 1.0, "p1_frac": 0.5, "rest": "eod", "hold": 0}
VERSIONS = [
    ("Nuestra regla (09:00, cierre, 70% a 1,7R, resto 20 dias)", dict(OURS)),
    ("Zcoin: 08:30, retesteo hasta 12:00, 50% a 1R, resto al cierre 16:00", dict(ZC)),
    ("Zcoin, el resto a 2R (o al cierre 16:00)", dict(ZC, rest="2r")),
    ("Zcoin, el resto hasta 20 dias", dict(ZC, rest="hold", hold=20)),
    ("Zcoin con divergencias 5m y 1h (maximo 20 dias)", dict(ZC, rest="div", hold=20)),
    ("Zcoin con divergencias 5m y 1h (maximo 5 dias)", dict(ZC, rest="div", hold=5)),
    ("Zcoin con divergencias, entrada al cierre (sin retesteo)", dict(ZC, rest="div", hold=20, entry="close")),
    ("Caja 08:30-10:00 con nuestra regla", dict(OURS, box_start="08:30")),
    ("Caja 09:00 con entrada y salidas de Zcoin (divergencias)",
     dict(ZC, box_start="09:00", rest="div", hold=20)),
    # the box size (his tentative filter: be careful with an S&P box of more than 30-40 points)
    ("Zcoin, sin cajas de mas de 0,65% del precio (~35 pts del S&P)", dict(ZC, max_pct=0.65)),
    ("Zcoin, sin cajas de mas de 0,75 ATR", dict(ZC, max_atr=0.75)),
    ("Zcoin, sin cajas de mas de 0,50 ATR", dict(ZC, max_atr=0.50)),
    ("Zcoin con divergencias, sin cajas de mas de 0,65%", dict(ZC, rest="div", hold=20, max_pct=0.65)),
    # our first target instead of his: 70% at 1.7R
    ("Zcoin con 70% a 1,7R, resto al cierre 16:00", dict(ZC, p1_r=1.7, p1_frac=0.7)),
    ("Zcoin con 70% a 1,7R, resto hasta 20 dias", dict(ZC, p1_r=1.7, p1_frac=0.7, rest="hold", hold=20)),
    ("Zcoin con 70% a 1,7R y divergencias (maximo 20 dias)", dict(ZC, p1_r=1.7, p1_frac=0.7, rest="div", hold=20)),
    ("Zcoin con 70% a 1,7R, resto 20 dias, sin cajas de mas de 0,65%",
     dict(ZC, p1_r=1.7, p1_frac=0.7, rest="hold", hold=20, max_pct=0.65)),
    ("Nuestra regla, sin cajas de mas de 0,65%", dict(OURS, max_pct=0.65)),
]


def _hm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def rsi(closes, n=14):
    """Wilder's RSI. -> list (None until there are n changes)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for k in range(1, n + 1):
        ch = closes[k] - closes[k - 1]
        gain += max(ch, 0.0)
        loss += max(-ch, 0.0)
    gain /= n
    loss /= n
    out[n] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    for k in range(n + 1, len(closes)):
        ch = closes[k] - closes[k - 1]
        gain = (gain * (n - 1) + max(ch, 0.0)) / n
        loss = (loss * (n - 1) + max(-ch, 0.0)) / n
        out[k] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    return out


def divergences(highs, lows, rs, side=2, lookback=60):
    """Bearish and bullish RSI divergences. -> (bear, bull): {index of the confirming bar: index of the pivot}."""
    bear, bull = {}, {}
    last_hi = last_lo = None
    n = len(highs)
    for p in range(side, n - side):
        if rs[p] is None:
            continue
        win = range(p - side, p + side + 1)
        if all(highs[p] > highs[q] for q in win if q != p):
            if last_hi is not None and p - last_hi <= lookback and highs[p] > highs[last_hi] and rs[last_hi] is not None \
                    and rs[p] < rs[last_hi]:
                bear[p + side] = p
            last_hi = p
        if all(lows[p] < lows[q] for q in win if q != p):
            if last_lo is not None and p - last_lo <= lookback and lows[p] < lows[last_lo] and rs[last_lo] is not None \
                    and rs[p] > rs[last_lo]:
                bull[p + side] = p
            last_lo = p
    return bear, bull


def hourly_divergences(bars):
    """The 1 hour divergences mapped to the 5 minute bar that closes the confirming hour.
    -> (bear, bull): {5 minute index: 5 minute index where the pivot hour starts}."""
    groups, keys = [], []
    for i, b in enumerate(bars):
        key = (b["t"].date(), b["t"].hour)
        if not keys or keys[-1] != key:
            keys.append(key)
            groups.append([i])
        else:
            groups[-1].append(i)
    hi = [max(bars[i]["h"] for i in g) for g in groups]
    lo = [min(bars[i]["l"] for i in g) for g in groups]
    cl = [bars[g[-1]]["c"] for g in groups]
    bear, bull = divergences(hi, lo, rsi(cl))
    return ({groups[c][-1]: groups[p][0] for c, p in bear.items()},
            {groups[c][-1]: groups[p][0] for c, p in bull.items()})


class Context(object):
    """One symbol's bars with what every version needs, computed once."""

    def __init__(self, bars):
        self.bars = bars
        self.days = orb.by_day(bars)
        self.pos = {d: k for k, (d, _) in enumerate(self.days)}
        rs = rsi([b["c"] for b in bars])
        self.bear5, self.bull5 = divergences([b["h"] for b in bars], [b["l"] for b in bars], rs)
        self.bear60, self.bull60 = hourly_divergences(bars)
        self.atr = {}
        trs, prev = [], None
        for d, idx in self.days:
            rth = [bars[i] for i in idx if datetime.time(9, 30) <= bars[i]["t"].time() < datetime.time(16, 0)]
            if not rth:
                continue
            self.atr[d] = sum(trs[-14:]) / 14.0 if len(trs) >= 14 else None
            h, l, c = max(b["h"] for b in rth), min(b["l"] for b in rth), rth[-1]["c"]
            trs.append(max(h - l, abs(h - prev), abs(l - prev)) if prev is not None else h - l)
            prev = c


def walk(ctx, j0, d, entry, stop, ref, cfg, end):
    """The exits from the bar after j0 to `end`. -> (R, outcome, last bar index)."""
    bars = ctx.bars
    risk = (entry - stop) * d
    p1 = ref + d * cfg["p1_r"] * abs(ref - stop)           # the first target, measured from the signal close (or edge)
    t2 = entry + d * 2.0 * risk if cfg["rest"] == "2r" else None

    def rr(x):
        return (x - entry) * d / risk

    def past(x, level):
        return (x - level) * d >= 0
    left, got, st = 1.0, 0.0, stop
    took, d5, d60 = False, False, False
    div5, div60 = (ctx.bear5, ctx.bear60) if d > 0 else (ctx.bull5, ctx.bull60)
    eod = cfg["rest"] in ("eod", "2r")
    for j in range(j0 + 1, end + 1):
        b = bars[j]
        o, fav, adv = b["o"], (b["h"] if d > 0 else b["l"]), (b["l"] if d > 0 else b["h"])
        if not past(o, st) or o == st:
            return got + left * rr(o), ("stop" if not took else "parcial+breakeven"), j
        if not past(adv, st) or adv == st:
            return got + left * rr(st), ("stop" if not took else "parcial+breakeven"), j
        if not took:
            px = o if past(o, p1) else (p1 if past(fav, p1) else None)
            if px is not None:
                took = True
                got += cfg["p1_frac"] * rr(px)
                left -= cfg["p1_frac"]
                st = entry                                     # breakeven for the rest
            continue
        if t2 is not None:
            px = o if past(o, t2) else (t2 if past(fav, t2) else None)
            if px is not None:
                return got + left * rr(px), "parcial+2R", j
        if cfg["rest"] == "div":
            half = (1.0 - cfg["p1_frac"]) / 2.0
            if not d5 and j in div5 and div5[j] > j0:
                d5 = True
                take = min(left, half)
                got += take * rr(b["c"])
                left -= take
            if not d60 and j in div60 and div60[j] > j0:
                d60 = True
                take = min(left, half)
                got += take * rr(b["c"])
                left -= take
            if left <= 1e-9:
                return got, "divergencias", j
    last = bars[end]
    return got + left * rr(last["c"]), ("cierre" if eod else "tiempo"), end


def run_symbol(ctx, cfg):
    """Every trade of one version on one index. -> [(entry day, R before costs, risk % of the price, outcome)]."""
    bars, days = ctx.bars, ctx.days
    b0, b1 = _hm(cfg["box_start"]), _hm("10:00")
    last_sig, deadline, flat = _hm(cfg["last_sig"]), _hm(cfg["deadline"]), _hm("16:00")
    need = max(2, int((b1.hour * 60 + b1.minute - b0.hour * 60 - b0.minute) / 5) // 2)
    out, busy = [], -1
    for d, idx in days:
        box = [i for i in idx if b0 <= bars[i]["t"].time() < b1]
        if len(box) < need:
            continue
        hi, lo = max(bars[i]["h"] for i in box), min(bars[i]["l"] for i in box)
        if cfg.get("max_pct") and (hi - lo) / lo * 100 > cfg["max_pct"]:
            continue                                           # a box too wide: no trade that day
        if cfg.get("max_atr"):
            atr = ctx.atr.get(d)
            if not atr or (hi - lo) > cfg["max_atr"] * atr:
                continue
        sess = [i for i in idx if b1 <= bars[i]["t"].time() < flat]
        if not sess:
            continue
        for i in sess:
            if i <= busy:
                continue
            if bars[i]["t"].time() > last_sig:
                break
            c = bars[i]["c"]
            k = 1 if c > hi else -1 if c < lo else 0
            if not k:
                continue
            stop = lo if k > 0 else hi
            if cfg["entry"] == "retest":
                fill = None
                edge = hi if k > 0 else lo
                for j in sess:
                    if j <= i:
                        continue
                    if bars[j]["t"].time() >= deadline:
                        break
                    if k > 0 and bars[j]["l"] <= edge:
                        px = min(edge, bars[j]["o"])
                        fill = None if px <= stop else (j, px)
                        break
                    if k < 0 and bars[j]["h"] >= edge:
                        px = max(edge, bars[j]["o"])
                        fill = None if px >= stop else (j, px)
                        break
                if fill is None:
                    break                                      # no fill before the deadline: no trade today
                j0, entry, ref = fill[0], fill[1], edge
            else:
                j0, entry, ref = i, c, c
            end = orb.end_index(bars, days, ctx.pos[d], sess[-1], cfg["hold"], flat)
            if end <= j0:
                break
            r, why, last = walk(ctx, j0, k, entry, stop, ref, cfg, end)
            out.append((d.isoformat(), r, abs(entry - stop) / entry * 100, why))
            busy = last
            break                                              # one try a day
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zcoin's opening box against the chosen box.")
    ap.add_argument("--symbols", default="QQQ,SPY,IWM,DIA")
    ap.add_argument("--days", type=int, default=1827)
    ap.add_argument("--cost-pct", type=float, default=0.01)
    ap.add_argument("--offline", metavar="DIR")
    a = ap.parse_args(argv)
    t0 = time.time()
    data = optimize.load([s.strip().upper() for s in a.symbols.split(",") if s.strip()], a.days, a.offline)
    if not data:
        print("Sin datos.")
        return 1
    cal = sorted({b["t"].date().isoformat() for d in data.values() for b in d["bars"]
                  if b["t"].weekday() < 5 and datetime.time(9, 30) <= b["t"].time() < datetime.time(16, 0)})
    mid = cal[len(cal) // 2]
    years = sorted({c[:4] for c in cal})
    p = optimize.params(BASE)
    p["last_entry"] = "12:55"
    check = []
    for d in data.values():
        check += [validate.net_r(t["R"], t["risk_pct"], a.cost_pct) for t in orb.run(d["bars"], p, d["ema"])]
    ctxs = {s: Context(d["bars"]) for s, d in data.items()}
    print("Estrategia de aperturas de Zcoin contra la caja elegida: %s, %s a %s, costo %.3f%% por lado." % (
        ", ".join(sorted(data)), cal[0], cal[-1], a.cost_pct))
    print("Control: el motor de siempre da n %d, %+.3fR por operacion con la regla elegida." % (
        len(check), sum(check) / len(check) if check else 0))
    print("\n   %-62s %5s %7s %6s %6s %7s %7s %6s %7s %7s" % (
        "version", "n", "R/op", "+-", "gana", "Sharpe", "R total", "caida", "1a mit", "2a mit"))
    rows = []
    for name, cfg in VERSIONS:
        ts = []
        for sym, ctx in ctxs.items():
            ts += [(day, validate.net_r(r, rp, a.cost_pct), why, sym) for day, r, rp, why in run_symbol(ctx, cfg)]
        ts.sort()
        xs = [t[1] for t in ts]
        st = validate.tstats(xs) if xs else {"n": 0, "mean": 0.0, "se": 0.0}
        x = validate.daily([(t[0], t[1]) for t in ts], cal)
        h1 = [t[1] for t in ts if t[0] < mid]
        h2 = [t[1] for t in ts if t[0] >= mid]
        outs = {}
        for t in ts:
            outs[t[2]] = outs.get(t[2], 0) + 1
        row = {"name": name, "cfg": cfg, "n": st["n"], "mean": st["mean"], "se": st["se"], "win": st.get("win", 0.0),
               "sharpe": perf.sharpe(x) * math.sqrt(252), "total": sum(xs), "dd": validate.max_dd(x),
               "h1": sum(h1) / len(h1) if h1 else 0.0, "h2": sum(h2) / len(h2) if h2 else 0.0, "outcomes": outs,
               "years_total": {y: sum(t[1] for t in ts if t[0][:4] == y) for y in years},
               "by_symbol": {s: (lambda v: sum(v) / len(v) if v else None)([t[1] for t in ts if t[3] == s])
                             for s in sorted(data)}}
        rows.append(row)
        print("   %-62s %5d %+7.3f %6.3f %5.0f%% %+7.2f %+7.1f %6.1f %+7.3f %+7.3f" % (
            name[:62], row["n"], row["mean"], row["se"], 100 * row["win"], row["sharpe"], row["total"], row["dd"],
            row["h1"], row["h2"]))
    print("\n   (R por operacion despues de costos, a igual riesgo por operacion; Sharpe anual del resultado diario;"
          " caida = peor racha en R)")
    print("\nPor ano (R total):")
    print("   %-62s %s" % ("", " ".join("%7s" % y for y in years)))
    for r in rows:
        print("   %-62s %s" % (r["name"][:62], " ".join("%+7.1f" % v for v in r["years_total"].values())))
    print("\nPor indice (R por operacion):")
    for r in rows:
        print("   %-62s %s" % (r["name"][:62], "  ".join("%s %+.3f" % (s, v) for s, v in r["by_symbol"].items()
                                                          if v is not None)))
    print("\nComo terminan (operaciones):")
    for r in rows:
        print("   %-62s %s" % (r["name"][:62], ", ".join("%s %d" % kv for kv in sorted(r["outcomes"].items()))))
    print("\n@@ORBZCOIN@@ " + json.dumps(validate.clean({"from": cal[0], "to": cal[-1], "check": len(check),
                                                       "rows": rows}), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
