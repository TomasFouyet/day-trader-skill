#!/usr/bin/env python3
"""Research: can a filter tell strong box breakouts from weak ones? Volatility, relative volume, momentum.

    python3 trader-analyst/orb_filters.py --symbols QQQ,SPY,IWM,DIA --days 1827

What is measured at the close of the breakout bar (only what is known by then):
  volatility   atr_rank      the daily ATR(14) as % of price, ranked against its last 250 days (0 = calmest)
               box_atr       box height over the daily ATR(14)
  volume       rvol_sesion   volume from 09:30 to this bar over the 20 day average up to the same time
               rvol_vela     the breakout bar's volume over the 20 day average of that same 5 minute slot
  momentum     cuerpo        body of the breakout bar in the trade's direction over its range (-1 to 1)
               cierre        where the bar closes in its range, in the trade's direction (1 = at the extreme)
               extension     how far past the box edge it closes, in box heights
               rango_vela    the bar's range over the average of the 14 bars before it
               vwap          distance from the session VWAP in the trade's direction, in daily ATRs
               tendencia20   the 20 day move before today in the trade's direction, in daily ATRs
               gap           today's 09:30 open against yesterday's close, in the trade's direction, in ATRs
               hora          minutes after 10:00
               fuerza        rvol_sesion + cuerpo + rango_vela together (z-scores, oriented by the first half)

How it avoids fooling itself:
1. Every feature is cut in thirds with the FIRST HALF of the period alone; its R per trade by third is shown
   in the first half and in the second half. The better side is chosen in the first half only.
2. Each filter (drop the worse third, or keep the better half) runs inside the engine, so a skipped trade
   frees the index for the next days like it would live. Its second half is out of sample.
3. The same share of entries dropped AT RANDOM, 50 times: a filter only counts if it beats that.

Two base strategies: the chosen one (09:00-10:00 box, both sides, entries until 13:00, 70% at 1.7R, the rest
at breakeven up to 20 days) and the same closed at 16:00 (intraday). Aggregates only. Research, never orders.
"""

import argparse
import datetime
import importlib.util
import json
import math
import multiprocessing
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


optimize = _module("trader_orb_optimize_f", os.path.join(HERE, "orb_optimize.py"))
validate, perf, orb, scan = optimize.validate, optimize.perf, optimize.orb, optimize.scan

BASES = {
    "elegida": ("10:00", "both", "none", "12:55", 1.7, 0.7, 20),
    "intradia": ("10:00", "both", "none", "12:55", 1.7, 0.7, 0),
}
FEATURES = ("atr_rank", "box_atr", "rvol_sesion", "rvol_vela", "cuerpo", "cierre", "extension", "rango_vela",
            "vwap", "tendencia20", "gap", "hora", "fuerza")
GROUP = {"atr_rank": "volatilidad", "box_atr": "volatilidad", "rvol_sesion": "volumen", "rvol_vela": "volumen"}
STRENGTH = ("rvol_sesion", "cuerpo", "rango_vela")
T930, T1600 = datetime.time(9, 30), datetime.time(16, 0)


