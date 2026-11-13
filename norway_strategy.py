#!/usr/bin/env python3
"""
NORWAY strategy: one set of functions for the backtester, the optimizer and the live paper trader.

Universe  pump.fun tokens that graduated to PumpSwap (seeded with real SOL liquidity, LP burned, mint + freeze
          authority revoked by the launchpad itself, so the classic honeypot is not possible in this universe).
Candles   5 minutes, on a regular grid starting at the token's first trade (empty candles are forward-filled).
Entry     see signal_mask(): a survivor that breaks out of its own range on real volume.
Exit      see on_bar(): hard stop, trailing stop on closes, optional partial take-profit, time stop.

Nothing in here looks at a candle that is not closed yet.
"""
import math

import numpy as np

BAR = 300                       # seconds
CRASH = 0.75                    # a candle that closes below stop * CRASH is a rug / dump candle: the stop order is
                                # assumed to be filled at that close, not at the stop price
SUPPLY = 1_000_000_000          # every pump.fun token has a fixed supply of 1 billion

DEFAULT_PARAMS = {
    "age_min_h": 8.0,           # hours since graduation before the bot is allowed to buy
    "age_max_h": 168.0,         # ... and after which it loses interest
    "mcap_min": 150_000.0,      # market cap window in USD
    "mcap_max": 5_000_000.0,
    "brk_n": 24,                # breakout: close above the highest high of the previous brk_n candles
    "vol_n": 12,                # volume window (candles)
    "vol_min": 10_000.0,        # USD traded inside the volume window
    "act_min": 0.75,            # share of the last vol_n candles that had at least one trade
    "max_ext": 2.5,             # do not chase: close / lowest low of the last brk_n candles must stay below this
    "turn_min": 2.0,            # organic flow: volume in the window must be at least this % of the market cap
    "jump_max": 3.0,            # bundle filter: skip tokens whose price was pushed above this multiple of the
                                # graduation price within the first 10 minutes (one wallet buying the pool)
    "stop_pct": 25.0,           # hard stop below the entry price
    "trail_pct": 25.0,          # trailing stop below the highest close since entry
    "tp_r": 0.0,                # partial take-profit at this many R (0 = off)
    "tp_frac": 0.5,             # share of the position sold at the take-profit
    "max_hold_h": 48.0,         # time stop
    "cooldown_h": 6.0,          # no new entry in the same token for this long after an exit
    "dead_mcap": 15_000.0,      # a token that closes below this market cap (after its first hour) is dropped for good
}

PARAM_LIMITS = {                # (low, high, type) - used by the optimizer and to validate website input
    "age_min_h": (0.0, 48.0, float), "age_max_h": (2.0, 336.0, float),
    "mcap_min": (20_000.0, 2_000_000.0, float), "mcap_max": (100_000.0, 100_000_000.0, float),
    "brk_n": (3, 288, int), "vol_n": (1, 72, int), "vol_min": (0.0, 500_000.0, float),
    "act_min": (0.0, 1.0, float), "max_ext": (1.1, 50.0, float),
    "turn_min": (0.0, 100.0, float), "jump_max": (1.2, 100000.0, float),
    "stop_pct": (3.0, 60.0, float), "trail_pct": (5.0, 80.0, float),
    "tp_r": (0.0, 20.0, float), "tp_frac": (0.1, 0.9, float),
    "max_hold_h": (0.5, 336.0, float), "cooldown_h": (0.0, 72.0, float), "dead_mcap": (0.0, 100_000.0, float),
}

# PumpSwap fee per swap by market cap in SOL (creator + protocol + LP), canonical pump.fun pools
FEE_TIERS = [(420, 1.25), (1470, 1.20), (2460, 1.15), (3440, 1.10), (4420, 1.05), (9820, 1.00), (14740, 0.95),
             (19650, 0.90), (24560, 0.85), (29470, 0.80), (34380, 0.75), (39300, 0.70), (44210, 0.65), (49120, 0.60),
             (54030, 0.55), (58940, 0.525), (63860, 0.50), (68770, 0.475), (73681, 0.45), (78590, 0.425),
             (83500, 0.40), (88400, 0.375), (93330, 0.35), (98240, 0.325), (float("inf"), 0.30)]


