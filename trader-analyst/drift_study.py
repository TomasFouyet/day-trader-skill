#!/usr/bin/env python3
"""Research: does a stock keep beating the market after a strong earnings reaction? Standard library only.

    python3 drift_study.py --symbols trader-scanner/watchlist.example.txt     # SEC + Yahoo, prints tables
    python3 drift_study.py --offline DIR                                      # sec/<SYM>.json + prices/<SYM>.json

Earnings days are the SEC filing times of each company's 8-K with item 2.02 (results of
operations), from data.sec.gov/submissions (the "recent" block and the older pages). The
market learns the result: before 9:30 New York, that same day; after 16:00, the next trading day;
in between, that day. Reaction = that day's close over the previous close, minus SPY's.

Then, for each event: the return over SPY from the reaction day's close to 5, 20, 40 and 60 trading
days later, and the same event as a long trade measured in R (stop K x 14-day ATR under the entry,
checked first; a gap through it fills at the open; no target; closed after H days), next to a
baseline of random entry days in the same stocks (one every 10 trading days). Prices: Yahoo daily
bars from 2005 (period1/period2: range=max gives monthly bars).

The SEC asks for a User-Agent with a name and an email: SEC_USER_AGENT. It prints aggregates only
(counts and averages): nothing about any account. Research, not advice: a past average is not a
promise, and the stocks are today's list (survivorship bias).
"""

import argparse
import datetime
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBS = "https://data.sec.gov/submissions/%s"
YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/%s?period1=%d&period2=%d&interval=1d"
START = datetime.datetime(2005, 1, 1)
HS = (5, 20, 40, 60)
ERAS = (("2005-2009", "2005", "2010"), ("2010-2014", "2010", "2015"), ("2015-2019", "2015", "2020"),
        ("2020-2022", "2020", "2023"), ("2023-2026", "2023", "2030"))
BUCKETS = ((-9, -.10), (-.10, -.05), (-.05, -.02), (-.02, .02), (.02, .05), (.05, .10), (.10, 9))


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def to_eastern(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return utc + datetime.timedelta(hours=-4 if start <= utc < end else -5)


# ---------------------------------------------------------------- data

class Net(object):
    def __init__(self, ua, offline=None, pause=0.12):
        self.ua, self.offline, self.pause, self.last = ua, offline, pause, 0.0

    def get(self, url, ua=None):
        gap = time.time() - self.last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self.last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": ua or self.ua, "Accept": "application/json"})
        for attempt in (0, 1, 2):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise


def _hm(filing_date, stamp):
    """The acceptance time as 'HH:MM' on the filing date (None if unknown). EDGAR's stamp is New York
    wall-clock time in most records; when its date runs past the filing date (a UTC stamp of an
    evening filing) the release came after the close, and when it is before the filing date (an
    after-hours filing dated the next business day) it came before that day's open."""
    if len(stamp) < 16:
        return None
    day = stamp[:10]
    if day > filing_date:
        return "23:59"
    if day < filing_date:
        return "00:00"
    return stamp[11:16]


def earnings_times(subs_pages):
    """[submissions page dicts] -> sorted [(filing date, acceptance 'HH:MM' New York or None)] of 8-K item 2.02."""
    out = set()
    for page in subs_pages:
        rec = page.get("filings", {}).get("recent", page) if "filings" in page else page
        forms, dates = rec.get("form", []), rec.get("filingDate", [])
        items, acc = rec.get("items", []), rec.get("acceptanceDateTime", [])
        for i, f in enumerate(forms):
            if f in ("8-K", "8-K/A") and "2.02" in (items[i] if i < len(items) else "" or ""):
                t = acc[i] if i < len(acc) else ""
                out.add((dates[i], _hm(dates[i], t)))
    return sorted(out)


def parse_prices(doc):
    r = doc["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = q["close"][i]
        if c is None:
            continue
        d = to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t)).date().isoformat()
        out.append({"date": d, "o": q["open"][i] or c, "h": q["high"][i] or c, "l": q["low"][i] or c, "c": c})
    return out


