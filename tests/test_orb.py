"""trader-scanner/orb.py: the 09:00-10:30 box, the close outside it, the stop at the other edge, the 3R
target, the 15 minute 50/200 EMA filter and the 16:00 close; trader-analyst/orb_study.py offline."""

import datetime
import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

orb = load("trader-scanner", "orb")
study = load("trader-analyst", "orb_study")

UP = (2.0, 1.0, 999)       # (ema 50, ema 200, completed 15m bars): trend up
DOWN = (1.0, 2.0, 999)


def day(date, moves=None, start="09:00", end="16:00", base=100.0):
    """5 minute bars from start to end, flat at `base` (+-0.1) unless moves[HH:MM] = (o, h, l, c)."""
    out = []
    t = datetime.datetime.combine(date, datetime.time(*map(int, start.split(":"))))
    stop = datetime.datetime.combine(date, datetime.time(*map(int, end.split(":"))))
    while t < stop:
        o, h, l, c = (moves or {}).get(t.strftime("%H:%M"), (base, base + 0.1, base - 0.1, base))
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1})
        t += datetime.timedelta(minutes=5)
    return out


D = datetime.date(2026, 11, 17)


class Box(unittest.TestCase):
    def test_box_is_0900_to_1030(self):
        bars = day(D, {"09:00": (100, 103, 100, 100), "10:25": (100, 100, 97, 100), "10:30": (100, 110, 90, 100)})
        (d, hi, lo, sess), = orb.boxes(bars)
        self.assertEqual((hi, lo), (103, 97))                 # the 10:30 bar is not in the box
        self.assertEqual(bars[sess[0]]["t"].strftime("%H:%M"), "10:30")
        self.assertEqual(bars[sess[-1]]["t"].strftime("%H:%M"), "15:55")

    def test_too_few_box_bars_no_day(self):
        self.assertEqual(orb.boxes(day(D, start="10:00")), [])


class Trades(unittest.TestCase):
    BOX = {"09:00": (100, 101, 99, 100)}

    def run_with(self, moves, ema=UP, **p):
        bars = day(D, dict(self.BOX, **moves))
        return orb.run(bars, p, [ema] * len(bars))

    def test_long_to_the_target(self):
        # close 102 above the box (101); stop at the box low 99 -> risk 3, target 102 + 9 = 111
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "12:00": (102, 111.5, 102, 111)})
        self.assertEqual((t["dir"], t["entry"], t["stop"], t["outcome"], t["R"]), (1, 102, 99, "target", 3.0))
        self.assertEqual(t["entry_time"], "2026-11-17 11:00")

    def test_short_stopped_at_the_box_high(self):
        t, = self.run_with({"11:00": (100, 100, 97.5, 98), "11:30": (98, 101.2, 98, 100)}, ema=DOWN)
        self.assertEqual((t["dir"], t["stop"], t["outcome"], t["R"]), (-1, 101, "stop", -1.0))

    def test_filter_blocks_the_long(self):
        moves = {"11:00": (100, 102.5, 100, 102)}
        self.assertEqual(self.run_with(moves, ema=DOWN), [])
        self.assertEqual(len(self.run_with(moves, ema=DOWN, filter="none")), 1)
        self.assertEqual(len(self.run_with(moves, ema=DOWN, filter="counter")), 1)

    def test_no_filter_until_200_bars_of_15m(self):
        self.assertEqual(self.run_with({"11:00": (100, 102.5, 100, 102)}, ema=(2.0, 1.0, 150)), [])

    def test_a_wick_is_not_a_break(self):
        self.assertEqual(self.run_with({"11:00": (100, 105, 100, 100.5)}), [])

    def test_first_break_against_the_filter(self):
        moves = {"10:45": (100, 100, 97, 98), "11:00": (100, 102.5, 100, 102)}
        t, = self.run_with(moves)                              # the short is ignored, the long is taken
        self.assertTrue(t["features"]["first_against"])
        self.assertEqual(self.run_with(moves, first_only=True), [])

    def test_out_at_the_1600_close(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "15:55": (102, 103.6, 102, 103.5)})
        self.assertEqual((t["outcome"], t["R"]), ("close", 0.5))

    def test_gap_through_the_stop(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (97, 97.5, 96, 97)})
        self.assertAlmostEqual(t["R"], -5 / 3.0)

    def test_stop_and_target_in_one_bar_is_a_stop(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (102, 112, 98, 105)})
        self.assertEqual(t["outcome"], "stop")

    def test_one_trade_a_day(self):
        got = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (102, 102, 98.5, 98.8),
                             "12:00": (100, 102.5, 100, 102)})
        self.assertEqual(len(got), 1)

    def test_last_entry(self):
        self.assertEqual(self.run_with({"14:00": (100, 102.5, 100, 102)}, last_entry="12:00"), [])

    def test_target_option(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "12:00": (102, 105.5, 102, 105)}, target_r=1.0)
        self.assertEqual((t["outcome"], t["R"]), ("target", 1.0))

    def test_hold_days_keeps_it_overnight(self):
        bars = day(D, dict(self.BOX, **{"11:00": (100, 102.5, 100, 102)})) + day(
            D + datetime.timedelta(days=1), {"10:00": (102, 111.5, 102, 111)})
        ema = [UP] * len(bars)
        self.assertEqual(orb.run(bars, {}, ema)[0]["outcome"], "close")
        self.assertEqual(orb.run(bars, {"hold_days": 1}, ema)[0]["outcome"], "target")


