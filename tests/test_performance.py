"""trader-analyst/performance.py: the portfolio walk, Sharpe, Sortino, PSR and the Deflated Sharpe
Ratio against hand-computed values."""

import math
import random
import unittest

from _load import load

pf = load("trader-analyst", "performance")


class Ratios(unittest.TestCase):
    def test_sharpe_sortino(self):
        x = [0.01, -0.01, 0.02, 0.0]
        m = 0.005
        sd = math.sqrt(sum((v - m) ** 2 for v in x) / 4)
        self.assertAlmostEqual(pf.sharpe(x), m / sd)
        self.assertAlmostEqual(pf.sortino(x), m / math.sqrt(0.0001 / 4))
        s = pf.stats(x)
        self.assertAlmostEqual(s["sharpe"], m / sd * math.sqrt(252))
        self.assertAlmostEqual(s["max_dd"], 0.01)

    def test_psr_and_dsr(self):
        # normal returns (skew 0, kurtosis 3): PSR = Phi(SR * sqrt(T-1) / sqrt(1 + SR^2 / 2))
        self.assertAlmostEqual(pf.psr(0.1, 101, 0.0, 3.0), pf.NORM.cdf(1.0 / math.sqrt(1.005)), places=9)
        # one trial: no luck threshold
        self.assertEqual(pf.expected_max_sharpe(1, 0.01), 0.0)
        # the threshold grows with the number of trials (Bailey & Lopez de Prado's formula)
        e100 = pf.expected_max_sharpe(100, 0.0001)
        self.assertAlmostEqual(e100, 0.01 * ((1 - pf.EULER) * pf.NORM.inv_cdf(0.99)
                                             + pf.EULER * pf.NORM.inv_cdf(1 - 1 / (100 * math.e))))
        self.assertGreater(pf.expected_max_sharpe(1000, 0.0001), e100)
        rng = random.Random(1)
        x = [rng.gauss(0.0005, 0.01) for _ in range(1000)]
        d1, _ = pf.deflated_sharpe(x, 1, 0.0001)
        d1000, sr0 = pf.deflated_sharpe(x, 1000, 0.0001)
        self.assertGreater(d1, d1000)
        self.assertGreater(sr0, 0)


class Portfolio(unittest.TestCase):
    def test_marked_daily_with_a_cap_and_one_per_symbol(self):
        cal = list(range(6))
        trades = [{"sym": "A", "start": 0, "path": [0.5, 1.0, 2.0]},     # +2R over 3 days
                  {"sym": "A", "start": 1, "path": [-1.0]},             # same symbol: skipped
                  {"sym": "B", "start": 1, "path": [-1.0]},             # max 1 open: skipped
                  {"sym": "C", "start": 3, "path": [-1.0]}]             # -1R on day 4
        r, n = pf.portfolio(trades, cal, max_positions=1, risk_pct=1.0, cost_pct=0)
        self.assertEqual(n, 2)
        self.assertEqual(len(r), 5)
        eq = 1.0
        for v in r:
            eq *= 1 + v
        self.assertAlmostEqual(eq, 1.0 + 0.02 - 0.0102, places=6)       # 1 % of 1.02 lost on C

    def test_walk_path(self):
        bars = [{"o": 100, "h": 100, "l": 100, "c": 100}, {"o": 101, "h": 103, "l": 100, "c": 102},
                {"o": 102, "h": 102, "l": 89, "c": 90}]
        path, cost = pf.walk_path(bars, 0, 15, 2.0, 5.0, cost_pct=0.1)
        self.assertEqual(path, [0.2, -1.0])
        self.assertAlmostEqual(cost, 0.01)
        bars[2]["o"] = 85
        self.assertEqual(pf.walk_path(bars, 0, 15, 2.0, 5.0)[0], [0.2, -1.5])


if __name__ == "__main__":
    unittest.main()
