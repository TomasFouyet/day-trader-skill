"""trader-alerts/paper_trader.py against a fake Alpaca PAPER API: bracket orders for the setups that
passed today's risk check, fills read back, time stops closed, the track record."""

import io
import json
import os
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
        self.assertEqual(bodies[0], {"symbol": "WMT", "qty": "9", "side": "buy", "type": "market",
                                     "time_in_force": "gtc", "order_class": "bracket",
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
        text = paper.summary(state, "5012.34")
        for must in ("Cerradas: <b>2</b> (1 objetivo, 1 stop, 0 por tiempo)", "Winrate real: <b>50%</b> · estimado 55%",
                     "R promedio real: <b>+0.50R</b> · expectativa estimada +0.05R", "Total: <b>+1.0R</b>",
                     "Abiertas o pendientes: <b>1</b> (C)", "Sin operar: 1", "$5,012.34", "muestra chica",
                     "sin dinero real"):
            self.assertIn(must, text)


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
        os.environ["PAPER_TRADING"] = "1"
        code, text, _ = self.run_("submit", "--now", "2026-11-10 08:55", "--quiet")
        self.assertEqual((code, text.strip()), (0, "paper submit: 1 accion(es)."))
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
