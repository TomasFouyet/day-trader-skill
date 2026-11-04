"""trader-exit in swing mode ("mode": "swing"): a daily-ATR stop, a 2R target,
no flat-by and a time stop at the close of trading day N. Offline: TSLA's and
AAPL's real daily bars from Yahoo, saved up to Fri 2026-10-30 (5 years)."""

import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

exitp = load("trader-exit", "exit")

DAILY = os.path.join(FIX, "trader-timemachine")
TSLA_D = os.path.join(DAILY, "TSLA-5y1d.json.gz")
NOW = "2026-11-02 09:00"     # Monday before the open: the last closed session is Fri 2026-10-30


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = exitp.main(argv)
    return code, out.getvalue(), err.getvalue()


def raw_daily(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        r = json.load(fh)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    return list(zip(q["high"], q["low"], q["close"]))


def atr14(rows):
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for (_, _, pc), (h, l, _) in zip(rows, rows[1:])]
    return sum(trs[-14:]) / 14.0


class SwingConfig(unittest.TestCase):
    def test_defaults(self):
        sw = exitp.swing_config({})
        self.assertEqual((sw["horizon_days"], sw["stop"], sw["target"], sw["time_stop_days"]), (7, "atr:2", "2R", None))
        self.assertEqual((sw["max_risk_per_trade_pct"], sw["max_positions"], sw["max_exposure_pct"]), (1.0, 5, 60.0))

    def test_bad_values_are_refused(self):
        for bad in ({"horizon_days": 0}, {"horizon_days": 2.5}, {"max_positions": 0}, {"max_risk_per_trade_pct": 0},
                    {"max_risk_per_trade_pct": 25}, {"max_exposure_pct": 120}, {"stop": "or-mid"},
                    {"target": "big"}, {"time_stop_days": 99}):
            with self.assertRaises(exitp.PlanError, msg=str(bad)):
                exitp.swing_config({"swing": bad})
        with self.assertRaises(exitp.PlanError):
            exitp.swing_config({"swing": [1, 2]})

    def test_mode(self):
        class A:
            mode = None
        self.assertEqual(exitp.resolve_mode(A(), {}), "intraday")
        self.assertEqual(exitp.resolve_mode(A(), {"mode": "Swing"}), "swing")
        A.mode = "intraday"
        self.assertEqual(exitp.resolve_mode(A(), {"mode": "swing"}), "intraday")   # the flag wins
        A.mode = None
        with self.assertRaises(exitp.PlanError):
            exitp.resolve_mode(A(), {"mode": "scalp"})

    def test_example_config_parses(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "trader-budget", "config.swing.example.json")) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["mode"], "swing")
        self.assertIs(cfg["paper"], True)
        sw = exitp.swing_config(cfg)
        self.assertEqual((sw["horizon_days"], sw["time_stop_days"], sw["stop"], sw["target"]), (7, 7, "atr:2", "2R"))


class TradingDays(unittest.TestCase):
    def test_add_trading_days_skips_weekends_and_holidays(self):
        d = datetime.date
        self.assertEqual(exitp.add_trading_days(d(2026, 10, 30), 7), d(2026, 11, 10))
        # Thanksgiving (Thu 11-26) is skipped; the half day after it is a session
        self.assertEqual(exitp.add_trading_days(d(2026, 11, 24), 3), d(2026, 11, 30))
        self.assertEqual(exitp.add_trading_days(d(2026, 12, 23), 2), d(2026, 12, 28))

    def test_last_closed_session(self):
        p = exitp.parse_now
        self.assertEqual(exitp.last_closed_session(p("2026-11-02 09:00")).isoformat(), "2026-10-30")
        self.assertEqual(exitp.last_closed_session(p("2026-11-02 16:05")).isoformat(), "2026-11-02")
        self.assertEqual(exitp.last_closed_session(p("2026-11-01 12:00")).isoformat(), "2026-10-30")


