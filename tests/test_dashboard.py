"""trader-alerts/dashboard_data.py: the encryption the site undoes in the browser, the trades rebuilt from the
paper orders, the public market data, and that it only reads (no orders) and never publishes the account in
clear."""

import datetime
import json
import os
import re
import tempfile
import unittest

from _load import load, ROOT

dash = load("trader-alerts", "dashboard_data")
DAY = datetime.date(2026, 11, 17)


def bars(moves, start="08:30", end="12:00", base=100.0):
    out, t = [], datetime.datetime.combine(DAY, datetime.time(*map(int, start.split(":"))))
    stop = datetime.datetime.combine(DAY, datetime.time(*map(int, end.split(":"))))
    while t <= stop:
        o, h, l, c = moves.get(t.strftime("%H:%M"), (base, base + 0.1, base - 0.1, base))
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
        t += datetime.timedelta(minutes=5)
    return out


class Crypto(unittest.TestCase):
    def test_round_trip_and_wrong_passphrase(self):
        doc = {"account": {"equity": 100412.5}, "trades": [{"symbol": "QQQ", "R": 1.19}], "texto": "ñandú"}
        enc = dash.encrypt(doc, "una frase larga de prueba", rounds=1000)
        self.assertEqual(dash.decrypt(enc, "una frase larga de prueba"), doc)
        with self.assertRaises(ValueError):
            dash.decrypt(enc, "otra frase")
        self.assertNotIn("100412", json.dumps(enc))
        self.assertNotIn("QQQ", json.dumps(enc))

    def test_fresh_salt_and_nonce(self):
        a, b = dash.encrypt({"x": 1}, "frase de prueba 123", 1000), dash.encrypt({"x": 1}, "frase de prueba 123", 1000)
        self.assertNotEqual((a["salt"], a["nonce"], a["ct"]), (b["salt"], b["nonce"], b["ct"]))

    def test_known_keystream(self):
        # the site's decryptDoc computes HMAC-SHA256(k1, nonce || uint32 big-endian i): pin that layout
        import hashlib
        import hmac
        k, n = b"k" * 32, b"n" * 16
        self.assertEqual(dash._keystream(k, n, 40)[:32], hmac.new(k, n + b"\x00\x00\x00\x00", hashlib.sha256).digest())
        self.assertEqual(dash._keystream(k, n, 40)[32:], hmac.new(k, n + b"\x00\x00\x00\x01", hashlib.sha256).digest()[:8])


class Public(unittest.TestCase):
    def test_today(self):
        b = bars({"09:00": (100, 101, 99, 100), "10:10": (100, 102.5, 100, 102)})
        now = dash.scan.eastern_to_utc(datetime.datetime.combine(DAY, datetime.time(10, 20)))
        doc = dash.today_doc({"QQQ": b}, now)
        q = doc["symbols"]["QQQ"]
        self.assertEqual(doc["session"], "2026-11-17")
        self.assertEqual((q["box"]["high"], q["box"]["low"]), (101, 99))
        self.assertEqual((q["signal"]["dir"], q["signal"]["close"], q["signal"]["stop"]), (1, 102, 99))
        self.assertEqual(q["signal"]["partial"], round(102 + 1.7 * 3, 2))
        self.assertTrue(doc["market_open"])

    def test_paths_are_relative_moves(self):
        b = bars({"09:00": (100, 101, 99, 100.5)})
        p = dash.paths_doc({"QQQ": b})["symbols"]["QQQ"][0]
        self.assertEqual(p["day"], "2026-11-17")
        i = [k for k, x in enumerate(b) if x["t"].strftime("%H:%M") == "09:00"][0]
        self.assertEqual(p["bars"][i - 1], [1000, -1000, 500])      # vs the previous close of 100


