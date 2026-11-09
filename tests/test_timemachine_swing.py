"""trader-timemachine --horizon-days: the multi-day replay on daily bars.
Offline: TSLA's and AAPL's real daily bars from Yahoo (range=5y interval=1d),
saved up to Fri 2026-10-30. Synthetic pools pin the day rules exactly."""

import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")
exitp = load("trader-exit", "exit")

DIR = os.path.join(FIX, "trader-timemachine")
NOW = "2026-11-02 09:00"
NOW_UTC = tm.parse_now(NOW)
TSLA_TEXT = tm.read_offline_daily(DIR, "TSLA")

# the swing plan trader-exit writes for TSLA on Fri 2026-10-30 (breakout20, atr:2, 2R, 7 days)
PLAN = {"created_by": "trader-exit", "mode": "swing", "symbol": "TSLA", "side": "buy", "date": "2026-10-30",
        "setup": "breakout20", "entry": 399.81, "interval": "1d", "stop": 371.04, "stop_rule": "atr",
        "stop_param": 2.0, "target": 457.35, "target_r": 2.0, "risk_per_share": 28.77, "reward_risk": 2.0,
        "horizon_days": 7, "time_stop_days": 7, "time_stop_date": "2026-11-10", "flat_by": None,
        "atr": 14.3829, "atr_bars": 14}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


def bar(d, o, h, l, c):
    return {"date": d, "o": o, "h": h, "l": l, "c": c}


class DayRules(unittest.TestCase):
    def test_walk_days(self):
        w = tm.walk_days
        self.assertEqual(w([(100, 101, 99, 100)] * 3, 100, 95, 110, 5, 2.0), ("open", 0.0, 3, False))
        self.assertEqual(w([(100, 111, 99, 108)], 100, 95, 110, 5, 2.0), ("target", 2.0, 1, False))
        self.assertEqual(w([(100, 101, 94, 96)], 100, 95, 110, 5, 2.0), ("stop", -1.0, 1, False))
        # a day touching both counts as the stop
        self.assertEqual(w([(100, 112, 94, 100)], 100, 95, 110, 5, 2.0), ("stop", -1.0, 1, False))
        # a gap through the stop fills at the open: worse than -1R
        self.assertEqual(w([(101, 102, 100, 101), (90, 92, 88, 91)], 100, 95, 110, 5, 2.0), ("stop", -2.0, 2, True))
        # a gap over the target gets the target's R only
        self.assertEqual(w([(115, 116, 114, 115)], 100, 95, 110, 5, 2.0), ("target", 2.0, 1, True))
        # unresolved: R at day N's close (the time stop)
        self.assertEqual(w([(100, 102, 99, 102.5)], 100, 95, 110, 5, 2.0), ("open", 0.5, 1, False))

    def test_simulation_on_synthetic_pools(self):
        up = [(1.0, 1.03, 0.995, 1.02)] * 50          # +2 % every day: the target always comes first
        mc = tm.simulate_swing(up, 100.0, 95.0, 110.0, 7, paths=500, seed=1)
        self.assertEqual((mc["p_target"], mc["p_stop"], mc["p_unresolved"]), (1.0, 0.0, 0.0))
        self.assertAlmostEqual(mc["expectancy_R"], 2.0)
        self.assertEqual(mc["median_days_to_target"], 5)    # 100 x 1.02^4 x 1.03 = 111.5 on day 5
        flat = [(1.0, 1.001, 0.999, 1.0)] * 50
        mc = tm.simulate_swing(flat, 100.0, 95.0, 110.0, 7, paths=500, seed=1)
        self.assertEqual(mc["p_unresolved"], 1.0)
        self.assertAlmostEqual(mc["expectancy_R"], 0.0)
        gap = [(0.9, 0.92, 0.88, 0.9)] * 50              # opens 10 % lower: gaps through the 5 % stop
        mc = tm.simulate_swing(gap, 100.0, 95.0, 110.0, 7, paths=200, seed=1)
        self.assertEqual((mc["p_stop"], mc["p_gap_stop"]), (1.0, 1.0))
        self.assertAlmostEqual(mc["expectancy_R"], -2.0)

    def test_blocks_are_consecutive_days(self):
        # a pool where only day 0 rises: with blocks of 3 a path holds day 0 only if its block starts
        # at 0, 98 or 99; the block then runs on in order and wraps around (circular)
        pool = [(1.0, 1.0, 1.0, 1.0)] * 100
        pool[0] = (1.0, 1.2, 1.0, 1.0)
        mc = tm.simulate_swing(pool, 100.0, 50.0, 110.0, 3, paths=20000, seed=3, block=3)
        self.assertAlmostEqual(mc["p_target"], 0.03, delta=0.004)

    def test_same_seed_same_numbers(self):
        rets = [(1.0, 1.02, 0.98, 1.0 + ((i * 7) % 11 - 5) / 400.0) for i in range(120)]
        a = tm.simulate_swing(rets, 50.0, 47.0, 56.0, 7, 3000, 19)
        b = tm.simulate_swing(rets, 50.0, 47.0, 56.0, 7, 3000, 19)
        c = tm.simulate_swing(rets, 50.0, 47.0, 56.0, 7, 3000, 20)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertAlmostEqual(a["p_target"] + a["p_stop"] + a["p_unresolved"], 1.0)


