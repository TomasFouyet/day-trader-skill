#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the reel: stop 358.32, target 370.04
    python3 exit.py plan --symbol TSLA --entry 364.18 --or-high 362.18 --or-low 354.47
    python3 exit.py plan --stop atr:1.5 --offline TSLA.json # ATR needs the session's bars
    python3 exit.py plan --symbol NVDA --entry 190.25 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the cent, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round half up to the cent):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (v2
plans longs only).

flat_by (default 15:55, or "flat_by" in trader/config.json): out by then, win
or lose. On a 1:00 p.m. early close it moves up with the close (12:55).
"min_reward_risk" in the config (default 1.0) only warns here; trader-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:55", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is New York time."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" ET"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants a New York time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants a New York time like 15:55, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def session(d):
    """(open_time, close_time) in New York time, or None if closed all day."""
    if d.weekday() >= 5 or d.isoformat() in NYSE_HOLIDAYS:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


# ---------------------------------------------------------------- money math (Decimal, to the cent)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def cents(x):
    """Round half up to the cent."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_cents(x):
    """A long's stop: round down, away from the entry."""
    return D(x).quantize(CENT, rounding=ROUND_FLOOR)


def money(x):
    return "$%s" % format(D(x).quantize(CENT), ",f")


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*\$?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None):
    """The stop, the target and the risk, in exact cents. Raises PlanError with the reason."""
    e = cents(entry)
    if e <= 0:
        raise PlanError("the entry price must be above $0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = cents(or_high), cents(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = $%s" % (money(oh), money(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = $%s" % (money(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR $%s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = floor_cents(raw)
    if stop != D(raw):
        detail = (detail + ", rounded down" if detail else "$%s rounded down" % num(raw))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above $0" % money(stop))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)), money(e)))
    if target_rule == "r":
        target = cents(e + target_param * risk)
        t_label = r_label(target_param)
    else:
        target = cents(target_param)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def _marketdata():
    """trader-scanner/marketdata.py (Alpaca, when its keys are set), one shared instance per process."""
    m = sys.modules.get("trader_marketdata")
    if m is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-scanner",
                            "marketdata.py")
        if not os.path.exists(path):
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("trader_marketdata", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules["trader_marketdata"] = m
        spec.loader.exec_module(m)
    return m


def _alpaca(path_or_url):
    md = _marketdata()
    return md.yahoo_like(path_or_url) if md is not None and md.enabled() else None


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    alp = _alpaca(path)
    if alp is not None:
        return alp
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    ysym = sym.upper().replace(".", "-")
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, ysym + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_eastern(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None):
    """Closed, on-grid bars of one New York session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_eastern(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_eastern(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
        if doc.get("mode") is not None:
            cfg["mode"] = doc["mode"]
        if doc.get("swing") is not None:
            cfg["swing"] = doc["swing"]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). The config's flat-by moves up with a 1:00 p.m. early close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = session(day) if day else None
    if s:
        close_dt = datetime.datetime.combine(day, s[1])
        at = datetime.datetime.combine(day, t)
        if at >= close_dt:
            if explicit:
                raise PlanError("--flat-by %s is at or after the %s close on %s" % (hm(t), ampm(s[1]), day))
            moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
            if moved.time() <= OPEN:
                raise PlanError("flat_by %s in the config is at or after the close" % hm(t))
            t = moved.time()
            note = "moved up for the 1:00 p.m. early close (%s)" % NYSE_EARLY_CLOSE.get(day.isoformat(), "")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("v2 plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v2 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_eastern(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(cents(info["or_high"])), "low": float(cents(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = to_eastern(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s ET, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


# ---------------------------------------------------------------- swing mode (multi-day, daily bars)
#
# "mode": "swing" in trader/config.json (or --mode swing) plans a long held for
# days, not minutes: the stop comes from the DAILY ATR (atr:2 by default), the
# target is 2R, there is no flat-by (the trade holds overnight) and a time stop
# closes it at the close of trading day N after the signal, win or lose. The
# intraday path above is untouched: without "mode": "swing" nothing changes.

SWING_DEFAULTS = {
    "horizon_days": 7,              # trading days you plan to hold
    "stop": "atr:2",                # 2 x the 14-day ATR under the entry
    "target": "2R",
    "time_stop_days": None,         # None = the horizon: close at the close of day N
    "max_risk_per_trade_pct": 1.0,  # of the account, for trader-budget / trader-risk
    "max_positions": 5,             # open swing positions at once
    "max_exposure_pct": 60.0,       # all open swing positions together, % of the account
    "atr_days": 14,
}
SWING_SETUPS = ("breakout20", "pullback_ema20", "rsi2", "breakdown20", "pullback_ema20_short", "rsi2_short", "manual")
SHORT_SETUPS = ("breakdown20", "pullback_ema20_short", "rsi2_short")
SWING_RANGE = "1y"
SWING_NOTE = ("Un stop es un plan, no una garantia: en swing la accion duerme fuera del mercado y un gap "
              "de apertura puede ejecutarse por debajo del stop.")
WORDS_ES = {1: "una vez", 2: "dos veces", 3: "tres veces", 4: "cuatro veces", 5: "cinco veces"}
DIAS = ("lun", "mar", "mie", "jue", "vie", "sab", "dom")


def resolve_mode(a, cfg):
    m = str(getattr(a, "mode", None) or cfg.get("mode") or "intraday").strip().lower()
    if m not in ("intraday", "swing"):
        raise PlanError('"mode" in the config is "intraday" or "swing", not %r' % m)
    return m


def _int_in(v, lo, hi, what):
    try:
        n = int(v)
        if n != float(v):
            raise ValueError
    except (TypeError, ValueError):
        raise PlanError("%s tiene que ser un numero entero, no %r" % (what, v))
    if not lo <= n <= hi:
        raise PlanError("%s tiene que estar entre %d y %d (es %d)" % (what, lo, hi, n))
    return n


def _num_in(v, lo, hi, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PlanError("%s tiene que ser un numero, no %r" % (what, v))
    if not lo < x <= hi:
        raise PlanError("%s tiene que ser mayor que %g y como mucho %g (es %g)" % (what, lo, hi, x))
    return x


def swing_config(cfg):
    """The "swing" block of the config, with the defaults filled in and checked."""
    raw = cfg.get("swing") or {}
    if not isinstance(raw, dict):
        raise PlanError('"swing" en la config tiene que ser un objeto {...}')
    sw = dict(SWING_DEFAULTS)
    sw.update({k: v for k, v in raw.items() if v is not None})
    sw["horizon_days"] = _int_in(sw["horizon_days"], 1, 60, "swing.horizon_days")
    if sw["time_stop_days"] is not None:
        sw["time_stop_days"] = _int_in(sw["time_stop_days"], 1, 60, "swing.time_stop_days")
    sw["atr_days"] = _int_in(sw["atr_days"], 2, 100, "swing.atr_days")
    sw["max_positions"] = _int_in(sw["max_positions"], 1, 100, "swing.max_positions")
    sw["max_risk_per_trade_pct"] = _num_in(sw["max_risk_per_trade_pct"], 0, 10, "swing.max_risk_per_trade_pct")
    sw["max_exposure_pct"] = _num_in(sw["max_exposure_pct"], 0, 100, "swing.max_exposure_pct")
    rule, _ = parse_stop(sw["stop"])
    if rule not in ("atr", "pct", "price"):
        raise PlanError("swing.stop es atr:K, pct:X o price:X (no hay rango de apertura en swing), no %r" % sw["stop"])
    parse_target(sw["target"])
    return sw


def add_trading_days(day, n):
    """The NYSE session n trading days after day (weekends and the holiday table skipped)."""
    d, k = day, 0
    while k < n:
        d += datetime.timedelta(days=1)
        if session(d):
            k += 1
    return d


def last_closed_session(now_utc):
    """The latest New York session that has already closed at now_utc."""
    local = to_eastern(now_utc)
    d = local.date()
    s = session(d)
    if s and local.time() >= s[1]:
        return d
    d -= datetime.timedelta(days=1)
    while not session(d):
        d -= datetime.timedelta(days=1)
    return d


def load_daily(sym, offline, now_utc):
    """Daily bars [{date, o, h, l, c}] oldest first. Today's bar is dropped until the session closes."""
    ysym = sym.upper().replace(".", "-")
    if offline:
        path = offline
        if os.path.isdir(offline):
            for name in ("%s-1y1d.json" % ysym, "%s-5y1d.json" % ysym, "%s-1d.json" % ysym, "%s.json" % ysym):
                for cand in (name, name + ".gz"):
                    if os.path.exists(os.path.join(offline, cand)):
                        path = os.path.join(offline, cand)
                        break
                if path != offline:
                    break
        if not os.path.isfile(path):
            raise PlanError("no hay archivo offline de velas diarias de %s en %s" % (ysym, offline))
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("no se pudo leer %s: %s" % (path, e))
    else:
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=1d" % (ysym, SWING_RANGE))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "1d":
        raise PlanError("el modo swing necesita velas diarias (1d); el archivo tiene %s" % gran)
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    local = to_eastern(now_utc)
    today = local.date()
    s_today = session(today)
    by_date = {}
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        d = to_eastern(utc_of(t)).date()
        if d > today or (d == today and s_today and local.time() < s_today[1]):
            continue   # the session is still open: that bar is not a daily close yet
        h, lo, o = at("high", i), at("low", i), at("open", i)
        by_date[d] = {"date": d, "o": float(c if o is None else o), "h": float(c if h is None else h),
                      "l": float(c if lo is None else lo), "c": float(c)}
    return [by_date[d] for d in sorted(by_date)]


def daily_atr(bars, n):
    """Average true range of the last n daily bars, the overnight gap included."""
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev["c"]), abs(b["l"] - prev["c"])))
    use = trs[-n:]
    if not use:
        return None, 0
    return sum(use) / len(use), len(use)


