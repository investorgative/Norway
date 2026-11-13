#!/usr/bin/env python3
"""
NORWAY research collector.

Builds an UNBIASED sample of pump.fun graduations (tokens that migrated from the bonding curve to PumpSwap),
including every token that died afterwards, straight from the chain:

  1. page through all signatures of the pump.fun migration account (public Solana RPC)
  2. read a random sample of every day's migrations -> time, mint, pool, SOL and tokens seeded into the pool
  3. download the 5-minute candles of each pool from GeckoTerminal (free API)

Everything lands in research/data.db (sqlite). The script is resumable; start it again and it continues.

    python research/collect.py --days 60 --per-day 100
    python research/collect.py --days 60 --per-day 100 --cg-key YOUR_FREE_COINGECKO_DEMO_KEY
"""
import argparse, json, os, random, sqlite3, sys, threading, time, zlib
import requests

RPC = "https://api.mainnet-beta.solana.com"
GECKO = "https://api.geckoterminal.com/api/v2"          # keyless, about 10-30 calls a minute
COINGECKO = "https://api.coingecko.com/api/v3/onchain"  # same data with a free CoinGecko Demo key: 30 calls a minute
MIGRATOR = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
WSOL = "So11111111111111111111111111111111111111112"
DB = __file__.rsplit("/", 1)[0] + "/data.db"
UA = {"User-Agent": "norway-research/1.0", "Accept": "application/json"}


def db():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sigs(sig TEXT PRIMARY KEY, t INTEGER, ord REAL);
    CREATE TABLE IF NOT EXISTS grads(sig TEXT PRIMARY KEY, t INTEGER, mint TEXT, pool TEXT, quote TEXT, sol REAL, tok REAL, status TEXT);
    CREATE TABLE IF NOT EXISTS candles(pool TEXT PRIMARY KEY, fetched INTEGER, n INTEGER, pages INTEGER, data BLOB);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def rpc(method, params):
    for i in range(8):
        try:
            r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
            if r.status_code == 429:
                time.sleep(2.5); continue
            j = r.json()
            if "error" in j:
                if j["error"].get("code") in (-32015, -32602): return None      # unsupported / malformed: skip
                time.sleep(2 + i); continue
            return j.get("result")
        except Exception:
            time.sleep(3 + i)
    return None


def stage_sigs(days):
    c = db()
    cutoff = time.time() - days * 86400
    row = c.execute("SELECT v FROM kv WHERE k='sig_cursor'").fetchone()
    before = row[0] if row else None
    row = c.execute("SELECT MIN(t) FROM sigs").fetchone()
    if row and row[0] and row[0] < cutoff:
        return
    while True:
        p = {"limit": 1000}
        if before: p["before"] = before
        s = rpc("getSignaturesForAddress", [MIGRATOR, p])
        if not s: break
        c.executemany("INSERT OR IGNORE INTO sigs VALUES(?,?,?)",
                      [(x["signature"], x["blockTime"], random.random()) for x in s if not x.get("err") and x.get("blockTime")])
        before = s[-1]["signature"]
        c.execute("INSERT OR REPLACE INTO kv VALUES('sig_cursor',?)", (before,))
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM sigs").fetchone()[0]
        print(f"[sigs] {n} back to {time.strftime('%Y-%m-%d %H:%M', time.gmtime(s[-1]['blockTime']))}", flush=True)
        if s[-1]["blockTime"] < cutoff or len(s) < 1000: break
        time.sleep(0.4)


def parse_tx(t):
    """-> (mint, pool, quote_mint, quote_amount, token_amount) or None"""
    if not t or t["meta"].get("err"): return None
    if not any("Instruction: Migrate" in l for l in t["meta"].get("logMessages") or []): return None
    post = t["meta"]["postTokenBalances"]
    by_owner = {}
    for b in post:
        by_owner.setdefault(b["owner"], {})[b["mint"]] = b["uiTokenAmount"]["uiAmount"] or 0.0
    best = None
    for owner, m in by_owner.items():
        if len(m) >= 2:
            pm = [k for k in m if k.endswith("pump")] or [k for k in m if k != WSOL]
            qm = [k for k in m if k not in pm]
            if pm and qm and m[pm[0]] > 0:
                cand = (pm[0], owner, qm[0], m[qm[0]], m[pm[0]])
                if best is None or cand[4] > best[4]: best = cand
    return best


def stage_tx(per_day, stop, sig_done):
    """Reads `per_day` randomly chosen migrations of every fully paged day, newest day first."""
    c = db()
    while not stop.is_set():
        lo = c.execute("SELECT MIN(t) FROM sigs").fetchone()[0]
        if lo is None:
            time.sleep(5); continue
        first_day = lo // 86400 + (0 if sig_done.is_set() else 1)
        days = [r[0] for r in c.execute("SELECT DISTINCT t/86400 FROM sigs WHERE t/86400>=? ORDER BY 1 DESC", (first_day,))]
        have = dict(c.execute("SELECT t/86400, COUNT(*) FROM grads GROUP BY 1").fetchall())
        work = []
        for d in sorted(days, key=lambda d: (have.get(d, 0), -d)):          # always the least-covered day first
            if have.get(d, 0) >= per_day: break
            work = c.execute("SELECT sig,t FROM sigs WHERE t/86400=? AND sig NOT IN (SELECT sig FROM grads) ORDER BY ord LIMIT 4", (d,)).fetchall()
            if work: break
        if not work:
            if sig_done.is_set(): break
            time.sleep(10); continue
        for sig, ts in work:
            if stop.is_set(): break
            t = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
            if t is None:
                c.execute("INSERT OR REPLACE INTO grads VALUES(?,?,?,?,?,?,?,?)", (sig, ts, None, None, None, None, None, "notx"))
            else:
                p = parse_tx(t)
                if p: c.execute("INSERT OR REPLACE INTO grads VALUES(?,?,?,?,?,?,?,?)", (sig, ts, p[0], p[1], p[2], p[3], p[4], "ok"))
                else: c.execute("INSERT OR REPLACE INTO grads VALUES(?,?,?,?,?,?,?,?)", (sig, ts, None, None, None, None, None, "other"))
            c.commit()
            time.sleep(1.0)
        n = c.execute('SELECT COUNT(*) FROM grads').fetchone()[0]
        if n % 100 < 4: print(f"[tx] {n} read", flush=True)


