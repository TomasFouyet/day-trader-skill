"""trader-scanner/orb.py: the 09:00-10:30 box, the close outside it, the stop at the other edge, the 3R
target, the 15 minute 50/200 EMA filter and the 16:00 close; trader-analyst/orb_study.py offline."""

import datetime
import gzip
import io
import json
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

orb = load("trader-scanner", "orb")
study = load("trader-analyst", "orb_study")
validate = load("trader-analyst", "orb_validate")
optimize = load("trader-analyst", "orb_optimize")
filters = load("trader-analyst", "orb_filters")

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

    def test_box_0900_to_0930_trades_from_0930(self):
        bars = day(D, {"09:05": (100, 101, 99, 100), "09:30": (100, 102.5, 100, 102)})
        p = {"box_start": "09:00", "box_end": "09:30", "min_box_bars": 3}
        (d, hi, lo, sess), = orb.boxes(bars, p)
        self.assertEqual((hi, lo, bars[sess[0]]["t"].strftime("%H:%M")), (101, 99, "09:30"))
        t, = orb.run(bars, p, [UP] * len(bars))
        self.assertEqual((t["entry_time"], t["entry"], t["stop"]), ("2026-11-17 09:30", 102, 99))

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

    def test_entry_filter_skip_or_wait(self):
        moves = {"11:00": (100, 102.5, 100, 102), "11:30": (102, 103.5, 102, 103), "12:00": (103, 111.5, 103, 111)}
        weak = lambda i, k, hi, lo, d: self.bars[i]["c"] >= 103          # only a close of 103 or more is strong
        self.bars = day(D, dict(self.BOX, **moves))
        self.assertEqual(orb.run(self.bars, {"accept": weak}, [UP] * len(self.bars)), [])      # skip: day over
        t, = orb.run(self.bars, {"accept": weak, "accept_mode": "wait"}, [UP] * len(self.bars))
        self.assertEqual((t["entry_time"], t["entry"]), ("2026-11-17 11:30", 103))
        self.assertEqual(len(orb.run(self.bars, {"accept": lambda *a: True}, [UP] * len(self.bars))), 1)

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


class Holding(unittest.TestCase):
    def test_no_new_trade_while_one_is_open(self):
        box = {"09:00": (100, 101, 99, 100)}
        d1 = day(D, dict(box, **{"11:00": (100, 102.5, 100, 102)}))
        d2 = day(D + datetime.timedelta(days=1), {"09:00": (100, 101, 99.5, 100), "11:00": (100, 102.5, 100, 102)})
        bars = d1 + d2
        ema = [UP] * len(bars)
        self.assertEqual(len(orb.run(bars, {}, ema)), 2)                  # closed at 16:00 each day
        held = orb.run(bars, {"hold_days": 5}, ema)
        self.assertEqual(len(held), 1)                                     # still open on day two
        self.assertTrue(held[0]["exit_time"].startswith("2026-11-18"))

    def test_last_entry_1300(self):
        box = {"09:00": (100, 101, 99, 100)}
        bars = day(D, dict(box, **{"13:05": (100, 102.5, 100, 102)}))
        self.assertEqual(orb.run(bars, {"last_entry": "12:55"}, [UP] * len(bars)), [])
        bars = day(D, dict(box, **{"12:55": (100, 102.5, 100, 102)}))
        self.assertEqual(len(orb.run(bars, {"last_entry": "12:55"}, [UP] * len(bars))), 1)


class Retest(unittest.TestCase):
    BOX = {"09:00": (100, 101, 99, 100)}

    def run_with(self, moves, **p):
        """After the 11:00 break, price holds above the box (103) unless a bar is given."""
        full = dict(self.BOX)
        t = datetime.datetime.combine(D, datetime.time(11, 5))
        while t.time() < datetime.time(16, 0):
            full[t.strftime("%H:%M")] = (103, 103.5, 102.5, 103)
            t += datetime.timedelta(minutes=5)
        full.update(moves)
        bars = day(D, full)
        return orb.run(bars, dict({"entry": "retest"}, **p), [UP] * len(bars))

    def test_limit_at_the_box_top_after_the_break(self):
        # close 102 breaks out; 11:30 trades back to 101 -> long at 101, stop 99 (risk 2), target 101 + 6 = 107
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (102, 103, 101.5, 102.5),
                            "11:30": (102, 102, 100.8, 101.5), "12:00": (101.5, 107.5, 101.5, 107)})
        self.assertEqual((t["entry"], t["stop"], t["outcome"], t["R"]), (101, 99, "target", 3.0))
        self.assertEqual(t["entry_time"], "2026-11-17 11:30")

    def test_no_retest_no_trade(self):
        self.assertEqual(self.run_with({"11:00": (100, 102.5, 100, 102)}), [])

    def test_cancelled_at_the_last_entry(self):
        moves = {"11:00": (100, 102.5, 100, 102), "14:00": (102, 102, 100.5, 101)}   # comes back after 13:00
        self.assertEqual(self.run_with(moves, last_entry="12:55"), [])

    def test_stop_in_the_fill_bar(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (102, 102, 98.5, 99)})
        self.assertEqual((t["entry"], t["outcome"], t["R"]), (101, "stop", -1.0))

    def test_better_open_fills_at_the_open(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (100.5, 101, 100.2, 100.8)})
        self.assertEqual(t["entry"], 100.5)

    def test_filter_still_applies(self):
        bars = day(D, dict(self.BOX, **{"11:00": (100, 102.5, 100, 102), "11:30": (102, 102, 100.8, 101.5)}))
        self.assertEqual(orb.run(bars, {"entry": "retest"}, [DOWN] * len(bars)), [])
        self.assertEqual(len(orb.run(bars, {"entry": "retest"}, [UP] * len(bars))), 1)


