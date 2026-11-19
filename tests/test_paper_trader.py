"""trader-alerts/paper_trader.py against a fake Alpaca PAPER API: bracket orders for the setups that
passed today's risk check, fills read back, time stops closed, the track record."""

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

paper = load("trader-alerts", "paper_trader")
tg = load("trader-alerts", "notify_telegram")

TICKET = {"symbol": "WMT", "side": "buy", "qty": 9.45, "entry": 123.51, "stop": 118.22, "target": 134.09,
          "setup": "breakout20", "signal_date": "2026-11-09", "time_stop_date": "2026-11-18", "time_stop_days": 7,
          "timemachine": {"p_win": 0.55, "expectancy_R": 0.12, "p_target": 0.06, "p_stop": 0.24}}
SHORT = dict(TICKET, symbol="USO", side="sell", qty=4.139, entry=142.66, stop=154.74, target=118.50,
             setup="breakdown20")


class FakeAPI(object):
    def __init__(self):
        self.calls, self.orders, self.n = [], {}, 0

    def __call__(self, method, url, body=None):
        paper._check_url(url)
        self.calls.append((method, url.replace(paper.PAPER_URL, ""), body))
        path = url.replace(paper.PAPER_URL, "")
        if method == "POST" and path == "/v2/orders":
            self.n += 1
            oid = "o%d" % self.n
            self.orders[oid] = {"id": oid, "status": "accepted", "legs": [
                {"id": oid + "tp", "type": "limit", "status": "held"},
                {"id": oid + "sl", "type": "stop", "status": "held"}]}
            return {"id": oid, "status": "accepted"}
        if method == "GET" and path.startswith("/v2/orders/"):
            return self.orders[path.split("/")[3].split("?")[0]]
        if method == "DELETE" and path.startswith("/v2/positions/"):
            self.n += 1
            oid = "c%d" % self.n
            self.orders[oid] = {"id": oid, "status": "accepted"}
            return {"id": oid}
        if method == "DELETE":
            return {}
        if path == "/v2/account":
            return {"status": "ACTIVE", "equity": "5012.34"}
        raise AssertionError(path)


def risk_doc(tickets, date="2026-11-10"):
    return {"mode": "swing", "date": date,
            "results": [{"verdict": "PASS", "fingerprint": tg.swing_fingerprint(t)} for t in tickets]}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "PAPER_TRADING")}
        os.environ.update(ALPACA_API_KEY_ID="PK", ALPACA_API_SECRET_KEY="s")
        self.fake = FakeAPI()
        self.api = paper.Paper(self.fake)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class Submit(Base):
    def test_one_bracket_per_passed_setup(self):
        state = {}
        res = paper.submit(self.api, {"tickets": [TICKET, SHORT]}, risk_doc([TICKET, SHORT]), state, "2026-11-10")
        self.assertEqual([r[1] for r in res], ["orden paper enviada"] * 2)
        bodies = [c[2] for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual(bodies[0], {"symbol": "WMT", "qty": "9", "side": "buy", "type": "limit",
                                     "limit_price": "123.51", "time_in_force": "gtc", "order_class": "bracket",
                                     "take_profit": {"limit_price": "134.09"}, "stop_loss": {"stop_price": "118.22"},
                                     "client_order_id": "swing-WMT-20261109-breakout20"})
        self.assertEqual((bodies[1]["side"], bodies[1]["qty"], bodies[1]["stop_loss"]["stop_price"]), ("sell", "4", "154.74"))
        self.assertEqual(state["trades"]["WMT|2026-11-09|breakout20"]["predicted"]["p_win"], 0.55)
        # the same setups the next run: not again
        res = paper.submit(self.api, {"tickets": [TICKET]}, risk_doc([TICKET]), state, "2026-11-10")
        self.assertEqual(res, [("WMT", "ya operado")])
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "POST"]), 2)

    def test_skips_and_premarket(self):
        state = {}
        pricey = dict(TICKET, symbol="BKNG", qty=0.4, entry=5000, stop=4800, target=5400)
        bad = dict(TICKET, symbol="XOM", premarket={"status": "invalid"})
        warn = dict(TICKET, symbol="JNJ", premarket={"status": "warn", "qty": 2.7})
        blocked = dict(TICKET, symbol="KO")
        res = dict(paper.submit(self.api, {"tickets": [pricey, bad, warn, blocked]}, risk_doc([pricey, bad, warn]),
                                state, "2026-11-10"))
        self.assertIn("menos de 1 accion", res["BKNG"])
        self.assertIn("invalido", res["XOM"])
        self.assertEqual(res["JNJ"], "orden paper enviada")
        self.assertNotIn("KO", res)                                # not a PASS: never ordered
        self.assertEqual([c[2]["qty"] for c in self.fake.calls if c[0] == "POST"], ["2"])   # the pre-market size
        self.assertEqual(paper.submit(self.api, {"tickets": [TICKET]}, risk_doc([TICKET], "2026-11-09"), {},
                                      "2026-11-10"), [("*", "sin chequeo de risk de hoy: nada que operar")])

    def test_dry_run_places_nothing(self):
        res = paper.submit(None, {"tickets": [TICKET]}, risk_doc([TICKET]), {}, "2026-11-10", dry_run=True)
        self.assertIn("dry-run", res[0][1])
        self.assertEqual(self.fake.calls, [])


