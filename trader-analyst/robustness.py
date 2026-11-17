"""Robustness tests for a trading rule (standard library only): walk-forward, parameter sensitivity
and block bootstrap. Research, never orders.

An event is one entry signal: {"date": "YYYY-MM-DD", "entry": price at the signal close, "atr": ATR
at the signal, "fwd": array('d') of the next bars' o, h, l, c (flat), plus any fields a filter
needs}. A long is walked bar by bar like everywhere else in this repo: the stop is checked first, a
gap through it fills at the open, a gap over the target gets the target only, still open after H
bars = out at the close of bar H. Every R is net of a round-trip cost (cost_pct of the entry, in R).

    out = outcomes(events, K, T, Hs)      # R of every event for every horizon in Hs, one pass
    walk_forward(...)                     # choose on the past, measure on the next year
    sensitivity(...)                      # how the result moves around the chosen parameters
    bootstrap(...)                        # month-block resampling: intervals for mean R, edge, drawdown
"""

import math
import random
from array import array


def fwd_array(bars):
    """[{o, h, l, c}] -> array('d') o, h, l, c, o, h, ..."""
    out = array("d")
    for b in bars:
        out.extend((b["o"], b["h"], b["l"], b["c"]))
    return out


def outcomes(events, K, T, Hs, cost_pct=0.1):
    """-> [[R at H for H in Hs] for each event]. K = stop in ATRs, T = target in R (None = none)."""
    Hs = sorted(Hs)
    maxH = Hs[-1]
    out = []
    for e in events:
        E, a, f = e["entry"], e["atr"], e["fwd"]
        risk = K * a
        if risk <= 0:
            out.append([0.0] * len(Hs))
            continue
        cost = cost_pct / 100.0 * E / risk
        stop = E - risk
        tgt = E + T * risk if T else None
        n = min(maxH, len(f) // 4)
        res, last, rs, hi = None, 0.0, [], 0
        for d in range(1, n + 1):
            o, h, l, c = f[4 * d - 4], f[4 * d - 3], f[4 * d - 2], f[4 * d - 1]
            if res is None:
                if o <= stop:
                    res = (o - E) / risk
                elif l <= stop:
                    res = -1.0
                elif tgt is not None and h >= tgt:
                    res = float(T)
                last = (c - E) / risk
            while hi < len(Hs) and Hs[hi] == d:
                rs.append((res if res is not None else last) - cost)
                hi += 1
            if res is not None and hi < len(Hs):
                while hi < len(Hs):
                    rs.append(res - cost)
                    hi += 1
                break
        while hi < len(Hs):                     # the data ran out: the last close
            rs.append((res if res is not None else last) - cost)
            hi += 1
        out.append(rs)
    return out


class Grid(object):
    """R of every event under every (H, K, T), computed once per (K, T)."""

    def __init__(self, events, Hs, Ks, Ts, cost_pct=0.1):
        self.events, self.Hs = events, sorted(Hs)
        self.table = {(K, T): outcomes(events, K, T, self.Hs, cost_pct) for K in Ks for T in Ts}

    def rs(self, H, K, T, idx=None):
        h = self.Hs.index(H)
        t = self.table[(K, T)]
        return [t[i][h] for i in (range(len(t)) if idx is None else idx)]


def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "mean": float("nan"), "se": float("nan"), "win": float("nan"), "total": 0.0}
    m = sum(rs) / n
    var = sum((x - m) ** 2 for x in rs) / n
    return {"n": n, "mean": m, "se": math.sqrt(var / n), "win": sum(1 for x in rs if x > 0) / float(n), "total": sum(rs)}


def walk_forward(grid, rand_grid, configs, fixed, select, train_years=5, min_n=30):
    """Each test year: pick the config with the best mean R over the previous `train_years` years
    (at least min_n trades), then record what it did in the test year, next to the fixed config and
    random entries under the chosen config. select(event, cfg) -> True when the event is a trade.
    -> [{"year", "chosen", "n", "oos", "fixed", "rand", "train"}]"""
    years = sorted({int(e["date"][:4]) for e in grid.events})
    rows = []
    by_year = {}
    for i, e in enumerate(grid.events):
        by_year.setdefault(int(e["date"][:4]), []).append(i)
    rby = {}
    for i, e in enumerate(rand_grid.events):
        rby.setdefault(int(e["date"][:4]), []).append(i)

    def rs_of(g, cfg, idx):
        sel = [i for i in idx if select(g.events[i], cfg)] if g is grid else idx
        return g.rs(cfg["H"], cfg["K"], cfg["T"], sel)
    for y in years:
        train = [i for yy in range(y - train_years, y) for i in by_year.get(yy, [])]
        test = by_year.get(y, [])
        if not test or len({yy for yy in range(y - train_years, y) if yy in by_year}) < train_years:
            continue
        best, best_m = None, None
        for cfg in configs:
            r = rs_of(grid, cfg, train)
            if len(r) < min_n:
                continue
            m = sum(r) / len(r)
            if best is None or m > best_m:
                best, best_m = cfg, m
        if best is None:
            continue
        rows.append({"year": y, "chosen": best, "train": best_m, "oos": rs_of(grid, best, test),
                     "fixed": rs_of(grid, fixed, test), "rand": rs_of(rand_grid, best, rby.get(y, [])),
                     "rand_fixed": rs_of(rand_grid, fixed, rby.get(y, []))})
    return rows


