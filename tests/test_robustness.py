"""trader-analyst/robustness.py: the multi-horizon walk, the grid, walk-forward and the bootstrap."""

import unittest

from _load import load

rb = load("trader-analyst", "robustness")


def ev(date, path, entry=100.0, atr=5.0):
    return {"date": date, "entry": entry, "atr": atr, "fwd": rb.fwd_array([{"o": o, "h": h, "l": l, "c": c}
                                                                          for o, h, l, c in path])}


UP = [(100 + k, 101 + k, 99 + k, 100.5 + k) for k in range(1, 21)]


class Walk(unittest.TestCase):
    def test_every_horizon_in_one_pass(self):
        r = rb.outcomes([ev("2020-01-02", UP)], 2.0, None, (1, 5, 20), cost_pct=0)[0]
        self.assertEqual(r, [0.15, 0.55, 2.05])                # (c - 100) / 10 at bars 1, 5 and 20
        r = rb.outcomes([ev("2020-01-02", UP)], 2.0, 1.0, (1, 5, 20), cost_pct=0)[0]
        self.assertEqual(r, [0.15, 0.55, 1.0])                 # the 110 target on bar 9 (high 110): after H=5

    def test_stop_first_gap_and_cost(self):
        path = [(99, 100, 95, 96), (85, 86, 84, 85)] + UP
        r = rb.outcomes([ev("2020-01-02", path)], 2.0, 1.0, (1, 2, 10), cost_pct=0)[0]
        self.assertEqual(r, [-0.4, -1.5, -1.5])                # gapped through the 90 stop at 85
        r = rb.outcomes([ev("2020-01-02", path)], 2.0, 1.0, (1,), cost_pct=0.1)[0]
        self.assertAlmostEqual(r[0], -0.4 - 0.01)              # 0.1 % of 100 over a risk of 10
        short = rb.outcomes([ev("2020-01-02", UP[:3])], 2.0, None, (5,), cost_pct=0)[0]
        self.assertAlmostEqual(short[0], 0.35)                # the data ran out: the last close (103.5)


class WalkForwardAndBootstrap(unittest.TestCase):
    def setUp(self):
        good = [(100 + 2 * k, 101 + 2 * k, 99.5 + 2 * k, 100.8 + 2 * k) for k in range(1, 21)]
        bad = [(100 - k, 100.5 - k, 99 - k, 99.4 - k) for k in range(1, 21)]
        self.events = []
        for y in range(2010, 2018):
            for m in range(1, 13):
                self.events.append(dict(ev("%d-%02d-10" % (y, m), good), kind="a"))
                self.events.append(dict(ev("%d-%02d-20" % (y, m), bad), kind="b"))
        self.grid = rb.Grid(self.events, (5, 10), (1.0, 2.0), (None,), cost_pct=0)

    def test_walk_forward_picks_on_the_past_only(self):
        configs = [{"H": h, "K": k, "T": None, "kind": kind} for h in (5, 10) for k in (1.0, 2.0) for kind in "ab"]
        fixed = {"H": 5, "K": 2.0, "T": None, "kind": "b"}
        rows = rb.walk_forward(self.grid, self.grid, configs, fixed, lambda e, c: e["kind"] == c["kind"],
                               train_years=3, min_n=10)
        self.assertEqual([r["year"] for r in rows], list(range(2013, 2018)))
        for r in rows:
            self.assertEqual(r["chosen"]["kind"], "a")                 # the rising one, every year
            self.assertTrue(all(x > 0 for x in r["oos"]))
            self.assertTrue(all(x < 0 for x in r["fixed"]))
        text = "\n".join(rb.report_walk_forward(rows, fixed))
        self.assertIn("anos fuera de muestra positivos (elegida): 5 de 5", text)

    def test_bootstrap(self):
        trades = [(e["date"], 1.0 if e["kind"] == "a" else -0.5) for e in self.events]
        rand = [(e["date"], 0.0) for e in self.events]
        b = rb.bootstrap(trades, rand, n=300)
        self.assertAlmostEqual(b["mean"][1], 0.25)
        self.assertEqual(b["p_mean_le0"], 0.0)
        self.assertEqual(b["months"], 96)
        self.assertEqual(b["p_losing_year"], 0.0)
        self.assertIn("P(promedio <= 0) = 0.0%", "\n".join(rb.report_bootstrap("x", b)))
        b = rb.bootstrap([(e["date"], -0.2) for e in self.events], rand, n=200)
        self.assertEqual((b["p_mean_le0"], b["p_edge_le0"], b["p_losing_year"]), (1.0, 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
