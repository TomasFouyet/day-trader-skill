#!/usr/bin/env python3
"""Box breakout (opening range) on 5 minute bars. Research only, never orders.

The rules, as given:
  1. The box: the highest high and the lowest low of the 5 minute bars from 09:00 to 10:30 New York
     (pre-market included).
  2. After 10:30, a bar that CLOSES above the box is a long, one that closes below is a short.
  3. Entry at that close. Stop at the other edge of the box. Target at 3R.
  4. Filter: longs only while the 50 EMA is above the 200 EMA on 15 minute bars, shorts only while it is
     below. The 15 minute EMAs are the ones known when the 5 minute bar closes (completed 15 minute bars
     only, no look-ahead).

Choices the rules leave open (switches in run()):
  - one trade a day, and none while an earlier one is still open (hold_days); by default the first close outside the box in the direction the filter allows
    (a break the other way before it is ignored). first_only=True takes only the day's first close
    outside the box and skips the day when the filter is against it.
  - the trade is closed at the 16:00 close if neither stop nor target was hit (day trading);
    hold_days=N keeps it, overnight bars included, up to the 16:00 close N trading days later.
  - a bar that touches stop and target is counted as a stop; a bar that opens past the stop or the
    target exits at that open.
  - entry="retest": the breakout close (in the filter's direction) arms a limit order at the box edge it
    broke. It fills on a later bar that trades back to that edge (at the edge, or at a better open), up to
    the last entry time, else it is cancelled. Same stop, target in R from the fill. In the fill bar only the
    stop is checked. overlap=True drops the one-position-at-a-time rule.
"""

import datetime

DEFAULTS = {
    "box_start": "09:00", "box_end": "10:30", "last_entry": "15:55", "flat": "16:00",
    "target_r": 3.0, "ema_fast": 50, "ema_slow": 200, "filter": "trend",   # trend | none | counter
    "first_only": False, "min_box_bars": 9, "bar_minutes": 5, "sides": "both",
    "hold_days": 0,      # 0: out at the 16:00 close; N: keep it (overnight too) up to the 16:00 close N days later
    "entry": "close",    # close: at the close of the breakout bar | retest: limit order at the box edge after it
    "overlap": False,    # True: a new day's trade may open while an earlier one on the same symbol is still open
}