class Trades(unittest.TestCase):
    def test_rebuilt_from_orders(self):
        orders = [
            {"client_order_id": "orb-SPY-20261112-in", "status": "filled", "side": "buy", "filled_avg_price": "740.10",
             "filled_qty": "60", "qty": "60", "type": "market", "filled_at": "2026-11-12T15:40:02Z"},
            {"client_order_id": "orb-SPY-20261112-tp", "status": "filled", "side": "sell", "filled_avg_price": "745.03",
             "filled_qty": "42", "type": "limit", "filled_at": "2026-11-12T18:00:00Z",
             "legs": [{"status": "canceled", "type": "stop", "stop_price": "737.20", "side": "sell"}]},
            {"client_order_id": "orb-SPY-20261112-run", "status": "replaced", "side": "sell", "type": "stop", "stop_price": "737.20"},
            {"client_order_id": "orb-SPY-20261112-run-be", "status": "filled", "side": "sell", "type": "stop",
             "stop_price": "740.10", "filled_avg_price": "740.10", "filled_qty": "18", "filled_at": "2026-11-13T15:00:00Z"},
            {"client_order_id": "something-else", "status": "filled"},
        ]
        t, = dash.trades_from_orders(orders)
        self.assertEqual((t["symbol"], t["day"], t["side"], t["entry"], t["stop"]), ("SPY", "2026-11-12", "largo", 740.10, 737.20))
        self.assertTrue(t["partial_done"])
        self.assertFalse(t["open"])
        # 42 x 4.93 + 18 x 0 over 60 x 2.90
        self.assertAlmostEqual(t["realized_R"], round(42 * 4.93 / (60 * 2.9), 3), places=3)


class Safety(unittest.TestCase):
    def read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_read_only(self):
        code = re.sub(r'"""[\s\S]*?"""', "", self.read("trader-alerts/dashboard_data.py"))
        for bad in ('"POST"', '"PATCH"', '"DELETE"', ".submit(", ".replace(o", ".cancel(", ".close(", "trader-broker"):
            self.assertNotIn(bad, code, bad)
        self.assertEqual(re.findall(r"https?://", code), [])

    def test_no_account_in_clear(self):
        code = self.read("trader-alerts/dashboard_data.py")
        self.assertIn("encrypt(private_doc(", code)
        self.assertIn("site/data/", self.read(".gitignore").split())

    def test_workflow(self):
        wf = self.read(".github/workflows/dashboard.yml")
        for must in ("secrets.DASHBOARD_KEY", "pages: write", "id-token: write", "actions/deploy-pages",
                     "dashboard_data.py --out site/data", "contents: read"):
            self.assertIn(must, wf)
        for bad in ("git push", "git commit", "orb_paper.py run", "actions/cache/save"):
            self.assertNotIn(bad, wf)

    def test_live_link_is_paper_only_and_keys_stay_local(self):
        js = self.read("site/app.js")
        for must in ("wss://stream.data.alpaca.markets/v2/iex", "wss://paper-api.alpaca.markets/stream",
                     "https://paper-api.alpaca.markets", "/^PK/", "alpaca-paper-keys"):
            self.assertIn(must, js)
        for bad in ('method: "POST"', 'method: "DELETE"', 'method: "PATCH"', "https://api.alpaca.markets"):
            self.assertNotIn(bad, js)                     # the page only reads, and never the live (real) account
        hosts = set(re.findall(r"(?:https|wss)://([a-z0-9.-]+)", js))
        self.assertEqual(hosts, {"data.alpaca.markets", "stream.data.alpaca.markets", "paper-api.alpaca.markets"})
        html = self.read("site/index.html")
        self.assertIn('integrity="sha384-', html)        # the chart library can't be swapped under the keys

    def test_site_decrypts_the_same_layout(self):
        js = self.read("site/app.js")
        for must in ('"PBKDF2"', '"SHA-256"', "setUint32(nonce.length, i)", "doc.rounds", "Frase incorrecta"):
            self.assertIn(must, js)


if __name__ == "__main__":
    unittest.main()
