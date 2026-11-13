#!/usr/bin/env python3
"""
NORWAY backtester + optimizer.

The backtest replays every graduated token of the research dataset in time order through the same strategy
functions (norway_strategy) and the same account book (norway_core.Book) the paper trader uses.

    python norway_backtest.py                         # default parameters, whole dataset
    python norway_backtest.py --days 30 --risk 2 --balance 100
    python norway_backtest.py --optimize --combos 200
    python norway_backtest.py --walk                  # walk-forward check
"""
import argparse
import gzip
import heapq
import json
import os
import random
import sqlite3
import sys
import time
import zlib

import numpy as np

import norway_core as core
import norway_strategy as S

DATASET = os.path.join(core.BASE_DIR, "research", "dataset.jsonl.gz")
RESEARCH_DB = os.path.join(core.BASE_DIR, "research", "data.db")
OPT_STATE = os.path.join(core.DATA_DIR, "optimizer.json")
OPT_STOP = os.path.join(core.DATA_DIR, "optimizer.stop")
BT_RESULT = os.path.join(core.DATA_DIR, "backtest.json")
BAR = S.BAR

SPACE = {
    "age_min_h": [0.25, 1, 2, 4, 8, 12, 24], "age_max_h": [24, 48, 72, 168],
    "mcap_min": [50e3, 75e3, 100e3, 150e3, 250e3, 500e3], "mcap_max": [1e6, 3e6, 10e6, 100e6],
    "brk_n": [6, 12, 24, 48, 96], "vol_n": [3, 6, 12, 24], "vol_min": [2e3, 5e3, 15e3, 40e3, 100e3],
    "act_min": [0.5, 0.75, 0.9], "max_ext": [1.5, 2, 3, 5, 10],
    "stop_pct": [10, 15, 20, 25, 30, 40], "trail_pct": [15, 20, 30, 40, 50],
    "tp_r": [0, 1, 2, 3], "max_hold_h": [6, 12, 24, 48, 96],
    "turn_min": [0, 1, 2, 5, 10], "jump_max": [1.6, 3, 10, 100000],
}


# ---------------------------------------------------------------- dataset
def _token(rec):
    rows = rec["rows"]
    if not rows:
        return None
    k = S.sanitize(S.regularize(rows))
    sol_usd = float(rec.get("sol_usd") or 0) or 80.0
    k.update(pool=rec["pool"], mint=rec["mint"], t0=int(rec["t0"]), fetched=int(rec.get("fetched") or 0),
             symbol=rec.get("symbol") or rec["mint"][:4], sol_usd=sol_usd,
             p0=rec["sol"] / rec["tok"] * sol_usd, seed_usd=rec["sol"] * sol_usd, _c={})
    return k


def _sol_lookup(series):
    if not series:
        return lambda t: 80.0
    ts = np.array([x[0] for x in series], dtype=np.int64)
    px = np.array([x[1] for x in series], dtype=float)
    return lambda t: float(px[min(max(int(np.searchsorted(ts, t, side="right")) - 1, 0), len(px) - 1)])


def load_dataset(min_seed_sol=50.0, progress=None):
    """tokens from research/dataset.jsonl.gz (shipped) plus research/data.db (your own collector runs)"""
    toks, seen = [], set()
    if os.path.exists(DATASET):
        with gzip.open(DATASET, "rt", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec["sol"] < min_seed_sol or rec["pool"] in seen:
                    continue
                k = _token(rec)
                if k:
                    toks.append(k)
                    seen.add(rec["pool"])
    if os.path.exists(RESEARCH_DB):
        try:
            c = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True, timeout=30)
            row = c.execute("SELECT v FROM kv WHERE k='sol_usd'").fetchone()
            sol = _sol_lookup(json.loads(row[0]) if row else None)
            q = ("SELECT g.pool,g.mint,g.t,g.sol,g.tok,c.fetched,c.data FROM candles c JOIN grads g ON g.pool=c.pool "
                 "WHERE g.status='ok' AND g.quote=? AND g.sol>=? AND c.n>0")
            for pool, mint, t0, s, tk, fetched, data in c.execute(q, (core.WSOL, min_seed_sol)):
                if pool in seen:
                    continue
                k = _token({"pool": pool, "mint": mint, "t0": t0, "sol": s, "tok": tk, "fetched": fetched,
                            "sol_usd": sol(t0), "rows": json.loads(zlib.decompress(data))})
                if k:
                    toks.append(k)
                    seen.add(pool)
            c.close()
        except sqlite3.Error:
            pass
    toks.sort(key=lambda k: k["t0"])
    return toks