def atr_series(bars, n=14):
    out = [None] * len(bars)
    trs = [None]
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
        if i >= n:
            out[i] = sum(trs[i - n + 1:i + 1]) / n
    return out


def walk(bars, i, H, K, atr):
    """Long from bars[i]'s close, stop K x ATR (checked first, gaps at the open), no target, out after H days. -> R."""
    e = bars[i]["c"]
    risk = K * atr
    stop = e - risk
    c = e
    for b in bars[i + 1:i + 1 + H]:
        if b["o"] <= stop:
            return (b["o"] - e) / risk
        if b["l"] <= stop:
            return -1.0
        c = b["c"]
    return (c - e) / risk


def reaction_index(bars, date, hm):
    """The index of the first session that traded on the news."""
    after_close = hm is not None and hm >= "16:00"
    for k, b in enumerate(bars):
        if b["date"] > date or (b["date"] == date and not after_close):
            return k
    return None


def hour_class(hm):
    return "antes 09:30" if hm and hm < "09:30" else "despues 16:00" if hm and hm >= "16:00" else "en sesion/sin hora"


def timing_check(bars, times, spy, acc):
    """A clock check that needs no time zone: the mean absolute move over SPY on the filing date and
    on the next session, per hour class. The reaction day is the one with the big move."""
    pos = {b["date"]: k for k, b in enumerate(bars)}
    for date, hm in times:
        k = pos.get(date)
        if k is None or k < 1 or k + 1 >= len(bars):
            continue
        moves = []
        for j in (k, k + 1):
            d, dp = bars[j]["date"], bars[j - 1]["date"]
            if d not in spy or dp not in spy:
                break
            moves.append(abs(bars[j]["c"] / bars[j - 1]["c"] - spy[d] / spy[dp]))
        if len(moves) == 2:
            a = acc.setdefault(hour_class(hm), [0, 0.0, 0.0])
            a[0] += 1
            a[1] += moves[0]
            a[2] += moves[1]


def events_for(sym, bars, times, spy):
    if len(bars) < 80:
        return []
    atr = atr_series(bars)
    out, seen = [], set()
    for date, hm in times:
        k = reaction_index(bars, date, hm)
        if k is None or k < 20 or k + 60 >= len(bars) or k in seen or not atr[k]:
            continue
        seen.add(k)
        out.append(row(sym, bars, k, atr[k], spy))
    return [r for r in out if r]


def row(sym, bars, k, atr, spy):
    d0, dp = bars[k]["date"], bars[k - 1]["date"]
    if d0 not in spy or dp not in spy:
        return None
    r = {"sym": sym, "date": d0, "r0": bars[k]["c"] / bars[k - 1]["c"] - 1 - (spy[d0] / spy[dp] - 1)}
    for h in HS:
        dh = bars[k + h]["date"]
        if dh not in spy:
            return None
        r["x%d" % h] = bars[k + h]["c"] / bars[k]["c"] - 1 - (spy[dh] / spy[d0] - 1)
    r["R20"] = walk(bars, k, 20, 2.0, atr)
    r["R60"] = walk(bars, k, 60, 3.0, atr)
    return r


def baseline(sym, bars, spy, rng):
    atr = atr_series(bars)
    out = []
    for k in range(20 + rng.randrange(10), len(bars) - 61, 10):
        if atr[k]:
            r = row(sym, bars, k, atr[k], spy)
            if r:
                out.append(r)
    return out


# ---------------------------------------------------------------- report

def _m(xs):
    return statistics.mean(xs) if xs else float("nan")


