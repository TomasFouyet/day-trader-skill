"""Performance statistics for research (standard library only): a daily equity curve from a list of
trades, then the Sharpe ratio, the Sortino ratio, the Probabilistic Sharpe Ratio and the Deflated
Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    from performance import portfolio, stats, deflated_sharpe

Trades are walked on a shared calendar of trading days. Each trade risks `risk_pct` of the CURRENT
equity on its entry day (fixed fractional), and is marked to the close every day it is open: its
P&L that day is risk x (change in R). At most `max_positions` trades are open at once and one per
symbol: a signal that finds the book full is skipped. A round-trip cost is charged on the entry.

The Deflated Sharpe Ratio answers: after trying N strategy variants, is the best Sharpe still more
than luck? It is the probability that the true Sharpe is above the highest Sharpe that N variants
with no skill would show by chance, given the sample length, skewness and kurtosis.
"""

import math
import statistics

NORM = statistics.NormalDist()
EULER = 0.5772156649015329


def portfolio(trades, calendar, max_positions=6, risk_pct=1.0, cost_pct=0.1):
    """trades: [{"sym", "start" (index in calendar of the entry day, entry at its close), "path": [R at
    the close of each later day; the last value is the exit R], "cost_R"}] -> (daily returns, n taken).
    The returns cover calendar[1:]: returns[t-1] is the equity change from day t-1 to day t."""
    by_start = {}
    for tr in trades:
        by_start.setdefault(tr["start"], []).append(tr)
    equity = 1.0
    open_ = []          # [sym, path, day k, risk in equity units, last R]
    rets = []
    taken = 0
    for t in range(len(calendar)):
        pnl = 0.0
        still = []
        for pos in open_:
            sym, path, k, risk, last = pos
            r = path[k]
            pnl += risk * (r - last)
            if k + 1 < len(path):
                still.append([sym, path, k + 1, risk, r])
        open_ = still
        if t > 0:
            before = equity
            equity += pnl
            rets.append(equity / before - 1 if before > 0 else 0.0)
        held = {p[0] for p in open_}
        for tr in by_start.get(t, []):
            if len(open_) >= max_positions or tr["sym"] in held or not tr["path"]:
                continue
            risk = equity * risk_pct / 100.0
            equity -= risk * tr.get("cost_R", 0.0) if cost_pct else 0.0
            open_.append([tr["sym"], tr["path"], 0, risk, 0.0])
            held.add(tr["sym"])
            taken += 1
    return rets, taken


def moments(x):
    n = len(x)
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / n
    sd = math.sqrt(var)
    if sd == 0:
        return m, 0.0, 0.0, 3.0
    skew = sum((v - m) ** 3 for v in x) / n / sd ** 3
    kurt = sum((v - m) ** 4 for v in x) / n / sd ** 4
    return m, sd, skew, kurt


def sharpe(x):
    """Per-period Sharpe (mean / standard deviation), no risk-free rate."""
    m, sd, _, _ = moments(x)
    return m / sd if sd else 0.0


def sortino(x, target=0.0):
    """Per-period Sortino: mean over the downside deviation (below `target`, over all periods)."""
    n = len(x)
    dd = math.sqrt(sum(min(0.0, v - target) ** 2 for v in x) / n)
    return (sum(x) / n - target) / dd if dd else 0.0


def psr(sr, n, skew, kurt, sr0=0.0):
    """Probabilistic Sharpe Ratio: P(true per-period Sharpe > sr0)."""
    den = 1 - skew * sr + (kurt - 1) / 4.0 * sr * sr
    if den <= 0 or n < 2:
        return float("nan")
    return NORM.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den))


def expected_max_sharpe(n_trials, var_trials):
    """The highest per-period Sharpe expected among n_trials skill-less variants."""
    if n_trials < 2:
        return 0.0
    return math.sqrt(var_trials) * ((1 - EULER) * NORM.inv_cdf(1 - 1.0 / n_trials)
                                    + EULER * NORM.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(x, n_trials, var_trials):
    """-> (DSR, the luck threshold sr0 per period)."""
    _, _, skew, kurt = moments(x)
    sr0 = expected_max_sharpe(n_trials, var_trials)
    return psr(sharpe(x), len(x), skew, kurt, sr0), sr0


def stats(x, periods=252):
    """Annualized figures of a daily return series."""
    m, sd, skew, kurt = moments(x)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for v in x:
        eq *= 1 + v
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    years = len(x) / float(periods)
    cagr = eq ** (1 / years) - 1 if years > 0 and eq > 0 else float("nan")
    return {"days": len(x), "cagr": cagr, "vol": sd * math.sqrt(periods),
            "sharpe": sharpe(x) * math.sqrt(periods), "sortino": sortino(x) * math.sqrt(periods),
            "sharpe_d": sharpe(x), "skew": skew, "kurt": kurt, "max_dd": mdd, "total": eq - 1,
            "psr0": psr(sharpe(x), len(x), skew, kurt)}


def walk_path(bars, i, horizon, k_atr, atr, target_r=None, cost_pct=0.1):
    """A long entered at the close of bars[i]: [R at each later close], the last one the exit
    (stop checked first, a gap through it fills at the open; target_r None = no target).
    -> (path, cost in R)."""
    e = bars[i]["c"]
    risk = k_atr * atr
    if risk <= 0:
        return [], 0.0
    stop = e - risk
    tgt = e + target_r * risk if target_r else None
    path = []
    for b in bars[i + 1:i + 1 + horizon]:
        if b["o"] <= stop:
            path.append((b["o"] - e) / risk)
            break
        if b["l"] <= stop:
            path.append(-1.0)
            break
        if tgt is not None and b["h"] >= tgt:
            path.append(float(target_r))
            break
        path.append((b["c"] - e) / risk)
    return path, (cost_pct / 100.0) * e / risk


def report(name, x, n_trials=None, var_trials=None):
    s = stats(x)
    line = ("%-34s %5.1f anos  CAGR %+6.1f%%  vol %5.1f%%  Sharpe %5.2f  Sortino %5.2f  maxDD %5.1f%%  "
            "PSR(>0) %5.1f%%" % (name, s["days"] / 252.0, 100 * s["cagr"], 100 * s["vol"], s["sharpe"],
                                 s["sortino"], 100 * s["max_dd"], 100 * s["psr0"]))
    if n_trials:
        dsr, sr0 = deflated_sharpe(x, n_trials, var_trials)
        line += "  DSR %5.1f%% (umbral de suerte: Sharpe %.2f, N=%d)" % (100 * dsr, sr0 * math.sqrt(252), n_trials)
    return line
