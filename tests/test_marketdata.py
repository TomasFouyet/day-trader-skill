"""trader-scanner/marketdata.py: Alpaca's market-data API served in Yahoo's shapes. No network:
a fake Alpaca built from the real Yahoo fixtures answers, so the same bars must give the same
setups, plans and odds through either source."""

import datetime
import gzip
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse

from _load import load, FIX

md = load("trader-scanner", "marketdata")
scan = load("trader-scanner", "scan")
tm = load("trader-timemachine", "timemachine")
pm = load("trader-alerts", "premarket")

SWDIR = os.path.join(FIX, "trader-scanner", "swing")
PRE = os.path.join(FIX, "trader-alerts", "premarket")


def yahoo(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)["chart"]["result"][0]


def to_alpaca(res, daily=True):
    """A Yahoo chart result -> Alpaca bars (daily bars stamped at midnight New York, like Alpaca's)."""
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        if q["close"][i] is None:
            continue
        utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=ts)
        if daily:
            et = tm.to_eastern(utc).date()
            off = tm.eastern_offset(utc)
            t = datetime.datetime.combine(et, datetime.time(0, 0)) - datetime.timedelta(hours=off)
        else:
            t = utc
        out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                    "c": q["close"][i], "v": q["volume"][i] or 0})
    return out