class Sync(Base):
    def placed(self, *tickets):
        state = {}
        paper.submit(self.api, {"tickets": list(tickets)}, risk_doc(list(tickets)), state, "2026-11-10")
        return state

    def test_fill_then_target_and_stop(self):
        state = self.placed(TICKET, SHORT)
        self.fake.orders["o1"].update(status="filled", filled_avg_price="124.00", filled_at="2026-11-10T14:30:01Z")
        self.fake.orders["o2"].update(status="filled", filled_avg_price="142.00", filled_at="2026-11-10T14:30:01Z")
        out = dict(paper.sync(self.api, state, "2026-11-10"))
        self.assertEqual(out, {"WMT": "entrada ejecutada", "USO": "entrada ejecutada"})
        self.fake.orders["o1"]["legs"][0].update(status="filled", filled_avg_price="134.09")
        self.fake.orders["o2"]["legs"][1].update(status="filled", filled_avg_price="155.20")   # gapped past the stop
        out = dict(paper.sync(self.api, state, "2026-11-12"))
        self.assertEqual(out, {"WMT": "cerrada en el objetivo", "USO": "cerrada en el stop"})
        w, u = state["trades"]["WMT|2026-11-09|breakout20"], state["trades"]["USO|2026-11-09|breakdown20"]
        self.assertEqual((w["outcome"], w["R"]), ("target", round((134.09 - 124.0) / 5.29, 3)))
        self.assertEqual((u["outcome"], u["R"]), ("stop", round((142.0 - 155.2) / 12.08, 3)))
        self.assertLess(u["R"], -1)

    def test_time_stop_closes_at_the_next_open(self):
        state = self.placed(TICKET)
        self.fake.orders["o1"].update(status="filled", filled_avg_price="123.00")
        paper.sync(self.api, state, "2026-11-10")
        self.assertEqual(paper.sync(self.api, state, "2026-11-18"), [])          # the time-stop day itself
        out = paper.sync(self.api, state, "2026-11-19")
        self.assertEqual(out, [("WMT", "stop de tiempo: cierre a mercado enviado")])
        cancels = [c[1] for c in self.fake.calls if c[0] == "DELETE" and "/orders/" in c[1]]
        self.assertEqual(cancels, ["/v2/orders/o1tp", "/v2/orders/o1sl"])
        self.assertIn(("DELETE", "/v2/positions/WMT", None), self.fake.calls)
        self.fake.orders["c2"].update(status="filled", filled_avg_price="125.65")
        paper.sync(self.api, state, "2026-11-20")
        tr = state["trades"]["WMT|2026-11-09|breakout20"]
        self.assertEqual((tr["status"], tr["outcome"], tr["R"]), ("closed", "time", round(2.65 / 5.29, 3)))

    def test_unfilled_entry(self):
        state = self.placed(TICKET)
        self.fake.orders["o1"]["status"] = "canceled"
        self.assertEqual(paper.sync(self.api, state, "2026-11-11"), [("WMT", "entrada no ejecutada (canceled)")])


