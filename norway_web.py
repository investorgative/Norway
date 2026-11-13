#!/usr/bin/env python3
"""
NORWAY dashboard + API (tmux session "NorwayWeb", port 8759).

The page itself contains no data. Every /api call needs a token that only the correct passcode hands out
(checked here, on the Pi). Tokens live in memory: restarting the web server locks every browser.
"""
import hmac as _hmac
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

from flask import Flask, Response, request, send_from_directory

import norway_backtest as bt
import norway_core as core
import norway_exec as ex
import norway_strategy as S

cfg0 = core.load_config()
log = core.setup_logging("web")
app = Flask(__name__, static_folder=None)
STATIC = os.path.join(core.BASE_DIR, "static")
CF_LOG = os.path.join(core.LOG_DIR, "cloudflared.log")
TOKENS_PATH = os.path.join(core.DATA_DIR, "tokens.json")

AUTH_LOCK = threading.Lock()
TOKENS, FAILS = {}, {}
TOKEN_TTL = 12 * 3600
LOCKOUT = {6: 60, 7: 300, 8: 900}
PUBLIC_API = {"/api/auth/status", "/api/unlock"}
BT = {"running": False, "pct": 0.0, "error": None, "result": None}
BT_LOCK = threading.Lock()
OPT = {"p": None}
DATA = {"toks": None, "by_mint": {}}
CACHE, CACHE_LOCK = {}, threading.Lock()
CMD_LOCK = threading.Lock()


def _init_passcode():
    pc = str(os.environ.get("NORWAY_PASSCODE") or cfg0["web"].get("passcode") or "")
    if pc.isdigit() and 4 <= len(pc) <= 8:
        return pc
    pc = "".join(secrets.choice("0123456789") for _ in range(6))
    try:
        core.update_config({"web": {"passcode": pc}})
    except Exception:
        pass
    print(f"\n*** No valid web passcode in config.json - generated one: {pc}  (change: web.passcode) ***\n", flush=True)
    return pc


PASSCODE = _init_passcode()
AUTOLOCK_MIN = float(cfg0["web"].get("autolock_minutes", 10))


def cfg_now():
    return core.load_config()


def jresp(obj, status=200):
    import json
    return Response(json.dumps(core.clean_json(obj), separators=(",", ":")), status=status, mimetype="application/json",
                    headers={"Cache-Control": "no-store"})


def cached(key, ttl, fn):
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with CACHE_LOCK:
        CACHE[key] = (time.time(), val)
        if len(CACHE) > 300:
            for k in sorted(CACHE, key=lambda k: CACHE[k][0])[:100]:
                CACHE.pop(k, None)
    return val


def body():
    return request.get_json(force=True, silent=True) or {}


# ---------------------------------------------------------------- vault (lock screen)
def client_ip():
    peer = request.remote_addr or ""
    if peer in ("127.0.0.1", "::1"):  # behind the Cloudflare tunnel
        return request.headers.get("CF-Connecting-IP") or peer
    return peer


def lockout_for(n):
    return 0 if n < 6 else LOCKOUT.get(n, 3600)


def token_ok(tok):
    if not tok:
        return False
    with AUTH_LOCK:
        exp = TOKENS.get(tok)
        if not exp or exp < time.time():
            TOKENS.pop(tok, None)
            return False
        return True


@app.before_request
def _auth():
    if request.path.startswith("/api/") and request.path not in PUBLIC_API:
        if not token_ok(request.headers.get("X-Norway-Token", "")):
            return jresp({"error": "locked"}, 401)
    return None


@app.after_request
def _headers(r):
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@app.errorhandler(Exception)
def _err(e):
    code = getattr(e, "code", 500)
    if code == 500 or not isinstance(code, int):
        log.exception(f"web error on {request.path}")
        return jresp({"error": str(e)}, 500)
    return jresp({"error": str(e)}, code)


@app.route("/api/auth/status")
def api_auth_status():
    with AUTH_LOCK:
        f = FAILS.get(client_ip(), {"n": 0, "until": 0})
    return jresp({"attempts": f["n"], "wait": max(0, int(f["until"] - time.time())), "digits": len(PASSCODE),
                  "autolock_min": AUTOLOCK_MIN, "version": core.VERSION})


