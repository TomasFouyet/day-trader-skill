#!/usr/bin/env python3
"""The daily swing pipeline, after the US close. Standard library only.

    python3 swing_pipeline.py --dry-run          # everything, then print the Telegram messages instead of sending
    python3 swing_pipeline.py                    # everything, then send the Telegram notices
    python3 swing_pipeline.py --force-time       # skip the "is it after today's close?" check

The steps, each one the pack's own tool run in this process:
  1. scanner     trader-scanner scan.py swing -> trader/swing.json
                 (trader/watchlist.txt if it exists, else the universe)
  2. news        trader-news news.py on the candidates -> trader/news.json (best effort)
  3. earnings    trader-analyst earnings.py check: flags and blocks reports inside the horizon
  4. per candidate (up to --max-candidates, earnings-blocked ones skipped):
       exit      trader-exit plan --mode swing --pick SYM -> trader/plans/SYM.json
       time      trader-timemachine run -> trader/timemachine/SYM.json
       budget    trader-budget -> a manual ticket in trader/swing_orders.json
  5. risk        trader-risk check.py --swing -> trader/state/swing_risk.json
  5b. premarket  (morning) premarket.py: the pre-market price of each ticket: ok, gap or invalid
  6. alerts      trader-alerts: stop, target and time-stop rules for each PASS -> trader/alerts.json
  7. notify      notify_telegram.py: one message per new PASS

The clock: it runs only on an NYSE trading day and once a day
(trader/state/pipeline.json). By default in the MORNING, 8:15 to 9:59 New York,
on the previous session's close, with a pre-market check of each ticket
(premarket.py: gap, still valid, size from the pre-market price). With
--session evening, at or after 15 minutes past the close instead. New York time is worked out here, daylight time
included, so a scheduler in UTC can fire at two times and the right one runs.

It never places an order and never imports trader-broker: the tickets are for
you to buy by hand. With --quiet (CI) it prints step names and counts only, no
symbols, sizes or account numbers, because public CI logs are public.
"""

import argparse
import datetime
import glob
import importlib.util
import io
import json
import os
import shutil
import sys
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = {"scan": ("trader-scanner", "scan"), "news": ("trader-news", "news"),
         "earnings": ("trader-analyst", "earnings"), "exit": ("trader-exit", "exit"),
         "tm": ("trader-timemachine", "timemachine"), "budget": ("trader-budget", "size"),
         "risk": ("trader-risk", "check"), "alerts": ("trader-alerts", "alerts"),
         "notify": ("trader-alerts", "notify_telegram"), "premarket": ("trader-alerts", "premarket")}
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}
AFTER_CLOSE_MIN = 15
MORNING_FROM = 8 * 60 + 15      # the morning run: 8:15 to 9:59 New York, before and just after the open
MORNING_TO = 10 * 60
STATE = "trader/state/pipeline.json"


