"""trader-alerts/orb_paper.py against a fake Alpaca PAPER API: the 09:00-10:00 box, the signal, the entry with
its OCO (70% at 1.7R or the stop) and the runner's stop, breakeven after the partial, the 20 day limit, and
that it only ever talks to the paper API."""

import datetime
import os
import re
import tempfile
import unittest

from _load import load, ROOT

orb = load("trader-alerts", "orb_paper")
paper = orb.paper

DAY = datetime.date(2026, 11, 18)          # a Wednesday


def ny(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(h, m))


def utc(day, hhmm):
    return orb.scan.eastern_to_utc(ny(day, hhmm))


def bars(day=DAY, moves=None, until="12:55", base=100.0):
    """5 minute bars from 09:00, flat at base +- 0.1 (the box is set by moves['09:00'])."""
    out, t = [], ny(day, "09:00")
    end = ny(day, until)
    while t <= end:
        o, h, l, c = (moves or {}).get(t.strftime("%H:%M"), (base, base + 0.1, base - 0.1, base))
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        t += datetime.timedelta(minutes=5)
    return out


BOX = {"09:00": (100, 101, 99, 100)}


class FakePaper(object):
    def __init__(self, equity=100000.0, fill=102.05):
        self.orders, self.positions, self.calls = {}, {}, []
        self.equity, self.fill, self.n = equity, fill, 0

    def http(self, method, url, body=None):
        assert url.startswith("https://paper-api.alpaca.markets/"), url
        path = url[len("https://paper-api.alpaca.markets"):]
        self.calls.append((method, path, body))
        if path == "/v2/account":
            return {"equity": str(self.equity)}
        if path == "/v2/positions":
            return list(self.positions.values())
        if path.startswith("/v2/orders:by_client_order_id"):
            c = path.split("client_order_id=")[1]
            for o in self.orders.values():
                if o["client_order_id"] == c:
                    return o
            raise paper.PaperError("Alpaca paper answered HTTP 404: order not found")
        if path.startswith("/v2/orders?") and method == "GET":
            q = dict(kv.split("=") for kv in path.split("?")[1].split("&"))
            out = [o for o in self.orders.values() if o["symbol"] == q.get("symbols", o["symbol"])]
            if q["status"] == "open":
                out = [o for o in out if o["status"] in ("new", "accepted", "held")]
            elif q["status"] == "closed":
                out = [o for o in out if o["status"] in ("filled", "canceled", "replaced")]
            return sorted(out, key=lambda o: -o["n"])
        if path == "/v2/orders" and method == "POST":
            return self.add(dict(body))
        if method == "PATCH":
            old = self.orders[path.split("/")[-1]]
            old["status"] = "replaced"
            new = dict(old, **body)
            new.pop("id")
            new["status"] = "new"
            return self.add(new)
        if method == "DELETE" and path.startswith("/v2/positions/"):
            self.positions.pop(path.split("/")[-1], None)
            return {}
        if method == "DELETE" and path.startswith("/v2/orders/"):
            self.orders[path.split("/")[-1]]["status"] = "canceled"
            return {}
        raise AssertionError("unexpected call %s %s" % (method, path))

    def add(self, o):
        self.n += 1
        o["id"], o["n"] = "id%d" % self.n, self.n
        o.setdefault("status", "new")
        # an entry limit fills now if the price is inside it; if not, an ioc is cancelled and a day order rests
        entry_limit = o.get("type") == "limit" and o.get("order_class") != "oco" and o.get("time_in_force") in ("ioc", "day")
        marketable = entry_limit and (
            (o["side"] == "buy" and self.fill <= float(o["limit_price"])) or
            (o["side"] == "sell" and self.fill >= float(o["limit_price"])))
        if entry_limit and not marketable:
            if o.get("time_in_force") == "ioc":
                o["status"], o["filled_qty"] = "canceled", "0"
            else:
                o["status"], o["filled_qty"] = "new", "0"
        elif o.get("type") == "market" or marketable:
            o["status"], o["filled_avg_price"], o["filled_qty"] = "filled", str(self.fill), o["qty"]
            q = float(o["qty"]) * (1 if o["side"] == "buy" else -1)
            self.positions[o["symbol"]] = {"symbol": o["symbol"], "qty": str(q),
                                           "side": "long" if q > 0 else "short", "current_price": str(self.fill)}
        self.orders[o["id"]] = o
        return o

    def posts(self):
        return [b for m, p, b in self.calls if m == "POST"]


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["ALPACA_API_KEY_ID"] = "k"
        os.environ["ALPACA_API_SECRET_KEY"] = "s"
        for k in ("ORB_RISK_PCT", "ORB_MAX_NOTIONAL_PCT", "ORB_SYMBOLS"):
            os.environ.pop(k, None)
        os.environ["ORB_SYMBOLS"] = "QQQ"
        self.tmp = tempfile.mkdtemp()
        self.old_state = orb.STATE
        orb.STATE = os.path.join(self.tmp, "orb.json")
        self.fake = FakePaper()
        self.acct = orb.Account(paper.Paper(http=self.fake.http))

    def tearDown(self):
        orb.STATE = self.old_state
        os.environ.pop("ORB_SYMBOLS", None)

    def run_at(self, hhmm, moves, dry_run=False, state=None, day=DAY):
        b = bars(day, dict(BOX, **moves), until=hhmm)
        return orb.run_pass(self.acct, utc(day, hhmm), {"QQQ": b}, dry_run=dry_run, state=state,
                            sleep=lambda s: None)


