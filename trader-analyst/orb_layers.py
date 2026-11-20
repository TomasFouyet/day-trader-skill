#!/usr/bin/env python3
"""Research: what to do with the 09:00 box signal of a day when a trade from an earlier day is still open.

The chosen rules (09:00-10:00 box, both sides, entries until 13:00, 70% at 1.7R and the rest at breakeven up
to 20 days) skip that signal: one position at a time per index. Two other ways, simulated bar by bar with
every open layer of an index:

    reversal   the new signal goes against the open trade: close it at that bar's close and take the new one
    reload     the new signal goes the same way: add a new trade (its own 70% at 1.7R) and move the stop of
               the whole position to its breakeven price (the share-weighted average entry), or to the new
               box's edge when that is closer: if it is hit, the position as a whole loses nothing

    python3 trader-analyst/orb_layers.py --symbols QQQ,SPY,IWM,DIA --days 1827

Each layer counts on the day it opens, after costs, sized to 1R on its distance to the box edge. With both
off this reproduces the chosen rules (a check). Aggregates only. Research, never orders.
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


optimize = _module("trader_orb_optimize_l", os.path.join(HERE, "orb_optimize.py"))
validate, perf, orb = optimize.validate, optimize.perf, optimize.orb

BASE = ("10:00", "both", "none", "12:55", 1.7, 0.7, 20)
MODES = [
    ("Una a la vez (la elegida)", False, False),
    ("Reversal: la senal contraria cierra y da vuelta", True, False),
    ("Recargar: la senal a favor agrega y sube el stop", False, True),
    ("Reversal y recargar", True, True),
]


class Leg(object):
    __slots__ = ("dir", "entry", "risk", "stop", "ptgt", "took", "first", "frac", "end", "day", "kind", "done", "R")

    def __init__(self, d, entry, stop, end, day, kind, partial_r, frac):
        self.dir, self.entry, self.stop, self.end, self.day, self.kind = d, entry, stop, end, day, kind
        self.risk = abs(entry - stop)
        self.ptgt = entry + d * partial_r * self.risk
        self.took, self.first, self.frac, self.done, self.R = False, None, frac, False, None

    def rr(self, x):
        return (x - self.entry) * self.dir / self.risk

    def close(self, px):
        self.R = (self.frac * self.first + (1 - self.frac) * self.rr(px)) if self.took else self.rr(px)
        self.done = True

    def shares(self):
        """Position size, with 1R = one unit of risk at the entry: the rest after the partial counts its share."""
        return (1 - self.frac if self.took else 1.0) / self.risk

    def step(self, b, partial_r):
        """One bar, like orb.simulate_partial: the open before the range, the stop before the target."""
        d, o, hi, lo = self.dir, b["o"], b["h"], b["l"]
        fav, adv = (hi, lo) if d > 0 else (lo, hi)

        def past(x, level):
            return (x - level) * d >= 0
        if not self.took:
            if not past(o, self.stop) or o == self.stop:
                return self.close(o)
            if not past(adv, self.stop) or adv == self.stop:
                return self.close(self.stop)
            if past(o, self.ptgt):
                self.took, self.first = True, self.rr(o)
                self.stop = self.more_protective(self.entry)
            elif past(fav, self.ptgt):
                self.took, self.first = True, partial_r
                self.stop = self.more_protective(self.entry)
            return None
        if not past(o, self.stop) or o == self.stop:
            return self.close(o)
        if not past(adv, self.stop) or adv == self.stop:
            return self.close(self.stop)
        return None

    def more_protective(self, level):
        return max(self.stop, level) if self.dir > 0 else min(self.stop, level)


def simulate_symbol(bars, ema, p, reverse, reload_, cost):
    """Every layer of one index. -> [(entry day, net R, kind)]."""
    p = dict(orb.DEFAULTS, **p)
    days = orb.by_day(bars)
    pos = {d: k for k, (d, _) in enumerate(days)}
    flat = orb._hm(p["flat"])
    last = orb._hm(p["last_entry"])
    b1 = orb._hm(p["box_end"])
    box = {d: (hi, lo, sess) for d, hi, lo, sess in orb.boxes(bars, p)}
    legs, out = [], []
    for d, idx in days:
        bx = box.get(d)
        acted = False
        for i in idx:
            b = bars[i]
            for leg in legs:
                if leg.done:
                    continue
                leg.step(b, p["partial_r"])
                if not leg.done and i >= leg.end:
                    leg.close(b["c"])
            closed_now = [x for x in legs if x.done]
            for leg in closed_now:
                out.append((leg.day, validate.net_r(leg.R, leg.risk / leg.entry * 100, cost), leg.kind))
            legs = [x for x in legs if not x.done]
            if bx is None or acted or closed_now:            # the bar a trade ends on is not a new entry (as orb.run)
                continue
            t = b["t"].time()
            if t < b1 or t > last:
                continue
            hi, lo, sess = bx
            c = b["c"]
            k = 1 if c > hi else -1 if c < lo else 0
            if not k:
                continue
            end = orb.end_index(bars, days, pos[d], sess[-1], p["hold_days"], flat)
            stop0 = lo if k > 0 else hi
            if not legs:
                legs.append(Leg(k, c, stop0, end, d.isoformat(), "nueva", p["partial_r"], p["partial_frac"]))
                acted = True
            elif legs[0].dir != k and reverse:
                for leg in legs:                          # the breakout went the other way: out, and turn
                    leg.close(c)
                    out.append((leg.day, validate.net_r(leg.R, leg.risk / leg.entry * 100, cost), leg.kind))
                legs = [Leg(k, c, stop0, end, d.isoformat(), "reversal", p["partial_r"], p["partial_frac"])]
                acted = True
            elif legs[0].dir == k and reload_:
                new = Leg(k, c, stop0, end, d.isoformat(), "recarga", p["partial_r"], p["partial_frac"])
                legs.append(new)
                # the whole position's breakeven: the share-weighted average entry
                w = sum(x.shares() for x in legs)
                be = sum(x.shares() * x.entry for x in legs) / w
                for x in legs:
                    x.stop = x.more_protective(be)
                acted = True
            # otherwise the open trade stays alone, like the chosen rules: a later breakout may still enter
            # the same day once it is closed
    for leg in legs:                                       # still open at the end of the data
        leg.close(bars[-1]["c"])
        out.append((leg.day, validate.net_r(leg.R, leg.risk / leg.entry * 100, cost), leg.kind))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reversal and reload when a box trade from an earlier day is open.")
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
    print("Capas de la caja de las 09:00: %s, %s a %s, costo %.3f%% por lado." % (", ".join(sorted(data)), cal[0], cal[-1], a.cost_pct))
    print("Control: el motor de siempre da n %d, %+.3fR por operacion." % (len(check), sum(check) / len(check) if check else 0))
    print("\n   %-50s %6s %8s %7s %7s %8s %7s %8s %8s" % ("version", "n", "R/op", "+-", "Sharpe", "R total", "caida", "1a mitad", "2a mitad"))
    rows = []
    for name, rev, rel in MODES:
        ts = []
        for sym, d in data.items():
            ts += [(day, r, kind, sym) for day, r, kind in simulate_symbol(d["bars"], d["ema"], p, rev, rel, a.cost_pct)]
        xs = [t[1] for t in ts]
        st = validate.tstats(xs) if xs else {"n": 0, "mean": 0.0, "se": 0.0}
        x = validate.daily([(t[0], t[1]) for t in ts], cal)
        h1 = [t[1] for t in ts if t[0] < mid]
        h2 = [t[1] for t in ts if t[0] >= mid]
        kinds = {}
        for t in ts:
            kinds.setdefault(t[2], []).append(t[1])
        row = {"name": name, "n": st["n"], "mean": st["mean"], "se": st["se"], "sharpe": perf.sharpe(x) * math.sqrt(252),
               "total": sum(xs), "dd": validate.max_dd(x), "h1": sum(h1) / len(h1) if h1 else 0.0,
               "h2": sum(h2) / len(h2) if h2 else 0.0,
               "kinds": {k: {"n": len(v), "mean": sum(v) / len(v)} for k, v in kinds.items()},
               "years": {y: (lambda v: sum(v) / len(v) if v else None)([t[1] for t in ts if t[0][:4] == y]) for y in years},
               "years_total": {y: sum(t[1] for t in ts if t[0][:4] == y) for y in years},
               "by_symbol": {s: (lambda v: sum(v) / len(v) if v else None)([t[1] for t in ts if t[3] == s]) for s in sorted(data)}}
        rows.append(row)
        print("   %-50s %6d %+8.3f %7.3f %+7.2f %+8.1f %7.1f %+8.3f %+8.3f" % (
            name[:50], row["n"], row["mean"], row["se"], row["sharpe"], row["total"], row["dd"], row["h1"], row["h2"]))
    print("\n   (R por capa despues de costos; Sharpe anual del resultado diario; R total = suma de 5 anos a igual riesgo por capa)")
    print("\nPor tipo de capa (n, R por capa):")
    for r in rows:
        print("   %-50s %s" % (r["name"][:50], " | ".join("%s: %d, %+.3fR" % (k, v["n"], v["mean"]) for k, v in sorted(r["kinds"].items()))))
    print("\nPor ano (R total del ano):")
    print("   %-50s %s" % ("", " ".join("%8s" % y for y in years)))
    for r in rows:
        print("   %-50s %s" % (r["name"][:50], " ".join("%+8.1f" % v for v in r["years_total"].values())))
    print("\nPor indice (R por capa):")
    for r in rows:
        print("   %-50s %s" % (r["name"][:50], "  ".join("%s %+.3f" % (s, v) for s, v in r["by_symbol"].items() if v is not None)))
    print("\n@@ORBLAYERS@@ " + json.dumps(validate.clean({"from": cal[0], "to": cal[-1], "check": len(check), "rows": rows}), separators=(",", ":")))
    print("\nInvestigacion, no asesoria. %.0fs." % (time.time() - t0), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