class FakeAlpaca(object):
    def __init__(self, daily=None, minute=None, fail=False):
        self.daily, self.minute, self.fail, self.urls = daily or {}, minute or {}, fail, []

    def __call__(self, url, timeout=30):
        self.urls.append(url)
        if self.fail:
            raise urllib.error.URLError("down")
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        src = self.daily if q["timeframe"] == "1Day" else self.minute
        end = q.get("end")

        def clip(bs):
            return [b for b in bs if q["start"] <= b["t"] and (not end or b["t"] <= end)]
        if u.path == "/v2/stocks/bars":
            return {"bars": {s: clip(src.get(s, [])) for s in q["symbols"].split(",") if src.get(s)},
                    "next_page_token": None}
        sym = urllib.parse.unquote(u.path.split("/")[3])
        bs = clip(src.get(sym, []))
        half = len(bs) // 2                    # two pages, to exercise page_token
        if "page_token" not in q and half:
            return {"bars": bs[:half], "next_page_token": "p2"}
        return {"bars": bs[half:] if "page_token" in q else bs, "next_page_token": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_DISABLE")}
        os.environ.update(ALPACA_API_KEY_ID="PKTEST", ALPACA_API_SECRET_KEY="secret")
        os.environ.pop("ALPACA_DISABLE", None)
        self.daily = {f.split(".")[0]: to_alpaca(yahoo(os.path.join(SWDIR, f))) for f in os.listdir(SWDIR)}
        self.fake = FakeAlpaca(self.daily, {"MSFT": to_alpaca(yahoo(os.path.join(PRE, "MSFT.json.gz")), False)})
        self.saved = md._http
        md._http = self.fake
        shared = scan._marketdata()            # the instance the tools share
        self.saved_shared = shared._http
        shared._http = self.fake

    def tearDown(self):
        md._http = self.saved
        scan._marketdata()._http = self.saved_shared
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Adapter(Base):
    def test_off_without_keys(self):
        os.environ.pop("ALPACA_API_KEY_ID")
        self.assertIsNone(md.yahoo_like("/v8/finance/chart/AAPL?range=1y&interval=1d"))
        os.environ["ALPACA_API_KEY_ID"] = "x"
        os.environ["ALPACA_DISABLE"] = "1"
        self.assertIsNone(md.yahoo_like("/v8/finance/chart/AAPL?range=1y&interval=1d"))

    def test_daily_chart_same_bars_as_yahoo(self):
        now = md._utc_now().replace(year=2026, month=10, day=31, hour=12)
        text = md.yahoo_like("https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1y&interval=1d", now)
        a = scan.daily_bars(scan.parse_chart(text, "AAPL"), scan.parse_now("2026-10-30 17:00"))
        y = scan.daily_bars(scan.parse_chart(json.dumps({"chart": {"result": [yahoo(os.path.join(SWDIR, "AAPL.json.gz"))]}}),
                                             "AAPL"), scan.parse_now("2026-10-30 17:00"))
        self.assertEqual([(b["date"], b["c"], b["h"], b["l"]) for b in a][-200:],
                         [(b["date"], b["c"], b["h"], b["l"]) for b in y][-200:])
        url = self.fake.urls[0]
        self.assertTrue(url.startswith("https://data.alpaca.markets/v2/stocks/AAPL/bars?"))
        self.assertIn("feed=sip", url)
        self.assertIn("adjustment=split", url)
        self.assertIn("timeframe=1Day", url)
        self.assertIn("page_token=p2", self.fake.urls[1])
        self.assertEqual(md.STATS["alpaca"] >= 1, True)

    def test_spark_and_symbols_with_a_dot(self):
        now = md._utc_now().replace(year=2026, month=10, day=31)
        self.daily["BRK.B"] = self.daily["KO"]
        text = md.yahoo_like("/v7/finance/spark?symbols=KO,BRK-B,ZZZZ&range=5d&interval=1d", now)
        got = scan.parse_spark(text, {"KO": "KO", "BRK-B": "BRK.B"})
        self.assertEqual(sorted(got), ["BRK.B", "KO"])
        self.assertIn("symbols=KO%2CBRK.B%2CZZZZ", self.fake.urls[-1])

    def test_errors_fall_back(self):
        self.fake.fail = True
        before = md.STATS["fallback"]
        self.assertIsNone(md.yahoo_like("/v8/finance/chart/AAPL?range=1y&interval=1d"))
        self.assertEqual(md.STATS["fallback"], before + 1)
        self.fake.fail = False
        self.assertIsNone(md.yahoo_like("/v8/finance/chart/NOPE?range=1y&interval=1d"))   # no bars: Yahoo


class ThroughTheTools(Base):
    def test_scanner_finds_the_same_setups(self):
        calls = []
        saved = scan.http_get
        scan.http_get = lambda url, timeout=20: calls.append(url) or (_ for _ in ()).throw(AssertionError("yahoo"))
        try:
            d, _ = scan.load_offline(SWDIR)
            setups, p, floors = scan.swing_params({})
            yahoo_c, _, _ = scan.scan_swing(d, sorted(d), scan.parse_now("2026-10-30 17:00"), setups, p, floors)
            data, failed = scan.fetch_charts(sorted(d), 0)
            self.assertEqual(failed, {})
            alp_c, _, _ = scan.scan_swing(data, sorted(d), scan.parse_now("2026-10-30 17:00"), setups, p, floors)
        finally:
            scan.http_get = saved
        self.assertEqual(calls, [])                                      # Yahoo never asked
        self.assertEqual([(c["symbol"], c["setup"], c["price"]) for c in alp_c],
                         [(c["symbol"], c["setup"], c["price"]) for c in yahoo_c])

    def test_time_machine_daily_and_premarket(self):
        text = tm.fetch_daily("TSLA")
        self.assertIn('"source": "alpaca"', text)
        _, bars = tm.parse_daily(tm.chart_result(text, "TSLA"), tm.parse_now("2026-10-30 17:00"))
        self.assertEqual(bars[-1]["date"], "2026-10-30")
        now = pm.parse_now("2026-11-09 09:05")
        text = md.yahoo_like("/v8/finance/chart/MSFT?range=1d&interval=1m&includePrePost=true", now)
        p, at, phase = pm.latest_price(text, "MSFT", now)
        self.assertEqual((round(p, 2), at, phase), (542.07, "09:03", "pre"))
        self.assertIn("feed=iex", self.fake.urls[-1])


if __name__ == "__main__":
    unittest.main()