class Box(unittest.TestCase):
    def test_box_and_first_close_outside(self):
        b = bars(moves=dict(BOX, **{"10:00": (100, 100.5, 99.5, 100.2), "10:10": (100, 102.5, 100, 102)}))
        box = orb.box_of(b, DAY)
        self.assertEqual(box, (101, 99, 12))
        bar, d = orb.first_signal(b, DAY, box, ny(DAY, "10:16"))
        self.assertEqual((bar["t"].strftime("%H:%M"), d), ("10:10", 1))

    def test_a_forming_bar_is_not_a_signal(self):
        b = bars(moves=dict(BOX, **{"10:10": (100, 102.5, 100, 102)}))
        self.assertIsNone(orb.first_signal(b, DAY, orb.box_of(b, DAY), ny(DAY, "10:13")))

    def test_short_and_the_1300_limit(self):
        b = bars(moves=dict(BOX, **{"13:00": (100, 100, 97.5, 98)}), until="13:10")
        self.assertIsNone(orb.first_signal(b, DAY, orb.box_of(b, DAY), ny(DAY, "13:11")))
        b = bars(moves=dict(BOX, **{"12:55": (100, 100, 97.5, 98)}), until="13:00")
        self.assertEqual(orb.first_signal(b, DAY, orb.box_of(b, DAY), ny(DAY, "13:01"))[1], -1)

    def test_box_needs_bars(self):
        b = [x for x in bars(moves=BOX) if x["t"].time() >= datetime.time(9, 50)]
        self.assertIsNone(orb.box_of(b, DAY))

    def test_plan(self):
        p = orb.plan(1, 102.0, (101, 99, 12), 100000.0)
        # 0.25% of 100000 = 250 over a risk of 3 -> 83 shares; 70% = 58 at 102 + 1.7 * 3
        self.assertEqual((p["qty"], p["partial_qty"], p["runner_qty"], p["stop"], p["partial_px"]), (83, 58, 25, 99, 107.1))
        os.environ["ORB_MAX_NOTIONAL_PCT"] = "5"
        self.assertEqual(orb.plan(1, 102.0, (101, 99, 12), 100000.0)["qty"], 49)
        os.environ.pop("ORB_MAX_NOTIONAL_PCT")
        self.assertIsNone(orb.plan(1, 102.0, (101, 99, 12), 500.0))


