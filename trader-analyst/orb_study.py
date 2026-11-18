#!/usr/bin/env python3
"""Research: the 09:00-10:30 box breakout (trader-scanner/orb.py) on years of 5 minute bars.

    python3 trader-analyst/orb_study.py --symbols QQQ,SPY              # Nasdaq 100 and S&P 500, 5 years
    python3 trader-analyst/orb_study.py --symbols NQ=F,ES=F            # the futures (Yahoo: 60 days only)
    python3 trader-analyst/orb_study.py --days 730 --cost-pct 0.05     # the 250 stocks, 2 years

What it prints: the rules as given (3R, stop at the other edge of the box, 15 minute 50/200 EMA filter,
out at the 16:00 close), before and after costs, by year and side; the same with one thing changed at a
time (no filter, filter reversed, other targets, holding for days...); a baseline of random entries in
the filter's direction with the box as the stop; and a search for patterns: filters chosen in the first
half of the period, measured in the second half, which took no part in choosing them.

Bars: Alpaca market data (consolidated tape, pre-market included) when ALPACA_API_KEY_ID /
ALPACA_API_SECRET_KEY are set; Yahoo otherwise and always for futures (=F), 60 days of 5 minute bars.
Prints aggregates only. Research, never orders.
"""

import argparse
import concurrent.futures
import datetime
import importlib.util
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


scan = _module("trader_scan_for_orb", os.path.join(ROOT, "trader-scanner", "scan.py"))
orb = _module("trader_orb", os.path.join(ROOT, "trader-scanner", "orb.py"))


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


def net(t, cost_pct):
    """R after costs: cost_pct per side, as a share of the price, over the risk as a share of the price."""
    return t["R"] - 2 * cost_pct / t["risk_pct"] if t["risk_pct"] else t["R"]


def profit_factor(rs):
    w = sum(r for r in rs if r > 0)
    l = -sum(r for r in rs if r < 0)
    return w / l if l else float("inf")


def max_drawdown(trades, key):
    peak = eq = dd = 0.0
    for t in sorted(trades, key=lambda t: t.get("entry_time", t["day"])):
        eq += key(t)
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


VARIANTS = (
    ("las reglas tal cual (3R, filtro 15m, cierre 16:00)", {}),
    ("sin el filtro de medias", {"filter": "none"}),
    ("filtro al reves", {"filter": "counter"}),
    ("solo la primera ruptura del dia", {"first_only": True}),
    ("solo largos", {"sides": "long"}),
    ("solo cortos", {"sides": "short"}),
    ("objetivo 1R", {"target_r": 1.0}),
    ("objetivo 1.5R", {"target_r": 1.5}),
    ("objetivo 2R", {"target_r": 2.0}),
    ("entradas solo hasta las 12:00", {"last_entry": "12:00"}),
    ("mantener hasta 3 dias (3R)", {"hold_days": 3}),
    ("mantener hasta 10 dias (3R)", {"hold_days": 10}),
)

CUTS = (
    ("largos", lambda t: t["dir"] > 0),
    ("cortos", lambda t: t["dir"] < 0),
    ("la primera ruptura del dia fue al otro lado", lambda t: t["features"]["first_against"]),
    ("la primera ruptura fue esta", lambda t: not t["features"]["first_against"]),
)
BUCKETS = (
    ("hora NY de la entrada", "hour", (10.5, 11, 12, 14, 16)),
    ("caja / rango diario medio", "box_vs_range", (0, 0.4, 0.6, 0.8, 99)),
    ("cierre mas alla de la caja, en cajas", "ext_box", (0, 0.05, 0.15, 0.3, 99)),
    ("distancia entre medias 50/200, % precio", "ema_gap_pct", (0, 0.1, 0.25, 0.5, 99)),
)
DOW = ("lunes", "martes", "miercoles", "jueves", "viernes")


def all_filters():
    out = list(CUTS)
    for label, key, edges in BUCKETS:
        for lo, hi in zip(edges, edges[1:]):
            out.append(("%s %g-%g" % (label, lo, hi),
                        (lambda k, a, b: lambda t: t["features"][k] is not None and a <= t["features"][k] < b)(key, lo, hi)))
    for d, name in enumerate(DOW):
        out.append(("entrada en %s" % name, (lambda dd: lambda t: t["features"]["dow"] == dd)(d)))
    return out