def r_label_es(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s el riesgo)" % (num(r), WORDS_ES.get(n, num(r) + " veces"))


def gather_swing(a):
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.or_high is not None or a.or_low is not None:
        raise PlanError("--or-high/--or-low son del intradia: en swing no hay rango de apertura")
    if a.symbol:
        return {"symbol": a.symbol.upper(), "entry": a.entry, "date": a.date, "setup": a.setup, "source": "flags",
                "setup_params": None, "earnings": None, "side": side_of(a.side), "name": None}
    if not a.from_path:
        raise PlanError("en modo swing da --symbol TICKER (y --entry PRECIO, o se usa el ultimo cierre diario), "
                        "o --from un JSON con symbol")
    doc = read_json(a.from_path, "candidate")
    if isinstance(doc, dict) and isinstance(doc.get("candidates"), list):
        cands = doc["candidates"]
        if not cands:
            raise PlanError("%s no tiene candidatos: el scanner swing no encontro setups" % a.from_path)
        if a.pick:
            found = [c for c in cands if str(c.get("symbol") or "").upper() == a.pick.upper()]
            if not found:
                raise PlanError("%s no esta entre los candidatos de %s (%s)" % (
                    a.pick.upper(), a.from_path, ", ".join(str(c.get("symbol")) for c in cands)))
            doc = dict(found[0])
        elif len(cands) == 1:
            doc = dict(cands[0])
        else:
            raise PlanError("%s tiene %d candidatos: elige uno con --pick TICKER (%s)" % (
                a.from_path, len(cands), ", ".join(str(c.get("symbol")) for c in cands)))
        top = read_json(a.from_path, "candidate")
        if not doc.get("setup_params") and isinstance(top.get("setup_params"), dict):
            doc["setup_params"] = top["setup_params"]
    sym = str(doc.get("symbol") or "").upper()
    if not sym:
        raise PlanError("%s no tiene symbol" % a.from_path)
    entry = a.entry if a.entry is not None else (doc.get("entry") if doc.get("entry") is not None else doc.get("price"))
    return {"symbol": sym, "entry": entry, "date": a.date or doc.get("date"), "setup": a.setup or doc.get("setup"),
            "source": a.from_path, "setup_params": doc.get("setup_params"), "earnings": doc.get("earnings"),
            "side": side_of(doc.get("side") or a.side), "name": doc.get("name"), "asset": doc.get("asset")}


