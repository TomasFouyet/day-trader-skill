"""trader-scanner/sweep.py: the liquidity sweep -> MSS -> FVG entry scanner on 30-minute bars.

The fixtures are Yahoo's real 30-minute charts of NOW, APP and SPCX to Fri 2026-11-13, the three
charts the rules were fitted to: the TradingView indicator showed NOW open short from 102.10 with
the stop at 103.85 and the target at 98.60 (-0.1 R), APP swept down waiting for its MSS under
398.45, and SPCX swept up waiting for its MSS over 142.24."""

import datetime
import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

sw = load("trader-scanner", "sweep")
DIR = os.path.join(FIX, "trader-scanner", "sweep")
NOW = "2026-11-13 16:05"


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sw.main(argv)
    return code, out.getvalue(), err.getvalue()


def bars_of(sym, now=NOW):
    series, _ = sw.scan.load_offline(DIR)
    return sw.session_bars(series[sym], sw.scan.parse_now(now))


class TheThreeCharts(unittest.TestCase):
    def test_now_is_open_short_at_the_indicators_levels(self):
        s = sw.run(bars_of("NOW"))["state"]
        self.assertEqual((s["stage"], s["dir"]), ("open", -1))
        self.assertAlmostEqual(s["level"], 103.78, places=2)        # the swing high that was swept
        self.assertAlmostEqual(s["mss_level"], 101.50, places=2)    # the internal low that had to break
        self.assertAlmostEqual(s["entry"], 102.10, places=2)        # the near edge of the gap
        self.assertAlmostEqual(s["stop"], 103.85, places=2)         # the sweep bar's high
        self.assertAlmostEqual(s["target"], 98.60, places=2)        # 2R
        self.assertEqual(s["R"], -0.1)
        self.assertEqual((s["sweep_time"], s["mss_time"], s["entry_time"]),
                         ("2026-11-09 13:00", "2026-11-10 10:00", "2026-11-11 11:00"))

    def test_app_and_spcx_are_swept_and_waiting(self):
        a = sw.run(bars_of("APP"))["state"]
        self.assertEqual((a["stage"], a["dir"], a["sweep_time"], a["bars"]), ("swept", -1, "2026-11-13 10:00", 11))
        self.assertAlmostEqual(a["level"], 408.99, places=2)
        self.assertAlmostEqual(a["mss_level"], 398.45, places=2)
        s = sw.run(bars_of("SPCX"))["state"]
        self.assertEqual((s["stage"], s["dir"], s["sweep_time"]), ("swept", 1, "2026-11-13 11:00"))
        self.assertAlmostEqual(s["level"], 137.75, places=2)
        self.assertAlmostEqual(s["mss_level"], 142.24, places=2)

    def test_no_look_ahead(self):
        bars = bars_of("NOW")
        full = sw.run(bars)["trades"]
        cut = sw.run(bars[:500])["trades"]
        done = [t for t in full if t["exit_i"] < 500]
        self.assertEqual(cut, done)                 # the past does not change when the future arrives
        self.assertGreaterEqual(len(full), 2)

    def test_the_bar_in_progress_is_left_out(self):
        self.assertEqual(bars_of("NOW", "2026-11-13 15:40")[-1]["t"].strftime("%H:%M"), "15:00")
        self.assertEqual(bars_of("NOW", "2026-11-13 16:00")[-1]["t"].strftime("%H:%M"), "15:30")
        b = bars_of("NOW")
        self.assertTrue(all(datetime.time(9, 30) <= x["t"].time() <= datetime.time(15, 30) for x in b))
        self.assertEqual(len([x for x in b if x["day"] == "2026-11-12"]), 13)


