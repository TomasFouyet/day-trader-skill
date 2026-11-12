"""earnings_gap (momentum de resultados): the scanner rule (a +12 % jump over SPY on 3x volume),
the SEC confirmation and the time stop cut before the next report, the setup's own exit (60 days,
3 ATR, far target), the Time Machine's same rule, the Telegram lines and one paper trade a symbol."""

import datetime
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
earn = load("trader-analyst", "earnings")
exitp = load("trader-exit", "exit")
tg = load("trader-alerts", "notify_telegram")
paper = load("trader-alerts", "paper_trader")
size = load("trader-budget", "size")
risk = load("trader-risk", "check")

TSLA_D = os.path.join(FIX, "trader-timemachine", "TSLA-5y1d.json.gz")
D = datetime.date


def sessions(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return out[::-1]


def bars(jump=0.15, vol=5.0, age=0, n=80, end=D(2026, 11, 11)):
    days = sessions(end, n)
    out = []
    for k, d in enumerate(days):
        c = 50.0 + 0.01 * k
        out.append({"date": d, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1e6})
    j = n - 1 - age
    out[j] = dict(out[j], c=out[j - 1]["c"] * (1 + jump), h=out[j - 1]["c"] * (1 + jump) * 1.01, v=vol * 1e6)
    for k in range(j + 1, n):
        c = out[j]["c"]
        out[k] = dict(out[k], o=c, h=c * 1.01, l=c * 0.99, c=c)
    return days, out


class Scanner(unittest.TestCase):
    def setUp(self):
        self.setups, self.p, self.floors = scan.swing_params({})

    def test_fires_on_a_big_jump_with_volume(self):
        days, b = bars()
        c, why = scan.swing_candidate("ABC", b, self.setups, self.p, self.floors)
        self.assertEqual(why, "hit")
        self.assertEqual(c["setup"], "earnings_gap")
        self.assertEqual((c["reaction_date"], c["reaction_pct"], c["volume_ratio"], c["vs"]),
                         (days[-1].isoformat(), 15.0, 5.0, "propio"))
        self.assertEqual(c["exit"], {"horizon_days": 60, "time_stop_days": 60, "stop": "atr:3", "target": "10R"})
        self.assertEqual(c["time_stop_days"], 60)

    def test_a_day_back_still_counts_two_days_back_does_not(self):
        days, b = bars(age=1)
        c, _ = scan.swing_candidate("ABC", b, self.setups, self.p, self.floors)
        self.assertEqual((c["setup"], c["reaction_date"], c["date"]), ("earnings_gap", days[-2].isoformat(),
                                                                       days[-1].isoformat()))
        _, b = bars(age=2)
        c, _ = scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors)
        self.assertIsNone(c)

    def test_needs_the_volume_and_the_jump_over_spy(self):
        _, b = bars(vol=2.0)
        self.assertIsNone(scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors)[0])
        _, b = bars(jump=0.10)
        self.assertIsNone(scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors)[0])
        days, b = bars(jump=0.15)
        spy = {d: 400.0 for d in days}
        spy[days[-1]] = 420.0       # SPY +5 % that day: the stock beat it by 10 points only
        self.assertIsNone(scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors, spy)[0])
        spy[days[-1]] = 400.0
        c, _ = scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors, spy)
        self.assertEqual(c["vs"], "SPY")

    def test_config_overrides_the_exit(self):
        ex = scan.swing_exits({"swing": {"setup_exits": {"earnings_gap": {"horizon_days": 40, "stop": "atr:2.5"}}}})
        self.assertEqual(ex["earnings_gap"], {"horizon_days": 40, "time_stop_days": 60, "stop": "atr:2.5",
                                              "target": "10R"})
        with self.assertRaises(scan.ScanError):
            scan.swing_exits({"swing": {"setup_exits": {"earnings_gap": {"flat_by": "15:00"}}}})
        _, b = bars()
        c, _ = scan.swing_candidate("ABC", b, ["earnings_gap"], self.p, self.floors, None, ex)
        self.assertEqual(c["exit"]["stop"], "atr:2.5")

    def test_the_time_machine_uses_the_same_rule(self):
        for kw in ({}, {"vol": 2.0}, {"jump": 0.10}, {"age": 1}):
            _, b = bars(**kw)
            ind_s, ind_t = scan.swing_indicators(b, scan.SWING_PARAMS), tm.indicators(b)
            for i in range(len(b)):
                self.assertEqual(scan.swing_fires("earnings_gap", b, i, ind_s, scan.SWING_PARAMS),
                                 tm.setup_fires("earnings_gap", b, i, ind_t), (kw, i))

    def test_comodines_never_get_it(self):
        days, b = bars()
        series = {"t": [int(datetime.datetime(d.year, d.month, d.day, 20, 0).timestamp()) for d in days],
                  "o": [x["o"] for x in b], "h": [x["h"] for x in b], "l": [x["l"] for x in b],
                  "c": [x["c"] for x in b], "v": [x["v"] for x in b]}
        now = scan.parse_now("2026-11-12 08:30")
        cands, _, _ = scan.scan_swing({"GLD": series, "ABC": series}, ["ABC", "GLD"], now, self.setups, self.p,
                                      self.floors, extras=["GLD"])
        # GLD's jump is a plain 20-day breakout, never an earnings_gap
        self.assertEqual([(c["symbol"], c["setup"]) for c in cands], [("ABC", "earnings_gap"), ("GLD", "breakout20")])