class Report(Base):
    def test_summary(self):
        state = {"trades": {
            "a": {"symbol": "A", "status": "closed", "R": 2.0, "outcome": "target", "predicted": {"p_win": 0.6, "expectancy_R": 0.1}},
            "b": {"symbol": "B", "status": "closed", "R": -1.0, "outcome": "stop", "predicted": {"p_win": 0.5, "expectancy_R": 0.0}},
            "c": {"symbol": "C", "status": "open"}, "d": {"symbol": "D", "status": "skipped"}}}
        state["trades"]["a"].update(setup="earnings_gap", side="buy", entry_fill=100.0, exit_fill=110.0,
                                    entry_at="2026-11-02T14:31:00Z", exit_at="2026-11-04T18:00:00Z")
        state["trades"]["b"].update(setup="rsi2", side="buy", entry_fill=50.0, exit_fill=49.0,
                                    entry_at="2026-11-03T14:31:00Z", exit_at="2026-11-04T15:00:00Z")
        spy = {"2026-11-02": (600.0, 601.0), "2026-11-03": (606.0, 604.0), "2026-11-04": (610.0, 612.0)}
        text = paper.summary(state, "5012.34", spy)
        for must in ("Cerradas: <b>2</b> (1 objetivo, 1 stop, 0 por tiempo)", "Winrate real: <b>50%</b> · estudio 50%",
                     "R promedio real: <b>+0.50R</b> · estudio +0.39R", "Total: <b>+1.0R</b>",
                     # +10 % vs SPY 600 -> 612 (+2 %); -2 % vs SPY 606 -> 612 (+0.99 %)
                     "operaciones <b>+4.00%</b> · SPY <b>+1.50%</b> · diferencia <b>+2.50 pts</b> · le ganó en 1 de 2",
                     "Abiertas o pendientes: <b>1</b> de 6 (C)", "Sin operar: 1", "$5,012.34", "muestra chica",
                     "sin dinero real"):
            self.assertIn(must, text)
        self.assertIn("sin precios de SPY", paper.summary(state, None, {}))
        self.assertNotIn("S&amp;P", paper.summary(state, None, None))


class Limit(Base):
    def live(self, sym, setup, status="open", entry=100.0):
        return {"symbol": sym, "side": "buy", "qty": 1, "setup": setup, "status": status, "order_id": "x" + sym,
                "entry_fill": entry if status == "open" else None, "risk_per_share": 5.0,
                "signal_date": "2026-11-09"}

    def test_six_live_then_only_a_measured_edge_replaces_the_weakest(self):
        state = {"trades": {}}
        for i, sym in enumerate(("AA", "BB", "CC", "DD", "EE")):
            state["trades"][sym] = self.live(sym, "breakout20")
        state["trades"]["FF"] = self.live("FF", "earnings_gap")
        self.fake.orders["xCC"] = {"id": "xCC", "status": "filled", "legs": [
            {"id": "xCCtp", "type": "limit", "status": "held"}, {"id": "xCCsl", "type": "stop", "status": "held"}]}
        prices = {"AA": 104, "BB": 101, "CC": 93, "DD": 99, "EE": 102, "FF": 80}   # CC: -1.4R, FF worse but earnings_gap
        fake = self.fake

        def positions():
            return [{"symbol": k, "current_price": str(v)} for k, v in prices.items()]
        self.api.positions = positions
        tech = dict(TICKET, symbol="GG", setup="pullback_ema20", signal_date="2026-11-10")
        res = paper.submit(self.api, {"tickets": [tech]}, risk_doc([tech]), state, "2026-11-10")
        self.assertEqual(res, [("GG", "omitido: limite de 6 operaciones vivas")])
        gap = dict(TICKET, symbol="HH", setup="earnings_gap", signal_date="2026-11-10", target=200.0)
        res = paper.submit(self.api, {"tickets": [gap]}, risk_doc([gap]), state, "2026-11-10")
        self.assertEqual(res, [("CC", "reemplazada por HH"), ("HH", "orden paper enviada")])
        self.assertEqual((state["trades"]["CC"]["status"], state["trades"]["CC"]["close_reason"]), ("closing", "replaced"))
        self.assertIn(("DELETE", "/v2/positions/CC", None), fake.calls)
        self.assertIn(("DELETE", "/v2/orders/xCCtp", None), fake.calls)
        # the close fills: recorded as replaced
        cid = state["trades"]["CC"]["close_order_id"]
        fake.orders[cid].update(status="filled", filled_avg_price="93.0", filled_at="2026-11-10T15:00:00Z")
        fake.orders.setdefault("xAA", {"id": "xAA", "status": "filled", "legs": []})
        for s_ in ("AA", "BB", "DD", "EE", "FF"):
            fake.orders.setdefault("x" + s_, {"id": "x" + s_, "status": "filled", "legs": []})
        fake.orders["o1"] = dict(fake.orders.get("o1") or {}, status="new")
        paper.sync(self.api, state, "2026-11-10")
        self.assertEqual((state["trades"]["CC"]["status"], state["trades"]["CC"]["outcome"], state["trades"]["CC"]["R"]),
                         ("closed", "replaced", -1.4))

    def test_the_worst_open_R_goes_before_an_unfilled_entry(self):
        state = {"trades": {s: self.live(s, "rsi2") for s in ("AA", "BB", "CC", "DD", "EE")}}
        state["trades"]["PP"] = self.live("PP", "rsi2", status="submitted")
        state["trades"]["AA"]["entry_fill"] = 110.0      # AA open at -2R: but PP (not filled, 0R) ...
        self.api.positions = lambda: [{"symbol": "AA", "current_price": "100"}]
        self.fake.orders["xAA"] = {"id": "xAA", "status": "filled", "legs": []}
        gap = dict(TICKET, symbol="HH", setup="earnings_gap", signal_date="2026-11-10", target=200.0)
        res = paper.submit(self.api, {"tickets": [gap]}, risk_doc([gap]), state, "2026-11-10")
        self.assertEqual(res[0], ("AA", "reemplazada por HH"))      # ... the worst open R goes first
        os.environ["PAPER_MAX_POSITIONS"] = "8"
        try:
            self.assertEqual(paper.max_positions(), 8)
        finally:
            os.environ.pop("PAPER_MAX_POSITIONS")


