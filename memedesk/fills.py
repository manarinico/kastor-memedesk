"""Paper fills with fees + slippage + price impact. No order ever leaves this box."""


def _slip(notional_usd, liquidity_usd, costs):
    impact = notional_usd / (liquidity_usd / 2.0) * costs["impact_mult"]
    return min(costs["base_slippage_bps"] / 1e4 + impact, 0.9)


def buy(mid, budget_usd, liquidity_usd, costs):
    """Spend budget_usd in total (fees included). Returns None if unfillable."""
    if not mid or mid <= 0 or not liquidity_usd or liquidity_usd <= 0 or budget_usd <= 0:
        return None
    fee = budget_usd * costs["dex_fee_bps"] / 1e4 + costs["network_fee_usd"]
    spend = budget_usd - fee
    if spend <= 0:
        return None
    slip = _slip(spend, liquidity_usd, costs)
    px = mid * (1 + slip)
    qty = spend / px
    return {"side": "buy", "mid": mid, "px": px, "qty": qty, "fee_usd": fee,
            "slippage_usd": qty * (px - mid), "cost_usd": budget_usd, "slip_frac": slip}


def sell(mid, qty, liquidity_usd, costs):
    if not mid or mid <= 0 or not liquidity_usd or liquidity_usd <= 0 or qty <= 0:
        return None
    slip = _slip(qty * mid, liquidity_usd, costs)
    px = mid * (1 - slip)
    gross = qty * px
    fee = gross * costs["dex_fee_bps"] / 1e4 + costs["network_fee_usd"]
    return {"side": "sell", "mid": mid, "px": px, "qty": qty, "fee_usd": fee,
            "slippage_usd": qty * (mid - px), "proceeds_usd": gross - fee, "slip_frac": slip}


SLIP_CAP = 0.9
MID_SPIKE_X = 10.0


def is_unreliable(slip_frac, mid, entry_mid):
    """A sell fill is unreliable when the slippage model hit its cap (pool drained) or the
    quoted mid is more than 10x the entry mid (misprint). Approved by Manari 2026-10-26."""
    return slip_frac >= SLIP_CAP - 1e-9 or (bool(entry_mid) and mid > MID_SPIKE_X * entry_mid)


def guard_sell(f, entry_mid):
    """Value an unreliable sell at no more than the entry price; measure slippage against the
    lower of mid and entry so a phantom mid can't inflate costs. Adds 'flagged' to the fill."""
    if f is None:
        return None
    if not is_unreliable(f["slip_frac"], f["mid"], entry_mid):
        return {**f, "flagged": None}
    ref = min(f["mid"], entry_mid)
    proceeds = min(f["proceeds_usd"], f["qty"] * entry_mid)
    gross = proceeds + f["fee_usd"]
    why = "slip cap" if f["slip_frac"] >= SLIP_CAP - 1e-9 else ""
    if entry_mid and f["mid"] > MID_SPIKE_X * entry_mid:
        why = (why + "+" if why else "") + f"mid {f['mid'] / entry_mid:.1f}x entry"
    return {**f, "proceeds_usd": proceeds, "slippage_usd": max(0.0, f["qty"] * ref - gross),
            "flagged": why}
