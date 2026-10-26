"""Scout: discovery + contract safety HARD GATES (reject, never score down)."""
from .gates import allow, block, insufficient, num, combine

WSOL = "So11111111111111111111111111111111111111112"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_NULL_KEYS = (None, "", "11111111111111111111111111111111")
STABLES = {WSOL, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def parse_discovery(kind, payload):
    """-> list of (mint, origin). Solana only. Bad shapes yield nothing (never crash)."""
    out = []
    try:
        if kind in ("dex_profiles", "dex_boosts"):
            for t in payload or []:
                if isinstance(t, dict) and t.get("chainId") == "solana" and t.get("tokenAddress"):
                    out.append((t["tokenAddress"], kind))
        elif kind in ("gecko_new_pools", "gecko_trending"):
            for p in (payload or {}).get("data", []):
                bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if bid.startswith("solana_"):
                    out.append((bid[len("solana_"):], kind))
        elif kind == "rug_new_tokens":
            for t in payload or []:
                if isinstance(t, dict) and t.get("mint"):
                    out.append((t["mint"], kind))
    except (AttributeError, TypeError):
        pass
    return [(m, o) for m, o in out if m not in STABLES]


def gecko_pools(payload):
    """Trending pools -> list of (pool_address, base_mint)."""
    res = []
    try:
        for p in (payload or {}).get("data", []):
            addr = (p.get("attributes") or {}).get("address")
            bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if addr and bid.startswith("solana_"):
                res.append((addr, bid[len("solana_"):]))
    except (AttributeError, TypeError):
        pass
    return res


def pick_pair(pairs, mint, prefer_pair=None):
    """Most liquid Solana pair where the token is the BASE token."""
    cands = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"
             and (p.get("baseToken") or {}).get("address") == mint]
    if prefer_pair:
        for p in cands:
            if p.get("pairAddress") == prefer_pair:
                return p
    if not cands:
        return None
    return max(cands, key=lambda p: num(p, "liquidity", "usd") or 0)


def cheap_gates(pair, receipt_ts, now, cfg):
    s = cfg["scout"]
    g = []
    if pair is None:
        return [insufficient("pair_data", "no DexScreener pair for token")]
    age_s = None if receipt_ts is None else now - receipt_ts
    if age_s is None:
        g.append(insufficient("snapshot_fresh", "no receipt timestamp"))
    elif age_s > s["max_snapshot_age_s"]:
        g.append(insufficient("snapshot_fresh", f"pair snapshot {age_s:.0f}s old", round(age_s, 1)))
    else:
        g.append(allow("snapshot_fresh", round(age_s, 1)))
    liq = num(pair, "liquidity", "usd")
    if liq is None:
        g.append(insufficient("min_liquidity", "liquidity missing"))
    elif liq < s["min_liquidity_usd"]:
        g.append(block("min_liquidity", f"liquidity ${liq:,.0f} < ${s['min_liquidity_usd']:,.0f}", liq))
    else:
        g.append(allow("min_liquidity", liq))
    created = num(pair, "pairCreatedAt")
    if created is None:
        g.append(insufficient("min_age", "pairCreatedAt missing"))
    else:
        age_min = (now - created / 1000) / 60
        if age_min < s["min_age_min"]:
            g.append(block("min_age", f"pair age {age_min:.0f}m < {s['min_age_min']:.0f}m", round(age_min, 1)))
        elif age_min > s["max_age_hours"] * 60:
            g.append(block("max_age", f"pair age {age_min/60:.0f}h > {s['max_age_hours']:.0f}h (not a fresh launch)", round(age_min, 1)))
        else:
            g.append(allow("min_age", round(age_min, 1)))
    return g


def _amm_addresses(rep):
    addrs = set()
    for m in rep.get("markets") or []:
        if isinstance(m, dict):
            for k in ("pubkey",):
                if m.get(k):
                    addrs.add(m[k])
            for acct in ("liquidityAAccount", "liquidityBAccount"):
                o = (m.get(acct) or {}).get("owner")
                if o:
                    addrs.add(o)
            for k in ("liquidityA", "liquidityB"):
                if m.get(k):
                    addrs.add(m[k])
    for addr, info in (rep.get("knownAccounts") or {}).items():
        if isinstance(info, dict) and str(info.get("type", "")).upper() == "AMM":
            addrs.add(addr)
    return addrs


