#!/usr/bin/env python3
"""Telegram notice for swing setups that passed trader-risk. Standard library only.

    python3 notify_telegram.py --dry-run         # print the messages, send nothing, remember nothing
    python3 notify_telegram.py                   # send one message per new setup that passed risk
    python3 notify_telegram.py --test            # one test message, to check the bot and the chat

Reads trader/state/swing_risk.json (trader-risk check.py --swing) and the
tickets in trader/swing_orders.json (trader-budget). One message per ticket
with a PASS from TODAY's risk check whose ticket is still exactly the one that
was checked (same fingerprint): symbol, entry, stop, target, suggested size,
time stop, the Time Machine's odds and verdict, the real history count and
the earnings warnings. A setup is sent once: the symbol, signal day and setup
are remembered in trader/state/telegram.json and never sent again.

Credentials come ONLY from the environment:
  TELEGRAM_BOT_TOKEN   the token @BotFather gives you
  TELEGRAM_CHAT_ID     your chat id (send /start to the bot, then see README)
They are never printed, written to a file or put in a message.

It sends messages. It never buys, sells or talks to a broker: the tickets are
for you to read, then buy by hand if you want. Not financial advice.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot%s/sendMessage"
MAX_LEN = 4000
KEEP_DAYS = 60
UA = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"
FOOT = "Simulacion, no prediccion. No es asesoria financiera. No se compro nada: es un aviso para decidir tu."


class NotifyError(Exception):
    pass


# ---------------------------------------------------------------- New York time (no tz files)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
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
    if t.tzinfo is None:   # New York wall clock
        for off in (-4, -5):
            utc = t - datetime.timedelta(hours=off)
            if eastern_offset(utc) == off:
                return utc
        return t + datetime.timedelta(hours=5)
    return (t - t.utcoffset()).replace(tzinfo=None)


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise NotifyError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise NotifyError("%s is not valid JSON: %s" % (path, e))


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)


def swing_fingerprint(t):
    """The same function as trader-risk check.py's swing_fingerprint: the exact ticket that was checked."""
    core = {k: t.get(k) for k in ("symbol", "side", "qty", "type", "entry", "limit_price", "stop", "target",
                                   "signal_date", "time_stop_date") if t.get(k) is not None}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def key_of(t):
    return "%s|%s|%s" % (str(t.get("symbol") or "").upper(), t.get("signal_date"), t.get("setup") or "")


# ---------------------------------------------------------------- the messages

def _m(x):
    return "${:,.2f}".format(float(x))


def _q(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0").rstrip(".")


def _pc(x):
    return "%.1f %%" % (100.0 * float(x))


def message(t, result):
    lines = ["\U0001F7E2 Setup swing: %s (%s)" % (t["symbol"], t.get("setup") or "manual"),
             "Senal del %s. Para comprar a mano en tu app." % t.get("signal_date"),
             "Entrada ~%s | Stop %s | Objetivo %s (%sR)" % (
                 _m(t["entry"]), _m(t["stop"]), _m(t["target"]), ("%.2f" % float(t.get("reward_risk") or 0)).rstrip("0").rstrip(".")),
             "Tamano sugerido: %s acciones (~%s). Perdida maxima en el stop: %s." % (
                 _q(t["qty"]), _m(t.get("cost") or float(t["qty"]) * float(t["entry"])), _m(t.get("max_loss") or 0)),
             "Stop de tiempo: vende al cierre del %s si sigue abierta (%s dias habiles)." % (
                 t.get("time_stop_date"), t.get("time_stop_days") or "?")]
    m = t.get("timemachine") or {}
    if m.get("p_target") is not None:
        lines.append("Time Machine (%s trayectorias): objetivo primero %s, stop primero %s, sin resolver %s, "
                     "expectativa %+.2fR." % ("{:,}".format(int(m.get("paths") or 0)).replace(",", "."),
                                              _pc(m["p_target"]), _pc(m["p_stop"]), _pc(m["p_unresolved"]),
                                              float(m.get("expectancy_R") or 0)))
        if m.get("verdict"):
            lines.append("Veredicto: " + m["verdict"])
        if m.get("history_cases") is not None:
            lines.append("Historial real: %d caso(s)%s." % (
                m["history_cases"], ", pocos (menos de 30): tomalo con pinzas" if m.get("few_cases") else ""))
    else:
        lines.append("Time Machine: sin simulacion para este ticket.")
    er = t.get("earnings") or {}
    if er.get("flag"):
        lines.append("⚠ %s (%s)." % (er["flag"], er.get("next") or "fecha desconocida"))
    elif er.get("next"):
        lines.append("Resultados: proximos %s (%s), fuera del horizonte." % (
            er["next"], "fecha conocida" if er.get("known") else "estimada"))
    warns = list((result or {}).get("warnings") or [])
    for w in warns[:6]:
        lines.append("~ " + w)
    lines.append(FOOT)
    text = "\n".join(lines)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 3] + "..."