def report(events, base):
    lines = ["Eventos de resultados (8-K 2.02): %d en %d acciones; base al azar: %d" % (
        len(events), len({e["sym"] for e in events}), len(base)), ""]
    lines.append("Rendimiento sobre el S&P 500 despues del dia de reaccion (promedio; mediana a 60d; % que gana a SPY a 60d)")
    lines.append("%-16s %6s %8s %8s %8s %8s %9s %6s" % ("reaccion vs SPY", "n", "5d", "20d", "40d", "60d", "med60d", "gana"))
    for lo, hi in BUCKETS:
        s = [e for e in events if lo <= e["r0"] < hi]
        if s:
            lines.append("%-16s %6d %+7.2f%% %+7.2f%% %+7.2f%% %+7.2f%% %+8.2f%% %5.0f%%" % (
                "%+.0f%% a %+.0f%%" % (100 * max(lo, -.99), 100 * min(hi, .99)), len(s),
                *[100 * _m([e["x%d" % h] for e in s]) for h in HS], 100 * statistics.median(e["x60"] for e in s),
                100 * sum(1 for e in s if e["x60"] > 0) / len(s)))
    lines.append("%-16s %6d %+7.2f%% %+7.2f%% %+7.2f%% %+7.2f%% %+8.2f%% %5.0f%%" % (
        "AZAR", len(base), *[100 * _m([e["x%d" % h] for e in base]) for h in HS],
        100 * statistics.median(e["x60"] for e in base), 100 * sum(1 for e in base if e["x60"] > 0) / len(base)))
    lines.append("")
    lines.append("Regla long (R): 20 dias stop 2 ATR / 60 dias stop 3 ATR, sin objetivo; ventaja = regla - azar")
    for thr in (0.05, 0.08, 0.12):
        s = [e for e in events if e["r0"] >= thr]
        if not s:
            continue
        for key in ("R20", "R60"):
            rs = [e[key] for e in s]
            rb = [e[key] for e in base]
            lines.append("  reaccion>=%+3.0f%% %s: n%5d exp%+.3f (+-%.3f) win%4.1f%% | azar %+.3f | ventaja %+.3fR" % (
                100 * thr, key, len(rs), _m(rs), statistics.pstdev(rs) / len(rs) ** 0.5,
                100 * sum(1 for x in rs if x > 0) / len(rs), _m(rb), _m(rs) - _m(rb)))
    lines.append("")
    lines.append("Por epoca, reaccion >= +8% y >= +12% (sobre SPY a 60d, y ventaja en R a 60d frente al azar)")
    for name, a0, a1 in ERAS:
        bs = [e for e in base if a0 <= e["date"][:4] < a1]
        if not bs:
            continue
        parts = []
        for thr in (0.08, 0.12):
            s = [e for e in events if e["r0"] >= thr and a0 <= e["date"][:4] < a1]
            if s:
                parts.append(">=%+.0f%% n%4d SPY60 %+6.2f%% (azar %+5.2f%%) ventaja %+.2fR" % (
                    100 * thr, len(s), 100 * _m([e["x60"] for e in s]), 100 * _m([e["x60"] for e in bs]),
                    _m([e["R60"] for e in s]) - _m([e["R60"] for e in bs])))
        lines.append("  %-10s %s" % (name, " | ".join(parts)))
    lines.append("")
    lines.append("Controles (si dan parecido, la ventaja no es de los resultados sino de volatilidad o supervivencia)")
    rb60, rb20, xb = _m([e["R60"] for e in base]), _m([e["R20"] for e in base]), _m([e["x60"] for e in base])
    controls = (("subida >= +12% con 8-K", events, lambda r: r >= .12),
                ("caida  <= -12% con 8-K", events, lambda r: r <= -.12),
                ("caida  <=  -8% con 8-K", events, lambda r: r <= -.08),
                ("subida >= +12% sin 8-K", base, lambda r: r >= .12),
                ("subida >=  +8% sin 8-K", base, lambda r: r >= .08))
    for label, pool, cond in controls:
        s = [e for e in pool if cond(e["r0"])]
        if s:
            lines.append("  %-24s n%5d SPY20 %+6.2f%% SPY60 %+6.2f%% (azar %+5.2f%%) R20 %+.3f R60 %+.3f ventaja R60 %+.3fR" % (
                label, len(s), 100 * _m([e["x20"] for e in s]), 100 * _m([e["x60"] for e in s]), 100 * xb,
                _m([e["R20"] for e in s]), _m([e["R60"] for e in s]), _m([e["R60"] for e in s]) - rb60))
    return lines