def symbol_features(bars):
    """Per day (known before the open) and per regular-session bar (known at its close)."""
    days = orb.by_day(bars)
    daily, order = {}, []
    for d, idx in days:
        r = [i for i in idx if T930 <= bars[i]["t"].time() < T1600]
        if r:
            daily[d] = (bars[r[0]]["o"], max(bars[i]["h"] for i in r), min(bars[i]["l"] for i in r), bars[r[-1]]["c"], r)
            order.append(d)
    info, trs, closes, ranks = {}, [], [], []
    prev_c = None
    for d in order:
        o, h, l, c, r = daily[d]
        atr = sum(trs[-14:]) / 14.0 if len(trs) >= 14 else None
        x = atr / prev_c if atr and prev_c else None
        hist = ranks[-250:]
        rank = sum(1 for y in hist if y <= x) / float(len(hist)) if x is not None and len(hist) >= 60 else None
        info[d] = {"atr": atr, "prev_c": prev_c, "c20": closes[-20] if len(closes) >= 20 else None, "open": o, "atr_rank": rank}
        if x is not None:
            ranks.append(x)
        trs.append(h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c)))
        closes.append(c)
        prev_c = c
    n = len(bars)
    rv_bar, rv_ses, vwap, avg_rng = [None] * n, [None] * n, [None] * n, [None] * n
    slot_v, slot_cum, last_rngs = {}, {}, []
    for d in order:
        r = daily[d][4]
        cum = pv = vv = 0.0
        today = []
        for i in r:
            b = bars[i]
            slot = (b["t"].hour * 60 + b["t"].minute - 570) // 5
            cum += b["v"]
            vv += b["v"]
            pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            hv, hc = slot_v.get(slot, []), slot_cum.get(slot, [])
            if len(hv) >= 10:
                mv, mc = sum(hv[-20:]) / len(hv[-20:]), sum(hc[-20:]) / len(hc[-20:])
                rv_bar[i] = b["v"] / mv if mv else None
                rv_ses[i] = cum / mc if mc else None
            vwap[i] = pv / vv if vv else b["c"]
            if len(last_rngs) >= 14:
                avg_rng[i] = sum(last_rngs[-14:]) / 14.0
            last_rngs.append(b["h"] - b["l"])
            today.append((slot, b["v"], cum))
        for slot, v, c in today:
            slot_v.setdefault(slot, []).append(v)
            slot_cum.setdefault(slot, []).append(c)
            if len(slot_v[slot]) > 40:
                del slot_v[slot][:20]
                del slot_cum[slot][:20]
        del last_rngs[:-14]
    return {"info": info, "rv_bar": rv_bar, "rv_ses": rv_ses, "vwap": vwap, "avg_rng": avg_rng}


def feats_at(bars, fx, i, k, hi, lo, day):
    b = bars[i]
    inf = fx["info"].get(day) or {}
    atr, rng = inf.get("atr"), b["h"] - b["l"]
    box = hi - lo
    t = b["t"]
    out = {
        "atr_rank": inf.get("atr_rank"),
        "box_atr": box / atr if atr else None,
        "rvol_sesion": fx["rv_ses"][i],
        "rvol_vela": fx["rv_bar"][i],
        "cuerpo": k * (b["c"] - b["o"]) / rng if rng > 0 else 0.0,
        "cierre": ((b["c"] - b["l"]) if k > 0 else (b["h"] - b["c"])) / rng if rng > 0 else 1.0,
        "extension": ((b["c"] - hi) if k > 0 else (lo - b["c"])) / box if box > 0 else None,
        "rango_vela": rng / fx["avg_rng"][i] if fx["avg_rng"][i] else None,
        "vwap": k * (b["c"] - fx["vwap"][i]) / atr if atr and fx["vwap"][i] is not None else None,
        "tendencia20": k * (inf["prev_c"] - inf["c20"]) / atr if atr and inf.get("c20") else None,
        "gap": k * (inf["open"] - inf["prev_c"]) / atr if atr and inf.get("prev_c") else None,
        "hora": (t.hour * 60 + t.minute) - 600,
    }
    return out


def strength(f, z):
    """fuerza: the sum of the oriented z-scores of the strength features (None if one is missing)."""
    if z is None:
        return None
    tot = 0.0
    for name in STRENGTH:
        x = f.get(name)
        if x is None:
            return None
        mean, sd, sign = z[name]
        tot += sign * (x - mean) / sd if sd else 0.0
    return tot


DATA, CAL, CTX = {}, {}, {}      # filled before the worker processes fork


