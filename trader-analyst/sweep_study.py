#!/usr/bin/env python3
"""Research: the sweep replica (trader-scanner/sweep.py) on years of 30-minute bars of the watchlist,
the same test as the Pine strategy in TradingView's Strategy Tester but on every stock at once, and a
search for patterns in its trades that would sharpen it.

Overfitting guard: the trades are split in time. Filters are looked for in the first half only (the
discovery half); every filter that looks good there is then shown on the second half, which took no
part in choosing it. A filter that does not hold up there is noise. Baseline: random long entries in
the same stocks with a stop at the median stop distance of the rule's own trades, the same target and
the same time limit.

    python3 trader-analyst/sweep_study.py                       # longs, 1.5R, 30m, 5 years
    python3 trader-analyst/sweep_study.py --target 2 --shorts   # both sides, 2R
    python3 trader-analyst/sweep_study.py --offline DIR         # saved Yahoo-shaped charts

Bars: Alpaca's market data when ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are set (years of 30-minute
bars), else Yahoo (60 days only). Prints aggregates only. Research, never orders.
"""

import argparse
import concurrent.futures
import importlib.util
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


sw = _module("trader_sweep_for_study", os.path.join(ROOT, "trader-scanner", "sweep.py"))
scan = sw.scan
BENCH = "SPY"


def stats(rs):
    n = len(rs)
    if not n:
        return None
    m = sum(rs) / n
    se = (sum((x - m) ** 2 for x in rs) / n / n) ** 0.5
    return {"n": n, "mean": m, "se": se, "win": sum(1 for x in rs if x > 0) / float(n), "total": sum(rs)}


def fmt(s):
    if not s:
        return "%-34s" % "      -"
    return "n%5d gana %3.0f%% %+.3fR (+-%.3f)" % (s["n"], 100 * s["win"], s["mean"], s["se"])


def random_longs(bars, atr, k_stop, target_r, max_bars, n, seed):
    rng = random.Random(seed)
    out = []
    if len(bars) < 300:
        return out
    for _ in range(n):
        i = rng.randrange(60, len(bars) - 2)
        if not atr[i]:
            continue
        e, risk = bars[i]["c"], k_stop * atr[i]
        stop, tgt = e - risk, e + target_r * risk
        r = None
        for j in range(i + 1, min(len(bars), i + 1 + max_bars)):
            b = bars[j]
            if b["o"] <= stop:
                r = (b["o"] - e) / risk
            elif b["l"] <= stop:
                r = -1.0
            elif b["h"] >= tgt:
                r = target_r
            if r is not None:
                break
        if r is None:
            r = (bars[min(len(bars) - 1, i + max_bars)]["c"] - e) / risk
        out.append((bars[i]["t"].strftime("%Y-%m-%d"), r))
    return out


def spy_trend(spy_bars, days=20):
    """{New York date: True when SPY closed the day above its average of the last `days` closes}."""
    closes = {}
    for b in spy_bars:
        closes[b["day"]] = b["c"]
    keys = sorted(closes)
    out = {}
    for k in range(days, len(keys)):
        avg = sum(closes[d] for d in keys[k - days:k]) / days
        out[keys[k]] = closes[keys[k - 1]] > avg               # known before that day's open
    return out


CUTS = (
    ("entrada en el hueco (FVG)", lambda t: t["entry_kind"] == "fvg"),
    ("entrada en el retesteo", lambda t: t["entry_kind"] == "retest"),
    ("a favor de la media de 200 velas", lambda t: t["features"]["with_trend"] is True),
    ("contra la media de 200 velas", lambda t: t["features"]["with_trend"] is False),
    ("SPY sobre su media de 20 dias", lambda t: t.get("spy_up") is True),
    ("SPY bajo su media de 20 dias", lambda t: t.get("spy_up") is False),
)
BUCKETS = (
    ("riesgo (stop) en ATR", "risk_atr", (0, 1.5, 2.5, 3.5, 99)),
    ("profundidad del barrido en ATR", "depth_atr", (0, 0.15, 0.4, 1, 99)),
    ("velas del barrido al MSS", "delay", (0, 4, 9, 14, 99)),
    ("vela mas grande hasta el MSS, en ATR", "body_atr", (0, 0.8, 1.3, 2, 99)),
    ("distancia del cierre a la entrada, ATR", "to_entry_atr", (0, 0.5, 1, 2, 99)),
    ("tamano del hueco en ATR", "gap_atr", (0, 0.1, 0.3, 0.7, 99)),
    ("volatilidad (ATR % del precio)", "atr_pct", (0, 0.4, 0.8, 1.5, 99)),
    ("volumen de la vela del barrido / promedio", "vol_ratio", (0, 0.8, 1.2, 2, 99)),
    ("hora NY del barrido", "hour", (9, 10, 11, 13, 15, 16)),
)
DOW = ("lunes", "martes", "miercoles", "jueves", "viernes")