def pending(risk_doc, tickets_doc, state, today, any_date=False):
    """-> ([(key, ticket, result)], skipped [(symbol, why)]). Pure."""
    skipped = []
    if not isinstance(risk_doc, dict) or risk_doc.get("mode") != "swing":
        raise NotifyError("the risk file is not a swing check (run trader-risk check.py --swing)")
    if risk_doc.get("date") != today and not any_date:
        return [], [("*", "the risk check is from %s, not today (%s): nothing sent" % (risk_doc.get("date"), today))]
    tickets = (tickets_doc or {}).get("tickets") or []
    by_fp = {swing_fingerprint(t): t for t in tickets}
    sent = (state or {}).get("sent") or {}
    out = []
    for r in risk_doc.get("results") or []:
        if r.get("verdict") != "PASS":
            continue
        t = by_fp.get(r.get("fingerprint"))
        if t is None:
            skipped.append((r.get("symbol"), "the ticket changed after the risk check: run check.py --swing again"))
            continue
        k = key_of(t)
        if k in sent:
            skipped.append((t["symbol"], "already sent on %s" % sent[k].get("at", "?")[:10]))
            continue
        out.append((k, t, r))
    return out, skipped


# ---------------------------------------------------------------- Telegram

def _post(url, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sleep(s):
    time.sleep(s)


def credentials():
    tok = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", tok), ("TELEGRAM_CHAT_ID", chat)) if not v]
    if missing:
        raise NotifyError("set %s in the environment (never in a file): see the README" % " and ".join(missing))
    if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", tok):
        raise NotifyError("TELEGRAM_BOT_TOKEN does not look like a bot token (digits:letters from @BotFather)")
    return tok, chat


def send(token, chat, text):
    """-> message_id. One retry after Telegram's retry_after on a 429. The token never shows in errors."""
    url = API % token
    for attempt in (0, 1):
        try:
            res = _post(url, {"chat_id": chat, "text": text, "disable_web_page_preview": True})
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                body = {}
            finally:
                e.close()
            wait = ((body.get("parameters") or {}).get("retry_after"))
            if e.code == 429 and attempt == 0 and wait:
                _sleep(min(float(wait), 60))
                continue
            raise NotifyError("Telegram answered HTTP %d: %s" % (e.code, body.get("description") or "no reason"))
        except (urllib.error.URLError, OSError) as e:
            raise NotifyError("could not reach api.telegram.org: %s" % getattr(e, "reason", e))
        if not res.get("ok"):
            raise NotifyError("Telegram said no: %s" % res.get("description"))
        return (res.get("result") or {}).get("message_id")
    raise NotifyError("Telegram kept rate limiting")


def prune(state, now_utc):
    cut = (now_utc - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    state["sent"] = {k: v for k, v in (state.get("sent") or {}).items() if str(v.get("at", ""))[:10] >= cut}
    return state


def main(argv=None):
    ap = argparse.ArgumentParser(description="Telegram notice for swing setups that passed trader-risk.")
    ap.add_argument("--risk", default="trader/state/swing_risk.json")
    ap.add_argument("--tickets", default="trader/swing_orders.json")
    ap.add_argument("--state", default="trader/state/telegram.json")
    ap.add_argument("--dry-run", action="store_true", help="print the messages; send and remember nothing")
    ap.add_argument("--test", action="store_true", help="send one test message and stop")
    ap.add_argument("--heartbeat", action="store_true", help="also send a short note when nothing new passed")
    ap.add_argument("--any-date", action="store_true", help="use a risk check from another day (testing)")
    ap.add_argument("--now", help="pretend it is this time (New York, or ISO with Z)")
    ap.add_argument("--quiet", action="store_true", help="print counts only (for public CI logs)")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.test:
            text = ("✅ Prueba de day-trader-skill (%s UTC): el bot y el chat funcionan. Los avisos de setups "
                    "swing llegaran aqui." % now.strftime("%Y-%m-%d %H:%M"))
            if a.dry_run:
                print(text)
                return 0
            tok, chat = credentials()
            send(tok, chat, text)
            print("Test message sent.")
            return 0
        today = to_eastern(now).date().isoformat()
        risk_doc = read_json(a.risk, "swing risk check (run trader-risk check.py --swing)")
        tickets = read_json(a.tickets, "swing tickets (run trader-budget)")
        state = read_json(a.state, "state", required=False) or {}
        todo, skipped = pending(risk_doc, tickets, state, today, a.any_date)
        tok = chat = None
        if not a.dry_run and (todo or a.heartbeat):
            tok, chat = credentials()
        sent = 0
        for k, t, r in todo:
            text = message(t, r)
            if a.dry_run:
                if not a.quiet:
                    print(text + "\n")
                continue
            mid = send(tok, chat, text)
            state.setdefault("sent", {})[k] = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "message_id": mid}
            write_json(a.state, prune(state, now))      # after each one: a crash never resends what went out
            sent += 1
        if a.heartbeat and not todo:
            text = "Sin setups swing nuevos que pasen risk hoy (%s)." % today
            if a.dry_run:
                print(text)
            else:
                send(tok, chat, text)
        for sym, why in skipped:
            if not a.quiet:
                print("skip %s: %s" % (sym, why))
        print("%s %d message(s); %d skipped." % ("Would send" if a.dry_run else "Sent", len(todo) if a.dry_run else sent,
                                               len(skipped)))
    except NotifyError as e:
        print("notify_telegram.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