class Partial(unittest.TestCase):
    BOX = {"09:00": (100, 101, 99, 100)}

    def run_with(self, moves, **p):
        bars = day(D, dict(self.BOX, **moves))
        return orb.run(bars, dict({"partial_r": 1.7, "partial_frac": 0.7, "target_r": 3.0}, **p), [UP] * len(bars))

    # entry 102, stop 99, risk 3: partial at 102 + 5.1 = 107.1, runner target 111

    def test_partial_then_breakeven(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "12:00": (102, 107.5, 102, 107),
                            "12:05": (107, 107, 101.5, 102)})
        self.assertEqual(t["outcome"], "parcial+breakeven")
        self.assertAlmostEqual(t["R"], 0.7 * 1.7)

    def test_partial_then_runner_target(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "12:00": (102, 107.5, 102, 107),
                            "12:05": (107, 111.5, 107, 111)})
        self.assertEqual(t["outcome"], "parcial+objetivo")
        self.assertAlmostEqual(t["R"], 0.7 * 1.7 + 0.3 * 3.0)

    def test_partial_then_close(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "12:00": (102, 107.5, 102, 107)}, target_r=1.7)
        # the next bar opens at 100, under the entry: the rest goes out at that open (-2/3 R on 30%)
        self.assertEqual(t["outcome"], "parcial+breakeven")
        self.assertAlmostEqual(t["R"], 0.7 * 1.7 + 0.3 * (100 - 102) / 3.0)

    def test_stop_before_partial(self):
        t, = self.run_with({"11:00": (100, 102.5, 100, 102), "11:05": (102, 102, 98.5, 99)})
        self.assertEqual((t["outcome"], t["R"]), ("stop", -1.0))

    def test_no_partial_then_close(self):
        bars = day(D, dict(self.BOX, **{"11:00": (100, 102.5, 100, 102), "15:55": (103, 104, 103, 104)}))
        t, = orb.run(bars, {"partial_r": 1.7}, [UP] * len(bars))
        self.assertEqual(t["outcome"], "close")


class Overlap(unittest.TestCase):
    def test_overlap_opens_while_one_is_open(self):
        box = {"09:00": (100, 101, 99, 100)}
        bars = day(D, dict(box, **{"11:00": (100, 102.5, 100, 102)})) + day(
            D + datetime.timedelta(days=1), {"09:00": (100, 101, 99.5, 100), "11:00": (100, 102.5, 100, 102)})
        ema = [UP] * len(bars)
        self.assertEqual(len(orb.run(bars, {"hold_days": 5}, ema)), 1)
        self.assertEqual(len(orb.run(bars, {"hold_days": 5, "overlap": True}, ema)), 2)


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


def write_test_chart(tmp, days=30):
    ts, o, h, l, c = [], [], [], [], []
    for k in range(days):
        d = D - datetime.timedelta(days=days + 10 - k)
        if d.weekday() > 4:
            continue
        p = 100.0 + k
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


