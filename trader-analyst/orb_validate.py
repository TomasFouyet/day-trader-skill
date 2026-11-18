#!/usr/bin/env python3
"""Research: is the 09:00 box breakout (trader-scanner/orb.py) profitable in the long run? The quant tests.

    python3 trader-analyst/orb_validate.py --symbols QQQ,SPY,IWM,DIA --days 1827

1. Backtest of a grid of 288 versions: box end 09:30 / 10:00 / 10:30, both sides / longs / shorts, with and
   without the 15 minute EMA filter, target 1R / 1.7R / 2R / 3R, entries until 13:00 or all day, out at the
   16:00 close or left to run to stop or target (up to 20 trading days, one position at a time). Each after
   costs, with its daily Sharpe (a trade counts on the day it opens). The 3 best by Sharpe, and the 3 best
   chosen on the first half of the period alone, measured on the second half.
2. Deflated Sharpe (Bailey and Lopez de Prado): the best of 288 versions looks good by luck alone; this says
   whether the chosen one beats what luck gives.
3. Monte Carlo, two kinds:
   a. random entries (the null): the same days, the same box as the stop, the same target and close, but a
      random entry time, 300 times. How often does chance do as well as the rules?
   b. month-block bootstrap of the daily results: the spread of one year and of five years, the worst
      drawdown, the chance of a losing year.
4. Walk-forward: every 6 months, pick the best version on the 24 months before and trade it for the next
   6 months only. The out-of-sample result is what choosing by backtest would have earned.
5. Costs: the same at 0 to 0.03% per side.

Prints a report, and one line "@@ORBJSON@@ {...}" with the series for the charts. Aggregates only.
Research, never orders.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


study = _module("trader_orb_study_v", os.path.join(HERE, "orb_study.py"))
perf = _module("trader_perf_v", os.path.join(HERE, "performance.py"))
orb, scan = study.orb, study.scan

BOX_ENDS = ("09:30", "10:00", "10:30")
SIDES = ("both", "long", "short")
FILTERS = ("trend", "none")
TARGETS = (1.0, 1.7, 2.0, 3.0)
LASTS = ("12:55", "15:55")       # entry bars that start by 12:55 (close by 13:00), or all day
HOLD = 20                        # "let it run": up to 20 trading days, to stop or target
HOLDS = (0, HOLD)
USER_KEYS = (
    ("Estrategia 1: caja 09:00-09:30, hasta 13:00, dejar correr", ("09:30", "both", "trend", 3.0, "12:55", HOLD)),
    ("Estrategia 2: caja 09:00-10:30, 1:1,7, hasta 13:00, dejar correr", ("10:30", "both", "trend", 1.7, "12:55", HOLD)),
    ("Estrategia 2: caja 09:00-10:30, 1:1,7, hasta 13:00, cierre 16:00", ("10:30", "both", "trend", 1.7, "12:55", 0)),
    ("Reglas anteriores: caja 09:00-09:30, todo el dia, cierre 16:00", ("09:30", "both", "trend", 3.0, "15:55", 0)),
)
SIDE_ES = {"both": "ambos", "long": "largos", "short": "cortos"}


def label(cfg):
    b, s, f, t, last, hold = cfg
    return "caja 09:00-%s, %s, %s, %gR, %s, %s" % (b, SIDE_ES[s], "filtro" if f == "trend" else "sin filtro", t,
                                                   "hasta 13:00" if last == "12:55" else "todo el dia",
                                                   "dejar correr" if hold else "cierre 16:00")


OPTS = {"entry": "close", "overlap": False, "partial_r": None, "partial_frac": 0.7}     # set for the whole run from --entry and --overlap


def params(cfg):
    b, s, f, t, last, hold = cfg
    bars = (orb._minutes(b) - orb._minutes("09:00")) // 5
    return {"box_start": "09:00", "box_end": b, "sides": s, "filter": f, "target_r": t, "last_entry": last,
            "hold_days": hold, "min_box_bars": max(2, bars // 2), "entry": OPTS["entry"], "overlap": OPTS["overlap"],
            "partial_r": OPTS["partial_r"], "partial_frac": OPTS["partial_frac"]}


def fast_random(d, boxes, cfg, rng, cost_pct):
    """One random strategy on one symbol: each day a random entry bar of the entry window (none while a
    random trade is still open), the filter's direction (or a random one without filter), the box height as
    the stop, the same target and the same exit (16:00 close, or left to run)."""
    b, side, flt, tgt, last, hold = cfg
    bars, ema, days, pos = d["bars"], d["ema"], d["days"], d["pos"]
    last_t, flat = orb._hm(last), orb._hm("16:00")
    out, busy = [], -1
    for day, hi, lo, sess in boxes:
        win = [i for i in sess if bars[i]["t"].time() <= last_t and i > busy]
        if hi <= lo or not win:
            continue
        i = win[rng.randrange(len(win))]
        f, s, n = ema[i]
        if flt == "none":
            k = rng.choice((1, -1))
        else:
            if n < 200 or f == s:
                continue
            k = 1 if f > s else -1
        if side == "long" and k < 0 or side == "short" and k > 0:
            continue
        e = bars[i]["c"]
        end = orb.end_index(bars, days, pos[day], sess[-1], hold, flat)
        r, _, held = orb.simulate(bars, i, k, e, e - k * (hi - lo), tgt, end, OPTS["partial_r"], OPTS["partial_frac"])
        if not OPTS["overlap"]:
            busy = i + held
        out.append((day.isoformat(), net_r(r, (hi - lo) / e * 100, cost_pct)))
    return out


def net_r(r, risk_pct, cost_pct):
    return r - 2 * cost_pct / risk_pct if risk_pct else r


def tstats(xs):
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": xs[0] if xs else 0.0, "se": 0.0, "t": 0.0}
    m = sum(xs) / n
    sd = statistics.pstdev(xs)
    se = sd / math.sqrt(n)
    return {"n": n, "mean": m, "se": se, "t": m / se if se else 0.0,
            "win": sum(1 for x in xs if x > 0) / float(n),
            "pf": (sum(x for x in xs if x > 0) / -sum(x for x in xs if x < 0)) if any(x < 0 for x in xs) else float("inf")}


def max_dd(xs):
    peak = eq = dd = 0.0
    for x in xs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def daily(trades, calendar):
    by = {}
    for t in trades:
        by[t[0]] = by.get(t[0], 0.0) + t[1]
    return [by.get(d, 0.0) for d in calendar]


def block_bootstrap(series_by_month, months, n_sims, rng):
    """Totals and worst drawdowns of `months` months drawn with replacement from the real months."""
    keys = list(series_by_month)
    tots, dds = [], []
    for _ in range(n_sims):
        path = []
        for _ in range(months):
            path += series_by_month[rng.choice(keys)]
        tots.append(sum(path))
        dds.append(max_dd(path))
    tots.sort()
    dds.sort()
    return tots, dds


def clean(v):
    """JSON without NaN or infinity."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    return v


