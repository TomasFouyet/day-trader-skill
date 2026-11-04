"""Phase 5: the watchlist builder, the Telegram notice, the daily pipeline, the
GitHub Actions workflow and the privacy rules. All offline: Telegram is never
called for real (its HTTP function is swapped out)."""

import gzip
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

wl = load("trader-scanner", "watchlist_top")
tg = load("trader-alerts", "notify_telegram")
pipe = load("trader-alerts", "swing_pipeline")
risk = load("trader-risk", "check")
size = load("trader-budget", "size")

SWDIR = os.path.join(FIX, "trader-scanner", "swing")
SEC = os.path.join(FIX, "trader-analyst")
TOKEN = "123456:ABCdefGhIJKlmnoPQRstuVWXyz_0123456"


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self.env = {k: os.environ.pop(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TRADER_BUDGET")
                    if k in os.environ}

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TRADER_BUDGET"):
            os.environ.pop(k, None)
        os.environ.update(self.env)


# ---------------------------------------------------------------- watchlist

NASDAQ = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
          "%s"
          "File Creation Time: 1030202617:00|||||||\n")
OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
         "%s"
         "File Creation Time: 1030202617:00|||||||\n")


class Watchlist(Tmp):
    def build_dir(self):
        os.mkdir("wl")
        nas = ["AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N", "MSFT|Microsoft Corp - Common Stock|Q|N|N|100|N|N",
               "NVDA|NVIDIA Corp - Common Stock|Q|N|N|100|N|N", "AMD|Advanced Micro Devices - Common Stock|Q|N|N|100|N|N",
               "AMZN|Amazon.com Inc - Common Stock|Q|N|N|100|N|N", "META|Meta Platforms - Class A Common Stock|Q|N|N|100|N|N",
               "GOOGL|Alphabet Inc. - Class A Common Stock|Q|N|N|100|N|N", "TSLA|Tesla, Inc. - Common Stock|Q|N|N|100|N|N",
               "INTC|Intel Corp - Common Stock|Q|N|N|100|N|N", "COST|Costco Wholesale - Common Stock|Q|N|N|100|N|N",
               "PLTR|Palantir Technologies - Class A Common Stock|Q|N|N|100|N|N",
               "QQQ|Invesco QQQ Trust, Series 1|Q|N|N|100|Y|N",
               "PENY|Penny Co - Common Stock|Q|N|N|100|N|N"]
        oth = ["KO|Coca-Cola Company (The) Common Stock|N|KO|N|100|N|KO", "JPM|JP Morgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM",
               "XOM|Exxon Mobil Corporation Common Stock|N|XOM|N|100|N|XOM", "WMT|Walmart Inc. Common Stock|N|WMT|N|100|N|WMT",
               "SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY"]
        with open("wl/nasdaqlisted.txt", "w") as fh:
            fh.write(NASDAQ % "".join(r + "\n" for r in nas))
        with open("wl/otherlisted.txt", "w") as fh:
            fh.write(OTHER % "".join(r + "\n" for r in oth))
        for f in os.listdir(SWDIR):
            shutil.copy(os.path.join(SWDIR, f), "wl")
        # a penny stock: KO's real chart, every price divided by 100
        with gzip.open(os.path.join(SWDIR, "KO.json.gz"), "rt") as fh:
            doc = json.load(fh)
        r = doc["chart"]["result"][0]
        r["meta"]["symbol"] = "PENY"
        r["meta"]["regularMarketPrice"] /= 100.0
        q = r["indicators"]["quote"][0]
        for k in ("open", "high", "low", "close"):
            q[k] = [None if v is None else v / 100.0 for v in q[k]]
        with open("wl/PENY.json", "w") as fh:
            json.dump(doc, fh)
        return "wl"

    def test_top_by_dollar_volume_without_etfs_or_pennies(self):
        d = self.build_dir()
        code, text, err = run(wl, ["--offline", d, "--now", "2026-10-30 17:00", "--top", "5", "--prefilter", "50",
                                   "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        syms = [r["symbol"] for r in doc["rows"]]
        self.assertEqual(len(syms), 5)
        self.assertNotIn("QQQ", syms)
        self.assertNotIn("SPY", syms)
        self.assertNotIn("PENY", syms)
        self.assertEqual(doc["counts"]["penny"], 1)
        dvs = [r["avg_dollar_volume_3m"] for r in doc["rows"]]
        self.assertEqual(dvs, sorted(dvs, reverse=True))
        self.assertTrue(all(r["sessions"] == 63 for r in doc["rows"]))
        with open("trader/watchlist.txt") as fh:
            lines = [ln for ln in fh.read().splitlines() if not ln.startswith("#")]
        self.assertEqual([ln.split("|")[0] for ln in lines], syms)
        # scan.py reads it back: the swing scan then scans exactly these
        scan = load("trader-scanner", "scan")
        self.assertEqual(scan.symbols_from_file("trader/watchlist.txt"), syms)

    def test_exclusions_and_floors(self):
        d = self.build_dir()
        with open("trader/watchlist_exclude.txt", "w") as fh:
            fh.write("# Fintual no las tiene\nNVDA\ntsla\n")
        code, text, _ = run(wl, ["--offline", d, "--now", "2026-10-30 17:00", "--json", "--no-write",
                                 "--min-dollar-volume", "5000000000"])
        doc = json.loads(text)
        syms = [r["symbol"] for r in doc["rows"]]
        self.assertNotIn("NVDA", syms)
        self.assertNotIn("TSLA", syms)
        self.assertEqual(doc["counts"]["excluded"], 2)
        self.assertTrue(all(r["avg_dollar_volume_3m"] >= 5e9 for r in doc["rows"]))
        self.assertGreater(doc["counts"]["illiquid"], 0)
        self.assertFalse(os.path.exists("trader/watchlist.txt"))
        code, _, err = run(wl, ["--offline", d, "--now", "2026-10-30 17:00", "--min-dollar-volume", "1e15"])
        self.assertEqual(code, 1)
        self.assertIn("nothing written", err)

    def test_example_file_is_readable(self):
        scan = load("trader-scanner", "scan")
        syms = scan.symbols_from_file(os.path.join(ROOT, "trader-scanner", "watchlist.example.txt"))
        self.assertGreaterEqual(len(syms), 100)
        self.assertTrue(all(re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", s) for s in syms))
        self.assertNotIn("SPY", syms)


# ---------------------------------------------------------------- Telegram

TICKET = {"symbol": "WMT", "side": "buy", "qty": 9, "type": "market", "entry": 123.51, "stop": 118.22,
          "target": 134.09, "risk_per_share": 5.29, "reward_risk": 2.0, "max_loss": 47.61, "cost": 1111.59,
          "setup": "breakout20", "signal_date": "2026-10-30", "time_stop_date": "2026-11-10", "time_stop_days": 7,
          "manual": True,
          "earnings": {"signal_date": "2026-10-30", "horizon_days": 7, "flag": "resultados dentro del horizonte",
                       "blocked": False, "next": "2026-11-06", "warnings": []},
          "timemachine": {"p_target": 0.301, "p_stop": 0.32, "p_unresolved": 0.379, "expectancy_R": 0.05,
                          "horizon_days": 7, "paths": 10000, "seed": 19, "verdict": "Cercano a una moneda al aire: x",
                          "history_cases": 12, "history_hit_rate": 0.25, "history_avg_R": 0.1, "few_cases": True}}


class Telegram(Tmp):
    def setUp(self):
        Tmp.setUp(self)
        saved = (tg._post, tg._sleep)
        self.addCleanup(lambda: (setattr(tg, "_post", saved[0]), setattr(tg, "_sleep", saved[1])))

    def files(self, tickets=(TICKET,), date="2026-11-02", cfg=None):
        res = risk.check_swing(list(tickets), cfg or {"budget": 5000, "cash_buffer_pct": 10, "max_position_pct": 75},
                               today=date)
        with open("trader/swing_orders.json", "w") as fh:
            json.dump({"mode": "swing", "tickets": list(tickets)}, fh)
        os.mkdir("trader/state")
        with open("trader/state/swing_risk.json", "w") as fh:
            json.dump(res, fh)
        return res

    def fake(self, answers=None):
        self.posts = []
        answers = list(answers or [])

        def post(url, payload, timeout=20):
            self.posts.append((url, payload))
            if answers:
                a = answers.pop(0)
                if isinstance(a, Exception):
                    raise a
                return a
            return {"ok": True, "result": {"message_id": len(self.posts)}}
        tg._post = post
        tg._sleep = lambda s: None

    def test_fingerprint_matches_trader_risk(self):
        self.assertEqual(tg.swing_fingerprint(TICKET), risk.swing_fingerprint(TICKET))

    def test_dry_run_prints_and_remembers_nothing(self):
        self.files()
        self.fake()
        code, text, err = run(tg, ["--dry-run", "--now", "2026-11-02 18:00"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts, [])
        for must in ("Setup swing: WMT (breakout20)", "Entrada ~$123.51 | Stop $118.22 | Objetivo $134.09 (2R)",
                     "Tamano sugerido: 9 acciones (~$1,111.59)", "Perdida maxima en el stop: $47.61",
                     "objetivo primero 30.1 %, stop primero 32.0 %, sin resolver 37.9 %, expectativa +0.05R",
                     "Cercano a una moneda al aire", "12 caso(s), pocos",
                     "⚠ resultados dentro del horizonte (2026-11-06)", "No es asesoria financiera",
                     "cierre del 2026-11-10", "Would send 1"):
            self.assertIn(must, text)
        self.assertFalse(os.path.exists("trader/state/telegram.json"))

    def test_sends_once_and_never_repeats(self):
        self.files()
        self.fake()
        os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="987654")
        code, text, err = run(tg, ["--now", "2026-11-02 18:00"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.posts), 1)
        url, payload = self.posts[0]
        self.assertEqual(url, "https://api.telegram.org/bot%s/sendMessage" % TOKEN)
        self.assertEqual(payload["chat_id"], "987654")
        self.assertNotIn(TOKEN, text + err + payload["text"])
        with open("trader/state/telegram.json") as fh:
            st = json.load(fh)
        self.assertIn("WMT|2026-10-30|breakout20", st["sent"])
        code, text, _ = run(tg, ["--now", "2026-11-02 21:00"])
        self.assertEqual(len(self.posts), 1)                # not again
        self.assertIn("already sent", text)

    def test_only_todays_passes_with_the_same_ticket(self):
        self.files(tickets=(TICKET, dict(TICKET, symbol="XOM", qty=500)))   # XOM blocked by its loss
        self.fake()
        code, text, _ = run(tg, ["--dry-run", "--now", "2026-11-02 18:00"])
        self.assertIn("Would send 1", text)
        self.assertNotIn("XOM", text.split("Would send")[0])
        code, text, _ = run(tg, ["--dry-run", "--now", "2026-11-03 18:00"])
        self.assertIn("not today", text)
        with open("trader/swing_orders.json", "w") as fh:      # the ticket changed after the risk check
            json.dump({"tickets": [dict(TICKET, qty=8)]}, fh)
        code, text, _ = run(tg, ["--dry-run", "--now", "2026-11-02 18:00"])
        self.assertIn("ticket changed", text)

    def test_credentials_only_from_the_environment(self):
        self.files()
        self.fake()
        code, _, err = run(tg, ["--now", "2026-11-02 18:00"])
        self.assertEqual(code, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", err)
        os.environ.update(TELEGRAM_BOT_TOKEN="not-a-token", TELEGRAM_CHAT_ID="1")
        code, _, err = run(tg, ["--now", "2026-11-02 18:00"])
        self.assertIn("does not look like a bot token", err)
        self.assertNotIn("not-a-token", err)
        self.assertEqual(self.posts, [])

    def test_test_message_and_errors(self):
        self.fake()
        code, text, _ = run(tg, ["--test", "--dry-run"])
        self.assertIn("Prueba de day-trader-skill", text)
        self.assertEqual(self.posts, [])
        os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="5")
        code, text, _ = run(tg, ["--test"])
        self.assertEqual((code, len(self.posts)), (0, 1))
        self.assertIn("funcionan", self.posts[0][1]["text"])
        self.assertFalse(os.path.exists("trader/state/telegram.json"))     # a test is not a setup
        err401 = urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))
        self.fake([err401])
        code, _, err = run(tg, ["--test"])
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401: Unauthorized", err)
        self.assertNotIn(TOKEN, err)
        e429 = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b'{"ok":false,"parameters":{"retry_after":3}}'))
        self.fake([e429])
        code, _, _ = run(tg, ["--test"])
        self.assertEqual((code, len(self.posts)), (0, 2))