def clean_params(raw, base=None):
    p = dict(base or DEFAULT_PARAMS)
    for k, (lo, hi, typ) in PARAM_LIMITS.items():
        if raw and k in raw and raw[k] is not None:
            try:
                v = float(raw[k])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v):
                continue
            v = min(hi, max(lo, v))
            p[k] = int(round(v)) if typ is int else float(v)
    if p["age_max_h"] <= p["age_min_h"]:
        p["age_max_h"] = p["age_min_h"] + 1.0
    if p["mcap_max"] <= p["mcap_min"]:
        p["mcap_max"] = p["mcap_min"] * 2
    return p


# ---------------------------------------------------------------- costs
def dex_fee_pct(mcap_usd, sol_usd):
    m = mcap_usd / sol_usd if sol_usd > 0 else 0.0
    for top, fee in FEE_TIERS:
        if m < top:
            return fee
    return 0.30


def pool_quote_usd(price, p0, seed_usd):
    """USD value of the SOL side of a constant-product pool that was seeded with seed_usd at price p0."""
    if p0 <= 0 or price <= 0:
        return max(seed_usd, 1.0)
    return max(seed_usd * math.sqrt(price / p0), 1.0)


def fill_cost(price, size_usd, fees, sol_usd, p0, seed_usd, stop=False):
    """-> (percent of the notional lost on this fill, fixed USD network cost).
    DEX fee tier + aggregator fee + slippage + price impact of our own order in the pool."""
    mcap = price * SUPPLY
    pct = dex_fee_pct(mcap, sol_usd) + fees["aggregator_bps"] / 100.0
    slip = fees["slippage_pct"] + (fees["stop_slippage_pct"] if stop else 0.0)
    impact = size_usd / pool_quote_usd(price, p0, seed_usd) * 100.0
    return pct, slip + impact, fees["network_usd"]


# ---------------------------------------------------------------- candles
def regularize(rows):
    """[[t,o,h,l,c,v], ...] (sparse, ascending) -> dict of numpy arrays on a gap-free 5m grid."""
    a = np.asarray(rows, dtype=float)
    t = a[:, 0].astype(np.int64)
    g0 = int(t[0])
    idx = ((t - g0) // BAR).astype(int)
    n = int(idx[-1]) + 1
    O = np.full(n, np.nan)
    H, L, C, V = O.copy(), O.copy(), O.copy(), np.zeros(n)
    O[idx], H[idx], L[idx], C[idx], V[idx] = a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]
    last = np.where(~np.isnan(C), np.arange(n), 0)
    np.maximum.accumulate(last, out=last)
    fill = np.isnan(C)
    C = C[last]
    O[fill], H[fill], L[fill] = C[fill], C[fill], C[fill]
    return {"T": g0 + BAR * np.arange(n, dtype=np.int64), "O": O, "H": H, "L": L, "C": C, "V": V}


def sanitize(tok, wick=3.0):
    """Bad ticks: a wick more than `wick` times beyond the candle body is cut back to that limit."""
    hi_b, lo_b = np.maximum(tok["O"], tok["C"]), np.minimum(tok["O"], tok["C"])
    tok["H"] = np.minimum(tok["H"], hi_b * wick)
    tok["L"] = np.maximum(tok["L"], lo_b / wick)
    return tok


def _roll_max_prev(a, n):
    """max of the previous n values (excluding the current one); nan until n values exist"""
    out = np.full(len(a), np.nan)
    if len(a) > n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n:] = w.max(axis=1)[: len(a) - n]
    return out


def _roll_min_prev(a, n):
    out = np.full(len(a), np.nan)
    if len(a) > n:
        w = np.lib.stride_tricks.sliding_window_view(a, n)
        out[n:] = w.min(axis=1)[: len(a) - n]
    return out


def _roll_sum(a, n):
    c = np.cumsum(np.insert(a, 0, 0.0))
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        out[n - 1:] = c[n:] - c[:-n]
    return out