class Entry(Base):
    SIGNAL = {"10:00": (100, 102.5, 100, 102)}             # closes at 10:05 above the box

    def test_entry_with_its_exits(self):
        events = self.run_at("10:06", self.SIGNAL)
        posts = self.fake.posts()
        self.assertEqual([p["client_order_id"] for p in posts], ["orb-QQQ-20261118-in", "orb-QQQ-20261118-tp",
                                                                 "orb-QQQ-20261118-run"])
        entry, oco, run = posts
        # a limit at the bar close (102) plus 0.15R of the 3.00 risk, good for the day (cancelled at 13:00)
        self.assertEqual((entry["type"], entry["side"], entry["qty"], entry["time_in_force"], entry["limit_price"]),
                         ("limit", "buy", "83", "day", "102.45"))
        # the OCO: 58 shares at fill + 1.7 x (fill - stop), or the stop at the box low
        self.assertEqual((oco["order_class"], oco["side"], oco["qty"]), ("oco", "sell", "58"))
        # measured from the close of the breakout bar (102, stop 99), like the backtest, not from the 102.05 fill
        self.assertEqual(oco["take_profit"]["limit_price"], "%.2f" % (102 + 1.7 * 3))
        self.assertEqual(oco["stop_loss"]["stop_price"], "99.00")
        self.assertEqual((run["type"], run["qty"], run["stop_price"], run["time_in_force"]), ("stop", "25", "99.00", "gtc"))
        self.assertTrue(any("entro a 102.05" in e[1] for e in events))

    def test_once_a_day(self):
        self.run_at("10:06", self.SIGNAL)
        self.fake.positions.clear()                            # even if it already closed
        self.run_at("10:11", self.SIGNAL)
        self.assertEqual(len(self.fake.posts()), 3)

    def test_dry_run_places_nothing(self):
        events = self.run_at("10:06", self.SIGNAL, dry_run=True)
        self.assertEqual(self.fake.posts(), [])
        self.assertTrue(any("largo 83 a mercado" in e[1] for e in events))

    def test_late_signal_is_skipped_once(self):
        state = {}
        events = self.run_at("10:20", self.SIGNAL, state=state)
        self.assertEqual(self.fake.posts(), [])
        self.assertTrue(any("se paso la senal de las 10:05" in e[1] for e in events))
        self.assertEqual(self.run_at("10:25", self.SIGNAL, state=state), [])

    def test_no_entry_after_1315_or_on_a_holiday(self):
        self.assertEqual(self.run_at("13:20", {"13:10": (100, 102.5, 100, 102)}), [])
        self.assertEqual(self.fake.posts(), [])
        events = self.run_at("10:06", self.SIGNAL, day=datetime.date(2026, 11, 26))      # Thanksgiving
        self.assertIn("mercado cerrado", events[0][1])

    def test_one_position_at_a_time(self):
        self.fake.positions["QQQ"] = {"symbol": "QQQ", "qty": "25", "side": "long"}
        self.fake.orders["old"] = {"id": "old", "n": 0, "symbol": "QQQ", "client_order_id": "orb-QQQ-20261117-in",
                                   "status": "filled", "side": "buy", "filled_avg_price": "100"}
        self.fake.orders["oldrun"] = {"id": "oldrun", "n": 0, "symbol": "QQQ", "client_order_id": "orb-QQQ-20261117-run",
                                      "status": "new", "type": "stop", "stop_price": "98"}
        self.run_at("10:06", self.SIGNAL)
        self.assertEqual(self.fake.posts(), [])

    def test_after_a_runner_closes_only_later_bars(self):
        state = {"open": {"QQQ": True}}                       # open on the last pass, gone now (11:01)
        moves = {"10:00": (100, 102.5, 100, 102)}
        self.run_at("11:01", moves, state=state)
        self.assertEqual(state["closed_today"], {"QQQ-2026-11-18": "11:01"})
        self.assertEqual(self.fake.posts(), [])               # the 10:00 break came before
        moves["11:00"] = (100, 100, 97.5, 98)
        self.run_at("11:06", moves, state=state)
        self.assertEqual(self.fake.posts()[0]["side"], "sell")


    def test_a_lost_state_still_waits_for_the_runner_to_close(self):
        # yesterday's runner stopped out at 11:01 (Alpaca's fill time); the saved state was lost, so only
        # Alpaca says when. The 10:55 break (closed 11:00, inside the 10 minute grace) came while it was open.
        self.fake.orders["y"] = {"id": "y", "n": 0, "symbol": "QQQ", "client_order_id": "orb-QQQ-20261117-run-be",
                                 "status": "filled", "type": "stop", "side": "sell", "filled_at": "2026-11-18T16:01:30Z"}
        moves = {"10:55": (100, 102.5, 100, 102)}
        self.run_at("11:05", moves, state={})
        self.assertEqual(self.fake.posts(), [])
        moves["11:00"] = (102, 102.6, 101.5, 102.4)            # a close outside after the runner closed
        self.run_at("11:06", moves, state={})
        self.assertEqual(self.fake.posts()[0]["side"], "buy")

    def test_the_stop_leg_of_the_oco_counts_too(self):
        leg = {"client_order_id": "x-leg", "status": "filled", "type": "stop", "filled_at": "2026-11-18T16:01:00Z"}
        self.fake.orders["y"] = {"id": "y", "n": 0, "symbol": "QQQ", "client_order_id": "orb-QQQ-20261117-tp",
                                 "status": "canceled", "type": "limit", "legs": [leg]}
        self.assertEqual(orb.closed_today_at(self.acct, "QQQ", DAY), datetime.time(11, 1))
        self.assertIsNone(orb.closed_today_at(self.acct, "QQQ", DAY + datetime.timedelta(days=1)))


    def test_audit_measures_the_entry_against_the_rule(self):
        # breakout bar 10:00-10:05 closes at 102 (box 99-101, stop 99, risk 3); the market order fills at 102.05
        self.run_at("10:06", self.SIGNAL)
        o = [x for x in self.fake.orders.values() if x["client_order_id"].endswith("-in")][0]
        o["filled_at"] = "2026-11-18T15:06:30Z"                       # 10:06:30 New York
        b = bars(DAY, dict(BOX, **self.SIGNAL), until="10:30")
        r, = orb.audit_rows(self.acct, utc(DAY, "10:30"), {"QQQ": b}, DAY)
        self.assertEqual((r["bar"], r["delay_s"]), ("10:00", 90))
        self.assertAlmostEqual(r["slip_R"], -0.05 / 3)                 # bought 0.05 above the close: worse, in R
        self.assertAlmostEqual(r["tp_R_from_close"], 1.7, places=2)    # the 70% is 1.7R from the bar close
        self.assertTrue(r["stop_is_box_edge"])
        self.assertIn("ejecutada 90 s", orb.audit_text([r]))

    def test_the_watcher_enters_seconds_after_the_bar_closes(self):
        # the 10:00 bar closes outside the box at 10:05:00; the watcher passes at 10:05:06, not at the next
        # scheduled pass a minute or two later
        clock = {"t": utc(DAY, "10:02")}
        slept = []

        def sleep(sec):
            slept.append(sec)
            clock["t"] += datetime.timedelta(seconds=sec)

        def fetch(now):
            et = orb.scan.to_eastern(now)
            return {"QQQ": [b for b in bars(DAY, dict(BOX, **self.SIGNAL), until="10:30")
                            if b["t"] + datetime.timedelta(minutes=5) <= et]}
        sent = []
        n = orb.watch(self.acct, "10:11", False, sent.append, clock=lambda: clock["t"], fetch=fetch, sleep=sleep)
        entry = [x for x in self.fake.orders.values() if x["client_order_id"].endswith("-in")]
        self.assertEqual(len(entry), 1)
        self.assertEqual(slept[0], 3 * 60 + 2)                      # 10:02 -> 10:05:02
        self.assertIn("largo", sent[0])
        self.assertEqual(n, 3)                                      # 10:02, 10:05:02 and 10:10:02

    def test_bar_ready(self):
        b = bars(DAY, BOX, until="10:05")
        self.assertTrue(orb._bar_ready({"QQQ": b}, ny(DAY, "10:10").replace(second=6)))
        self.assertFalse(orb._bar_ready({"QQQ": b[:-1]}, ny(DAY, "10:10").replace(second=6)))