class Cli(Base):
    def setUp(self):
        Base.setUp(self)
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.makedirs("trader/state")
        with open("trader/swing_orders.json", "w") as fh:
            json.dump({"tickets": [TICKET]}, fh)
        with open("trader/state/swing_risk.json", "w") as fh:
            json.dump(risk_doc([TICKET]), fh)
        self.saved = paper._http
        paper._http = self.fake

    def tearDown(self):
        paper._http = self.saved
        os.chdir(self.cwd)
        shutil.rmtree(self.d)
        Base.tearDown(self)

    def run_(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = paper.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_needs_paper_trading_1_to_place(self):
        code, text, _ = self.run_("submit", "--now", "2026-11-10 08:55")
        self.assertIn("PAPER_TRADING no es 1", text)
        self.assertEqual(self.fake.calls, [])
        os.environ["PAPER_TRADING"] = "true"
        code, text, _ = self.run_("submit", "--now", "2026-11-10 08:55", "--quiet")
        self.assertEqual((code, text.strip()), (0, "paper submit: 1 enviada(s)."))
        self.assertNotIn("WMT", text)
        with open("trader/state/paper.json") as fh:
            self.assertIn("WMT|2026-11-09|breakout20", json.load(fh)["trades"])
        code, text, _ = self.run_("account", "--quiet")
        self.assertEqual(text.strip(), "Cuenta paper de Alpaca: estado ACTIVE.")      # no balance in public logs
        os.environ.pop("ALPACA_API_KEY_ID")
        code, _, err = self.run_("sync")
        self.assertEqual(code, 2)
        self.assertIn("ALPACA_API_KEY_ID", err)


if __name__ == "__main__":
    unittest.main()


class LimitAndDaily(Base):
    def test_limit_at_the_pre_market_price_and_unfilled_entries_are_cancelled(self):
        state = {}
        t = dict(TICKET, premarket={"status": "ok", "price": 124.30, "qty": 8.1})
        paper.submit(self.api, {"tickets": [t]}, risk_doc([t]), state, "2026-11-10",
                     now=paper.parse_now("2026-11-10 09:00"))
        body = [c[2] for c in self.fake.calls if c[0] == "POST"][0]
        self.assertEqual((body["type"], body["limit_price"], body["qty"]), ("limit", "124.30", "8"))
        self.assertEqual(paper.sync(self.api, state, "2026-11-10"), [])                    # still waiting, same day
        out = paper.sync(self.api, state, "2026-11-10", after_close=True)
        self.assertEqual(out, [("WMT", "entrada no ejecutada (limite no alcanzado, cancelada)")])
        self.assertIn(("DELETE", "/v2/orders/o1", None), self.fake.calls)
        self.assertEqual(state["trades"]["WMT|2026-11-09|breakout20"]["status"], "not_filled")

    def test_evening_window(self):
        p = paper.parse_now
        self.assertEqual(paper.evening_check(p("2026-11-10 16:20"), {})[0], True)
        self.assertEqual(paper.evening_check(p("2026-11-10 15:50"), {})[0], False)
        self.assertEqual(paper.evening_check(p("2026-11-10 16:20"), {"last_daily": "2026-11-10"})[0], False)
        self.assertEqual(paper.evening_check(p("2026-11-14 17:00"), {})[0], False)                # Saturday
        self.assertEqual(paper.evening_check(p("2026-11-14 17:00"), {}, force=True)[0], True)
        self.assertEqual(paper.sessions_left("2026-11-10", "2026-11-18"), 6)

    def test_daily_message(self):
        state = {}
        paper.submit(self.api, {"tickets": [TICKET, SHORT]}, risk_doc([TICKET, SHORT]), state, "2026-11-10",
                     now=paper.parse_now("2026-11-10 09:00"))
        self.fake.orders["o1"].update(status="filled", filled_avg_price="123.40", filled_at="2026-11-10T14:31:00Z")
        self.fake.orders["o2"].update(status="filled", filled_avg_price="142.60", filled_at="2026-11-10T14:31:00Z")
        self.fake.orders["o2"]["legs"][0].update(status="filled", filled_avg_price="118.50",
                                                 filled_at="2026-11-10T20:00:00Z")
        positions = [{"symbol": "WMT", "current_price": "125.10", "unrealized_pl": "15.30"}]
        saved = self.fake.__call__

        def call(method, url, body=None):
            if url.endswith("/v2/positions") and method == "GET":
                return positions
            return saved(method, url, body)
        api = paper.Paper(call)
        text, events = paper.daily(api, state, paper.parse_now("2026-11-10 16:30"))
        self.assertEqual(dict(events), {"WMT": "entrada ejecutada", "USO": "cerrada en el objetivo"})
        for must in ("🌙 <b>Cierre 10-11</b>", "<b>Abiertas (1)</b>",
                     "🟢 <b>WMT</b> LONG · 9 acc · entrada $123.40 → <b>$125.10</b> (+1.4%) · <b>+0.32R</b> · +$15.30",
                     "Stop $118.22 (a 5.5%) · Objetivo $134.09 (a 7.2%) · salida por tiempo 18-11 (6 días hábiles)",
                     "<b>Cerradas hoy (1)</b>", "<b>USO</b> · ✅ objetivo · <b>+2.00R</b> · +$96.40",
                     "Resultado abierto: <b>+$15.30</b>", "Patrimonio de la cuenta paper: <b>$5,012.34</b>"):
            self.assertIn(must, text)
        self.assertEqual(state["last_daily"], "2026-11-10")
        self.assertEqual(paper.daily(api, state, paper.parse_now("2026-11-10 17:00"))[0], None)   # once a day


class EveningWorkflow(unittest.TestCase):
    def test_paper_daily_workflow(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".github", "workflows", "paper-daily.yml")) as fh:
            wf = fh.read()
        for must in ("paper_trader.py daily", "--quiet", "vars.PAPER_TRADING", "secrets.TELEGRAM_BOT_TOKEN",
                     "contents: read", "actions/cache/save", 'cron: "17 21 * * 1-5"'):
            self.assertIn(must, wf)
        for bad in ("upload-artifact", "git push", "trader-broker"):
            self.assertNotIn(bad, wf)
        self.assertIsNone(re.search(r"(?<!paper-)api\.alpaca\.markets", wf))      # never the live trading API


