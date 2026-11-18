#!/usr/bin/env python3
"""Research: optimize the 09:00 box breakout (trader-scanner/orb.py) without fooling ourselves.

    python3 trader-analyst/orb_optimize.py --symbols QQQ,SPY,IWM,DIA --days 1827

Picking the best of thousands of backtests always finds something that looks good; the question is how much
of it is luck. Every method below answers that question from a different side:

1. The grid: box end x sides x EMA filter x last entry x partial R x partial size (100% = all out there, no
   runner) x days a trade may stay open. One position at a time per index, after costs. Daily Sharpe of the
   four indices together (a trade counts on the day it opens).
2. CSCV and the probability of backtest overfitting (Bailey, Borwein, Lopez de Prado and Zhu): the months are
   cut in 10 blocks; for each of the 252 ways to take 5 of them as the backtest, the version chosen there is
   ranked among all versions on the other 5. PBO is how often it lands in the bottom half. Done for two
   ways to choose: the best Sharpe, and the best plateau (a version averaged with its neighbours).
3. White's Reality Check and Hansen's SPA (studentized, month bootstrap): with this many versions tried, is
   the best one better than zero for real?
4. Walk-forward optimization: every 6 months choose on the months before (rolling 24 months, and anchored
   from the start) and trade the choice for the next 6 only. Three rules: best Sharpe, best plateau, and an
   equal-weight mix of the 10 best. The out-of-sample result is the honest estimate of "optimizing".
5. Stability: the Sharpe of each pick's neighbours (one step away in one parameter) and two maps of the
   exit parameters. A real edge sits on a plateau; luck sits on a lonely peak.
6. Deflated Sharpe of the picks.

Prints a report and one line "@@ORBOPT@@ {...}". Aggregates only. Research, never orders.
"""

import argparse
import datetime
import importlib.util
import itertools
import json
import math
import multiprocessing
import operator
import os
import random
import statistics
import sys
import time
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


validate = _module("trader_orb_validate_o", os.path.join(HERE, "orb_validate.py"))
study, perf, orb, scan = validate.study, validate.perf, validate.orb, validate.scan

FULL = {
    "box": ("09:30", "10:00", "10:30"),
    "side": ("both", "long"),
    "filter": ("none", "trend"),
    "last": ("12:55", "15:55"),
    "partial_r": (1.0, 1.3, 1.7, 2.0, 2.5, 3.0),
    "partial_f": (0.5, 0.7, 0.9, 1.0),      # 1.0 = all out at partial_r, no runner
    "hold": (0, 5, 10, 20, 40),             # 0 = out at the 16:00 close
}
SMALL = {"box": ("10:00", "10:30"), "side": ("both",), "filter": ("none",), "last": ("12:55",),
         "partial_r": (1.0, 1.7), "partial_f": (0.7, 1.0), "hold": (0, 20)}
DIMS = ("box", "side", "filter", "last", "partial_r", "partial_f", "hold")
ORDERED = ("box", "partial_r", "partial_f", "hold")       # neighbours: one step along one of these
CURRENT = ("10:00", "both", "none", "12:55", 1.7, 0.7, 20)


def label(cfg):
    b, s, f, last, pr, pf, hold = cfg
    out = ("todo a %gR" % pr) if pf >= 1 else ("%d%% a %gR + resto en breakeven" % (round(pf * 100), pr))
    return "caja 09:00-%s, %s, %s, %s, %s, %s" % (
        b, validate.SIDE_ES[s], "filtro" if f == "trend" else "sin filtro",
        "hasta 13:00" if last == "12:55" else "todo el dia", out, "cierre 16:00" if not hold else "hasta %d dias" % hold)


