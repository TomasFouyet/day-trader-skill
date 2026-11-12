"""trader-analyst/drift_study.py offline: SEC submissions (the analyst fixture, AAPL's 8-K 2.02 of
2026-07-30 filed after the close) and real daily bars, so the event lands on the next session."""

import io
import json
import os
import shutil
import tempfile
import unittest
import gzip
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

ds = load("trader-analyst", "drift_study")
SEC = os.path.join(FIX, "trader-analyst", "submissions-CIK0000320193.json")
SW = os.path.join(FIX, "trader-scanner", "swing")


class Drift(unittest.TestCase):
    def test_earnings_times_and_reaction_day(self):
        with open(SEC) as fh:
            times = ds.earnings_times([json.load(fh)])
        self.assertEqual(len(times), 1)
        date, hm = times[0]
        self.assertEqual(date, "2026-07-30")
        bars = [{"date": d, "o": 1, "h": 1, "l": 1, "c": 1} for d in ("2026-07-29", "2026-07-30", "2026-07-31")]
        self.assertEqual(ds.reaction_index(bars, "2026-07-30", "16:31"), 2)    # after the close: next session
        self.assertEqual(ds.reaction_index(bars, "2026-07-30", "06:05"), 1)    # before the open: that day
        self.assertEqual(ds.reaction_index(bars, "2026-07-30", None), 1)
        older = {"form": ["8-K", "10-Q", "8-K"], "filingDate": ["2020-01-28", "2020-01-29", "2020-02-03"],
                 "items": ["2.02,9.01", "", "5.02"], "acceptanceDateTime": ["2020-01-28T16:30:41.000Z", "", ""]}
        self.assertEqual(ds.earnings_times([older]), [("2020-01-28", "16:30")])

    def test_walk(self):
        bars = [{"date": "d0", "o": 100, "h": 100, "l": 100, "c": 100}] + \
               [{"date": "d%d" % i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i} for i in range(1, 30)]
        self.assertAlmostEqual(ds.walk(bars, 0, 20, 2.0, 5.0), 2.0)         # +20 over a 10 risk
        bars[3] = {"date": "d3", "o": 85, "h": 86, "l": 84, "c": 85}
        self.assertAlmostEqual(ds.walk(bars, 0, 20, 2.0, 5.0), -1.5)        # gapped through the 90 stop

    def test_offline_run(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "sec"))
            os.makedirs(os.path.join(d, "prices"))
            shutil.copy(SEC, os.path.join(d, "sec", "AAPL.json"))
            for sym, src in (("AAPL", "AAPL"), ("SPY", "KO")):
                with gzip.open(os.path.join(SW, src + ".json.gz"), "rt") as fh:
                    doc = json.load(fh)
                with open(os.path.join(d, "prices", sym + ".json"), "w") as fh:
                    json.dump(doc, fh)
            with open(os.path.join(d, "syms.txt"), "w") as fh:
                fh.write("AAPL\nKO\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ds.main(["--offline", d, "--symbols", os.path.join(d, "syms.txt")])
            text = out.getvalue()
            self.assertEqual(code, 0, err.getvalue() + text)
            self.assertIn("Eventos de resultados (8-K 2.02): 1 en 1 acciones", text)
            self.assertIn("despues 16:00 1", text)
            self.assertIn("AZAR", text)
            self.assertIn("sesgo de supervivencia", text)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