class Manage(Base):
    def open_trade(self, day="20261118", partial=False, runner_stop="99.00"):
        self.fake.positions["QQQ"] = {"symbol": "QQQ", "qty": "25" if partial else "83", "side": "long"}
        self.fake.orders["e"] = {"id": "e", "n": 1, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-in" % day,
                                 "status": "filled", "side": "buy", "filled_avg_price": "102.05", "type": "market"}
        self.fake.orders["t"] = {"id": "t", "n": 2, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-tp" % day,
                                 "status": "filled" if partial else "new", "type": "limit"}
        self.fake.orders["r"] = {"id": "r", "n": 3, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-run" % day,
                                 "status": "new", "type": "stop", "stop_price": runner_stop, "qty": "25"}

    def test_breakeven_after_the_partial(self):
        self.open_trade(partial=True)
        events = self.run_at("12:31", {})
        patch = [(p, b) for m, p, b in self.fake.calls if m == "PATCH"]
        self.assertEqual(patch, [("/v2/orders/r", {"stop_price": "102.05", "client_order_id": "orb-QQQ-20261118-run-be"})])
        self.assertTrue(any("stop del resto a la entrada 102.05" in e[1] for e in events))
        self.run_at("12:36", {})                               # already at breakeven: nothing more
        self.assertEqual(len([1 for m, p, b in self.fake.calls if m == "PATCH"]), 1)

    def test_partial_seen_by_the_shares_when_its_order_was_replaced(self):
        self.open_trade(partial=True)
        self.fake.orders["e"]["filled_qty"] = "83"
        self.fake.orders["t"]["status"] = "replaced"              # edited by hand in Alpaca: a new id took over
        self.run_at("12:31", {})
        self.assertTrue([p for m, p, b in self.fake.calls if m == "PATCH"])

    def test_no_breakeven_before_the_partial(self):
        self.open_trade(partial=False)
        self.run_at("12:31", {})
        self.assertEqual([c for c in self.fake.calls if c[0] in ("PATCH", "POST")], [])

    def test_closed_after_20_trading_days(self):
        self.open_trade(day="20261020")
        self.run_at("15:31", {}, day=DAY)
        self.assertNotIn("QQQ", self.fake.positions)
        self.assertEqual(self.fake.orders["r"]["status"], "canceled")

    def test_missing_exits_are_put_back(self):
        self.open_trade()
        self.fake.orders.pop("t")
        self.fake.orders.pop("r")
        self.run_at("12:31", {})
        posts = self.fake.posts()
        self.assertEqual([p["client_order_id"] for p in posts], ["orb-QQQ-20261118-tp", "orb-QQQ-20261118-run"])
        self.assertEqual(posts[0]["stop_loss"]["stop_price"], "99.00")         # rebuilt from today's box

    def test_a_position_that_is_not_ours_is_left_alone(self):
        self.fake.positions["QQQ"] = {"symbol": "QQQ", "qty": "10", "side": "long"}
        state = {}
        self.run_at("12:31", {}, state=state)
        self.run_at("12:36", {}, state=state)
        self.assertEqual([c for c in self.fake.calls if c[0] in ("PATCH", "POST", "DELETE")], [])


