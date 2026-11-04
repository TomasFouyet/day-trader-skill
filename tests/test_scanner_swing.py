"""scan.py swing: multi-day setups on daily bars. Offline: 15 real Yahoo v8
daily charts (range=1y interval=1d) saved up to Fri 2026-10-30."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")
tm = load("trader-timemachine", "timemachine")

SWDIR = os.path.join(FIX, "trader-scanner", "swing")
NOW = "2026-10-30 17:00"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def data():
    d, failed = scan.load_offline(SWDIR)
    assert not failed
    return d


class Params(unittest.TestCase):
    def test_defaults_and_config(self):
        setups, p, floors = scan.swing_params({})
        self.assertEqual(setups, ["breakout20", "pullback_ema20", "rsi2"])
        self.assertEqual(p, tm.SWING_PARAMS)           # the same rules as the Time Machine's history
        self.assertEqual(floors, {"min_price": 5.0, "min_dollar_volume": 20000000.0})
        setups, p, floors = scan.swing_params({"swing": {"setups": "rsi2, breakout20",
                                                         "setup_params": {"rsi_max": 5, "breakout_days": 55},
                                                         "min_price": 10}})
        self.assertEqual(setups, ["rsi2", "breakout20"])
        self.assertEqual((p["rsi_max"], p["breakout_days"], floors["min_price"]), (5.0, 55, 10.0))

    def test_bad_config(self):
        for bad in ({"setups": ["cup_handle"]}, {"setups": []}, {"setup_params": {"nope": 1}},
                    {"setup_params": {"rsi_max": "x"}}, {"setup_params": {"ema_days": 0}}, {"min_price": "x"}):
            with self.assertRaises(scan.ScanError, msg=str(bad)):
                scan.swing_params({"swing": bad})


class SameRulesAsTheTimeMachine(unittest.TestCase):
    def test_every_bar_of_every_chart_agrees(self):
        now = scan.parse_now(NOW)
        fired = {s: 0 for s in scan.SWING_SETUPS}
        for sym, series in data().items():
            bars = scan.daily_bars(series, now)
            ind_s = scan.swing_indicators(bars, scan.SWING_PARAMS)
            ind_t = tm.indicators(bars)
            for i in range(len(bars)):
                for s in scan.SWING_SETUPS:
                    a = scan.swing_fires(s, bars, i, ind_s, scan.SWING_PARAMS)
                    self.assertEqual(a, tm.setup_fires(s, bars, i, ind_t), (sym, i, s))
                    fired[s] += a
                    if a and i >= 21:
                        closes = [b["c"] for b in bars[:i + 1]]
                        self.assertTrue(scan.swing_prefilter(closes, [s], scan.SWING_PARAMS), (sym, i, s))
        for s, n in fired.items():
            self.assertGreater(n, 5, s)     # every setup fires somewhere in a year of 15 charts


class Scan(unittest.TestCase):
    def test_the_candidates_on_2026_10_30(self):
        setups, p, floors = scan.swing_params({})
        d = data()
        cands, counts, last = scan.scan_swing(d, sorted(d), scan.parse_now(NOW), setups, p, floors)
        self.assertEqual(last.isoformat(), "2026-10-30")
        self.assertEqual([(c["symbol"], c["setup"]) for c in cands],
                         [("WMT", "breakout20"), ("XOM", "breakout20"), ("AAPL", "pullback_ema20"),
                          ("NVDA", "rsi2"), ("MSFT", "rsi2"), ("PLTR", "rsi2")])
        self.assertEqual((counts["scanned"], counts["hits"], counts["illiquid"]), (15, 6, 0))
        a = [c for c in cands if c["symbol"] == "AAPL"][0]
        self.assertEqual((a["date"], a["price"]), ("2026-10-30", 344.63))
        self.assertGreaterEqual(a["price"], a["ema20"])           # closed at or above the EMA20
        self.assertGreater(a["price"], a["sma50"])                 # in an uptrend
        n = [c for c in cands if c["symbol"] == "NVDA"][0]
        self.assertLess(n["rsi2"], 10)
        self.assertGreater(n["price"], n["sma200"])
        w = [c for c in cands if c["symbol"] == "WMT"][0]
        self.assertGreater(w["price"], w["high20"])
        self.assertIsInstance(w["daily_tags"], list)             # the daily scan's tags, reused
        self.assertIn("breakout_20d", w["daily_tags"])

    def test_todays_bar_waits_for_the_close_and_floors(self):
        d = data()
        setups, p, floors = scan.swing_params({})
        _, _, last = scan.scan_swing(d, sorted(d), scan.parse_now("2026-10-30 15:00"), setups, p, floors)
        self.assertEqual(last.isoformat(), "2026-10-29")
        cands, counts, _ = scan.scan_swing(d, sorted(d), scan.parse_now(NOW), setups, p,
                                           {"min_price": 300, "min_dollar_volume": 0})
        self.assertTrue(all(c["price"] >= 300 for c in cands))
        self.assertGreater(counts["illiquid"], 0)
        spark = {"X": {"t": [1], "c": [1.0], "o": None, "h": None, "l": None, "v": None, "meta": {}}}
        _, counts, _ = scan.scan_swing(spark, ["X"], scan.parse_now(NOW), setups, p, floors)
        self.assertEqual(counts["no_ohlc"], 1)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_writes_swing_json(self):
        code, text, err = run(["swing", "--offline", SWDIR, "--now", NOW, "--quiet"])
        self.assertEqual(code, 0, err)
        self.assertIn("Scanner swing", text)
        self.assertIn("no una lista de acciones para comprar", text)
        with open(os.path.join("trader", "swing.json")) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["mode"], doc["as_of"], len(doc["candidates"])), ("swing", "2026-10-30 close", 6))
        self.assertEqual(doc["setup_params"], tm.SWING_PARAMS)
        self.assertFalse(os.path.exists(os.path.join("trader", "watchlist.json")))   # the daily scan's file is left alone

    def test_watchlist_txt_limits_the_scan(self):
        with open(os.path.join("trader", "watchlist.txt"), "w") as fh:
            fh.write("# my list\nAAPL\nKO\nnvda\n")
        code, text, err = run(["swing", "--offline", SWDIR, "--now", NOW, "--quiet", "--json", "--no-write"])
        doc = json.loads(text)
        self.assertEqual(doc["universe"], 3)
        self.assertEqual([c["symbol"] for c in doc["candidates"]], ["AAPL", "NVDA"])
        code, text, _ = run(["swing", "--offline", SWDIR, "--now", NOW, "--json", "--no-write", "--symbols", "KO"])
        self.assertEqual(code, 1)          # nothing fired on KO
        self.assertEqual(json.loads(text)["universe"], 1)

    def test_setups_flag_and_config(self):
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"swing": {"setups": ["breakout20"]}}, fh)
        code, text, _ = run(["swing", "--offline", SWDIR, "--now", NOW, "--json", "--no-write"])
        self.assertEqual({c["setup"] for c in json.loads(text)["candidates"]}, {"breakout20"})
        code, text, _ = run(["swing", "--offline", SWDIR, "--now", NOW, "--json", "--no-write", "--setups", "rsi2"])
        self.assertEqual([c["symbol"] for c in json.loads(text)["candidates"]], ["NVDA", "MSFT", "PLTR"])
        code, _, err = run(["swing", "--offline", SWDIR, "--now", NOW, "--setups", "bogus"])
        self.assertEqual(code, 1)
        self.assertIn("unknown setup", err)

    def test_network_paths(self):
        texts = {}
        for f in os.listdir(SWDIR):
            texts[f.split(".")[0]] = scan._read_maybe_gz(os.path.join(SWDIR, f))
        calls = []

        def fake(url, timeout=20):
            calls.append(url)
            sym = url.split("/chart/")[1].split("?")[0]
            return texts[sym]
        saved, sleep = scan.http_get, scan.SLEEP
        scan.http_get, scan.SLEEP = fake, (lambda s: None)
        try:
            code, text, err = run(["swing", "--symbols", "AAPL,WMT", "--now", NOW, "--json", "--no-write"])
        finally:
            scan.http_get, scan.SLEEP = saved, sleep
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, ["https://query2.finance.yahoo.com/v8/finance/chart/AAPL?range=1y&interval=1d",
                                 "https://query2.finance.yahoo.com/v8/finance/chart/WMT?range=1y&interval=1d"])
        self.assertEqual([c["symbol"] for c in json.loads(text)["candidates"]], ["WMT", "AAPL"])


if __name__ == "__main__":
    unittest.main()
