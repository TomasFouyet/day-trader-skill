"""Swing mode through trader-budget, trader-risk and trader-alerts, and the
whole daily flow offline: scanner -> earnings -> exit -> time machine ->
budget -> risk. Nothing here can place an order: swing tickets go to
trader/swing_orders.json, which trader-broker never reads."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

size = load("trader-budget", "size")
risk = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
scan = load("trader-scanner", "scan")
exitp = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
er = load("trader-analyst", "earnings")

SWDIR = os.path.join(FIX, "trader-scanner", "swing")
SEC = os.path.join(FIX, "trader-analyst")
CFG = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000, "min_order_usd": 5,
       "min_reward_risk": 1.0, "mode": "swing",
       "swing": {"max_risk_per_trade_pct": 1.0, "max_positions": 3, "max_exposure_pct": 60}}
EARN_OK = {"signal_date": "2026-10-30", "horizon_days": 7, "in_horizon": False, "blocked": False, "flag": None,
           "next": "2027-01-28", "warnings": []}
PLAN = {"mode": "swing", "symbol": "WMT", "side": "buy", "date": "2026-10-30", "setup": "breakout20",
        "entry": 123.51, "stop": 118.22, "target": 134.09, "time_stop_days": 7, "time_stop_date": "2026-11-10",
        "earnings": EARN_OK}
TODAY = "2026-11-02"


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def ticket(**kw):
    s = size.size_swing(dict(PLAN, **kw), CFG, today=TODAY)
    return size.swing_ticket(s)


class Budget(unittest.TestCase):
    def test_max_loss_is_a_percent_of_the_account(self):
        s = size.size_swing(PLAN, CFG, today=TODAY)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["risk_per_share"], s["qty"], s["bound_by"]), (5.29, 9, "max loss"))   # $50 / 5.29
        self.assertEqual((s["max_loss"], s["cost"]), (47.61, 1111.59))
        t = size.swing_ticket(s)
        self.assertEqual((t["manual"], t["time_stop_date"], t["signal_date"]), (True, "2026-11-10", "2026-10-30"))
        self.assertNotIn("order_class", t)              # not a broker order
        text = size.swing_report(s, t)
        self.assertIn("A MANO", text)
        self.assertIn("no asesoria financiera", text)

    def test_exposure_positions_and_held(self):
        pf = {"cash": 2500, "positions": [{"ticker": "AAPL", "shares": 5, "price": 344.63, "swing": True},
                                          {"ticker": "VTI", "shares": 3, "price": 300}]}
        s = size.size_swing(PLAN, CFG, portfolio=pf, today=TODAY)
        self.assertEqual(s["open_positions"], ["AAPL"])          # only the tagged swing positions
        self.assertEqual(s["account"], round(2500 + 5 * 344.63 + 900, 2))
        s = size.size_swing(dict(PLAN, entry=50.0, stop=49.0, target=52.0), CFG, portfolio=pf, today=TODAY)
        self.assertEqual(s["bound_by"], "exposure")              # 60% of 5123.15 minus 1723.15 = 1350.74 -> 27
        self.assertEqual(s["qty"], 27)
        s = size.size_swing(dict(PLAN, symbol="AAPL"), CFG, portfolio=pf, today=TODAY)
        self.assertIn("ya tienes AAPL", s["refusal"])
        full = {"cash": 4000, "positions": [{"ticker": t, "shares": 1, "price": 10, "swing": True} for t in ("A", "B", "C")]}
        s = size.size_swing(PLAN, CFG, portfolio=full, today=TODAY)
        self.assertIn("tu maximo es 3", s["refusal"])

    def test_refusals(self):
        s = size.size_swing(dict(PLAN, earnings=dict(EARN_OK, blocked=True, flag="resultados dentro del horizonte",
                                                     next="2026-11-04")), CFG, today=TODAY)
        self.assertIn("BLOQUEADO", s["refusal"])
        s = size.size_swing(PLAN, CFG, today="2026-11-11")
        self.assertIn("ya paso", s["refusal"])
        s = size.size_swing(PLAN, CFG, today="2026-11-05")
        self.assertIn("vuelve a escanear", s["refusal"])
        s = size.size_swing(dict(PLAN, stop=60.0, target=250.0), CFG, today=TODAY)
        self.assertIn("ni una accion", s["refusal"])
        with self.assertRaises(size.SizeError):
            size.size_swing(dict(PLAN, side="sell"), CFG, today=TODAY)

    def test_fractional(self):
        s = size.size_swing(PLAN, dict(CFG, fractional=True), today=TODAY)
        self.assertEqual(s["qty"], 9.4517)                      # 50 / 5.29, to 0.0001
        self.assertLessEqual(s["max_loss"], 50.0)

    def test_attaches_the_time_machine_odds(self):
        doc = {"mode": "swing", "plan": {"symbol": "WMT", "date": "2026-10-30"}, "verdict": "Cercano...",
               "mc": {"p_target": 0.3, "p_stop": 0.32, "p_unresolved": 0.38, "expectancy_R": 0.05, "horizon_days": 7,
                      "paths": 10000, "seed": 19}, "history": {"triggered": 12, "hit_rate": 0.25, "avg_R": 0.1,
                                                                 "few_cases": True}}
        s = size.size_swing(PLAN, CFG, today=TODAY, tm=doc)
        self.assertEqual((s["timemachine"]["p_stop"], s["timemachine"]["few_cases"]), (0.32, True))
        s = size.size_swing(dict(PLAN, symbol="XOM"), CFG, today=TODAY, tm=doc)
        self.assertIsNone(s["timemachine"])


class Risk(unittest.TestCase):
    def test_pass_and_the_cumulative_limits(self):
        t1 = ticket()
        t2 = size.swing_ticket(size.size_swing(dict(PLAN, symbol="XOM", entry=187.25, stop=178.97, target=203.81),
                                               CFG, today=TODAY))
        t3 = size.swing_ticket(size.size_swing(dict(PLAN, symbol="AAPL", entry=344.63, stop=331.23, target=371.43),
                                               CFG, today=TODAY))
        t4 = size.swing_ticket(size.size_swing(dict(PLAN, symbol="NVDA", entry=204.64, stop=191.56, target=230.80),
                                               CFG, today=TODAY))
        res = risk.check_swing([t1, t2, t3, t4], CFG, today=TODAY)
        # 60 % of $5,000 = $3,000: WMT $1,111.59 + XOM $1,123.50 + AAPL $1,033.89 is over; NVDA $613.92 fits
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS", "BLOCK", "PASS"])
        self.assertTrue(res["results"][2]["reasons"][0].startswith("exposure:"))
        res = risk.check_swing([t1, t2, t3, t4], dict(CFG, swing=dict(CFG["swing"], max_exposure_pct=90)), today=TODAY)
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertTrue(any(x.startswith("positions:") for x in res["results"][3]["reasons"]))
        cfg = dict(CFG, swing=dict(CFG["swing"], max_positions=10, max_exposure_pct=50))
        res = risk.check_swing([t1, t2, t3], cfg, today=TODAY)
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS", "BLOCK"])
        self.assertTrue(res["results"][2]["reasons"][0].startswith("exposure:"))

    def test_blocks(self):
        t = ticket()
        cases = [
            (dict(t, qty=20), "loss:"),
            (dict(t, qty=9.5), "no es un numero entero"),
            (dict(t, stop=130.0), "stop < entrada < objetivo"),
            (dict(t, time_stop_date=None), "sin time_stop_date"),
            (dict(t, earnings=None), "sin revision de resultados"),
            (dict(t, earnings=dict(EARN_OK, blocked=True, flag="resultados dentro del horizonte")), "earnings:"),
            (dict(t, earnings=dict(EARN_OK, signal_date="2026-10-01")), "otra senal"),
            (dict(t, side="sell"), "no tiene short habilitado"),
            (dict(t, target=125.0), "reward:risk"),
        ]
        for tk, msg in cases:
            res = risk.check_swing([tk], CFG, today=TODAY)
            self.assertEqual(res["results"][0]["verdict"], "BLOCK", msg)
            self.assertTrue(any(msg in r for r in res["results"][0]["reasons"]), (msg, res["results"][0]["reasons"]))
        res = risk.check_swing([t], CFG, today="2026-11-11")
        self.assertIn("stale", res["results"][0]["reasons"][0])
        res = risk.check_swing([t, t], CFG, today=TODAY)
        self.assertIn("dos veces", res["results"][1]["reasons"][0])
        res = risk.check_swing([t], dict(CFG, blocked=["wmt"]), today=TODAY)
        self.assertIn("blocked", res["results"][0]["reasons"][0])
        pf = {"cash": 1000, "positions": [{"ticker": "WMT", "shares": 1, "price": 120}]}
        res = risk.check_swing([t], CFG, portfolio=pf, today=TODAY)
        self.assertTrue(any(r.startswith("held:") for r in res["results"][0]["reasons"]))
        self.assertTrue(any(r.startswith("cash:") for r in res["results"][0]["reasons"]))

    def test_unblocked_earnings_flag_is_a_warning(self):
        cfg = dict(CFG, swing=dict(CFG["swing"], block_on_earnings=False))
        res = risk.check_swing([dict(ticket(), earnings=None)], cfg, today=TODAY)
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        res = risk.check_swing([dict(ticket(), earnings=dict(EARN_OK, flag="resultados dentro del horizonte"))],
                               CFG, today=TODAY)
        self.assertEqual(res["results"][0]["verdict"], "PASS")
        self.assertTrue(any("resultados dentro del horizonte" in w for w in res["results"][0]["warnings"]))


class Alerts(unittest.TestCase):
    def test_swing_plan_rules(self):
        rules = alerts.rules_from_plan(PLAN, "15:55")
        ids = [r["id"] for r in rules]
        self.assertEqual(ids, ["wmt-swing-stop", "wmt-swing-target", "wmt-time-stop"])
        for r in rules:
            alerts.validate(r)
        stop = rules[0]
        self.assertEqual((stop["from"], stop["until"]), ("2026-10-30", "2026-11-10"))
        self.assertTrue(alerts.active(stop, "2026-11-05"))
        self.assertFalse(alerts.active(stop, "2026-11-11"))
        self.assertFalse(alerts.active(stop, "2026-10-29"))
        self.assertEqual((rules[2]["date"], rules[2]["type"]), ("2026-11-10", "time"))
        self.assertNotIn("flat", json.dumps(rules))
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(dict(PLAN, time_stop_date=None))

    def test_intraday_plan_rules_unchanged(self):
        rules = alerts.rules_from_plan({"symbol": "TSLA", "stop": 358.32, "target": 370.04, "date": "2026-10-19"})
        self.assertEqual([r["id"] for r in rules], ["tsla-plan-stop", "tsla-plan-target", "tsla-flat-by"])


class DailyFlow(unittest.TestCase):
    """The whole swing day, offline, as the Telegram job will run it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        with open(os.path.join(ROOT, "trader-budget", "config.swing.example.json")) as fh:
            cfg = json.load(fh)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_scan_to_risk(self):
        now = "2026-10-30 17:00"
        self.assertEqual(run(scan, ["swing", "--offline", SWDIR, "--now", now, "--quiet"])[0], 0)
        code, text, _ = run(er, ["check", "--offline", SEC, "--now", "2026-10-30T21:00:00Z"])
        self.assertEqual(code, 1)                                  # AAPL's estimated report is inside 7 days
        self.assertIn("AAPL", text)
        verdicts = {}
        for sym in ("AAPL", "WMT", "XOM"):
            code, _, err = run(exitp, ["plan", "--from", "trader/swing.json", "--pick", sym, "--offline", SWDIR,
                                       "--now", now, "--write"])
            self.assertEqual(code, 0, err)
            code, _, err = run(tm, ["run", "--offline", os.path.join(SWDIR, sym + ".json.gz"), "--now", now,
                                    "--write", "--replays", "2000"])
            self.assertEqual(code, 0, err)
            verdicts[sym] = run(size, ["--now", "2026-11-02 09:00", "--write"])[0]
        self.assertEqual(verdicts, {"AAPL": 1, "WMT": 0, "XOM": 0})   # AAPL: blocked by the earnings check
        self.assertFalse(os.path.exists(os.path.join("trader", "orders.json")))   # nothing for trader-broker
        with open(os.path.join("trader", "swing_orders.json")) as fh:
            doc = json.load(fh)
        self.assertEqual([t["symbol"] for t in doc["tickets"]], ["WMT", "XOM"])
        self.assertTrue(all(t["timemachine"] and t["timemachine"]["paths"] == 2000 for t in doc["tickets"]))
        code, text, err = run(risk, ["--now", "2026-11-02 09:00"])     # "mode": "swing" in the config
        self.assertEqual(code, 1, err + text)
        # Fintual holds 70 % of $5,000; 60 % of that is $2,100: WMT fits, XOM on top does not
        self.assertIn("1 pasan, 1 bloqueados", text)
        self.assertIn("supera el 60% del capital de fintual ($2,100)", text)
        with open(os.path.join("trader", "state", "swing_risk.json")) as fh:
            rec = json.load(fh)
        self.assertEqual((rec["mode"], rec["pass"]), ("swing", 1))
        self.assertFalse(os.path.exists(os.path.join("trader", "state", "risk.json")))
        code, text, _ = run(alerts, ["add", "--from-plan"])
        self.assertEqual(code, 0)
        self.assertIn("xom-time-stop", text)


if __name__ == "__main__":
    unittest.main()