class Safety(unittest.TestCase):
    def test_paper_only(self):
        with open(os.path.join(ROOT, "trader-alerts", "orb_paper.py"), encoding="utf-8") as fh:
            src = fh.read()
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        self.assertEqual(re.findall(r"https?://", code), [])          # every call goes through paper_trader.Paper
        for bad in ("trader-broker", "ticket.py", "api.alpaca.markets"):
            self.assertNotIn(bad, code)
        self.assertIn("paper.Paper()", code)
        with self.assertRaises(paper.PaperError):
            paper._http("POST", "https://api.alpaca.markets/v2/orders", {})

    def test_workflow(self):
        with open(os.path.join(ROOT, ".github", "workflows", "caja-diaria.yml"), encoding="utf-8") as fh:
            wf = fh.read()
        for must in ("orb_paper.py run", "vars.PAPER_TRADING", "secrets.ALPACA_API_KEY_ID", "secrets.TELEGRAM_BOT_TOKEN",
                     "actions/cache/restore", "concurrency"):
            self.assertIn(must, wf)
        self.assertNotIn("upload-artifact", wf)


if __name__ == "__main__":
    unittest.main()


class CloseReport(Base):
    def fill_day(self):
        day = "20261119"
        self.fake.orders.update({
            "e": {"id": "e", "n": 1, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-in" % day, "status": "filled",
                  "side": "sell", "type": "market", "filled_avg_price": "667.56", "filled_qty": "30", "qty": "30",
                  "filled_at": "2026-11-19T15:16:30Z"},
            "t": {"id": "t", "n": 2, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-tp" % day, "status": "filled",
                  "side": "buy", "type": "limit", "filled_avg_price": "654.57", "filled_qty": "21",
                  "filled_at": "2026-11-19T16:52:10Z",
                  "legs": [{"symbol": "QQQ", "status": "canceled", "type": "stop", "stop_price": "675.20", "side": "buy"}]},
            "r": {"id": "r", "n": 3, "symbol": "QQQ", "client_order_id": "orb-QQQ-%s-run-be" % day, "status": "new",
                  "side": "buy", "type": "stop", "stop_price": "667.56", "qty": "9"}})
        self.fake.positions["QQQ"] = {"symbol": "QQQ", "qty": "-9", "side": "short", "current_price": "652.00"}

    def test_the_report(self):
        self.fill_day()
        text = orb.report_text(self.acct, utc(datetime.date(2026, 11, 19), "16:06"))
        for must in ("Entradas de hoy (1)", "QQQ CORTO", "Salidas de hoy (1)", "70% a 1,7R", "+1.19R",
                     "Abiertas (1)", "cobrado +1.19R", "(breakeven)", "Cobrado hoy"):
            self.assertIn(must, text)

    def test_once_a_day_after_the_close(self):
        self.fill_day()
        sent = []
        self.assertFalse(orb.close_report_once(self.acct, utc(datetime.date(2026, 11, 19), "15:56"), sent.append))
        self.assertTrue(orb.close_report_once(self.acct, utc(datetime.date(2026, 11, 19), "16:01"), sent.append))
        self.assertFalse(orb.close_report_once(self.acct, utc(datetime.date(2026, 11, 19), "16:06"), sent.append))
        self.assertEqual(len(sent), 1)