class Indicators(unittest.TestCase):
    def test_ema_sma_rsi_atr(self):
        v = [1.0, 2, 3, 4, 5, 6]
        self.assertEqual(tm.sma_series(v, 3), [None, None, 2.0, 3.0, 4.0, 5.0])
        e = tm.ema_series(v, 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 3.0)        # 4 x 0.5 + 2 x 0.5
        r = tm.rsi_series([10, 11, 12, 11, 10, 9], 2)
        self.assertEqual(r[:2], [None, None])
        self.assertEqual(r[2], 100.0)
        self.assertEqual(r[3:], [50.0, 25.0, 12.5])    # Wilder: gains 1,1,0,0,0 and losses 0,0,1,1,1
        bars = [bar("d%d" % i, 10, 11, 9, 10) for i in range(5)]
        bars[3] = bar("d3", 13, 14, 12, 13)      # a gap up (TR 14 - 10 = 4), then back down (TR 13 - 9 = 4)
        self.assertEqual(tm.atr_series(bars, 2)[:5], [None, None, 2.0, 3.0, 4.0])

    def test_setups(self):
        base = [bar("d%02d" % i, 100, 101, 99, 100) for i in range(25)]
        ind = tm.indicators(base)
        self.assertFalse(tm.setup_fires("breakout20", base, 24, ind))
        brk = base[:24] + [bar("d24", 100, 103, 100, 102)]
        self.assertTrue(tm.setup_fires("breakout20", brk, 24, tm.indicators(brk)))
        self.assertTrue(tm.setup_fires("any", base, 3, ind))
        # pullback: a steady uptrend, then a dip that touches the EMA20 and closes above it
        up = [bar("u%02d" % i, 100 + i, 101 + i, 99.5 + i, 100.5 + i) for i in range(60)]
        ind = tm.indicators(up)
        e = ind["ema"][59]
        dip = up[:59] + [bar("u59", e + 2, e + 3, e - 0.2, e + 0.5)]
        self.assertTrue(tm.setup_fires("pullback_ema20", dip, 59, tm.indicators(dip)))
        self.assertFalse(tm.setup_fires("pullback_ema20", up, 59, ind))   # no touch
        # rsi2: two hard down days inside a 200-day uptrend
        long_up = [bar("r%03d" % i, 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5, 100 + i * 0.5) for i in range(210)]
        drop = long_up[:208] + [bar("r208", 200, 200, 195, 196), bar("r209", 196, 196, 192, 193)]
        self.assertTrue(tm.setup_fires("rsi2", drop, 209, tm.indicators(drop)))
        self.assertFalse(tm.setup_fires("rsi2", long_up, 209, tm.indicators(long_up)))

    def test_history_one_trade_at_a_time_and_no_peeking(self):
        bars = [bar("d%03d" % i, 100, 101, 99, 100) for i in range(40)]
        rule = {"stop": "pct", "stop_pct": 2.0, "target_r": 1.0, "horizon_days": 5}
        h = tm.history_swing(bars, 39, "any", rule)
        # every trade runs its full 5 days unresolved, so one starts every 6 days and all end by bar 39
        self.assertEqual(h["triggered"], 6)
        self.assertEqual(h["unresolved"], 6)
        self.assertTrue(h["few_cases"])
        self.assertTrue(all(t["date"] <= "d034" for t in h["trades"]))
        bars[20] = bar("d020", 100, 104, 100, 103)
        h = tm.history_swing(bars, 39, "breakout20", rule)
        self.assertEqual(h["triggered"], 1)
        self.assertEqual(h["trades"][0]["date"], "d020")
        self.assertEqual((h["trades"][0]["stop"], h["trades"][0]["outcome"]), (100.94, "stop"))