def side_of(x):
    s = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}.get(str(x or "buy").lower())
    if s is None:
        raise PlanError("side es buy/long o sell/short, no %r" % x)
    return s


def compute_short(entry, stop_rule, stop_param, target_rule, target_param, atr=None):
    """The short side of compute(): the stop ABOVE the entry, rounded up (away), the target below."""
    e = cents(entry)
    if e <= 0:
        raise PlanError("the entry price must be above $0")
    if stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the daily bars")
        raw = e + stop_param * D(round(atr, 6))
    elif stop_rule == "pct":
        raw = e * (1 + stop_param / 100)
    else:
        raw = stop_param
    stop = D(raw).quantize(CENT, rounding=ROUND_CEILING)
    if stop <= e:
        raise PlanError("el stop %s de un short tiene que ir sobre la entrada %s" % (money(stop), money(e)))
    risk = stop - e
    if risk < e * MIN_RISK_PCT / 100:
        raise PlanError("el stop esta a %s de la entrada: demasiado cerca para sobrevivir el spread" % money(risk))
    if target_rule == "r":
        target = cents(e - target_param * risk)
    else:
        target = cents(target_param)
    if target >= e or target <= 0:
        raise PlanError("el objetivo %s de un short tiene que ir bajo la entrada %s y sobre $0" % (money(target), money(e)))
    rr = ((e - target) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"entry": e, "stop": stop, "target": target, "risk": risk, "reward_risk": rr,
            "target_r": target_param if target_rule == "r" else rr, "raw": raw}