def summary_lines(trades, rnd, cost_pct):
    rs = [t["R"] for t in trades]
    ns = [net(t, cost_pct) for t in trades]
    s, sn, b = stats(rs), stats(ns), stats([r["R"] for r in rnd])
    years = {}
    for t in trades:
        years.setdefault(t["day"][:4], []).append(t)
    out = ["Reglas tal cual: %s | con costos %+.3fR | azar %s" % (fmt(s), sn["mean"], fmt(b)),
           "  factor de beneficio %.2f, suma %+.1fR antes de costos (%+.1fR despues), peor caida %.1fR, %.0f operaciones por ano y simbolo" % (
               profit_factor(rs), sum(rs), sum(ns), max_drawdown(trades, lambda t: net(t, cost_pct)),
               len(trades) / max(1.0, len(set(t["symbol"] for t in trades))) / max(1.0, len(years))),
           "  stop mediano %.2f%% del precio, costo de ida y vuelta %.3fR por operacion" % (
               sorted(t["risk_pct"] for t in trades)[len(trades) // 2], 2 * cost_pct / sorted(t["risk_pct"] for t in trades)[len(trades) // 2])]
    rby = {}
    for r in rnd:
        rby.setdefault(r["day"][:4], []).append(r["R"])
    for y in sorted(years):
        st = stats([t["R"] for t in years[y]])
        out.append("  %s %s | con costos %+.3fR | azar %+.3fR" % (
            y, fmt(st), stats([net(t, cost_pct) for t in years[y]])["mean"], (stats(rby.get(y, [])) or {"mean": float("nan")})["mean"]))
    outs = {}
    for t in trades:
        outs[t["outcome"]] = outs.get(t["outcome"], 0) + 1
    out.append("  salidas: %s" % ", ".join("%s %d" % kv for kv in sorted(outs.items())))
    return out


def pattern_lines(trades, min_n):
    dates = sorted(t["day"] for t in trades)
    mid = dates[len(dates) // 2]
    A = [t for t in trades if t["day"] < mid]
    B = [t for t in trades if t["day"] >= mid]
    sa, sb = stats([t["R"] for t in A]), stats([t["R"] for t in B])
    lines = ["", "Patrones: mitad de descubrimiento (hasta %s) | mitad de validacion (desde %s)" % (mid, mid),
             "  %-46s %s | %s" % ("todas", fmt(sa), fmt(sb))]
    found = []
    for name, pred in all_filters():
        a = stats([t["R"] for t in A if pred(t)])
        v = stats([t["R"] for t in B if pred(t)])
        lines.append("  %-46s %s | %s" % (name, fmt(a), fmt(v)))
        if a and sa and a["n"] >= min_n and a["mean"] - sa["mean"] >= 2 * a["se"]:
            found.append((name, a, v))
    lines += ["", "Filtros que destacan en la mitad de descubrimiento (al menos %d operaciones y 2 errores sobre el" % min_n,
              "promedio) y lo que hicieron en la de validacion, que no participo en elegirlos:"]
    if not found:
        lines.append("  ninguno")
    for name, a, v in found:
        held = v and sb and v["mean"] > sb["mean"] and v["mean"] > 0
        lines.append("  %-46s descubrimiento %+.3fR (n%d) -> validacion %s  %s" % (
            name, a["mean"], a["n"], fmt(v), "SE SOSTIENE" if held else "no se sostiene"))
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t["R"])
    if len(by_sym) > 30:
        a_sym = {}
        for t in A:
            a_sym.setdefault(t["symbol"], []).append(t["R"])
        top = [k for k, v in sorted(a_sym.items(), key=lambda kv: -sum(kv[1])) if len(v) >= 5][:25]
        lines += ["", "Simbolos con resultado total positivo: %d de %d" % (sum(1 for v in by_sym.values() if sum(v) > 0), len(by_sym)),
                  "Los 25 mejores de la mitad de descubrimiento, en la de validacion: %s" % fmt(
                      stats([t["R"] for t in B if t["symbol"] in top]))]
    return lines


def fetch(sym, days, offline=None, now_utc=None):
    """5 minute bars, pre-market included -> Yahoo-shaped series, or None."""
    if offline:
        for name in (sym + ".json", sym + ".json.gz"):
            path = os.path.join(offline, name)
            if os.path.exists(path):
                return scan.parse_chart(scan._read_maybe_gz(path), sym)
        return None
    md = scan._marketdata()
    if md is not None and md.enabled() and not sym.endswith("=F"):
        now_utc = now_utc or datetime.datetime.utcnow().replace(microsecond=0)
        start = md._iso(now_utc - datetime.timedelta(days=days))
        end = md._iso(now_utc - datetime.timedelta(minutes=16))
        bs = md.bars_chunked(md._to_alpaca(sym), "5Min", start, end, "sip")
        return scan.series_from_result(md.chart_result(sym, bs, "5m", "%dd" % days)) if bs else None
    return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, "60d", "5m") + "&includePrePost=true"), sym)