def safety_gates(rep, rep_receipt_ts, pair, now, cfg):
    s = cfg["scout"]
    if not isinstance(rep, dict):
        return [insufficient("rugcheck", "no RugCheck report")]
    g = []
    age = None if rep_receipt_ts is None else now - rep_receipt_ts
    if age is None or age > s["rug_report_max_age_s"]:
        return [insufficient("rugcheck_fresh", f"RugCheck report too old or undated ({age})")]
    if rep.get("rugged") is True:
        g.append(block("rugged", "RugCheck flags token as rugged"))
    for fld, name in (("mintAuthority", "mint_authority_revoked"), ("freezeAuthority", "freeze_authority_revoked")):
        if fld not in rep:
            g.append(insufficient(name, f"{fld} missing from report"))
        elif rep[fld] in (None, "", "11111111111111111111111111111111"):
            g.append(allow(name))
        else:
            g.append(block(name, f"{fld} still set ({str(rep[fld])[:8]}…)", rep[fld]))
    # LP burned/locked on the market we would actually trade
    markets = [m for m in (rep.get("markets") or []) if isinstance(m, dict)]
    want = (pair or {}).get("pairAddress")
    mkt = next((m for m in markets if m.get("pubkey") == want), None)
    if mkt is None and markets:
        mkt = max(markets, key=lambda m: num(m, "lp", "lpLockedUSD") or 0)
    lp = num(mkt, "lp", "lpLockedPct") if mkt else None
    if lp is None:
        g.append(insufficient("lp_locked", "no LP lock data for traded market"))
    elif lp < s["min_lp_locked_pct"]:
        g.append(block("lp_locked", f"LP locked/burned {lp:.0f}% < {s['min_lp_locked_pct']:.0f}%", lp))
    else:
        g.append(allow("lp_locked", lp))
    # top-10 holders excluding AMM pool accounts
    holders = rep.get("topHolders")
    if not isinstance(holders, list) or not holders:
        g.append(insufficient("top10_share", "topHolders missing"))
    else:
        amm = _amm_addresses(rep)
        real = [h for h in holders if isinstance(h, dict)
                and h.get("owner") not in amm and h.get("address") not in amm]
        pcts = [num(h, "pct") for h in real[:10]]
        if any(p is None for p in pcts):
            g.append(insufficient("top10_share", "holder pct missing"))
        else:
            top10 = sum(pcts)
            if top10 >= s["max_top10_pct"]:
                g.append(block("top10_share", f"top-10 holders own {top10:.1f}% >= {s['max_top10_pct']:.0f}%", round(top10, 2)))
            else:
                g.append(allow("top10_share", round(top10, 2)))
    g += token2022_gates(rep, cfg)  # v1.1
    return g


# --- v1.1 (2026-10-26): Token-2022 extension gates -------------------------------------
# Read from RugCheck's `token_extensions` (the on-chain extension data), NEVER from its
# top-level `transferFee.pct` (reads 0 for a 300 bps token). Legacy SPL mints have no
# extensions and are unchanged. A missing extension field on a Token-2022 mint is
# INSUFFICIENT_DATA (`token2022_ext`), never permission.
_MISSING = object()


def _set(v):
    """A pubkey-like value counts as set unless null / empty / the system program."""
    return v not in _NULL_KEYS


def _field(d, *names):
    """First present key (case-insensitive) in dict d, else _MISSING."""
    if not isinstance(d, dict):
        return _MISSING
    low = {str(k).lower(): k for k in d}
    for n in names:
        if n.lower() in low:
            return d[low[n.lower()]]
    return _MISSING


def _ext_insuff(field):
    return insufficient("token2022_ext", f"Token-2022 extension field {field} missing/unreadable")


def token2022_gates(rep, cfg):
    """-> list of Gate: transfer_fee + transfer_restriction (or token2022_ext INSUFF)."""
    prog = rep.get("tokenProgram")
    if prog == SPL_TOKEN:
        return [allow("transfer_fee", reason="legacy SPL: no extensions"),
                allow("transfer_restriction", reason="legacy SPL: no extensions")]
    if prog != TOKEN_2022:
        return [insufficient("token2022_ext", f"tokenProgram missing/unknown ({str(prog)[:8]})")]
    ext = rep.get("token_extensions")
    if not isinstance(ext, dict):
        return [insufficient("token2022_ext", "token_extensions missing for Token-2022 mint")]
    return [_transfer_fee_gate(ext, cfg), _transfer_restriction_gate(ext)]