class Gecko:
    def __init__(self, gap=2.2, key=None):
        self.gap, self.last, self.calls, self.limited = gap, 0.0, 0, 0
        self.base = COINGECKO if key else GECKO
        self.headers = dict(UA, **({"x-cg-demo-api-key": key} if key else {}))

    def get(self, path, params):
        for i in range(10):
            w = self.last + self.gap - time.time()
            if w > 0: time.sleep(w)
            self.last = time.time()
            try:
                r = requests.get(self.base + path, params=params, headers=self.headers, timeout=40)
            except Exception:
                time.sleep(5); continue
            self.calls += 1
            if r.status_code == 429:
                self.limited += 1
                self.gap = min(8.0, self.gap * 1.12)
                time.sleep(10 + 5 * i); continue
            if r.status_code == 404: return None
            if r.status_code != 200:
                time.sleep(5); continue
            self.gap = max(2.2, self.gap * 0.985)
            return r.json()
        return None


def fetch_candles(g, pool, mint, t0, max_days, now):
    """5m candles from the graduation until max_days later (or until the pool goes quiet)."""
    out, pages = {}, 0
    end = min(now, t0 + max_days * 86400)
    before = min(end, t0 + 300 * 1000)          # first page: the first 3.47 days
    while pages < 6:
        j = g.get(f"/networks/solana/pools/{pool}/ohlcv/minute",
                  {"aggregate": 5, "limit": 1000, "before_timestamp": int(before), "currency": "usd", "token": mint})
        pages += 1
        if j is None: break
        rows = j.get("data", {}).get("attributes", {}).get("ohlcv_list") or []
        for r in rows: out[int(r[0])] = r
        if before >= end: break
        last = max(out) if out else 0
        if last < before - 6 * 3600: break       # nothing traded in the last 6 h of the page: the token is dead
        before = min(end, before + 300 * 1000)
    rows = [out[k] for k in sorted(out)]
    return rows, pages


SOL_POOL = "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"      # Orca SOL/USDC


def stage_sol(g, c, days):
    """hourly SOL/USD closes (needed for the PumpSwap fee tiers, which are defined in SOL)"""
    out, before = {}, int(time.time())
    while before > time.time() - (days + 3) * 86400:
        j = g.get(f"/networks/solana/pools/{SOL_POOL}/ohlcv/hour", {"aggregate": 1, "limit": 1000, "before_timestamp": before, "currency": "usd", "token": WSOL})
        rows = (j or {}).get("data", {}).get("attributes", {}).get("ohlcv_list") or []
        if not rows: break
        for r in rows: out[int(r[0])] = float(r[4])
        before = min(out) - 1
    if out:
        c.execute("INSERT OR REPLACE INTO kv VALUES('sol_usd',?)", (json.dumps(sorted(out.items())),))
        c.commit()
        print(f"[sol] {len(out)} hourly prices", flush=True)


def stage_candles(min_sol, max_days, stop, tx_done, days=60, key=None):
    c = db(); g = Gecko(key=key)
    stage_sol(g, c, days)
    while not stop.is_set():
        rows = c.execute("""SELECT g.pool,g.mint,g.t FROM grads g JOIN sigs s ON s.sig=g.sig
                            WHERE g.status='ok' AND g.quote=? AND g.sol>=? AND g.pool NOT IN (SELECT pool FROM candles)
                            ORDER BY s.ord LIMIT 5""", (WSOL, min_sol)).fetchall()
        if not rows:
            if tx_done.is_set(): break
            time.sleep(10); continue
        for pool, mint, t0 in rows:
            if stop.is_set(): break
            now = int(time.time())
            data, pages = fetch_candles(g, pool, mint, t0, max_days, now)
            c.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?)",
                      (pool, now, len(data), pages, zlib.compress(json.dumps(data).encode())))
            c.commit()
        n = c.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        print(f"[candles] {n} pools, gecko calls {g.calls}, 429s {g.limited}, gap {g.gap:.1f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=60)
    ap.add_argument("--per-day", type=int, default=100, help="migrations read per day (random sample)")
    ap.add_argument("--min-sol", type=float, default=50, help="only fetch candles for pools seeded with at least this much SOL")
    ap.add_argument("--max-days", type=float, default=14)
    ap.add_argument("--cg-key", help="free CoinGecko Demo API key (or env COINGECKO_DEMO_KEY): 3x faster candle downloads")
    a = ap.parse_args()
    stop, sig_done, tx_done = threading.Event(), threading.Event(), threading.Event()
    def sg():
        try: stage_sigs(a.days)
        finally: sig_done.set()
    def tx():
        try: stage_tx(a.per_day, stop, sig_done)
        finally: tx_done.set()
    ths = [threading.Thread(target=sg, daemon=True), threading.Thread(target=tx, daemon=True)]
    for th in ths: th.start()
    try:
        stage_candles(a.min_sol, a.max_days, stop, tx_done, a.days, a.cg_key or os.environ.get('COINGECKO_DEMO_KEY'))
    except KeyboardInterrupt:
        stop.set()
    print("done", flush=True)


if __name__ == "__main__":
    main()