def mk(rows):
    t0 = datetime.datetime(2026, 11, 2, 9, 30)
    out = []
    for k, (o, h, l, c) in enumerate(rows):
        t = t0 + datetime.timedelta(days=k // 13, minutes=30 * (k % 13))
        out.append({"t": t, "day": t.date().isoformat(), "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    return out


def base():
    """A swing high of 110 (bar 12), an internal low of 104 (bar 22), then a sweep of 110 on bar 26."""
    rows = [(100 + k * 0.5, 101 + k * 0.5, 99.5 + k * 0.5, 100.5 + k * 0.5) for k in range(12)]
    rows.append((106.5, 110.0, 106.0, 108.0))                                    # 12: the swing high
    rows += [(108.0 - k * 0.3, 108.5 - k * 0.3, 107.0 - k * 0.3, 107.5 - k * 0.3) for k in range(9)]   # 13-21
    rows.append((105.0, 105.5, 104.0, 105.2))                                    # 22: the internal low
    rows += [(105.2, 106.5, 105.0, 106.2), (106.2, 107.5, 106.0, 107.2), (107.2, 108.5, 107.0, 108.2)]
    rows.append((108.2, 110.6, 108.0, 109.0))                                    # 26: the sweep
    return rows


class Rules(unittest.TestCase):
    P = {"swing_len": 8, "internal_len": 3}

    def test_sweep_mss_gap_entry_target(self):
        rows = base()
        rows.append((109.0, 109.2, 106.8, 107.0))        # 27
        rows.append((106.5, 106.6, 103.0, 103.2))        # 28: closes under 104 = MSS, and its high 106.6
        res = sw.run(mk(rows), self.P)                   #     is under bar 26's low 108.0: the gap
        s = res["state"]
        self.assertEqual((s["stage"], s["dir"], s["level"], s["mss_level"]), ("setup", -1, 110.0, 104.0))
        self.assertEqual((s["entry"], s["stop"], s["fvg"]), (106.6, 110.6, [106.6, 108.0]))
        self.assertAlmostEqual(s["target"], 106.6 - 2 * 4.0)
        rows.append((103.2, 105.9, 103.0, 105.5))        # 29: not back yet
        rows.append((105.5, 106.8, 105.0, 105.2))        # 30: back into the gap: filled at 106.6
        rows.append((105.2, 105.3, 96.0, 96.5))          # 31: through the target
        res = sw.run(mk(rows), self.P)
        self.assertEqual(res["state"]["stage"], "scan")
        t = res["trades"][0]
        self.assertEqual((t["side"], t["outcome"], t["R"], t["entry_i"], t["exit_i"]), ("sell", "target", 2.0, 30, 31))

    def test_beyond_the_sweep_extreme_cancels_it(self):
        rows = base() + [(109.0, 110.7, 108.9, 109.5)]   # 27: over the sweep's 110.6, closes under 110: a new sweep
        s = sw.run(mk(rows), self.P)["state"]
        self.assertEqual((s["stage"], s["sweep_i"], s["extreme"]), ("swept", 27, 110.7))
        rows = base() + [(109.0, 110.7, 108.9, 110.5)]   # 27: over it and closes above 110: nothing left
        self.assertEqual(sw.run(mk(rows), self.P)["state"]["stage"], "scan")

    def test_no_break_in_time_and_no_gap(self):
        rows = base() + [(109.0, 109.3, 108.7, 109.0)] * 21
        self.assertEqual(sw.run(mk(rows), self.P)["state"]["stage"], "scan")       # 20 bars without an MSS
        rows = base() + [(109.0, 109.2, 107.5, 107.8), (107.8, 108.2, 103.5, 103.8), (103.8, 107.6, 103.6, 105.0),
                         (105.0, 105.5, 104.5, 105.0), (105.0, 105.5, 104.5, 105.0)]
        s = sw.run(mk(rows[:-3]), self.P)["state"]          # broke with overlapping bars: no gap, so the
        self.assertEqual((s["stage"], s["entry_kind"], s["fvg"]), ("setup", "retest", None))    # limit is the retest
        self.assertEqual((s["entry"], s["stop"]), (104.0, 110.6))
        self.assertAlmostEqual(s["target"], 104.0 - 2 * 6.6)
        s = sw.run(mk(rows), self.P)["state"]               # bar 29 comes back to 104: filled
        self.assertEqual((s["stage"], s["entry_i"], s["bars"]), ("open", 29, 2))

    def test_a_gap_too_small_does_not_count(self):
        rows = base() + [(109.0, 109.2, 106.8, 107.0), (106.5, 107.95, 103.0, 103.2)]    # the gap is 107.95 to 108.0
        s = sw.run(mk(rows), self.P)["state"]
        self.assertEqual((s["entry_kind"], s["entry"]), ("retest", 104.0))
        s = sw.run(mk(rows), dict(self.P, min_fvg_atr=0))["state"]
        self.assertEqual((s["entry_kind"], s["entry"]), ("fvg", 107.95))

    def test_stop_first_and_the_stop_floor(self):
        rows = base()
        rows += [(109.0, 109.2, 106.8, 107.0), (106.5, 106.6, 103.0, 103.2), (103.2, 105.9, 103.0, 105.5)]
        rows.append((105.5, 111.0, 95.0, 105.0))         # 30         # fills, and touches the stop and the target: the stop counts
        t = sw.run(mk(rows), self.P)["trades"][0]
        self.assertEqual((t["outcome"], t["R"]), ("stop", -1.0))
        p = dict(self.P, min_stop_atr=10.0)              # an absurd floor: the stop moves out to 10 x ATR
        s = sw.run(mk(rows[:-1]), p)["state"]
        self.assertGreater(s["stop"], 110.6)

    def test_flat_eod_holds_nothing_overnight(self):
        bars = bars_of("NOW")
        res = sw.run(bars, None, True)
        self.assertTrue(res["trades"])
        for t in res["trades"]:
            self.assertEqual(t["entry_time"][:10], t["exit_time"][:10])
        self.assertTrue(any(t["outcome"] == "eod" for t in res["trades"]))

    def test_params_from_the_config(self):
        p = sw.sweep_params({"sweep": {"take_profit_r": 3, "swing_len": "10", "symbols": ["NVDA"]}})
        self.assertEqual((p["take_profit_r"], p["swing_len"], p["internal_len"]), (3.0, 10, 3))
        for bad in ({"sweep": {"foo": 1}}, {"sweep": {"swing_len": 0}}, {"sweep": {"interval": "2m"}}, {"sweep": []}):
            with self.assertRaises(sw.SweepError, msg=str(bad)):
                sw.sweep_params(bad)


class Cli(unittest.TestCase):
    def test_scan_table_and_json(self):
        code, text, err = cli(["scan", "--offline", DIR, "--symbols", "NASDAQ:APP, NOW,SPCX", "--now", NOW, "--no-write"])
        self.assertEqual(code, 0, err)
        lines = text.splitlines()
        self.assertIn("1 abiertas · 0 setups · 2 barridos · velas hasta 2026-11-13 15:30 NY", lines[0])
        self.assertRegex(lines[2], r"^NOW +▼ ABIERTA +35 +Q\d+ +102\.10 +103\.85 / 98\.60 +-0\.1 R")
        self.assertRegex(lines[3], r"^APP +▼ BARRIDO · esperando MSS +11 ")
        self.assertRegex(lines[4], r"^SPCX +▲ BARRIDO · esperando MSS +9 ")
        code, text, err = cli(["scan", "--offline", DIR, "--symbols", "NOW", "--now", NOW, "--no-write", "--json"])
        doc = json.loads(text)
        self.assertEqual(doc["rows"][0]["symbol"], "NOW")
        self.assertEqual(doc["params"]["take_profit_r"], 2.0)
        self.assertNotIn("sweep_i", doc["rows"][0])

    def test_backtest_and_refusals(self):
        code, text, err = cli(["backtest", "--offline", DIR, "--symbols", "NOW,APP,SPCX", "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual(doc["all"]["n"], len(doc["trades"]))
        self.assertEqual(doc["all"]["n"], doc["long"]["n"] + doc["short"]["n"])
        self.assertEqual(doc["random"]["n"], 600)
        self.assertEqual(sum(doc["outcomes"].values()), doc["all"]["n"])
        code, text, err = cli(["backtest", "--offline", DIR, "--symbols", "NOW", "--now", NOW])
        self.assertIn("Al azar (sin senal)", text)
        self.assertIn("Sin comisiones", text)
        code, text, err = cli(["scan", "--offline", DIR, "--symbols", "BTCUSDT", "--now", NOW, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("las cripto no pasan por este script", err)

    def test_the_watchlist_file(self):
        syms = sw.scan.symbols_from_file(sw.WATCHLIST)
        self.assertEqual(len(syms), 20)
        self.assertTrue(all(":" in s for s in syms))            # TradingView's EXCHANGE:TICKER

        class A(object):
            symbols = watchlist = None
        A.watchlist = sw.WATCHLIST
        got = sw.read_symbols(A(), {})
        self.assertEqual(got[:3], ["NVDA", "MU", "SPCX"])
        self.assertEqual(len(got), 20)
        A.watchlist = None
        self.assertGreater(len(sw.read_symbols(A(), {})), 200)      # by default: the 250 (the example list here)
        self.assertEqual(sw.read_symbols(A(), {"sweep": {"symbols": ["nyse:orcl", "TSM"]}}), ["ORCL", "TSM"])

    def test_it_never_trades(self):
        with open(os.path.join(os.path.dirname(FIX), "..", "trader-scanner", "sweep.py")) as fh:
            src = fh.read()
        for bad in ("trader-broker", "alpaca.markets", "/v2/orders", "ticket.py"):
            self.assertNotIn(bad, src)


class Timeframes(unittest.TestCase):
    def hourly(self, day, n=7):
        out = []
        for k in range(n):
            t = datetime.datetime.combine(day, datetime.time(9, 30)) + datetime.timedelta(hours=k)
            out.append({"t": t, "day": day.isoformat(), "o": 100.0 + k, "h": 101.5 + k, "l": 99.0 + k, "c": 101.0 + k,
                        "v": 10.0})
        return out

    def test_4h_bars_are_the_two_halves_of_the_session(self):
        d1, d2 = datetime.date(2026, 11, 12), datetime.date(2026, 11, 13)
        hourly = self.hourly(d1) + self.hourly(d2)
        b = sw.bars_4h(hourly, sw.scan.parse_now("2026-11-13 16:05"))
        self.assertEqual([x["t"].strftime("%d %H:%M") for x in b], ["12 09:30", "12 13:30", "13 09:30", "13 13:30"])
        self.assertEqual((b[0]["o"], b[0]["h"], b[0]["l"], b[0]["c"], b[0]["v"]), (100.0, 104.5, 99.0, 104.0, 40.0))
        self.assertEqual((b[1]["o"], b[1]["c"], b[1]["v"]), (104.0, 107.0, 30.0))
        b = sw.bars_4h(hourly, sw.scan.parse_now("2026-11-13 14:00"))        # the afternoon half is still forming
        self.assertEqual(len(b), 3)
        b = sw.bars_4h(hourly, sw.scan.parse_now("2026-11-13 12:00"))
        self.assertEqual(len(b), 2)

    def test_the_last_hour_bar_ends_at_the_close(self):
        series = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        for hh, mm in ((14, 30), (15, 30)):
            t = sw.scan.eastern_to_utc(datetime.datetime(2026, 11, 13, hh, mm))
            series["t"].append(int((t - datetime.datetime(1970, 1, 1)).total_seconds()))
            for k in "ohlc":
                series[k].append(100.0)
            series["v"].append(1)
        got = sw.session_bars(series, sw.scan.parse_now("2026-11-13 16:00"), 60)
        self.assertEqual([x["t"].strftime("%H:%M") for x in got], ["14:30", "15:30"])   # 15:30-16:00 is complete
        got = sw.session_bars(series, sw.scan.parse_now("2026-11-13 15:45"), 60)
        self.assertEqual([x["t"].strftime("%H:%M") for x in got], ["14:30"])

    def test_interval_flag_and_brief_table(self):
        code, text, err = cli(["scan", "--offline", DIR, "--symbols", "NOW", "--now", NOW, "--no-write",
                               "--interval", "2m"])
        self.assertEqual(code, 1)
        self.assertIn("--interval es uno de", err)
        self.assertEqual(sw.RANGE["backtest"]["1h"], "730d")
        self.assertEqual(sw.FETCH["4h"], "1h")


class MarketData(unittest.TestCase):
    def test_30m_from_alpaca_is_the_delayed_consolidated_tape(self):
        md = load("trader-scanner", "marketdata")
        now = datetime.datetime(2026, 11, 13, 20, 0)
        frame, start, end, feed = md.window("60d", "30m", now, False)
        self.assertEqual((frame, feed, end), ("30Min", "sip", "2026-11-13T19:44:00Z"))
        self.assertEqual(md.window("1d", "5m", now, False)[0], "5Min")     # the intraday scan is untouched


if __name__ == "__main__":
    unittest.main()