def export_dataset(path=DATASET):
    """research/data.db -> research/dataset.jsonl.gz (what gets committed to the repository)"""
    c = sqlite3.connect(RESEARCH_DB, timeout=30)
    row = c.execute("SELECT v FROM kv WHERE k='sol_usd'").fetchone()
    sol = _sol_lookup(json.loads(row[0]) if row else None)
    n = 0
    sig = lambda x: float(f"{x:.6g}")
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        q = ("SELECT g.pool,g.mint,g.t,g.sol,g.tok,c.fetched,c.data FROM candles c JOIN grads g ON g.pool=c.pool "
             "WHERE g.status='ok' AND g.quote=? AND c.n>0 ORDER BY g.t")
        for pool, mint, t0, s, tk, fetched, data in c.execute(q, (core.WSOL,)):
            rows = [[int(r[0]), sig(r[1]), sig(r[2]), sig(r[3]), sig(r[4]), round(r[5], 1)] for r in json.loads(zlib.decompress(data))]
            f.write(json.dumps({"pool": pool, "mint": mint, "t0": t0, "sol": round(s, 4), "tok": round(tk, 1),
                                "fetched": fetched, "sol_usd": round(sol(t0), 3), "rows": rows}, separators=(",", ":")) + "\n")
            n += 1
    return n


