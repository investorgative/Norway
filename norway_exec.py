#!/usr/bin/env python3
"""
NORWAY execution layer.

There is no demo account on a Solana DEX, so the DEMO mode is a *shadow executor*: for every paper fill it asks
Jupiter for a real, executable quote of the same size and records how far the paper price was from it.
That is the honest test of the fee + slippage model before real money.

LIVE (real swaps signed with your wallet) is built as a locked cockpit: keys can be stored and tested, but
LIVE_ENABLED is False and nothing in this file can send a transaction.
"""
import os
import threading
import time

import norway_core as core

LIVE_ENABLED = False          # real-money execution is not implemented on purpose
SECRETS_PATH = os.path.join(core.DATA_DIR, "secrets.json")
SHADOW_PATH = os.path.join(core.DATA_DIR, "shadow.json")
TOKEN_DECIMALS = 6            # every pump.fun token
_LOCK = threading.Lock()

FIELDS = {
    "demo": [("jupiter_key", "Jupiter API key (free at portal.jup.ag, optional)"), ("rpc_url", "Solana RPC URL (optional, e.g. free Helius)")],
    "live": [("wallet_address", "Wallet public address"), ("private_key", "Wallet private key (base58)"),
             ("jupiter_key", "Jupiter API key"), ("rpc_url", "Solana RPC URL")],
}


def load_secrets():
    return core.read_json(SECRETS_PATH, {}) or {}


def save_keys(kind, values):
    if kind not in FIELDS:
        raise ValueError("unknown key set")
    with _LOCK:
        s = load_secrets()
        cur = s.get(kind, {})
        for name, _ in FIELDS[kind]:
            v = str(values.get(name, "") or "").strip()
            if v:
                cur[name] = v
        cur["saved"] = core.now_ms()
        s[kind] = cur
        core.save_json(SECRETS_PATH, s)
        try:
            os.chmod(SECRETS_PATH, 0o600)
        except Exception:
            pass


def clear_keys(kind):
    with _LOCK:
        s = load_secrets()
        s.pop(kind, None)
        core.save_json(SECRETS_PATH, s)


def mask(v):
    v = str(v or "")
    return "" if not v else (v[:4] + "…" + v[-4:] if len(v) > 10 else "••••")


def public_view(kind):
    """what the browser may see: which fields are stored, never the values"""
    k = load_secrets().get(kind, {})
    return {"fields": [{"name": n, "label": lbl, "stored": bool(k.get(n)),
                        "hint": (k.get(n) if n == "wallet_address" else mask(k.get(n))) if k.get(n) else ""} for n, lbl in FIELDS[kind]],
            "present": any(k.get(n) for n, _ in FIELDS[kind]), "saved": k.get("saved")}


def apis_for(cfg, kind="demo"):
    k = load_secrets().get(kind, {})
    c = core.deep_merge(cfg, {})
    if k.get("rpc_url"):
        c["apis"] = dict(c["apis"], rpc=k["rpc_url"])
    return core.Apis(c, jup_key=k.get("jupiter_key"))


def test(kind, cfg):
    """connection test for the stored keys -> {"ok", "lines": [...]}"""
    out, ok = [], True
    k = load_secrets().get(kind, {})
    a = apis_for(cfg, kind)
    try:
        h = a.rpc("getHealth", [])
        out.append(f"Solana RPC: {h or 'ok'}")
    except Exception as e:
        ok = False
        out.append(f"Solana RPC failed: {e}")
    try:
        q = a.quote(core.WSOL, core.USDC, 10_000_000)
        out.append(f"Jupiter quote: 0.01 SOL → {int(q['outAmount']) / 1e6:.4f} USDC")
    except Exception as e:
        ok = False
        out.append(f"Jupiter quote failed: {e}")
    if kind == "live":
        addr = k.get("wallet_address")
        if addr:
            try:
                out.append(f"Wallet {mask(addr)}: {a.sol_balance(addr):.4f} SOL")
            except Exception as e:
                ok = False
                out.append(f"Wallet balance failed: {e}")
        else:
            ok = False
            out.append("no wallet address stored")
        out.append("Live execution is locked in code (LIVE_ENABLED = False). Nothing can be traded.")
    return {"ok": ok, "lines": out}


# ---------------------------------------------------------------- shadow executor (demo)
def shadow_state():
    return core.read_json(SHADOW_PATH, {"rows": []}) or {"rows": []}


def shadow_summary():
    rows = shadow_state()["rows"]
    ok = [r for r in rows if r.get("diff_pct") is not None]
    return {"n": len(rows), "quoted": len(ok),
            "avg_diff_pct": sum(r["diff_pct"] for r in ok) / len(ok) if ok else None,
            "worst_diff_pct": max((r["diff_pct"] for r in ok), default=None), "rows": rows[-40:]}


def shadow_fill(apis, side, mint, symbol, usd, paper_eff_px, sol_usd, trade_id):
    """Quote the paper fill on Jupiter. diff_pct > 0 means the real fill would have been WORSE than the paper fill."""
    row = {"ts": core.now_ms(), "side": side, "symbol": symbol, "mint": mint, "usd": usd, "paper_px": paper_eff_px,
           "id": trade_id, "quote_px": None, "diff_pct": None, "error": None}
    try:
        if side == "buy":
            lam = int(usd / sol_usd * 1e9)
            q = apis.quote(core.WSOL, mint, lam)
            toks = int(q["outAmount"]) / 10 ** TOKEN_DECIMALS
            px = usd / toks if toks > 0 else None
            if px:
                row.update(quote_px=px, diff_pct=(px / paper_eff_px - 1) * 100)
        else:
            qty = usd / paper_eff_px
            q = apis.quote(mint, core.WSOL, int(qty * 10 ** TOKEN_DECIMALS))
            px = int(q["outAmount"]) / 1e9 * sol_usd / qty if qty > 0 else None
            if px:
                row.update(quote_px=px, diff_pct=(1 - px / paper_eff_px) * 100)
        row["impact_pct"] = float(q.get("priceImpactPct") or 0) * 100
    except Exception as e:
        row["error"] = str(e)[:120]
    with _LOCK:
        st = shadow_state()
        st["rows"] = (st["rows"] + [row])[-500:]
        core.save_json(SHADOW_PATH, st)
    return row


def shadow_async(cfg, *a):
    def run():
        try:
            shadow_fill(apis_for(cfg, "demo"), *a)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- live (locked)
def live_status(cfg):
    return {"enabled": LIVE_ENABLED, "armed": False, "keys": public_view("live"),
            "note": "Real-money execution is switched off in code. Keys can be stored and tested; no order can be sent."}


def live_swap(*_a, **_k):
    raise RuntimeError("live trading is not implemented (LIVE_ENABLED = False)")