def _transfer_fee_gate(ext, cfg):
    max_bps = cfg["scout"].get("max_transfer_fee_bps", 0.0)
    tfc = _field(ext, "transferFeeConfig")
    if tfc is _MISSING:
        return _ext_insuff("transferFeeConfig")
    if tfc is None:
        return allow("transfer_fee", 0.0, "no transfer-fee extension")
    if not isinstance(tfc, dict):
        return _ext_insuff("transferFeeConfig (bad shape)")
    bps, missing = [], []
    for which in ("newerTransferFee", "olderTransferFee"):  # older applies until its epoch passes
        sub = _field(tfc, which)
        b = num(sub, "transferFeeBasisPoints") if isinstance(sub, dict) else None
        if b is None:
            missing.append(f"transferFeeConfig.{which}.transferFeeBasisPoints")
        else:
            bps.append(b)
    if bps and max(bps) > max_bps:
        return block("transfer_fee", f"Token-2022 transfer fee {max(bps):.0f} bps > {max_bps:.0f} bps", max(bps))
    if missing:
        return _ext_insuff(", ".join(missing))
    auth = _field(tfc, "transferFeeConfigAuthority")
    if auth is _MISSING:
        return _ext_insuff("transferFeeConfig.transferFeeConfigAuthority")
    if _set(auth):
        return block("transfer_fee", f"fee config authority still set ({str(auth)[:8]}…): fee can be raised", 0.0)
    return allow("transfer_fee", max(bps), "fee 0 bps, authority revoked")


def _restriction_hits(ext):
    """-> (hits, missing). Each hit is a short description of an extension that can stop or seize a sell."""
    hits, missing = [], []
    pd = _field(ext, "permanentDelegate")
    if pd is _MISSING:
        missing.append("permanentDelegate")
    elif isinstance(pd, dict):
        d = _field(pd, "delegate")
        if d is _MISSING or _set(d):
            hits.append("permanentDelegate")
    elif _set(pd):
        hits.append("permanentDelegate")
    th = _field(ext, "transferHook")
    if th is _MISSING:
        missing.append("transferHook")
    elif isinstance(th, dict):  # counts ONLY when its program id is actually set
        pid = _field(th, "programId", "program_id", "program")
        if pid is _MISSING:
            missing.append("transferHook.programId")
        elif _set(pid):
            hits.append(f"transferHook(program {str(pid)[:8]}…)")
    elif th is not None:
        missing.append("transferHook (bad shape)")
    das = _field(ext, "defaultAccountState")
    if das is _MISSING:
        missing.append("defaultAccountState")
    elif das is not None:
        st = _field(das, "accountState", "state") if isinstance(das, dict) else das
        if st is _MISSING:
            missing.append("defaultAccountState.accountState")
        elif str(st).lower() in ("frozen", "2"):
            hits.append("defaultAccountState=frozen")
    nt = _field(ext, "nonTransferable")
    if nt is _MISSING:
        missing.append("nonTransferable")
    elif nt not in (None, False):
        hits.append("nonTransferable")
    pc = _field(ext, "pausableConfig")
    if pc is _MISSING:
        missing.append("pausableConfig")
    elif isinstance(pc, dict):
        a, paused = _field(pc, "authority"), _field(pc, "paused")
        if paused is True or a is _MISSING or _set(a):
            hits.append("pausableConfig")
    elif pc is not None:
        hits.append("pausableConfig")
    mca = _field(ext, "mintCloseAuthority")
    if mca is _MISSING:
        missing.append("mintCloseAuthority")
    elif isinstance(mca, dict):
        a = _field(mca, "closeAuthority", "authority")
        if a is _MISSING or _set(a):
            hits.append("mintCloseAuthority")
    elif _set(mca):
        hits.append("mintCloseAuthority")
    return hits, missing


def _transfer_restriction_gate(ext):
    hits, missing = _restriction_hits(ext)
    if hits:
        return block("transfer_restriction", "Token-2022 can stop/seize a sell: " + ", ".join(hits), hits)
    if missing:
        return _ext_insuff(", ".join(missing))
    return allow("transfer_restriction")


def verdict(gates):
    return combine(gates)
