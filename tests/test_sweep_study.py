"""trader-analyst/sweep_study.py offline: the sweep replica on the saved 30-minute charts, its report
(by year, discovery and validation halves, filters) and the random baseline."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

st = load("trader-analyst", "sweep_study")
DIR = os.path.join(FIX, "trader-scanner", "sweep")


class Study(unittest.TestCase):
    def run_it(self, *extra):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("NOW\nAPP\nSPCX\nMU\nLITE\nTSLA\nPLTR\nNOPE\n")
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = st.main(["--offline", DIR, "--symbols", fh.name, "--now", "2026-11-13 16:05", "--min-n", "5"]
                               + list(extra))
        finally:
            os.unlink(fh.name)
        return code, out.getvalue()

    def test_longs_at_one_and_a_half_r(self):
        code, text = self.run_it()
        self.assertEqual(code, 0, text)
        self.assertIn("velas de 30m, solo largos, objetivo 1.5R, sin costos: 7 acciones (1 sin datos suficientes)", text)
        self.assertIn("Patrones: mitad de descubrimiento", text)
        self.assertIn("| azar n", text)
        self.assertIn("Filtros que destacan", text)
        self.assertIn("entrada en el retesteo", text)

    def test_both_sides_and_a_target(self):
        code, text = self.run_it("--shorts", "--target", "2")
        self.assertEqual(code, 0, text)
        self.assertIn("largos y cortos, objetivo 2R", text)

    def test_random_longs(self):
        sw = st.sw
        series, _ = sw.scan.load_offline(DIR)
        bars = sw.session_bars(series["NOW"], sw.scan.parse_now("2026-11-13 16:05"))
        r = st.random_longs(bars, sw.atr_series(bars), 2.5, 1.5, 200, 50, 1)
        self.assertEqual(len(r), 50)
        self.assertTrue(all(x <= 1.5 for _, x in r))             # a gap through the stop can lose more than 1R


if __name__ == "__main__":
    unittest.main()
