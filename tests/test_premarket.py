"""premarket.py and the morning run. Fixtures: Yahoo 1-minute charts with pre-market bars
(range=1d, interval=1m, includePrePost=true) for AAPL, MSFT and ABNB, fetched on Mon 2026-11-09
at 09:05 New York."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

pm = load("trader-alerts", "premarket")
tg = load("trader-alerts", "notify_telegram")
pipe = load("trader-alerts", "swing_pipeline")

PRE = os.path.join(FIX, "trader-alerts", "premarket")
SWDIR = os.path.join(FIX, "trader-scanner", "swing")
SEC = os.path.join(FIX, "trader-analyst")
NOW = pm.parse_now("2026-11-09 09:05")
MSFT = {"symbol": "MSFT", "side": "buy", "qty": 1.5743, "entry": 530.61, "stop": 498.85, "target": 594.13,
        "max_loss": 50.0, "reward_risk": 2.0, "signal_date": "2026-11-06", "time_stop_date": "2026-11-17",
        "setup": "pullback_ema20", "account": 5000, "venue": "fintual",
        "venues": {"fintual": {"venue": "fintual", "qty": 1.5743, "cost": 835.34, "capital": 3500},
                   "bitunix": {"venue": "bitunix", "leveraged": True, "qty": 1.574, "notional": 835.18, "leverage": 3,
                               "margin": 278.39, "liquidation": 356.39, "capital": 1500}}}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Price(unittest.TestCase):
    def test_latest_pre_market_bar(self):
        p, at, phase = pm.latest_price(pm.read_offline(PRE, "MSFT"), "MSFT", NOW)
        self.assertEqual((round(p, 2), at, phase), (542.07, "09:03", "pre"))
        p, at, _ = pm.latest_price(pm.read_offline(PRE, "AAPL"), "AAPL", pm.parse_now("2026-11-09 08:00"))
        self.assertEqual(at, "08:00")                         # nothing after the clock
        self.assertEqual(pm.latest_price(pm.read_offline(PRE, "AAPL"), "AAPL", pm.parse_now("2026-11-10 09:00")),
                         (None, None, None))                  # no bars today
        with self.assertRaises(pm.PremarketError):
            pm.latest_price(pm.read_offline(PRE, "AAPL"), "MSFT", NOW)


class Assess(unittest.TestCase):
    def test_valid_gap_up_sizes_down_never_up(self):
        b = pm.assess(MSFT, 542.07, "09:03", "pre")
        self.assertEqual((b["status"], b["gap_pct"], b["reward_risk"]), ("ok", 2.16, 1.2))
        self.assertEqual(b["risk_per_share"], 43.22)
        self.assertLessEqual(b["max_loss"], 50.0)
        self.assertLess(b["qty"], MSFT["qty"])
        self.assertAlmostEqual(b["venues"]["bitunix"]["liquidation"], 356.39 * 542.07 / 530.61, places=2)
        down = pm.assess(MSFT, 520.0, "09:03", "pre")        # closer to the stop: smaller risk, same size
        self.assertEqual(down["qty"], MSFT["qty"])
        self.assertEqual(down["status"], "ok")

    def test_invalid_and_warn(self):
        self.assertEqual(pm.assess(MSFT, 495.0, "09:00", "pre")["status"], "invalid")      # through the stop
        self.assertIn("paso el stop", pm.assess(MSFT, 495.0, "09:00", "pre")["note"])
        self.assertEqual(pm.assess(MSFT, 600.0, "09:00", "pre")["status"], "invalid")      # past the target
        rr = pm.assess(MSFT, 565.0, "09:00", "pre")                                        # R:R 0.44
        self.assertEqual(rr["status"], "invalid")
        self.assertIn("bajo tu minimo", rr["note"])
        w = pm.assess(MSFT, 510.0, "09:00", "pre", gap_warn_pct=3)                         # -3.9 %
        self.assertEqual(w["status"], "warn")
        self.assertEqual(pm.assess(MSFT, None, None, None)["status"], "unknown")
        short = dict(MSFT, side="sell", entry=100.0, stop=110.0, target=80.0, qty=5, max_loss=50.0, venues={})
        self.assertEqual(pm.assess(short, 111.0, "09:00", "pre")["status"], "invalid")
        self.assertEqual(pm.assess(short, 98.0, "09:00", "pre")["status"], "ok")


class Message(unittest.TestCase):
    def test_message_uses_the_pre_market_price(self):
        t = dict(MSFT, premarket=pm.assess(MSFT, 542.07, "09:03", "pre"))
        m = tg.message(t, {})
        self.assertIn("🌅 Pre-mercado 09:03 ET: <b>$542.07</b> (+2.2% vs cierre) ✅ sigue válido", m)
        self.assertIn("<code>Entrada  $542.07</code> pre-mercado (cierre $530.61)", m)
        self.assertIn("1.2R", m)
        bad = dict(MSFT, premarket=pm.assess(MSFT, 495.0, "09:00", "pre"))
        sm = tg.summary([("a", t, {})], "2026-11-09", [("b", bad, {})])
        self.assertIn("pre +2.2% ✅", sm)
        self.assertIn("Descartados en el pre-mercado", sm)
        self.assertIn("<b>MSFT</b>: el precio ya paso el stop", sm)


class MorningRun(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        with open(os.path.join(ROOT, "trader-budget", "config.swing.example.json")) as fh:
            cfg = json.load(fh)
        with open("trader/config.json", "w") as fh:
            json.dump(cfg, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_monday_morning_on_fridays_close(self):
        code, text, err = run(pipe, ["--dry-run", "--now", "2026-11-02 09:05", "--offline-charts", SWDIR,
                                     "--offline-sec", SEC, "--offline-premarket", PRE, "--skip-news",
                                     "--replays", "500"])
        self.assertEqual(code, 0, err + text)
        self.assertIn("antes de la apertura", text)
        self.assertIn("== pre-mercado", text)
        self.assertIn("señal del 30-10-2026", text)            # Friday's close, planned on Monday
        self.assertIn("Pre-mercado: ❔ sin dato", text)         # the fixtures are from another day
        with open("trader/swing_orders.json") as fh:
            tickets = json.load(fh)["tickets"]
        self.assertTrue(all(t["premarket"]["status"] == "unknown" for t in tickets))
        code, text, _ = run(pipe, ["--now", "2026-11-02 12:30"])
        self.assertIn("08:15 a 11:59", text)


if __name__ == "__main__":
    unittest.main()
