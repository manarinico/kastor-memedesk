"""Strategy / rules config. Every decision row records cfg['version_tag'].

Bump RULES_VERSION by hand when you change a rule on purpose; the hash suffix
changes automatically whenever *any* parameter differs (e.g. replay overrides),
so two runs with different settings can never share a version tag.
"""
import copy
import hashlib
import json
import os

RULES_VERSION = "rules-2026.10.26-r6"  # r6: desk position caps (4% per token, 20% total open, Gambler+Spy); r2: re-price before entry, Gecko tracking pools 5->4; r3: baseline candidates re-priced too; r4: explicit already_holding gate; r5: equity = liquidation value (sell fill at mark liquidity)

DEFAULT_CONFIG = {
    "rules_version": RULES_VERSION,
    "fill_model_version": "fill-guard-1",  # 2026-10-26: unreliable sells flagged, valued <= entry (fills.guard_sell)
    "start_book_usd": 1000.0,
    "chain": "solana",
    "scout": {
        "min_liquidity_usd": 20000.0,     # hard gate
        "min_age_min": 30.0,              # hard gate: pair must be at least this old
        "max_age_hours": 72.0,            # hard gate: "fresh launch" only
        "max_top10_pct": 30.0,            # hard gate: top-10 holders (excl. AMM pools) share
        "min_lp_locked_pct": 95.0,        # hard gate: LP burned/locked share of main market
        "max_rug_checks_per_cycle": 25,   # RugCheck politeness budget
        "rug_report_max_age_s": 1800,     # reuse a RugCheck report for up to 30 min
        "max_snapshot_age_s": 120,        # per-token stale gate
    },
    "gambler": {
        "risk_pct": 0.02,                 # size = 2% of CURRENT desk equity
        "min_h1_change_pct": 5.0,
        "max_h1_change_pct": 60.0,        # don't chase vertical candles
        "min_m5_change_pct": 0.0,
        "min_buy_sell_ratio_h1": 1.2,
        "min_h1_volume_usd": 10000.0,
        "take_profit_pct": 25.0,
        "stop_loss_pct": 12.0,
        "time_stop_min": 240.0,
        "max_open": 5,
        "reentry_cooldown_min": 120.0,
    },
    "baseline": {
        # Same momentum signal and exits as Gambler, but NONE of Scout's safety gates
        # and no desk breakers; only a fillability floor. Separate $1,000 paper book.
        "enabled": True,
        "min_liquidity_usd": 5000.0,
        "max_open": 5,
    },
    "spy": {
        "risk_pct": 0.02,
        "max_chase_pct": 15.0,            # skip if our fill > leader entry * 1.15
        "max_signal_age_min": 30.0,       # ignore leader buys older than this
        "wallet_cap_pct": 0.05,           # max open cost per leader wallet, share of equity
        "demote_after_losses": 3,
        "stop_loss_pct": 20.0,            # backstop if the leader never sells
        "time_stop_min": 360.0,
        "max_active_wallets": 8,
        "discovery_pools_per_cycle": 3,
        "tracking_pools_per_cycle": 4,
        "min_wallet_realized_usd": 100.0,
        "min_wallet_roi": 0.20,
        "max_wallet_trades_in_window": 30, # more than this in one pool window looks like a bot/MM
        "manual_wallets": [],             # optional: addresses you want followed
    },
    "risk": {
        # r6 (2026-10-26) pre-entry caps on the DESK book (Gambler + Spy combined; baseline excluded).
        # Exposure = cost basis (entry_cost_usd) of open desk positions; denominator = current
        # desk equity. A new entry that would push either above its cap is SKIPPED (never resized).
        "max_token_pct": 4.0,             # reason_code cap_token_4pct
        "max_total_open_pct": 20.0,       # reason_code cap_total_20pct
        "daily_loss_pct": 5.0,
        "max_consecutive_losses": 4,
        "consecutive_cooldown_min": 240.0,
        "liquidity_drop_pct": 40.0,       # vs liquidity at entry -> force exit + block
        "stale_feed_s": 120.0,
    },
    "costs": {
        "dex_fee_bps": 30.0,              # ~0.25-0.30% AMM fee
        "network_fee_usd": 0.05,          # base + priority fee per swap
        "base_slippage_bps": 50.0,
        "impact_mult": 1.0,               # impact ~= size / (liquidity/2) * mult
        "latency_s": 3.0,                 # simulated decision->fill lag
    },
    "http": {"timeout_s": 12.0},
}


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_config(overrides=None, path=None):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = path or os.environ.get("MEMEDESK_CONFIG")
    if path and os.path.exists(path):
        with open(path) as f:
            _merge(cfg, json.load(f))
    if overrides:
        _merge(cfg, copy.deepcopy(overrides))
    body = {k: v for k, v in cfg.items() if k not in ("config_hash", "version_tag")}
    h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]
    cfg["config_hash"] = h
    cfg["version_tag"] = f"{cfg['rules_version']}+{h}"
    return cfg
