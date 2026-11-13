#!/usr/bin/env python3
"""Offline check of the paper trader's trade path with fake APIs (no network, nothing touches data/).
    python tests/test_trader_offline.py
"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import norway_core as core

tmp = tempfile.mkdtemp()
core.CONFIG_PATH = tmp + "/config.json"
for n in ("STATE_PATH", "BOOK_PATH", "EVENTS_PATH", "COMMANDS_PATH"):
    setattr(core, n, f"{tmp}/{n}.json")
core.DB_PATH = tmp + "/n.db"
import norway_trader as nt

nt.TOKENS_PATH = tmp + "/tokens.json"
T = nt.Trader()
P = T.p


class Fake:
    rows = []
    rpc_h = dex_h = gecko_h = rug_h = jup_h = type("H", (), {"name": "x", "calls": 0, "errors": 0, "limited": 0})()

    def pairs(self, pools):
        return {p: {"pairAddress": p, "priceUsd": str(tok["price"]), "priceNative": str(tok["price"] / 80), "liquidity": {"usd": 60000},
                    "volume": {"m5": 9000, "h1": 90000}, "txns": {"h1": {"buys": 300, "sells": 250}}, "priceChange": {"h1": 12},
                    "baseToken": {"symbol": "TEST", "name": "Test"}} for p in pools}

    def candles(self, pool, mint, before=None, limit=1000):
        return [list(r) for r in self.rows]

    def rugcheck(self, mint):
        return {"rugged": False, "score_normalised": 5, "topHolders": [{"owner": "a", "pct": 3.0}], "totalHolders": 900, "risks": [],
                "transferFee": {"pct": 0}, "graphInsidersDetected": 0, "knownAccounts": {}}

    def quote(self, a, b, amt, slippage_bps=300):
        return {"outAmount": str(int(amt * 0.985))}


f = Fake()
T.apis, T.sol_usd = f, 80.0
now = int(time.time())
M = "M" * 40 + "pump"
T.add_token({"mint": M, "pool": "P" * 44, "quote": core.WSOL, "seed_quote": 70.0, "seed_tok": 206_900_000.0, "symbol": "TEST"}, now - 10 * 3600)
tok = T.tokens[M]
b = now // 300 * 300
rows = [[b - 300 * (60 - i), 3e-5, 3.1e-5, 2.9e-5, 3e-5, 8000.0] for i in range(3)] + \
       [[b - 300 * (60 - i), 2.5e-4, 2.55e-4, 2.45e-4, 2.5e-4, 8000.0] for i in range(3, 59)]
T.store_rows(tok, rows)
f.rows = rows
pair = {"vol_m5": 9000, "vol_h1": 90000, "tx_h1": {"buys": 300, "sells": 250}, "chg_h1": 12, "liquidity": {"usd": 60000},
        "txns": {"h1": {"buys": 300, "sells": 250}}}
tok.update(price=3e-4, mcap=3e5, liq=60000, hot=True, pair=pair)
c1 = [b - 300, 2.5e-4, 3.05e-4, 2.5e-4, 3e-4, 9000.0]
T.store_rows(tok, [c1])
T.on_candle(tok, c1)
assert all(c["ok"] for c in tok["checks"]), tok["checks"]
assert list(T.book.positions) == [M], core.read_json(core.EVENTS_PATH)
rec = T.book.positions[M]
assert abs(rec["risk_pct"] - T.book.risk_pct) < 1e-6 and rec["fees"] > 0
T.manage_close(tok, [b, 3e-4, 4e-4, 3e-4, 4e-4, 9000.0])
assert abs(rec["pos"]["stop"] - 4e-4 * (1 - P["trail_pct"] / 100)) < 1e-12
tok["price"] = 4e-4
T.t_state = 0
T.write_state()
st = core.read_json(core.STATE_PATH)
assert st["positions"][0]["upnl_r"] > 0
T.manage_price(tok, rec["pos"]["stop"] * 0.99)
assert not T.book.positions and T.book.trades[-1]["reason"] == "trail"
tr = T.book.trades[-1]
bd = core.bundle(T.book, T.cfg, T.book.started_ms, core.now_ms())
assert bd["stats"]["trades"] == 1 and abs(bd["stats"]["balance"] - (100 + tr["pnl"])) < 1e-9
# approval mode
T.cfg["mode"]["autonomy"] = "approve"
tok["free_at"] = 0
tok["price"] = 4.2e-4
c2 = [b, 4e-4, 4.2e-4, 4e-4, 4.2e-4, 9000.0]
T.store_rows(tok, [c2])
f.rows = rows + [c1]
T.on_candle(tok, c2)
assert list(T.proposals) == [M]
core.save_json(core.COMMANDS_PATH, [{"id": core.now_ms(), "ts": core.now_ms(), "cmd": "approve", "mint": M}])
T.commands()
assert list(T.book.positions) == [M]
assert core.fmt_discord({"type": "close", "ts": core.now_ms(), "trade": tr}, T.cfg)["embeds"][0]["title"]
print(f"OK  buy -> trail -> stop-out: pnl ${tr['pnl']:+.3f} ({tr['r']:+.2f} R), fees ${tr['fees']:.3f}; approval flow OK")
