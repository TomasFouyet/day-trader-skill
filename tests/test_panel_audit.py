"""trader-alerts/panel_audit.py: the panel compared with the paper account, without prices or sizes."""

import os
import unittest

from _load import load

audit = load("trader-alerts", "panel_audit")


class Audit(unittest.TestCase):
    def setUp(self):
        os.environ["ORB_SYMBOLS"] = "QQQ,DIA"

    def tearDown(self):
        os.environ.pop("ORB_SYMBOLS", None)

    def test_box_flags_a_stale_publication(self):
        fresh = {"trades": [{"id": "orb-DIA-20261123", "symbol": "DIA", "open": False},
                            {"id": "orb-QQQ-20261125", "symbol": "QQQ", "open": True}]}
        pub = {"trades": [{"id": "orb-DIA-20261123", "symbol": "DIA", "open": True}]}
        lines, bad = audit.box_audit({"QQQ": {}}, fresh, pub)
        text = "\n".join(lines)
        self.assertIn("!!  DIA: Alpaca cerrada | datos nuevos cerrada | publicado abierta", text)
        self.assertIn("!!  QQQ: Alpaca abierta | datos nuevos abierta | publicado cerrada", text)
        self.assertEqual(bad, 3)                      # DIA, QQQ and the closed DIA missing from the page

    def test_swing_flags_what_the_state_does_not_know(self):
        state = {"trades": {"z": {"symbol": "ZS", "status": "submitted"}, "h": {"symbol": "HD", "status": "open"}}}
        fresh = {"trades": [{"symbol": "ZS", "status": "open"}, {"symbol": "HD", "status": "closed"}]}
        lines, bad = audit.swing_audit({"ZS": {}}, [], state, fresh, None)
        text = "\n".join(lines)
        self.assertIn("OK  ZS: Alpaca abierta | estado del bot submitted | datos nuevos open", text)
        self.assertIn("OK  HD: Alpaca cerrada | estado del bot open | datos nuevos cerrada", text)
        self.assertEqual(bad, 0)

    def test_pages_url(self):
        self.assertEqual(audit.pages_url("TomasFouyet/day-trader-skill"),
                         "https://tomasfouyet.github.io/day-trader-skill/data/")