def main(argv=None):
    ap = argparse.ArgumentParser(description="The 09:00-10:30 box breakout on 5 minute bars, and its patterns.")
    ap.add_argument("--symbols", default=os.path.join(ROOT, "trader-scanner", "watchlist.example.txt"),
                    help="a file with one symbol a line, or a comma list (QQQ,SPY or NQ=F,ES=F)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--days", type=int, default=1827, help="history in calendar days (Alpaca)")
    ap.add_argument("--target", type=float, default=3.0)
    ap.add_argument("--cost-pct", type=float, default=0.01, help="cost per side, %% of the price (stocks: ~0.05)")
    ap.add_argument("--offline", metavar="DIR", help="saved Yahoo-shaped charts, SYM.json or SYM.json.gz")
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    if os.path.exists(a.symbols):
        syms = [s.split(":")[-1] for s in scan.symbols_from_file(a.symbols)]
    else:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.limit:
        syms = syms[:a.limit]
    base = {"target_r": a.target}

    def get(sym):
        try:
            return fetch(sym, a.days, a.offline)
        except scan.ScanError:
            return None

    trades, rnd, var = [], [], {name: [] for name, _ in VARIANTS}
    used, skipped, first, last = 0, [], None, None
    ahead = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    futs = {}
    for k, sym in enumerate(syms):
        for nxt in syms[k:k + 3]:
            if nxt not in futs:
                futs[nxt] = ahead.submit(get, nxt)
        series = futs.pop(sym).result()
        bars = orb.ny_bars(series, scan.ts_to_eastern) if series else []
        if a.debug:
            print("  %s: %d velas, %s a %s" % (sym, len(bars), bars[0]["t"] if bars else "-", bars[-1]["t"] if bars else "-"),
                  file=sys.stderr)
        if len(bars) < 2000:
            skipped.append(sym)
            continue
        used += 1
        first = min(first or bars[0]["t"], bars[0]["t"])
        last = max(last or bars[-1]["t"], bars[-1]["t"])
        ema = orb.ema_15m(bars)
        for name, extra in VARIANTS:
            got = orb.run(bars, dict(base, **extra), ema)
            for t in got:
                t["symbol"] = sym
            var[name] += got
        rnd += orb.random_trades(bars, base, ema, random.Random(k), per_day=2)
        if (k + 1) % 25 == 0:
            print("  %d/%d simbolos, %d operaciones, %.0fs" % (k + 1, len(syms), len(trades), time.time() - t0), file=sys.stderr)
    ahead.shutdown()
    trades = var[VARIANTS[0][0]]
    if not trades:
        print("Sin operaciones (%d simbolos sin datos: %s)." % (len(skipped), ", ".join(skipped[:10])))
        return 1
    print("Caja 09:00-10:30 en velas de 5 min, ruptura con cierre, stop al otro lado de la caja, objetivo %gR, "
          "filtro EMA 50/200 de 15 min, fuera a las 16:00. Costo %.2f%% por lado." % (a.target, a.cost_pct))
    print("%d simbolos (%d sin datos suficientes%s), %s a %s." % (
        used, len(skipped), (": " + ", ".join(skipped[:10])) if skipped else "", first.date(), last.date()))
    for line in summary_lines(trades, rnd, a.cost_pct):
        print(line)
    print("\nUna cosa distinta a la vez (antes de costos | despues de costos):")
    for name, _ in VARIANTS:
        st = stats([t["R"] for t in var[name]])
        print("  %-50s %s | %+.3fR" % (name, fmt(st), stats([net(t, a.cost_pct) for t in var[name]])["mean"] if st else float("nan")))
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)
    if len(by_sym) <= 30:
        print("\nPor simbolo:")
        for sym in sorted(by_sym):
            ts = by_sym[sym]
            print("  %-8s %s | con costos %+.3fR | factor %.2f" % (
                sym, fmt(stats([t["R"] for t in ts])), stats([net(t, a.cost_pct) for t in ts])["mean"],
                profit_factor([t["R"] for t in ts])))
    for line in pattern_lines(trades, a.min_n):
        print(line)
    print("\nInvestigacion, no asesoria. Una operacion al dia por simbolo; en una vela que toca stop y objetivo cuenta el stop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
