#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, take-profit) to trader/orders.json in the
shape of Alpaca's place_stock_order. It never places anything: trader-risk
checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 364.00 --write           # a limit order at 364.00
    python3 size.py --now "2026-10-19 10:15" --json  # pin the clock (New York time)

How many shares. Whole shares only: Alpaca takes no fractions in a bracket.

    shares = floor of the smallest of
      max loss       max_loss_per_trade_usd / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order_usd / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Swing mode: a plan.json with "mode": "swing" is sized as a manual, multi-day
ticket (trader/swing_orders.json, never trader/orders.json): see "swing mode"
below.

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight.

    python3 size.py --weights AAPL=22.04,AXP=17.14,GOOGL=12.62,KO=10.86,BAC=9.20
    python3 size.py --targets my-weights.json --budget 5000 --write
    python3 size.py --weights ... --whole-shares --prices trader/prices.json

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_usd is dropped and its weight
                   shared out, so you never place a $2 order
  5. rounding      whole dollars by default (--cents for cents); the rounding
                   dollar goes to the biggest position that is not capped
  6. whole shares  with --whole-shares or "fractional": false; needs prices

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import sys

# v2 defaults: the contract's config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 4000,
    "min_order_usd": 5,
    "max_loss_per_trade_usd": 50,
    "max_daily_loss_usd": 150,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "fractional": False,
    "broker": "alpaca",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (New York, no tz files)

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
        raise SizeError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": str(r["ticker"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """AAPL=22.04,AXP=17.14 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[str(t).upper()] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_cent(x):
    """Round a price to the cent (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(usd, per_share):
    """Whole shares that fit in usd at per_share, in exact integer cents."""
    u, p = int(round(float(usd) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """358.32 -> "358.32": Alpaca wants prices as strings."""
    return "%.2f" % round_cent(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13" (Alpaca wants these as strings)."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x):
    x = float(x)
    if abs(x - round(x)) < 0.005:
        return "$" + "{:,.0f}".format(round(x))
    return "$" + "{:,.2f}".format(x)


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  New York date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in New York: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        ref, ref_from = round_cent(limit), "limit"
    elif price is not None and round_cent(price) > round_cent(entry_plan):
        ref, ref_from = round_cent(price), "price"
    else:
        ref, ref_from = round_cent(entry_plan), "plan"
    stop, target = round_cent(stop), round_cent(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_usd"))
    if max_loss:
        limits.append({"name": "max loss", "usd": max_loss, "per": risk, "shares": shares_for(max_loss, risk),
                       "how": "%s max loss / %s risk a share" % (money(max_loss), _price(risk))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "usd": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_usd"))
    if max_order:
        limits.append({"name": "max order", "usd": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "usd": available, "per": ref, "shares": shares_for(available, ref),
                   "how": "%s = %s / %s" % (cash_note, money(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss = round(qty * risk_c / 100.0, 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (money(risk), _price(ref), _price(stop), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_usd") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_usd"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_cent(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_usd": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "max_loss": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def bracket_order(s):
    """The one proposed order, Alpaca place_stock_order fields (strings) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "side": "buy", "qty": str(int(s["qty"])), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_usd"] or 0),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], _price(s["risk_per_share"]), money(s["max_loss"])))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-58s %3d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), money(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (place_stock_order arguments, nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s New York: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten)." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) dollars per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: dollars}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole dollars (unit 1) or cents (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(dollars, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(dollars[t] / prices[t] + EPS)) if dollars[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = dollars[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict)."""
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_usd", 5) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not cfg.get("fractional", True)
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        dollars = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(dollars, prices, order, cap, sum(dollars.values()))
            dollars = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if dollars[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs $%s, more than its slice; it cannot be bought in whole shares"
                      % _num(prices[worst["ticker"]]))
        else:
            reason = "its slice ($%s) is under your $%s minimum order" % (
                _num(dollars[worst["ticker"]]), _num(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "dollars": dollars[t],
             "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if whole else round(dollars[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["dollars"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_usd": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: proposed buy orders, one per position, in place_stock_order shape."""
    out = []
    for p in result["positions"]:
        if p["dollars"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        o = {"symbol": p["ticker"], "side": "buy"}
        if result["whole_shares"]:
            o["qty"] = str(int(p["shares"]))
        else:
            o["notional"] = _num(p["dollars"])
        o.update({"type": "market", "time_in_force": "day", "why": why})
        out.append(o)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                    money(r["investable"]), _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-7s %-22s %8s %11s" % ("ticker", "name", "weight", "dollars")
    if has_price:
        head += " %10s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-7s %-22s %7.2f%% %11s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], money(p["dollars"]))
        if has_price:
            row += " %10s %10s" % (money(p["price"]) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-7s %-22s %8s %11s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-7s %-22s %8s %11s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (money(r["idle"]), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (place_stock_order arguments, nothing has been placed):")
    for o in orders:
        amount = ("notional \"%s\"" % o["notional"]) if "notional" in o else ("qty \"%s\"" % o["qty"])
        lines.append("  %s %s %s, %s, %s" % (o["side"], o["symbol"], amount, o["type"], o["time_in_force"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- swing mode (a manual, multi-day ticket)
#
# A plan.json with "mode": "swing" (trader-exit --mode swing) is sized as a
# position you buy BY HAND (Fintual or any app) and hold for days. Nothing
# here talks to a broker: the ticket goes to trader/swing_orders.json, a file
# trader-broker never reads.
#
#   shares = floor of the smallest of
#     max loss       account x swing.max_risk_per_trade_pct / risk per share
#     exposure       account x swing.max_exposure_pct, minus the swing positions you hold, / entry
#     position cap   account x max_position_pct / entry
#     max order      max_order_usd / entry
#     cash           (cash minus the cash buffer) / entry
#   account = --budget, else cash + positions in trader/portfolio.json, else
#   TRADER_BUDGET in the environment (a GitHub Secret), else "budget" in the config. Positions
#   marked "swing": true are the open swing trades (all of them when none is
#   marked). "fractional": true sizes to 0.0001 shares (apps that buy by amount).
#
# Refused, with the reason: max_positions already open, the symbol already
# held, a stale signal (more than 2 trading days old, or past its time stop),
# a reward:risk under min_reward_risk, zero shares, and a plan whose earnings
# check (trader-analyst earnings.py) says BLOCKED.

SWING_LIMITS = {"max_risk_per_trade_pct": 1.0, "max_positions": 5, "max_exposure_pct": 60.0}
SWING_STALE_DAYS = 2
SWING_ORDERS = "trader/swing_orders.json"


def swing_limits(c):
    sw = c.get("swing") if isinstance(c.get("swing"), dict) else {}
    out = dict(SWING_LIMITS)
    for k in out:
        v = _f(sw.get(k))
        if sw.get(k) is not None:
            if v is None or v <= 0:
                raise SizeError("swing.%s must be a number above 0" % k)
            out[k] = v
    out["max_positions"] = int(out["max_positions"])
    return out


def weekdays_between(a, b):
    """Weekdays after a up to and including b."""
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def swing_book(portfolio, prices):
    """(account value or None, cash or None, [open swing positions {symbol, shares, value}])."""
    if not isinstance(portfolio, dict):
        return None, None, []
    cash = _f(portfolio.get("cash"))
    rows = []
    for p in portfolio.get("positions") or []:
        sym = str(p.get("ticker") or p.get("symbol") or "").upper()
        n = _f(p.get("shares", p.get("qty")))
        if not sym or not n:
            continue
        px = (prices or {}).get(sym) or _f(p.get("price")) or _f(p.get("entry"))
        value = n * px if px else (_f(p.get("cost")) or 0.0)
        rows.append({"symbol": sym, "shares": n, "value": round(value, 2), "swing": bool(p.get("swing")),
                     "venue": str(p["venue"]).lower() if p.get("venue") else None, "margin": _f(p.get("margin"))})
    tagged = [r for r in rows if r["swing"]]
    open_ = tagged if tagged else rows
    total = (cash or 0.0) + sum(r["value"] for r in rows) if cash is not None else None
    return total, cash, open_


def shares_frac(usd, per_share, step):
    if per_share <= 0 or usd <= 0:
        return 0
    if step >= 1:
        return shares_for(usd, per_share)
    return math.floor(usd / per_share / step + EPS) * step


VENUE_DEFAULTS = {"capital_pct": 100.0, "max_leverage": 1.0, "short": False, "max_margin_per_trade_pct": 25.0,
                  "maintenance_margin_pct": 0.5, "fee_pct": 0.06, "liq_buffer": 1.5, "symbols": [],
                  "qty_step": 0.001}


def swing_venues(c):
    """The venues from the config's swing.venues, checked. None = the single, unlevered manual account."""
    sw = c.get("swing") if isinstance(c.get("swing"), dict) else {}
    raw = sw.get("venues")
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise SizeError("swing.venues tiene que ser un objeto {\"fintual\": {...}, \"bitunix\": {...}}")
    out, total = {}, 0.0
    for name, v in raw.items():
        if not isinstance(v, dict):
            raise SizeError("swing.venues.%s tiene que ser un objeto" % name)
        d = dict(VENUE_DEFAULTS)
        d.update(v)
        for k in ("capital_pct", "max_leverage", "max_margin_per_trade_pct", "maintenance_margin_pct", "fee_pct",
                  "liq_buffer", "qty_step"):
            d[k] = _f(d[k])
            if d[k] is None or d[k] < 0:
                raise SizeError("swing.venues.%s.%s tiene que ser un numero >= 0" % (name, k))
        if not 1 <= d["max_leverage"] <= 100:
            raise SizeError("swing.venues.%s.max_leverage va de 1 a 100" % name)
        if d["liq_buffer"] < 1:
            raise SizeError("swing.venues.%s.liq_buffer tiene que ser al menos 1" % name)
        d["short"] = bool(d["short"])
        d["symbols"] = [str(x).upper() for x in (d.get("symbols") or [])]
        total += d["capital_pct"]
        out[str(name).lower()] = d
    if total > 100.0 + EPS:
        raise SizeError("la suma de swing.venues.*.capital_pct es %s%%: no puede pasar de 100" % _num(total))
    return out


def lev_sizing(name, v, side, ref, risk, risk_usd, capital, used_margin, sym):
    """A leveraged venue (perpetual futures): the notional the max loss allows, the smallest leverage that
    fits the margin cap, and a liquidation price that stays beyond the stop with a buffer."""
    out = {"venue": name, "leveraged": True, "qty": 0, "refusal": None}
    if side == "sell" and not v["short"]:
        out["refusal"] = "%s no tiene short habilitado (swing.venues.%s.short)" % (name, name)
        return out
    if v["symbols"] and sym not in v["symbols"]:
        out["refusal"] = "%s no esta en la lista de simbolos de %s" % (sym, name)
        return out
    stop_pct = risk / ref
    mmr = v["maintenance_margin_pct"] / 100.0
    l_safe = int(math.floor(1.0 / (stop_pct * v["liq_buffer"] + mmr) + EPS))
    l_max = max(1, min(int(v["max_leverage"]), l_safe))
    free = max(capital - used_margin, 0.0)
    margin_cap = min(capital * v["max_margin_per_trade_pct"] / 100.0, free)
    notional_risk = risk_usd / stop_pct
    if margin_cap <= 0:
        out["refusal"] = "sin margen libre en %s (capital %s, en uso %s)" % (name, money(capital), money(used_margin))
        return out
    need = notional_risk / margin_cap
    if need <= 1 + EPS:
        lev, notional, bound = 1, notional_risk, "max loss"
    elif need <= l_max + EPS:
        lev, notional, bound = int(math.ceil(need - EPS)), notional_risk, "max loss"
    else:
        lev, notional, bound = l_max, margin_cap * l_max, ("margin" if l_max == int(v["max_leverage"]) else "liquidation")
    step = v["qty_step"] or 0.001
    qty = math.floor(notional / ref / step + EPS) * step
    qty = round(qty, 6)
    if qty <= 0:
        out["refusal"] = "ni una unidad cabe en %s" % name
        return out
    notional = round(qty * ref, 2)
    margin = round(notional / lev, 2)
    if side == "sell":
        liq = ref * (1 + 1.0 / lev - mmr)
    else:
        liq = ref * (1 - 1.0 / lev + mmr)
    fees = round(notional * v["fee_pct"] / 100.0 * 2, 2)
    out.update(qty=qty, notional=notional, leverage=lev, max_leverage=int(v["max_leverage"]), safe_leverage=l_safe,
               margin=margin, liquidation=round(max(liq, 0.0), 2), fees=fees, max_loss=round(qty * risk, 2),
               bound_by=bound, capital=round(capital, 2), margin_pct_of_venue=round(100.0 * margin / capital, 1)
               if capital else None, stop_pct=round(100 * stop_pct, 2))
    return out


def cash_sizing(name, c, lim, ref, risk, risk_usd, capital, exposure, cash_now, step):
    """A cash venue (no leverage): the original swing limits on this venue's capital."""
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    buffer_usd = round(capital * buf_pct / 100.0, 2)
    available = max(round(cash_now - buffer_usd, 2), 0.0)
    room = round(capital * lim["max_exposure_pct"] / 100.0 - exposure, 2)
    limits = [
        {"name": "max loss", "usd": risk_usd, "shares": shares_frac(risk_usd, risk, step),
         "how": "%s riesgo maximo / %s de riesgo por accion" % (money(risk_usd), _price(risk))},
        {"name": "exposure", "usd": max(room, 0), "shares": shares_frac(max(room, 0), ref, step),
         "how": "%s%% de %s menos %s ya invertido en swing = %s / %s" % (
             _num(lim["max_exposure_pct"]), money(capital), money(exposure), money(max(room, 0)), _price(ref))},
    ]
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(capital * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "usd": cap, "shares": shares_frac(cap, ref, step),
                       "how": "%s%% de %s = %s / %s" % (_num(cap_pct), money(capital), money(cap), _price(ref))})
    mo = _f(c.get("max_order_usd"))
    if mo:
        limits.append({"name": "max order", "usd": mo, "shares": shares_frac(mo, ref, step),
                       "how": "%s / %s" % (money(mo), _price(ref))})
    limits.append({"name": "cash", "usd": available, "shares": shares_frac(available, ref, step),
                   "how": "caja %s menos el colchon %s = %s / %s" % (money(cash_now), money(buffer_usd),
                                                                    money(available), _price(ref))})
    qty = min(l["shares"] for l in limits)
    qty = round(qty, 4) if step < 1 else int(qty)
    bound = [l["name"] for l in limits if l["shares"] == min(x["shares"] for x in limits)][0]
    for l in limits:
        l["binds"] = l["name"] == bound
    cost = round(qty * ref, 2)
    out = {"venue": name, "leveraged": False, "leverage": 1, "qty": qty, "cost": cost, "notional": cost,
           "margin": cost, "max_loss": round(qty * risk, 2), "limits": limits, "bound_by": bound,
           "capital": round(capital, 2), "exposure_before": exposure,
           "margin_pct_of_venue": round(100.0 * cost / capital, 1) if capital else None, "refusal": None}
    if qty <= 0:
        out["refusal"] = "ni una accion cabe: limita %s (%s)" % (bound, [l["how"] for l in limits if l["binds"]][0])
    elif cost < float(c.get("min_order_usd") or 0) - 0.005:
        out["refusal"] = "%s es menos que tu orden minima %s" % (money(cost), money(c["min_order_usd"]))
    return out


def size_swing(plan, cfg, budget=None, limit=None, portfolio=None, prices=None, today=None, tm=None):
    """Pure: a swing plan + rules + the account -> the sizing (a dict). qty may be 0.

    Long or short. With swing.venues in the config each venue is sized on its share of the account
    (capital_pct): a cash venue (max_leverage 1, e.g. Fintual) with the share limits, a leveraged one
    (e.g. Bitunix perpetuals) with notional, leverage, margin and liquidation price. The max loss per
    trade is always a percent of the WHOLE account: leverage changes the margin, never the risk."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    lim = swing_limits(c)
    venues = swing_venues(c)
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("el plan no tiene symbol (corre trader-exit --mode swing)")
    side = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}.get(str(plan.get("side") or "buy").lower())
    if side is None:
        raise SizeError("side es buy (long) o sell (short), no %r" % plan.get("side"))
    if side == "sell" and not venues:
        raise SizeError("un short necesita un venue apalancado con short habilitado en swing.venues (p. ej. bitunix)")
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("el plan necesita %s positivo (corre trader-exit --mode swing)" % name)
    if sym in {str(b).upper() for b in (c.get("blocked") or [])}:
        raise SizeError("%s esta en tu lista blocked" % sym)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    acct, cash, held = swing_book(portfolio, prices)
    account = float(budget) if budget is not None else (acct if acct else _f(c.get("budget")))
    if not account or account <= 0:
        raise SizeError("sin tamano de cuenta: pon \"budget\" en trader/config.json, --budget, o trader/portfolio.json")
    ref = round_cent(limit) if limit is not None else round_cent(entry_plan)
    stop, target = round_cent(stop), round_cent(target)
    if side == "buy" and not stop < ref < target:
        raise SizeError("un long necesita stop < entrada < objetivo (stop %s, entrada %s, objetivo %s)"
                        % (_price(stop), _price(ref), _price(target)))
    if side == "sell" and not target < ref < stop:
        raise SizeError("un short necesita objetivo < entrada < stop (objetivo %s, entrada %s, stop %s)"
                        % (_price(target), _price(ref), _price(stop)))
    risk = round(abs(ref - stop), 2)
    rr = round(abs(target - ref) / risk, 2)
    risk_usd = round(account * lim["max_risk_per_trade_pct"] / 100.0, 2)
    step = 0.0001 if c.get("fractional") else 1

    sizings = {}
    if not venues:
        exposure = round(sum(r["value"] for r in held), 2)
        cash_now = cash if cash is not None else account - exposure
        sizings["manual"] = cash_sizing("manual", c, lim, ref, risk, risk_usd, account, exposure, cash_now, step)
        primary = "manual"
    else:
        first_cash = next((n for n, v in venues.items() if v["max_leverage"] <= 1), None)
        for name, v in venues.items():
            capital = account * v["capital_pct"] / 100.0
            mine = [r for r in held if (r.get("venue") or first_cash) == name]
            if v["max_leverage"] <= 1:
                exposure = round(sum(r["value"] for r in mine), 2)
                if side == "sell":
                    sizings[name] = {"venue": name, "leveraged": False, "qty": 0,
                                     "refusal": "%s no permite short" % name}
                    continue
                vstep = step if name == "manual" else (0.0001 if v.get("fractional", c.get("fractional")) else 1)
                sizings[name] = cash_sizing(name, c, lim, ref, risk, risk_usd, capital, exposure,
                                            capital - exposure, vstep)
            else:
                used = sum(_f(r.get("margin")) or r["value"] for r in mine)
                sizings[name] = lev_sizing(name, v, side, ref, risk, risk_usd, capital, used, sym)
        order = ([n for n, v in venues.items() if v["max_leverage"] <= 1] +
                 [n for n, v in venues.items() if v["max_leverage"] > 1])
        ok = [n for n in order if not sizings[n]["refusal"] and sizings[n]["qty"]]
        primary = ok[0] if ok else order[0] if side == "buy" else \
            next((n for n in order if venues[n]["max_leverage"] > 1), order[0])
    pv = sizings[primary]
    for v in sizings.values():
        if v.get("margin") is not None:
            v["margin_pct_of_account"] = round(100.0 * v["margin"] / account, 1)
            v["notional_pct_of_account"] = round(100.0 * (v.get("notional") or v["margin"]) / account, 1)

    refusal = None
    t = _date(today)
    pd, ts = _date(plan.get("date")), _date(plan.get("time_stop_date"))
    er = plan.get("earnings") if isinstance(plan.get("earnings"), dict) else None
    if sym in [r["symbol"] for r in held]:
        refusal = "ya tienes %s en la cartera: un solo swing por accion" % sym
    elif len(held) >= lim["max_positions"]:
        refusal = ("ya tienes %d posiciones swing abiertas (%s): tu maximo es %d"
                   % (len(held), ", ".join(r["symbol"] for r in held), lim["max_positions"]))
    elif t and ts and t > ts:
        refusal = "el stop de tiempo del plan (%s) ya paso: es un plan viejo" % ts.isoformat()
    elif t and pd and weekdays_between(pd, t) > SWING_STALE_DAYS:
        refusal = ("la senal es del %s, mas de %d dias habiles atras: vuelve a escanear"
                   % (pd.isoformat(), SWING_STALE_DAYS))
    elif er and er.get("blocked"):
        refusal = "⚠ %s (%s): BLOQUEADO por tu regla block_on_earnings" % (
            er.get("flag") or "resultados", er.get("next") or "fecha desconocida")
    elif pv["refusal"]:
        refusal = pv["refusal"]
    elif _f(c.get("min_reward_risk")) and rr < _f(c["min_reward_risk"]) - EPS:
        refusal = "recompensa:riesgo %.2f bajo tu minimo %s" % (rr, _num(c["min_reward_risk"]))
    tm_info = None
    if isinstance(tm, dict) and tm.get("mode") == "swing":
        tp = tm.get("plan") or {}
        if str(tp.get("symbol", "")).upper() == sym and tp.get("date") == plan.get("date"):
            m, h = tm.get("mc") or {}, tm.get("history") or {}
            tm_info = {k: m.get(k) for k in ("p_target", "p_stop", "p_unresolved", "expectancy_R", "p_win", "horizon_days",
                                              "paths", "seed")}
            tm_info.update(verdict=tm.get("verdict"), history_cases=h.get("triggered"),
                           history_hit_rate=h.get("hit_rate"), history_avg_R=h.get("avg_R"),
                           history_win_rate=h.get("win_rate"),
                           few_cases=h.get("few_cases"))
    exposure_all = round(sum(r["value"] for r in held), 2)
    return {"mode": "swing", "symbol": sym, "side": side, "name": plan.get("name"), "asset": plan.get("asset"),
            "setup": plan.get("setup"),
            "date": plan.get("date"), "entry_plan": round_cent(entry_plan), "entry": ref,
            "order_type": "limit" if limit is not None else "market",
            "stop": stop, "target": target, "risk_per_share": risk, "reward_risk": rr,
            "time_stop_date": plan.get("time_stop_date"), "time_stop_days": plan.get("time_stop_days"),
            "account": round(account, 2), "risk_usd": risk_usd, "exposure_before": exposure_all,
            "open_positions": [r["symbol"] for r in held], "limits_swing": lim,
            "limits": pv.get("limits") or [], "venue": primary, "venues": sizings,
            "qty": pv["qty"], "bound_by": pv.get("bound_by"), "cost": pv.get("cost", pv.get("notional")),
            "notional": pv.get("notional"), "leverage": pv.get("leverage", 1), "margin": pv.get("margin"),
            "liquidation": pv.get("liquidation"), "max_loss": pv.get("max_loss", 0.0),
            "fractional": step < 1, "earnings": er, "timemachine": tm_info, "refusal": refusal}


def _date(x):
    try:
        return datetime.date.fromisoformat(str(x)[:10]) if x else None
    except ValueError:
        return None


def _venue_brief(v):
    keys = ("venue", "leveraged", "qty", "cost", "notional", "leverage", "margin", "liquidation", "fees", "max_loss",
            "bound_by", "capital", "margin_pct_of_venue", "margin_pct_of_account", "notional_pct_of_account",
            "max_leverage", "safe_leverage", "refusal")
    return {k: v.get(k) for k in keys if v.get(k) is not None}


def swing_ticket(s):
    """The manual ticket: what to buy (or short) by hand, where the stop and target go, when to get out."""
    if s["refusal"] or not s["qty"]:
        return None
    word = "acciones" if not s["venues"][s["venue"]].get("leveraged") else "unidades"
    t = {"symbol": s["symbol"], "name": s.get("name"), "asset": s.get("asset"), "side": s["side"], "qty": s["qty"], "type": s["order_type"],
         "entry": s["entry"], "stop": s["stop"], "target": s["target"], "risk_per_share": s["risk_per_share"],
         "reward_risk": s["reward_risk"], "max_loss": s["max_loss"], "cost": s["cost"], "setup": s["setup"],
         "signal_date": s["date"], "time_stop_date": s["time_stop_date"], "time_stop_days": s["time_stop_days"],
         "manual": True, "venue": s["venue"], "leverage": s["leverage"], "margin": s["margin"],
         "notional": s["notional"], "liquidation": s["liquidation"], "account": s["account"],
         "risk_usd": s["risk_usd"], "venues": {k: _venue_brief(v) for k, v in s["venues"].items()},
         "earnings": s["earnings"], "timemachine": s["timemachine"],
         "why": "%s %s %s: %s %s en %s, limita %s (%s x %s = %s en el stop)" % (
             "SHORT" if s["side"] == "sell" else "LONG", s["setup"] or "swing", s["symbol"], _qty(s["qty"]), word,
             s["venue"], s["bound_by"], _qty(s["qty"]), _price(s["risk_per_share"]), money(s["max_loss"]))}
    if s["order_type"] == "limit":
        t["limit_price"] = s["entry"]
    return t


def _qty(q):
    return ("%.4f" % q).rstrip("0").rstrip(".") if isinstance(q, float) else str(q)


def venue_line(v):
    if v.get("refusal"):
        return "  %-8s no: %s" % (v["venue"], v["refusal"])
    if not v.get("leveraged"):
        return ("  %-8s 1x: %s acciones = %s (%s%% de su capital %s), perdida en el stop %s"
                % (v["venue"], _qty(v["qty"]), money(v["cost"]), v.get("margin_pct_of_venue"), money(v["capital"]),
                   money(v["max_loss"])))
    return ("  %-8s %dx: %s unidades, nocional %s, margen %s (%s%% de su capital %s), liquidacion ~%s, "
            "comisiones ~%s, perdida en el stop %s (limita %s)"
            % (v["venue"], v["leverage"], _qty(v["qty"]), money(v["notional"]), money(v["margin"]),
               v.get("margin_pct_of_venue"), money(v["capital"]), _price(v["liquidation"]), money(v["fees"]),
               money(v["max_loss"]), v["bound_by"]))


def swing_report(s, ticket):
    word = "SHORT (venta en corto)" if s["side"] == "sell" else "LONG (compra)"
    out = ["%s %s swing (%s), senal del %s: entrada %s, stop %s, objetivo %s (R:R %.2f)."
           % (s["symbol"], word, s["setup"] or "manual", s["date"], _price(s["entry"]), _price(s["stop"]),
              _price(s["target"]), s["reward_risk"]),
           "Cuenta %s; riesgo maximo por trade %s; ya invertido en swing %s en %d posicion(es)%s." % (
               money(s["account"]), money(s["risk_usd"]), money(s["exposure_before"]), len(s["open_positions"]),
               (" (" + ", ".join(s["open_positions"]) + ")") if s["open_positions"] else "")]
    if s["limits"]:
        out += ["", "Limites en %s (gana el menor):" % s["venue"]]
        for l in s["limits"]:
            out.append("  %-13s %-70s %s%s" % (l["name"], l["how"],
                                              _qty(round(l["shares"], 4) if s["fractional"] else int(l["shares"])),
                                              "  <- limita" if l["binds"] else ""))
    if len(s["venues"]) > 1 or s["venue"] != "manual":
        out += ["", "Por venue:"] + [venue_line(v) for v in s["venues"].values()]
    out.append("")
    if ticket is None:
        out.append("Sin ticket: " + s["refusal"] + ".")
        out.append("Educacion y herramientas, no asesoria financiera.")
        return "\n".join(out)
    v = s["venues"][s["venue"]]
    if v.get("leveraged"):
        out.append("Sugerido en %s: %s unidades a %dx, nocional %s, margen %s. Liquidacion ~%s. Perdida maxima en "
                   "el stop: %s." % (s["venue"], _qty(s["qty"]), s["leverage"], money(s["notional"]), money(s["margin"]),
                                     _price(s["liquidation"]), money(s["max_loss"])))
    else:
        out.append("Tamano sugerido en %s: %s acciones de %s = %s. Perdida maxima en el stop: %s."
                   % (s["venue"], _qty(s["qty"]), s["symbol"], money(s["cost"]), money(s["max_loss"])))
    out.append("Stop de tiempo: si sigue abierta, cierra al cierre del %s." % s["time_stop_date"])
    if s["timemachine"]:
        m = s["timemachine"]
        out.append("Time Machine: objetivo primero %.1f %%, stop primero %.1f %%, sin resolver %.1f %%, "
                   "expectativa %+.2fR." % (100 * m["p_target"], 100 * m["p_stop"], 100 * m["p_unresolved"],
                                            m["expectancy_R"]))
    if s["earnings"] and s["earnings"].get("flag"):
        out.append("\u26a0 %s (%s): aviso." % (s["earnings"]["flag"], s["earnings"].get("next")))
    out.append("Es un ticket para operar A MANO: nada se coloca. Siguiente: trader-risk check.py --swing.")
    out.append("Educacion y herramientas, no asesoria financiera.")
    return "\n".join(out)


def write_swing(path, ticket, cfg, now_utc, append=True):
    doc = read_json(path, "swing orders") if (append and os.path.exists(path)) else None
    tickets = []
    if isinstance(doc, dict) and isinstance(doc.get("tickets"), list):
        tickets = [t for t in doc["tickets"] if t.get("symbol") != ticket["symbol"]
                   and t.get("signal_date") == ticket["signal_date"]]
    tickets.append(ticket)
    out = {"created_by": "trader-budget", "mode": "swing", "created_at": utc_iso(now_utc), "status": "proposed",
           "manual": True,
           "note": "Tickets para comprar a mano. Nada se coloca: trader-risk los revisa y tu decides.",
           "tickets": tickets}
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    return len(tickets)


def env_budget():
    """TRADER_BUDGET: the account size from the environment (a GitHub Secret), so it never sits in a file."""
    v = os.environ.get("TRADER_BUDGET")
    if not v:
        return None
    b = _f(v.replace(",", "").replace("$", ""))
    if not b or b <= 0:
        raise SizeError("TRADER_BUDGET must be a number above 0")
    return b


def run_swing(a, cfg, plan):
    now = parse_now(a.now)
    today = to_eastern(now).date().isoformat()
    limit = None
    if a.limit is not None:
        limit = _f(plan.get("entry")) if a.limit == "plan" else _f(a.limit)
        if limit is None or limit <= 0:
            raise SizeError("--limit wants a price, like --limit 364.00")
    portfolio = read_json(a.portfolio, "portfolio") if a.portfolio and os.path.exists(a.portfolio) else None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    prices = load_prices(ppath) if ppath else {}
    tm = read_json(a.timemachine, "time machine") if a.timemachine and os.path.exists(a.timemachine) else None
    budget = a.budget if a.budget is not None else (env_budget() if portfolio is None else None)
    s = size_swing(plan, cfg, budget, limit, portfolio, prices, today, tm)
    ticket = swing_ticket(s)
    n = None
    if ticket and a.write:
        path = SWING_ORDERS if a.write == "trader/orders.json" else a.write
        n = write_swing(path, ticket, cfg, now, append=not a.fresh)
    if a.json:
        print(json.dumps({"sizing": s, "tickets": [ticket] if ticket else []}, indent=2))
    else:
        print(swing_report(s, ticket))
        if n:
            print("Guardado en %s (%d ticket(s) de esta senal)." % (
                SWING_ORDERS if a.write == "trader/orders.json" else a.write, n))
    return 0 if ticket else 1


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_eastern(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    if isinstance(plan, dict) and plan.get("mode") == "swing":
        return run_swing(a, cfg, plan)
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 364.00")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_cent(p) > round_cent(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s New York) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_eastern(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_eastern(t).strftime("%Y-%m-%d %H:%M") + " New York")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed bracket order to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, a.cents, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this New York time, e.g. \"2026-10-19 10:15\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    ap.add_argument("--timemachine", default="trader/timemachine.json",
                    help="swing: attach the Time Machine odds to the ticket when they are for this plan")
    ap.add_argument("--fresh", action="store_true", help="swing: start trader/swing_orders.json over")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: AAPL=22.04,AXP=17.14")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares only (needs --prices)")
    g.add_argument("--cents", action="store_true", help="size to the cent instead of whole dollars")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares or a.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