class LeftoverOrders(unittest.TestCase):
    """Open orders with no position behind them (the stop or target of a trade that already closed)."""

    def order(self, sym, side, typ, status="new", cid="", **kw):
        return dict({"id": "%s-%s-%s" % (sym, side, typ), "symbol": sym, "side": side, "type": typ,
                     "status": status, "client_order_id": cid}, **kw)

    def test_what_is_a_leftover(self):
        L = paper.leftover_orders
        pos = [{"symbol": "KO", "qty": "10"}, {"symbol": "QQQ", "qty": "-20"}]
        # a stop that still protects a long, and the buy stop of a short: fine
        self.assertEqual(L([self.order("KO", "sell", "stop")], pos), [])
        self.assertEqual(L([self.order("QQQ", "buy", "stop", cid="orb-QQQ-20261118-run")], pos), [])
        # a pending bracket entry with its held legs: not a leftover (sync cancels stale entries)
        entry = self.order("AAPL", "buy", "limit", cid="swing-AAPL-20261117-x", order_class="bracket",
                           legs=[self.order("AAPL", "sell", "limit", "held"), self.order("AAPL", "sell", "stop", "held")])
        self.assertEqual(L([entry], pos), [])
        # the legs of a filled bracket whose position is gone: leftovers, safe to cancel
        done = dict(entry, status="filled", legs=[self.order("AAPL", "sell", "stop", "new", id="leg1")])
        (o, why, sure), = L([done], pos)
        self.assertEqual((o["id"], sure), ("leg1", True))
        # any sell with no shares would open a short: safe to cancel
        (o, why, sure), = L([self.order("CMG", "sell", "limit")], pos)
        self.assertTrue(sure)
        # a box exit (buy to cover) with no short: safe; an unknown standalone buy: reported only
        self.assertTrue(L([self.order("SPY", "buy", "stop", cid="orb-SPY-20261118-run-be")], pos)[0][2])
        self.assertFalse(L([self.order("MRK", "buy", "limit", cid="manual-1")], pos)[0][2])

    def test_check_cancels_only_the_sure_ones_and_logs_no_sizes(self):
        calls = []

        class Api(object):
            def call(self, method, path, body=None):
                return [{"id": "a", "symbol": "CMG", "side": "sell", "type": "limit", "status": "new",
                         "client_order_id": "x", "qty": "37", "limit_price": "61.35"},
                        {"id": "b", "symbol": "MRK", "side": "buy", "type": "limit", "status": "new",
                         "client_order_id": "manual", "qty": "5", "limit_price": "99"}]

            def positions(self):
                return []

            def cancel(self, oid):
                calls.append(oid)
        lines, c = paper.orders_check(Api(), cancel=True)
        self.assertEqual(calls, ["a"])
        self.assertEqual((c["leftover"], c["sure"], c["cancelled"]), (2, 1, 1))
        text = " ".join(lines)
        self.assertNotIn("61.35", text)
        self.assertNotIn("37", text)
        lines, c = paper.orders_check(Api(), cancel=False)
        self.assertEqual((calls, c["cancelled"]), (["a"], 0))