def bootstrap(trades, rand, n=2000, seed=19):
    """trades, rand: [(date, R)]. Resample whole months with replacement (keeps the clustering of
    trades in time). -> {"mean": (p2.5, p50, p97.5), "p_mean_le0", "edge": (...), "p_edge_le0",
    "maxdd": (p50, p95), "p_losing_year": share of resampled years with a negative total}"""
    months = {}
    for d, r in trades:
        months.setdefault(d[:7], [[], []])[0].append(r)
    for d, r in rand:
        if d[:7] in months:
            months[d[:7]][1].append(r)
    keys = sorted(months)
    if not keys:
        return None
    rng = random.Random(seed)
    means, edges, dds, years_neg, years = [], [], [], 0, 0
    for _ in range(n):
        pick = [keys[rng.randrange(len(keys))] for _ in keys]
        s = [r for k in pick for r in months[k][0]]
        b = [r for k in pick for r in months[k][1]]
        if not s:
            continue
        m = sum(s) / len(s)
        means.append(m)
        if b:
            edges.append(m - sum(b) / len(b))
        eq = peak = dd = 0.0
        for k in pick:
            for r in months[k][0]:
                eq += r
                peak = max(peak, eq)
                dd = max(dd, peak - eq)
        dds.append(dd)
        for j in range(0, len(pick) - 11, 12):
            years += 1
            if sum(r for k in pick[j:j + 12] for r in months[k][0]) < 0:
                years_neg += 1

    def pct(xs, p):
        xs = sorted(xs)
        return xs[min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))] if xs else float("nan")
    return {"mean": (pct(means, 2.5), pct(means, 50), pct(means, 97.5)),
            "p_mean_le0": sum(1 for x in means if x <= 0) / float(len(means)),
            "edge": (pct(edges, 2.5), pct(edges, 50), pct(edges, 97.5)) if edges else None,
            "p_edge_le0": (sum(1 for x in edges if x <= 0) / float(len(edges))) if edges else None,
            "maxdd": (pct(dds, 50), pct(dds, 95)), "p_losing_year": years_neg / float(years) if years else None,
            "months": len(keys), "trades": len(trades)}


# ---------------------------------------------------------------- printing

def fmt(s):
    if not s["n"]:
        return "     0 ops"
    return "n%6d gana %3.0f%% %+.3fR (+-%.3f)" % (s["n"], 100 * s["win"], s["mean"], s["se"])


def cfg_label(cfg):
    t = "sin obj." if not cfg["T"] else "%gR" % cfg["T"]
    extra = "".join(" %s %s" % (k, v) for k, v in sorted(cfg.items()) if k not in ("H", "K", "T"))
    return "%2dd %gATR %s%s" % (cfg["H"], cfg["K"], t, extra)


def report_walk_forward(rows, fixed):
    lines = ["Walk-forward: cada ano se elige la mejor configuracion de los 5 anos anteriores y se mide en el ano",
             "siguiente, que no vio. 'fija' = la configuracion en uso; 'azar' = entradas al azar con lo elegido.",
             "  %-6s %-30s %8s | %-28s | %-28s | %s" % ("ano", "elegida", "en train", "fuera de muestra (elegida)",
                                                      "fija (%s)" % cfg_label(fixed), "azar")]
    allo, allf, allr, allrf = [], [], [], []
    for r in rows:
        so, sf, sr = stats(r["oos"]), stats(r["fixed"]), stats(r["rand"])
        allo += r["oos"]; allf += r["fixed"]; allr += r["rand"]; allrf += r["rand_fixed"]
        lines.append("  %-6d %-30s %+7.3fR | n%5d %+.3fR (+-%.3f) | n%5d %+.3fR (+-%.3f) | %+.3fR" % (
            r["year"], cfg_label(r["chosen"]), r["train"], so["n"], so["mean"], so["se"], sf["n"], sf["mean"], sf["se"],
            sr["mean"]))
    so, sf, sr, srf = stats(allo), stats(allf), stats(allr), stats(allrf)
    lines.append("  TOTAL fuera de muestra: elegida %s | fija %s" % (fmt(so), fmt(sf)))
    lines.append("  azar con lo elegido %+.3fR, azar con la fija %+.3fR -> ventaja elegida %+.3fR, fija %+.3fR" % (
        sr["mean"], srf["mean"], so["mean"] - sr["mean"], sf["mean"] - srf["mean"]))
    pos = sum(1 for r in rows if stats(r["oos"])["n"] and stats(r["oos"])["mean"] > 0)
    lines.append("  anos fuera de muestra positivos (elegida): %d de %d" % (pos, len(rows)))
    return lines


def report_bootstrap(name, b):
    if not b:
        return ["  %s: sin datos" % name]
    lines = ["  %s (%d operaciones en %d meses; 2.000 remuestreos por mes):" % (name, b["trades"], b["months"]),
             "    R promedio: %+.3f [%+.3f a %+.3f] (95%%), P(promedio <= 0) = %.1f%%" % (
                 b["mean"][1], b["mean"][0], b["mean"][2], 100 * b["p_mean_le0"])]
    if b["edge"]:
        lines.append("    ventaja sobre el azar: %+.3f [%+.3f a %+.3f], P(ventaja <= 0) = %.1f%%" % (
            b["edge"][1], b["edge"][0], b["edge"][2], 100 * b["p_edge_le0"]))
    lines.append("    caida maxima (en R sumados): mediana %.0fR, 1 de cada 20 caminos peor que %.0fR" % b["maxdd"])
    if b["p_losing_year"] is not None:
        lines.append("    probabilidad de un ano negativo: %.0f%%" % (100 * b["p_losing_year"]))
    return lines
