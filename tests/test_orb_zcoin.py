"""trader-analyst/orb_zcoin.py: Zcoin's opening box (retest entry, 12:00 deadline, 50% at 1R, divergences)."""

import datetime
import unittest

from _load import load

zc = load("trader-analyst", "orb_zcoin")
DAY = datetime.date(2026, 11, 18)


def day_bars(moves, base=100.0, start="08:00", end="15:55"):
    out, t = [], datetime.datetime.combine(DAY, datetime.time(*map(int, start.split(":"))))
    stop = datetime.datetime.combine(DAY, datetime.time(*map(int, end.split(":"))))
    while t <= stop:
        o, h, l, c = moves.get(t.strftime("%H:%M"), (base, base + 0.1, base - 0.1, base))
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        t += datetime.timedelta(minutes=5)
    return out


BOX = {"08:30": (100, 101, 99, 100)}            # box 99-101 from 08:30


def above_after(moves, after="10:10", level=(102, 102.2, 101.8, 102)):
    """Bars after the signal sit above the box unless a test sets them."""
    for h in range(10, 16):
        for m in range(0, 60, 5):
            k = "%02d:%02d" % (h, m)
            if k > after and k not in moves:
                moves[k] = level
    return moves


class Rules(unittest.TestCase):
    def test_retest_then_half_at_1r_and_breakeven(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6),     # closes above: the signal
                             "10:20": (101.5, 101.6, 100.9, 101.2),     # back to the edge: filled at 101
                             "10:40": (101.5, 103.2, 101.5, 103.0),     # 1R = 2 points: 103 -> half out
                             "11:00": (102, 102, 100.8, 101.0)})        # back to the entry: the rest at breakeven
        moves["10:15"] = (101.7, 101.9, 101.5, 101.8)
        bars = day_bars(above_after(moves))
        t, = zc.run_symbol(zc.Context(bars), dict(zc.ZC))
        self.assertAlmostEqual(t[1], 0.5)                                   # 50% at +1R, 50% at 0
        self.assertEqual(t[3], "parcial+breakeven")

    def test_no_retest_before_noon_no_trade(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6)})
        for h in range(10, 16):
            for m in range(0, 60, 5):
                k = "%02d:%02d" % (h, m)
                if k > "10:10":
                    moves[k] = (102, 102.2, 101.8, 102)
        moves["12:10"] = (101.5, 101.6, 100.9, 101.2)                   # the retest comes after 12:00
        self.assertEqual(zc.run_symbol(zc.Context(day_bars(moves)), dict(zc.ZC)), [])

    def test_stop_at_the_other_edge(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6),
                             "10:20": (101.5, 101.6, 100.9, 101.2),
                             "10:30": (100.5, 100.6, 98.8, 99.0)})        # through 99: -1R
        moves["10:15"] = (101.7, 101.9, 101.5, 101.8)
        t, = zc.run_symbol(zc.Context(day_bars(above_after(moves, level=(100.5, 100.6, 98.8, 99.0)))), dict(zc.ZC))
        self.assertAlmostEqual(t[1], -1.0)

    def test_close_entry_measures_from_the_close(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6),
                             "10:40": (101.6, 105.0, 101.6, 104.0)})      # 1.7R from 101.6 (risk 2.6) = 106.02: no
        cfg = dict(zc.OURS, box_start="08:30")
        t, = zc.run_symbol(zc.Context(day_bars(moves)), cfg)
        self.assertEqual(t[3], "tiempo")


class Divergence(unittest.TestCase):
    def test_bearish_divergence(self):
        closes = [100 + k for k in range(20)] + [118, 116, 117, 125, 119, 118, 117]
        highs = list(closes)
        highs[23] = 125.0                                                   # a higher high
        rs = [None] * 20 + [80, 60, 70, 65, 55, 50, 45]                     # with a lower RSI than at bar 19
        rs[19] = 90
        bear, _ = zc.divergences(highs, [c - 1 for c in closes], rs)
        self.assertIn(25, bear)                                             # confirmed 2 bars after the pivot

    def test_rsi_bounds(self):
        r = zc.rsi([100 + (k % 3) for k in range(40)])
        self.assertTrue(all(0 <= v <= 100 for v in r if v is not None))


class BoxSize(unittest.TestCase):
    def test_a_wide_box_is_skipped(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6), "10:15": (101.7, 101.9, 101.5, 101.8),
                             "10:20": (101.5, 101.6, 100.9, 101.2)})
        bars = day_bars(above_after(moves))
        self.assertEqual(zc.run_symbol(zc.Context(bars), dict(zc.ZC, max_pct=1.0)), [])     # box 2% of the price
        self.assertEqual(len(zc.run_symbol(zc.Context(bars), dict(zc.ZC, max_pct=3.0))), 1)


class SecondVideo(unittest.TestCase):
    def test_tranches_1r_25r_5r(self):
        moves = dict(BOX, **{"10:10": (100.5, 101.8, 100.5, 101.6),     # signal close 101.6, stop 99: 1R 2.6
                             "10:15": (101.7, 101.9, 101.5, 101.6),     # back to 101.6: filled at the close
                             "10:30": (102, 104.3, 102, 104.0),         # 1R = 104.2: half out
                             "11:00": (104, 108.2, 104, 108.0),         # 2.5R = 108.1: a quarter out
                             "11:30": (108, 115, 108, 114.8)})          # 5R = 114.6: the last quarter
        bars = day_bars(above_after(moves, level=(103, 103.2, 102.8, 103)))
        t, = zc.run_symbol(zc.Context(bars), dict(zc.Z2))
        self.assertAlmostEqual(t[1], 0.5 * 1 + 0.25 * 2.5 + 0.25 * 5, places=6)
        self.assertEqual(t[3], "objetivos")
        self.assertEqual(t[4], 1.0)
