#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Replays a planned day trade before you risk a dollar: 10,000 simulated runs of
the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:55            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol TSLA --date 2026-10-19 --entry 364.18 \\
        --stop 358.32 --target 370.04 --entry-time "10:15 AM"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the reel's sim.py, same algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m;
     one request). Keep only FULL sessions before the plan's date: 78 bars,
     9:30 to 4:00 New York time. Half days, gaps and today are left out.
  2. The pool: for every bar slot 1..77 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..77, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 16:00, the close, or
     13:00 on an NYSE early-close day).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: earnings, news and halts can do what the last 60 days never did.

Swing (multi-day): --horizon-days N, or a plan.json with "mode": "swing" from
trader-exit, replays the next N trading days on daily bars instead. See the
"swing: multi-day" section below for the method.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<T>?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are New York (ET) wall-clock; --now "2026-10-19 10:15" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 78    # 9:30 to 16:00 in 5-minute bars
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET, from https://www.nyse.com/markets/hours-calendars (read 2026-10-13)
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
EARLY_MIN = 13 * 60


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time (copied from trader-alerts)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
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


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'10:15 AM', '10:15am', '15:55' or '3:55' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants a New York time like 10:15 AM or 15:55, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(sym.upper().replace(".", "-")))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym, url_for=None):
    """One request for 60 days of 5-min bars (or url_for's chart). query2 first,
    one retry after a 429, then query1 on 429 or 5xx."""
    url_for = url_for or chart_url
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = url_for(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the ticker" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown ticker"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:49:45), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 9:30 to 16:00 New York time."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_eastern(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all 78 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (half days, gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the reel's sim.py."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def cents_away(x, long):
    """Round a stop to the cent, away from the entry: down for a buy, up for a sell."""
    c = round(x * 100, 4)
    return (math.floor(c) if long else math.ceil(c)) / 100.0


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long):
    r = rule["stop_rule"]
    if r == "or_mid":
        return cents_away((oh + ol) / 2, long)
    if r in ("or_low", "or_high"):
        return cents_away(ol if long else oh, long)
    frac = rule["stop_pct"] / 100.0
    return cents_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def history_replay(sessions, rule, last, side="buy"):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = round(e + rule["target_r"] * risk, 2) if long else round(e - rule["target_r"] * risk, 2)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": clock12(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_eastern(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        raise TMError("the plan needs a ticker symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15 AM)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15 AM) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 9:35 AM and 3:55 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= 390:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(cents_away((oh + ol) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(cents_away(ol if long else oh, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or "16:00"
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo"):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != sym.replace(".", "-"):
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    early = plan["date"] in NYSE_EARLY_CLOSE
    end_min = EARLY_MIN if early else CLOSE_MIN
    until_min = end_min if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock12(end_min), " (an early close)" if early else ""))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock12(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"])
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"])
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s ET" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: earnings, "
                 "news and halts can do things those sessions never did. Same seed, same numbers." % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- swing: multi-day, daily bars
#
# --horizon-days N (or a plan.json with "mode": "swing") replays the next N
# trading days instead of the rest of one session:
#   1. Fetch 5 years of daily bars (Yahoo v8 chart, range=5y interval=1d; one
#      request). Today's bar counts only once the session has closed.
#   2. The pool: the last --window-days daily bars up to the plan's signal day
#      (252, about one year), each as (open, high, low, close) / the previous
#      close. The overnight gap is in the open.
#   3. One path starts at the entry and walks N days. It is built from blocks
#      of --block-days (5) CONSECUTIVE days of the pool: a random start, then
#      the following days in order (circular block bootstrap, so a calm week
#      stays calm and a wild week stays wild). random.Random(seed): same plan,
#      same data, same seed, same numbers.
#   4. Each day, in this order: an open at or under the stop = stopped at the
#      open (a gap: worse than -1R); a low at or under the stop = -1R; an open
#      at or over the target = the target's R (no credit for the gap up); a
#      high at or over the target = the target's R. A day that touches both
#      counts as the stop. Still open after day N = unresolved, closed at day
#      N's close: R = (close - entry) / risk. That is the time stop.
#   5. Expectancy = the average R of all paths.
#   6. History: the same setup on the stock's real daily bars before the
#      signal day (the 5 years), one trade at a time, entry at the signal
#      day's close, same stop rule, target R, horizon and day rules. Fewer than
#      30 cases gets a warning: too few to lean on.
# Verdict: |P(target first) - P(stop first)| under 5 points = close to a coin
# flip. It is a simulation built from the stock's own past, not a prediction.

SWING_RANGE = "5y"
SWING_WINDOW = 252
SWING_BLOCK = 5
SWING_MIN_WINDOW = 60
SWING_MIN_CASES = 30
SWING_SETUPS = ("breakout20", "pullback_ema20", "rsi2")
SWING_PARAMS = {"breakout_days": 20, "ema_days": 20, "trend_days": 50, "ema_touch_pct": 1.0,
                "rsi_days": 2, "rsi_max": 10.0, "rsi_trend_days": 200}
SWING_HOLIDAYS = {  # NYSE full closures, to know when today's daily bar is final
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}


def daily_chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
        host, urllib.parse.quote(sym.upper().replace(".", "-")), SWING_RANGE)


def fetch_daily(sym):
    return fetch_chart(sym, daily_chart_url)


def read_offline_daily(path, sym):
    """A saved Yahoo daily chart (.json or .json.gz), or a folder holding <SYM>-5y1d.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-5y1d.json" % sym.upper(), "%s-5y1d.json.gz" % sym.upper(),
                     "%s-1y1d.json" % sym.upper(), "%s-1y1d.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no hay %s-5y1d.json(.gz) en %s" % (sym.upper(), path))
    return read_offline(path, sym)


def parse_daily(result, now_utc):
    """Yahoo daily chart result -> (meta, bars) oldest first, one bar a date.
    Today's bar is left out until the 4:00 PM close (1:00 PM on a half day)."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close")]
    local = to_eastern(now_utc)
    today = local.date().isoformat()
    close_min = EARLY_MIN if today in NYSE_EARLY_CLOSE else CLOSE_MIN
    open_today = (local.weekday() < 5 and today not in SWING_HOLIDAYS
                  and local.hour * 60 + local.minute < close_min)
    by_date = {}
    for i, ts in enumerate(stamps):
        o, h, l, c = [col[i] if i < len(col) else None for col in cols]
        if ts is None or c is None:
            continue
        d = to_eastern(utc_of(int(ts))).date().isoformat()
        if d > today or (d == today and open_today):
            continue
        o = c if o is None else o
        h = max(c, o) if h is None else h
        l = min(c, o) if l is None else l
        by_date[d] = {"date": d, "o": float(o), "h": float(h), "l": float(l), "c": float(c)}
    return meta, [by_date[d] for d in sorted(by_date)]


# ---- indicators (causal: the value at i uses bars 0..i only)

def ema_series(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def sma_series(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_series(closes, n=2):
    """Wilder's RSI."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def atr_series(bars, n=14):
    """ATR at i = the simple average of the last n true ranges up to bar i (the gap counted)."""
    out = [None] * len(bars)
    trs = [None]
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
        if i >= n:
            out[i] = sum(trs[i - n + 1:i + 1]) / n
    return out


def indicators(bars, params=None):
    p = dict(SWING_PARAMS, **(params or {}))
    closes = [b["c"] for b in bars]
    return {"p": p, "ema": ema_series(closes, p["ema_days"]), "trend": sma_series(closes, p["trend_days"]),
            "rsi": rsi_series(closes, p["rsi_days"]), "rsi_trend": sma_series(closes, p["rsi_trend_days"])}


def setup_fires(setup, bars, i, ind):
    """Does the setup fire at the close of bar i?"""
    p, b = ind["p"], bars[i]
    if setup == "breakout20":
        n = p["breakout_days"]
        return i >= n and b["c"] > max(x["h"] for x in bars[i - n:i])
    if setup == "pullback_ema20":
        e, t = ind["ema"][i], ind["trend"][i]
        e5 = ind["ema"][i - 5] if i >= 5 else None
        if None in (e, t, e5):
            return False
        return (b["c"] > t and e > e5 and b["l"] <= e * (1 + p["ema_touch_pct"] / 100.0) and b["c"] >= e)
    if setup == "rsi2":
        r, t = ind["rsi"][i], ind["rsi_trend"][i]
        return r is not None and t is not None and r < p["rsi_max"] and b["c"] > t
    return True   # "any": every day is an entry, the honest baseline


def walk_days(days, entry, stop, target, risk, r_target):
    """days = [(o, h, l, c)] -> (outcome, R, day, gapped). The stop is checked first."""
    c = entry
    for d, (o, h, l, c) in enumerate(days, 1):
        if o <= stop:
            return "stop", (o - entry) / risk, d, True
        if l <= stop:
            return "stop", -1.0, d, False
        if o >= target:
            return "target", r_target, d, True
        if h >= target:
            return "target", r_target, d, False
    return "open", (c - entry) / risk, len(days), False


def simulate_swing(rets, entry, stop, target, horizon, paths=10000, seed=19, block=SWING_BLOCK):
    """Circular block bootstrap of the daily pool, `paths` paths of `horizon` days."""
    rng = random.Random(seed)
    n = len(rets)
    risk = entry - stop
    r_target = (target - entry) / risk
    count = {"target": 0, "stop": 0, "open": 0}
    rs, gaps, days_to = [], 0, {"target": [], "stop": []}
    for _ in range(paths):
        p, pos, days = entry, 0, []
        for d in range(horizon):
            pos = rng.randrange(n) if d % block == 0 else (pos + 1) % n
            ro, rh, rl, rc = rets[pos]
            days.append((p * ro, p * rh, p * rl, p * rc))
            p = p * rc
        out, r, day, gapped = walk_days(days, entry, stop, target, risk, r_target)
        count[out] += 1
        rs.append(r)
        if out != "open":
            days_to[out].append(day)
            if gapped and out == "stop":
                gaps += 1
    srt = sorted(rs)
    return {
        "paths": paths, "seed": seed, "block_days": block, "horizon_days": horizon, "pool_days": n,
        "p_target": count["target"] / paths, "p_stop": count["stop"] / paths,
        "p_unresolved": count["open"] / paths, "expectancy_R": sum(rs) / paths,
        "r_at_target": round(r_target, 4), "p_gap_stop": gaps / paths,
        "R_p5": pct(srt, 0.05), "R_median": pct(srt, 0.5), "R_p95": pct(srt, 0.95),
        "median_days_to_target": statistics.median(days_to["target"]) if days_to["target"] else None,
        "median_days_to_stop": statistics.median(days_to["stop"]) if days_to["stop"] else None,
    }


def swing_rule(plan):
    """How the history replay places the stop: the plan's ATR multiple, or its distance in percent."""
    rule = {"target_r": plan["target_r"], "horizon_days": plan["horizon_days"]}
    if plan.get("stop_rule") == "atr" and plan.get("stop_param"):
        rule.update(stop="atr", atr_mult=float(plan["stop_param"]), atr_days=int(plan.get("atr_bars") or 14))
    elif plan.get("stop_rule") == "pct" and plan.get("stop_param"):
        rule.update(stop="pct", stop_pct=float(plan["stop_param"]))
    else:
        rule.update(stop="pct", stop_pct=round(100.0 * plan["risk_per_share"] / plan["entry"], 4))
    return rule


def history_swing(bars, upto, setup, rule, params=None):
    """The setup on the real daily bars 0..upto (the signal day), one trade at a time.
    A trade needs its whole horizon inside the data before the signal day."""
    ind = indicators(bars, params)
    atr = atr_series(bars, rule.get("atr_days", 14)) if rule["stop"] == "atr" else None
    hz = rule["horizon_days"]
    count = {"target": 0, "stop": 0, "open": 0}
    rs, trades = [], []
    i, first = 1, None
    while i + hz <= upto:
        if not setup_fires(setup, bars, i, ind):
            i += 1
            continue
        entry = bars[i]["c"]
        if rule["stop"] == "atr":
            if atr[i] is None:
                i += 1
                continue
            stop = math.floor((entry - rule["atr_mult"] * atr[i]) * 100 + 1e-9) / 100.0
        else:
            stop = math.floor(entry * (1 - rule["stop_pct"] / 100.0) * 100 + 1e-9) / 100.0
        risk = entry - stop
        if risk <= 0 or stop <= 0:
            i += 1
            continue
        target = entry + rule["target_r"] * risk
        days = [(b["o"], b["h"], b["l"], b["c"]) for b in bars[i + 1:i + 1 + hz]]
        out, r, d, gapped = walk_days(days, entry, stop, target, risk, rule["target_r"])
        count[out] += 1
        rs.append(r)
        first = first or bars[i]["date"]
        trades.append({"date": bars[i]["date"], "entry": round(entry, 2), "stop": round(stop, 2),
                       "target": round(target, 2), "outcome": out, "R": round(r, 3), "days": d})
        i += d + 1
    n = len(rs)
    return {"setup": setup, "rule": rule, "days_scanned": max(0, upto), "from": bars[0]["date"] if bars else None,
            "to": bars[upto]["date"] if bars else None, "triggered": n, "target": count["target"],
            "stop": count["stop"], "unresolved": count["open"],
            "hit_rate": (count["target"] / n) if n else None, "avg_R": (sum(rs) / n) if n else None,
            "few_cases": n < SWING_MIN_CASES, "trades": trades[-40:]}


def normalize_swing_plan(raw, src, horizon=None):
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        raise TMError("the plan needs a ticker symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "long"):
        raise TMError("el modo swing simula solo largos (compras), no %r" % raw.get("side"))
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-30, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    if not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    hz = horizon if horizon is not None else (raw.get("time_stop_days") or raw.get("horizon_days") or 7)
    try:
        hz = int(hz)
    except (TypeError, ValueError):
        raise TMError("--horizon-days must be a whole number of trading days")
    if not 1 <= hz <= 60:
        raise TMError("--horizon-days must be between 1 and 60 trading days")
    risk = entry - stop
    setup = str(raw.get("setup") or "manual").lower()
    sp = raw.get("stop_param")
    return {"mode": "swing", "symbol": sym, "side": "buy", "date": date, "setup": setup, "entry": entry,
            "stop": stop, "target": target, "risk_per_share": round(risk, 2),
            "target_r": round((target - entry) / risk, 2), "horizon_days": hz,
            "stop_rule": str(raw.get("stop_rule") or "price"), "stop_param": float(sp) if sp else None,
            "atr_bars": raw.get("atr_bars"), "time_stop_date": raw.get("time_stop_date"), "interval": "1d",
            "from": src}


def swing_verdict(mc):
    pt, ps, pu = mc["p_target"], mc["p_stop"], mc["p_unresolved"]
    n = mc["horizon_days"]
    if pt + ps <= 0:
        return "Ninguna trayectoria toco el objetivo ni el stop en %d dias: el cierre del dia %d decide todas." % (n, n)
    d = pt - ps
    if abs(d) < COIN_FLIP:
        head = "Cercano a una moneda al aire"
    elif abs(d) < STRONG:
        head = "Se inclina hacia el objetivo" if d > 0 else "Se inclina hacia el stop"
    else:
        head = "Claramente hacia el objetivo" if d > 0 else "Claramente hacia el stop"
    return "%s: objetivo primero %.1f %% vs stop primero %.1f %% (sin resolver al dia %d: %.1f %%)." % (
        head, 100 * pt, 100 * ps, n, 100 * pu)


def swing_notes(mc, history):
    notes = []
    pt, ps = mc["p_target"], mc["p_stop"]
    r = mc["r_at_target"]
    if pt + ps > 0 and abs(r - 1.0) >= 0.005:
        notes.append("Con un objetivo de %gR alcanza con que el objetivo llegue primero en el %.0f %% de las "
                     "trayectorias que tocan un lado para no perder; aqui llega en el %.0f %%."
                     % (round(r, 2), 100.0 / (1 + r), 100.0 * pt / (pt + ps)))
    ex = mc["expectancy_R"]
    notes.append("Expectativa: %+.2fR por trade (el promedio de las %s trayectorias)." % (ex, "{:,}".format(mc["paths"])))
    lean_d = pt - ps
    if abs(lean_d) >= COIN_FLIP and round(ex, 2) != 0 and (ex > 0) != (lean_d > 0):
        notes.append("Ojo: la expectativa va en contra de la inclinacion; las trayectorias sin resolver y el "
                     "tamano del objetivo (%gR) la empujan hacia el otro lado." % round(r, 2))
    if mc["p_unresolved"] > MANY_NEITHER:
        notes.append("%.1f %% de las trayectorias siguen abiertas al dia %d: el stop de tiempo (el cierre de ese "
                     "dia) decide esas." % (100 * mc["p_unresolved"], mc["horizon_days"]))
    if mc["p_gap_stop"] > 0:
        notes.append("En el %.1f %% de las trayectorias el precio abrio por debajo del stop (gap): la salida es "
                     "peor que -1R." % (100 * mc["p_gap_stop"]))
    if history.get("few_cases"):
        notes.append("Advertencia: el historial real tiene solo %d caso(s) de este setup (menos de %d): muy pocos "
                     "para sacar conclusiones." % (history["triggered"], SWING_MIN_CASES))
    return notes


def swing_time_machine(text, plan, now_utc, paths=10000, seed=19, window=SWING_WINDOW, block=SWING_BLOCK,
                       source="yahoo"):
    if not 1 <= paths <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not SWING_MIN_WINDOW <= window <= 5000:
        raise TMError("--window-days must be between %d and 5,000 daily bars" % SWING_MIN_WINDOW)
    if not 1 <= block <= 60:
        raise TMError("--block-days must be between 1 and 60")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars = parse_daily(result, now_utc)
    msym = str(meta.get("symbol") or "").upper()
    if msym and msym != sym.replace(".", "-"):
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "1d":
        raise TMError("the data has %s bars; the multi-day time machine needs daily bars (1d)" % gran)
    upto = None
    for j, b in enumerate(bars):
        if b["date"] <= plan["date"]:
            upto = j
    if upto is None or upto < 1:
        raise TMError("no closed daily bars of %s on or before %s in the data" % (sym, plan["date"]))
    warn = []
    sig = bars[upto]
    if sig["date"] != plan["date"]:
        warn.append("no hay vela diaria del %s; la ultima antes es del %s" % (plan["date"], sig["date"]))
    if abs(plan["entry"] / sig["c"] - 1) > 0.03:
        warn.append("la entrada %.2f esta a %.1f %% del cierre del %s (%.2f)"
                    % (plan["entry"], 100 * (plan["entry"] / sig["c"] - 1), sig["date"], sig["c"]))
    lo = max(1, upto - window + 1)
    rets = [(bars[j]["o"] / bars[j - 1]["c"], bars[j]["h"] / bars[j - 1]["c"], bars[j]["l"] / bars[j - 1]["c"],
             bars[j]["c"] / bars[j - 1]["c"]) for j in range(lo, upto + 1)]
    if len(rets) < SWING_MIN_WINDOW:
        raise TMError("solo %d dias de %s hasta el %s; la simulacion multi-dia necesita al menos %d"
                      % (len(rets), sym, sig["date"], SWING_MIN_WINDOW))
    if len(rets) < window:
        warn.append("la ventana tiene %d dias, menos que los %d pedidos" % (len(rets), window))
    mc = simulate_swing(rets, plan["entry"], plan["stop"], plan["target"], plan["horizon_days"], paths, seed, block)
    mc.update(window_days=len(rets), window_from=bars[lo]["date"], window_to=sig["date"])
    setup = plan["setup"] if plan["setup"] in SWING_SETUPS else "any"
    history = history_swing(bars, upto, setup, swing_rule(plan))
    if setup == "any":
        history["note"] = ("el setup %r no tiene un detector, asi que el historial usa cada dia como entrada "
                           "(la linea base)" % plan["setup"])
    return {
        "created_by": "trader-timemachine", "mode": "swing",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": source,
        "data": {"symbol": msym or sym, "interval": "1d", "range": SWING_RANGE, "first_bar": bars[0]["date"],
                 "last_bar": bars[-1]["date"]},
        "plan": {k: v for k, v in plan.items() if v is not None},
        "mc": mc, "history": history, "verdict": swing_verdict(mc), "verdict_notes": swing_notes(mc, history),
        "warnings": warn,
        "note": ("Es una simulacion construida con los ultimos %d dias de %s, no una prediccion: resultados "
                 "trimestrales, noticias y gaps pueden hacer lo que ese ano nunca hizo. Misma semilla, mismos numeros."
                 % (len(rets), sym)),
    }


def setup_name(setup):
    return "cualquier dia (linea base)" if setup == "any" else setup


def report_swing(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    n = m["horizon_days"]
    lines = [
        "%s largo (swing), senal del %s: entrada %.2f, stop %.2f, objetivo %.2f (%gR, riesgo %.2f por accion)."
        % (p["symbol"], p["date"], p["entry"], p["stop"], p["target"], p["target_r"], p["risk_per_share"]),
        "%s trayectorias de %d dias habiles: bootstrap de bloques de %d dias sobre los ultimos %d dias de %s "
        "(%s a %s), semilla %d."
        % ("{:,}".format(m["paths"]), n, m["block_days"], m["window_days"], p["symbol"], m["window_from"],
           m["window_to"], m["seed"]),
        "",
        "  objetivo primero      %s" % pc(m["p_target"]),
        "  stop primero          %s" % pc(m["p_stop"]),
        "  sin resolver dia %-3d  %-8s se cierra al cierre del dia %d (stop de tiempo)" % (n, pc(m["p_unresolved"]), n),
        "  expectativa           %+.2fR" % m["expectancy_R"],
        "",
        "  R por trayectoria: mediana %+.2f, 90 %% entre %+.2f y %+.2f." % (m["R_median"], m["R_p5"], m["R_p95"]),
    ]
    dd = []
    if m.get("median_days_to_target") is not None:
        dd.append("al objetivo %g" % m["median_days_to_target"])
    if m.get("median_days_to_stop") is not None:
        dd.append("al stop %g" % m["median_days_to_stop"])
    if dd:
        lines.append("  Dias hasta resolver (mediana): %s." % ", ".join(dd))
    lines.append("")
    ru = h["rule"]
    stop_words = ("%g x ATR(%d)" % (ru["atr_mult"], ru["atr_days"]) if ru["stop"] == "atr"
                  else "%.2f %% bajo la entrada" % ru["stop_pct"])
    if h.get("note"):
        lines.append("Historial: " + h["note"] + ".")
    if h["triggered"]:
        lines.append("Historial real: el setup %s (stop %s, objetivo %gR, %d dias) en las velas diarias de %s a %s "
                     "se disparo %d veces: %d objetivo, %d stop, %d sin resolver. Acierto %.0f %%, expectativa %+.2fR."
                     % (setup_name(h["setup"]), stop_words, ru["target_r"], ru["horizon_days"], h["from"], h["to"],
                        h["triggered"], h["target"], h["stop"], h["unresolved"], 100 * h["hit_rate"], h["avg_R"]))
    else:
        lines.append("Historial real: el setup %s no se disparo nunca de %s a %s."
                     % (setup_name(h["setup"]), h["from"], h["to"]))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Nota: " + w + ".")
    lines.append(doc["note"] + " Repite con --seed %d." % m["seed"])
    return lines


def read_raw_plan(a):
    """(raw plan dict, source) from --plan or the flags, without the intraday checks."""
    if a.symbol:
        return ({"symbol": a.symbol, "side": a.side, "date": a.date, "entry": a.entry, "stop": a.stop,
                 "target": a.target, "setup": a.setup, "stop_rule": None if a.stop_rule == "auto" else a.stop_rule},
                "flags")
    if not os.path.exists(a.plan):
        raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop --target" % a.plan)
    try:
        with open(a.plan, encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as e:
        raise TMError("%s is not valid JSON: %s" % (a.plan, e))
    if not isinstance(raw, dict):
        raise TMError("%s is not a plan" % a.plan)
    return raw, a.plan


def swing_settings(a):
    """--window-days / --block-days, else the config's swing block, else 252 / 5."""
    sw = {}
    path = getattr(a, "config", None)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            sw = doc.get("swing") if isinstance(doc, dict) and isinstance(doc.get("swing"), dict) else {}
        except ValueError:
            raise TMError("%s is not valid JSON" % path)
    window = a.window_days if a.window_days is not None else sw.get("timemachine_window_days", SWING_WINDOW)
    block = a.block_days if a.block_days is not None else sw.get("timemachine_block_days", SWING_BLOCK)
    try:
        return int(window), int(block)
    except (TypeError, ValueError):
        raise TMError("timemachine_window_days and timemachine_block_days must be whole numbers")


def wants_swing(a):
    if a.horizon_days is not None:
        return True
    if a.symbol or not os.path.exists(a.plan):
        return False
    try:
        with open(a.plan, encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError:
        return False
    return isinstance(raw, dict) and (raw.get("mode") == "swing" or raw.get("interval") == "1d")


def compute_swing(a):
    now = parse_now(a.now)
    raw, src = read_raw_plan(a)
    if not raw.get("date"):
        raw = dict(raw, date=to_eastern(now).date().isoformat())
    plan = normalize_swing_plan(raw, src, a.horizon_days)
    window, block = swing_settings(a)
    if a.offline:
        text, source = read_offline_daily(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_daily(plan["symbol"]), "yahoo"
    return swing_time_machine(text, plan, now, a.replays, a.seed, window, block, source)


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"]), ),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    if wants_swing(a):
        return compute_swing(a)
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report_swing(doc) if doc.get("mode") == "swing" else report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    if doc.get("mode") == "swing":
        raise TMError("el fan chart es solo intradia: para un plan swing usa timemachine.py run")
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: ticker")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, New York)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15 AM"')
        p.add_argument("--setup", help="orb (intraday default); breakout20, pullback_ema20, rsi2 or manual (swing)")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default 16:00, the close)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ISO with Z)')
        p.add_argument("--horizon-days", type=int,
                       help="swing: replay the next N trading days on daily bars (default for a swing plan: its "
                            "time_stop_days)")
        p.add_argument("--window-days", type=int, help="swing: daily bars in the bootstrap pool (default 252)")
        p.add_argument("--block-days", type=int, help="swing: consecutive days per bootstrap block (default 5)")
        p.add_argument("--config", default="trader/config.json", help="swing: timemachine_window_days / _block_days")

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