@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    code = str(body().get("code", ""))
    ip = client_ip()
    with AUTH_LOCK:
        f = FAILS.setdefault(ip, {"n": 0, "until": 0})
        now = time.time()
        if f["until"] > now:
            return jresp({"error": "locked", "wait": int(f["until"] - now), "attempts": f["n"]}, 429)
        if _hmac.compare_digest(code.encode(), PASSCODE.encode()):
            FAILS.pop(ip, None)
            for t in [t for t, e in TOKENS.items() if e < now]:
                TOKENS.pop(t, None)
            tok = secrets.token_urlsafe(32)
            TOKENS[tok] = now + TOKEN_TTL
            return jresp({"token": tok, "autolock_min": AUTOLOCK_MIN})
        f["n"] += 1
        f["until"] = now + lockout_for(f["n"])
        n, wait = f["n"], lockout_for(f["n"])
    if n >= 6:
        log.warning(f"web: {n} wrong passcodes from {ip} - locked for {wait}s")
    return jresp({"error": "wrong", "wait": wait, "attempts": n}, 403)


@app.route("/api/lock", methods=["POST"])
def api_lock():
    with AUTH_LOCK:
        TOKENS.pop(request.headers.get("X-Norway-Token", ""), None)
    return jresp({"ok": True})


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    r = send_from_directory(STATIC, "index.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p, max_age=86400)


@app.route("/healthz")
def healthz():
    return "ok"


def tunnel_url():
    def f():
        try:
            with open(CF_LOG, "r", errors="ignore") as fh:
                m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", fh.read()[-20000:])
            return m[-1] if m else None
        except Exception:
            return None
    return cached("tunnel", 20, f)


# ---------------------------------------------------------------- live paper account
def live_book(cfg):
    book = core.Book(cfg, cfg["strategy"])
    st = core.read_json(core.BOOK_PATH)
    if st:
        book.load(st)
    return book


@app.route("/api/status")
def api_status():
    """small and fast: polled every few seconds"""
    cfg = cfg_now()
    st = core.read_json(core.STATE_PATH, {}) or {}
    age = (core.now_ms() - st.get("ts", 0)) / 1000 if st else None
    return jresp({"state": st, "age": age, "alive": age is not None and age < 90, "now": core.now_ms(),
                  "tz": cfg["timezone"], "tunnel": tunnel_url(), "version": core.VERSION,
                  "bt": {k: BT[k] for k in ("running", "pct", "error")},
                  "demo": {"enabled": bool(cfg.get("demo", {}).get("enabled")), "keys": ex.public_view("demo")},
                  "live": ex.live_status(cfg)})


@app.route("/api/overview")
def api_overview():
    cfg = cfg_now()

    def f():
        book = live_book(cfg)
        st = core.read_json(core.STATE_PATH, {}) or {}
        b = core.bundle(book, cfg, book.started_ms, core.now_ms(), marks=st.get("marks"))
        b["meta"] = {"live": True}
        return b
    return jresp(cached("overview", 4, f))


@app.route("/api/events")
def api_events():
    return jresp({"events": (core.read_json(core.EVENTS_PATH, []) or [])[-150:]})


@app.route("/api/log")
def api_log():
    n = min(1000, int(request.args.get("n", 250)))
    try:
        with open(core.LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except FileNotFoundError:
        lines = []
    return jresp({"lines": lines})


def dataset():
    if DATA["toks"] is None:
        toks = bt.load_dataset(cfg_now()["universe"]["min_seed_sol"])
        DATA["toks"], DATA["by_mint"] = toks, {k["mint"]: k for k in toks}
    return DATA["toks"]


def _live_rows(pool):
    try:
        c = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True, timeout=5)
        rows = [list(r) for r in c.execute("SELECT t,o,h,l,c,v FROM candles WHERE pool=? ORDER BY t", (pool,))]
        c.close()
        return rows
    except sqlite3.Error:
        return []


@app.route("/api/chart")
def api_chart():
    """candles, breakout level, stop path and fills of one token (live paper account or the last backtest)"""
    cfg = cfg_now()
    mint = request.args.get("mint", "")
    src = request.args.get("src", "live")
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{30,50}", mint):
        return jresp({"error": "bad mint"}, 400)

    def f():
        p = cfg["strategy"]
        trades, pos, meta, k = [], None, {"mint": mint}, None
        if src == "bt":
            dataset()
            res = BT["result"] or core.read_json(bt.BT_RESULT) or {}
            p = res.get("params") or p
            trades = [t for t in res.get("trades", []) if t["mint"] == mint]
            tok = DATA["by_mint"].get(mint)
            if tok is not None:
                k = tok
                meta.update(pool=tok["pool"], symbol=tok["symbol"], t0=tok["t0"])
        else:
            book = live_book(cfg)
            trades = [t for t in book.trades if t["mint"] == mint]
            pos = book.positions.get(mint)
            tk = ((core.read_json(TOKENS_PATH, {}) or {}).get("tokens") or {}).get(mint) or {}
            pool = tk.get("pool") or (pos or {}).get("pool") or (trades[-1]["pool"] if trades else None)
            meta.update(pool=pool, symbol=tk.get("symbol") or (pos or {}).get("symbol") or (trades[-1]["symbol"] if trades else mint[:4]),
                        t0=tk.get("t0"))
            rows = _live_rows(pool) if pool else []
            if tk.get("cur"):
                rows.append(tk["cur"])
            if len(rows) < 40 and pool:                     # little own history: borrow the exchange candles
                try:
                    g = core.Apis(cfg).candles(pool, mint, limit=600)
                    have = {r[0] for r in rows}
                    rows = sorted([r for r in g if r[0] not in have] + rows)
                except Exception:
                    pass
            if rows:
                k = S.sanitize(S.regularize(rows))
                k["t0"] = tk.get("t0") or rows[0][0]
        if k is None:
            return {"candles": [], "meta": meta}
        n = len(k["C"])
        lo = max(0, n - 1500)
        hh = S._roll_max_prev(k["H"], p["brk_n"])
        cand = [{"time": int(k["T"][i]), "open": float(k["O"][i]), "high": float(k["H"][i]), "low": float(k["L"][i]),
                 "close": float(k["C"][i])} for i in range(lo, n)]
        vol = [{"time": int(k["T"][i]), "value": float(k["V"][i]), "up": bool(k["C"][i] >= k["O"][i])} for i in range(lo, n)]
        lvl = [{"time": int(k["T"][i]), "value": float(hh[i])} for i in range(lo, n) if hh[i] == hh[i]]
        marks, stops = [], []
        for t in trades:
            marks.append({"time": t["entry_ts"] // 1000 // S.BAR * S.BAR, "kind": "buy", "px": t["entry"], "text": f"BUY #{t['id']}"})
            for fl in t["fills"]:
                marks.append({"time": fl["ts"] // 1000 // S.BAR * S.BAR, "kind": "sell", "px": fl["px"],
                              "text": f"{fl['reason'].upper()} {t['r']:+.1f}R" if fl is t["fills"][-1] else fl["reason"].upper()})
            stops.append({"from": t["entry_ts"] // 1000, "to": t["exit_ts"] // 1000, "stop0": t["stop0"], "entry": t["entry"]})
        if pos:
            marks.append({"time": pos["entry_ts"] // 1000 // S.BAR * S.BAR, "kind": "buy", "px": pos["entry"], "text": f"BUY #{pos['id']}"})
        return {"candles": cand, "volume": vol, "level": lvl, "marks": marks, "stops": stops, "meta": meta,
                "pos": {"entry": pos["entry"], "stop": pos["pos"]["stop"], "tp": pos["pos"].get("tp_px") if not pos["pos"]["tp_done"] else None} if pos else None,
                "brk_n": p["brk_n"]}
    return jresp(cached(("chart", mint, src), 6 if src == "live" else 60, f))


# ---------------------------------------------------------------- settings
def _mask(u):
    return ("…" + u[-6:]) if u else ""


ACC_LIMITS = {"risk_pct": (0.05, 25), "max_position_pct": (1, 100), "max_open": (1, 20), "max_pool_share_pct": (0.05, 10),
              "daily_loss_limit_pct": (0, 100), "start_balance": (1, 1e9)}
SAFE_LIMITS = {"rugcheck_max_score": (0, 100), "max_top_holder_pct": (1, 100), "max_top10_pct": (1, 100), "max_insiders": (0, 10000),
               "min_holders": (0, 1e7), "min_liquidity_usd": (0, 1e9), "min_h1_txns": (0, 1e6), "max_sell_buy_ratio": (0.1, 100),
               "max_roundtrip_loss_pct": (0.5, 50)}


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        b = body()
        cur = cfg_now()
        patch = {}
        if "strategy" in b:
            patch["strategy"] = S.clean_params(b["strategy"], cur["strategy"])
        acc = {k: min(hi, max(lo, float(b["account"][k]))) for k, (lo, hi) in ACC_LIMITS.items() if k in (b.get("account") or {})}
        if "max_open" in acc:
            acc["max_open"] = int(acc["max_open"])
        if acc:
            patch["account"] = acc
        sf = {k: min(hi, max(lo, float(b["safety"][k]))) for k, (lo, hi) in SAFE_LIMITS.items() if k in (b.get("safety") or {})}
        for k in ("enabled", "fail_closed", "roundtrip_check"):
            if k in (b.get("safety") or {}):
                sf[k] = bool(b["safety"][k])
        if sf:
            patch["safety"] = sf
        if (b.get("mode") or {}).get("autonomy") in ("auto", "approve"):
            patch["mode"] = {"autonomy": b["mode"]["autonomy"]}
        if "demo" in b:
            patch["demo"] = {"enabled": bool((b["demo"] or {}).get("enabled"))}
        dc = {}
        for k in ("trades", "alerts", "errors"):
            v = (b.get("discord") or {}).get(k)
            if v is not None:
                v = str(v).strip()
                if v and not v.startswith("https://"):
                    return jresp({"error": "a webhook must start with https://"}, 400)
                dc[k] = v
        if dc:
            patch["discord"] = dc
        if patch:
            core.update_config(patch)
            log.info(f"settings changed from the website: {sorted(patch)}")
        if b.get("test_discord"):
            c = cfg_now()["discord"]
            core.discord_send(c.get("trades"), "❄ **NORWAY** trade webhook works.")
            core.discord_send(c.get("alerts"), "❄ **NORWAY** alert webhook works.")
            core.discord_send(c.get("errors"), "❄ **NORWAY** error webhook works.")
    c = cfg_now()
    return jresp({"account": c["account"], "fees": c["fees"], "strategy": c["strategy"], "safety": c["safety"], "mode": c["mode"],
                  "universe": c["universe"], "timezone": c["timezone"], "optimizer": c["optimizer"],
                  "limits": {k: [v[0], v[1], "int" if v[2] is int else "float"] for k, v in S.PARAM_LIMITS.items()},
                  "demo": {"enabled": bool(c.get("demo", {}).get("enabled"))},
                  "discord": {k: {"set": bool(c["discord"].get(k)), "masked": _mask(c["discord"].get(k, ""))}
                              for k in ("trades", "alerts", "errors")}})


@app.route("/api/command", methods=["POST"])
def api_command():
    b = body()
    if b.get("cmd") not in ("approve", "reject", "close"):
        return jresp({"error": "unknown command"}, 400)
    with CMD_LOCK:
        cmds = core.read_json(core.COMMANDS_PATH, []) or []
        cmds.append({"id": core.now_ms(), "ts": core.now_ms(), "cmd": b["cmd"], "mint": str(b.get("mint", ""))[:60]})
        core.save_json(core.COMMANDS_PATH, cmds[-30:])
    log.info(f"website command: {b['cmd']} {str(b.get('mint', ''))[:8]}")
    return jresp({"ok": True})


# ---------------------------------------------------------------- backtester
def _bt_thread(kw):
    try:
        toks = dataset()
        if not toks:
            raise RuntimeError("no research data: research/dataset.jsonl.gz is missing")
        start = toks[-1]["t0"] - kw["days"] * 86400 if kw["days"] else None
        res = bt.result(toks, kw["params"], cfg_now(), start_ts=start, balance=kw["balance"], risk_pct=kw["risk_pct"],
                        progress=lambda x: BT.update(pct=float(x)))
        res["meta"]["live"] = False
        res["meta"]["span_days"] = (toks[-1]["t0"] - toks[0]["t0"]) / 86400
        core.save_json(bt.BT_RESULT, res)
        BT.update(result=res, error=None)
    except Exception as e:
        log.exception("backtest failed")
        BT.update(error=str(e))
    finally:
        BT.update(running=False, pct=0.0)


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    b = body()
    cfg = cfg_now()
    with BT_LOCK:
        if BT["running"]:
            return jresp({"error": "a backtest is already running"}, 409)
        kw = {"days": max(0.0, min(4000.0, float(b.get("days") or 0))),
              "risk_pct": min(25.0, max(0.05, float(b.get("risk") or cfg["account"]["risk_pct"]))),
              "balance": max(1.0, float(b.get("balance") or cfg["account"]["start_balance"])),
              "params": S.clean_params(b.get("params"), cfg["strategy"])}
        BT.update(running=True, pct=0.0, error=None)
        threading.Thread(target=_bt_thread, args=(kw,), daemon=True, name="backtest").start()
    return jresp({"ok": True})


@app.route("/api/backtest/result")
def api_backtest_result():
    if BT["result"] is None:
        BT["result"] = core.read_json(bt.BT_RESULT, None)
    return jresp({"result": BT["result"], "running": BT["running"], "pct": BT["pct"], "error": BT["error"]})


# ---------------------------------------------------------------- optimizer
def _opt_running():
    p = OPT["p"]
    return p is not None and p.poll() is None


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    b = body()
    if _opt_running():
        return jresp({"error": "the optimizer is already running"}, 409)
    combos = max(5, min(2000, int(b.get("combos") or cfg_now()["optimizer"]["combos"])))
    core.save_json(bt.OPT_STATE, {"running": True, "done": 0, "total": combos, "rows": [], "started": core.now_ms()})
    OPT["p"] = subprocess.Popen([sys.executable, os.path.join(core.BASE_DIR, "norway_backtest.py"), "--optimize", "--combos", str(combos)],
                                cwd=core.BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jresp({"ok": True})


@app.route("/api/optimize/stop", methods=["POST"])
def api_optimize_stop():
    open(bt.OPT_STOP, "w").close()
    return jresp({"ok": True})


@app.route("/api/optimize/status")
def api_optimize_status():
    st = core.read_json(bt.OPT_STATE, {}) or {}
    if st.get("running") and not _opt_running() and core.now_ms() - st.get("started", 0) > 20000:
        st["running"] = False
    return jresp(st)


@app.route("/api/optimize/apply", methods=["POST"])
def api_optimize_apply():
    p = S.clean_params(body().get("params"), cfg_now()["strategy"])
    core.update_config({"strategy": p})
    log.info(f"strategy parameters applied from the website: {p}")
    return jresp({"ok": True, "strategy": p})


# ---------------------------------------------------------------- API keys (demo = shadow quotes, live = locked)
def _kind():
    k = body().get("kind")
    if k not in ex.FIELDS:
        raise ValueError("kind must be demo or live")
    return k


@app.route("/api/keys", methods=["POST"])
def api_keys():
    b, kind = body(), _kind()
    if b.get("clear"):
        ex.clear_keys(kind)
        log.info(f"{kind} keys removed from the website")
    else:
        vals = {k: str(v)[:400] for k, v in (b.get("values") or {}).items()}
        if vals.get("rpc_url") and not vals["rpc_url"].startswith("https://"):
            return jresp({"error": "the RPC URL must start with https://"}, 400)
        ex.save_keys(kind, vals)
        log.info(f"{kind} keys saved from the website")
    return jresp({"ok": True, "keys": ex.public_view(kind)})


@app.route("/api/keys/test", methods=["POST"])
def api_keys_test():
    return jresp(ex.test(_kind(), cfg_now()))


@app.route("/api/shadow")
def api_shadow():
    return jresp(ex.shadow_summary())


if __name__ == "__main__":
    port = int(cfg0["web"].get("port", 8759))
    log.info(f"NORWAY web {core.VERSION} on port {port}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)