class SwingPlan(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def cfg(self, doc):
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(["plan", "--now", NOW, "--offline", TSLA_D] + list(args))

    def test_config_mode_swing_defaults_atr2_2r_time_stop(self):
        self.cfg({"mode": "swing"})
        code, text, err = self.plan("--symbol", "TSLA", "--setup", "breakout20", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        rows = raw_daily(TSLA_D)
        atr = atr14(rows)
        entry = rows[-1][2]
        self.assertEqual(p["mode"], "swing")
        self.assertEqual((p["date"], p["entry"], p["entry_basis"]), ("2026-10-30", entry, "cierre del 2026-10-30"))
        self.assertAlmostEqual(p["atr"], atr, places=4)
        self.assertEqual(p["atr_bars"], 14)
        self.assertEqual(p["stop"], float(int((entry - 2 * atr) * 100)) / 100)        # rounded down
        risk = round(entry - p["stop"], 2)
        self.assertEqual(p["risk_per_share"], risk)
        self.assertEqual(p["target"], round(entry + 2 * risk, 2))
        self.assertEqual((p["stop_rule"], p["stop_param"], p["target_r"], p["reward_risk"]), ("atr", 2.0, 2.0, 2.0))
        self.assertEqual((p["horizon_days"], p["time_stop_days"], p["time_stop_date"]), (7, 7, "2026-11-10"))
        self.assertIsNone(p["flat_by"])
        self.assertEqual(p["interval"], "1d")
        self.assertEqual(p["limits"], {"max_risk_per_trade_pct": 1.0, "max_positions": 5, "max_exposure_pct": 60.0})
        self.assertEqual(p["warnings"], [])

    def test_text_output_and_write_then_show(self):
        code, text, err = self.plan("--mode", "swing", "--symbol", "TSLA", "--write")
        self.assertEqual(code, 0, err)
        self.assertIn("Plan de salida swing: TSLA largo", text)
        self.assertIn("2 x ATR(14) diario", text)
        self.assertIn("Stop de tiempo   al cierre del mar 2026-11-10 (dia habil 7)", text)
        self.assertIn("Sin flat-by", text)
        self.assertIn("No se coloco nada", text)
        self.assertNotIn("Flat by", text)
        self.assertNotIn("pro" + "fit", text.lower())
        with open(os.path.join("trader", "plan.json")) as fh:
            self.assertEqual(json.load(fh)["mode"], "swing")
        code, text, err = run(["show", "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-11-10", text)
        self.assertNotIn("ya paso", text)
        code, text, _ = run(["show", "--now", "2026-11-12 10:00"])
        self.assertIn("ya paso", text)

    def test_config_values_and_flags(self):
        self.cfg({"mode": "swing", "swing": {"horizon_days": 5, "stop": "pct:6", "target": "1.5R"}})
        code, text, err = self.plan("--symbol", "TSLA", "--entry", "400", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["target"], p["stop_rule"]), (400.0, 376.0, 436.0, "pct"))
        self.assertEqual((p["horizon_days"], p["time_stop_days"], p["entry_basis"]), (5, 5, "tu precio"))
        code, text, _ = self.plan("--symbol", "TSLA", "--entry", "400", "--horizon-days", "6",
                                  "--time-stop-days", "8", "--stop", "price:390", "--target", "price:430", "--json")
        p = json.loads(text)
        self.assertEqual((p["stop"], p["target"], p["reward_risk"], p["time_stop_days"]), (390.0, 430.0, 3.0, 8))
        self.assertTrue(any("despues del horizonte" in w for w in p["warnings"]))

    def test_an_older_signal_date(self):
        code, text, err = self.plan("--mode", "swing", "--symbol", "TSLA", "--date", "2026-10-20", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["date"], p["entry"], p["time_stop_date"]), ("2026-10-20", 373.71, "2026-10-29"))
        self.assertTrue(any("ultima sesion cerrada es el 2026-10-30" in w for w in p["warnings"]))
        code, text, _ = self.plan("--mode", "swing", "--symbol", "TSLA", "--date", "2026-10-25", "--json")
        self.assertIn("se usa la ultima, del 2026-10-23", text)        # a Sunday: Friday's bar

    def test_todays_bar_waits_for_the_close(self):
        code, text, _ = run(["plan", "--mode", "swing", "--now", "2026-10-30 11:00", "--offline", TSLA_D,
                             "--symbol", "TSLA", "--json"])
        self.assertEqual(json.loads(text)["date"], "2026-10-29")
        code, text, _ = run(["plan", "--mode", "swing", "--now", "2026-10-30 16:01", "--offline", TSLA_D,
                             "--symbol", "TSLA", "--json"])
        self.assertEqual(json.loads(text)["date"], "2026-10-30")

    def test_from_a_candidate_file(self):
        with open("cand.json", "w") as fh:
            json.dump({"symbol": "tsla", "price": 399.81, "date": "2026-10-30", "setup": "rsi2"}, fh)
        code, text, err = self.plan("--mode", "swing", "--from", "cand.json", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["symbol"], p["setup"], p["entry"]), ("TSLA", "rsi2", 399.81))

    def test_refusals(self):
        self.cfg({"mode": "swing"})
        cases = [
            (["--symbol", "TSLA", "--flat-by", "15:00"], "--flat-by es del intradia"),
            (["--symbol", "TSLA", "--stop", "or-mid"], "atr:K, pct:X o price:X"),
            (["--symbol", "TSLA", "--setup", "orb"], "es intradia"),
            (["--symbol", "TSLA", "--side", "sell"], "solo largos"),
            (["--symbol", "TSLA", "--or-high", "1", "--or-low", "0.5"], "rango de apertura"),
            ([], "da --symbol"),
            (["--symbol", "TSLA", "--entry", "300", "--stop", "price:310"], "at or above the entry"),
            (["--symbol", "TSLA", "--horizon-days", "0"], "--horizon-days"),
            (["--symbol", "AAPL"], "holds TSLA, not AAPL"),
        ]
        for args, msg in cases:
            code, _, err = self.plan(*args)
            self.assertEqual(code, 1, args)
            self.assertIn(msg, err, args)
            self.assertNotIn("Traceback", err)
        self.cfg({"mode": "swing", "swing": {"max_positions": -1}})
        code, _, err = self.plan("--symbol", "TSLA")
        self.assertIn("swing.max_positions", err)

    def test_intraday_is_untouched_without_mode(self):
        # no config at all: the intraday path, which refuses a symbol-only plan as before
        code, _, err = self.plan("--symbol", "TSLA")
        self.assertEqual(code, 1)
        self.assertIn("needs --entry", err)
        self.cfg({"mode": "intraday", "flat_by": "15:45"})
        code, text, _ = run(["plan", "--now", "2026-10-19 10:16", "--symbol", "TSLA", "--entry", "364.18",
                             "--or-high", "362.18", "--or-low", "354.47", "--json"])
        self.assertEqual(json.loads(text)["flat_by"], "15:45")
        self.assertNotIn("mode", json.loads(text))


if __name__ == "__main__":
    unittest.main()