class PublicLog(unittest.TestCase):
    def test_only_symbols_reach_the_ci_log(self):
        line = orb.log_line([("QQQ", "corto 30 a mercado (cierre 667.56)"), ("SPY", "parcial cobrado +$325")])
        self.assertEqual(line, "2 aviso(s) enviados a Telegram: QQQ, SPY")


class ClosedMessage(CloseReport):
    def test_the_rest_stopped_at_breakeven_is_announced(self):
        self.fill_day()
        self.fake.orders["r"].update(status="filled", filled_avg_price="667.56", filled_qty="9",
                                     filled_at="2026-11-20T15:40:00Z")
        self.fake.positions.pop("QQQ")
        state = {"open": {"QQQ": True}}                       # open on the last pass, gone now
        events = orb.run_pass(self.acct, utc(datetime.date(2026, 11, 20), "10:41"), {"QQQ": []}, state=state,
                              sleep=lambda s: None)
        (sym, text), = [e for e in events if "cerrada" in e[1]]
        for must in ("70% a 1,7R +1.19R", "breakeven (30%) +0.00R", "total +1.19R", "+$273"):
            self.assertIn(must, text)
        again = orb.run_pass(self.acct, utc(datetime.date(2026, 11, 20), "10:46"), {"QQQ": []}, state=state,
                             sleep=lambda s: None)
        self.assertFalse([e for e in again if "cerrada" in e[1]])   # said once