# ---------------------------------------------------------------- main

def symbols_from(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Earnings-reaction drift study (research).")
    ap.add_argument("--symbols", default="trader-scanner/watchlist.example.txt")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offline", metavar="DIR", help="DIR/sec/<SYM>.json (submissions) and DIR/prices/<SYM>.json")
    ap.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"))
    a = ap.parse_args(argv)
    syms = symbols_from(a.symbols)
    if a.limit:
        syms = syms[:a.limit]
    rng = random.Random(11)
    end = int(time.time())
    if a.offline:
        def subs(sym):
            p = os.path.join(a.offline, "sec", sym + ".json")
            return [json.load(open(p))] if os.path.exists(p) else []

        def prices(sym):
            return parse_prices(json.load(open(os.path.join(a.offline, "prices", sym + ".json"))))
    else:
        if not a.user_agent or "@" not in a.user_agent:
            print("drift_study.py: set SEC_USER_AGENT (\"Your Name you@example.com\")", file=sys.stderr)
            return 2
        net = Net(a.user_agent)
        tickers = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in net.get(SEC_TICKERS).values()}

        def subs(sym):
            cik = tickers.get(sym.replace(".", "-")) or tickers.get(sym)
            if not cik:
                return []
            first = net.get(SEC_SUBS % ("CIK%s.json" % cik))
            pages = [first]
            for f in first.get("filings", {}).get("files", []):
                pages.append(net.get(SEC_SUBS % f["name"]))
            return pages

        yahoo = Net("Mozilla/5.0", pause=0.3)

        def prices(sym):
            return parse_prices(yahoo.get(YAHOO % (sym.replace(".", "-"), int(START.timestamp()), end)))
    spy_bars = prices("SPY")
    spy = {b["date"]: b["c"] for b in spy_bars}
    events, base, missing = [], [], 0
    hours, clock = {}, {}
    for n, sym in enumerate(syms):
        try:
            pages = subs(sym)
            times = earnings_times(pages)
            if not times:
                missing += 1
                continue
            bars = prices(sym)
        except Exception as e:
            missing += 1
            print("  %s: %s" % (sym, type(e).__name__), file=sys.stderr)
            continue
        for _, hm in times:
            hours[hour_class(hm)] = hours.get(hour_class(hm), 0) + 1
        timing_check(bars, times, spy, clock)
        events += events_for(sym, bars, times, spy)
        base += baseline(sym, bars, spy, rng)
        if (n + 1) % 25 == 0:
            print("  %d/%d acciones, %d eventos" % (n + 1, len(syms), len(events)), file=sys.stderr)
    print("Acciones sin datos del SEC (ETF, extranjeras) o con error: %d de %d" % (missing, len(syms)))
    print("Hora de publicacion de los 8-K 2.02: %s" % ", ".join("%s %d" % kv for kv in sorted(hours.items())))
    for key, (n_, d0, d1) in sorted(clock.items()):
        print("  chequeo de hora, %s: movimiento medio vs SPY el dia del 8-K %.2f%%, al dia siguiente %.2f%% (n %d)" % (
            key, 100 * d0 / n_, 100 * d1 / n_, n_))
    if not events or not base:
        print("Sin eventos: nada que medir.")
        return 1
    print("\n".join(report(events, base)))
    print("\nInvestigacion, no asesoria: promedios pasados, acciones de la lista de hoy (sesgo de supervivencia).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