def build_swing_plan(a, now_utc):
    cfg = read_config(a.config)
    sw = swing_config(cfg)
    if a.flat_by:
        raise PlanError("--flat-by es del intradia: un plan swing se mantiene overnight y termina con el stop de "
                        "tiempo (--time-stop-days)")
    info = gather_swing(a)
    setup = (info["setup"] or "manual").lower()
    if setup in ("orb", "level"):
        raise PlanError("el setup %s es intradia; en swing usa %s" % (setup, ", ".join(SWING_SETUPS)))
    if setup not in SWING_SETUPS:
        raise PlanError("setup swing desconocido %r: usa %s" % (setup, ", ".join(SWING_SETUPS)))
    if setup != "manual" and (setup in SHORT_SETUPS) != (info.get("side") == "sell"):
        raise PlanError("el setup %s es %s pero el plan es %s: usa --side %s" % (
            setup, "short" if setup in SHORT_SETUPS else "long", "short" if info.get("side") == "sell" else "long",
            "sell" if setup in SHORT_SETUPS else "buy"))
    horizon = _int_in(a.horizon_days if a.horizon_days is not None else sw["horizon_days"], 1, 60, "--horizon-days")
    if a.time_stop_days is not None:
        tstop = _int_in(a.time_stop_days, 1, 60, "--time-stop-days")
    else:
        tstop = sw["time_stop_days"] if sw["time_stop_days"] is not None else horizon
    stop_text = a.stop or sw["stop"]
    stop_rule, stop_param = parse_stop(stop_text)
    if stop_rule not in ("atr", "pct", "price"):
        raise PlanError("en swing el stop es atr:K, pct:X o price:X (no hay rango de apertura), no %r" % stop_text)
    target_rule, target_param = parse_target(a.target or sw["target"])
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    warnings = []
    atr = atr_n = None
    basis = "tu precio"
    entry = info["entry"]
    if stop_rule == "atr" or entry is None:
        bars = load_daily(info["symbol"], a.offline, now_utc)
        if day:
            bars = [b for b in bars if b["date"] <= day]
        if not bars:
            raise PlanError("no hay velas diarias cerradas de %s%s" % (info["symbol"], " hasta el %s" % day if day else ""))
        if day and bars[-1]["date"] != day:
            warnings.append("No hay vela diaria de %s el %s: se usa la ultima, del %s."
                            % (info["symbol"], day, bars[-1]["date"]))
        day = bars[-1]["date"]
        if entry is None:
            entry = bars[-1]["c"]
            basis = "cierre del %s" % day.isoformat()
        if stop_rule == "atr":
            if len(bars) < 4:
                raise PlanError("un stop atr necesita al menos 4 velas diarias de %s; hay %d" % (info["symbol"], len(bars)))
            atr, atr_n = daily_atr(bars, sw["atr_days"])
    elif day is None:
        day = last_closed_session(now_utc) if not session(to_eastern(now_utc).date()) else to_eastern(now_utc).date()
    short = info.get("side") == "sell"
    if short:
        c = compute_short(entry, stop_rule, stop_param, target_rule, target_param, atr=atr)
    else:
        c = compute(entry, stop_rule, stop_param, target_rule, target_param, atr=atr, atr_bars=atr_n)
    e = c["entry"]
    if short:
        raw = c["raw"]
        if stop_rule == "atr":
            label = "%s x ATR(%d) diario sobre la entrada" % (num(stop_param), atr_n)
            detail = "%s + %s x $%s = $%s" % (money(e), num(stop_param),
                                               num(D(round(atr, 6)).quantize(Decimal("0.0001"))),
                                               num(D(raw).quantize(Decimal("0.0001"))))
        elif stop_rule == "pct":
            label = "%s%% sobre la entrada" % num(stop_param)
            detail = "%s x (1 + %s%%) = $%s" % (money(e), num(stop_param), num(raw))
        else:
            label, detail = "tu precio", ""
        if c["stop"] != D(raw):
            detail = (detail + ", redondeado hacia arriba" if detail else "$%s redondeado hacia arriba" % num(raw))
    elif stop_rule == "atr":
        a4 = D(round(atr, 6)).quantize(Decimal("0.0001"))
        raw = e - stop_param * D(round(atr, 6))
        label = "%s x ATR(%d) diario bajo la entrada" % (num(stop_param), atr_n)
        detail = "%s - %s x $%s = $%s" % (money(e), num(stop_param), num(a4), num(raw.quantize(Decimal("0.0001"))))
        if atr_n < sw["atr_days"]:
            detail += " (solo %d dias de ATR)" % atr_n
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% bajo la entrada" % num(stop_param)
        detail = "%s x (1 - %s%%) = $%s" % (money(e), num(stop_param), num(raw))
    else:
        raw, label, detail = stop_param, "tu precio", ""
    if not short and c["stop"] != D(raw):
        detail = (detail + ", redondeado hacia abajo" if detail else "$%s redondeado hacia abajo" % num(raw))
    t_label = r_label_es(target_param) if target_rule == "r" else "tu precio (%sR)" % ratio(c["reward_risk"])
    tdate = add_trading_days(day, tstop)
    plan = {"created_by": "trader-exit", "mode": "swing", "symbol": info["symbol"], "side": info.get("side", "buy"),
            "name": info.get("name"), "asset": info.get("asset"),
            "date": day.isoformat(), "setup": setup, "entry": float(e), "entry_basis": basis, "interval": "1d",
            "stop": float(c["stop"]), "stop_rule": stop_rule,
            "stop_param": float(stop_param) if stop_rule in ("atr", "pct") else None,
            "stop_label": label, "stop_detail": detail,
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": t_label,
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]),
            "horizon_days": horizon, "time_stop_days": tstop, "time_stop_date": tdate.isoformat(),
            "time_stop_rule": "cerrar al cierre del dia habil %d despues de la senal, pase lo que pase" % tstop,
            "flat_by": None,
            "limits": {"max_risk_per_trade_pct": sw["max_risk_per_trade_pct"], "max_positions": sw["max_positions"],
                       "max_exposure_pct": sw["max_exposure_pct"]},
            "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": SWING_NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    if info.get("setup_params"):
        plan["setup_params"] = info["setup_params"]
    er = info.get("earnings")
    if isinstance(er, dict) and er.get("signal_date") == plan["date"] and er.get("horizon_days") == tstop:
        plan["earnings"] = er
        if er.get("flag"):
            warnings.append("\u26a0 %s (%s)%s." % (er["flag"], er.get("next") or "fecha desconocida",
                                                  ": BLOQUEADO, trader-risk no lo deja pasar" if er.get("blocked") else ""))
    else:
        warnings.append("Sin revision de resultados para este plan: corre trader-analyst earnings.py check "
                        "--from trader/plan.json antes de trader-risk.")
    if tstop > horizon:
        warnings.append("El stop de tiempo (dia %d) queda despues del horizonte (%d dias)." % (tstop, horizon))
    if tdate.year > int(max(NYSE_HOLIDAYS)[:4]):
        warnings.append("El calendario de feriados NYSE llega a %s: la fecha del stop de tiempo solo salta fines "
                        "de semana despues de eso." % max(NYSE_HOLIDAYS)[:4])
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Recompensa:riesgo %s esta bajo tu min_reward_risk %s de la config: trader-risk lo revisa."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    latest = last_closed_session(now_utc)
    if day < latest:
        warnings.append("Este plan es de la senal del %s; la ultima sesion cerrada es el %s." % (day, latest))
    return plan, warnings