def indicators(tok, p, cache=None):
    """cache: optional dict that lives as long as the token's candles do (the optimizer reuses rolling windows)"""
    C, H, L, V, T = tok["C"], tok["H"], tok["L"], tok["V"], tok["T"]

    def memo(key, fn):
        if cache is None:
            return fn()
        if key not in cache:
            cache[key] = fn()
        return cache[key]

    age = memo("age", lambda: (T + BAR - tok["t0"]) / 3600.0)
    mcap = memo("mcap", lambda: C * SUPPLY)
    return {
        "hh": memo(("hh", p["brk_n"]), lambda: _roll_max_prev(H, p["brk_n"])),
        "ll": memo(("ll", p["brk_n"]), lambda: _roll_min_prev(L, p["brk_n"])),
        "vol": memo(("vol", p["vol_n"]), lambda: _roll_sum(V, p["vol_n"])),
        "act": memo(("act", p["vol_n"]), lambda: _roll_sum((V > 0).astype(float), p["vol_n"]) / p["vol_n"]),
        "mcap": mcap, "age_h": age,
        "dead": memo(("dead", p["dead_mcap"]), lambda: np.maximum.accumulate((age >= 1.0) & (mcap < p["dead_mcap"]))),
        "jump": memo("jump", lambda: float(np.max(H[:2])) / tok["p0"] if tok.get("p0") else 1.0),
    }


def signal_mask(tok, ind, p):
    """True at candle i = buy at the open of candle i+1."""
    C = tok["C"]
    with np.errstate(invalid="ignore", divide="ignore"):
        m = ((ind["age_h"] >= p["age_min_h"]) & (ind["age_h"] <= p["age_max_h"])
             & (ind["mcap"] >= p["mcap_min"]) & (ind["mcap"] <= p["mcap_max"])
             & (C > ind["hh"])
             & (ind["vol"] >= p["vol_min"]) & (ind["act"] >= p["act_min"])
             & (C / ind["ll"] <= p["max_ext"]) & ~ind["dead"]
             & (ind["vol"] >= ind["mcap"] * p["turn_min"] / 100.0))
        if ind["jump"] > p["jump_max"]:
            m = m & False
    return np.nan_to_num(m, nan=0).astype(bool)


def checks(tok, ind, i, p):
    """The same conditions as signal_mask for candle i, itemised for the dashboard."""
    c = float(tok["C"][i])
    f = lambda x: None if x is None or not math.isfinite(float(x)) else float(x)
    age, mc, hh, ll = f(ind["age_h"][i]), f(ind["mcap"][i]), f(ind["hh"][i]), f(ind["ll"][i])
    vol, act = f(ind["vol"][i]), f(ind["act"][i])
    ext = c / ll if ll else None
    pr = lambda x: 0.0 if x is None else max(0.0, min(1.0, x))
    return [
        {"k": "age", "name": "Survivor age", "ok": age is not None and p["age_min_h"] <= age <= p["age_max_h"],
         "val": age, "need": [p["age_min_h"], p["age_max_h"]], "unit": "h", "prog": pr((age or 0) / max(p["age_min_h"], 1e-9))},
        {"k": "mcap", "name": "Market cap window", "ok": mc is not None and p["mcap_min"] <= mc <= p["mcap_max"],
         "val": mc, "need": [p["mcap_min"], p["mcap_max"]], "unit": "$", "prog": pr((mc or 0) / p["mcap_min"])},
        {"k": "brk", "name": f"Close above {p['brk_n']}-candle high", "ok": hh is not None and c > hh,
         "val": c, "need": [hh], "unit": "px", "prog": pr(c / hh if hh else 0)},
        {"k": "vol", "name": f"Volume last {p['vol_n'] * 5} min", "ok": vol is not None and vol >= p["vol_min"],
         "val": vol, "need": [p["vol_min"]], "unit": "$", "prog": pr((vol or 0) / max(p["vol_min"], 1e-9))},
        {"k": "act", "name": "Continuous trading", "ok": act is not None and act >= p["act_min"],
         "val": act, "need": [p["act_min"]], "unit": "x", "prog": pr((act or 0) / max(p["act_min"], 1e-9))},
        {"k": "ext", "name": "Not over-extended", "ok": ext is not None and ext <= p["max_ext"],
         "val": ext, "need": [p["max_ext"]], "unit": "x", "prog": pr(1.0 if ext is not None and ext <= p["max_ext"] else 0.0)},
        {"k": "turn", "name": "Organic turnover", "ok": vol is not None and mc is not None and vol >= mc * p["turn_min"] / 100.0,
         "val": (vol / mc * 100.0) if vol is not None and mc else None, "need": [p["turn_min"]], "unit": "%",
         "prog": pr((vol / mc * 100.0) / max(p["turn_min"], 1e-9) if vol is not None and mc else 0)},
        {"k": "jump", "name": "No bundle pump at migration", "ok": ind["jump"] <= p["jump_max"],
         "val": float(ind["jump"]), "need": [p["jump_max"]], "unit": "x", "prog": 1.0 if ind["jump"] <= p["jump_max"] else 0.0},
    ]