# ---------------------------------------------------------------- backtest
def run_backtest(toks, params, cfg, start_ts=None, end_ts=None, balance=None, risk_pct=None, progress=None, light=False):
    """-> (book, meta). start_ts / end_ts (seconds) select tokens by graduation time."""
    p = S.clean_params(params)
    book = core.Book(cfg, p, start_balance=balance, risk_pct=risk_pct)
    use = [k for k in toks if (start_ts is None or k["t0"] >= start_ts) and (end_ts is None or k["t0"] < end_ts)]
    if not use:
        book.started_ms = int((start_ts or time.time()) * 1000)
        return book, {"tokens": 0, "signals": 0, "start_ms": book.started_ms, "end_ms": book.started_ms}
    sigs = []
    for n, k in enumerate(use):
        if len(k["C"]) < p["brk_n"] + 2:
            continue
        m = S.signal_mask(k, S.indicators(k, p, k["_c"]), p)
        for i in np.flatnonzero(m):
            sigs.append((int(k["T"][i]) + BAR, n, int(i)))
        if progress and n % 200 == 0:
            progress(0.5 * n / len(use))
    sigs.sort()
    t_first = min(k["t0"] for k in use)
    book.started_ms = t_first * 1000
    book.day_key, book.day_start_eq = None, book.start_balance
    heap, seq = [], 0
    free_at, open_tok = {}, {}

    def marks(ts):
        out = {}
        for mint, k in open_tok.items():
            j = min(max((ts - int(k["T"][0])) // BAR - 1, 0), len(k["C"]) - 1)
            out[mint] = float(k["C"][j])
        return out

    def advance(ts):
        """apply every fill up to ts, keep the daily mark-to-market series"""
        nonlocal seq
        while heap and heap[0][0] <= ts:
            ft, _, mint, fr, px, why, st = heapq.heappop(heap)
            if not light and ft // 86400 != (book.day_key or 0):
                book.roll_day(ft * 1000, book.equity(marks(ft)))
            rec = book.positions[mint]
            rec["pos"]["left"] = max(0.0, rec["pos"]["left"] - fr)
            if rec["pos"]["left"] < 1e-9:
                rec["pos"]["left"] = 0.0
            done = book.fill(mint, fr, px, ft * 1000, why, st, open_tok[mint]["sol_usd"])
            if done:
                del open_tok[mint]
        if not light and ts // 86400 != (book.day_key or 0):
            book.roll_day(ts * 1000, book.equity(marks(ts)))

    last = t_first
    for n_s, (ts, n, i) in enumerate(sigs):
        k = use[n]
        advance(ts)
        last = ts
        if free_at.get(k["mint"], 0) > ts:
            continue
        tr = S.sim_trade(k, i, p)
        if tr is None:
            continue
        rec, why = book.open(k, tr["entry"], tr["entry_t"] * 1000, k["sol_usd"])
        if rec is None:
            continue
        rec["pos"]["stop0"], rec["pos"]["peak"] = tr["stop0"], tr["peak"]
        open_tok[k["mint"]] = k
        free_at[k["mint"]] = tr["exit_t"] + p["cooldown_h"] * 3600
        for fr, px, why, st, ft in tr["fills"]:
            seq += 1
            heapq.heappush(heap, (ft, seq, k["mint"], fr, px, why, st))
        if progress and n_s % 50 == 0:
            progress(0.5 + 0.5 * n_s / len(sigs))
    end = max([last] + [h[0] for h in heap])
    advance(end + 1)
    return book, {"tokens": len(use), "signals": len(sigs), "start_ms": t_first * 1000,
                  "end_ms": max(end, max(int(k["T"][-1]) for k in use)) * 1000}


def result(toks, params, cfg, **kw):
    book, meta = run_backtest(toks, params, cfg, **kw)
    out = core.bundle(book, cfg, meta["start_ms"], meta["end_ms"])
    out["meta"] = meta
    return out


def quick(book, meta):
    """compact numbers for the optimizer table"""
    tr = book.trades
    n = len(tr)
    gp = sum(t["pnl"] for t in tr if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    pk, dd = book.start_balance, 0.0
    for t in tr:
        pk = max(pk, t["bal_after"])
        dd = max(dd, (pk - t["bal_after"]) / pk * 100)
    return {"n": n, "r": sum(t["r"] for t in tr), "win": sum(1 for t in tr if t["pnl"] > 0) / n * 100 if n else 0.0,
            "pf": gp / gl if gl > 0 else (0.0 if gp == 0 else 99.0), "ret": (book.balance / book.start_balance - 1) * 100,
            "dd": dd, "tokens": len({t["mint"] for t in tr})}


# ---------------------------------------------------------------- optimizer
def split_time(toks, share):
    a, b = toks[0]["t0"], toks[-1]["t0"]
    return a, int(a + (b - a) * share), b + 1


def score(q):
    """what the optimizer maximises on the TRAIN part: R per trade matters, but only with enough trades and tokens"""
    if q["n"] < 8 or q["tokens"] < 4:
        return -999.0
    return q["r"] * min(1.0, q["pf"] / 1.5) - 0.2 * q["dd"]


def optimize(toks, cfg, combos=150, seed=None, state_path=OPT_STATE, stop_flag=None, log=None):
    rnd = random.Random(seed)
    t0, tm, t1 = split_time(toks, cfg["optimizer"].get("train_share", 0.6))
    rows, seen = [], set()
    base = S.clean_params(cfg["strategy"])
    st = {"running": True, "done": 0, "total": combos, "rows": [], "started": core.now_ms(),
          "train": [t0 * 1000, tm * 1000], "test": [tm * 1000, t1 * 1000], "error": None}
    core.save_json(state_path, st)
    cand = [dict(base)]
    while len(cand) < combos:
        c = dict(base)
        for k, vals in SPACE.items():
            c[k] = rnd.choice(vals)
        c = S.clean_params(c, base)
        key = json.dumps(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            cand.append(c)
    for n, c in enumerate(cand):
        if stop_flag and stop_flag():
            break
        a = quick(*run_backtest(toks, c, cfg, start_ts=t0, end_ts=tm, light=True))
        b = quick(*run_backtest(toks, c, cfg, start_ts=tm, end_ts=t1, light=True))
        rows.append({"params": c, "train": a, "test": b, "score": score(a), "base": n == 0})
        if n % 5 == 0 or n == len(cand) - 1:
            top = sorted(rows, key=lambda r: -r["score"])[:40]
            ok = [r for r in rows if r["train"]["n"] >= 8]
            st.update(done=n + 1, rows=top, summary={
                "tested": len(rows), "enough_trades": len(ok),
                "train_profitable": sum(1 for r in ok if r["train"]["r"] > 0),
                "test_profitable": sum(1 for r in ok if r["test"]["r"] > 0),
                "both_profitable": sum(1 for r in ok if r["train"]["r"] > 0 and r["test"]["r"] > 0)})
            core.save_json(state_path, st)
            if log:
                log(f"optimizer {n + 1}/{len(cand)}")
    st["running"] = False
    core.save_json(state_path, st)
    return st


def walk_forward(toks, cfg, folds=4, combos=60, seed=7):
    """choose the best parameters on everything before a fold, trade the fold with them"""
    a, b = toks[0]["t0"], toks[-1]["t0"] + 1
    edges = [int(a + (b - a) * i / (folds + 1)) for i in range(folds + 2)]
    rnd = random.Random(seed)
    base = S.clean_params(cfg["strategy"])
    cand = [dict(base)]
    while len(cand) < combos:
        c = dict(base)
        for k, vals in SPACE.items():
            c[k] = rnd.choice(vals)
        cand.append(S.clean_params(c, base))
    out = []
    for f in range(1, folds + 1):
        best, bs = None, -1e9
        for c in cand:
            s = score(quick(*run_backtest(toks, c, cfg, start_ts=edges[0], end_ts=edges[f], light=True)))
            if s > bs:
                best, bs = c, s
        q = quick(*run_backtest(toks, best, cfg, start_ts=edges[f], end_ts=edges[f + 1], light=True))
        out.append({"fold": f, "from": edges[f], "to": edges[f + 1], "params": best, "train_score": bs, "oos": q})
    return out


# ---------------------------------------------------------------- command line
def _print(res):
    s, m = res["stats"], res["meta"]
    d0, d1 = (time.strftime("%Y-%m-%d", time.gmtime(x / 1000)) for x in (m["start_ms"], m["end_ms"]))
    print(f"\n  {d0} → {d1}   {m['tokens']} graduated tokens, {m['signals']} signals, {s['skips']} skipped")
    print(f"  trades {s['trades']}  ({s['tokens']} tokens)   win rate {s['winrate']:.1f}%   profit factor {s['profit_factor'] or 0:.2f}")
    print(f"  ${s['start_balance']:.2f} → ${s['balance']:.2f}  ({s['net_pct']:+.1f}%)   total {s['cum_r']:+.1f} R   avg {s['avg_r']:+.3f} R")
    print(f"  avg win {s['avg_win_r']:+.2f} R   avg loss {s['avg_loss_r']:+.2f} R   best {s['best_r']:+.1f} R   worst {s['worst_r']:+.1f} R")
    print(f"  max drawdown {s['max_dd_pct']:.1f}% ({s['max_dd_r']:.1f} R)   worst day {s['max_daily_dd']:.1f}%   "
          f"streaks {s['max_consec_wins']}W / {s['max_consec_losses']}L")
    print(f"  fees ${s['fees']:.2f} + slippage ${s['slippage_usd']:.2f}   avg order ${s['avg_size']:.2f}   avg hold {s['avg_hold_h']:.1f} h")
    print("  exits: " + ", ".join(f"{k} {v['n']} ({v['r']:+.1f} R)" for k, v in s["reasons"].items()) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="only the last N days of the dataset (0 = all)")
    ap.add_argument("--risk", type=float)
    ap.add_argument("--balance", type=float)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--walk", action="store_true")
    ap.add_argument("--combos", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--export", action="store_true", help="write research/dataset.jsonl.gz from research/data.db")
    ap.add_argument("--set", action="append", default=[], help="override a strategy parameter, e.g. --set stop_pct=25")
    a = ap.parse_args()
    cfg = core.load_config()
    log = core.setup_logging("backtest")
    try:
        if a.export:
            print(f"exported {export_dataset()} tokens to {DATASET}")
            return
        toks = load_dataset(cfg["universe"]["min_seed_sol"])
        if not toks:
            print("no data: research/dataset.jsonl.gz is missing - run  python research/collect.py  first")
            return
        params = dict(cfg["strategy"])
        for kv in a.set:
            k, v = kv.split("=", 1)
            params[k] = float(v)
        start = toks[-1]["t0"] - a.days * 86400 if a.days else None
        if a.optimize:
            if os.path.exists(OPT_STOP):
                os.remove(OPT_STOP)
            st = optimize([k for k in toks if start is None or k["t0"] >= start], cfg, combos=a.combos or cfg["optimizer"]["combos"],
                          seed=a.seed, log=lambda m: print(m, flush=True), stop_flag=lambda: os.path.exists(OPT_STOP))
            print(f"\n{'train R':>8} {'n':>4} {'PF':>5} | {'test R':>8} {'n':>4} {'PF':>5} {'DD%':>5} | parameters that differ from the config")
            for r in st["rows"][:15]:
                diff = {k: v for k, v in r["params"].items() if cfg["strategy"].get(k) != v}
                print(f"{r['train']['r']:+8.1f} {r['train']['n']:4d} {r['train']['pf']:5.2f} | {r['test']['r']:+8.1f} {r['test']['n']:4d} "
                      f"{r['test']['pf']:5.2f} {r['test']['dd']:5.1f} | {'(config)' if r['base'] else diff}")
            print("\nsummary:", st.get("summary"))
        elif a.walk:
            tot = 0.0
            for f in walk_forward(toks, cfg, combos=a.combos or 60, seed=a.seed or 7):
                q = f["oos"]
                tot += q["r"]
                print(f"fold {f['fold']}: {time.strftime('%m-%d', time.gmtime(f['from']))} → {time.strftime('%m-%d', time.gmtime(f['to']))}  "
                      f"out-of-sample {q['r']:+.1f} R, {q['n']} trades, PF {q['pf']:.2f}, {q['ret']:+.1f}%")
            print(f"walk-forward total {tot:+.1f} R")
        else:
            _print(result(toks, params, cfg, start_ts=start, balance=a.balance, risk_pct=a.risk))
    except Exception:
        log.exception("backtest failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