def params(cfg):
    b, s, f, last, pr, pf, hold = cfg
    nb = (orb._minutes(b) - orb._minutes("09:00")) // 5
    p = {"box_start": "09:00", "box_end": b, "sides": s, "filter": f, "last_entry": last, "hold_days": hold,
         "min_box_bars": max(2, nb // 2), "entry": "close", "overlap": False, "target_r": pr, "partial_r": None}
    if pf < 1:
        p.update(partial_r=pr, partial_frac=pf)
    return p


DATA, CAL = {}, {}          # filled before the worker processes fork


def evaluate(job):
    """One version on every index -> (cfg, daily net R by calendar day, trades opened by calendar day,
    [R sum, trades] of the shorts)."""
    cfg, cost = job
    n = len(CAL["days"])
    daily, counts, shorts = array("d", [0.0]) * n, array("i", [0]) * n, [0.0, 0]
    for d in DATA.values():
        for t in orb.run(d["bars"], params(cfg), d["ema"]):
            k = CAL["pos"].get(t["day"])
            if k is not None:
                r = validate.net_r(t["R"], t["risk_pct"], cost)
                daily[k] += r
                counts[k] += 1
                if t["dir"] < 0:
                    shorts[0] += r
                    shorts[1] += 1
    return cfg, daily, counts, shorts


def _worker(jobs, queue):
    for job in jobs:
        queue.put(evaluate(job))


def load(syms, days, offline):
    data = {}
    scan._marketdata()
    for sym in syms:
        try:
            series = study.fetch(sym, days, offline)
        except Exception as e:
            print("  %s: sin datos (%s)" % (sym, type(e).__name__), file=sys.stderr)
            series = None
        bars = orb.ny_bars(series, scan.ts_to_eastern) if series else []
        if len(bars) > 2000:
            data[sym] = {"bars": bars, "ema": orb.ema_15m(bars)}
    return data


class Agg(object):
    """Per version, sums by month, so the Sharpe of any set of months is a few additions."""

    def __init__(self, daily, counts, month_of, n_months):
        self.s1, self.s2, self.n = [0.0] * n_months, [0.0] * n_months, [0] * n_months
        for k, (v, c) in enumerate(zip(daily, counts)):
            m = month_of[k]
            self.s1[m] += v
            self.s2[m] += v * v
            self.n[m] += c


def sharpe_of(agg, months, days_in):
    s1 = sum(agg.s1[m] for m in months)
    s2 = sum(agg.s2[m] for m in months)
    d = sum(days_in[m] for m in months)
    if not d:
        return 0.0
    mean = s1 / d
    var = s2 / d - mean * mean
    return mean / math.sqrt(var) if var > 1e-12 else 0.0


def neighbours(cfgs, space):
    index = {c: k for k, c in enumerate(cfgs)}
    out = []
    for c in cfgs:
        near = []
        for dim in ORDERED:
            j = DIMS.index(dim)
            vals = space[dim]
            p = vals.index(c[j])
            for q in (p - 1, p + 1):
                if 0 <= q < len(vals):
                    other = c[:j] + (vals[q],) + c[j + 1:]
                    if other in index:
                        near.append(index[other])
        out.append(near)
    return out


def plateau(scores, near, eligible):
    """A version's score averaged with its neighbours' (a lonely peak scores low)."""
    out = []
    for k, s in enumerate(scores):
        if not eligible[k]:
            out.append(-1e9)
            continue
        vals = [s] + [scores[j] for j in near[k]]
        out.append(sum(vals) / len(vals))
    return out


def argmax(xs, eligible):
    best, bk = None, None
    for k, x in enumerate(xs):
        if eligible[k] and (best is None or x > best):
            best, bk = x, k
    return bk


def cscv(aggs, eligible, near, n_months, days_in, blocks=10):
    """Probability of backtest overfitting for two ways of choosing, over every half/half split of the blocks."""
    cuts = [round(i * n_months / float(blocks)) for i in range(blocks + 1)]
    groups = [list(range(cuts[i], cuts[i + 1])) for i in range(blocks)]
    groups = [g for g in groups if g]
    res = {"best": [], "plateau": []}
    if len(groups) < 4:
        return {r: {"splits": 0, "pbo": float("nan"), "oos_mean": 0.0, "is_mean": 0.0, "oos_loss": float("nan"),
                    "median_mean": 0.0} for r in res}
    for is_blocks in itertools.combinations(range(len(groups)), len(groups) // 2):
        ins = [m for b in is_blocks for m in groups[b]]
        oos = [m for b in range(len(groups)) if b not in is_blocks for m in groups[b]]
        sr_is = [sharpe_of(a, ins, days_in) if eligible[k] else -1e9 for k, a in enumerate(aggs)]
        sr_oos = [sharpe_of(a, oos, days_in) if eligible[k] else None for k, a in enumerate(aggs)]
        pool = sorted(x for x in sr_oos if x is not None)
        for rule, pick in (("best", argmax(sr_is, eligible)), ("plateau", argmax(plateau(sr_is, near, eligible), eligible))):
            o = sr_oos[pick]
            below = sum(1 for x in pool if x < o)
            w = (below + 0.5) / (len(pool) + 1.0)
            res[rule].append({"is": sr_is[pick], "oos": o, "logit": math.log(w / (1 - w)), "median": pool[len(pool) // 2]})
    out = {}
    for rule, xs in res.items():
        n = float(len(xs))
        out[rule] = {"splits": len(xs), "pbo": sum(1 for x in xs if x["logit"] <= 0) / n,
                     "oos_mean": sum(x["oos"] for x in xs) / n, "is_mean": sum(x["is"] for x in xs) / n,
                     "oos_loss": sum(1 for x in xs if x["oos"] <= 0) / n,
                     "median_mean": sum(x["median"] for x in xs) / n}
    return out


def reality_check(aggs, eligible, n_months, boots, rng):
    """Studentized White Reality Check (upper) and Hansen SPA (consistent), months drawn with replacement.
    H0: no version beats zero. -> (statistic, p RC, p SPA, best index)."""
    rows, idx = [], []
    for k, a in enumerate(aggs):
        if not eligible[k]:
            continue
        m = a.s1
        mean = sum(m) / n_months
        sd = statistics.pstdev(m)
        if sd > 0:
            rows.append((m, mean, sd))
            idx.append(k)
    if not rows:
        return None
    root = math.sqrt(n_months)
    tstat = [root * mean / sd for _, mean, sd in rows]
    stat = max(0.0, max(tstat))
    best = idx[tstat.index(max(tstat))]
    thr = math.sqrt(2 * math.log(math.log(max(n_months, 3))))
    g_c = [mean if root * mean / sd >= -thr else 0.0 for _, mean, sd in rows]
    hit_rc = hit_spa = 0
    mul = operator.mul
    for _ in range(boots):
        c = [0] * n_months
        for _ in range(n_months):
            c[rng.randrange(n_months)] += 1
        v_rc = v_spa = 0.0
        for (m, mean, sd), gc in zip(rows, g_c):
            mb = sum(map(mul, m, c)) / n_months
            v_rc = max(v_rc, root * (mb - mean) / sd)
            v_spa = max(v_spa, root * (mb - gc) / sd)
        hit_rc += v_rc >= stat
        hit_spa += v_spa >= stat
    return {"stat": stat, "p_rc": hit_rc / float(boots), "p_spa": hit_spa / float(boots), "best": best,
            "versions": len(rows)}


def walk_forward(aggs, dailies, counts, eligible_full, near, months, month_of, days_in, min_train, anchored, top=10):
    """Choose every 6 months on the months before, trade the next 6. -> {rule: oos stats}."""
    n_m = len(months)
    out = {r: {"daily": [], "n": 0.0, "picks": []} for r in ("best", "plateau", "top10")}
    k = 24
    while k < n_m:
        train = list(range(0 if anchored else k - 24, k))
        test = set(range(k, min(k + 6, n_m)))
        days = [i for i, m in enumerate(month_of) if m in test]
        n_train = [sum(a.n[m] for m in train) for a in aggs]
        elig = [eligible_full[i] and n_train[i] >= min_train for i in range(len(aggs))]
        if not any(elig):
            k += 6
            continue
        sr = [sharpe_of(a, train, days_in) if elig[i] else -1e9 for i, a in enumerate(aggs)]
        picks = {"best": [argmax(sr, elig)], "plateau": [argmax(plateau(sr, near, elig), elig)],
                 "top10": sorted((i for i in range(len(aggs)) if elig[i]), key=lambda i: -sr[i])[:top]}
        for rule, ks in picks.items():
            w = 1.0 / len(ks)
            out[rule]["daily"] += [w * sum(dailies[i][d] for i in ks) for d in days]
            out[rule]["n"] += w * sum(counts[i][d] for i in ks for d in days)
            out[rule]["picks"].append({"from": months[k], "pick": ks[0]})
        k += 6
    first_day = next((i for i, m in enumerate(month_of) if m >= 24), None)
    for rule, r in out.items():
        x = r["daily"]
        r["sharpe"] = perf.sharpe(x) * math.sqrt(252) if len(x) > 2 else 0.0
        r["total"] = sum(x)
        r["per_trade"] = sum(x) / r["n"] if r["n"] else 0.0
        r["dd"] = validate.max_dd(x)
        r["first_day"] = first_day
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Optimize the 09:00 box breakout, measuring the overfitting.")
    ap.add_argument("--symbols", default="QQQ,SPY,IWM,DIA")
    ap.add_argument("--days", type=int, default=1827)
    ap.add_argument("--cost-pct", type=float, default=0.01)
    ap.add_argument("--min-n", type=int, default=300, help="trades a version needs over the whole period to compete")
    ap.add_argument("--min-train", type=int, default=60, help="trades a version needs in a walk-forward training window")
    ap.add_argument("--boots", type=int, default=1000, help="bootstrap draws of the Reality Check")
    ap.add_argument("--workers", type=int, default=0, help="processes (0 = one per CPU)")
    ap.add_argument("--small", action="store_true", help="a 16 version grid (tests)")
    ap.add_argument("--offline", metavar="DIR")
    a = ap.parse_args(argv)
    t0 = time.time()
    space = SMALL if a.small else FULL
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    data = load(syms, a.days, a.offline)
    if not data:
        print("Sin datos.")
        return 1
    DATA.clear()
    DATA.update(data)
    calendar = sorted({b["t"].date().isoformat() for d in data.values() for b in d["bars"]
                       if b["t"].weekday() < 5 and datetime.time(9, 30) <= b["t"].time() < datetime.time(16, 0)})
    CAL.update(days=calendar, pos={d: k for k, d in enumerate(calendar)})
    months = sorted({d[:7] for d in calendar})
    m_index = {m: k for k, m in enumerate(months)}
    month_of = [m_index[d[:7]] for d in calendar]
    days_in = [0] * len(months)
    for m in month_of:
        days_in[m] += 1

    cfgs = list(itertools.product(*(space[d] for d in DIMS)))
    jobs = [(c, a.cost_pct) for c in cfgs]
    workers = a.workers or os.cpu_count() or 1
    results = {}
    if workers > 1:
        # forked processes inherit the bars; only the results travel back (no pickled functions, so this
        # also works when the module was loaded from a path)
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        procs = [ctx.Process(target=_worker, args=(jobs[w::workers], queue)) for w in range(workers)]
        for pr in procs:
            pr.start()
        for k in range(len(jobs)):
            cfg, dl, ct, sh = queue.get()
            results[cfg] = (dl, ct, sh)
            if (k + 1) % 200 == 0:
                print("  %d de %d versiones, %.0fs" % (k + 1, len(cfgs), time.time() - t0), file=sys.stderr)
        for pr in procs:
            pr.join()
    else:
        for job in jobs:
            cfg, dl, ct, sh = evaluate(job)
            results[cfg] = (dl, ct, sh)
    dailies = [results[c][0] for c in cfgs]
    counts = [results[c][1] for c in cfgs]
    shorts = [results[c][2] for c in cfgs]
    aggs = [Agg(dl, ct, month_of, len(months)) for dl, ct in zip(dailies, counts)]
    near = neighbours(cfgs, space)
    mid = len(calendar) // 2
    rows = []
    for k, c in enumerate(cfgs):
        x = list(dailies[k])
        n = sum(counts[k])
        n1 = sum(counts[k][:mid])
        rows.append({"k": k, "cfg": c, "label": label(c), "n": n, "mean": sum(x) / n if n else 0.0,
                     "sharpe": perf.sharpe(x) * math.sqrt(252), "dd": validate.max_dd(x), "total": sum(x),
                     "first": sum(x[:mid]) / n1 if n1 else 0.0,
                     "second": sum(x[mid:]) / (n - n1) if n - n1 else 0.0})
    eligible = [r["n"] >= a.min_n for r in rows]
    if not any(eligible):
        eligible = [r["n"] > 0 for r in rows]
    n_elig = sum(eligible)
    print("Optimizacion de la caja de las 09:00: %s, %s a %s (%d dias, %d meses), costo %.3f%% por lado, una operacion "
          "a la vez por indice." % (", ".join(sorted(data)), calendar[0], calendar[-1], len(calendar), len(months), a.cost_pct))
    print("%d versiones (%s); compiten las %d con al menos %d operaciones. %.0fs." % (
        len(cfgs), " x ".join("%d %s" % (len(space[d]), d) for d in DIMS), n_elig, a.min_n, time.time() - t0))

    # 1. the grid
    ranked = sorted((r for r in rows if eligible[r["k"]]), key=lambda r: -r["sharpe"])
    sr_all = [r["sharpe"] / math.sqrt(252) for r in rows]
    pl_all = plateau(sr_all, near, eligible)
    k_best, k_plat = ranked[0]["k"], argmax(pl_all, eligible)
    k_cur = cfgs.index(CURRENT) if CURRENT in cfgs else None
    print("\n1. Las 15 mejores por Sharpe (despues de costos):")
    print("   %-92s %6s %8s %7s %7s %8s %8s" % ("version", "n", "R/op", "Sharpe", "caida", "1a mitad", "2a mitad"))
    for r in ranked[:15]:
        print("   %-92s %6d %+8.3f %+7.2f %7.1f %+8.3f %+8.3f" % (r["label"], r["n"], r["mean"], r["sharpe"], r["dd"],
                                                                    r["first"], r["second"]))
    pos = sum(1 for r in ranked if r["mean"] > 0)
    print("   positivas despues de costos: %d de %d; mediana del Sharpe %+.2f" % (
        pos, len(ranked), statistics.median(r["sharpe"] for r in ranked)))
    by_dim = {}
    for dim in DIMS:
        j = DIMS.index(dim)
        by_dim[dim] = []
        for v in space[dim]:
            xs = [r["sharpe"] for r in ranked if r["cfg"][j] == v]
            if xs:
                by_dim[dim].append({"value": v, "median": statistics.median(xs), "best": max(xs), "n": len(xs)})
    print("\n   Sharpe mediano y maximo de las versiones con cada valor (si un valor gana en todas, no es suerte de una):")
    for dim, vals in by_dim.items():
        print("   %-10s %s" % (dim, "  ".join("%s: %+.2f / %+.2f" % (v["value"], v["median"], v["best"]) for v in vals)))

    # 2. CSCV
    print("\n2. Probabilidad de sobreajuste (CSCV, 10 bloques de meses, %s divisiones mitad/mitad):" % "252")
    pbo = cscv(aggs, eligible, near, len(months), days_in)
    for rule, name in (("best", "elegir el mejor Sharpe"), ("plateau", "elegir la mejor meseta")):
        p = pbo[rule]
        print("   %-24s PBO %3.0f%% | Sharpe anual dentro %+.2f -> fuera %+.2f (mediana de todas fuera %+.2f) | "
              "fuera <= 0: %3.0f%%" % (name, 100 * p["pbo"], p["is_mean"] * math.sqrt(252), p["oos_mean"] * math.sqrt(252),
                                     p["median_mean"] * math.sqrt(252), 100 * p["oos_loss"]))
    print("   (PBO = veces que la elegida queda en la mitad de abajo fuera de muestra; 50% = elegir no sirve, se pide < 10-20%)")

    # 3. reality check
    print("\n3. Reality Check de White y SPA de Hansen (%d remuestreos de meses):" % a.boots)
    rc = reality_check(aggs, eligible, len(months), a.boots, random.Random(3))
    if rc:
        print("   la mejor por t mensual: %s" % label(cfgs[rc["best"]]))
        print("   t = %.2f | p Reality Check %.3f | p SPA %.3f (ventaja real despues de probar %d versiones si p < 0,05)" % (
            rc["stat"], rc["p_rc"], rc["p_spa"], rc["versions"]))

    # 4. walk-forward optimization
    print("\n4. Walk-forward: elegir cada 6 meses con los meses anteriores y operar solo los 6 siguientes:")
    wf = {}
    for anchored in (False, True):
        name = "anclado (desde el inicio)" if anchored else "movil (24 meses)"
        res = walk_forward(aggs, dailies, counts, eligible, near, months, month_of, days_in, a.min_train, anchored)
        wf["anchored" if anchored else "rolling"] = res
        for rule, rname in (("best", "mejor Sharpe"), ("plateau", "mejor meseta"), ("top10", "mezcla de las 10 mejores")):
            r = res[rule]
            print("   %-26s %-26s fuera de muestra: n %5.0f, %+.3fR por operacion, Sharpe %+.2f, suma %+6.1fR, caida %.1fR" % (
                name, rname, r["n"], r["per_trade"], r["sharpe"], r["total"], r["dd"]))
        picks = [label(cfgs[p["pick"]]) for p in res["plateau"]["picks"]]
        print("   %-26s elegidas por meseta: %s" % ("", " | ".join("%s: %s" % (p["from"], l) for p, l in zip(res["plateau"]["picks"], picks))))
    if k_cur is not None and wf["rolling"]["best"]["first_day"] is not None:
        x = list(dailies[k_cur])[wf["rolling"]["best"]["first_day"]:]
        n = sum(counts[k_cur][wf["rolling"]["best"]["first_day"]:])
        print("   mismos meses, la elegida antes (con todo el periodo a la vista): n %d, %+.3fR por operacion, Sharpe %+.2f, suma %+.1fR" % (
            n, sum(x) / n if n else 0.0, perf.sharpe(x) * math.sqrt(252), sum(x)))

    # 5. stability
    print("\n5. Estabilidad: Sharpe de las vecinas (un paso en caja, parcial, tamano del parcial o dias):")
    picks = [("mejor Sharpe", k_best), ("mejor meseta", k_plat)] + ([("elegida antes", k_cur)] if k_cur is not None else [])
    stab = []
    for name, k in picks:
        nb = [rows[j]["sharpe"] for j in near[k]]
        print("   %-14s %s" % (name, rows[k]["label"]))
        print("   %-14s Sharpe %+.2f, R/op %+.3f, n %d | vecinas: %d, media %+.2f, min %+.2f, positivas %d de %d" % (
            "", rows[k]["sharpe"], rows[k]["mean"], rows[k]["n"], len(nb), sum(nb) / len(nb) if nb else 0.0,
            min(nb) if nb else 0.0, sum(1 for v in nb if v > 0), len(nb)))
        sr, sn = shorts[k]
        lr, ln = rows[k]["total"] - sr, rows[k]["n"] - sn
        print("   %-14s largos: n %d, %+.3fR por operacion | cortos: n %d, %+.3fR por operacion" % (
            "", ln, lr / ln if ln else 0.0, sn, sr / sn if sn else 0.0))
        stab.append({"long": [ln, lr / ln if ln else 0.0], "short": [sn, sr / sn if sn else 0.0], "name": name, "label": rows[k]["label"], "sharpe": rows[k]["sharpe"], "mean": rows[k]["mean"],
                     "n": rows[k]["n"], "neighbours": nb})
    maps = []
    base = cfgs[k_plat]
    for dims in (("partial_r", "partial_f"), ("hold", "partial_r")):
        jr, jc = DIMS.index(dims[0]), DIMS.index(dims[1])
        grid = []
        print("\n   Mapa de Sharpe (%s en filas, %s en columnas), resto como la mejor meseta:" % dims)
        print("   %8s %s" % ("", " ".join("%7s" % v for v in space[dims[1]])))
        for vr in space[dims[0]]:
            line = []
            for vc in space[dims[1]]:
                c = list(base)
                c[jr], c[jc] = vr, vc
                c = tuple(c)
                line.append(rows[cfgs.index(c)]["sharpe"] if c in cfgs else None)
            grid.append(line)
            print("   %8s %s" % (vr, " ".join("%+7.2f" % v if v is not None else "      -" for v in line)))
        maps.append({"rows": dims[0], "cols": dims[1], "row_vals": list(space[dims[0]]), "col_vals": list(space[dims[1]]),
                     "sharpe": grid})

    # 6. deflated sharpe
    var_trials = statistics.pvariance([sr_all[r["k"]] for r in ranked]) if len(ranked) > 1 else 0.0
    luck = perf.expected_max_sharpe(n_elig, var_trials) * math.sqrt(252) if var_trials > 0 else 0.0
    print("\n6. Deflated Sharpe (la suerte sola da %.2f de Sharpe anual a la mejor de %d):" % (luck, n_elig))
    dsr = []
    for name, k in picks:
        x = list(dailies[k])
        d, _ = perf.deflated_sharpe(x, n_elig, var_trials) if var_trials > 0 else (0.0, 0.0)
        dsr.append({"name": name, "dsr": d})
        print("   %-14s Sharpe %+.2f  DSR %.2f (se pide 0,95)" % (name, rows[k]["sharpe"], d))

    out = {"symbols": sorted(data), "from": calendar[0], "to": calendar[-1], "cost_pct": a.cost_pct, "versions": len(cfgs),
           "eligible": n_elig, "top": [{k: (list(v) if k == "cfg" else v) for k, v in r.items()} for r in ranked[:30]],
           "by_dim": by_dim, "pbo": pbo, "rc": dict(rc, best=label(cfgs[rc["best"]])) if rc else None,
           "wf": {m: {r: {k: v for k, v in x.items() if k not in ("daily", "picks")} for r, x in res.items()} for m, res in wf.items()},
           "stability": stab, "maps": maps, "dsr": dsr, "luck_sharpe": luck,
           "plateau_pick": rows[k_plat]["label"], "best_pick": rows[k_best]["label"]}
    print("\n@@ORBOPT@@ " + json.dumps(validate.clean(out), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