def _minutes(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _hm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def ny_bars(series, to_eastern):
    """A Yahoo-shaped series (lists t/o/h/l/c/v, Unix seconds) -> bars with New York start times."""
    out = []
    t, o, h, l, c = series["t"], series.get("o"), series.get("h"), series.get("l"), series["c"]
    v = series.get("v") or [0] * len(t)
    for k in range(len(t)):
        if None in (o[k], h[k], l[k], c[k]):
            continue
        out.append({"t": to_eastern(t[k]), "o": o[k], "h": h[k], "l": l[k], "c": c[k], "v": v[k] or 0})
    out.sort(key=lambda b: b["t"])
    return clean_spikes(out)


def clean_spikes(bars, limit=0.02):
    """A lone bad tick (seen in Yahoo's futures: a 15:00 bar opening 4% away from the bars on both sides of
    it): an open or extreme more than `limit` away from both the previous close and the bar's own close is
    pulled back to the bar's other prices."""
    for k in range(1, len(bars)):
        b, pc = bars[k], bars[k - 1]["c"]

        def far(x):
            return abs(x - pc) / pc > limit and abs(x - b["c"]) / b["c"] > limit
        if far(b["o"]):
            b["o"] = pc
        if far(b["h"]):
            b["h"] = max(b["o"], b["c"])
        if far(b["l"]):
            b["l"] = min(b["o"], b["c"])
    return bars


def ema_15m(bars, fast=50, slow=200, bar_minutes=5, span=15):
    """For each bar: (ema_fast, ema_slow, completed 15 minute bars so far) as known at the bar's close.

    A 15 minute bar is complete at the close of its last 5 minute bar, or when a bar of a later 15 minute
    period arrives (a gap). EMAs start from the first close, like TradingView's ta.ema."""
    af, aslow = 2.0 / (fast + 1), 2.0 / (slow + 1)
    st = {"f": None, "s": None, "n": 0}
    out = []
    key = close = None
    pending = False

    def done(c):
        st["f"] = c if st["f"] is None else st["f"] + af * (c - st["f"])
        st["s"] = c if st["s"] is None else st["s"] + aslow * (c - st["s"])
        st["n"] += 1

    for b in bars:
        t = b["t"]
        k = t.replace(minute=t.minute // span * span, second=0, microsecond=0)
        if pending and k != key:
            done(close)
            pending = False
        key, close, pending = k, b["c"], True
        if t + datetime.timedelta(minutes=bar_minutes) >= k + datetime.timedelta(minutes=span):
            done(close)
            pending = False
        out.append((st["f"], st["s"], st["n"]))
    return out


def by_day(bars):
    days, order = {}, []
    for i, b in enumerate(bars):
        d = b["t"].date()
        if d not in days:
            days[d] = []
            order.append(d)
        days[d].append(i)
    return [(d, days[d]) for d in order]


def daily_ranges(bars, days):
    """{day: average high-low of the 09:30-16:00 session over the 14 previous days} (None at the start)."""
    rng, out, hist = {}, {}, []
    for d, idx in days:
        reg = [bars[i] for i in idx if datetime.time(9, 30) <= bars[i]["t"].time() < datetime.time(16, 0)]
        out[d] = sum(hist[-14:]) / 14.0 if len(hist) >= 14 else None
        if reg:
            hist.append(max(b["h"] for b in reg) - min(b["l"] for b in reg))
    return out


def simulate(bars, i, d, entry, stop, target_r, session_end_idx):
    """Walk the bars after i up to session_end_idx (inclusive). -> (R, outcome, bars held)."""
    risk = (entry - stop) * d
    tgt = entry + d * target_r * risk
    for j in range(i + 1, session_end_idx + 1):
        b = bars[j]
        o, hi, lo = b["o"], b["h"], b["l"]
        if d > 0:
            if o <= stop:
                return (o - entry) / risk, "stop", j - i
            if lo <= stop:
                return -1.0, "stop", j - i
            if o >= tgt:
                return (o - entry) / risk, "target", j - i
            if hi >= tgt:
                return target_r, "target", j - i
        else:
            if o >= stop:
                return (entry - o) / risk, "stop", j - i
            if hi >= stop:
                return -1.0, "stop", j - i
            if o <= tgt:
                return (entry - o) / risk, "target", j - i
            if lo <= tgt:
                return target_r, "target", j - i
    last = bars[session_end_idx]
    return (last["c"] - entry) * d / risk, "close", session_end_idx - i


def boxes(bars, p=None):
    """[(day, box_high, box_low, session indices from box_end to flat)] for days with a full box."""
    p = dict(DEFAULTS, **(p or {}))
    b0, b1, flat = _hm(p["box_start"]), _hm(p["box_end"]), _hm(p["flat"])
    out = []
    for d, idx in by_day(bars):
        box = [i for i in idx if b0 <= bars[i]["t"].time() < b1]
        sess = [i for i in idx if b1 <= bars[i]["t"].time() < flat]
        if len(box) < p["min_box_bars"] or not sess:
            continue
        out.append((d, max(bars[i]["h"] for i in box), min(bars[i]["l"] for i in box), sess))
    return out


def end_index(bars, days_list, day_pos, sess_last, hold_days, flat):
    """Index of the last bar the trade may live through."""
    if not hold_days:
        return sess_last
    k = min(len(days_list) - 1, day_pos + hold_days)
    idx = days_list[k][1]
    last = [i for i in idx if bars[i]["t"].time() < flat]
    return last[-1] if last else idx[-1]


def retest_fill(bars, i, k, level, stop, sess, last_entry):
    """The limit order at the broken edge, armed at the close of bar i. -> (fill bar, fill price) or None.
    A bar that opens past the edge (toward the stop side) fills at its open, unless it opens past the stop."""
    for j in sess:
        if j <= i:
            continue
        b = bars[j]
        if b["t"].time() > last_entry:
            return None
        if k > 0 and b["l"] <= level:
            px = min(level, b["o"])
            return None if px <= stop else (j, px)
        if k < 0 and b["h"] >= level:
            px = max(level, b["o"])
            return None if px >= stop else (j, px)
    return None


def run(bars, p=None, ema=None):
    """All trades of the rules on these bars. -> list of trade dicts (R before costs)."""
    p = dict(DEFAULTS, **(p or {}))
    ema = ema or ema_15m(bars, p["ema_fast"], p["ema_slow"], p["bar_minutes"])
    last_entry = _hm(p["last_entry"])
    days = by_day(bars)
    pos = {d: k for k, (d, _) in enumerate(days)}
    flat = _hm(p["flat"])
    avg_rng = daily_ranges(bars, days)
    trades = []
    busy = -1                      # index of the bar where the open trade ends: one position at a time
    for d, hi, lo, sess in boxes(bars, p):
        first_dir, pick = None, None
        for i in sess:
            if bars[i]["t"].time() > last_entry:
                break
            if i <= busy:
                continue
            c = bars[i]["c"]
            k = 1 if c > hi else -1 if c < lo else 0
            if not k:
                continue
            if first_dir is None:
                first_dir = k
            f, s, n = ema[i]
            trend = (1 if f > s else -1 if f < s else 0) if n >= p["ema_slow"] else None
            ok = {"trend": trend == k, "counter": trend == -k, "none": True}[p["filter"]]
            if p["sides"] == "long" and k < 0 or p["sides"] == "short" and k > 0:
                ok = False
            if ok:
                pick = (i, k, trend, f, s)
                break
            if p["first_only"]:
                break
        if pick is None:
            continue
        i, k, trend, f, s = pick
        stop = lo if k > 0 else hi
        end = end_index(bars, days, pos[d], sess[-1], p["hold_days"], flat)
        if p["entry"] == "retest":
            got = retest_fill(bars, i, k, hi if k > 0 else lo, stop, sess, last_entry)
            if got is None:
                continue
            i, entry = got
            b = bars[i]
            if (k > 0 and b["l"] <= stop) or (k < 0 and b["h"] >= stop):
                r, outcome, held = -1.0, "stop", 0
            else:
                r, outcome, held = simulate(bars, i, k, entry, stop, p["target_r"], end)
        else:
            entry = bars[i]["c"]
            r, outcome, held = simulate(bars, i, k, entry, stop, p["target_r"], end)
        if not p["overlap"]:
            busy = i + held
        t = bars[i]["t"]
        box_h = hi - lo
        trades.append({
            "day": d.isoformat(), "entry_time": t.strftime("%Y-%m-%d %H:%M"), "dir": k, "entry": entry,
            "exit_time": bars[i + held]["t"].strftime("%Y-%m-%d %H:%M"),
            "stop": stop, "R": r, "outcome": outcome, "bars": held,
            "risk_pct": abs(entry - stop) / entry * 100, "box_pct": box_h / entry * 100,
            "features": {
                "hour": t.hour + t.minute / 60.0, "dow": t.weekday(),
                "ext_box": (entry - hi if k > 0 else lo - entry) / box_h if box_h else 0.0,
                "box_vs_range": box_h / avg_rng[d] if avg_rng.get(d) else None,
                "first_against": first_dir == -k,
                "ema_gap_pct": abs(f - s) / entry * 100 if f is not None and s is not None else None,
            }})
    return trades


def random_trades(bars, p=None, ema=None, rng=None, per_day=1):
    """Baseline: on each day with a box, enter at a random bar of the entry window in the direction the 15m
    filter allows (or a random one with filter none), with the box height as the stop distance, the same
    target and the same 16:00 close."""
    import random as _r
    p = dict(DEFAULTS, **(p or {}))
    rng = rng or _r.Random(7)
    ema = ema or ema_15m(bars, p["ema_fast"], p["ema_slow"], p["bar_minutes"])
    last_entry = _hm(p["last_entry"])
    days = by_day(bars)
    pos = {d: k for k, (d, _) in enumerate(days)}
    flat = _hm(p["flat"])
    out = []
    for d, hi, lo, sess in boxes(bars, p):
        win = [i for i in sess if bars[i]["t"].time() <= last_entry]
        if not win or hi <= lo:
            continue
        for _ in range(per_day):
            i = rng.choice(win)
            f, s, n = ema[i]
            if p["filter"] == "none":
                k = rng.choice((1, -1))
            else:
                if n < p["ema_slow"] or f == s:
                    continue
                k = 1 if f > s else -1
                if p["filter"] == "counter":
                    k = -k
            if p["sides"] == "long" and k < 0 or p["sides"] == "short" and k > 0:
                continue
            e = bars[i]["c"]
            r, outcome, held = simulate(bars, i, k, e, e - k * (hi - lo), p["target_r"],
                                        end_index(bars, days, pos[d], sess[-1], p["hold_days"], flat))
            out.append({"day": d.isoformat(), "R": r, "dir": k, "outcome": outcome, "risk_pct": (hi - lo) / e * 100})
    return out