class Spikes(unittest.TestCase):
    def test_lone_bad_tick_is_pulled_back(self):
        bars = day(D, {"15:00": (104.0, 104.0, 99.9, 100.0)}, start="14:50", end="15:10")
        orb.clean_spikes(bars)
        self.assertEqual((bars[2]["o"], bars[2]["h"]), (100.0, 100.0))

    def test_a_real_gap_stays(self):
        bars = day(D, {"09:30": (104.0, 104.5, 103.8, 104.2)}, start="09:20", end="09:40")
        orb.clean_spikes(bars)
        self.assertEqual(bars[2]["o"], 104.0)


class Ema(unittest.TestCase):
    def test_15m_bar_counts_when_its_last_5m_bar_closes(self):
        bars = day(D, start="09:00", end="09:30")
        got = orb.ema_15m(bars)
        self.assertEqual([n for _, _, n in got], [0, 0, 1, 1, 1, 2])

    def test_gap_completes_the_open_bar(self):
        bars = day(D, start="09:00", end="09:10") + day(D, start="09:30", end="09:35")
        self.assertEqual([n for _, _, n in orb.ema_15m(bars)], [0, 0, 1])

    def test_ema_follows_the_trend(self):
        bars = []
        for k in range(10):
            d = D + datetime.timedelta(days=k)
            bars += day(d, start="04:00", end="20:00", base=100 + k)
        f, s, n = orb.ema_15m(bars)[-1]
        self.assertGreater(f, s)
        self.assertGreaterEqual(n, 200)


class StudyOffline(unittest.TestCase):
    def test_report(self):
        tmp = tempfile.mkdtemp()
        ts, o, h, l, c = [], [], [], [], []
        for k in range(30):
            d = D - datetime.timedelta(days=40 - k)
            if d.weekday() > 4:
                continue
            p = 100.0 + k                               # rising, so the 15m 50 EMA is above the 200
            moves = {"09:00": (p, p + 1, p - 1, p), "11:00": (p, p + 2.5, p, p + 2)}
            if k % 3 == 0:
                moves["12:00"] = (p + 2, p + 11.5, p + 2, p + 11)
            for b in day(d, moves, start="04:00", end="20:00", base=p):
                utc = b["t"] + datetime.timedelta(hours=5)
                ts.append(int((utc - datetime.datetime(1970, 1, 1)).total_seconds()))
                o.append(b["o"]); h.append(b["h"]); l.append(b["l"]); c.append(b["c"])
        doc = {"chart": {"result": [{"meta": {"symbol": "TEST"}, "timestamp": ts,
                                     "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c,
                                                               "volume": [1] * len(ts)}]}}], "error": None}}
        with gzip.open(os.path.join(tmp, "TEST.json.gz"), "wt") as fh:
            json.dump(doc, fh)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = study.main(["--offline", tmp, "--symbols", "TEST,NOPE", "--min-n", "5"])
        text = out.getvalue()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("Reglas tal cual: n", text)
        self.assertIn("sin el filtro de medias", text)
        self.assertIn("1 sin datos suficientes: NOPE", text)
        self.assertIn("Patrones: mitad de descubrimiento", text)
        self.assertIn("TEST", text)


if __name__ == "__main__":
    unittest.main()