def all_filters():
    out = list(CUTS)
    for label, key, edges in BUCKETS:
        for lo, hi in zip(edges, edges[1:]):
            out.append(("%s %g-%g" % (label, lo, hi), (lambda k, a, b: lambda t: a <= t["features"][k] < b)(key, lo, hi)))
    for d, name in enumerate(DOW):
        out.append(("barrido en %s" % name, (lambda dd: lambda t: t["features"]["dow"] == dd)(d)))
    for lo, hi in ((0, 40), (40, 55), (55, 70), (70, 101)):
        out.append(("calidad propia %d-%d" % (lo, hi), (lambda a, b: lambda t: a <= t["quality"] < b)(lo, hi)))
    return out


def report(trades, rnd, min_n):
    lines = []
    rs = [t["R"] for t in trades]
    s, b = stats(rs), stats([r for _, r in rnd])
    lines.append("Todas: %s | azar %s | ventaja %+.3fR" % (fmt(s), fmt(b), s["mean"] - b["mean"]))
    by = {}
    for t in trades:
        by.setdefault(t["entry_time"][:4], []).append(t["R"])
    rby = {}
    for d, r in rnd:
        rby.setdefault(d[:4], []).append(r)
    for y in sorted(by):
        lines.append("  %s %s | azar %+.3fR" % (y, fmt(stats(by[y])), (stats(rby.get(y, [])) or {"mean": float("nan")})["mean"]))
    outs = {}
    for t in trades:
        outs[t["outcome"]] = outs.get(t["outcome"], 0) + 1
    lines.append("  salidas: %s; velas en la operacion (mediana) %d" % (
        ", ".join("%s %d" % kv for kv in sorted(outs.items())), statistics.median(t["bars"] for t in trades)))

    dates = sorted(t["entry_time"] for t in trades)
    mid = dates[len(dates) // 2][:10]
    A = [t for t in trades if t["entry_time"][:10] < mid]
    B = [t for t in trades if t["entry_time"][:10] >= mid]
    sa, sb = stats([t["R"] for t in A]), stats([t["R"] for t in B])
    lines += ["", "Patrones: mitad de descubrimiento (hasta %s) | mitad de validacion (desde %s)" % (mid, mid),
              "  %-44s %s | %s" % ("todas", fmt(sa), fmt(sb))]
    found = []
    for name, pred in all_filters():
        a = stats([t["R"] for t in A if pred(t)])
        v = stats([t["R"] for t in B if pred(t)])
        lines.append("  %-44s %s | %s" % (name, fmt(a), fmt(v)))
        if a and a["n"] >= min_n and a["mean"] - sa["mean"] >= 2 * a["se"]:
            found.append((name, pred, a, v))
    lines += ["", "Filtros que destacan en la mitad de descubrimiento (al menos %d operaciones y 2 errores sobre el" % min_n,
              "promedio) y lo que hicieron en la de validacion, que no participo en elegirlos:"]
    if not found:
        lines.append("  ninguno")
    for name, pred, a, v in found:
        held = v and v["mean"] - sb["mean"] > 0 and v["mean"] > 0
        lines.append("  %-44s descubrimiento %+.3fR (n%d) -> validacion %s  %s" % (
            name, a["mean"], a["n"], fmt(v), "SE SOSTIENE" if held else "no se sostiene"))
    if len(found) > 1:
        ok = [f for f in found if f[3] and f[3]["mean"] > 0]
        both = [t for t in B if all(f[1](t) for f in ok)] if ok else []
        if ok and len(both) >= 20:
            lines.append("  todos los que se sostienen juntos, en validacion: %s" % fmt(stats([t["R"] for t in both])))
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t["R"])
    pos = sum(1 for v in by_sym.values() if sum(v) > 0)
    lines += ["", "Acciones con resultado total positivo: %d de %d (con %.0f%% de operaciones ganadoras de promedio)" % (
        pos, len(by_sym), 100 * s["win"])]
    sa_sym = {}
    for t in A:
        sa_sym.setdefault(t["symbol"], []).append(t["R"])
    top = [k for k, v in sorted(sa_sym.items(), key=lambda kv: -sum(kv[1])) if len(v) >= 5][:25]
    lines.append("Las 25 mejores acciones de la mitad de descubrimiento, en la de validacion: %s" % fmt(
        stats([t["R"] for t in B if t["symbol"] in top])))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="The sweep replica on years of bars of the watchlist, and its patterns.")
    ap.add_argument("--symbols", default=os.path.join(ROOT, "trader-scanner", "watchlist.example.txt"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--interval", default="30m")
    ap.add_argument("--range", default="5y")
    ap.add_argument("--target", type=float, default=1.5, help="take profit in R (the Pine input)")
    ap.add_argument("--shorts", action="store_true", help="trade the short side too (off, like the Pine default)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="New York time, e.g. '2026-11-17 08:50'")
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--debug", action="store_true", help="per stock: bars, first and last date, bars a year")
    a = ap.parse_args(argv)
    t0 = time.time()
    now = scan.parse_now(a.now)
    syms = scan.symbols_from_file(a.symbols)
    if a.limit:
        syms = syms[:a.limit]
    syms = [s.split(":")[-1] for s in syms]
    p = {"take_profit_r": a.target}
    minutes = sw.BAR_MINUTES[sw.FETCH.get(a.interval, a.interval)]

    def get(sym):
        try:
            if a.offline:
                for name in (sym + ".json", sym + ".json.gz"):
                    path = os.path.join(a.offline, name)
                    if os.path.exists(path):
                        return scan.parse_chart(scan._read_maybe_gz(path), sym)
                return None
            return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, a.range, sw.FETCH.get(a.interval, a.interval))), sym)
        except scan.ScanError:
            return None
    spy = get(BENCH)
    trend = spy_trend(sw.session_bars(spy, now, minutes)) if spy else {}
    trades, rnd, used, skipped, nbars, days = [], [], 0, 0, 0, set()
    ahead = concurrent.futures.ThreadPoolExecutor(max_workers=3)     # download the next stocks while this one runs
    futs = {}
    for k, sym in enumerate(syms):
        for nxt in syms[k:k + 3]:
            if nxt not in futs:
                futs[nxt] = ahead.submit(get, nxt)
        series = futs.pop(sym).result()
        bars = sw.session_bars(series, now, minutes) if series else []
        if a.interval == "4h":
            bars = sw.bars_4h(bars, now)
        if a.debug:
            raw = (series or {}).get("t") or []
            per_year = {}
            for b in bars:
                per_year[b["day"][:4]] = per_year.get(b["day"][:4], 0) + 1
            print("  %s: %d marcas de tiempo, %d velas de sesion, %s a %s, por ano %s" % (
                sym, len(raw), len(bars), bars[0]["day"] if bars else "-", bars[-1]["day"] if bars else "-",
                per_year), file=sys.stderr)
        if len(bars) < 300:
            skipped += 1
            continue
        used += 1
        nbars += len(bars)
        days.update(b["day"] for b in bars[:1] + bars[-1:])
        res = sw.run(bars, p)
        mine = [dict(t, symbol=sym, spy_up=trend.get(t["entry_time"][:10])) for t in res["trades"]
                if a.shorts or t["dir"] > 0]
        trades += mine
        if mine:
            k_stop = statistics.median(t["features"]["risk_atr"] for t in mine)
        else:
            k_stop = 2.5
        rnd += random_longs(bars, sw.atr_series(bars), k_stop, a.target, sw.DEFAULTS["max_bars_in_trade"], 60, k)
        if (k + 1) % 25 == 0:
            print("  %d/%d acciones, %d operaciones, %.0fs" % (k + 1, len(syms), len(trades), time.time() - t0),
                  file=sys.stderr, flush=True)
    span = sorted(days)
    print("Sweep replica, velas de %s, %s, objetivo %gR, sin costos: %d acciones (%d sin datos suficientes), "
          "%d velas, %s a %s." % (a.interval, "largos y cortos" if a.shorts else "solo largos", a.target, used, skipped,
                                  nbars, span[0] if span else "?", span[-1] if span else "?"))
    if not trades:
        print("Sin operaciones: nada que medir (sin keys de Alpaca, Yahoo solo da 60 dias de 30m).")
        return 1
    for ln in report(trades, rnd, a.min_n):
        print(ln)
    print("\nInvestigacion, no asesoria. Sin comisiones (en acciones, ~0,05% por lado son ~0,03R con el stop mediano).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
