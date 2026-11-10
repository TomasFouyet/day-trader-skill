"""The winrate (share of paths and of real past trades that end in profit) and the commodity
"comodines" (GLD, SLV, USO, BNO, UNG, CPER: real Yahoo daily charts, range=1y, saved 2026-11-10)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

tm = load("trader-timemachine", "timemachine")
scan = load("trader-scanner", "scan")
er = load("trader-analyst", "earnings")
tg = load("trader-alerts", "notify_telegram")
size = load("trader-budget", "size")

CMD = os.path.join(FIX, "trader-scanner", "commodities")
SWDIR = os.path.join(FIX, "trader-scanner", "swing")
EXAMPLE = os.path.join(ROOT, "trader-budget", "config.swing.example.json")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Winrate(unittest.TestCase):
    def test_simulation(self):
        up = [(1.0, 1.03, 0.995, 1.02)] * 50
        self.assertEqual(tm.simulate_swing(up, 100.0, 95.0, 110.0, 7, 200, 1)["p_win"], 1.0)
        flat_up = [(1.0, 1.001, 0.999, 1.0005)] * 50      # never touches a side, a little higher each day
        mc = tm.simulate_swing(flat_up, 100.0, 95.0, 110.0, 7, 200, 1)
        self.assertEqual((mc["p_unresolved"], mc["p_win"]), (1.0, 1.0))   # closed at the time stop in profit
        down = [(1.0, 1.001, 0.94, 0.95)] * 50
        self.assertEqual(tm.simulate_swing(down, 100.0, 95.0, 110.0, 7, 200, 1)["p_win"], 0.0)
        rets = [(1.0, 1.02, 0.98, 1.0 + ((i * 7) % 11 - 5) / 400.0) for i in range(120)]
        mc = tm.simulate_swing(rets, 50.0, 47.0, 56.0, 7, 3000, 19)
        self.assertGreaterEqual(mc["p_win"], mc["p_target"])     # every target is a win, some open ones too
        self.assertLessEqual(mc["p_win"], mc["p_target"] + mc["p_unresolved"])

    def test_history_and_report(self):
        bars = [{"date": "d%03d" % i, "o": 100, "h": 101, "l": 99, "c": 100 + i * 0.01} for i in range(40)]
        h = tm.history_swing(bars, 39, "any", {"stop": "pct", "stop_pct": 2.0, "target_r": 1.0, "horizon_days": 5})
        self.assertEqual(h["win_rate"], 1.0)                       # each one closes a little higher
        text = tm.read_offline_daily(SWDIR, "TSLA")
        plan = tm.normalize_swing_plan({"symbol": "TSLA", "date": "2026-10-30", "entry": 399.81, "stop": 371.04,
                                        "target": 457.35, "setup": "breakout20", "time_stop_days": 7}, "t")
        d = tm.swing_time_machine(text, plan, tm.parse_now("2026-10-30 17:00"), paths=1000)
        self.assertIn("winrate", "\n".join(tm.report_swing(d)))
        self.assertIsNotNone(d["history"]["win_rate"])

    def test_message(self):
        t = {"symbol": "WMT", "side": "buy", "qty": 9, "entry": 123.51, "stop": 118.22, "target": 134.09,
             "max_loss": 47.61, "cost": 1111.59, "reward_risk": 2.0, "setup": "breakout20",
             "signal_date": "2026-10-30", "time_stop_date": "2026-11-10", "time_stop_days": 7,
             "timemachine": {"p_target": 0.06, "p_stop": 0.24, "p_unresolved": 0.70, "expectancy_R": 0.12,
                             "p_win": 0.548, "paths": 10000, "horizon_days": 7, "history_cases": 9,
                             "history_win_rate": 0.556, "few_cases": True}}
        m = tg.message(t, {})
        self.assertIn("🎯 Winrate estimado: <b>54.8 %</b> en la simulación · <b>55.6 %</b> en el historial real", m)
        self.assertIn("win 55%", tg.summary([("k", t, {})], "2026-11-02"))


class Commodities(unittest.TestCase):
    def test_extras_skip_the_volume_floor_and_are_labelled(self):
        with open(EXAMPLE) as fh:
            cfg = json.load(fh)
        extras = scan.swing_extras(cfg)
        self.assertEqual(extras, ["GLD", "SLV", "USO", "BNO", "UNG", "CPER"])
        setups, p, floors = scan.swing_params(cfg)
        d, _ = scan.load_offline(CMD)
        now = scan.parse_now("2026-11-09 17:00")
        cands, counts, _ = scan.scan_swing(d, sorted(d), now, setups, p, dict(floors, min_dollar_volume=1e12), extras)
        self.assertEqual(counts["illiquid"], 0)                    # the comodines are never "illiquid"
        self.assertEqual([(c["symbol"], c["setup"], c["asset"]) for c in cands],
                         [("USO", "breakdown20", "Petroleo WTI"), ("BNO", "breakdown20", "Petroleo Brent"),
                          ("UNG", "rsi2_short", "Gas natural")])
        self.assertTrue(all(c["extra"] and c["side"] == "sell" for c in cands))
        _, counts, _ = scan.scan_swing(d, sorted(d), now, setups, p, dict(floors, min_dollar_volume=1e12))
        self.assertEqual(counts["illiquid"], 6)                    # without the flag they would be
        with self.assertRaises(scan.ScanError):
            scan.swing_extras({"swing": {"extra_symbols": ["GC=F"]}})

    def test_cli_adds_them_to_the_watchlist(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(d)
            os.mkdir("trader")
            shutil.copy(EXAMPLE, "trader/config.json")
            os.mkdir("charts")
            for f in os.listdir(CMD):
                shutil.copy(os.path.join(CMD, f), "charts")
            shutil.copy(os.path.join(SWDIR, "AAPL.json.gz"), "charts")
            with open("trader/watchlist.txt", "w") as fh:
                fh.write("AAPL\n")
            code, text, err = run(scan, ["swing", "--offline", "charts", "--now", "2026-11-09 17:00", "--json",
                                         "--no-write"])
            doc = json.loads(text)
            self.assertEqual(doc["universe"], 7)                   # AAPL + the six comodines
            code, text, _ = run(scan, ["swing", "--offline", "charts", "--now", "2026-11-09 17:00", "--json",
                                       "--no-write", "--no-extras"])
            self.assertEqual(json.loads(text)["universe"], 1)
            code, text, _ = run(scan, ["swing", "--offline", "charts", "--now", "2026-11-09 17:00", "--json",
                                       "--no-write", "--symbols", "AAPL"])
            self.assertEqual(json.loads(text)["universe"], 1)       # explicit --symbols: only those
        finally:
            os.chdir(cwd)
            shutil.rmtree(d)

    def test_no_earnings_and_the_message(self):
        sw = er.swing_config(EXAMPLE)
        items = [{"symbol": "USO", "date": "2026-11-09", "extra": True}, {"symbol": "GLD", "date": "2026-11-09"}]
        res = er.annotate(items, sw, er.Sec(None), {})
        self.assertTrue(all(r["no_earnings"] and not r["blocked"] and not r["warnings"] for r in res))
        t = {"symbol": "USO", "name": "United States Oil Fund, LP", "asset": "Petroleo WTI", "side": "sell",
             "qty": 3, "entry": 142.66, "stop": 150.0, "target": 128.0, "max_loss": 22.0, "reward_risk": 2.0,
             "setup": "breakdown20", "signal_date": "2026-11-09", "time_stop_date": "2026-11-18",
             "earnings": res[0]}
        m = tg.message(t, {})
        self.assertIn("🔴 🛢 <b>SHORT · USO</b> · <b>Petroleo WTI</b>", m)
        self.assertIn("Sin resultados trimestrales (materia prima)", m)


if __name__ == "__main__":
    unittest.main()
