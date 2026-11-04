"""trader-analyst earnings.py: is a quarterly report due inside the swing
horizon? Offline: the analyst's SEC fixtures (AAPL's and KO's real
submissions, newest 8-K item 2.02 on 2026-07-30 and 2026-07-28). The cadence
cases use small hand-made date lists."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

er = load("trader-analyst", "earnings")
news = load("trader-news", "news")

SEC = os.path.join(FIX, "trader-analyst")
D = datetime.date
SW = dict(er.DEFAULTS)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = er.main(argv)
    return code, out.getvalue(), err.getvalue()


class Estimate(unittest.TestCase):
    def test_one_past_report_uses_91_days(self):
        e = er.estimate([D(2026, 7, 30)], D(2026, 10, 20), 5)
        self.assertEqual((e["date"], e["lo"], e["hi"], e["known"]), (D(2026, 10, 29), D(2026, 10, 24), D(2026, 11, 3), False))
        self.assertIn("91 dias", e["basis"])

    def test_median_gap_and_the_same_week_a_year_ago(self):
        past = [D(2025, 1, 30), D(2025, 5, 1), D(2025, 7, 31), D(2025, 10, 30), D(2026, 1, 29), D(2026, 4, 30),
                D(2026, 7, 30)]
        e = er.estimate(past, D(2026, 10, 1), 5)
        self.assertEqual(e["date"], D(2026, 10, 29))          # 2025-10-30 + 364: the same Thursday
        self.assertIn("hace un ano", e["basis"])
        e = er.estimate([D(2026, 2, 10), D(2026, 5, 12), D(2026, 8, 11)], D(2026, 9, 1), 3)
        self.assertEqual(e["date"], D(2026, 11, 10))           # median gap 91 days

    def test_a_filed_report_is_known_and_an_overdue_one_is_imminent(self):
        e = er.estimate([D(2026, 7, 30), D(2026, 10, 30)], D(2026, 10, 28), 5)
        self.assertEqual((e["date"], e["known"]), (D(2026, 10, 30), True))
        e = er.estimate([D(2026, 4, 30)], D(2026, 9, 1), 5)
        self.assertTrue(e["overdue"])
        self.assertEqual((e["lo"], e["hi"]), (D(2026, 9, 1), D(2026, 9, 6)))
        self.assertIsNone(er.estimate([], D(2026, 9, 1), 5))

    def test_8k_dates_from_submissions(self):
        with open(os.path.join(SEC, "submissions-CIK0000320193.json")) as fh:
            sub = json.load(fh)
        self.assertEqual(er.earnings_8k_dates(sub), [D(2026, 7, 30)])


class Assess(unittest.TestCase):
    def test_inside_and_outside_the_horizon(self):
        r = er.assess("AAPL", D(2026, 10, 20), 7, SW, sec_dates=[D(2026, 7, 30)])
        self.assertEqual((r["horizon_end"], r["next"], r["in_horizon"], r["blocked"]), ("2026-10-29", "2026-10-29", True, True))
        self.assertEqual(r["flag"], "resultados dentro del horizonte")
        self.assertTrue(any("estimacion" in w for w in r["warnings"]))
        r = er.assess("AAPL", D(2026, 8, 3), 7, SW, sec_dates=[D(2026, 7, 30)])
        self.assertEqual((r["in_horizon"], r["blocked"], r["flag"]), (False, False, None))
        r = er.assess("AAPL", D(2026, 10, 20), 7, dict(SW, block_on_earnings=False), sec_dates=[D(2026, 7, 30)])
        self.assertEqual((r["flag"], r["blocked"]), ("resultados dentro del horizonte", False))

    def test_a_report_on_the_signal_day_counts(self):
        r = er.assess("X", D(2026, 10, 29), 5, SW, manual=[D(2026, 10, 29)])
        self.assertEqual((r["source"], r["in_horizon"], r["known"]), ("manual", True, True))
        r = er.assess("X", D(2026, 10, 30), 5, SW, manual=[D(2026, 10, 29)])   # past: no next date in the list
        self.assertIsNone(r["next"])

    def test_manual_dates_win_and_unknown(self):
        r = er.assess("AAPL", D(2026, 10, 20), 7, SW, manual=[D(2026, 11, 20)], sec_dates=[D(2026, 7, 30)])
        self.assertEqual((r["source"], r["next"], r["in_horizon"]), ("manual", "2026-11-20", False))
        r = er.assess("SPY", D(2026, 10, 20), 7, SW, sec_error="no CIK")
        self.assertEqual((r["in_horizon"], r["blocked"]), (None, False))
        self.assertIn("desconocida", r["warnings"][0])
        r = er.assess("SPY", D(2026, 10, 20), 7, dict(SW, block_on_unknown_earnings=True), sec_error="no CIK")
        self.assertEqual((r["blocked"], r["flag"]), (True, "resultados: fecha desconocida"))

    def test_horizon_skips_weekends_and_holidays(self):
        self.assertEqual(er.add_trading_days(D(2026, 11, 24), 3), D(2026, 11, 30))

    def test_news_headlines(self):
        doc = {"items": [
            {"ticker": "AAPL", "touches": ["AAPL"], "tags": ["earnings"], "title": "Apple results beat",
             "time": "2026-10-30T20:31:00+00:00"},
            {"ticker": "AAPL", "touches": ["AAPL"], "tags": ["earnings"], "title": "old", "time": "2026-10-01T20:00:00+00:00"},
            {"ticker": "KO", "touches": ["KO"], "tags": ["earnings"], "title": "KO", "time": "2026-10-30T20:00:00+00:00"},
            {"ticker": "AAPL", "touches": ["AAPL"], "tags": ["deal"], "title": "deal", "time": "2026-10-30T20:00:00+00:00"}]}
        got = er.news_flags(doc, "AAPL", datetime.datetime(2026, 10, 31, 12, 0))
        self.assertEqual(len(got), 1)
        self.assertIn("Apple results beat", got[0])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_annotates_swing_json(self):
        doc = {"mode": "swing", "candidates": [{"symbol": "AAPL", "date": "2026-10-20", "setup": "rsi2"},
                                               {"symbol": "KO", "date": "2026-08-03", "setup": "breakout20"},
                                               {"symbol": "ZZZZ", "date": "2026-10-20", "setup": "rsi2"}]}
        with open(os.path.join("trader", "swing.json"), "w") as fh:
            json.dump(doc, fh)
        code, text, err = run(["check", "--offline", SEC, "--now", "2026-10-20T21:00:00Z"])
        self.assertEqual(code, 1, err)                    # one blocked
        self.assertIn("⚠ resultados dentro del horizonte: BLOQUEADO", text)
        with open(os.path.join("trader", "swing.json")) as fh:
            out = json.load(fh)
        c = {x["symbol"]: x["earnings"] for x in out["candidates"]}
        self.assertTrue(c["AAPL"]["blocked"])
        self.assertFalse(c["KO"]["blocked"])
        self.assertIsNone(c["ZZZZ"]["in_horizon"])
        self.assertEqual(out["earnings_rules"]["block_on_earnings"], True)
        # the config can switch the block off: the flag stays, as a warning
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"swing": {"block_on_earnings": False}}, fh)
        code, text, _ = run(["check", "--offline", SEC, "--now", "2026-10-20T21:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("(aviso, no bloquea)", text)

    def test_check_a_plan_and_your_own_dates(self):
        with open(os.path.join("trader", "plan.json"), "w") as fh:
            json.dump({"mode": "swing", "symbol": "AAPL", "date": "2026-10-20", "time_stop_days": 3}, fh)
        with open(os.path.join("trader", "earnings_dates.json"), "w") as fh:
            json.dump({"AAPL": "2026-10-29"}, fh)
        code, text, err = run(["check", "--from", "trader/plan.json", "--offline", SEC, "--json"])
        self.assertEqual(code, 0, err)                    # 3 days: ends 2026-10-23, before the 29th
        with open(os.path.join("trader", "plan.json")) as fh:
            p = json.load(fh)
        self.assertEqual((p["earnings"]["source"], p["earnings"]["in_horizon"], p["earnings"]["horizon_end"]),
                         ("manual", False, "2026-10-23"))

    def test_no_user_agent_is_unknown_not_a_crash(self):
        saved = os.environ.pop("SEC_USER_AGENT", None)
        try:
            code, text, err = run(["next", "AAPL", "--date", "2026-10-20", "--json"])
        finally:
            if saved is not None:
                os.environ["SEC_USER_AGENT"] = saved
        self.assertEqual(code, 0, err)
        self.assertIn("sin SEC User-Agent", json.loads(text)[0]["warnings"][0])

    def test_news_watches_swing_candidates(self):
        with open(os.path.join("trader", "swing.json"), "w") as fh:
            json.dump({"candidates": [{"symbol": "TSLA"}]}, fh)
        self.assertIn("TSLA", news.watch_from_files("trader/portfolio.json", "trader/swing.json", "trader/plan.json"))


if __name__ == "__main__":
    unittest.main()