def describe_swing(plan):
    day = datetime.date.fromisoformat(plan["date"])
    tday = datetime.date.fromisoformat(plan["time_stop_date"])
    e = D(plan["entry"])
    pct = (D(plan["risk_per_share"]) / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    out = ["Plan de salida swing: %s %s, senal del %s %s (setup %s), velas diarias" % (
        plan["symbol"], "corto (SHORT)" if plan.get("side") == "sell" else "largo", DIAS[day.weekday()], day.isoformat(), plan.get("setup") or "manual")]
    basis = plan.get("entry_basis")
    out.append("  Entrada          %s%s" % (money(plan["entry"]), ("  " + basis) if basis else ""))
    out.append("  Stop             %s  %s%s" % (money(plan["stop"]), plan.get("stop_label") or "",
                                                (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Objetivo         %s  %s" % (money(plan["target"]), plan.get("target_label") or ""))
    out.append("  Riesgo/accion    %s (%s%% de la entrada)" % (money(plan["risk_per_share"]), pct))
    out.append("  Recompensa:riesgo %s" % ratio(plan["reward_risk"]))
    out.append("  Horizonte        %d dias habiles" % plan["horizon_days"])
    out.append("  Stop de tiempo   al cierre del %s %s (dia habil %d), gane o pierda" % (
        DIAS[tday.weekday()], tday.isoformat(), plan["time_stop_days"]))
    out.append("  Sin flat-by: la posicion se mantiene overnight.")
    out.append("Fijado antes de la entrada. Si el precio toca el stop, sales; nunca lo alejes.")
    out.append(plan.get("note") or SWING_NOTE)
    return out


def cmd_plan_swing(a, now):
    plan, warnings = build_swing_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe_swing(plan):
        print(line)
    for w in warnings:
        print("Nota: " + w)
    if a.write:
        print("Guardado en %s." % a.write)
    print("Siguiente: /trader-timemachine --horizon-days %d simula los proximos dias, luego /trader-budget lo "
          "dimensiona. No se coloco nada." % plan["time_stop_days"])
    return 0


def show_swing(plan, a):
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "horizon_days",
              "time_stop_days", "time_stop_date"):
        if plan.get(k) is None:
            raise PlanError("%s no tiene %r: crea un plan nuevo con exit.py plan --mode swing --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe_swing(plan):
        print(line)
    today = to_eastern(parse_now(a.now)).date().isoformat()
    if today > plan["time_stop_date"]:
        print("Nota: el stop de tiempo (%s) ya paso: si sigues dentro, cierra." % plan["time_stop_date"])
    return 0


def cmd_plan(a):
    now = parse_now(a.now)
    if resolve_mode(a, read_config(a.config)) == "swing":
        return cmd_plan_swing(a, now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    if plan.get("mode") == "swing":
        return show_swing(plan, a)
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_eastern(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level", "breakout20", "pullback_ema20", "rsi2", "breakdown20",
                                       "pullback_ema20_short", "rsi2_short", "manual"],
                   help="orb, level (intraday); breakout20, pullback_ema20, rsi2, manual, and the short ones (swing)")
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:15 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM New York (default: config flat_by, 15:55). Intraday only")
    p.add_argument("--mode", choices=["intraday", "swing"],
                   help="intraday (default) or swing: daily ATR stop, time stop, no flat-by. Default: config \"mode\"")
    p.add_argument("--horizon-days", type=int, help="swing: trading days you plan to hold (config swing.horizon_days, 7)")
    p.add_argument("--pick", metavar="TICKER", help="swing: the candidate to plan from a --from trader/swing.json")
    p.add_argument("--time-stop-days", type=int,
                   help="swing: close at the close of trading day N after the signal (default: the horizon)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop (daily bars in swing)")
    p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-19 10:15"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