class PipelineError(Exception):
    pass


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """-4 in daylight time (second Sunday of March to first Sunday of November), -5 otherwise."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def parse_now(text):
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        for off in (-4, -5):
            utc = t - datetime.timedelta(hours=off)
            if eastern_offset(utc) == off:
                return utc
        return t + datetime.timedelta(hours=5)
    return (t - t.utcoffset()).replace(tzinfo=None)


def market_check(now_utc, session="morning"):
    """(ok, why) in New York time, on an NYSE trading day. "morning": 8:15 to 9:59, before the open, on
    yesterday's close. "evening": at or after the close + 15 minutes."""
    et = to_eastern(now_utc)
    d = et.date().isoformat()
    if et.weekday() >= 5:
        return False, "fin de semana en Nueva York (%s)" % d
    if d in NYSE_HOLIDAYS:
        return False, "feriado NYSE (%s)" % d
    mins = et.hour * 60 + et.minute
    if session == "morning":
        if not MORNING_FROM <= mins < MORNING_TO:
            return False, "en Nueva York son las %s: la corrida de la manana va de 08:15 a 09:59" % et.strftime("%H:%M")
        return True, "Nueva York %s %s, antes de la apertura (senales del cierre anterior)" % (d, et.strftime("%H:%M"))
    close = 13 * 60 if d in EARLY_CLOSE else 16 * 60
    mins = et.hour * 60 + et.minute
    if mins < close + AFTER_CLOSE_MIN:
        return False, "en Nueva York son las %s: corre despues de las %02d:%02d" % (
            et.strftime("%H:%M"), (close + AFTER_CLOSE_MIN) // 60, (close + AFTER_CLOSE_MIN) % 60)
    return True, "Nueva York %s %s, despues del cierre" % (d, et.strftime("%H:%M"))


def load_tool(name):
    folder, mod = TOOLS[name]
    path = os.path.join(ROOT, folder, mod + ".py")
    spec = importlib.util.spec_from_file_location("pipeline_" + mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Runner(object):
    def __init__(self, quiet=False):
        self.quiet, self.mods, self.log = quiet, {}, []

    def run(self, name, argv, label=None):
        if name not in self.mods:
            self.mods[name] = load_tool(name)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.mods[name].main(argv)
        except SystemExit as e:          # argparse
            code = e.code if isinstance(e.code, int) else 2
        except Exception as e:            # one tool failing must not hide the others' results
            code = 99
            err.write("%s: %s: %s\n" % (name, type(e).__name__, e))
        self.log.append({"step": label or name, "code": code})
        if not self.quiet:
            text = out.getvalue().rstrip()
            print("== %s (exit %s)" % (label or name, code))
            if text:
                print(text)
            if err.getvalue().strip():
                print(err.getvalue().rstrip())
        else:
            print("== %s: exit %s" % (label or name, code))
        return code, out.getvalue()


def read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return None


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)


def clean_run_outputs():
    for p in ("trader/swing_orders.json", "trader/swing.json", "trader/news.json", "trader/state/swing_risk.json"):
        if os.path.exists(p):
            os.remove(p)
    for d in ("trader/plans", "trader/timemachine"):
        if os.path.isdir(d):
            shutil.rmtree(d)


def pipeline(a):
    now = parse_now(a.now)
    et = to_eastern(now)
    today = et.date().isoformat()
    ok, why = market_check(now, a.session)
    print("Reloj: " + why + ".")
    if not ok and not a.force_time:
        print("No es hora: no se corre nada (usa --force-time para forzar).")
        return 1 if a.check_clock else 0
    state = read_json(STATE) or {}
    if state.get("last_ok") == today and not a.force:
        print("Ya corrio hoy (%s): nada que hacer (usa --force para repetir)." % today)
        return 1 if a.check_clock else 0
    if a.check_clock:
        print("Hay que correr hoy.")
        return 0
    if not os.path.exists("trader/config.json"):
        raise PipelineError("no trader/config.json: copia trader-budget/config.swing.example.json")
    cfg = read_json("trader/config.json") or {}
    if str(cfg.get("mode") or "").lower() != "swing":
        raise PipelineError('trader/config.json tiene que decir "mode": "swing"')
    ny = et.strftime("%Y-%m-%d %H:%M")
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    clean_run_outputs()
    r = Runner(a.quiet)

    scan_argv = ["swing", "--now", ny, "--quiet"]
    if a.offline_charts:
        scan_argv += ["--offline", a.offline_charts]
    r.run("scan", scan_argv, "scanner")
    doc = read_json("trader/swing.json") or {}
    cands = doc.get("candidates") or []
    print("Candidatos: %d." % len(cands))
    if cands and not a.skip_news:
        code, text = r.run("news", ["--watchlist", "trader/swing.json", "--json", "--dry-run", "--no-moves",
                                    "--now", iso, "--state", "trader/state/news.json"], "news")
        try:
            json.loads(text)
            with open("trader/news.json", "w", encoding="utf-8") as fh:
                fh.write(text)
        except ValueError:
            pass
    if cands:
        ea = ["check", "--now", iso]
        if os.path.exists("trader/news.json"):
            ea += ["--news", "trader/news.json"]
        if a.offline_sec:
            ea += ["--offline", a.offline_sec]
        r.run("earnings", ea, "resultados")
        cands = (read_json("trader/swing.json") or {}).get("candidates") or []
    planned = blocked = tickets = 0
    for c in cands:
        if planned >= a.max_candidates:
            break
        sym = c["symbol"]
        if (c.get("earnings") or {}).get("blocked"):
            blocked += 1
            continue
        plan = "trader/plans/%s.json" % sym
        tmf = "trader/timemachine/%s.json" % sym
        ex = ["plan", "--mode", "swing", "--from", "trader/swing.json", "--pick", sym, "--now", ny, "--write", plan]
        tma = ["run", "--plan", plan, "--now", ny, "--write", tmf, "--replays", str(a.replays)]
        if a.offline_charts:
            ex += ["--offline", a.offline_charts]
            tma += ["--offline", a.offline_charts]
        planned += 1
        if r.run("exit", ex, "exit")[0] != 0:
            continue
        r.run("tm", tma, "time machine")
        if r.run("budget", ["--plan", plan, "--timemachine", tmf, "--now", ny, "--write"], "budget")[0] == 0:
            tickets += 1
    print("Planificados %d, bloqueados por resultados %d, tickets %d." % (planned, blocked, tickets))
    passed = 0
    if tickets:
        r.run("risk", ["--swing", "--now", ny], "risk")
        if a.session == "morning":
            pa = ["--now", ny]
            if a.offline_premarket:
                pa += ["--offline", a.offline_premarket]
            r.run("premarket", pa, "pre-mercado")
        rk = read_json("trader/state/swing_risk.json") or {}
        passed = rk.get("pass", 0)
        for res in rk.get("results") or []:
            if res.get("verdict") == "PASS":
                r.run("alerts", ["add", "--from-plan", "--plan", "trader/plans/%s.json" % res["symbol"]], "alertas")
    print("Pasan risk: %d." % passed)
    if os.path.exists("trader/state/swing_risk.json") or a.heartbeat:
        na = ["--now", iso]
        if a.dry_run:
            na.append("--dry-run")
        if a.quiet:
            na.append("--quiet")
        if a.heartbeat:
            na.append("--heartbeat")
        if not os.path.exists("trader/state/swing_risk.json"):
            write_json("trader/state/swing_risk.json", {"mode": "swing", "date": today, "results": [], "pass": 0,
                                                        "block": 0})
            if not os.path.exists("trader/swing_orders.json"):
                write_json("trader/swing_orders.json", {"mode": "swing", "tickets": []})
        code, _ = r.run("notify", na, "telegram")
        if code not in (0,):
            state.update(last_error=today)
            write_json(STATE, state)
            return code
    state.update(last_run=iso, last_ok=today if not a.dry_run else state.get("last_ok"),
                 last_counts={"candidates": len(cands), "planned": planned, "earnings_blocked": blocked,
                              "tickets": tickets, "passed": passed},
                 steps=r.log)
    write_json(STATE, state)
    print("Listo. Ninguna orden fue colocada: los tickets son para comprar a mano.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily swing pipeline: scanner to Telegram, after the US close.")
    ap.add_argument("--dry-run", action="store_true", help="print the Telegram messages instead of sending")
    ap.add_argument("--force-time", action="store_true", help="run even if it is not after today's close")
    ap.add_argument("--force", action="store_true", help="run again even if it already ran today")
    ap.add_argument("--max-candidates", type=int, default=10, help="plan at most this many candidates")
    ap.add_argument("--replays", type=int, default=10000, help="Time Machine paths per candidate")
    ap.add_argument("--heartbeat", action="store_true", help="send a short Telegram note when nothing passed")
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--check-clock", action="store_true",
                    help="only say whether it should run now: exit 0 yes, 1 no (for the workflow)")
    ap.add_argument("--quiet", action="store_true", help="CI: step names and counts only")
    ap.add_argument("--offline-charts", metavar="DIR", help="tests: daily chart files instead of Yahoo")
    ap.add_argument("--offline-sec", metavar="DIR", help="tests: SEC files instead of EDGAR")
    ap.add_argument("--offline-premarket", metavar="DIR", help="tests: 1-minute charts with pre-market bars")
    ap.add_argument("--session", choices=["morning", "evening"], default="morning",
                    help="morning (default): 8:15 to 9:59 New York, with the pre-market check; evening: after the close")
    ap.add_argument("--now", help="pretend it is this time (New York, or ISO with Z)")
    a = ap.parse_args(argv)
    try:
        return pipeline(a)
    except PipelineError as e:
        print("swing_pipeline.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