# ---------------------------------------------------------------- position management
def new_position(entry, t, p):
    stop = entry * (1 - p["stop_pct"] / 100.0)
    return {"entry": float(entry), "entry_t": int(t), "stop": stop, "stop0": stop, "peak": float(entry),
            "left": 1.0, "tp_done": p["tp_r"] <= 0,
            "tp_px": entry * (1 + p["tp_r"] * p["stop_pct"] / 100.0) if p["tp_r"] > 0 else None}


def on_price(pos, price, t, p):
    """Intrabar check (live polling). -> list of (fraction_of_original, price, reason, is_stop)."""
    out = []
    if price <= pos["stop"]:
        out.append((pos["left"], price, "trail" if pos["stop"] > pos["stop0"] else "stop", True))
        pos["left"] = 0.0
        return out
    if not pos["tp_done"] and price >= pos["tp_px"]:
        out.append((p["tp_frac"], price, "tp", False))
        pos["left"] -= p["tp_frac"]
        pos["tp_done"] = True
    return out


def on_close(pos, close, t_close, p):
    """End of a 5m candle: trail the stop on the close, apply the time stop."""
    out = []
    if pos["left"] <= 0:
        return out
    if close > pos["peak"]:
        pos["peak"] = float(close)
        pos["stop"] = max(pos["stop"], pos["peak"] * (1 - p["trail_pct"] / 100.0))
    if t_close - pos["entry_t"] >= p["max_hold_h"] * 3600:
        out.append((pos["left"], close, "time", False))
        pos["left"] = 0.0
    return out


def on_bar(pos, o, h, l, c, t, p):
    """Backtest: one closed candle. Pessimistic order: gap, stop, take-profit, then the close."""
    out = []
    if o <= pos["stop"]:
        out.append((pos["left"], o, "trail" if pos["stop"] > pos["stop0"] else "stop", True))
        pos["left"] = 0.0
        return out
    if l <= pos["stop"]:
        px = pos["stop"] if c >= pos["stop"] * CRASH else c
        out.append((pos["left"], px, "trail" if pos["stop"] > pos["stop0"] else "stop", True))
        pos["left"] = 0.0
        return out
    if not pos["tp_done"] and h >= pos["tp_px"]:
        out.append((p["tp_frac"], max(o, pos["tp_px"]), "tp", False))
        pos["left"] -= p["tp_frac"]
        pos["tp_done"] = True
    out += on_close(pos, c, t + BAR, p)
    return out


def sim_trade(tok, i, p):
    """Signal at candle i -> entry at the open of i+1 -> walk forward. Prices only, no costs.
    -> {"i": entry index, "j": exit index, "entry", "fills": [(frac, price, reason, is_stop, t)], "open": bool}"""
    O, H, L, C, T = tok["O"], tok["H"], tok["L"], tok["C"], tok["T"]
    n = len(C)
    e = i + 1
    if e >= n:
        return None
    pos = new_position(O[e], T[e], p)
    fills, j = [], e
    for j in range(e, n):
        for fr, px, why, st in on_bar(pos, O[j], H[j], L[j], C[j], int(T[j]), p):
            fills.append((fr, float(px), why, st, int(T[j]) + (BAR if why == "time" else 0)))
        if pos["left"] <= 1e-12:
            break
    still_open = pos["left"] > 1e-12
    if still_open:                                   # data ends: mark at the last close
        fills.append((pos["left"], float(C[n - 1]), "end", False, int(T[n - 1]) + BAR))
        j = n - 1
    return {"i": e, "j": j, "entry": float(O[e]), "entry_t": int(T[e]), "stop0": pos["stop0"], "fills": fills,
            "exit_t": fills[-1][4], "open": still_open, "peak": pos["peak"]}


def gross_multiple(tr):
    return sum(fr * px for fr, px, *_ in tr["fills"]) / tr["entry"]