def make_accept(sym, rule, rng=None, record=None):
    """rule: None (all), ("random", pass_rate), or (feature, op, threshold) with op ">=" or "<="."""
    d = DATA[sym]

    def accept(i, k, hi, lo, day):
        if rule is not None and rule[0] == "random":
            return rng.random() < rule[1]
        f = feats_at(d["bars"], d["fx"], i, k, hi, lo, day)
        f["fuerza"] = strength(f, CTX.get("z"))
        if record is not None:
            record[(sym, day.isoformat())] = f
        if rule is None:
            return True
        x = f.get(rule[0])
        if x is None:
            return True
        return x >= rule[2] if rule[1] == ">=" else x <= rule[2]
    return accept


def run_rule(base, rule, seed=0, cost=0.01, record=None):
    """-> [(day, net R)] of the base strategy with this entry filter, every index."""
    rng = random.Random(seed)
    out = []
    for sym, d in DATA.items():
        p = optimize.params(BASES[base])
        p["accept"] = make_accept(sym, rule, rng, record)
        for t in orb.run(d["bars"], p, d["ema"]):
            out.append((t["day"], validate.net_r(t["R"], t["risk_pct"], cost)))
    return out


def summary(trades):
    cal, mid = CAL["days"], CAL["mid"]
    xs = [r for _, r in trades]
    st = validate.tstats(xs) if xs else {"n": 0, "mean": 0.0, "se": 0.0}
    x = validate.daily(trades, cal)
    h1 = [r for d, r in trades if d < mid]
    h2 = [r for d, r in trades if d >= mid]
    return {"n": st["n"], "mean": st["mean"], "se": st["se"], "sharpe": perf.sharpe(x) * math.sqrt(252) if xs else 0.0,
            "dd": validate.max_dd(x), "h1": sum(h1) / len(h1) if h1 else 0.0, "n1": len(h1),
            "h2": sum(h2) / len(h2) if h2 else 0.0, "n2": len(h2),
            "se2": validate.tstats(h2)["se"] if len(h2) > 1 else 0.0}


def _worker(jobs, queue):
    for key, base, rule, seed in jobs:
        queue.put((key, summary(run_rule(base, rule, seed, CTX["cost"]))))