class RealData(unittest.TestCase):
    def doc(self, plan=PLAN, **kw):
        p = tm.normalize_swing_plan(plan, "test", kw.pop("horizon", None))
        return tm.swing_time_machine(TSLA_TEXT, p, NOW_UTC, **kw)

    def test_tsla_breakout20_numbers(self):
        d = self.doc()
        m, h = d["mc"], d["history"]
        self.assertEqual(d["mode"], "swing")
        self.assertEqual((m["paths"], m["seed"], m["block_days"], m["horizon_days"]), (10000, 19, 5, 7))
        self.assertEqual((m["window_days"], m["window_from"], m["window_to"]), (252, "2025-10-30", "2026-10-30"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_unresolved"]), (0.0483, 0.3057, 0.646))
        self.assertAlmostEqual(m["expectancy_R"], -0.0331, places=4)
        self.assertEqual(m["p_gap_stop"], 0.0401)
        self.assertEqual((h["setup"], h["triggered"], h["target"], h["stop"], h["unresolved"]),
                         ("breakout20", 38, 7, 13, 18))
        self.assertEqual(h["rule"], {"target_r": 2.0, "horizon_days": 7, "side": "buy", "stop": "atr",
                                     "atr_mult": 2.0, "atr_days": 14})
        self.assertFalse(h["few_cases"])
        self.assertTrue(d["verdict"].startswith("Claramente hacia el stop: objetivo primero 4.8 % vs stop primero "
                                                "30.6 % (sin resolver al dia 7: 64.6 %)"))
        self.assertEqual(d["warnings"], [])
        text = "\n".join(tm.report_swing(d))
        for must in ("10,000 trayectorias de 7 dias habiles", "bloques de 5 dias", "semilla 19",
                     "objetivo primero      4.8 %", "stop primero          30.6 %", "sin resolver dia 7",
                     "expectativa           -0.03R", "se disparo 38 veces: 7 objetivo, 13 stop, 18 sin resolver",
                     "no una prediccion", "el 33 %"):
            self.assertIn(must, text)
        self.assertNotIn("pro" + "fit", text.lower())

    def test_reproducible_and_seed_and_window(self):
        self.assertEqual(self.doc()["mc"], self.doc()["mc"])
        self.assertNotEqual(self.doc(seed=7)["mc"]["p_stop"], self.doc()["mc"]["p_stop"])
        d = self.doc(window=120, block=3, horizon=5)
        self.assertEqual((d["mc"]["window_days"], d["mc"]["block_days"], d["mc"]["horizon_days"]), (120, 3, 5))
        self.assertEqual(d["history"]["rule"]["horizon_days"], 5)

    def test_coin_flip_verdict(self):
        mc = {"p_target": 0.31, "p_stop": 0.345, "p_unresolved": 0.345, "horizon_days": 7}
        self.assertTrue(tm.swing_verdict(mc).startswith("Cercano a una moneda al aire"))
        mc.update(p_target=0.40, p_stop=0.30)
        self.assertTrue(tm.swing_verdict(mc).startswith("Se inclina hacia el objetivo"))
        mc.update(p_target=0.0, p_stop=0.0, p_unresolved=1.0)
        self.assertIn("Ninguna trayectoria", tm.swing_verdict(mc))

    def test_few_cases_and_unknown_setup(self):
        d = self.doc(dict(PLAN, setup="rsi2", stop_rule="pct", stop_param=7.2))
        self.assertEqual(d["history"]["rule"]["stop"], "pct")
        manual = self.doc(dict(PLAN, setup="manual"))
        self.assertEqual(manual["history"]["setup"], "any")
        self.assertIn("linea base", manual["history"]["note"])
        # a short history: the warning
        short = self.doc(dict(PLAN, date="2022-03-01", entry=290.0, stop=270.0, target=330.0), window=60)
        self.assertTrue(short["history"]["few_cases"])
        self.assertTrue(any("Advertencia" in n and "menos de 30" in n for n in short["verdict_notes"]))

    def test_refusals(self):
        bad = [
            (dict(PLAN, side="sell"), "a short needs target < entry < stop"),
            (dict(PLAN, side="sideways"), "side es buy"),
            (dict(PLAN, stop=410.0), "stop < entry < target"),
            (dict(PLAN, date="nope"), "date"),
            (dict(PLAN, horizon_days=None, time_stop_days=99), "between 1 and 60"),
        ]
        for plan, msg in bad:
            with self.assertRaises(tm.TMError) as cm:
                self.doc(plan)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            self.doc(dict(PLAN, date="2021-11-10"))
        self.assertIn("al menos 60", str(cm.exception))
        with self.assertRaises(tm.TMError):
            self.doc(window=10)
        with self.assertRaises(tm.TMError) as cm:
            tm.swing_time_machine(TSLA_TEXT, tm.normalize_swing_plan(dict(PLAN, symbol="AAPL"), "t"), NOW_UTC)
        self.assertIn("the data is for TSLA", str(cm.exception))
        five = tm.read_offline(os.path.join(DIR, "TSLA-60d5m.json.gz"), "TSLA")
        with self.assertRaises(tm.TMError) as cm:
            tm.swing_time_machine(five, tm.normalize_swing_plan(PLAN, "t"), NOW_UTC)
        self.assertIn("daily bars", str(cm.exception))

    def test_todays_bar_waits_for_the_close(self):
        res = tm.chart_result(TSLA_TEXT, "TSLA")
        _, bars = tm.parse_daily(res, tm.parse_now("2026-10-30 15:59"))
        self.assertEqual(bars[-1]["date"], "2026-10-29")
        _, bars = tm.parse_daily(res, tm.parse_now("2026-10-30 16:00"))
        self.assertEqual(bars[-1]["date"], "2026-10-30")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_exit_plan_to_time_machine(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = exitp.main(["plan", "--mode", "swing", "--symbol", "TSLA", "--setup", "breakout20", "--now", NOW,
                               "--offline", os.path.join(DIR, "TSLA-5y1d.json.gz"), "--write"])
        self.assertEqual(code, 0, err.getvalue())
        with open(os.path.join("trader", "plan.json")) as fh:
            written = json.load(fh)
        for k in ("symbol", "date", "entry", "stop", "target", "stop_param", "time_stop_days"):
            self.assertEqual(written[k], PLAN[k], k)
        # a swing plan.json switches the time machine to the multi-day replay on its own
        code, text, err = run(["run", "--offline", DIR, "--now", NOW, "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("10,000 trayectorias de 7 dias habiles", text)
        self.assertIn("Claramente hacia el stop", text)
        with open(os.path.join("trader", "timemachine.json")) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["mode"], doc["mc"]["p_stop"]), ("swing", 0.3057))
        code, text, _ = run(["run", "--offline", DIR, "--now", NOW, "--horizon-days", "5", "--json"])
        self.assertEqual(json.loads(text)["mc"]["horizon_days"], 5)
        code, _, err = run(["chart", "--offline", DIR, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("solo intradia", err)

    def test_flags_and_config(self):
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"mode": "swing", "swing": {"timemachine_window_days": 200, "timemachine_block_days": 4}}, fh)
        code, text, err = run(["run", "--symbol", "AAPL", "--date", "2026-10-30", "--entry", "344.63",
                               "--stop", "330", "--target", "373.89", "--setup", "rsi2", "--horizon-days", "5",
                               "--offline", DIR, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        d = json.loads(text)
        self.assertEqual((d["mc"]["window_days"], d["mc"]["block_days"]), (200, 4))
        self.assertEqual(d["plan"]["symbol"], "AAPL")
        self.assertEqual(d["history"]["rule"]["stop"], "pct")
        code, text, _ = run(["run", "--symbol", "AAPL", "--date", "2026-10-30", "--entry", "344.63",
                             "--stop", "330", "--target", "373.89", "--horizon-days", "5", "--window-days", "100",
                             "--offline", DIR, "--now", NOW, "--json"])
        self.assertEqual(json.loads(text)["mc"]["window_days"], 100)

    def test_network_url(self):
        self.assertEqual(tm.daily_chart_url("brk.b"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/BRK-B?range=5y&interval=1d")
        calls = []
        saved = tm._get
        try:
            tm._get = lambda url, timeout=20: calls.append(url) or TSLA_TEXT
            self.assertEqual(tm.fetch_daily("TSLA"), TSLA_TEXT)
        finally:
            tm._get = saved
        self.assertEqual(calls, [tm.daily_chart_url("TSLA")])


if __name__ == "__main__":
    unittest.main()