# ---------------------------------------------------------------- pipeline

class Pipeline(Tmp):
    def config(self, **kw):
        with open(os.path.join(ROOT, "trader-budget", "config.swing.example.json")) as fh:
            cfg = json.load(fh)
        cfg.update(kw)
        with open("trader/config.json", "w") as fh:
            json.dump(cfg, fh)

    def go(self, *args, now="2026-10-30 17:00"):
        return run(pipe, ["--now", now, "--offline-charts", SWDIR, "--offline-sec", SEC, "--skip-news",
                          "--replays", "500"] + list(args))

    def test_the_clock_in_new_york(self):
        p = pipe.parse_now
        self.assertTrue(pipe.market_check(p("2026-10-30T20:40:00Z"))[0])    # EDT: 16:40 in New York
        self.assertFalse(pipe.market_check(p("2026-11-02T20:40:00Z"))[0])   # EST since Nov 1: 15:40
        self.assertTrue(pipe.market_check(p("2026-11-02T21:40:00Z"))[0])    # 16:40
        self.assertTrue(pipe.market_check(p("2026-03-09T20:40:00Z"))[0])    # EDT again from Mar 8
        self.assertFalse(pipe.market_check(p("2026-03-06T20:40:00Z"))[0])   # still EST on Mar 6
        self.assertFalse(pipe.market_check(p("2026-10-31T22:00:00Z"))[0])   # Saturday
        self.assertFalse(pipe.market_check(p("2026-11-26T22:00:00Z"))[0])   # Thanksgiving
        self.assertTrue(pipe.market_check(p("2026-11-27T18:20:00Z"))[0])    # half day: after 13:15
        self.assertFalse(pipe.market_check(p("2026-11-27T18:05:00Z"))[0])

    def test_full_run_dry_then_once_a_day(self):
        self.config()
        code, text, err = self.go("--dry-run")
        self.assertEqual(code, 0, err + text)
        for step in ("== scanner", "== resultados", "== exit", "== time machine", "== budget", "== risk",
                     "== alertas", "== telegram"):
            self.assertIn(step, text)
        self.assertIn("Pasan risk: 3.", text)
        self.assertIn("Setup swing: WMT", text)
        self.assertIn("Would send 3", text)
        self.assertNotIn("Setup swing: AAPL", text)      # blocked by its estimated report
        self.assertFalse(os.path.exists("trader/orders.json"))
        with open("trader/state/pipeline.json") as fh:
            st = json.load(fh)
        self.assertIsNone(st.get("last_ok"))              # a dry run does not count as today's run
        self.assertEqual(st["last_counts"]["passed"], 3)

        self.posts = []
        saved = tg._post
        pipe_tg = None

        def post(url, payload, timeout=20):
            self.posts.append(payload)
            return {"ok": True, "result": {"message_id": 1}}
        os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")
        runner_load = pipe.load_tool

        def load_tool(name):
            m = runner_load(name)
            if name == "notify":
                m._post = post
            return m
        pipe.load_tool = load_tool
        try:
            code, text, err = self.go("--quiet")
            self.assertEqual(code, 0, err + text)
            self.assertEqual(len(self.posts), 3)
            self.assertNotIn("$", text)                   # --quiet: no sizes, no account in the log
            self.assertNotIn("WMT", text)
            code, text, _ = self.go("--quiet", now="2026-10-30 17:40")
            self.assertIn("Ya corrio hoy", text)
            self.assertEqual(len(self.posts), 3)
            code, text, _ = self.go("--quiet", "--force", now="2026-10-30 17:45")
            self.assertEqual(len(self.posts), 3)          # forced again: same setups, nothing repeated
        finally:
            pipe.load_tool = runner_load
            tg._post = saved

    def test_check_clock_and_refusals(self):
        self.assertEqual(run(pipe, ["--check-clock", "--now", "2026-10-30 15:00"])[0], 1)
        self.assertEqual(run(pipe, ["--check-clock", "--now", "2026-10-30 16:20"])[0], 0)
        self.assertEqual(run(pipe, ["--check-clock", "--now", "2026-10-31 12:00", "--force-time"])[0], 0)
        code, _, err = run(pipe, ["--now", "2026-10-30 17:00"])
        self.assertEqual(code, 2)
        self.assertIn("config.json", err)
        self.config(mode="intraday")
        code, _, err = run(pipe, ["--now", "2026-10-30 17:00"])
        self.assertIn('"mode": "swing"', err)

    def test_budget_from_the_environment(self):
        self.config(budget=None)
        os.environ["TRADER_BUDGET"] = "20000"
        plan = {"mode": "swing", "symbol": "WMT", "side": "buy", "date": "2026-10-30", "setup": "breakout20",
                "entry": 123.51, "stop": 118.22, "target": 134.09, "time_stop_days": 7, "time_stop_date": "2026-11-10",
                "earnings": TICKET["earnings"]}
        with open("trader/plan.json", "w") as fh:
            json.dump(plan, fh)
        code, text, err = run(size, ["--now", "2026-11-02 09:00", "--json", "--timemachine", "none"])
        self.assertEqual(code, 0, err)
        s = json.loads(text)["sizing"]
        self.assertEqual((s["account"], s["qty"], s["bound_by"]), (20000.0, 32, "max order"))   # $4,000 / 123.51
        self.assertEqual([l["shares"] for l in s["limits"] if l["name"] == "max loss"], [37])   # 1 % = 200 / 5.29