def run_jobs(jobs, workers):
    out = {}
    if workers > 1 and len(jobs) > 1:
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        procs = [ctx.Process(target=_worker, args=(jobs[w::workers], queue)) for w in range(workers)]
        for pr in procs:
            pr.start()
        for _ in jobs:
            key, s = queue.get()
            out[key] = s
        for pr in procs:
            pr.join()
    else:
        for key, base, rule, seed in jobs:
            out[key] = summary(run_rule(base, rule, seed, CTX["cost"]))
    return out


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry filters for the 09:00 box breakout: volatility, volume, momentum.")
    ap.add_argument("--symbols", default="QQQ,SPY,IWM,DIA")
    ap.add_argument("--days", type=int, default=1827)
    ap.add_argument("--cost-pct", type=float, default=0.01)
    ap.add_argument("--random", type=int, default=50, help="random filters with the same pass rate, per filter")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--min-n", type=int, default=30, help="first-half trades a measure needs")
    ap.add_argument("--offline", metavar="DIR")
    a = ap.parse_args(argv)
    t0 = time.time()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    data = optimize.load(syms, a.days, a.offline)
    if not data:
        print("Sin datos.")
        return 1
    DATA.clear()
    for sym, d in data.items():
        d["fx"] = symbol_features(d["bars"])
        DATA[sym] = d
    cal = sorted({b["t"].date().isoformat() for d in data.values() for b in d["bars"]
                  if b["t"].weekday() < 5 and T930 <= b["t"].time() < T1600})
    CAL.update(days=cal, mid=cal[len(cal) // 2])
    CTX.update(cost=a.cost_pct, z=None)
    workers = a.workers or os.cpu_count() or 1
    print("Filtros de entrada para la caja de las 09:00: %s, %s a %s (%d dias), costo %.3f%% por lado." % (
        ", ".join(sorted(data)), cal[0], cal[-1], len(cal), a.cost_pct))
    print("Primera mitad (elegir): hasta %s. Segunda mitad (medir, fuera de muestra): desde %s." % (CAL["mid"], CAL["mid"]))

    out = {"from": cal[0], "to": cal[-1], "mid": CAL["mid"], "bases": {}}
    for base in BASES:
        CTX["z"] = None
        rec = {}
        trades = []
        for sym, d in DATA.items():
            p = optimize.params(BASES[base])
            p["accept"] = make_accept(sym, None, None, rec)
            for t in orb.run(d["bars"], p, d["ema"]):
                trades.append((sym, t["day"], validate.net_r(t["R"], t["risk_pct"], a.cost_pct), rec.get((sym, t["day"]), {})))
        h1 = [t for t in trades if t[1] < CAL["mid"]]
        # the strength score, oriented and scaled with the first half only
        z = {}
        for name in STRENGTH:
            xs = [(t[3].get(name), t[2]) for t in h1 if t[3].get(name) is not None]
            if len(xs) < a.min_n:
                z = None
                break
            vals = [x for x, _ in xs]
            lo_c, hi_c = q(vals, 1 / 3.0), q(vals, 2 / 3.0)
            top = [r for x, r in xs if x >= hi_c]
            bot = [r for x, r in xs if x <= lo_c]
            sign = 1.0 if (sum(top) / max(1, len(top))) >= (sum(bot) / max(1, len(bot))) else -1.0
            z[name] = (statistics.mean(vals), statistics.pstdev(vals), sign)
        CTX["z"] = z
        for t in trades:
            t[3]["fuerza"] = strength(t[3], z)
        base_sum = summary([(t[1], t[2]) for t in trades])
        print("\n" + "=" * 100)
        print("BASE %s: %s" % (base.upper(), optimize.label(BASES[base])))
        print("   n %d, %+.3fR por operacion (1a mitad %+.3f, 2a mitad %+.3f), Sharpe %+.2f" % (
            base_sum["n"], base_sum["mean"], base_sum["h1"], base_sum["h2"], base_sum["sharpe"]))

        # 1. thirds
        print("\n1. Cada medida en tercios (cortes de la primera mitad). R por operacion: 1a mitad | 2a mitad")
        print("   %-12s %-12s %26s   %26s   %s" % ("medida", "grupo", "bajo / medio / alto (1a)", "bajo / medio / alto (2a)", "lado mejor"))
        thirds, rules = [], []
        for name in FEATURES:
            xs1 = [(t[3].get(name), t[2]) for t in h1 if t[3].get(name) is not None]
            if len(xs1) < a.min_n:
                continue
            v1 = [x for x, _ in xs1]
            c1, c2, med = q(v1, 1 / 3.0), q(v1, 2 / 3.0), q(v1, 0.5)

            def by_third(ts):
                g = ([], [], [])
                for _, day, r, f in ts:
                    x = f.get(name)
                    if x is not None:
                        g[0 if x < c1 else 2 if x > c2 else 1].append(r)
                return [sum(v) / len(v) if v else None for v in g], [len(v) for v in g]
            m1, n1 = by_third(h1)
            m2, n2 = by_third([t for t in trades if t[1] >= CAL["mid"]])
            up = (m1[2] or 0) >= (m1[0] or 0)
            held = None not in (m2[0], m2[2]) and ((m2[2] > m2[0]) == up)
            grp = GROUP.get(name, "momentum")
            fmt = lambda v: "%+.3f" % v if v is not None else "   -  "
            print("   %-12s %-12s %s / %s / %s    %s / %s / %s    %s%s" % (
                name, grp, fmt(m1[0]), fmt(m1[1]), fmt(m1[2]), fmt(m2[0]), fmt(m2[1]), fmt(m2[2]),
                "alto" if up else "bajo", "  (se repite en la 2a)" if held else ""))
            thirds.append({"feature": name, "group": grp, "cuts": [c1, c2], "h1": m1, "h2": m2, "n1": n1, "n2": n2,
                           "better": "alto" if up else "bajo", "held": held})
            if up:
                rules += [(name, ">=", c1, "quita el tercio bajo"), (name, ">=", med, "deja la mitad alta")]
            else:
                rules += [(name, "<=", c2, "quita el tercio alto"), (name, "<=", med, "deja la mitad baja")]

        # pass rates on the base trades, for the random twins
        def pass_rate(rule):
            xs = [t[3].get(rule[0]) for t in trades]
            ok = sum(1 for x in xs if x is None or (x >= rule[2] if rule[1] == ">=" else x <= rule[2]))
            return ok / float(len(xs)) if xs else 1.0
        jobs = []
        for k, r in enumerate(rules):
            jobs.append((("f", k), base, r[:3], 0))
            pr = pass_rate(r)
            for s in range(a.random):
                jobs.append((("r", k, s), base, ("random", pr), 1000 + 97 * k + s))
        res = run_jobs(jobs, workers)
        print("\n2. Cada filtro dentro del motor (una operacion a la vez por indice) contra quitar al azar la misma parte:")
        print("   %-12s %-22s %6s %8s %8s %8s %7s | %-22s %s" % ("medida", "regla", "n", "R/op", "1a mitad", "2a mitad", "Sharpe",
                                                                  "azar: 2a mitad mediana", "p (2a mitad)"))
        print("   %-12s %-22s %6d %+8.3f %+8.3f %+8.3f %+7.2f" % ("(ninguno)", "", base_sum["n"], base_sum["mean"], base_sum["h1"],
                                                                   base_sum["h2"], base_sum["sharpe"]))
        rows = []
        for k, r in enumerate(rules):
            s = res[("f", k)]
            null = sorted(res[("r", k, j)]["h2"] for j in range(a.random))
            null_sh = sorted(res[("r", k, j)]["sharpe"] for j in range(a.random))
            p2 = sum(1 for v in null if v >= s["h2"]) / float(len(null)) if null else 1.0
            psh = sum(1 for v in null_sh if v >= s["sharpe"]) / float(len(null_sh)) if null_sh else 1.0
            rows.append(dict(s, feature=r[0], rule=r[3], op=r[1], thr=r[2], null_h2=null[len(null) // 2] if null else None,
                             p_h2=p2, p_sharpe=psh, beats_base_h2=s["h2"] > base_sum["h2"]))
            print("   %-12s %-22s %6d %+8.3f %+8.3f %+8.3f %+7.2f | %+22.3f %.2f" % (
                r[0], r[3], s["n"], s["mean"], s["h1"], s["h2"], s["sharpe"], null[len(null) // 2] if null else 0.0, p2))
        n_tests = len(rows)
        good = [r for r in rows if r["p_h2"] < 0.05 and r["h2"] > base_sum["h2"]]
        print("\n   %d filtros probados. Con p < 0,05 y mejor que sin filtro en la 2a mitad: %d (por azar se esperan ~%.1f)." % (
            n_tests, len(good), 0.05 * n_tests))
        for r in sorted(good, key=lambda r: r["p_h2"]):
            print("   - %s %s %.3f (%s): 2a mitad %+.3f vs %+.3f sin filtro, p %.2f; ajustado por %d intentos (Bonferroni) %.2f" % (
                r["feature"], r["op"], r["thr"], r["rule"], r["h2"], base_sum["h2"], r["p_h2"], n_tests, min(1.0, r["p_h2"] * n_tests)))
        out["bases"][base] = {"label": optimize.label(BASES[base]), "base": base_sum, "thirds": thirds, "filters": rows,
                              "z": z, "tests": n_tests}
    print("\n@@ORBFILTERS@@ " + json.dumps(validate.clean(out), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
