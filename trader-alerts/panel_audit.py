#!/usr/bin/env python3
"""Audit of the dashboard (GitHub Pages) against the paper account. Standard library only. READ ONLY.

    python3 trader-alerts/panel_audit.py            # in Actions: ALPACA keys, DASHBOARD_KEY, GITHUB_REPOSITORY

Three views of the same account, compared:
  Alpaca      the positions and orders right now (the truth)
  fresh       what the panel's data builder (dashboard_data.py) makes from it now
  published   what the published page holds (data/private*.json, decrypted with DASHBOARD_KEY) and how old it is

The CI log is public: it prints symbols, statuses, times and R, never prices, sizes or dollars.
It never places, changes or cancels an order.
"""

import datetime
import importlib.util
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dash = _load("audit_dashboard_data", "trader-alerts/dashboard_data.py")
orb, paper, scan = dash.orb, dash.paper, dash.scan

OPEN = ("new", "accepted", "held", "pending_new", "partially_filled")


def pages_url(repo):
    owner, name = repo.split("/", 1)
    return "https://%s.github.io/%s/data/" % (owner.lower(), name)


def published(base, name, key, get=None):
    """The published file, decrypted. -> doc or None."""
    get = get or (lambda u: urllib.request.urlopen(urllib.request.Request(u, headers={"Cache-Control": "no-cache"}),
                                                   timeout=20).read())
    try:
        raw = json.loads(get(base + name + "?t=%d" % int(time.time())))
        return dash.decrypt(raw, key)
    except Exception as e:                          # noqa: BLE001 (missing file, network, wrong key)
        print("  no se pudo leer %s publicado: %s" % (name, type(e).__name__))
        return None


def age(doc, now_utc):
    try:
        t = datetime.datetime.strptime(doc["updated"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        return "?"
    return "%s NY (hace %d min)" % (scan.to_eastern(t).strftime("%Y-%m-%d %H:%M"), (now_utc - t).total_seconds() // 60)


def box_audit(positions, fresh, pub):
    """-> lines and the number of mismatches."""
    syms = set(orb.symbols())
    real = {s for s in positions if s in syms}
    f_open = {t["symbol"] for t in fresh["trades"] if t.get("open")}
    p_open = {t["symbol"] for t in (pub or {}).get("trades", []) if t.get("open")} if pub else None
    lines, bad = [], 0
    for s in sorted(real | f_open | (p_open or set())):
        a, b, c = s in real, s in f_open, (s in p_open) if p_open is not None else None
        ok = a == b and (c is None or a == c)
        bad += 0 if ok else 1
        lines.append("  %s %s: Alpaca %s | datos nuevos %s | publicado %s" % (
            "OK " if ok else "!! ", s, "abierta" if a else "cerrada", "abierta" if b else "cerrada",
            "-" if c is None else ("abierta" if c else "cerrada")))
    f_closed = {t["id"] for t in fresh["trades"] if not t.get("open")}
    p_closed = {t["id"] for t in (pub or {}).get("trades", []) if not t.get("open")} if pub else set()
    missing = sorted(f_closed - p_closed) if pub else []
    if missing:
        bad += len(missing)
        lines.append("  !! cerradas que el panel publicado todavia no muestra: %s" % ", ".join(missing))
    lines.append("  operaciones cerradas: %d en Alpaca, %s publicadas" % (len(f_closed), len(p_closed) if pub else "-"))
    return lines, bad


def swing_audit(positions, orders, state, fresh, pub):
    syms = set(orb.symbols())
    real = {s for s in positions if s not in syms}
    pending = {str(o.get("symbol") or "").upper() for o in orders
               if (o.get("client_order_id") or "").startswith("swing-") and o.get("status") in OPEN
               and o.get("order_class") in ("bracket", "oto") and str(o.get("symbol") or "").upper() not in positions}
    st = {}
    for t in (state.get("trades") or {}).values():
        if t.get("status") in ("submitted", "open", "closing"):
            st[str(t.get("symbol") or "").upper()] = t["status"]
    f = {str(t.get("symbol") or "").upper(): t.get("status") for t in fresh["trades"]
         if t.get("status") in ("submitted", "open", "closing")}
    p = ({str(t.get("symbol") or "").upper(): t.get("status") for t in pub.get("trades", [])
          if t.get("status") in ("submitted", "open", "closing")} if pub else None)
    lines, bad = [], 0
    for s in sorted(real | pending | set(st) | set(f) | set(p or {})):
        truth = "abierta" if s in real else "orden de entrada esperando" if s in pending else "cerrada"
        want = {"abierta": "open", "orden de entrada esperando": "submitted", "cerrada": None}[truth]
        okf = f.get(s) == want or (want == "open" and f.get(s) == "closing")
        okp = p is None or p.get(s) == want or (want == "open" and p.get(s) == "closing")
        bad += 0 if (okf and okp) else 1
        lines.append("  %s %s: Alpaca %s | estado del bot %s | datos nuevos %s | publicado %s" % (
            "OK " if okf and okp else "!! ", s, truth, st.get(s, "cerrada/ninguna"), f.get(s, "cerrada"),
            "-" if p is None else p.get(s, "cerrada")))
        if s in real and s not in st:
            lines.append("      (el bot del swing aun no lo sincroniza: lo hace antes de abrir y despues del cierre)")
    return lines, bad


def main(argv=None):
    now_utc = paper.utc_now()
    key = os.environ.get("DASHBOARD_KEY") or ""
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    acct = orb.Account(paper.Paper())
    positions = acct.positions()
    orders = acct.orders("all", None, 500)
    orb_state = paper.read_json(orb.STATE, {}) or {}
    swing_state = paper.read_json(paper.STATE, {}) or {}
    fresh_box = dash.private_doc(acct, now_utc, orb_state)
    fresh_swing = dash.swing_doc(acct.api, now_utc, swing_state)
    pub_box = pub_swing = None
    if len(key) >= 12 and repo:
        base = pages_url(repo)
        pub_box = published(base, "private.json", key)
        pub_swing = published(base, "private_swing.json", key)
    else:
        print("Sin DASHBOARD_KEY o GITHUB_REPOSITORY: solo Alpaca contra los datos nuevos.")
    print("Auditoria del panel, %s NY" % scan.to_eastern(now_utc).strftime("%Y-%m-%d %H:%M"))
    print("Panel publicado: caja leida %s; swing leido %s" % (age(pub_box, now_utc) if pub_box else "-",
                                                           age(pub_swing, now_utc) if pub_swing else "-"))
    lb, bb = box_audit(positions, fresh_box, pub_box)
    print("\nCaja de las 09:00:")
    print("\n".join(lb))
    ls, bs = swing_audit(positions, orders, swing_state, fresh_swing, pub_swing)
    print("\nSwing:")
    print("\n".join(ls) or "  sin operaciones")
    other = sorted(s for s in positions if s not in set(orb.symbols()) and not any(
        str(t.get("symbol") or "").upper() == s for t in (swing_state.get("trades") or {}).values()))
    if other:
        print("\nPosiciones que no son de ninguna estrategia: %s" % ", ".join(other))
    print("\nDiferencias: %d (caja %d, swing %d)" % (bb + bs, bb, bs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