# ---------------------------------------------------------------- safety and privacy

NEW_FILES = ["trader-alerts/notify_telegram.py", "trader-alerts/swing_pipeline.py", "trader-scanner/watchlist_top.py",
             "trader-analyst/earnings.py", ".github/workflows/swing-daily.yml"]


class Safety(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_no_flow_places_orders_or_touches_trader_broker(self):
        for rel in NEW_FILES:
            src = self.read(rel)
            code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
            code = re.sub(r'"""[\s\S]*?"""', "", code)
            for bad in ("trader-broker", "ticket.py", "place_stock_order", "alpaca", "ibkr"):
                self.assertNotIn(bad, code.lower(), (rel, bad))
            self.assertIsNone(re.search(r"(?<!swing_)orders\.json", code), rel)   # never the broker's orders file
        self.assertNotIn("broker", pipe.TOOLS)
        self.assertEqual(sorted(f for f, _ in pipe.TOOLS.values()),
                         sorted(["trader-scanner", "trader-news", "trader-analyst", "trader-exit", "trader-timemachine",
                                 "trader-budget", "trader-risk", "trader-alerts", "trader-alerts"]))

    def test_gitignore_keeps_private_files_out(self):
        gi = self.read(".gitignore").split()
        self.assertIn("trader/", gi)

    def test_workflow_secrets_and_no_artifacts(self):
        wf = self.read(".github/workflows/swing-daily.yml")
        for s in ("secrets.SEC_USER_AGENT", "secrets.TELEGRAM_BOT_TOKEN", "secrets.TELEGRAM_CHAT_ID",
                  "secrets.TRADER_BUDGET", "--quiet", "--check-clock", "actions/cache/save", "contents: read"):
            self.assertIn(s, wf)
        self.assertNotIn("upload-artifact", wf)          # public repo: artifacts are downloadable
        self.assertNotIn("git push", wf)
        self.assertNotIn("git commit", wf)
        self.assertRegex(wf, r'cron: "40 20 \* \* 1-5"')
        self.assertRegex(wf, r'cron: "40 21 \* \* 1-5"')

    def test_no_secrets_in_the_repo(self):
        rx = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")      # a real Telegram bot token
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "fixtures")]
            for f in files:
                if f.endswith((".py", ".md", ".json", ".yml", ".txt")):
                    with open(os.path.join(base, f), encoding="utf-8", errors="replace") as fh:
                        self.assertIsNone(rx.search(fh.read()), f)


if __name__ == "__main__":
    unittest.main()
