#!/usr/bin/env python3
"""
NORWAY live paper trader (tmux session "Norway").

Loop:
  1. discover   every pump.fun graduation, straight from the chain (public Solana RPC)
  2. poll       prices / volume / liquidity of every watched pool (DexScreener, 30 pools per call) and
                build 5-minute candles from them
  3. signal     at every candle close run the SAME strategy functions the backtester uses
  4. confirm    re-check the signal on GeckoTerminal's exchange candles, then run the safety gate
                (RugCheck, order flow, Jupiter buy+sell quote)
  5. trade      open / manage / close paper positions through the same Book as the backtester

    python norway_trader.py            # run
    python norway_trader.py --reset    # wipe the paper account and the watch list
"""
import argparse
import os
import signal
import sqlite3
import sys
import time

import norway_core as core
import norway_exec as ex
import norway_strategy as S

BAR = S.BAR
TOKENS_PATH = os.path.join(core.DATA_DIR, "tokens.json")


class Trader:
    def __init__(self):
        self.cfg = core.load_config()
        self.log = core.setup_logging("trader")
        self.apis = ex.apis_for(self.cfg, "demo")
        self.p = self.cfg["strategy"]
        self.book = core.Book(self.cfg, self.p)
        st = core.read_json(core.BOOK_PATH)
        if st:
            self.book.load(st)
        else:
            self.save_book()
        self.db = sqlite3.connect(core.DB_PATH, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS candles(pool TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY(pool,t))")
        self.db.commit()
        saved = core.read_json(TOKENS_PATH, {}) or {}
        self.tokens = saved.get("tokens", {})            # mint -> token
        self.last_sig = saved.get("last_sig")
        self.seen = saved.get("seen", 0)                 # graduations seen since the start
        self.rows = {}                                   # mint -> candle rows (memory cache)
        self.proposals = {}                              # mint -> proposal waiting for approval
        self.sol_usd = saved.get("sol_usd") or 0.0
        self.t_disc = self.t_state = self.t_save = self.t_cfg = self.t_boot = 0.0
        self.started = time.time()
        self.stop = False
        self.last_err = {}
        self.backfill_q = []
        self.cmd_seen = 0

    # ------------------------------------------------------------ persistence
    def save_book(self):
        core.save_json(core.BOOK_PATH, self.book.export())

    def save_tokens(self):
        core.save_json(TOKENS_PATH, {"tokens": self.tokens, "last_sig": self.last_sig, "seen": self.seen, "sol_usd": self.sol_usd})

    def event(self, typ, **kw):
        ev = dict(type=typ, ts=core.now_ms(), **kw)
        core.push_event(ev)
        d = self.cfg["discord"]
        if typ in ("open", "close", "partial", "trail"):
            core.discord_send(d.get("trades"), core.fmt_discord(ev, self.cfg))
        elif typ in ("proposal", "blocked"):
            core.discord_send(d.get("alerts") or d.get("trades"), core.fmt_discord(ev, self.cfg))
        return ev

    def warn(self, key, msg, every=600):
        """log an error at most once per `every` seconds per key (API hiccups must not flood Discord)"""
        if time.time() - self.last_err.get(key, 0) > every:
            self.last_err[key] = time.time()
            self.log.error(msg)
        else:
            self.log.warning(msg)

    # ------------------------------------------------------------ candles
    def load_rows(self, tok):
        if tok["mint"] not in self.rows:
            self.rows[tok["mint"]] = [list(r) for r in self.db.execute(
                "SELECT t,o,h,l,c,v FROM candles WHERE pool=? ORDER BY t", (tok["pool"],))]
        return self.rows[tok["mint"]]

    def store_rows(self, tok, rows, replace=False):
        if replace:
            self.db.execute("DELETE FROM candles WHERE pool=? AND t<=?", (tok["pool"], rows[-1][0]))
        self.db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?)", [(tok["pool"], *r) for r in rows])
        self.db.commit()
        self.rows.pop(tok["mint"], None)

    def arrays(self, tok, rows=None):
        rows = rows if rows is not None else self.load_rows(tok)
        if not rows:
            return None
        k = S.sanitize(S.regularize(rows))
        k["t0"] = tok["t0"]
        return k

    # ------------------------------------------------------------ 1. discovery
    def add_token(self, g, t0, src="chain"):
        if g["mint"] in self.tokens:
            return
        sol = self.sol_usd or 80.0
        self.tokens[g["mint"]] = {
            "mint": g["mint"], "pool": g["pool"], "symbol": g.get("symbol") or g["mint"][:4], "name": g.get("name") or "",
            "t0": int(t0), "seed_sol": g["seed_quote"], "seed_tok": g["seed_tok"], "p0": g["seed_quote"] / g["seed_tok"] * sol,
            "seed_usd": g["seed_quote"] * sol, "state": "watch", "hot": False, "next_poll": 0, "price": None, "mcap": None,
            "liq": None, "cur": None, "pair": None, "checks": None, "free_at": 0, "src": src, "backfilled": 0, "added": int(time.time())}

    def discover(self):
        if time.time() - self.t_disc < 20:
            return
        self.t_disc = time.time()
        try:
            if not self.last_sig:                         # first start: begin with "now"
                s = self.apis.new_graduation_sigs(limit=1)
                if s:
                    self.last_sig = s[0]["signature"]
                    self.log.info("discovery starts at the newest graduation")
                return
            sigs = self.apis.new_graduation_sigs(until=self.last_sig, limit=120)
            for x in reversed(sigs):
                g = self.apis.graduation(x["signature"])
                self.last_sig = x["signature"]
                self.seen += 1
                if g and g["quote"] == core.WSOL and g["seed_quote"] >= self.cfg["universe"]["min_seed_sol"]:
                    self.add_token(g, x["blockTime"])
        except Exception as e:
            self.warn("disc", f"discovery failed: {e}")

    def bootstrap(self):
        """First start / after downtime: pull the PumpSwap pools that are trading right now from GeckoTerminal so the
        bot does not have to wait for brand-new graduations to grow up."""
        if time.time() - self.t_boot < 1800:
            return
        self.t_boot = time.time()
        try:
            n = 0
            for page in (1, 2, 3):
                j = self.apis.gecko_h.request("GET", f"{self.cfg['apis']['gecko']}/networks/solana/dexes/pumpswap/pools",
                                              params={"page": page, "sort": "h24_volume_usd_desc"})
                for x in (j or {}).get("data") or []:
                    a, rel = x["attributes"], x["relationships"]
                    mint = rel["base_token"]["data"]["id"].split("_", 1)[1]
                    if not mint.endswith("pump") or rel["quote_token"]["data"]["id"].split("_", 1)[1] != core.WSOL:
                        continue
                    t0 = int(time.mktime(time.strptime(a["pool_created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
                    if time.time() - t0 > self.p["age_max_h"] * 3600 or mint in self.tokens:
                        continue
                    self.add_token({"mint": mint, "pool": a["address"], "quote": core.WSOL, "seed_quote": 84.0, "seed_tok": 206_900_000.0,
                                    "symbol": a["name"].split(" / ")[0]}, t0, src="bootstrap")
                    n += 1
            if n:
                self.log.info(f"bootstrap: {n} young PumpSwap pools added from GeckoTerminal")
        except Exception as e:
            self.warn("boot", f"bootstrap failed: {e}")

    # ------------------------------------------------------------ 2. polling
    def poll(self):
        now = time.time()
        due = [t for t in self.tokens.values() if t["state"] == "watch" and t["next_poll"] <= now]
        due.sort(key=lambda t: (t["mint"] not in self.book.positions, not t["hot"], t["next_poll"]))
        for i in range(0, min(len(due), 300), 30):
            batch = due[i:i + 30]
            try:
                pairs = self.apis.pairs([t["pool"] for t in batch])
            except Exception as e:
                self.warn("dex", f"price poll failed: {e}")
                return
            for t in batch:
                self.on_pair(t, pairs.get(t["pool"]))

    def on_pair(self, t, pair):
        now = time.time()
        u = self.cfg["universe"]
        age_h = (now - t["t0"]) / 3600
        in_pos = t["mint"] in self.book.positions
        if not pair or not pair.get("priceUsd"):
            t["next_poll"] = now + (u["poll_hot_s"] if in_pos else u["poll_cold_s"])
            if not in_pos and age_h > 2 and t["price"] is None:
                t["state"] = "dead"
            return
        price = float(pair["priceUsd"])
        try:
            native = float(pair.get("priceNative") or 0)
            if native > 0:
                self.sol_usd = price / native
        except Exception:
            pass
        t.update(price=price, mcap=price * S.SUPPLY, liq=float((pair.get("liquidity") or {}).get("usd") or 0),
                 symbol=(pair.get("baseToken") or {}).get("symbol") or t["symbol"], name=(pair.get("baseToken") or {}).get("name") or t["name"],
                 pair={"vol_m5": float((pair.get("volume") or {}).get("m5") or 0), "vol_h1": float((pair.get("volume") or {}).get("h1") or 0),
                       "tx_h1": (pair.get("txns") or {}).get("h1") or {}, "chg_h1": float((pair.get("priceChange") or {}).get("h1") or 0),
                       "liquidity": pair.get("liquidity"), "txns": pair.get("txns")}, polled=int(now))
        # --- 5-minute candle from the polled prices
        b = int(now) // BAR * BAR
        cur = t["cur"]
        if cur is None or b > cur[0]:
            if cur is not None:
                cur[5] = t["pair"]["vol_m5"] if b - cur[0] <= BAR else cur[5]
                self.store_rows(t, [cur])
                t["cur"] = [b, cur[4], max(cur[4], price), min(cur[4], price), price, 0.0]
                self.on_candle(t, cur)
            else:
                t["cur"] = [b, price, price, price, price, 0.0]
        else:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], price), min(cur[3], price), price, t["pair"]["vol_m5"]
        # --- open position: intrabar stop / take-profit
        if in_pos:
            self.manage_price(t, price)
        # --- lifecycle
        in_pos = t["mint"] in self.book.positions
        if not in_pos:
            if age_h >= 1 and t["mcap"] < self.p["dead_mcap"]:
                t["state"] = "dead"
            elif age_h > self.p["age_max_h"] + 1:
                t["state"] = "expired"
        t["hot"] = in_pos or (t["state"] == "watch" and t["mcap"] >= 0.5 * self.p["mcap_min"] and t["mcap"] <= self.p["mcap_max"] * 1.5)
        t["next_poll"] = now + (u["poll_hot_s"] if t["hot"] else u["poll_cold_s"])

    # ------------------------------------------------------------ 3. + 4. signal, confirmation, safety
    def on_candle(self, t, candle):
        if t["mint"] in self.book.positions:
            self.manage_close(t, candle)
            return
        if not t["hot"] or t["state"] != "watch":
            return
        k = self.arrays(t)
        if k is None:
            return
        age_h = (candle[0] + BAR - t["t0"]) / 3600
        own = len(k["C"])
        need = self.p["brk_n"] + 2
        if own < need and age_h * 12 > own + 3 and time.time() - t["backfilled"] > 3600 and t["mint"] not in self.backfill_q:
            self.backfill_q.append(t["mint"])               # history is missing (restart, bootstrap): get exchange candles
        if own < need:
            t["checks"] = None
            return
        ind = S.indicators(k, self.p)
        i = len(k["C"]) - 1
        t["checks"] = S.checks(k, ind, i, self.p)
        t["level"] = float(ind["hh"][i]) if ind["hh"][i] == ind["hh"][i] else None
        if not bool(S.signal_mask(k, ind, self.p)[i]) or t["free_at"] > time.time() or t["mint"] in self.proposals:
            return
        self.signal(t, candle)

    def backfill(self, max_n=1):
        for _ in range(max_n):
            if not self.backfill_q:
                return
            t = self.tokens.get(self.backfill_q.pop(0))
            if not t or t["state"] != "watch":
                continue
            try:
                rows = self.apis.candles(t["pool"], t["mint"], limit=1000)
                t["backfilled"] = time.time()
                b = int(time.time()) // BAR * BAR
                rows = [r for r in rows if r[0] < b]
                if rows:
                    self.store_rows(t, rows, replace=True)
            except Exception as e:
                self.warn("gecko", f"candle backfill failed: {e}")

    def confirmed(self, t, candle):
        """the signal must also hold on the exchange candles (GeckoTerminal) - that is what the backtest was run on"""
        try:
            rows = self.apis.candles(t["pool"], t["mint"], limit=max(300, self.p["brk_n"] + 60))
        except Exception as e:
            self.warn("gecko", f"signal confirmation failed: {e}")
            return None
        rows = [r for r in rows if r[0] <= candle[0]]
        if not rows or rows[-1][0] < candle[0]:
            rows.append(list(candle))                       # GeckoTerminal has not published the last candle yet
        k = self.arrays(t, rows)
        if len(k["C"]) < self.p["brk_n"] + 2:
            return False
        ind = S.indicators(k, self.p)
        t["backfilled"] = time.time()
        self.store_rows(t, rows[:-1] if rows[-1][0] == candle[0] else rows, replace=False)
        return bool(S.signal_mask(k, ind, self.p)[len(k["C"]) - 1])

    def signal(self, t, candle):
        tok = {"mint": t["mint"], "pool": t["pool"], "symbol": t["symbol"], "p0": t["price"],
               "seed_usd": max((t["liq"] or 0) / 2, 1.0)}
        why = self.book.can_open(t["mint"], self.book.equity(self.marks()))
        size, _ = self.book.size_for(t["price"], tok, self.sol_usd)
        if why or size < self.cfg["account"]["min_order_usd"]:
            t["free_at"] = time.time() + 600
            self.event("blocked", symbol=t["symbol"], mint=t["mint"], reason=why or f"order ${size:.2f} too small")
            return
        ok = self.confirmed(t, candle)
        if not ok:
            t["free_at"] = time.time() + (300 if ok is None else 600)
            self.event("blocked", symbol=t["symbol"], mint=t["mint"],
                       reason="exchange candles unavailable" if ok is None else "breakout not confirmed on exchange candles")
            return
        safety = {"ok": True, "checks": [], "score": None}
        if self.cfg["safety"].get("enabled", True):
            safety = core.safety_report(self.apis, self.cfg, t["mint"], t["pool"], t["pair"], size, self.sol_usd or 80.0)
        t["safety"] = safety
        if not safety["ok"]:
            t["free_at"] = time.time() + 3600
            bad = "; ".join(f"{c['name']}: {c['detail']}" for c in safety["checks"] if not c["ok"])[:300]
            self.event("blocked", symbol=t["symbol"], mint=t["mint"], reason="safety gate — " + bad)
            self.log.info(f"{t['symbol']} blocked by the safety gate: {bad}")
            return
        if self.cfg["mode"].get("autonomy", "auto") == "approve":
            prop = {"mint": t["mint"], "pool": t["pool"], "symbol": t["symbol"], "price": t["price"], "mcap": t["mcap"],
                    "size": size, "safety": safety, "ts": core.now_ms(),
                    "expires": core.now_ms() + int(self.cfg["mode"]["approve_timeout_s"]) * 1000}
            self.proposals[t["mint"]] = prop
            self.event("proposal", prop=prop)
            return
        self.enter(t, safety)

    def enter(self, t, safety):
        try:                                                # freshest price right before the fill
            pair = self.apis.pairs([t["pool"]]).get(t["pool"])
            if pair and pair.get("priceUsd"):
                t["price"] = float(pair["priceUsd"])
                t["liq"] = float((pair.get("liquidity") or {}).get("usd") or t["liq"] or 0)
        except Exception:
            pass
        tok = {"mint": t["mint"], "pool": t["pool"], "symbol": t["symbol"], "p0": t["price"], "seed_usd": max((t["liq"] or 0) / 2, 1.0)}
        rec, why = self.book.open(tok, t["price"], core.now_ms(), self.sol_usd or 80.0,
                                  extra={"safety_score": safety.get("score"), "safety": safety.get("checks"), "level": t.get("level"),
                                         "mcap": t["mcap"], "liq": t["liq"], "age_h": (time.time() - t["t0"]) / 3600})
        if rec is None:
            t["free_at"] = time.time() + 600
            self.event("blocked", symbol=t["symbol"], mint=t["mint"], reason=why)
            return
        t["hot"] = True
        self.save_book()
        self.log.info(f"BUY {t['symbol']} ${rec['size']:.2f} at {core.fmt_px(rec['entry_eff'])} (mcap {core.fmt_usd(rec['mcap'])}), "
                      f"stop {core.fmt_px(rec['pos']['stop0'])}, fee ${rec['fees']:.3f}")
        self.event("open", pos=rec)
        if self.cfg.get("demo", {}).get("enabled"):
            ex.shadow_async(self.cfg, "buy", t["mint"], t["symbol"], rec["size"], rec["entry_eff"], self.sol_usd, rec["id"])

    # ------------------------------------------------------------ 5. position management
    def apply_fills(self, t, fills):
        rec = self.book.positions.get(t["mint"])
        for fr, px, why, is_stop in fills:
            usd = rec["qty"] * fr * px
            done = self.book.fill(t["mint"], fr, px, core.now_ms(), why, is_stop, self.sol_usd or 80.0)
            if self.cfg.get("demo", {}).get("enabled"):
                ex.shadow_async(self.cfg, "sell", t["mint"], t["symbol"], usd, rec["fills"][-1]["eff"], self.sol_usd, rec["id"])
            if done:
                t["free_at"] = time.time() + self.p["cooldown_h"] * 3600
                self.log.info(f"SOLD {t['symbol']} ({why}) pnl ${done['pnl']:+.2f} = {done['r']:+.2f} R, balance ${done['bal_after']:.2f}")
                self.event("close", trade=done)
            else:
                self.event("partial", symbol=t["symbol"], mint=t["mint"], frac=fr, px=px)
        if fills:
            self.save_book()

    def manage_price(self, t, price):
        rec = self.book.positions[t["mint"]]
        self.apply_fills(t, S.on_price(rec["pos"], price, int(time.time()), self.p))

    def manage_close(self, t, candle):
        rec = self.book.positions.get(t["mint"])
        if not rec:
            return
        old = rec["pos"]["stop"]
        fills = S.on_close(rec["pos"], candle[4], candle[0] + BAR, self.p)
        if rec["pos"]["stop"] > old:
            self.event("trail", symbol=t["symbol"], mint=t["mint"], old=old, new=rec["pos"]["stop"])
            self.save_book()
        self.apply_fills(t, fills)

    def marks(self):
        return {m: self.tokens[m]["price"] for m in self.book.positions if m in self.tokens and self.tokens[m].get("price")}

    # ------------------------------------------------------------ website commands
    def commands(self):
        cmds = core.read_json(core.COMMANDS_PATH, []) or []
        new = [c for c in cmds if c.get("id", 0) > self.cmd_seen]
        for c in new:
            self.cmd_seen = max(self.cmd_seen, c.get("id", 0))
            if core.now_ms() - c.get("ts", 0) > 300_000:
                continue
            t = self.tokens.get(c.get("mint") or "")
            try:
                if c["cmd"] == "approve" and t and t["mint"] in self.proposals:
                    prop = self.proposals.pop(t["mint"])
                    self.log.info(f"{t['symbol']} approved on the dashboard")
                    self.enter(t, prop["safety"])
                elif c["cmd"] == "reject" and t:
                    self.proposals.pop(t["mint"], None)
                    t["free_at"] = time.time() + 3600
                    self.event("blocked", symbol=t["symbol"], mint=t["mint"], reason="rejected on the dashboard")
                elif c["cmd"] == "close" and t and t["mint"] in self.book.positions and t.get("price"):
                    rec = self.book.positions[t["mint"]]
                    left, rec["pos"]["left"] = rec["pos"]["left"], 0.0
                    self.apply_fills(t, [(left, t["price"], "manual", False)])
            except Exception:
                self.log.exception("command failed")
        for m, prop in list(self.proposals.items()):
            if prop["expires"] < core.now_ms():
                self.proposals.pop(m)
                self.tokens[m]["free_at"] = time.time() + 900
                self.event("blocked", symbol=prop["symbol"], mint=m, reason="approval timed out")

    # ------------------------------------------------------------ state for the website
    def write_state(self):
        if time.time() - self.t_state < 3:
            return
        self.t_state = time.time()
        now = time.time()
        marks = self.marks()
        eq = self.book.equity(marks)
        self.book.roll_day(core.now_ms(), eq)
        watch = [t for t in self.tokens.values() if t["state"] == "watch"]
        hot = [t for t in watch if t["hot"]]

        def view(t):
            ch = t.get("checks") or []
            return {"mint": t["mint"], "pool": t["pool"], "symbol": t["symbol"], "name": t.get("name"), "age_h": (now - t["t0"]) / 3600,
                    "price": t["price"], "mcap": t["mcap"], "liq": t["liq"], "vol_h1": (t.get("pair") or {}).get("vol_h1"),
                    "chg_h1": (t.get("pair") or {}).get("chg_h1"), "tx_h1": (t.get("pair") or {}).get("tx_h1"),
                    "checks": ch, "met": sum(1 for c in ch if c["ok"]), "level": t.get("level"),
                    "prog": sum(c["prog"] for c in ch) / len(ch) if ch else 0.0, "safety": t.get("safety"),
                    "cooldown": max(0, int(t["free_at"] - now)), "candles": len(self.rows.get(t["mint"], []))}

        cands = sorted((view(t) for t in hot if t["mint"] not in self.book.positions), key=lambda v: (-v["met"], -v["prog"]))[:14]
        pos = []
        for m, rec in self.book.positions.items():
            t = self.tokens.get(m, {})
            px = marks.get(m) or rec["entry_eff"]
            left = rec["pos"]["left"]
            upnl = rec["qty"] * left * px + rec["proceeds"] - rec["size"] - rec["fees"]
            pos.append({"id": rec["id"], "mint": m, "pool": rec["pool"], "symbol": rec["symbol"], "entry": rec["entry"],
                        "entry_eff": rec["entry_eff"], "entry_ts": rec["entry_ts"], "size": rec["size"], "qty": rec["qty"], "left": left,
                        "stop": rec["pos"]["stop"], "stop0": rec["pos"]["stop0"], "peak": rec["pos"]["peak"], "tp_px": rec["pos"].get("tp_px"),
                        "tp_done": rec["pos"]["tp_done"], "price": px, "upnl": upnl, "upnl_r": upnl / rec["risk_usd"] if rec["risk_usd"] else 0,
                        "risk_usd": rec["risk_usd"], "fees": rec["fees"], "mcap": px * S.SUPPLY, "liq": t.get("liq"),
                        "locked_r": (rec["qty"] * left * rec["pos"]["stop"] + rec["proceeds"] - rec["size"] - rec["fees"]) / rec["risk_usd"] if rec["risk_usd"] else 0,
                        "dist_pct": (px - rec["pos"]["stop"]) / px * 100 if px else 0, "safety_score": (rec.get("extra") or {}).get("safety_score"),
                        "hold_h": (core.now_ms() - rec["entry_ts"]) / core.HOUR, "max_hold_h": self.p["max_hold_h"]})
        focus = pos[0]["mint"] if pos else (cands[0]["mint"] if cands else None)
        st = {"ts": core.now_ms(), "version": core.VERSION, "started": int(self.started * 1000), "sol_usd": self.sol_usd,
              "watch": len(watch), "hot": len(hot), "seen": self.seen,
              "dead": sum(1 for t in self.tokens.values() if t["state"] != "watch"),
              "candidates": cands, "positions": pos, "proposals": list(self.proposals.values()), "focus": focus,
              "equity": eq, "marks": marks, "autonomy": self.cfg["mode"].get("autonomy", "auto"),
              "day_start_eq": self.book.day_start_eq,
              "apis": {h.name: {"calls": h.calls, "errors": h.errors, "limited": h.limited}
                       for h in (self.apis.rpc_h, self.apis.dex_h, self.apis.gecko_h, self.apis.rug_h, self.apis.jup_h)},
              "params": self.p}
        core.save_json(core.STATE_PATH, st)
        if time.time() - self.t_save > 60:
            self.t_save = time.time()
            self.prune()
            self.save_tokens()
            self.save_book()

    def prune(self):
        keep_pools = {t["pool"] for t in self.book.trades[-200:]} | {r["pool"] for r in self.book.positions.values()}
        for m in [m for m, t in self.tokens.items() if t["state"] != "watch" and m not in self.book.positions]:
            t = self.tokens.pop(m)
            self.rows.pop(m, None)
            if t["pool"] not in keep_pools:
                self.db.execute("DELETE FROM candles WHERE pool=?", (t["pool"],))
        self.db.commit()

    def reload_config(self):
        if time.time() - self.t_cfg < 15:
            return
        self.t_cfg = time.time()
        cfg = core.load_config()
        if cfg != self.cfg:
            self.cfg = cfg
            self.p = cfg["strategy"]
            self.book.cfg, self.book.fees, self.book.acc, self.book.p = cfg, cfg["fees"], cfg["account"], self.p
            self.book.risk_pct = float(cfg["account"]["risk_pct"])
            self.apis = ex.apis_for(cfg, "demo")
            self.log.info("config reloaded")

    # ------------------------------------------------------------ main loop
    def run(self):
        self.log.info(f"NORWAY paper trader {core.VERSION} started · balance ${self.book.balance:.2f} · "
                      f"{len(self.tokens)} tokens on the watch list · autonomy {self.cfg['mode'].get('autonomy')}")
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
        while not self.stop:
            t0 = time.time()
            try:
                self.reload_config()
                if sum(1 for t in self.tokens.values() if t["state"] == "watch" and t["hot"]) < 3:
                    self.bootstrap()
                self.discover()
                self.poll()
                self.backfill()
                self.commands()
                self.write_state()
            except KeyboardInterrupt:
                break
            except Exception:
                self.log.exception("trader loop error")
                time.sleep(5)
            time.sleep(max(0.5, 2.0 - (time.time() - t0)))
        self.save_tokens()
        self.save_book()
        self.log.info("trader stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe the paper account, the watch list and the event feed")
    a = ap.parse_args()
    if a.reset:
        for p in (core.BOOK_PATH, TOKENS_PATH, core.STATE_PATH, core.EVENTS_PATH, core.COMMANDS_PATH, core.DB_PATH, ex.SHADOW_PATH):
            for q in (p, p + "-wal", p + "-shm"):
                if os.path.exists(q):
                    os.remove(q)
        print("paper account wiped")
        return
    try:
        Trader().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        core.setup_logging("trader").exception("trader crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
