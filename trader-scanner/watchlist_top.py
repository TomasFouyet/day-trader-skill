#!/usr/bin/env python3
"""Watchlist builder for trader-scanner: the most traded US stocks -> trader/watchlist.txt.
Standard library only.

    python3 watchlist_top.py                         # top 250 by 3-month average dollar volume
    python3 watchlist_top.py --top 100 --min-price 10 --min-dollar-volume 50000000
    python3 watchlist_top.py --exclude trader/watchlist_exclude.txt   # tickers your app cannot buy
    python3 watchlist_top.py --offline DIR --now "2026-10-30 17:00"   # no network (tests)

How:
  1. The universe: Nasdaq Trader's nasdaqlisted.txt and otherlisted.txt with
     scan.py's rule (common stocks, ordinary shares and ADRs; no ETFs, no
     test issues, no warrants, units, rights, notes or preferreds), and no
     "when-issued" lines (temporary tickers that change when the issue settles).
  2. Your exclusions: --exclude FILE (default trader/watchlist_exclude.txt
     when it exists), one ticker a line. Put there what Fintual (or your app)
     does not offer, so a rebuild does not bring it back.
  3. Stage 1, Yahoo spark (20 tickers a request, 5 days of daily closes):
     the last price and the latest session's volume. Out: under --min-price
     (5, the penny stocks). The --prefilter (750) biggest by price x volume
     go on. Spark has no volume history, so this stage only shortlists.
  4. Stage 2, one Yahoo v8 daily chart each (range=3mo, interval=1d): the
     3-month average of close x volume over complete sessions. Out: a last
     close under --min-price, an average under --min-dollar-volume (20
     million), fewer than 40 sessions (a new listing), or a last bar more than
     5 days old (halted or delisted).
  5. The --top (250) biggest averages, written one SYMBOL|Name|avg a line,
     the format scan.py reads.

--offline DIR reads nasdaqlisted.txt and otherlisted.txt from DIR and the v8
daily charts (<T>.json or .json.gz) from DIR too; stage 1 then uses the charts.
Yahoo is free and unofficial: a full build is about 40 spark requests and up
to --prefilter chart requests, with a polite pause between them.
A watchlist is a list of liquid stocks to scan, not a list of stocks to buy.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("trader_scan_for_watchlist", os.path.join(HERE, "scan.py"))
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

TOP = 250
PREFILTER = 750
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20000000.0
MIN_SESSIONS = 40
MAX_STALE_DAYS = 5
OUT = "trader/watchlist.txt"
EXCLUDE = "trader/watchlist_exclude.txt"
WHEN_ISSUED = re.compile(r"\bwhen[- ]issued\b", re.I)   # temporary tickers that change on settlement
NOTE = "A watchlist is a list of liquid stocks to scan, not a list of stocks to buy."


def read_exclude(path):
    if not path or not os.path.exists(path):
        return set()
    return set(scan.symbols_from_file(path))


def universe(offline=None, pause=scan.PAUSE):
    texts = {}
    for k, fname in enumerate(scan.NASDAQ_FILES):
        if offline:
            p = os.path.join(offline, fname)
            if not os.path.exists(p):
                raise scan.ScanError("no %s in %s" % (fname, offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        else:
            if k and pause:
                scan.SLEEP(pause)
            try:
                texts[fname] = scan.http_get(scan.NASDAQ_URL + fname, timeout=60)
            except Exception as e:   # urllib errors: say which host
                raise scan.ScanError("could not get %s from nasdaqtrader.com: %s" % (fname, getattr(e, "reason", e)))
    kept, counts, created = scan.build_universe(texts)
    return kept, counts


def last_price_volume(series, now_utc):
    closes = scan.daily_closes(series, now_utc)
    meta = series.get("meta") or {}
    px = meta.get("regularMarketPrice") or (closes[-1][1] if closes else None)
    vol = meta.get("regularMarketVolume") or 0
    return px, vol


def avg_dollar_volume(series, now_utc, sessions=63):
    """(average close x volume over the last `sessions` complete days, days, last close, last date)."""
    bars = scan.daily_bars(series, now_utc)
    if not bars:
        return None, 0, None, None
    use = bars[-sessions:]
    dv = sum(b["c"] * b["v"] for b in use) / len(use)
    return dv, len(use), bars[-1]["c"], bars[-1]["date"]


def rank(names, stage1, charts, now_utc, top=TOP, prefilter=PREFILTER, min_price=MIN_PRICE,
         min_dv=MIN_DOLLAR_VOLUME):
    """Pure. names {SYM: name}; stage1 / charts {SYM: series}. -> (rows, counts, shortlist)."""
    counts = {"universe": len(names), "no_data": 0, "penny": 0, "shortlisted": 0, "illiquid": 0, "new": 0,
              "stale": 0, "kept": 0}
    short = []
    for sym in names:
        s = stage1.get(sym)
        if not s:
            counts["no_data"] += 1
            continue
        px, vol = last_price_volume(s, now_utc)
        if not px or px < min_price:
            counts["penny"] += 1
            continue
        short.append((-(px * (vol or 0)), sym))
    short.sort()
    shortlist = [s for _, s in short[:prefilter]]
    counts["shortlisted"] = len(shortlist)
    today = scan.to_eastern(now_utc).date()
    rows = []
    for sym in shortlist:
        s = charts.get(sym)
        if not s:
            counts["no_data"] += 1
            continue
        dv, n, last, d = avg_dollar_volume(s, now_utc)
        if dv is None:
            counts["no_data"] += 1
            continue
        if n < MIN_SESSIONS:
            counts["new"] += 1
            continue
        if (today - d).days > MAX_STALE_DAYS:
            counts["stale"] += 1
            continue
        if last < min_price:
            counts["penny"] += 1
            continue
        if dv < min_dv:
            counts["illiquid"] += 1
            continue
        rows.append({"symbol": sym, "name": names.get(sym, ""), "avg_dollar_volume_3m": int(dv),
                     "last_close": round(last, 2), "sessions": n})
    rows.sort(key=lambda r: (-r["avg_dollar_volume_3m"], r["symbol"]))
    rows = rows[:top]
    counts["kept"] = len(rows)
    return rows, counts, shortlist


def write_watchlist(path, rows, now_utc, opts):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("# watchlist_top.py %s UTC: top %d US stocks by 3-month average dollar volume\n"
                 % (now_utc.strftime("%Y-%m-%d %H:%M"), len(rows)))
        fh.write("# filters: no ETFs, price >= $%g, avg dollar volume >= $%s. Remove what your app cannot buy\n"
                 % (opts["min_price"], "{:,.0f}".format(opts["min_dollar_volume"])))
        fh.write("# (or list it in trader/watchlist_exclude.txt). One SYMBOL|Name|avg $ a line.\n")
        for r in rows:
            fh.write("%s|%s|%d\n" % (r["symbol"], r["name"].replace("|", "/"), r["avg_dollar_volume_3m"]))
    os.replace(path + ".tmp", path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Top US stocks by 3-month average dollar volume -> trader/watchlist.txt")
    ap.add_argument("--top", type=int, default=TOP)
    ap.add_argument("--prefilter", type=int, default=PREFILTER, help="stage-1 shortlist sent to stage 2")
    ap.add_argument("--min-price", type=float, default=MIN_PRICE)
    ap.add_argument("--min-dollar-volume", type=float, default=MIN_DOLLAR_VOLUME)
    ap.add_argument("--exclude", default=EXCLUDE, help="tickers to leave out, one a line (default %s)" % EXCLUDE)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--offline", metavar="DIR", help="nasdaqlisted.txt, otherlisted.txt and <T>.json charts")
    ap.add_argument("--now", help='pretend it is this New York time ("2026-10-30 17:00")')
    ap.add_argument("--pause", type=float, default=scan.PAUSE)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    try:
        if not 1 <= a.top <= 2000 or a.prefilter < a.top:
            raise scan.ScanError("--top must be 1 to 2,000 and --prefilter at least --top")
        now = scan.parse_now(a.now)
        kept, ucounts = universe(a.offline, a.pause)
        excl = read_exclude(a.exclude)
        names = {s: n for s, n in kept if s not in excl and not WHEN_ISSUED.search(n or "")}

        def say(msg):
            if not a.quiet and not a.json:
                print(msg, file=sys.stderr)
        if a.offline:
            charts, _ = scan.load_offline(a.offline)
            stage1 = charts
        else:
            syms = sorted(names)
            say("Stage 1: spark closes for %s stocks..." % "{:,}".format(len(syms)))
            stage1, _ = scan.fetch_spark(syms, "5d", "1d", a.pause)
            _, _, shortlist = rank(names, stage1, {}, now, a.top, a.prefilter, a.min_price, a.min_dollar_volume)
            say("Stage 2: 3-month daily charts for %d stocks..." % len(shortlist))
            charts = {}
            for k, sym in enumerate(shortlist):
                if k and a.pause:
                    scan.SLEEP(a.pause)
                try:
                    charts[sym] = scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, "3mo", "1d")), sym)
                except scan.ScanError:
                    continue
                if (k + 1) % 100 == 0:
                    say("  %d of %d" % (k + 1, len(shortlist)))
        rows, counts, _ = rank(names, stage1, charts, now, a.top, a.prefilter, a.min_price, a.min_dollar_volume)
        counts["excluded"] = len([s for s, _ in kept if s in excl])
        counts["dropped_by_universe_rule"] = sum(v for k, v in ucounts.items() if k not in ("keep", "rows"))
        if not rows:
            raise scan.ScanError("no stock passed the filters: nothing written")
        if not a.no_write:
            write_watchlist(a.out, rows, now, {"min_price": a.min_price, "min_dollar_volume": a.min_dollar_volume})
    except scan.ScanError as e:
        print("watchlist_top.py: %s" % e, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"rows": rows, "counts": counts, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
        return 0
    print("Watchlist: %d stocks by 3-month average dollar volume (of %s in the universe, %d excluded by you)."
          % (len(rows), "{:,}".format(counts["universe"] + counts["excluded"]), counts["excluded"]))
    print("Out: %d penny (under $%g), %d illiquid, %d new listings, %d stale, %d no data."
          % (counts["penny"], a.min_price, counts["illiquid"], counts["new"], counts["stale"], counts["no_data"]))
    for r in rows[:10]:
        print("  %-6s $%14s a day  %s" % (r["symbol"], "{:,}".format(r["avg_dollar_volume_3m"]), r["name"][:50]))
    if len(rows) > 10:
        print("  ... and %d more." % (len(rows) - 10))
    if not a.no_write:
        print("Wrote %s. Remove what your app cannot buy, or list it in %s." % (a.out, EXCLUDE))
    print(NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