class PreciseEntry(Base):
    SIGNAL = {"10:00": (100, 102.5, 100, 102)}             # closes at 102 above the box 99-101: risk 3

    def test_the_limit_rests_fills_later_and_gets_its_exits(self):
        self.fake.fill = 102.60                             # past the 102.45 cap: the limit waits
        events = self.run_at("10:06", self.SIGNAL)
        self.assertTrue(any("queda esperando hasta las 13:00" in e[1] for e in events))
        self.assertEqual([b["client_order_id"] for b in self.fake.posts()], ["orb-QQQ-20261118-in"])
        self.run_at("10:11", self.SIGNAL)                   # no second entry while it waits
        self.assertEqual(len(self.fake.posts()), 1)
        o = [x for x in self.fake.orders.values() if x["client_order_id"].endswith("-in")][0]
        o.update(status="filled", filled_qty="83", filled_avg_price="102.45")     # the price came back
        self.fake.positions["QQQ"] = {"symbol": "QQQ", "qty": "83", "side": "long"}
        events = self.run_at("10:16", self.SIGNAL)
        self.assertTrue(any("se lleno la orden limite" in e[1] for e in events))
        oco = [b for b in self.fake.posts() if b.get("order_class") == "oco"][0]
        self.assertEqual(oco["take_profit"]["limit_price"], "107.10")             # 1.7R from the bar close

    def test_a_limit_still_waiting_at_13_is_cancelled(self):
        self.fake.fill = 102.60
        self.run_at("10:06", self.SIGNAL)
        events = self.run_at("13:03", self.SIGNAL)
        o = [x for x in self.fake.orders.values() if x["client_order_id"].endswith("-in")][0]
        self.assertEqual(o["status"], "canceled")
        self.assertTrue(any("cancelada, hoy no opera" in e[1] for e in events))

    def test_a_partial_fill_gets_exits_for_its_shares(self):
        real = self.fake.add

        def half(o):
            o = real(o)
            if o["client_order_id"].endswith("-in"):
                o["status"], o["filled_qty"] = "canceled", "10"     # IOC: 10 of 83, the rest cancelled
                self.fake.positions["QQQ"]["qty"] = "10"
            return o
        self.fake.add = half
        self.run_at("10:06", self.SIGNAL)
        oco = [b for b in self.fake.posts() if b.get("order_class") == "oco"][0]
        run = [b for b in self.fake.posts() if b["client_order_id"].endswith("-run")][0]
        self.assertEqual((oco["qty"], run["qty"]), ("7", "3"))


class WatcherWithARestingLimit(Base):
    SIGNAL = {"10:00": (100, 102.5, 100, 102)}

    def test_checks_every_10_s_while_a_limit_waits(self):
        self.fake.fill = 102.60                                     # the entry limit will rest
        clock = {"t": utc(DAY, "10:05") + datetime.timedelta(seconds=2)}
        slept = []

        def sleep(sec):
            slept.append(sec)
            clock["t"] += datetime.timedelta(seconds=sec)

        def fetch(now):
            et = orb.scan.to_eastern(now)
            return {"QQQ": [b for b in bars(DAY, dict(BOX, **self.SIGNAL), until="10:30")
                            if b["t"] + datetime.timedelta(minutes=5) <= et]}
        orb.watch(self.acct, "10:12", False, None, clock=lambda: clock["t"], fetch=fetch, sleep=sleep)
        self.assertGreaterEqual(slept.count(10), 20)                # checked every 10 s from 10:05 to 10:10
        self.assertTrue(all(x <= 10 for x in slept))                # never a 5 minute wait while it rests
