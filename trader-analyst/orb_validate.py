#!/usr/bin/env python3
"""Research: is the 09:00 box breakout (trader-scanner/orb.py) profitable in the long run? The quant tests.

    python3 trader-analyst/orb_validate.py --symbols QQQ,SPY,IWM,DIA --days 1827

1. Backtest of a grid of 54 versions: box end 09:30 / 10:00 / 10:30, both sides / longs / shorts, with and
   without the 15 minute EMA filter, target 1R / 2R / 3R. Each after costs, with its daily Sharpe.
2. Deflated Sharpe (Bailey and Lopez de Prado): the best of 54 versions looks good by luck alone; this says
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
TARGETS = (1.0, 2.0, 3.0)
KEYS = (
    ("Tus reglas: caja 09:00-09:30", ("09:30", "both", "trend", 3.0)),
    ("Caja 09:00-09:30, solo largos", ("09:30", "long", "trend", 3.0)),
    ("Caja 09:00-10:30", ("10:30", "both", "trend", 3.0)),
    ("Caja 09:00-10:30, solo largos", ("10:30", "long", "trend", 3.0)),
)
SIDE_ES = {"both": "ambos", "long": "largos", "short": "cortos"}


def label(cfg):
    b, s, f, t = cfg
    return "09:00-%s %-6s %-9s %gR" % (b, SIDE_ES[s], "filtro" if f == "trend" else "sin filtro", t)


def params(cfg):
    b, s, f, t = cfg
    bars = (orb._minutes(b) - orb._minutes("09:00")) // 5
    return {"box_start": "09:00", "box_end": b, "sides": s, "filter": f, "target_r": t,
            "min_box_bars": max(2, bars // 2)}


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


def fast_random(bars, ema, boxes, cfg, rng, cost_pct):
    """One random strategy on one symbol: each day a random entry bar of the window, the filter's direction
    (or a random one without filter), the box height as the stop, the same target and the 16:00 close."""
    b, side, flt, tgt = cfg
    out = []
    for d, hi, lo, sess in boxes:
        if hi <= lo:
            continue
        i = sess[rng.randrange(len(sess))]
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
        r, _, _ = orb.simulate(bars, i, k, e, e - k * (hi - lo), tgt, sess[-1])
        out.append((d.isoformat(), net_r(r, (hi - lo) / e * 100, cost_pct)))
    return out


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
    ap.add_argument("--random", type=int, default=300, help="random strategies for the null")
    ap.add_argument("--sims", type=int, default=5000, help="bootstrap paths")
    ap.add_argument("--offline", metavar="DIR")
    a = ap.parse_args(argv)
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
            data[sym] = {"bars": bars, "ema": orb.ema_15m(bars)}
    if not data:
        print("Sin datos.")
        return 1
    calendar = sorted({b["t"].date().isoformat() for d in data.values() for b in d["bars"]
                       if b["t"].weekday() < 5 and datetime.time(9, 30) <= b["t"].time() < datetime.time(16, 0)})
    print("Validacion de la caja de las 09:00: %s, %s a %s (%d dias de mercado), costo %.3f%% por lado." % (
        ", ".join(sorted(data)), calendar[0], calendar[-1], len(calendar), a.cost_pct))

    # 1. the grid
    grid = {}
    gross = {}
    for b in BOX_ENDS:
        for s in SIDES:
            for f in FILTERS:
                for tg in TARGETS:
                    cfg = (b, s, f, tg)
                    ts, gs = [], []
                    for sym, d in data.items():
                        for t in orb.run(d["bars"], params(cfg), d["ema"]):
                            ts.append((t["day"], net_r(t["R"], t["risk_pct"], a.cost_pct), sym, t["dir"]))
                            gs.append((t["R"], t["risk_pct"]))
                    grid[cfg] = ts
                    gross[cfg] = gs
    print("  rejilla de %d versiones lista, %.0fs" % (len(grid), time.time() - t0), file=sys.stderr)
    rows = []
    for cfg, ts in grid.items():
        st = tstats([t[1] for t in ts])
        x = daily(ts, calendar)
        sr = perf.sharpe(x)
        rows.append({"cfg": cfg, "label": label(cfg), "n": st["n"], "mean": st["mean"], "se": st["se"], "t": st["t"],
                     "win": st.get("win", 0), "sharpe_d": sr, "sharpe_a": sr * math.sqrt(252), "total": sum(x),
                     "dd": max_dd(x)})
    rows.sort(key=lambda r: -r["sharpe_d"])
    var_trials = statistics.pvariance([r["sharpe_d"] for r in rows])

    print("\n1. Backtest de las %d versiones, despues de costos (R por operacion; Sharpe anual del resultado diario):" % len(rows))
    print("   %-38s %6s %8s %7s %6s %8s %7s" % ("version", "n", "R/op", "+-", "t", "Sharpe", "caida"))
    for r in rows:
        print("   %-38s %6d %+8.3f %7.3f %+6.1f %+8.2f %7.1f" % (r["label"], r["n"], r["mean"], r["se"], r["t"], r["sharpe_a"], r["dd"]))
    pos = sum(1 for r in rows if r["mean"] > 0)
    print("   versiones positivas despues de costos: %d de %d" % (pos, len(rows)))

    # 2. Deflated Sharpe
    print("\n2. Deflated Sharpe (cuanto Sharpe da la suerte con %d intentos: %.2f anual):" % (
        len(rows), perf.expected_max_sharpe(len(rows), var_trials) * math.sqrt(252)))
    dsr_out = []
    for name, cfg in KEYS + (("La mejor de la rejilla", rows[0]["cfg"]),):
        x = daily(grid[cfg], calendar)
        d, sr0 = perf.deflated_sharpe(x, len(rows), var_trials)
        _, _, sk, ku = perf.moments(x)
        p = perf.psr(perf.sharpe(x), len(x), sk, ku, 0.0)
        dsr_out.append({"name": name, "label": label(cfg), "sharpe_a": perf.sharpe(x) * math.sqrt(252), "psr": p, "dsr": d})
        print("   %-32s %-38s Sharpe %+.2f  P(Sharpe>0) %.2f  DSR %.2f" % (name, label(cfg), perf.sharpe(x) * math.sqrt(252), p, d))
    print("   (DSR = probabilidad de que el Sharpe sea mejor que el de la mejor de %d versiones sin ventaja; se pide 0,95)" % len(rows))

    # 3a. random entries
    print("\n3a. Monte Carlo de entradas al azar (%d estrategias al azar por version):" % a.random)
    rng = random.Random(11)
    boxes = {}
    for sym, d in data.items():
        for b in BOX_ENDS:
            boxes[(sym, b)] = orb.boxes(d["bars"], params((b, "both", "trend", 3.0)))
    null_out = []
    for name, cfg in KEYS:
        real = tstats([t[1] for t in grid[cfg]])["mean"]
        means, rand_daily = [], [0.0] * len(calendar)
        pos_of = {d: k for k, d in enumerate(calendar)}
        for k in range(a.random):
            ts = []
            for sym, d in data.items():
                ts += fast_random(d["bars"], d["ema"], boxes[(sym, cfg[0])], cfg, rng, a.cost_pct)
            if ts:
                means.append(sum(t[1] for t in ts) / len(ts))
                for day, r in ts:
                    if day in pos_of:
                        rand_daily[pos_of[day]] += r / a.random
        means.sort()
        p = sum(1 for m in means if m >= real) / float(len(means))
        null_out.append({"name": name, "real": real, "null": [round(m, 4) for m in means], "p": p,
                         "rand_daily": rand_daily})
        print("   %-32s reglas %+.3fR | azar: mediana %+.3fR, 95%% de las veces menos de %+.3fR | p = %.3f" % (
            name, real, pct(means, 50), pct(means, 95), p))
    print("   (p = parte de las estrategias al azar que igualan o superan a las reglas; ventaja real si p < 0,05)")
    print("  azar listo, %.0fs" % (time.time() - t0), file=sys.stderr)

    # 3b. bootstrap
    print("\n3b. Monte Carlo por bloques de meses (%d caminos), en R despues de costos, todos los indices juntos:" % a.sims)
    mc_out = []
    for name, cfg in KEYS:
        x = daily(grid[cfg], calendar)
        by_month = {}
        for day, v in zip(calendar, x):
            by_month.setdefault(day[:7], []).append(v)
        res = {"name": name}
        for months in (12, 60):
            tots, dds = block_bootstrap(by_month, months, a.sims, random.Random(5 + months))
            loss = sum(1 for v in tots if v < 0) / float(len(tots))
            res["y%d" % (months // 12)] = {"p5": pct(tots, 5), "p50": pct(tots, 50), "p95": pct(tots, 95), "loss": loss,
                                           "dd50": pct(dds, 50), "dd95": pct(dds, 95),
                                           "hist": [round(pct(tots, q), 2) for q in range(1, 100)]}
            print("   %-32s %d %s: total p5 %+6.1fR, mediana %+6.1fR, p95 %+6.1fR | perder: %3.0f%% | peor caida mediana %.1fR, p95 %.1fR" % (
                name, months // 12, "ano " if months == 12 else "anos", pct(tots, 5), pct(tots, 50), pct(tots, 95),
                100 * loss, pct(dds, 50), pct(dds, 95)))
        mc_out.append(res)

    # 4. walk-forward
    print("\n4. Walk-forward: elegir la mejor version en 24 meses, operarla los 6 siguientes:")
    months = sorted({d[:7] for d in calendar})
    wf_rows, oos_trades, user_oos, long_oos = [], [], [], []
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
        user_oos += [t for t in grid[KEYS[0][1]] if t[0][:7] in test]
        long_oos += [t for t in grid[KEYS[3][1]] if t[0][:7] in test]
        st = tstats([t[1] for t in got]) if got else {"n": 0, "mean": 0.0}
        wf_rows.append({"from": min(test), "to": max(test), "chosen": label(best), "train": best_m, "test": st["mean"], "n": st["n"]})
        print("   %s a %s  elegida %-38s entrenamiento %+.3fR -> fuera de muestra %+.3fR (n%d)" % (
            min(test), max(test), label(best), best_m, st["mean"], st["n"]))
        k += 6
    so, su, sl = tstats([t[1] for t in oos_trades]), tstats([t[1] for t in user_oos]), tstats([t[1] for t in long_oos])
    print("   fuera de muestra, eligiendo cada 6 meses: n %d, %+.3fR +- %.3f por operacion, suma %+.1fR" % (
        so["n"], so["mean"], so["se"], so["mean"] * so["n"]))
    print("   en los mismos meses, tus reglas fijas:     n %d, %+.3fR +- %.3f, suma %+.1fR" % (su["n"], su["mean"], su["se"], su["mean"] * su["n"]))
    print("   en los mismos meses, 09:00-10:30 largos:   n %d, %+.3fR +- %.3f, suma %+.1fR" % (sl["n"], sl["mean"], sl["se"], sl["mean"] * sl["n"]))

    # 5. costs
    print("\n5. Costos (R por operacion segun el costo por lado):")
    costs = (0.0, 0.005, 0.01, 0.02, 0.03)
    print("   %-32s %s" % ("", "  ".join("%6.3f%%" % c for c in costs)))
    cost_out = []
    for name, cfg in KEYS:
        vals = [sum(net_r(r, rp, c) for r, rp in gross[cfg]) / len(gross[cfg]) for c in costs]
        cost_out.append({"name": name, "vals": vals})
        print("   %-32s %s" % (name, "  ".join("%+7.3f" % v for v in vals)))

    # by year and symbol
    print("\nPor ano (R por operacion, despues de costos):")
    years = sorted({d[:4] for d in calendar})
    print("   %-32s %s" % ("", " ".join("%7s" % y for y in years)))
    year_out = []
    for name, cfg in KEYS:
        vals = []
        for y in years:
            xs = [t[1] for t in grid[cfg] if t[0][:4] == y]
            vals.append(sum(xs) / len(xs) if xs else None)
        year_out.append({"name": name, "vals": vals})
        print("   %-32s %s" % (name, " ".join("%+7.3f" % v if v is not None else "      -" for v in vals)))
    print("\nPor indice (R por operacion, despues de costos):")
    sym_out = []
    for name, cfg in KEYS:
        vals = {}
        for sym in sorted(data):
            xs = [t[1] for t in grid[cfg] if t[2] == sym]
            vals[sym] = sum(xs) / len(xs) if xs else None
        sym_out.append({"name": name, "vals": vals})
        print("   %-32s %s" % (name, "  ".join("%s %+.3f" % (s, v) for s, v in vals.items() if v is not None)))

    # the series for the charts, one point a week
    step = 5
    eq = []
    for (name, cfg), nl in zip(KEYS, null_out):
        x = daily(grid[cfg], calendar)
        c, cr, pts, rpts = 0.0, 0.0, [], []
        for i, (v, rv) in enumerate(zip(x, nl["rand_daily"])):
            c += v
            cr += rv
            if i % step == 0 or i == len(x) - 1:
                pts.append(round(c, 2))
                rpts.append(round(cr, 2))
        eq.append({"name": name, "equity": pts, "random": rpts})
    oos_x = daily(oos_trades, calendar)
    c, wf_eq = 0.0, []
    for i, v in enumerate(oos_x):
        c += v
        if i % step == 0 or i == len(oos_x) - 1:
            wf_eq.append(round(c, 2))
    dates = [d for i, d in enumerate(calendar) if i % step == 0 or i == len(calendar) - 1]
    out = {"symbols": sorted(data), "from": calendar[0], "to": calendar[-1], "cost_pct": a.cost_pct, "dates": dates,
           "equity": eq, "wf_equity": wf_eq, "wf_start": months[24] if len(months) > 24 else None,
           "grid": [{k: (v if k != "cfg" else list(v)) for k, v in r.items()} for r in rows],
           "dsr": dsr_out, "luck_sharpe_a": perf.expected_max_sharpe(len(rows), var_trials) * math.sqrt(252),
           "null": [{k: v for k, v in n.items() if k != "rand_daily"} for n in null_out], "mc": mc_out,
           "wf": wf_rows, "wf_sum": {"oos": so, "user": su, "long": sl}, "costs": {"levels": costs, "rows": cost_out},
           "years": {"years": years, "rows": year_out}, "by_symbol": sym_out}
    print("\n@@ORBJSON@@ " + json.dumps(clean(out), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
