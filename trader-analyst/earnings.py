#!/usr/bin/env python3
"""Earnings calendar check for swing trades (trader-analyst). Standard library only.

A swing trade held for days can sit through a quarterly report, and a report
can gap the stock far past any stop. This tool works out when each candidate's
next report is due and flags the ones that land inside the holding horizon:
"resultados dentro del horizonte". By default a flagged setup is BLOCKED
("block_on_earnings": true in the config's "swing" block).

    python3 earnings.py check                          # trader/swing.json, annotated in place
    python3 earnings.py check --from trader/plan.json  # one swing plan
    python3 earnings.py check --news trader/news.json  # also flag fresh earnings headlines (trader-news --json)
    python3 earnings.py next AAPL KO --date 2026-10-30 --horizon-days 7

Where the date comes from, in order:
  1. trader/earnings_dates.json, your own list: {"AAPL": "2026-10-29", ...}.
     Exact dates you checked on the company's investor-relations page win.
  2. SEC EDGAR (the same submissions feed facts.py reads): the filing dates of
     the company's 8-Ks with item 2.02 (results of operations), the earnings
     release. A 2.02 filed after the signal day and inside the horizon is a
     known date. Otherwise the next one is estimated from the cadence: the last
     2.02 plus the median gap between recent ones (91 days when there is only
     one), moved to the same week as a year earlier when there is a 2.02 about
     364 days before. The estimate gets +- earnings_margin_days (5) either way.
     If the estimate is already past and nothing was filed, the report is
     overdue: treated as imminent.
  3. Nothing (an ETF, a foreign filer, no SEC User-Agent): unknown. A warning,
     and blocked only with "block_on_unknown_earnings": true.

The horizon is the signal day to the close of trading day N (the plan's
time_stop_days, the config's swing.time_stop_days / horizon_days, or 7),
NYSE weekends and holidays skipped. A report ON the signal day counts: an
after-the-close release gaps the next open.

An estimate is not a calendar. Yahoo's earnings calendar needs a login token
these days, so this reads only free official data and says how sure it is.
Check the date yourself before you buy. Never a buy or sell call.
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("trader_facts_for_earnings", os.path.join(HERE, "facts.py"))
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

DEFAULTS = {"block_on_earnings": True, "block_on_unknown_earnings": False, "earnings_margin_days": 5,
            "horizon_days": 7, "time_stop_days": None}
DEFAULT_GAP = 91
ANNUAL = 364
NEWS_HOURS = 72
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
FLAG = "resultados dentro del horizonte"


class EarningsError(Exception):
    pass


def _date(x):
    try:
        return datetime.date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def add_trading_days(day, n):
    d, k = day, 0
    while k < n:
        d += datetime.timedelta(days=1)
        if trading_day(d):
            k += 1
    return d


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise EarningsError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise EarningsError("%s is not valid JSON: %s" % (path, e))


def swing_config(path):
    cfg = read_json(path, "config", required=False) or {}
    sw = cfg.get("swing") if isinstance(cfg.get("swing"), dict) else {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in sw.items() if k in DEFAULTS and v is not None})
    try:
        out["earnings_margin_days"] = int(out["earnings_margin_days"])
        out["horizon_days"] = int(out["horizon_days"])
    except (TypeError, ValueError):
        raise EarningsError("swing.earnings_margin_days and swing.horizon_days must be whole numbers")
    if not 0 <= out["earnings_margin_days"] <= 30:
        raise EarningsError("swing.earnings_margin_days must be between 0 and 30")
    ex = sw.get("extra_symbols") or []
    out["no_earnings"] = [str(x).upper() for x in (ex if isinstance(ex, list) else str(ex).replace(",", " ").split())]
    return out


def manual_dates(path):
    doc = read_json(path, "earnings dates", required=False) or {}
    if isinstance(doc, dict) and isinstance(doc.get("dates"), dict):
        doc = doc["dates"]
    out = {}
    for k, v in (doc.items() if isinstance(doc, dict) else []):
        vals = v if isinstance(v, list) else [v]
        ds = [d for d in (_date(x) for x in vals) if d]
        if ds:
            out[str(k).upper()] = sorted(ds)
    return out


def earnings_8k_dates(submissions):
    """Filing dates of 8-Ks with item 2.02, oldest first (SEC submissions 'recent' block)."""
    rec = (submissions or {}).get("filings", {}).get("recent", {})
    forms, dates, items = rec.get("form", []), rec.get("filingDate", []), rec.get("items", [])
    out = set()
    for i, f in enumerate(forms):
        it = items[i] if i < len(items) else ""
        if f in ("8-K", "8-K/A") and "2.02" in (it or ""):
            d = _date(dates[i] if i < len(dates) else None)
            if d:
                out.add(d)
    return sorted(out)


def estimate(dates, signal, margin):
    """Past 2.02 dates -> the next report: {date, lo, hi, basis, known} or None."""
    after = [d for d in dates if d >= signal]
    if after:
        return {"date": after[0], "lo": after[0], "hi": after[0], "known": True,
                "basis": "8-K 2.02 filed %s" % after[0].isoformat()}
    past = [d for d in dates if d < signal]
    if not past:
        return None
    last = past[-1]
    gaps = [(b - a).days for a, b in zip(past[-9:], past[-8:])]
    gaps = [g for g in gaps if 45 <= g <= 135]
    gap = int(statistics.median(gaps)) if gaps else DEFAULT_GAP
    est = last + datetime.timedelta(days=gap)
    basis = "ultimo 8-K 2.02 el %s + %d dias%s" % (last.isoformat(), gap, "" if gaps else " (cadencia trimestral por defecto)")
    near = [y + datetime.timedelta(days=ANNUAL) for y in past
            if abs((y + datetime.timedelta(days=ANNUAL) - est).days) <= 21]
    if near:
        best = min(near, key=lambda x: abs((x - est).days))
        if best > last + datetime.timedelta(days=30):
            est = best
            basis = "misma semana que hace un ano (8-K 2.02 del %s + 364 dias)" % (best - datetime.timedelta(days=ANNUAL)).isoformat()
    lo, hi = est - datetime.timedelta(days=margin), est + datetime.timedelta(days=margin)
    overdue = hi < signal
    if overdue:
        lo, hi = signal, signal + datetime.timedelta(days=margin)
        basis += "; ya deberia haber publicado y no hay 8-K: se trata como inminente"
    return {"date": est, "lo": lo, "hi": hi, "known": False, "overdue": overdue, "basis": basis}


def assess(sym, signal, horizon, sw, manual=None, sec_dates=None, sec_error=None):
    """One ticker -> the earnings verdict for a swing trade from `signal` held `horizon` trading days."""
    end = add_trading_days(signal, horizon)
    out = {"symbol": sym, "signal_date": signal.isoformat(), "horizon_days": horizon, "horizon_end": end.isoformat(),
           "source": None, "next": None, "window": None, "in_horizon": None, "blocked": False, "flag": None,
           "warnings": []}
    est = None
    if manual:
        nxt = [d for d in manual if d >= signal]
        if nxt:
            est = {"date": nxt[0], "lo": nxt[0], "hi": nxt[0], "known": True, "basis": "trader/earnings_dates.json"}
            out["source"] = "manual"
    if est is None and sec_dates is not None:
        est = estimate(sec_dates, signal, sw["earnings_margin_days"])
        out["source"] = "sec" if est else None
    if est is None:
        why = sec_error or ("no hay 8-K 2.02 en el SEC para %s" % sym)
        out["warnings"].append("Fecha de resultados desconocida (%s): revisala a mano." % why)
        out["in_horizon"] = None
        out["blocked"] = bool(sw["block_on_unknown_earnings"])
        if out["blocked"]:
            out["flag"] = "resultados: fecha desconocida"
        return out
    out["next"] = est["date"].isoformat()
    out["window"] = [est["lo"].isoformat(), est["hi"].isoformat()]
    out["known"] = est["known"]
    out["basis"] = est["basis"]
    out["in_horizon"] = est["lo"] <= end and est["hi"] >= signal
    if out["in_horizon"]:
        out["flag"] = FLAG
        out["blocked"] = bool(sw["block_on_earnings"])
    if not est["known"]:
        out["warnings"].append("La fecha es una estimacion (%s): confirmala en la web de la empresa." % est["basis"])
    return out


GAP_FLAG = "sin 8-K de resultados: el salto no vino de un reporte"
GAP_MIN_DAYS = 10


def assess_gap(sym, signal, reaction, horizon, sw, manual=None, sec_dates=None, sec_error=None):
    """earnings_gap (momentum de resultados): the jump must come from an earnings report (an 8-K item
    2.02, or a manual date, on the reaction day or up to 5 days before it), and the trade ends before
    the NEXT report: the time stop is cut to the last session before its window."""
    out = {"symbol": sym, "signal_date": signal.isoformat(), "horizon_days": horizon, "setup": "earnings_gap",
           "reaction_date": reaction.isoformat(), "source": None, "next": None, "window": None, "in_horizon": False,
           "blocked": False, "flag": None, "confirmed": None, "warnings": []}
    dates = sorted(set(manual or []) | set(sec_dates or []))
    out["source"] = "manual" if manual else ("sec" if sec_dates is not None else None)
    near = [d for d in dates if reaction - datetime.timedelta(days=5) <= d <= reaction]
    if not near:
        out["blocked"] = True
        out["flag"] = GAP_FLAG if (sec_dates is not None or manual) else "resultados: no se pudo confirmar (%s)" % (
            sec_error or "sin datos del SEC")
        out["horizon_end"] = add_trading_days(signal, horizon).isoformat()
        return out
    out["confirmed"] = "8-K 2.02 del %s" % near[-1].isoformat() if not manual else "fecha manual %s" % near[-1].isoformat()
    est = estimate(dates, reaction + datetime.timedelta(days=1), sw["earnings_margin_days"])
    cut = horizon
    if est:
        out["next"] = est["date"].isoformat()
        out["window"] = [est["lo"].isoformat(), est["hi"].isoformat()]
        out["known"] = est["known"]
        out["basis"] = est["basis"]
        n, d = 0, signal
        while True:
            nd = add_trading_days(d, 1)
            if nd >= est["lo"]:
                break
            n, d = n + 1, nd
        cut = min(horizon, n)
        if not est["known"]:
            out["warnings"].append("Proximos resultados estimados (%s): la salida por tiempo se corta antes." % est["basis"])
    if cut < horizon:
        out["warnings"].append("Stop de tiempo recortado de %d a %d sesiones para salir antes de los proximos "
                               "resultados." % (horizon, cut))
    out["horizon_days"] = cut
    out["horizon_end"] = add_trading_days(signal, cut).isoformat()
    if cut < GAP_MIN_DAYS:
        out["blocked"] = True
        out["flag"] = "proximos resultados en menos de %d sesiones" % GAP_MIN_DAYS
    return out


NAME_STOP = {"inc", "inc.", "corp", "corp.", "corporation", "company", "co", "co.", "the", "group", "holdings",
             "plc", "ltd", "sa", "s.a.", "class", "common", "stock", "incorporated", "limited", "&", "and"}


def name_words(name):
    """The words of a company name worth matching in a headline: 'Welltower Inc.' -> ['welltower']."""
    out = []
    for w in re.split(r"[\s,()]+", str(name or "").lower()):
        w = w.strip(".")
        if len(w) >= 3 and w not in NAME_STOP:
            out.append(w)
    return out[:2]


def relevant(title, sym, name=None):
    """Does a headline talk about THIS company (its ticker or the first word of its name)?"""
    t = " %s " % re.sub(r"[^A-Za-z0-9&.]+", " ", str(title or "")).lower()
    if re.search(r"\b%s\b" % re.escape(sym.lower()), t) and (len(sym) > 1 or "(%s)" % sym in str(title)):
        return True
    words = name_words(name)
    return bool(words) and words[0] in t.split()


def news_items(news_doc, sym, now_utc, name=None, hours=NEWS_HOURS, limit=3):
    """Headlines from trader-news --json about this company, newest first: [{time, title, tags, earnings}]."""
    out = []
    for it in (news_doc or {}).get("items", []) if isinstance(news_doc, dict) else []:
        if sym not in [str(t).upper() for t in (it.get("touches") or [it.get("ticker")])]:
            continue
        if not relevant(it.get("title"), sym, name):
            continue
        t = it.get("time")
        try:
            when = datetime.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            if when.tzinfo:
                when = (when - when.utcoffset()).replace(tzinfo=None)
        except (TypeError, ValueError):
            when = None
        if when is not None and now_utc is not None and (now_utc - when).total_seconds() > hours * 3600:
            continue
        out.append({"time": str(t)[:16] if t else None, "title": it.get("title", ""),
                    "earnings": "earnings" in (it.get("tags") or [])})
    out.sort(key=lambda x: x["time"] or "", reverse=True)
    seen, uniq = set(), []
    for x in out:
        k = x["title"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


def news_flags(news_doc, sym, now_utc, hours=NEWS_HOURS):
    """Earnings-tagged items from trader-news --json for this ticker, in the last `hours`."""
    out = []
    for it in (news_doc or {}).get("items", []) if isinstance(news_doc, dict) else []:
        if "earnings" not in (it.get("tags") or []):
            continue
        if sym not in [str(t).upper() for t in (it.get("touches") or [it.get("ticker")])]:
            continue
        t = it.get("time")
        try:
            when = datetime.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            if when.tzinfo:
                when = (when - when.utcoffset()).replace(tzinfo=None)
        except (TypeError, ValueError):
            when = None
        if when is not None and (now_utc - when).total_seconds() > hours * 3600:
            continue
        out.append("Noticia de resultados (%s): %s" % (str(t)[:16] if t else "sin fecha", it.get("title", "")))
    return out


class Sec(object):
    """SEC lookups through facts.py's Fetcher, cached per run."""

    def __init__(self, ua, offline=None):
        self.ua, self.offline = ua, offline
        self.fetcher = facts.Fetcher(ua, offline=offline)
        self._ciks = None

    def dates(self, sym):
        """-> (dates or None, error or None)."""
        if not self.ua and not self.offline:
            return None, "sin SEC User-Agent (SEC_USER_AGENT o sec_user_agent en la config)"
        try:
            if self._ciks is None:
                self._ciks = facts.ticker_ciks(self.fetcher)
            cik = (self._ciks.get(sym.replace("-", ".")) or (None,))[0]
            if not cik:
                return None, "%s no tiene CIK en el SEC (ETF o emisor extranjero?)" % sym
            sub = self.fetcher.json(facts.SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
        except facts.FactsError as e:
            return None, str(e)
        return earnings_8k_dates(sub), None


def horizon_of(item, sw, explicit=None):
    if explicit is not None:
        return int(explicit)
    for k in ("time_stop_days", "horizon_days"):
        if item.get(k):
            return int(item[k])
    return int(sw["time_stop_days"] or sw["horizon_days"])


def annotate(items, sw, sec, manual, horizon=None, news=None, now_utc=None, default_date=None):
    out = []
    for it in items:
        sym = str(it.get("symbol") or "").upper()
        signal = _date(it.get("date")) or default_date
        if not sym or signal is None:
            raise EarningsError("each item needs a symbol and a date (got %r)" % {k: it.get(k) for k in ("symbol", "date")})
        if it.get("extra") or sym in (sw.get("no_earnings") or []):
            hz = horizon_of(it, sw, horizon)
            res = {"symbol": sym, "signal_date": signal.isoformat(), "horizon_days": hz,
                   "horizon_end": add_trading_days(signal, hz).isoformat(), "source": "n/a", "next": None,
                   "window": None, "in_horizon": False, "blocked": False, "flag": None, "no_earnings": True,
                   "warnings": []}
        elif it.get("setup") == "earnings_gap":
            dates, err = sec.dates(sym)
            reaction = _date(it.get("reaction_date")) or signal
            res = assess_gap(sym, signal, reaction, horizon_of(it, sw, horizon), sw, manual.get(sym), dates, err)
            it["time_stop_days"] = res["horizon_days"]
            if isinstance(it.get("exit"), dict):
                it["exit"]["time_stop_days"] = res["horizon_days"]
        else:
            dates, err = (None, None) if manual.get(sym) else sec.dates(sym)
            res = assess(sym, signal, horizon_of(it, sw, horizon), sw, manual.get(sym), dates, err)
        if news is not None:
            res["news"] = news_items(news, sym, now_utc, it.get("name"))
            for n in res["news"]:
                if n["earnings"]:
                    res["warnings"].append("Noticia de resultados (%s): %s" % (n["time"] or "sin fecha", n["title"]))
        it["earnings"] = res
        out.append(res)
    return out


def describe(res):
    if res["next"]:
        when = "%s (%s)" % (res["next"], "fecha conocida" if res.get("known") else
                            "estimada, ventana %s a %s" % tuple(res["window"]))
    else:
        when = "desconocida"
    head = "%-6s senal %s, %d dias hasta el %s: proximos resultados %s." % (
        res["symbol"], res["signal_date"], res["horizon_days"], res["horizon_end"], when)
    lines = [head]
    if res.get("confirmed"):
        lines.append("       ✓ salto del %s confirmado: %s" % (res["reaction_date"], res["confirmed"]))
    if res["flag"]:
        lines.append("       ⚠ %s%s" % (res["flag"], ": BLOQUEADO" if res["blocked"] else " (aviso, no bloquea)"))
    for w in res["warnings"]:
        lines.append("       ~ " + w)
    return lines


def _now(text):
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (t - t.utcoffset()).replace(tzinfo=None) if t.tzinfo else t + datetime.timedelta(hours=5)


def cmd_check(a):
    sw = swing_config(a.config)
    doc = read_json(a.from_path, "candidates (run scan.py swing first) or plan")
    items = doc.get("candidates") if isinstance(doc, dict) and "candidates" in doc else [doc]
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise EarningsError("%s holds no candidates" % a.from_path)
    news = read_json(a.news, "news", required=True) if a.news else None
    sec = Sec(facts.user_agent(a.user_agent, a.config), a.offline)
    res = annotate(items, sw, sec, manual_dates(a.dates), a.horizon_days, news, _now(a.now))
    if isinstance(doc, dict) and "candidates" in doc:
        doc["earnings_checked_at"] = _now(a.now).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["earnings_rules"] = {k: sw[k] for k in ("block_on_earnings", "block_on_unknown_earnings",
                                                     "earnings_margin_days")}
    if not a.no_write:
        out = a.out or a.from_path
        with open(out + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(out + ".tmp", out)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for r in res:
            print("\n".join(describe(r)))
        nb = sum(1 for r in res if r["blocked"])
        print("%d revisado(s), %d bloqueado(s) por resultados. Una fecha estimada no es un calendario: "
              "confirmala antes de comprar." % (len(res), nb))
        if not a.no_write:
            print("Anotado en %s." % (a.out or a.from_path))
    return 1 if any(r["blocked"] for r in res) else 0


def cmd_next(a):
    sw = swing_config(a.config)
    day = _date(a.date) or (_now(a.now) - datetime.timedelta(hours=5)).date()
    items = [{"symbol": t.upper(), "date": day.isoformat()} for t in a.tickers]
    sec = Sec(facts.user_agent(a.user_agent, a.config), a.offline)
    res = annotate(items, sw, sec, manual_dates(a.dates), a.horizon_days)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        for r in res:
            print("\n".join(describe(r)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Earnings inside a swing horizon (trader-analyst).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--dates", default="trader/earnings_dates.json", help="your own dates {\"AAPL\": \"2026-10-29\"}")
        p.add_argument("--horizon-days", type=int, help="trading days held (default: the item's time_stop_days, or the config)")
        p.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
        p.add_argument("--offline", metavar="DIR", help="company_tickers.json and submissions-CIK*.json, no network")
        p.add_argument("--now", help="ISO time (UTC with Z), for reproducible runs")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="annotate trader/swing.json (or a plan) with the earnings flag")
    common(p)
    p.add_argument("--from", dest="from_path", default="trader/swing.json")
    p.add_argument("--out", help="write here instead of back into --from")
    p.add_argument("--news", metavar="FILE", help="trader-news --json output: flag fresh earnings headlines")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("next", help="the next report for some tickers")
    common(p)
    p.add_argument("tickers", nargs="+")
    p.add_argument("--date", help="signal day YYYY-MM-DD (default today)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    if a.horizon_days is not None and not 1 <= a.horizon_days <= 60:
        print("earnings.py: --horizon-days must be between 1 and 60", file=sys.stderr)
        return 2
    try:
        return {"check": cmd_check, "next": cmd_next}[a.cmd](a)
    except EarningsError as e:
        print("earnings.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
