#!/usr/bin/env python3
"""Research: entering the 09:00 box breakout better. The chosen rules (09:00-10:00 box, both sides, entries until
13:00, 70% at 1.7R and the rest at breakeven up to 20 days, one position at a time per index) with other ways in:

    close       the backtest's entry: the close of the breakout bar
    cap         the live bot's: a limit at that close + 0.15R, waiting until 13:00 if the price already ran, or
                with no wait: only if the next bar opens inside it (0.10, 0.15, 0.25R)
    next_open   a market order seconds after the close: the next bar's open, with no cap
    pullback    a limit back toward the box after the breakout: half way to the edge, or the edge itself
    mid stop    the stop in the middle of the box instead of its other edge (a cheaper R, more stops)
    reversal    after a stop, a close past the other edge of the box (until 13:00) is traded that way

    python3 trader-analyst/orb_entries.py --symbols QQQ,SPY,IWM,DIA --days 1827

Each one after costs (the cost in R grows when the stop is closer), with the share of signal days that got an
entry, the first and second half and the years. Aggregates only. Research, never orders.
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


optimize = _module("trader_orb_optimize_e", os.path.join(HERE, "orb_optimize.py"))
validate, perf, orb = optimize.validate, optimize.perf, optimize.orb

BASE = ("10:00", "both", "none", "12:55", 1.7, 0.7, 20)
VARIANTS = [
    ("Al cierre de la vela (la elegida)", {}),
    ("Limite al cierre + 0,15R hasta las 13:00 (el bot en vivo)", {"entry": "cap", "cap_r": 0.15}),
    ("Limite al cierre + 0,15R sin espera", {"entry": "cap", "cap_r": 0.15, "cap_wait": False}),
    ("Limite al cierre + 0,10R sin espera", {"entry": "cap", "cap_r": 0.10, "cap_wait": False}),
    ("Limite al cierre + 0,25R sin espera", {"entry": "cap", "cap_r": 0.25, "cap_wait": False}),
    ("A mercado al abrir la vela siguiente (sin tope)", {"entry": "next_open"}),
    ("Pullback a la mitad del camino al borde", {"entry": "pullback", "pull_frac": 0.5}),
    ("Pullback hasta el borde de la caja", {"entry": "pullback", "pull_frac": 1.0}),
    ("Al cierre, stop en el medio de la caja", {"stop_mode": "mid"}),
    ("Pullback a la mitad, stop en el medio", {"entry": "pullback", "pull_frac": 0.5, "stop_mode": "mid"}),
    ("Pullback al borde, stop en el medio", {"entry": "pullback", "pull_frac": 1.0, "stop_mode": "mid"}),
    ("Al cierre + reversal tras un stop", {"reverse": True}),
    ("Al cierre + reversal, stop en el medio", {"reverse": True, "stop_mode": "mid"}),
    ("Pullback a la mitad + reversal", {"entry": "pullback", "pull_frac": 0.5, "reverse": True}),
]


def run_variant(data, extra, cost):
    p = optimize.params(BASE)
    p["last_entry"] = "12:55"
    p.update(extra)
    out = []
    for sym, d in data.items():
        for t in orb.run(d["bars"], p, d["ema"]):
            out.append((t["day"], validate.net_r(t["R"], t["risk_pct"], cost), sym, bool(t.get("reversal")), t["risk_pct"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Other ways into the box breakout: pullback, closer stop, reversal.")
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
    print("Entradas para la caja de las 09:00: %s, %s a %s, costo %.3f%% por lado. Reglas: caja 09:00-10:00, ambos lados, "
          "hasta 13:00, 70%% a 1,7R y el resto en breakeven hasta 20 dias, una a la vez por indice." % (
              ", ".join(sorted(data)), cal[0], cal[-1], a.cost_pct))
    print("\n   %-58s %6s %8s %7s %7s %8s %7s %8s %8s %8s" % ("variante", "n", "R/op", "+-", "Sharpe", "R total", "caida",
                                                              "1a mitad", "2a mitad", "riesgo %"))
    rows = []
    base_n = None
    for name, extra in VARIANTS:
        ts = run_variant(data, extra, a.cost_pct)
        xs = [t[1] for t in ts]
        st = validate.tstats(xs) if xs else {"n": 0, "mean": 0.0, "se": 0.0}
        x = validate.daily([(t[0], t[1]) for t in ts], cal)
        h1 = [t[1] for t in ts if t[0] < mid]
        h2 = [t[1] for t in ts if t[0] >= mid]
        rev = [t[1] for t in ts if t[3]]
        row = {"name": name, "n": st["n"], "mean": st["mean"], "se": st["se"], "sharpe": perf.sharpe(x) * math.sqrt(252),
               "total": sum(xs), "dd": validate.max_dd(x), "h1": sum(h1) / len(h1) if h1 else 0.0,
               "h2": sum(h2) / len(h2) if h2 else 0.0, "risk": sum(t[4] for t in ts) / len(ts) if ts else 0.0,
               "rev_n": len(rev), "rev_mean": sum(rev) / len(rev) if rev else None,
               "years": {y: (lambda v: sum(v) / len(v) if v else None)([t[1] for t in ts if t[0][:4] == y]) for y in years},
               "by_symbol": {s: (lambda v: sum(v) / len(v) if v else None)([t[1] for t in ts if t[2] == s]) for s in sorted(data)}}
        if base_n is None:
            base_n = row["n"]
        rows.append(row)
        print("   %-58s %6d %+8.3f %7.3f %+7.2f %+8.1f %7.1f %+8.3f %+8.3f %8.2f" % (
            name[:58], row["n"], row["mean"], row["se"], row["sharpe"], row["total"], row["dd"], row["h1"], row["h2"], row["risk"]))
    print("\n   (R por operacion despues de costos; Sharpe anual del resultado diario; R total = suma de los 5 anos a igual riesgo")
    print("    por operacion; riesgo % = distancia media de la entrada al stop, en % del precio)")
    print("\n   Reversals: " + " | ".join("%s: %d, %+.3fR" % (r["name"].split(",")[0][:30], r["rev_n"], r["rev_mean"])
                                         for r in rows if r["rev_n"]))
    print("\nPor ano (R por operacion):")
    print("   %-58s %s" % ("", " ".join("%7s" % y for y in years)))
    for r in rows:
        print("   %-58s %s" % (r["name"][:58], " ".join("%+7.3f" % v if v is not None else "      -" for v in r["years"].values())))
    print("\nPor indice (R por operacion):")
    for r in rows:
        print("   %-58s %s" % (r["name"][:58], "  ".join("%s %+.3f" % (s, v) for s, v in r["by_symbol"].items() if v is not None)))
    print("\n@@ORBENTRIES@@ " + json.dumps(validate.clean({"from": cal[0], "to": cal[-1], "rows": rows}), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
