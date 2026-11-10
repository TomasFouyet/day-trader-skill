#!/usr/bin/env python3
"""Alpaca market data, served in Yahoo's chart and spark shapes. Standard library only.

When ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are in the environment, the
pack's Yahoo readers (scan.py, watchlist_top.py, exit.py, timemachine.py,
premarket.py) ask Alpaca's market-data API first and fall back to Yahoo on
any error. Without the keys nothing changes. It only READS market data: it
never calls Alpaca's trading API (paper-api / api.alpaca.markets) and cannot
place an order.

    https://data.alpaca.markets/v2/stocks/{SYM}/bars      daily or minute bars of one stock
    https://data.alpaca.markets/v2/stocks/bars?symbols=   daily bars of up to 100 stocks

Daily bars use the consolidated "sip" feed (the free plan serves it 15
minutes delayed, which is all a daily bar needs; the request ends 16 minutes
ago). Minute bars for the pre-market use the free real-time "iex" feed, which
trades in extended hours but thinly. Prices are split-adjusted, like Yahoo's.

ALPACA_DATA_URL overrides the host (default https://data.alpaca.markets).
ALPACA_DISABLE=1 turns it off. STATS counts what each source served.
"""

import calendar
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DATA_URL = "https://data.alpaca.markets"
RANGES = {"1d": 1, "5d": 7, "1mo": 31, "60d": 62, "3mo": 93, "6mo": 186, "1y": 366, "2y": 731, "5y": 1827}
FRAMES = {"1d": "1Day", "1m": "1Min", "5m": "5Min"}
STATS = {"alpaca": 0, "fallback": 0, "errors": []}
UA = "day-trader-skill/2.0 (market data)"


def enabled():
    return bool(os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_API_SECRET_KEY")
                and not os.environ.get("ALPACA_DISABLE"))


def _http(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET_KEY", ""),
        "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(t):
    """Alpaca's RFC 3339 UTC time ("2026-11-09T05:00:00Z", maybe with fractions) -> epoch seconds."""
    return calendar.timegm(datetime.datetime.strptime(str(t)[:19], "%Y-%m-%dT%H:%M:%S").timetuple())


def _to_alpaca(sym):
    return urllib.parse.unquote(sym).upper().replace("-", ".")


def window(rng, interval, now_utc, prepost):
    """(timeframe, start, end, feed) for a Yahoo range and interval."""
    days = RANGES.get(rng)
    frame = FRAMES.get(interval)
    if days is None or frame is None:
        return None
    if interval == "1d":
        return frame, _iso(now_utc - datetime.timedelta(days=days)), _iso(now_utc - datetime.timedelta(minutes=16)), "sip"
    start = now_utc - datetime.timedelta(days=days if days > 1 else 1)
    if days == 1:          # "today": from 04:00 New York, the start of the pre-market (08:00 UTC covers both offsets)
        start = now_utc.replace(hour=8, minute=0, second=0) if now_utc.hour >= 8 else start
    return frame, _iso(start), None, ("iex" if prepost else "sip")


def bars(sym, frame, start, end, feed, http=None):
    http = http or _http
    out, token = [], None
    for _ in range(20):
        q = {"timeframe": frame, "start": start, "adjustment": "split", "feed": feed, "limit": 10000}
        if end:
            q["end"] = end
        if token:
            q["page_token"] = token
        doc = http("%s/v2/stocks/%s/bars?%s" % (os.environ.get("ALPACA_DATA_URL", DATA_URL),
                                                 urllib.parse.quote(sym), urllib.parse.urlencode(q)))
        out += doc.get("bars") or []
        token = doc.get("next_page_token")
        if not token:
            break
    return out


def multi_bars(syms, frame, start, end, feed, http=None):
    http = http or _http
    out, token = {}, None
    for _ in range(50):
        q = {"symbols": ",".join(syms), "timeframe": frame, "start": start, "adjustment": "split", "feed": feed,
             "limit": 10000}
        if end:
            q["end"] = end
        if token:
            q["page_token"] = token
        doc = http("%s/v2/stocks/bars?%s" % (os.environ.get("ALPACA_DATA_URL", DATA_URL), urllib.parse.urlencode(q)))
        for s, b in (doc.get("bars") or {}).items():
            out.setdefault(s, []).extend(b or [])
        token = doc.get("next_page_token")
        if not token:
            break
    return out


def chart_result(ysym, bs, interval, rng):
    """Alpaca bars -> one Yahoo v8 chart result."""
    ts = [_epoch(b["t"]) for b in bs]
    last = bs[-1] if bs else {}
    return {"meta": {"symbol": ysym, "dataGranularity": interval, "range": rng, "source": "alpaca",
                     "regularMarketPrice": last.get("c"), "regularMarketVolume": last.get("v"),
                     "regularMarketTime": ts[-1] + (86400 if interval == "1d" else 60) if ts else None},
            "timestamp": ts,
            "indicators": {"quote": [{"open": [b.get("o") for b in bs], "high": [b.get("h") for b in bs],
                                      "low": [b.get("l") for b in bs], "close": [b.get("c") for b in bs],
                                      "volume": [b.get("v") for b in bs]}]}}


CHART = re.compile(r"/v8/finance/chart/([^?]+)\?(.*)$")
SPARK = re.compile(r"/v7/finance/spark\?(.*)$")


def yahoo_like(path_or_url, now_utc=None, http=None):
    """A Yahoo chart or spark request -> the same JSON text from Alpaca, or None (not handled, no keys, or
    an error: the caller then asks Yahoo). Never raises."""
    if not enabled():
        return None
    now_utc = now_utc or _utc_now()
    path = re.sub(r"^https?://[^/]+", "", path_or_url)
    try:
        m = CHART.search(path)
        if m:
            q = dict(urllib.parse.parse_qsl(m.group(2)))
            ysym = urllib.parse.unquote(m.group(1)).upper()
            w = window(q.get("range"), q.get("interval"), now_utc, q.get("includePrePost") == "true")
            if not w:
                return None
            bs = bars(_to_alpaca(ysym), *w, http=http)
            if not bs:
                raise ValueError("sin velas %s %s" % (q.get("interval"), q.get("range")))
            STATS["alpaca"] += 1
            return json.dumps({"chart": {"result": [chart_result(ysym, bs, q.get("interval"), q.get("range"))],
                                         "error": None}})
        m = SPARK.search(path)
        if m:
            q = dict(urllib.parse.parse_qsl(m.group(1)))
            ysyms = [s for s in q.get("symbols", "").split(",") if s]
            w = window(q.get("range"), q.get("interval"), now_utc, False)
            if not w or not ysyms:
                return None
            got = multi_bars([_to_alpaca(s) for s in ysyms], *w, http=http)
            res = []
            for y in ysyms:
                bs = got.get(_to_alpaca(y))
                if bs:
                    res.append({"symbol": y, "response": [chart_result(y, bs, q.get("interval"), q.get("range"))]})
            STATS["alpaca"] += 1
            return json.dumps({"spark": {"result": res, "error": None}})
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        STATS["fallback"] += 1
        if len(STATS["errors"]) < 5:
            why = str(e) if isinstance(e, ValueError) else type(e).__name__ + (" %s" % getattr(e, "code", "")).rstrip()
            if why not in STATS["errors"]:
                STATS["errors"].append(why)
        return None
    return None