SW = {"block_on_earnings": True, "block_on_unknown_earnings": False, "earnings_margin_days": 5,
      "horizon_days": 7, "time_stop_days": None}
PAST = [D(2025, 11, 4), D(2026, 2, 3), D(2026, 5, 5), D(2026, 8, 4)]


class Earnings(unittest.TestCase):
    def test_confirmed_and_cut_before_the_next_report(self):
        # reported after the close on Nov 10, jumped Nov 11, signal Nov 11
        res = earn.assess_gap("ABC", D(2026, 11, 11), D(2026, 11, 11), 60, SW, None, PAST + [D(2026, 11, 10)])
        self.assertFalse(res["blocked"])
        self.assertEqual(res["confirmed"], "8-K 2.02 del 2026-11-10")
        # next report: the same week as a year back (Feb 3 + 364 = Feb 2), window from Jan 28: the last
        # session before it is Jan 27
        self.assertEqual((res["next"], res["window"][0]), ("2027-02-02", "2027-01-28"))
        self.assertEqual(res["horizon_end"], "2027-01-27")
        self.assertLess(res["horizon_days"], 60)
        self.assertTrue(any("recortado" in w for w in res["warnings"]))

    def test_no_8k_blocks(self):
        res = earn.assess_gap("ABC", D(2026, 11, 11), D(2026, 11, 11), 60, SW, None, PAST)
        self.assertTrue(res["blocked"])
        self.assertEqual(res["flag"], earn.GAP_FLAG)
        res = earn.assess_gap("ABC", D(2026, 11, 11), D(2026, 11, 11), 60, SW, None, None, "SEC caido")
        self.assertTrue(res["blocked"])
        self.assertIn("SEC caido", res["flag"])

    def test_annotate_writes_the_cut_into_the_candidate(self):
        class Sec(object):
            def dates(self, sym):
                return PAST + [D(2026, 11, 10)], None
        it = {"symbol": "ABC", "date": "2026-11-11", "setup": "earnings_gap", "reaction_date": "2026-11-11",
              "time_stop_days": 60, "exit": {"horizon_days": 60, "time_stop_days": 60, "stop": "atr:3", "target": "10R"}}
        res = earn.annotate([it], SW, Sec(), {})[0]
        self.assertEqual(it["time_stop_days"], res["horizon_days"])
        self.assertEqual(it["exit"]["time_stop_days"], res["horizon_days"])
        self.assertTrue(any("confirmado" in ln for ln in earn.describe(res)))


