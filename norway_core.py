#!/usr/bin/env python3
"""
NORWAY core: config, logging + Discord, free data clients (Solana RPC, GeckoTerminal, DexScreener, RugCheck,
Jupiter), the account book used by the backtester AND the paper trader, statistics and the journal.
"""
import json
import logging
import math
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import numpy as np
import requests

import norway_strategy as S

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")          # live snapshot written by the trader, read by the website
BOOK_PATH = os.path.join(DATA_DIR, "book.json")            # the paper account (positions, trades, cash)
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")
COMMANDS_PATH = os.path.join(DATA_DIR, "commands.json")    # website -> trader (approve / reject / close)
LOG_PATH = os.path.join(LOG_DIR, "norway.log")
DB_PATH = os.path.join(DATA_DIR, "norway.db")
for _d in (DATA_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

DAY, HOUR = 86_400_000, 3_600_000
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIGRATOR = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"   # pump.fun migration account: one transaction per graduation

DEFAULT_CONFIG = {
    "name": "NORWAY",
    "timezone": "Europe/Berlin",
    "account": {
        "start_balance": 100.0,
        "risk_pct": 2.0,               # share of the account lost when a trade hits its hard stop
        "max_position_pct": 20.0,      # no single position larger than this share of the account
        "max_open": 4,                 # positions at the same time
        "max_pool_share_pct": 1.0,     # order size limit as a share of the pool's SOL side
        "min_order_usd": 2.0,
        "daily_loss_limit_pct": 10.0,  # no new entries for the rest of the (UTC) day after losing this much
    },
    "fees": {                          # venue: Jupiter aggregator -> PumpSwap pool, self-custody wallet
        "venue": "Jupiter → PumpSwap",
        "aggregator_bps": 10,          # Jupiter swap fee (5-10 bps), the PumpSwap tier fee is added automatically
        "slippage_pct": 0.5,           # adverse fill versus the price the bot saw, every fill
        "stop_slippage_pct": 2.0,      # extra slippage when a stop is hit (falling price, priority fee auction)
        "network_usd": 0.03,           # Solana base + priority fee + tip per transaction
    },
    "universe": {
        "min_seed_sol": 50.0,          # only graduations that seeded the pool with at least this much SOL
        "poll_hot_s": 15,
        "poll_cold_s": 120,
    },
    "strategy": dict(S.DEFAULT_PARAMS),
    "safety": {                        # live gate before every entry (cannot be backtested, see README)
        "enabled": True,
        "fail_closed": True,           # no data from RugCheck / DexScreener = no trade
        "rugcheck_max_score": 30,      # RugCheck normalised score 0 (clean) .. 100
        "max_top_holder_pct": 12.0,    # largest single wallet (pool and known programs excluded)
        "max_top10_pct": 35.0,
        "max_insiders": 15,            # wallets in detected insider networks
        "min_holders": 250,
        "min_liquidity_usd": 15_000.0,
        "min_h1_txns": 120,
        "max_sell_buy_ratio": 1.6,     # sells / buys during the last hour
        "roundtrip_check": True,       # ask Jupiter for a buy and a sell quote: detects taxes and blocked sells
        "max_roundtrip_loss_pct": 6.0,
    },
    "mode": {
        "autonomy": "auto",            # "auto" = the bot decides, "approve" = it asks on the website / Discord first
        "approve_timeout_s": 180,
    },
    "web": {"port": 8759, "passcode": "", "autolock_minutes": 10},
    "discord": {"trades": "", "alerts": "", "errors": ""},
    "apis": {
        "rpc": "https://api.mainnet-beta.solana.com",
        "gecko": "https://api.geckoterminal.com/api/v2",
        "dexscreener": "https://api.dexscreener.com",
        "rugcheck": "https://api.rugcheck.xyz/v1",
        "jupiter": "https://lite-api.jup.ag",
    },
    "optimizer": {"combos": 150, "train_share": 0.6},
}


# ---------------------------------------------------------------- json / config
def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clean_json(o):
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return clean_json(o.tolist())
    return o


def save_json(path, obj):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean_json(obj), f, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path, default=None):
    for _ in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception:
            time.sleep(0.05)
    return default


def load_config():
    user = read_json(CONFIG_PATH)
    if user is None:
        save_json_pretty(CONFIG_PATH, DEFAULT_CONFIG)
        user = {}
    cfg = deep_merge(DEFAULT_CONFIG, user)
    cfg["strategy"] = S.clean_params(cfg.get("strategy"))
    return cfg