class Unprotected(unittest.TestCase):
    def test_positions_without_a_full_stop(self):
        pos = [{"symbol": "KO", "qty": "10"}, {"symbol": "QQQ", "qty": "-20"}, {"symbol": "SPY", "qty": "30"}]
        orders = [
            {"symbol": "KO", "side": "buy", "type": "limit", "status": "filled", "order_class": "bracket",
             "legs": [{"symbol": "KO", "side": "sell", "type": "stop", "status": "new", "qty": "10"},
                      {"symbol": "KO", "side": "sell", "type": "limit", "status": "new", "qty": "10"}]},
            {"symbol": "QQQ", "side": "buy", "type": "stop", "status": "new", "qty": "6"},       # the runner only
            {"symbol": "SPY", "side": "sell", "type": "limit", "status": "new", "qty": "30"},    # a target, no stop
        ]
        got = dict(paper.unprotected(orders, pos))
        self.assertNotIn("KO", got)
        self.assertAlmostEqual(got["QQQ"], 0.3)
        self.assertEqual(got["SPY"], 0.0)

    def test_an_oco_exit_is_not_an_entry(self):
        oco = {"symbol": "SPY", "side": "sell", "type": "limit", "status": "new", "order_class": "oco",
               "client_order_id": "orb-SPY-20261118-tp", "legs": [{"symbol": "SPY", "side": "sell", "type": "stop", "status": "new"}]}
        self.assertEqual(len(paper.leftover_orders([oco], [])), 2)                 # both sides, no shares behind
        self.assertEqual(paper.leftover_orders([oco], [{"symbol": "SPY", "qty": "10"}]), [])