class ExitPlan(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        with open("trader/config.json", "w") as fh:
            json.dump({"mode": "swing"}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_the_setup_brings_its_own_exit(self):
        er = {"signal_date": "2026-10-30", "horizon_days": 55, "blocked": False, "flag": None,
              "confirmed": "8-K 2.02 del 2026-10-29"}
        cand = {"candidates": [{"symbol": "TSLA", "date": "2026-10-30", "setup": "earnings_gap", "side": "buy",
                                "reaction_date": "2026-10-30", "reaction_pct": 13.1, "volume_ratio": 3.4, "vs": "SPY",
                                "exit": {"horizon_days": 60, "time_stop_days": 55, "stop": "atr:3", "target": "10R"},
                                "earnings": er}]}
        with open("trader/swing.json", "w") as fh:
            json.dump(cand, fh)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = exitp.main(["plan", "--from", "trader/swing.json", "--pick", "TSLA", "--now", "2026-11-02 09:00",
                               "--offline", TSLA_D, "--json"])
        self.assertEqual(code, 0, err.getvalue())
        p = json.loads(out.getvalue())
        self.assertEqual((p["setup"], p["horizon_days"], p["time_stop_days"], p["stop_param"], p["target_r"]),
                         ("earnings_gap", 60, 55, 3.0, 10.0))
        self.assertEqual(p["reaction"]["reaction_pct"], 13.1)
        self.assertEqual(p["earnings"]["confirmed"], "8-K 2.02 del 2026-10-29")


class Telegram(unittest.TestCase):
    def test_lines(self):
        t = {"setup": "earnings_gap", "reaction": {"reaction_date": "2026-11-11", "reaction_pct": 14.2,
                                                   "volume_ratio": 4.1, "vs": "SPY"},
             "earnings": {"confirmed": "8-K 2.02 del 2026-11-10"}}
        text = "\n".join(tg.gap_lines(t))
        self.assertIn("+14.2%</b> sobre el S&amp;P 500 con volumen <b>4.1x</b>", text)
        self.assertIn("8-K 2.02 del 2026-11-10", text)
        self.assertIn("+0,27R", text)
        self.assertEqual(tg.gap_lines({"setup": "rsi2"}), [])


EARN = {"signal_date": "2026-11-03", "horizon_days": 55, "flag": None, "blocked": False, "next": "2027-01-28",
        "warnings": [], "confirmed": "8-K 2.02 del 2026-11-02", "setup": "earnings_gap"}
CFG = {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 4000, "min_reward_risk": 1.0,
       "swing": {"max_risk_per_trade_pct": 1, "max_positions": 5, "max_exposure_pct": 60, "min_expectancy_R": 0.0}}
PLAN = {"mode": "swing", "symbol": "WELL", "name": "Welltower Inc.", "side": "buy", "date": "2026-11-03",
        "setup": "earnings_gap", "entry": 241.17, "stop": 211.20, "target": 540.87, "time_stop_days": 55,
        "time_stop_date": "2027-01-22", "earnings": EARN,
        "reaction": {"reaction_date": "2026-11-03", "reaction_pct": 12.6, "volume_ratio": 3.2, "vs": "SPY"}}


class RiskAndMessage(unittest.TestCase):
    def ticket(self, **tm_):
        t = size.swing_ticket(size.size_swing(PLAN, CFG, today="2026-11-04"))
        t["timemachine"] = dict({"expectancy_R": -0.07, "p_target": 0.0, "p_stop": 0.60, "p_unresolved": 0.40,
                                 "p_win": 0.35, "horizon_days": 55, "paths": 10000,
                                 "verdict": "Claramente hacia el stop: ..."}, **tm_)
        return t

    def test_risk_judges_it_by_the_study_not_the_simulation(self):
        t = self.ticket()
        r = risk.check_swing([t], CFG, today="2026-11-04")["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        cfg = dict(CFG, swing=dict(CFG["swing"], min_expectancy_R=0.3))
        r = risk.check_swing([t], cfg, today="2026-11-04")["results"][0]
        self.assertIn("el estudio da +0.27R", r["reasons"][0])
        other = dict(t, setup="breakout20")
        r = risk.check_swing([other], CFG, today="2026-11-04")["results"][0]
        self.assertIn("el Time Machine da -0.07R", r["reasons"][0])

    def test_the_message(self):
        t = self.ticket()
        self.assertEqual(t["reaction"]["reaction_pct"], 12.6)
        text = tg.message(t, {})
        self.assertIn("Momentum de resultados", text)
        self.assertIn("+12.6%</b> sobre el S&amp;P 500", text)
        self.assertIn("8-K 2.02 del 2026-11-02", text)
        self.assertIn("lejano: la salida real es el stop o el tiempo", text)
        self.assertIn("mide el riesgo, no la ventaja", text)
        self.assertNotIn("Claramente hacia el stop", text)
        self.assertNotIn("Winrate estimado", text)


class Paper(unittest.TestCase):
    def test_one_live_trade_a_symbol(self):
        t1 = {"symbol": "ABC", "side": "buy", "qty": 3, "entry": 57.5, "stop": 50.0, "target": 132.5,
              "setup": "earnings_gap", "signal_date": "2026-11-11", "time_stop_date": "2027-02-03", "time_stop_days": 56}
        t2 = dict(t1, signal_date="2026-11-12")
        state = {"trades": {paper.key_of(t1): {"symbol": "ABC", "status": "open", "signal_date": "2026-11-11"}}}
        risk = {"date": "2026-11-12", "results": [{"verdict": "PASS", "fingerprint": tg.swing_fingerprint(t2)}]}
        res = paper.submit(None, {"tickets": [t2]}, risk, state, "2026-11-12", dry_run=True)
        self.assertEqual(res, [("ABC", "omitido: ya hay una operacion viva en ABC")])


if __name__ == "__main__":
    unittest.main()
