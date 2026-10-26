from memedesk import fills

COSTS = {"dex_fee_bps": 30, "network_fee_usd": 0.05, "base_slippage_bps": 50,
         "impact_mult": 1.0, "latency_s": 3.0}


def test_normal_sell_untouched():
    f = fills.guard_sell(fills.sell(1.2e-5, 1_000_000, 200_000, COSTS), 1.0e-5)
    assert f["flagged"] is None
    assert f["proceeds_usd"] == fills.sell(1.2e-5, 1_000_000, 200_000, COSTS)["proceeds_usd"]


def test_spiked_mid_in_drained_pool_capped_at_entry():
    # MagicDog-shaped: mid 62x entry, tiny pool -> slip cap. Must not book a gain.
    qty, entry_mid = 761_409.0, 2.378e-5
    f = fills.guard_sell(fills.sell(0.001477, qty, 500, COSTS), entry_mid)
    assert f["flagged"]
    assert f["proceeds_usd"] <= qty * entry_mid
    assert f["slippage_usd"] <= qty * entry_mid  # cost measured vs entry, not phantom mid


def test_drained_pool_loss_kept_and_cost_not_inflated():
    qty, entry_mid = 1_000_000.0, 1.0e-5
    raw = fills.sell(0.9e-5, qty, 10, COSTS)  # drained: hits slip cap
    f = fills.guard_sell(raw, entry_mid)
    assert f["flagged"] == "slip cap"
    assert f["proceeds_usd"] == raw["proceeds_usd"]  # real loss is never improved
    assert f["slippage_usd"] <= qty * 0.9e-5