def save_json_pretty(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean_json(obj), f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def update_config(patch):
    user = read_json(CONFIG_PATH, {}) or {}
    user = deep_merge(deep_merge(DEFAULT_CONFIG, user), patch)
    save_json_pretty(CONFIG_PATH, user)
    return load_config()


def now_ms():
    return int(time.time() * 1000)


def tz(cfg):
    try:
        return ZoneInfo(cfg.get("timezone", "UTC"))
    except Exception:
        return timezone.utc


def fmt_ts(ms, cfg, f="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ms / 1000, tz(cfg)).strftime(f)


# ---------------------------------------------------------------- logging + discord
def _discord_post(url, payload):
    try:
        if isinstance(payload, str):
            payload = {"content": payload[:1990]}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def discord_send(url, payload):
    if url and str(url).startswith("http") and payload:
        threading.Thread(target=_discord_post, args=(url, payload), daemon=True).start()


class DiscordHandler(logging.Handler):
    """Every ERROR / CRITICAL record goes to the error webhook (identical messages at most once per 5 minutes)."""

    def __init__(self, source):
        super().__init__(level=logging.ERROR)
        self.source, self._recent, self._lock = source, {}, threading.Lock()

    def emit(self, record):
        try:
            url = (read_json(CONFIG_PATH, {}) or {}).get("discord", {}).get("errors", "")
            if not url:
                return
            msg = record.getMessage()
            key = msg[:200]
            with self._lock:
                if time.time() - self._recent.get(key, 0) < 300:
                    return
                self._recent[key] = time.time()
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            body = msg + ("\n\n" + tb if tb else "")
            if len(body) > 1700:
                body = body[:500] + "\n...\n" + body[-1150:]
            discord_send(url, f"**❄ NORWAY {self.source} | {record.levelname}** "
                              f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n```{body}```")
        except Exception:
            pass


def setup_logging(source):
    log = logging.getLogger("norway")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + source + "] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.addHandler(DiscordHandler(source))
    import sys

    def hook(t, v, tb):
        log.critical("uncaught exception", exc_info=(t, v, tb))

    sys.excepthook = hook

    def thook(a):
        log.critical(f"uncaught exception in thread {a.thread.name if a.thread else '?'}",
                     exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

    threading.excepthook = thook
    return log


_EV_LOCK = threading.Lock()


def push_event(ev, keep=300):
    with _EV_LOCK:
        evs = read_json(EVENTS_PATH, []) or []
        evs.append(ev)
        save_json(EVENTS_PATH, evs[-keep:])


# ---------------------------------------------------------------- free data clients
class Http:
    """A polite GET/POST helper: minimum gap between calls per host, retries, backs off on 429."""

    def __init__(self, gap, name):
        self.gap, self.name, self.last, self.lock = gap, name, 0.0, threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "norway-bot/1.0", "Accept": "application/json"})
        self.calls = self.errors = self.limited = 0

    def _wait(self):
        with self.lock:
            w = self.last + self.gap - time.time()
            if w > 0:
                time.sleep(w)
            self.last = time.time()

    def request(self, method, url, tries=3, timeout=20, **kw):
        err = None
        for i in range(tries):
            self._wait()
            try:
                r = self.s.request(method, url, timeout=timeout, **kw)
                self.calls += 1
                if r.status_code == 429:
                    self.limited += 1
                    time.sleep(min(20, 3 + 4 * i))
                    err = "rate limited"
                    continue
                if r.status_code == 404:
                    return None
                if r.status_code >= 400:
                    err = f"HTTP {r.status_code}"
                    time.sleep(1 + i)
                    continue
                return r.json()
            except Exception as e:
                err = str(e)[:120]
                time.sleep(1 + i)
        self.errors += 1
        raise RuntimeError(f"{self.name}: {err}")


class Apis:
    def __init__(self, cfg, jup_key=None):
        a = cfg["apis"]
        self.url = a
        self.rpc_h = Http(1.1, "solana-rpc")
        self.gecko_h = Http(3.2, "geckoterminal")
        self.dex_h = Http(0.25, "dexscreener")
        self.rug_h = Http(1.0, "rugcheck")
        self.jup_h = Http(1.1, "jupiter")
        if jup_key:
            self.jup_h.s.headers["x-api-key"] = jup_key

    # --- Solana RPC -------------------------------------------------------
    def rpc(self, method, params):
        j = self.rpc_h.request("POST", self.url["rpc"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=40)
        if j is None:
            return None
        if "error" in j:
            if j["error"].get("code") in (-32015, -32602):
                return None
            raise RuntimeError(f"solana-rpc: {str(j['error'])[:140]}")
        return j.get("result")

    def new_graduation_sigs(self, until=None, limit=200):
        p = {"limit": limit}
        if until:
            p["until"] = until
        r = self.rpc("getSignaturesForAddress", [MIGRATOR, p]) or []
        return [x for x in r if not x.get("err") and x.get("blockTime")]

    def graduation(self, sig):
        """-> {mint, pool, quote, seed_quote, seed_tok} or None when the transaction is not a migration"""
        t = self.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        return parse_migration(t)

    def sol_balance(self, address):
        r = self.rpc("getBalance", [address])
        return (r or {}).get("value", 0) / 1e9

    # --- GeckoTerminal ----------------------------------------------------
    def candles(self, pool, mint, before=None, limit=1000):
        p = {"aggregate": 5, "limit": limit, "currency": "usd", "token": mint}
        if before:
            p["before_timestamp"] = int(before)
        j = self.gecko_h.request("GET", f"{self.url['gecko']}/networks/solana/pools/{pool}/ohlcv/minute", params=p)
        rows = ((j or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        return sorted([[int(r[0])] + [float(x) for x in r[1:6]] for r in rows])

    # --- DexScreener ------------------------------------------------------
    def pairs(self, pools):
        """up to 30 pool addresses -> {pool: pair dict}"""
        j = self.dex_h.request("GET", f"{self.url['dexscreener']}/latest/dex/pairs/solana/{','.join(pools)}")
        return {p["pairAddress"]: p for p in ((j or {}).get("pairs") or []) if p.get("pairAddress")}

    # --- RugCheck ---------------------------------------------------------
    def rugcheck(self, mint):
        return self.rug_h.request("GET", f"{self.url['rugcheck']}/tokens/{mint}/report", tries=2)

    # --- Jupiter ----------------------------------------------------------
    def quote(self, inp, out, amount, slippage_bps=300):
        return self.jup_h.request("GET", f"{self.url['jupiter']}/swap/v1/quote", tries=2,
                                  params={"inputMint": inp, "outputMint": out, "amount": int(amount), "slippageBps": slippage_bps})

    def sol_price(self):
        j = self.jup_h.request("GET", f"{self.url['jupiter']}/price/v3", params={"ids": WSOL}, tries=2)
        return float(j[WSOL]["usdPrice"])


def parse_migration(t):
    if not t or (t.get("meta") or {}).get("err"):
        return None
    if not any("Instruction: Migrate" in l for l in t["meta"].get("logMessages") or []):
        return None
    by_owner = {}
    for b in t["meta"].get("postTokenBalances") or []:
        by_owner.setdefault(b["owner"], {})[b["mint"]] = b["uiTokenAmount"].get("uiAmount") or 0.0
    best = None
    for owner, m in by_owner.items():
        if len(m) >= 2:
            pm = [k for k in m if k.endswith("pump")] or [k for k in m if k != WSOL]
            qm = [k for k in m if k not in pm]
            if pm and qm and m[pm[0]] > 0:
                cand = {"mint": pm[0], "pool": owner, "quote": qm[0], "seed_quote": float(m[qm[0]]), "seed_tok": float(m[pm[0]])}
                if best is None or cand["seed_tok"] > best["seed_tok"]:
                    best = cand
    return best


# ---------------------------------------------------------------- safety gate (live only)
def safety_report(apis, cfg, mint, pool, pair, size_usd, sol_usd):
    """-> {"ok": bool, "checks": [{name, ok, detail}], "score": 0..100}. Never raises."""
    sc, out = cfg["safety"], []
    closed = sc.get("fail_closed", True)

    def add(name, ok, detail):
        out.append({"name": name, "ok": bool(ok) if ok is not None else (not closed), "detail": detail, "unknown": ok is None})

    # 1. DexScreener: liquidity and order flow of the last hour
    try:
        liq = float(((pair or {}).get("liquidity") or {}).get("usd") or 0)
        tx = ((pair or {}).get("txns") or {}).get("h1") or {}
        buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
        if pair:
            add("Liquidity", liq >= sc["min_liquidity_usd"], f"${liq:,.0f} in the pool (min ${sc['min_liquidity_usd']:,.0f})")
            add("Trades last hour", buys + sells >= sc["min_h1_txns"], f"{buys} buys / {sells} sells (min {sc['min_h1_txns']})")
            ratio = sells / buys if buys else 99.0
            add("Sell pressure", ratio <= sc["max_sell_buy_ratio"], f"sells/buys {ratio:.2f} (max {sc['max_sell_buy_ratio']})")
        else:
            add("Liquidity", None, "no DexScreener data")
    except Exception as e:
        add("Liquidity", None, f"DexScreener error: {e}")
    # 2. RugCheck: authorities, holders, insiders
    try:
        r = apis.rugcheck(mint)
        if not r:
            add("RugCheck", None, "no report")
        else:
            add("Not flagged as rug", not r.get("rugged"), "RugCheck 'rugged' flag")
            add("Mint / freeze authority revoked", not r.get("mintAuthority") and not r.get("freezeAuthority"),
                "nobody can print tokens or freeze your wallet")
            fee = float(((r.get("transferFee") or {}).get("pct")) or 0)
            add("No transfer tax", fee == 0, f"transfer fee {fee}%")
            score = r.get("score_normalised")
            add("RugCheck score", score is not None and score <= sc["rugcheck_max_score"], f"{score} (max {sc['rugcheck_max_score']})")
            known = set((r.get("knownAccounts") or {}).keys()) | {pool}
            hs = [h for h in (r.get("topHolders") or []) if h.get("owner") not in known and h.get("address") not in known]
            pcts = sorted((float(h.get("pct") or 0) for h in hs), reverse=True)
            top1, top10 = (pcts[0] if pcts else 0.0), sum(pcts[:10])
            add("Largest wallet", top1 <= sc["max_top_holder_pct"], f"{top1:.1f}% (max {sc['max_top_holder_pct']}%)")
            add("Top-10 wallets", top10 <= sc["max_top10_pct"], f"{top10:.1f}% (max {sc['max_top10_pct']}%)")
            ins = int(r.get("graphInsidersDetected") or 0)
            add("Insider network", ins <= sc["max_insiders"], f"{ins} linked wallets (max {sc['max_insiders']})")
            holders = int(r.get("totalHolders") or 0)
            add("Holders", holders >= sc["min_holders"], f"{holders:,} (min {sc['min_holders']:,})")
            bad = [x.get("name") for x in (r.get("risks") or []) if x.get("level") == "danger"]
            add("No danger flags", not bad, ", ".join(bad) if bad else "none")
    except Exception as e:
        add("RugCheck", None, f"unreachable: {str(e)[:80]}")
    # 3. Jupiter round trip: can this size be bought AND sold, and what does it cost?
    if sc.get("roundtrip_check", True):
        try:
            lam = max(int(size_usd / sol_usd * 1e9), 1_000_000)
            q1 = apis.quote(WSOL, mint, lam)
            got = int(q1["outAmount"])
            q2 = apis.quote(mint, WSOL, got)
            back = int(q2["outAmount"])
            loss = (1 - back / lam) * 100
            add("Buy + sell route (honeypot test)", loss <= sc["max_roundtrip_loss_pct"],
                f"round trip costs {loss:.2f}% (max {sc['max_roundtrip_loss_pct']}%)")
        except Exception as e:
            out.append({"name": "Buy + sell route (honeypot test)", "ok": True, "unknown": True,
                        "detail": f"Jupiter quote unavailable ({str(e)[:60]}) - skipped"})
    ok = all(c["ok"] for c in out)
    score = int(round(100 * sum(1 for c in out if c["ok"] and not c.get("unknown")) / max(len(out), 1)))
    return {"ok": ok, "checks": out, "score": score}


# ---------------------------------------------------------------- the account book
class Book:
    """Cash, open positions and closed trades. The backtester and the paper trader both go through here,
    so every fill pays the same PumpSwap tier fee, aggregator fee, slippage, price impact and network cost."""

    def __init__(self, cfg, params=None, start_balance=None, risk_pct=None):
        self.cfg, self.fees, self.acc = cfg, cfg["fees"], cfg["account"]
        self.p = params or cfg["strategy"]
        self.start_balance = float(start_balance if start_balance is not None else self.acc["start_balance"])
        self.risk_pct = float(risk_pct if risk_pct is not None else self.acc["risk_pct"])
        self.cash = self.start_balance
        self.balance = self.start_balance           # realised: start + pnl of closed trades
        self.positions = {}                         # mint -> position
        self.trades = []
        self.next_id = 1
        self.started_ms = now_ms()
        self.days = []                              # [[ms, equity]] one mark-to-market point per UTC day
        self.day_key, self.day_start_eq = None, self.start_balance
        self.skips = 0

    # --- persistence (paper trader) ---
    def export(self):
        return {"start_balance": self.start_balance, "risk_pct": self.risk_pct, "cash": self.cash, "balance": self.balance,
                "positions": self.positions, "trades": self.trades, "next_id": self.next_id, "started_ms": self.started_ms,
                "days": self.days, "day_key": self.day_key, "day_start_eq": self.day_start_eq, "skips": self.skips}

    def load(self, st):
        for k, v in (st or {}).items():
            setattr(self, k, v)
        return self

    # --- helpers ---
    def cost_basis(self):
        return sum(p["size"] * p["pos"]["left"] for p in self.positions.values())

    def equity(self, marks=None):
        """cash + open positions at the mark price (or at cost when no mark is known)"""
        e = self.cash
        for m, p in self.positions.items():
            px = (marks or {}).get(m)
            e += p["qty"] * p["pos"]["left"] * (px if px else p["entry_eff"])
        return e

    def roll_day(self, ts_ms, eq):
        k = ts_ms // DAY
        if k != self.day_key:
            self.day_key, self.day_start_eq = k, eq
            self.days.append([int(ts_ms), eq])
            self.days = self.days[-1500:]

    def can_open(self, mint, eq):
        if mint in self.positions:
            return "already in this token"
        if len(self.positions) >= int(self.acc["max_open"]):
            return f"{len(self.positions)} positions open (max {self.acc['max_open']})"
        lim = float(self.acc.get("daily_loss_limit_pct") or 0)
        if lim > 0 and self.day_start_eq > 0 and (self.day_start_eq - eq) / self.day_start_eq * 100 >= lim:
            return f"daily loss limit {lim:.0f}% reached"
        return None

    def size_for(self, price, tok, sol_usd):
        """risk-based order size in USD, clipped by the position, cash and pool-share limits"""
        eq = self.cash + self.cost_basis()
        risk_usd = eq * self.risk_pct / 100.0
        size = risk_usd / (self.p["stop_pct"] / 100.0)
        size = min(size, eq * self.acc["max_position_pct"] / 100.0)
        size = min(size, S.pool_quote_usd(price, tok["p0"], tok["seed_usd"]) * self.acc["max_pool_share_pct"] / 100.0)
        size = min(size, self.cash * 0.985)            # room for fees
        return size, eq

    # --- fills ---
    def open(self, tok, price, ts_ms, sol_usd, extra=None):
        """tok: {mint, pool, symbol, p0, seed_usd}. -> (position, None) or (None, reason)"""
        size, eq = self.size_for(price, tok, sol_usd)
        why = self.can_open(tok["mint"], self.equity())
        if why:
            self.skips += 1
            return None, why
        if size < self.acc["min_order_usd"]:
            self.skips += 1
            return None, f"order ${size:.2f} below the minimum"
        fee_pct, slip_pct, net = S.fill_cost(price, size, self.fees, sol_usd, tok["p0"], tok["seed_usd"])
        eff = price * (1 + slip_pct / 100.0)
        fee = size * fee_pct / 100.0 + net
        pos = S.new_position(price, ts_ms // 1000, self.p)
        rec = {"id": self.next_id, "mint": tok["mint"], "pool": tok["pool"], "symbol": tok.get("symbol") or tok["mint"][:4],
               "entry": float(price), "entry_eff": eff, "entry_ts": int(ts_ms), "qty": size / eff, "size": size,
               "risk_usd": size * self.p["stop_pct"] / 100.0, "risk_pct": size * self.p["stop_pct"] / 100.0 / eq * 100 if eq > 0 else 0.0,
               "sl_pct": self.p["stop_pct"], "lev": size / eq if eq > 0 else 0.0, "bal_before": eq,
               "mcap": price * S.SUPPLY, "fees": fee, "slip_usd": size * slip_pct / 100.0, "proceeds": 0.0,
               "fills": [], "pos": pos, "p0": tok["p0"], "seed_usd": tok["seed_usd"], "extra": extra or {}}
        self.next_id += 1
        self.cash -= size + fee
        self.positions[tok["mint"]] = rec
        return rec, None

    def fill(self, mint, frac, price, ts_ms, reason, is_stop, sol_usd):
        """sell `frac` of the ORIGINAL position at `price`. -> closed trade dict when the position is flat, else None"""
        rec = self.positions[mint]
        qty = rec["qty"] * frac
        gross = qty * price
        fee_pct, slip_pct, net = S.fill_cost(price, gross, self.fees, sol_usd, rec["p0"], rec["seed_usd"], stop=is_stop)
        got = gross * (1 - slip_pct / 100.0)
        fee = got * fee_pct / 100.0 + net
        self.cash += got - fee
        rec["proceeds"] += got
        rec["fees"] += fee
        rec["slip_usd"] += gross * slip_pct / 100.0
        rec["fills"].append({"ts": int(ts_ms), "frac": frac, "px": float(price), "eff": price * (1 - slip_pct / 100.0), "reason": reason})
        if rec["pos"]["left"] > 1e-9:
            return None
        del self.positions[mint]
        pnl = rec["proceeds"] - rec["size"] - rec["fees"]
        self.balance += pnl
        sold = sum(f["frac"] for f in rec["fills"]) or 1.0
        t = {k: rec[k] for k in ("id", "mint", "pool", "symbol", "entry", "entry_eff", "entry_ts", "qty", "size", "risk_usd",
                                 "risk_pct", "sl_pct", "lev", "bal_before", "mcap", "fees", "slip_usd", "fills", "extra")}
        t.update({"exit": sum(f["px"] * f["frac"] for f in rec["fills"]) / sold, "exit_ts": int(ts_ms), "pnl": pnl,
                  "r": pnl / rec["risk_usd"] if rec["risk_usd"] > 0 else 0.0, "pct": pnl / rec["size"] * 100,
                  "reason": reason, "bal_after": self.balance, "stop0": rec["pos"]["stop0"], "peak": rec["pos"]["peak"],
                  "notional": rec["size"], "dir": 1})
        self.trades.append(t)
        return t


# ---------------------------------------------------------------- statistics
def kelly(rs):
    rs = [r for r in rs if r == r]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    if not wins or not losses:
        return {"full": 0.0, "half": 0.0}
    p = len(wins) / len(rs)
    aw, al = sum(wins) / len(wins), sum(losses) / len(losses)
    b = aw / al if al > 0 else 0.0
    f = max(0.0, (p - (1 - p) / b) / al) if b > 0 else 0.0
    return {"full": f, "half": f / 2}


def _streaks(flags):
    bw = bl = cw = cl = 0
    for w in flags:
        if w:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        bw, bl = max(bw, cw), max(bl, cl)
    return bw, bl, cw, cl


def compute_stats(book, cfg, start_ms, end_ms, marks=None):
    """every number on the overview"""
    trades, sb = book.trades, book.start_balance
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    rs = [t["r"] for t in trades]
    gp, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    cum = peak = dd_r = 0.0
    r_curve = [[int(start_ms), 0.0]]
    for t in trades:
        cum += t["r"]
        peak = max(peak, cum)
        dd_r = max(dd_r, peak - cum)
        r_curve.append([t["exit_ts"], cum])
    equity = book.equity(marks)
    curve = [[int(start_ms), sb]] + [[t["exit_ts"], t["bal_after"]] for t in trades] + [[int(end_ms), equity]]
    pk, dd_pct, dd_usd = sb, 0.0, 0.0
    for _, e in curve:
        pk = max(pk, e)
        if pk > 0 and (pk - e) / pk * 100 > dd_pct:
            dd_pct, dd_usd = (pk - e) / pk * 100, pk - e
    ds = [e for e in book.days if e[0] >= start_ms - DAY] + [[int(end_ms), equity]]
    daily_dd, pk2 = 0.0, 0.0
    for i, (_, e) in enumerate(ds):
        pk2 = max(pk2, e)
        if pk2 > 0:
            dd_pct = max(dd_pct, (pk2 - e) / pk2 * 100)
        if i and ds[i - 1][1] > 0:
            daily_dd = max(daily_dd, (ds[i - 1][1] - e) / ds[i - 1][1] * 100)
    days = max((end_ms - start_ms) / DAY, 1e-9)
    growth = equity / sb if sb > 0 else 1.0
    cagr = (growth ** (365.0 / days) - 1) * 100 if days >= 1 and growth > 0 else 0.0
    cagr = min(cagr, 1e9)
    geo_month = (growth ** (30.4375 / days) - 1) * 100 if days >= 1 and growth > 0 else 0.0
    bw, bl, cw, cl = _streaks([t["pnl"] > 0 for t in trades])
    aw_r = sum(t["r"] for t in wins) / len(wins) if wins else 0.0
    al_r = -sum(t["r"] for t in losses) / len(losses) if losses else 0.0
    fees = sum(t["fees"] for t in trades)
    slip = sum(t.get("slip_usd", 0) for t in trades)
    k = kelly(rs)
    f = cfg["fees"]
    reasons = {}
    for t in trades:
        a = reasons.setdefault(t["reason"], {"n": 0, "r": 0.0})
        a["n"] += 1
        a["r"] += t["r"]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "winrate": len(wins) / n * 100 if n else 0.0,
        "cum_r": sum(rs), "max_dd_r": dd_r, "avg_r": sum(rs) / n if n else 0.0,
        "start_balance": sb, "balance": book.balance, "equity": equity, "upnl": equity - book.balance, "cash": book.cash,
        "risk_pct": book.risk_pct, "fee_setup": {"venue": f.get("venue"), "dex": "PumpSwap 0.30–1.25 % by market cap",
                                                 "aggregator_bps": f["aggregator_bps"], "slippage_pct": f["slippage_pct"],
                                                 "stop_slippage_pct": f["stop_slippage_pct"], "network_usd": f["network_usd"]},
        "days": days, "net_pnl": equity - sb, "net_pct": (growth - 1) * 100,
        "profit_factor": gp / gl if gl > 0 else (None if gp == 0 else 999.0),
        "annual_return": cagr, "geo_month": geo_month,
        "avg_win": gp / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
        "avg_win_r": aw_r, "avg_loss_r": -al_r, "avg_rtr": aw_r / al_r if al_r > 0 else 0.0,
        "best_r": max(rs, default=0.0), "worst_r": min(rs, default=0.0),
        "max_dd_pct": dd_pct, "max_dd_usd": dd_usd, "max_daily_dd": daily_dd,
        "max_consec_wins": bw, "max_consec_losses": bl, "cur_wins": cw, "cur_losses": cl,
        "fees": fees, "slippage_usd": slip, "avg_fee": fees / n if n else 0.0,
        "avg_size": sum(t["size"] for t in trades) / n if n else 0.0,
        "avg_lev": sum(t["lev"] for t in trades) / n if n else 0.0,
        "avg_hold_h": sum(t["exit_ts"] - t["entry_ts"] for t in trades) / n / HOUR if n else 0.0,
        "avg_hold_win_h": sum(t["exit_ts"] - t["entry_ts"] for t in wins) / len(wins) / HOUR if wins else 0.0,
        "avg_hold_loss_h": sum(t["exit_ts"] - t["entry_ts"] for t in losses) / len(losses) / HOUR if losses else 0.0,
        "avg_sl_pct": sum(t["sl_pct"] for t in trades) / n if n else 0.0,
        "tokens": len({t["mint"] for t in trades}), "skips": book.skips, "open": len(book.positions),
        "kelly_full": k["full"] * 100, "kelly_half": k["half"] * 100, "reasons": reasons,
        "curve": curve, "r_curve": r_curve,
    }


def _period_keys(ms, z):
    d = datetime.fromtimestamp(ms / 1000, z)
    iso = d.isocalendar()
    return (f"{d.year}", f"{d.year}-{d.month:02d}", f"{iso[0]}-W{iso[1]:02d}", f"{d.year}-{d.month:02d}-{d.day:02d}")


def journal(trades, cfg, start_balance, start_ms, end_ms):
    """year -> month -> week -> day balances with parent links for the drill-down"""
    z = tz(cfg)
    end_ms = max(end_ms, start_ms + 1)
    days, order = {}, []
    d = datetime.fromtimestamp(start_ms / 1000, z).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp((end_ms - 1) / 1000, z)
    while d <= end:
        iso = d.isocalendar()
        key = f"{d.year}-{d.month:02d}-{d.day:02d}"
        days[key] = {"y": f"{d.year}", "m": f"{d.year}-{d.month:02d}", "w": f"{iso[0]}-W{iso[1]:02d}",
                     "pnl": 0.0, "r": 0.0, "fees": 0.0, "n": 0, "wins": 0}
        order.append(key)
        d = (d + timedelta(days=1)).replace(hour=0)
    for t in trades:
        x = days.get(_period_keys(t["exit_ts"], z)[3])
        if x is None:
            continue
        x["pnl"] += t["pnl"]
        x["r"] += t["r"]
        x["fees"] += t["fees"]
        x["n"] += 1
        x["wins"] += 1 if t["pnl"] > 0 else 0
    out = {"day": [], "week": [], "month": [], "year": []}
    bal, agg = start_balance, {"week": {}, "month": {}, "year": {}}
    for key in order:
        x = days[key]
        out["day"].append({"key": key, "start": bal, "end": bal + x["pnl"], "pnl": x["pnl"],
                           "pct": x["pnl"] / bal * 100 if bal > 0 else 0.0, "r": x["r"], "n": x["n"], "wins": x["wins"],
                           "fees": x["fees"], "week": x["w"], "month": x["m"], "year": x["y"]})
        for lvl, pk in (("week", x["w"]), ("month", x["m"]), ("year", x["y"])):
            a = agg[lvl].get(pk)
            if a is None:
                a = agg[lvl][pk] = {"key": pk, "start": bal, "end": bal, "pnl": 0.0, "r": 0.0, "n": 0, "wins": 0,
                                    "fees": 0.0, "from": key, "to": key, "year": x["y"]}
                if lvl == "week":
                    a["month"] = x["m"]
                out[lvl].append(a)
            a["end"] = bal + x["pnl"]
            for f in ("pnl", "r", "n", "wins", "fees"):
                a[f] += x[f]
            a["to"] = key
        bal += x["pnl"]
    for lvl in ("week", "month", "year"):
        for a in out[lvl]:
            a["pct"] = a["pnl"] / a["start"] * 100 if a["start"] > 0 else 0.0
    return out


def bundle(book, cfg, start_ms, end_ms, marks=None, with_trades=True):
    """everything the website needs to draw an account (the live paper account or a backtest)"""
    out = {"stats": compute_stats(book, cfg, start_ms, end_ms, marks),
           "journal": journal(book.trades, cfg, book.start_balance, start_ms, end_ms),
           "start_ms": int(start_ms), "end_ms": int(end_ms), "params": book.p}
    if with_trades:
        out["trades"] = book.trades
    return out


# ---------------------------------------------------------------- discord trade messages
def fmt_px(x):
    if not x:
        return "0"
    return f"{x:.4f}" if x >= 1 else f"{x:.3g}"


def fmt_usd(x):
    a = abs(x)
    return f"${x / 1e6:.2f}M" if a >= 1e6 else f"${x / 1e3:.1f}k" if a >= 1e3 else f"${x:.2f}"


def fmt_discord(ev, cfg):
    link = lambda pool: f"https://dexscreener.com/solana/{pool}"
    if ev["type"] == "open":
        p = ev["pos"]
        return {"embeds": [{
            "title": f"❄ NORWAY · BUY {p['symbol']}", "url": link(p["pool"]), "color": 0x4CE0D2,
            "fields": [
                {"name": "Entry", "value": f"`{fmt_px(p['entry_eff'])}` · mcap {fmt_usd(p['mcap'])}", "inline": True},
                {"name": "Stop", "value": f"`{fmt_px(p['pos']['stop0'])}` (−{p['sl_pct']:.0f}%)", "inline": True},
                {"name": "Size", "value": f"${p['size']:.2f} ({p['lev'] * 100:.0f}% of account)", "inline": True},
                {"name": "Risk", "value": f"${p['risk_usd']:.2f} ({p['risk_pct']:.2f}%)", "inline": True},
                {"name": "Safety", "value": f"{(p.get('extra') or {}).get('safety_score', '—')}/100", "inline": True},
                {"name": "Account", "value": f"${p['bal_before']:.2f}", "inline": True}],
            "footer": {"text": fmt_ts(ev["ts"], cfg) + " · paper · " + p["mint"]}}]}
    if ev["type"] == "partial":
        return f"❄ **{ev['symbol']}** take-profit: sold {ev['frac'] * 100:.0f}% at `{fmt_px(ev['px'])}`"
    if ev["type"] == "trail":
        return f"❄ **{ev['symbol']}** stop raised `{fmt_px(ev['old'])}` → `{fmt_px(ev['new'])}`"
    if ev["type"] == "close":
        t = ev["trade"]
        return {"embeds": [{
            "title": f"❄ NORWAY · SOLD {t['symbol']} · {t['r']:+.2f} R", "url": link(t["pool"]),
            "color": 0x4CE0D2 if t["pnl"] > 0 else 0xFF5C7A,
            "fields": [
                {"name": "Entry → Exit", "value": f"`{fmt_px(t['entry'])}` → `{fmt_px(t['exit'])}`", "inline": True},
                {"name": "PnL", "value": f"**${t['pnl']:+.2f}** ({t['pct']:+.1f}%)", "inline": True},
                {"name": "Reason", "value": t["reason"], "inline": True},
                {"name": "Fees + slippage", "value": f"${t['fees']:.3f} + ${t['slip_usd']:.3f}", "inline": True},
                {"name": "Held", "value": f"{(t['exit_ts'] - t['entry_ts']) / HOUR:.1f} h", "inline": True},
                {"name": "Balance", "value": f"${t['bal_after']:.2f}", "inline": True}],
            "footer": {"text": fmt_ts(ev["ts"], cfg) + " · paper"}}]}
    if ev["type"] == "proposal":
        s = ev["prop"]
        return (f"❄ **NORWAY needs your OK** — buy **{s['symbol']}** at `{fmt_px(s['price'])}` (mcap {fmt_usd(s['mcap'])}), "
                f"${s['size']:.2f}, safety {s['safety']['score']}/100. Approve on the dashboard within "
                f"{cfg['mode']['approve_timeout_s']} s. {link(s['pool'])}")
    if ev["type"] == "blocked":
        return f"❄ **{ev['symbol']}** signal blocked: {ev.get('reason', '')}"
    return None