class Validate(unittest.TestCase):
    def test_report_and_json(self):
        tmp = tempfile.mkdtemp()
        write_test_chart(tmp)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validate.main(["--offline", tmp, "--symbols", "TEST", "--random", "3", "--sims", "20", "--min-n", "1"])
        text = out.getvalue()
        self.assertEqual(code, 0, err.getvalue())
        for part in ("1. Backtest de las 288 versiones", "Las 3 mejores por Sharpe", "Tus estrategias:", "2. Deflated Sharpe", "3a. Monte Carlo", "3b. Monte Carlo",
                     "4. Walk-forward", "5. Costos"):
            self.assertIn(part, text)
        doc = json.loads(text.split("@@ORBJSON@@ ", 1)[1].splitlines()[0])
        self.assertEqual(len(doc["grid"]), 288)
        self.assertGreaterEqual(len(doc["equity"]), 4)
        self.assertEqual(len(doc["dates"]), len(doc["equity"][0]["equity"]))

    def test_retest_and_overlap_options(self):
        tmp = tempfile.mkdtemp()
        write_test_chart(tmp)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = validate.main(["--offline", tmp, "--symbols", "TEST", "--random", "2", "--sims", "10", "--min-n", "1",
                                  "--entry", "retest", "--overlap"])
        self.assertEqual(code, 0)
        self.assertIn("orden limite en el retesteo", out.getvalue())
        validate.OPTS.update(entry="close", overlap=False)

    def test_optimizer_report(self):
        tmp = tempfile.mkdtemp()
        write_test_chart(tmp)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = optimize.main(["--offline", tmp, "--symbols", "TEST", "--small", "--workers", "1", "--boots", "20",
                                  "--min-n", "1", "--min-train", "1"])
        text = out.getvalue()
        self.assertEqual(code, 0, err.getvalue())
        for part in ("1. Las 15 mejores", "2. Probabilidad de sobreajuste", "3. Reality Check", "4. Walk-forward",
                     "5. Estabilidad", "6. Deflated Sharpe"):
            self.assertIn(part, text)
        doc = json.loads(text.split("@@ORBOPT@@ ", 1)[1].splitlines()[0])
        self.assertEqual(doc["versions"], 16)

    def test_optimizer_grid_holds_the_current_rules(self):
        cfgs = list(__import__("itertools").product(*(optimize.FULL[d] for d in optimize.DIMS)))
        self.assertEqual(len(cfgs), 2880)
        self.assertIn(optimize.CURRENT, cfgs)
        p = optimize.params(optimize.CURRENT)
        self.assertEqual((p["box_end"], p["partial_r"], p["partial_frac"], p["hold_days"]), ("10:00", 1.7, 0.7, 20))
        self.assertIsNone(optimize.params(("10:00", "both", "none", "12:55", 1.7, 1.0, 0))["partial_r"])

    def test_overfitting_measures_tell_luck_from_edge(self):
        rng = random.Random(4)
        months, per = 60, 20
        month_of = [m for m in range(months) for _ in range(per)]
        days_in = [per] * months

        def agg(edge):
            daily = [rng.gauss(edge, 1.0) for _ in month_of]
            return optimize.Agg(daily, [1] * len(daily), month_of, months)
        noise = [agg(0.0) for _ in range(40)]
        near = [[] for _ in noise]
        elig = [True] * len(noise)
        luck = optimize.cscv(noise, elig, near, months, days_in)
        self.assertGreater(luck["best"]["pbo"], 0.2)
        rc = optimize.reality_check(noise, elig, months, 200, random.Random(1))
        self.assertGreater(rc["p_rc"], 0.05)
        self.assertLessEqual(rc["p_spa"], rc["p_rc"])       # SPA drops the hopeless versions, never adds
        real = noise + [agg(0.25)]
        near = [[] for _ in real]
        elig = [True] * len(real)
        self.assertLess(optimize.cscv(real, elig, near, months, days_in)["best"]["pbo"], 0.05)
        self.assertLess(optimize.reality_check(real, elig, months, 200, random.Random(1))["p_rc"], 0.05)

    def test_filter_study_report(self):
        tmp = tempfile.mkdtemp()
        write_test_chart(tmp)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = filters.main(["--offline", tmp, "--symbols", "TEST", "--random", "2", "--workers", "1",
                                 "--min-n", "3"])
        text = out.getvalue()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("BASE ELEGIDA", text)
        self.assertIn("BASE INTRADIA", text)
        doc = json.loads(text.split("@@ORBFILTERS@@ ", 1)[1].splitlines()[0])
        self.assertEqual(set(doc["bases"]), {"elegida", "intradia"})
        self.assertGreater(len(doc["bases"]["intradia"]["filters"]), 0)
        self.assertIn("contra quitar al azar", text)

    def test_features_only_use_the_past(self):
        b = []
        for k in range(30):
            d = datetime.date(2026, 9, 1) + datetime.timedelta(days=k)
            if d.weekday() > 4:
                continue
            for x in day(d, {}, start="09:30", end="15:55", base=100.0 + k):
                x["v"] = 1000
                b.append(x)
        fx = filters.symbol_features(b)
        last = [i for i, x in enumerate(b) if x["t"].time() == datetime.time(10, 30)][-1]
        self.assertAlmostEqual(fx["rv_bar"][last], 1.0)
        self.assertAlmostEqual(fx["rv_ses"][last], 1.0)
        # changing a later bar does not change what was known at this one
        b2 = [dict(x) for x in b]
        for x in b2[last + 1:]:
            x["v"] = 99999
            x["h"] += 5
        fx2 = filters.symbol_features(b2)
        self.assertEqual((fx["rv_bar"][last], fx["vwap"][last], fx["avg_rng"][last]),
                         (fx2["rv_bar"][last], fx2["vwap"][last], fx2["avg_rng"][last]))

    def test_block_bootstrap(self):
        import random
        tots, dds = validate.block_bootstrap({"a": [1.0, -2.0], "b": [3.0]}, 2, 50, random.Random(1))
        self.assertEqual(len(tots), 50)
        self.assertTrue(set(tots) <= {-2.0, 2.0, 6.0})
        self.assertTrue(all(d >= 0 for d in dds))


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
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            study.main(["--offline", tmp, "--symbols", "TEST", "--min-n", "5", "--by-month", "5"])
        self.assertIn("Por mes (antes de costos):", out.getvalue())
        self.assertIn("5 operaciones seguidas suman", out.getvalue())

    def test_box_option(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = study.main(["--offline", tempfile.mkdtemp(), "--symbols", "NOPE", "--box", "09:00-09:30"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