def pct(xs, p):
    return xs[min(len(xs) - 1, int(p / 100.0 * len(xs)))]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quant tests of the 09:00 box breakout.")
    ap.add_argument("--symbols", default="QQQ,SPY,IWM,DIA")
    ap.add_argument("--days", type=int, default=1827)
    ap.add_argument("--cost-pct", type=float, default=0.01)
    ap.add_argument("--random", type=int, default=100, help="random strategies for the null")
    ap.add_argument("--sims", type=int, default=5000, help="bootstrap paths")
    ap.add_argument("--min-n", type=int, default=300, help="trades a version needs to rank among the best")
    ap.add_argument("--extra", default="", help="more versions to test in full, e.g. "
                    "'10:30,long,trend,2,15:55,20;10:00,both,none,3,15:55,20' (box end, side, filter, target, last entry, hold days)")
    ap.add_argument("--entry", choices=("close", "retest"), default="close",
                    help="close: at the breakout close; retest: limit order at the broken box edge")
    ap.add_argument("--overlap", action="store_true", help="allow a new trade while one on the same index is open")
    ap.add_argument("--partial-r", type=float, help="close part of the trade at this R and move the stop to the entry")
    ap.add_argument("--partial-frac", type=float, default=0.7, help="the part closed there (0.7 = 70%%)")
    ap.add_argument("--offline", metavar="DIR")
    a = ap.parse_args(argv)
    OPTS.update(entry=a.entry, overlap=a.overlap, partial_r=a.partial_r, partial_frac=a.partial_frac)
    t0 = time.time()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    scan._marketdata()
    data = {}
    for sym in syms:
        try:
            series = study.fetch(sym, a.days, a.offline)
        except Exception as e:
            print("  %s: sin datos (%s)" % (sym, type(e).__name__), file=sys.stderr)
            series = None
        bars = orb.ny_bars(series, scan.ts_to_eastern) if series else []
        if len(bars) > 2000:
            days = orb.by_day(bars)
            data[sym] = {"bars": bars, "ema": orb.ema_15m(bars), "days": days,
                         "pos": {d: k for k, (d, _) in enumerate(days)}}
    if not data:
        print("Sin datos.")
        return 1
    calendar = sorted({b["t"].date().isoformat() for d in data.values() for b in d["bars"]
                       if b["t"].weekday() < 5 and datetime.time(9, 30) <= b["t"].time() < datetime.time(16, 0)})
    print("Validacion de la caja de las 09:00: %s, %s a %s (%d dias de mercado), costo %.3f%% por lado. Entrada: %s; %s." % (
        ", ".join(sorted(data)), calendar[0], calendar[-1], len(calendar), a.cost_pct,
        "orden limite en el retesteo del borde de la caja" if a.entry == "retest" else "al cierre de la vela que rompe",
        "varias operaciones abiertas por indice" if a.overlap else "una operacion abierta por indice"))
    if a.partial_r:
        print("Parcial: %.0f%% a %gR y stop del resto a la entrada; el resto va al objetivo si es mayor que %gR, "
              "si no al cierre." % (100 * a.partial_frac, a.partial_r, a.partial_r))

    # 1. the grid
    grid, gross = {}, {}
    for b in BOX_ENDS:
        for s in SIDES:
            for f in FILTERS:
                for tg in TARGETS:
                    for last in LASTS:
                        for hold in HOLDS:
                            cfg = (b, s, f, tg, last, hold)
                            ts, gs = [], []
                            for sym, d in data.items():
                                for t in orb.run(d["bars"], params(cfg), d["ema"]):
                                    ts.append((t["day"], net_r(t["R"], t["risk_pct"], a.cost_pct), sym, t["dir"]))
                                    gs.append((t["R"], t["risk_pct"]))
                            grid[cfg], gross[cfg] = ts, gs
    print("  rejilla de %d versiones lista, %.0fs" % (len(grid), time.time() - t0), file=sys.stderr)
    mid_day = calendar[len(calendar) // 2]
    rows = []
    for cfg, ts in grid.items():
        st = tstats([t[1] for t in ts])
        x = daily(ts, calendar)
        sr = perf.sharpe(x)
        first = [t[1] for t in ts if t[0] < mid_day]
        second = [t[1] for t in ts if t[0] >= mid_day]
        rows.append({"cfg": cfg, "label": label(cfg), "n": st["n"], "mean": st["mean"], "se": st["se"], "t": st["t"],
                     "win": st.get("win", 0), "sharpe_d": sr, "sharpe_a": sr * math.sqrt(252), "total": sum(x),
                     "dd": max_dd(x), "first": sum(first) / len(first) if first else None,
                     "second": sum(second) / len(second) if second else None,
                     "sharpe_first": perf.sharpe(daily([t for t in ts if t[0] < mid_day], [c for c in calendar if c < mid_day]))})
    rows.sort(key=lambda r: -r["sharpe_d"])
    var_trials = statistics.pvariance([r["sharpe_d"] for r in rows])
    def distinct(cands, n=3):
        """The first n with different trades: "until 13:00" and "all day" often take the very same ones."""
        out = []
        for r in cands:
            if r["n"] >= a.min_n and not any(r["n"] == o["n"] and abs(r["mean"] - o["mean"]) < 1e-6 for o in out):
                out.append(r)
            if len(out) == n:
                break
        return out
    top = distinct(rows)
    keys = list(USER_KEYS) + [("Mejor #%d: %s" % (k + 1, r["label"]), r["cfg"]) for k, r in enumerate(top)
                              if r["cfg"] not in [c for _, c in USER_KEYS]]
    for part in [x for x in a.extra.split(";") if x.strip()]:
        b, sd, f, tg, last, hold = [v.strip() for v in part.split(",")]
        cfg = ("%s" % b, sd, f, float(tg), last, int(hold))
        if cfg in grid and cfg not in [c for _, c in keys]:
            keys.append(("Extra: %s" % label(cfg), cfg))

    print("\n1. Backtest de las %d versiones, despues de costos (R por operacion; Sharpe anual del resultado diario):" % len(rows))
    print("   %-78s %6s %8s %7s %6s %8s %7s %8s %8s" % ("version", "n", "R/op", "+-", "t", "Sharpe", "caida", "1a mitad", "2a mitad"))
    for r in rows:
        print("   %-78s %6d %+8.3f %7.3f %+6.1f %+8.2f %7.1f %+8.3f %+8.3f" % (
            r["label"], r["n"], r["mean"], r["se"], r["t"], r["sharpe_a"], r["dd"], r["first"] or 0, r["second"] or 0))
    print("   versiones positivas despues de costos: %d de %d" % (sum(1 for r in rows if r["mean"] > 0), len(rows)))
    print("\n   Tus estrategias:")
    for name, cfg in USER_KEYS:
        r = next(x for x in rows if x["cfg"] == cfg)
        print("   %-66s n %5d  %+.3fR +- %.3f  Sharpe %+.2f  puesto %d de %d" % (
            name, r["n"], r["mean"], r["se"], r["sharpe_a"], rows.index(r) + 1, len(rows)))
    print("\n   Las 3 mejores por Sharpe (con al menos %d operaciones), y su primera y segunda mitad (desde %s):" % (a.min_n, mid_day))
    for k, r in enumerate(top):
        print("   %d. %-74s %+.3fR | %+.3fR -> %+.3fR" % (k + 1, r["label"], r["mean"], r["first"], r["second"]))
    chosen = distinct(sorted(rows, key=lambda r: -r["sharpe_first"]))
    print("   Elegidas solo con la primera mitad, en la segunda (lo que habria pasado eligiendo en %s):" % mid_day)
    for k, r in enumerate(chosen):
        print("   %d. %-74s 1a mitad %+.3fR -> 2a mitad %+.3fR" % (k + 1, r["label"], r["first"], r["second"]))
    best_out = [{"label": r["label"], "cfg": list(r["cfg"]), "n": r["n"], "mean": r["mean"], "se": r["se"],
                 "sharpe_a": r["sharpe_a"], "first": r["first"], "second": r["second"], "dd": r["dd"]} for r in top]
    chosen_out = [{"label": r["label"], "first": r["first"], "second": r["second"]} for r in chosen]

    # 2. Deflated Sharpe
    luck = perf.expected_max_sharpe(len(rows), var_trials) * math.sqrt(252)
    print("\n2. Deflated Sharpe (cuanto Sharpe da la suerte con %d intentos: %.2f anual):" % (len(rows), luck))
    dsr_out = []
    for name, cfg in keys:
        x = daily(grid[cfg], calendar)
        d, sr0 = perf.deflated_sharpe(x, len(rows), var_trials)
        _, _, sk, ku = perf.moments(x)
        p = perf.psr(perf.sharpe(x), len(x), sk, ku, 0.0)
        dsr_out.append({"name": name, "label": label(cfg), "sharpe_a": perf.sharpe(x) * math.sqrt(252), "psr": p, "dsr": d})
        print("   %-66s Sharpe %+.2f  P(Sharpe>0) %.2f  DSR %.2f" % (name[:66], perf.sharpe(x) * math.sqrt(252), p, d))
    print("   (DSR = probabilidad de que el Sharpe supere al de la mejor de %d versiones sin ventaja; se pide 0,95)" % len(rows))

    # 3a. random entries
    print("\n3a. Monte Carlo de entradas al azar (%d estrategias al azar por version):" % a.random)
    rng = random.Random(11)
    boxes = {}
    for sym, d in data.items():
        for b in BOX_ENDS:
            boxes[(sym, b)] = orb.boxes(d["bars"], params((b, "both", "trend", 3.0, "15:55", 0)))
    null_out = []
    pos_of = {d: k for k, d in enumerate(calendar)}
    for name, cfg in keys:
        real = tstats([t[1] for t in grid[cfg]])["mean"]
        means, rand_daily = [], [0.0] * len(calendar)
        for k in range(a.random):
            ts = []
            for sym, d in data.items():
                ts += fast_random(d, boxes[(sym, cfg[0])], cfg, rng, a.cost_pct)
            if ts:
                means.append(sum(t[1] for t in ts) / len(ts))
                for day, r in ts:
                    if day in pos_of:
                        rand_daily[pos_of[day]] += r / a.random
        means.sort()
        p = sum(1 for m in means if m >= real) / float(len(means))
        null_out.append({"name": name, "real": real, "null": [round(m, 4) for m in means], "p": p, "rand_daily": rand_daily})
        print("   %-66s reglas %+.3fR | azar mediana %+.3fR, 95%% menos de %+.3fR | p = %.3f" % (
            name[:66], real, pct(means, 50), pct(means, 95), p))
        print("    azar %d, %.0fs" % (len(null_out), time.time() - t0), file=sys.stderr)
    print("   (p = parte de las estrategias al azar que igualan o superan a las reglas; ventaja real si p < 0,05)")

    # 3b. bootstrap
    print("\n3b. Monte Carlo por bloques de meses (%d caminos), en R despues de costos, todos los indices juntos:" % a.sims)
    mc_out = []
    for name, cfg in keys:
        x = daily(grid[cfg], calendar)
        by_month = {}
        for day, v in zip(calendar, x):
            by_month.setdefault(day[:7], []).append(v)
        res = {"name": name}
        for months in (12, 60):
            tots, dds = block_bootstrap(by_month, months, a.sims, random.Random(5 + months))
            loss = sum(1 for v in tots if v < 0) / float(len(tots))
            res["y%d" % (months // 12)] = {"p5": pct(tots, 5), "p50": pct(tots, 50), "p95": pct(tots, 95), "loss": loss,
                                           "dd50": pct(dds, 50), "dd95": pct(dds, 95)}
            print("   %-66s %d %s: p5 %+6.1fR, mediana %+6.1fR, p95 %+6.1fR | perder %3.0f%% | caida mediana %.1fR, p95 %.1fR" % (
                name[:66], months // 12, "ano " if months == 12 else "anos", pct(tots, 5), pct(tots, 50), pct(tots, 95),
                100 * loss, pct(dds, 50), pct(dds, 95)))
        mc_out.append(res)

    # 4. walk-forward
    print("\n4. Walk-forward: elegir la mejor version en 24 meses, operarla los 6 siguientes:")
    months = sorted({d[:7] for d in calendar})
    wf_rows, oos_trades = [], []
    same = {name: [] for name, _ in keys}
    k = 24
    while k < len(months):
        train, test = set(months[k - 24:k]), set(months[k:k + 6])
        best, best_m = None, None
        for cfg, ts in grid.items():
            xs = [t[1] for t in ts if t[0][:7] in train]
            if len(xs) >= 60:
                m = sum(xs) / len(xs)
                if best_m is None or m > best_m:
                    best, best_m = cfg, m
        got = [t for t in grid[best] if t[0][:7] in test]
        oos_trades += got
        for name, cfg in keys:
            same[name] += [t for t in grid[cfg] if t[0][:7] in test]
        st = tstats([t[1] for t in got]) if got else {"n": 0, "mean": 0.0}
        wf_rows.append({"from": min(test), "to": max(test), "chosen": label(best), "train": best_m, "test": st["mean"], "n": st["n"]})
        print("   %s a %s  elegida %-72s %+.3fR -> %+.3fR (n%d)" % (min(test), max(test), label(best), best_m, st["mean"], st["n"]))
        k += 6
    so = tstats([t[1] for t in oos_trades])
    print("   fuera de muestra, eligiendo cada 6 meses: n %d, %+.3fR +- %.3f por operacion, suma %+.1fR" % (
        so["n"], so["mean"], so["se"], so["mean"] * so["n"]))
    wf_sum = {"oos": so}
    for name, _ in keys:
        sx = tstats([t[1] for t in same[name]])
        wf_sum[name] = sx
        print("   mismos meses, %-60s n %d, %+.3fR +- %.3f, suma %+.1fR" % (name[:60], sx["n"], sx["mean"], sx["se"], sx["mean"] * sx["n"]))

    # 5. costs
    print("\n5. Costos (R por operacion segun el costo por lado):")
    costs = (0.0, 0.005, 0.01, 0.02, 0.03)
    print("   %-66s %s" % ("", "  ".join("%6.3f%%" % c for c in costs)))
    cost_out = []
    for name, cfg in keys:
        vals = [sum(net_r(r, rp, c) for r, rp in gross[cfg]) / len(gross[cfg]) for c in costs]
        cost_out.append({"name": name, "vals": vals})
        print("   %-66s %s" % (name[:66], "  ".join("%+7.3f" % v for v in vals)))

    print("\nPor ano (R por operacion, despues de costos):")
    years = sorted({d[:4] for d in calendar})
    print("   %-66s %s" % ("", " ".join("%7s" % y for y in years)))
    year_out = []
    for name, cfg in keys:
        vals = []
        for y in years:
            xs = [t[1] for t in grid[cfg] if t[0][:4] == y]
            vals.append(sum(xs) / len(xs) if xs else None)
        year_out.append({"name": name, "vals": vals})
        print("   %-66s %s" % (name[:66], " ".join("%+7.3f" % v if v is not None else "      -" for v in vals)))
    print("\nPor indice (R por operacion, despues de costos):")
    sym_out = []
    for name, cfg in keys:
        vals = {}
        for sym in sorted(data):
            xs = [t[1] for t in grid[cfg] if t[2] == sym]
            vals[sym] = sum(xs) / len(xs) if xs else None
        sym_out.append({"name": name, "vals": vals})
        print("   %-66s %s" % (name[:66], "  ".join("%s %+.3f" % (s, v) for s, v in vals.items() if v is not None)))

    step = 5
    eq = []
    for (name, cfg), nl in zip(keys, null_out):
        x = daily(grid[cfg], calendar)
        c, cr, pts, rpts = 0.0, 0.0, [], []
        for i, (v, rv) in enumerate(zip(x, nl["rand_daily"])):
            c += v
            cr += rv
            if i % step == 0 or i == len(x) - 1:
                pts.append(round(c, 2))
                rpts.append(round(cr, 2))
        eq.append({"name": name, "equity": pts, "random": rpts})
    dates = [d for i, d in enumerate(calendar) if i % step == 0 or i == len(calendar) - 1]
    out = {"symbols": sorted(data), "from": calendar[0], "to": calendar[-1], "cost_pct": a.cost_pct, "dates": dates,
           "half": mid_day, "equity": eq, "best": best_out, "chosen_first_half": chosen_out,
           "grid": [{k: (v if k != "cfg" else list(v)) for k, v in r.items()} for r in rows],
           "dsr": dsr_out, "luck_sharpe_a": luck,
           "null": [{k: v for k, v in n.items() if k != "rand_daily"} for n in null_out], "mc": mc_out,
           "wf": wf_rows, "wf_sum": wf_sum, "costs": {"levels": costs, "rows": cost_out},
           "years": {"years": years, "rows": year_out}, "by_symbol": sym_out}
    print("\n@@ORBJSON@@ " + json.dumps(clean(out), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
