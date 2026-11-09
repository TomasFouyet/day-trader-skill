"""Long or short, and two venues: a cash one (Fintual, 1x, no shorts) and a leveraged one
(Bitunix perpetuals). The max loss per trade is always a percent of the WHOLE account;
leverage only changes the margin, and the liquidation price must stay beyond the stop."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")
scan = load("trader-scanner", "scan")
size = load("trader-budget", "size")
risk = load("trader-risk", "check")
alerts = load("trader-alerts", "alerts")
er = load("trader-analyst", "earnings")
tg = load("trader-alerts", "notify_telegram")

SWDIR = os.path.join(FIX, "trader-scanner", "swing")
TODAY = "2026-11-04"
EARN = {"signal_date": "2026-11-03", "horizon_days": 7, "flag": None, "blocked": False, "next": "2027-02-09",
        "warnings": []}
VENUES = {"fintual": {"capital_pct": 70, "max_leverage": 1, "short": False, "fractional": True},
          "bitunix": {"capital_pct": 30, "max_leverage": 5, "short": True, "max_margin_per_trade_pct": 25,
                      "maintenance_margin_pct": 0.5, "fee_pct": 0.06, "liq_buffer": 1.5}}
CFG = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000, "min_reward_risk": 1.0,
       "swing": {"max_risk_per_trade_pct": 1, "max_positions": 5, "max_exposure_pct": 60, "venues": VENUES}}
LONG = {"mode": "swing", "symbol": "WELL", "name": "Welltower Inc.", "side": "buy", "date": "2026-11-03",
        "setup": "breakout20", "entry": 241.17, "stop": 228.30, "target": 266.91, "time_stop_days": 7,
        "time_stop_date": "2026-11-12", "earnings": EARN}
SHORT = dict(LONG, symbol="SUPV", name="Grupo Supervielle S.A.", side="sell", setup="breakdown20", entry=5.69,
             stop=6.52, target=4.03)


class TimeMachineShort(unittest.TestCase):
    def test_a_short_is_a_mirrored_long(self):
        down = [(1.0, 1.005, 0.97, 0.98)] * 50            # -2 % a day: the short's target comes first
        mc = tm.simulate_swing(down, 100.0, 105.0, 90.0, 7, paths=300, seed=1, side="sell")
        self.assertEqual((mc["p_target"], mc["p_stop"]), (1.0, 0.0))
        self.assertAlmostEqual(mc["expectancy_R"], 2.0)
        gap = [(1.1, 1.12, 1.08, 1.1)] * 50              # opens +10 %: through the 5 % stop
        mc = tm.simulate_swing(gap, 100.0, 105.0, 90.0, 7, paths=100, seed=1, side="sell")
        self.assertEqual((mc["p_stop"], mc["p_gap_stop"]), (1.0, 1.0))
        self.assertAlmostEqual(mc["expectancy_R"], -2.0)  # out at 110: (100 - 110) / 5
        rets = [(1.0, 1.02, 0.98, 1.0 + ((i * 7) % 11 - 5) / 400.0) for i in range(120)]
        lo = tm.simulate_swing(rets, 50.0, 47.0, 56.0, 7, 2000, 19)
        sh = tm.simulate_swing(rets, 50.0, 53.0, 44.0, 7, 2000, 19, side="sell")
        self.assertAlmostEqual(lo["p_unresolved"] + lo["p_target"] + lo["p_stop"], 1.0)
        self.assertNotEqual(lo["expectancy_R"], sh["expectancy_R"])

    def test_short_plan_on_real_data(self):
        text = tm.read_offline_daily(SWDIR, "INTC")
        plan = tm.normalize_swing_plan({"symbol": "INTC", "side": "sell", "date": "2026-10-30", "entry": 65.76,
                                        "stop": 79.48, "target": 38.32, "setup": "breakdown20", "stop_rule": "atr",
                                        "stop_param": 2.0, "atr_bars": 14, "time_stop_days": 7}, "t")
        d = tm.swing_time_machine(text, plan, tm.parse_now("2026-10-30 17:00"), paths=1000)
        self.assertEqual((d["plan"]["side"], d["history"]["setup"], d["history"]["rule"]["side"]),
                         ("sell", "breakdown20", "sell"))
        self.assertGreater(d["history"]["triggered"], 0)
        self.assertIn("corto (short)", "\n".join(tm.report_swing(d)))
        with self.assertRaises(tm.TMError):
            tm.normalize_swing_plan(dict(plan, stop=60.0), "t")


class ScannerShort(unittest.TestCase):
    def test_short_setups_and_sides(self):
        setups, p, floors = scan.swing_params({"swing": {"sides": ["long", "short"]}})
        self.assertEqual(setups, list(scan.SWING_LONG_SETUPS + scan.SWING_SHORT_SETUPS))
        self.assertEqual(scan.swing_params({})[0], list(scan.SWING_LONG_SETUPS))     # long only by default
        with self.assertRaises(scan.ScanError):
            scan.swing_params({"swing": {"sides": ["sideways"]}})
        d, _ = scan.load_offline(SWDIR)
        cands, _, _ = scan.scan_swing(d, sorted(d), scan.parse_now("2026-10-30 17:00"), setups, p, floors)
        shorts = [c for c in cands if c["side"] == "sell"]
        self.assertTrue(shorts)
        for c in shorts:
            self.assertEqual(c["direction"], "SHORT")
            self.assertIn(c["setup"], scan.SWING_SHORT_SETUPS)
            self.assertTrue(c["name"])
        intc = [c for c in cands if c["symbol"] == "INTC"][0]
        self.assertEqual(intc["setup"], "breakdown20")
        self.assertIn("minimo de 20 dias", intc["why"])


class Venues(unittest.TestCase):
    def test_long_both_venues_same_risk(self):
        s = size.size_swing(LONG, CFG, today=TODAY)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["venue"], s["risk_usd"]), ("fintual", 50.0))
        f, b = s["venues"]["fintual"], s["venues"]["bitunix"]
        self.assertEqual((f["leverage"], f["capital"]), (1, 3500.0))
        self.assertLessEqual(f["max_loss"], 50.0)
        self.assertLessEqual(b["max_loss"], 50.0)
        # $50 / 12.87 risk = 3.885 units = $937 notional; 25 % of Bitunix's $1,500 = $375 margin -> 3x
        self.assertEqual((b["leverage"], b["bound_by"]), (3, "max loss"))
        self.assertAlmostEqual(b["notional"], 936.95, places=2)
        self.assertAlmostEqual(b["margin"], 312.32, places=2)
        self.assertLess(b["liquidation"], LONG["stop"])            # liquidation beyond the stop
        self.assertEqual(b["margin_pct_of_account"], 6.2)

    def test_short_goes_to_the_leveraged_venue(self):
        s = size.size_swing(SHORT, CFG, today=TODAY)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["venue"], s["side"]), ("bitunix", "sell"))
        self.assertIn("no permite short", s["venues"]["fintual"]["refusal"])
        self.assertGreater(s["liquidation"], SHORT["stop"])
        t = size.swing_ticket(s)
        self.assertEqual((t["side"], t["venue"]), ("sell", "bitunix"))
        self.assertIn("SHORT", t["why"])
        no_short = dict(CFG, swing=dict(CFG["swing"], venues={"fintual": VENUES["fintual"],
                                                              "bitunix": dict(VENUES["bitunix"], short=False)}))
        s = size.size_swing(SHORT, no_short, today=TODAY)
        self.assertIn("no tiene short habilitado", s["refusal"])
        with self.assertRaises(size.SizeError):
            size.size_swing(SHORT, {"budget": 5000}, today=TODAY)       # no venues: no shorts at all

    def test_leverage_is_capped_by_the_liquidation_buffer(self):
        # a 14.6 % stop: liquidation must sit beyond 1.5 x 14.6 % + 0.5 % -> at most 4x
        s = size.size_swing(SHORT, CFG, today=TODAY)
        self.assertEqual(s["venues"]["bitunix"]["safe_leverage"], 4)
        tight = dict(CFG, swing=dict(CFG["swing"], venues={"bitunix": dict(VENUES["bitunix"], capital_pct=2,
                                                                          max_leverage=20)}))
        s = size.size_swing(dict(LONG, setup="breakout20"), tight, today=TODAY)
        b = s["venues"]["bitunix"]
        self.assertEqual(b["bound_by"], "liquidation")           # not 20x: the stop is 5.3 % away
        self.assertLessEqual(b["leverage"], b["safe_leverage"])
        self.assertLess(b["liquidation"], LONG["stop"])
        self.assertLess(b["max_loss"], 50.0)                      # less size, never more risk

    def test_bad_venue_config(self):
        for bad in ({"x": {"capital_pct": 80}, "y": {"capital_pct": 30}}, {"x": {"max_leverage": 0}},
                    {"x": {"liq_buffer": 0.5}}, {"x": "nope"}):
            with self.assertRaises(size.SizeError, msg=str(bad)):
                size.size_swing(LONG, dict(CFG, swing=dict(CFG["swing"], venues=bad)), today=TODAY)


class RiskLeverage(unittest.TestCase):
    def test_pass_and_blocks(self):
        t = size.swing_ticket(size.size_swing(SHORT, CFG, today=TODAY))
        res = risk.check_swing([t], CFG, today=TODAY)
        self.assertEqual(res["results"][0]["verdict"], "PASS", res["results"][0]["reasons"])
        cases = [
            (dict(t, leverage=10), "supera tu maximo 5x"),
            (dict(t, liquidation=6.40), "llega antes que el stop"),
            (dict(t, venue="fintual"), "no tiene short habilitado"),
            (dict(t, venue="kraken"), "no esta en swing.venues"),
            (dict(t, margin=2000), "margin:"),
        ]
        for tk, msg in cases:
            r = risk.check_swing([tk], CFG, today=TODAY)["results"][0]
            self.assertEqual(r["verdict"], "BLOCK", msg)
            self.assertTrue(any(msg in x for x in r["reasons"]), (msg, r["reasons"]))

    def test_min_expectancy(self):
        t = size.swing_ticket(size.size_swing(LONG, CFG, today=TODAY))
        cfg = dict(CFG, swing=dict(CFG["swing"], min_expectancy_R=0.0))
        r = risk.check_swing([t], cfg, today=TODAY)["results"][0]
        self.assertIn("sin simulacion", r["reasons"][0])
        t["timemachine"] = {"expectancy_R": -0.05}
        r = risk.check_swing([t], cfg, today=TODAY)["results"][0]
        self.assertIn("-0.05R", r["reasons"][0])
        t["timemachine"] = {"expectancy_R": 0.10}
        self.assertEqual(risk.check_swing([t], cfg, today=TODAY)["results"][0]["verdict"], "PASS")


class AlertsAndNews(unittest.TestCase):
    def test_short_alerts_ring_the_other_way(self):
        rules = alerts.rules_from_plan(dict(SHORT, time_stop_date="2026-11-12"))
        self.assertEqual([(r["id"], r["type"]) for r in rules[:2]],
                         [("supv-swing-stop", "above"), ("supv-swing-target", "below")])

    def test_only_relevant_headlines(self):
        self.assertTrue(er.relevant("Welltower raises guidance", "WELL", "Welltower Inc."))
        self.assertTrue(er.relevant("UnitedHealth (UNH) rebounds", "UNH", "UnitedHealth Group Incorporated"))
        self.assertFalse(er.relevant("SBAC Q3 FFO Top Estimates", "WELL", "Welltower Inc."))
        self.assertFalse(er.relevant("CVS Tumbles 12% on Plan to Split", "UNH", "UnitedHealth Group Incorporated"))
        self.assertFalse(er.relevant("Visa reports record earnings", "V", None))   # one letter: needs "(V)"
        self.assertTrue(er.relevant("Visa (V) reports record earnings", "V", None))
        doc = {"items": [
            {"ticker": "WELL", "touches": ["WELL"], "tags": ["earnings"], "title": "Welltower beats", "time": "2026-11-03T17:00:00+00:00"},
            {"ticker": "WELL", "touches": ["WELL"], "tags": ["earnings"], "title": "SBAC Q3 FFO Top Estimates", "time": "2026-11-03T18:00:00+00:00"}]}
        got = er.news_items(doc, "WELL", er._now("2026-11-04T00:00:00Z"), "Welltower Inc.")
        self.assertEqual([n["title"] for n in got], ["Welltower beats"])

    def test_message_and_summary(self):
        t = size.swing_ticket(size.size_swing(LONG, CFG, today=TODAY))
        t2 = size.swing_ticket(size.size_swing(SHORT, CFG, today=TODAY))
        t["timemachine"] = {"p_target": 0.02, "p_stop": 0.15, "p_unresolved": 0.83, "expectancy_R": 0.14,
                            "paths": 10000, "horizon_days": 7, "verdict": "Se inclina hacia el stop: x",
                            "history_cases": 47}
        m = tg.message(t, {})
        for must in ("🟢 <b>LONG · WELL</b> · Welltower Inc.", "▸ Fintual 1x:", "▸ Bitunix <b>3x</b>",
                     "liquidación ~$", "⭐", "→ Se inclina hacia el stop; la mayoría se decide al cierre por tiempo"):
            self.assertIn(must, m)
        m2 = tg.message(t2, {})
        self.assertIn("🔴 <b>SHORT · SUPV</b>", m2)
        self.assertIn("(vender en corto)", m2)
        self.assertIn("Fintual: <i>fintual no permite short</i>", m2)
        self.assertIn("<code>Stop     $6.52  (+14.6%)</code>", m2)
        items = tg.rank([("a", t2, {}), ("b", t, {})])
        self.assertEqual(items[0][1]["symbol"], "WELL")           # the better expectancy first
        sm = tg.summary(items, "2026-11-04")
        self.assertIn("2 setup(s) para operar (1 long, 1 short)", sm)
        self.assertIn("1. 🟢 <b>WELL</b> LONG · exp. +0.14R", sm)
        self.assertIn("<b>Swing 04-11-2026</b>", sm)
        self.assertEqual(tg.esc("<b>&"), "&lt;b&gt;&amp;")


if __name__ == "__main__":
    unittest.main()
