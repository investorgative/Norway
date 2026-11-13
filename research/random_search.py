#!/usr/bin/env python3
"""
The robustness test behind the README: N random parameter sets of the breakout family, each run on the first
and on the second half of the dataset separately, without position limits (every signal is taken).

    python research/random_search.py 3000            # realistic fills (crash candles are filled at their close)
    CRASH=0 python research/random_search.py 3000    # optimistic fills (every stop is filled at the stop price)
"""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import norway_backtest as bt, norway_core as core, norway_strategy as S

S.PARAM_LIMITS["brk_n"] = (1, 288, int)
S.CRASH = float(os.environ.get("CRASH", "0.75"))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
cfg = core.deep_merge(core.DEFAULT_CONFIG, {})
cfg["account"].update(max_open=50, daily_loss_limit_pct=0, risk_pct=1.0, max_position_pct=10)
T = [k for k in bt.load_dataset(50) if k["seed_usd"] / k["sol_usd"] < 400]
SP = {"age_min_h": [0, 0.17, 0.5, 1, 2, 4, 8, 24], "age_max_h": [1, 3, 12, 48, 168], "mcap_min": [30e3, 50e3, 80e3, 150e3, 300e3, 600e3],
      "mcap_max": [2e5, 1e6, 5e6, 1e8], "brk_n": [1, 2, 3, 6, 12, 24, 48], "vol_n": [1, 3, 6, 12], "vol_min": [0, 3e3, 10e3, 30e3, 100e3],
      "act_min": [0, 0.75, 1.0], "max_ext": [1.5, 2.5, 5, 50], "stop_pct": [10, 15, 20, 30, 40], "trail_pct": [10, 20, 30, 45, 60],
      "tp_r": [0, 1, 2, 4], "max_hold_h": [1, 3, 12, 48, 168], "cooldown_h": [0, 1, 6], "turn_min": [0, 1, 3, 6, 12], "jump_max": [1.6, 3, 10, 1e5]}
rnd = random.Random(int(os.environ.get("SEED", "5")))
mid = (T[0]["t0"] + T[-1]["t0"]) / 2
rows = []
for _ in range(N):
    p = S.clean_params({k: rnd.choice(v) for k, v in SP.items()})
    rows.append((p, bt.quick(*bt.run_backtest(T, p, cfg, end_ts=mid, light=True)), bt.quick(*bt.run_backtest(T, p, cfg, start_ts=mid, light=True))))
avg = lambda q: q["r"] / q["n"] if q["n"] else 0.0
ok = [r for r in rows if r[1]["n"] >= 8 and r[2]["n"] >= 8]
print(f"{len(T)} tokens, {N} random parameter sets, fills: {'realistic' if S.CRASH else 'optimistic'}")
print(f"sets with at least 8 trades in each half: {len(ok)}")
print(f"  profitable in the 1st half: {sum(1 for r in ok if r[1]['r'] > 0)}   in the 2nd half: {sum(1 for r in ok if r[2]['r'] > 0)}   "
      f"in both: {sum(1 for r in ok if r[1]['r'] > 0 and r[2]['r'] > 0)}")
print(f"  median R per trade: 1st half {np.median([avg(r[1]) for r in ok]):+.2f}, 2nd half {np.median([avg(r[2]) for r in ok]):+.2f}")
