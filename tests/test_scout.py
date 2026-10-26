import pytest

from memedesk import scout
from memedesk.gates import ALLOW, BLOCK, INSUFFICIENT, combine
from conftest import T0, make_pair, make_rug


def verdict(cfg, pair=None, rug=None, now=T0, receipt=T0):
    pair = pair or make_pair("M")
    rug = rug if rug is not None else make_rug("M")
    g = scout.cheap_gates(pair, receipt, now, cfg)
    if combine(g).state == ALLOW:
        g += scout.safety_gates(rug, receipt, pair, now, cfg)
    return combine(g)


def test_clean_token_passes(cfg):
    assert verdict(cfg).state == ALLOW


@pytest.mark.parametrize("rug,gate", [
    (make_rug("M", mint_auth="SomeAuthority111"), "mint_authority_revoked"),
    (make_rug("M", freeze="SomeFreeze111"), "freeze_authority_revoked"),
    (make_rug("M", lp=50.0), "lp_locked"),
    (make_rug("M", holders=[4.0] * 10), "top10_share"),       # 40% >= 30%
    (make_rug("M", rugged=True), "rugged"),
])
def test_each_safety_gate_hard_rejects(cfg, rug, gate):
    v = verdict(cfg, rug=rug)
    assert v.state == BLOCK and v.name == gate


@pytest.mark.parametrize("pair,gate", [
    (make_pair("M", liq=5000.0), "min_liquidity"),
    (make_pair("M", age_min=5), "min_age"),
    (make_pair("M", age_min=80 * 60), "max_age"),
])
def test_cheap_gates_hard_reject(cfg, pair, gate):
    v = verdict(cfg, pair=pair)
    assert v.state == BLOCK and v.name == gate


def test_amm_pool_not_counted_in_top10(cfg):
    # make_rug puts a 40% AMM vault first; it must be excluded (10 x 2% = 20% -> pass)
    g = [x for x in scout.safety_gates(make_rug("M"), T0, make_pair("M"), T0, cfg) if x.name == "top10_share"][0]
    assert g.state == ALLOW and g.value == 20.0


@pytest.mark.parametrize("drop", ["mintAuthority", "freezeAuthority", "topHolders", "markets"])
def test_missing_rug_fields_are_insufficient_not_permission(cfg, drop):
    rug = make_rug("M")
    rug.pop(drop)
    assert verdict(cfg, rug=rug).state == INSUFFICIENT


@pytest.mark.parametrize("drop", [("liquidity",), ("pairCreatedAt",)])
def test_missing_pair_fields_are_insufficient(cfg, drop):
    p = make_pair("M")
    p.pop(drop[0])
    assert verdict(cfg, pair=p).state == INSUFFICIENT


def test_no_report_is_insufficient(cfg):
    p = make_pair("M")
    g = scout.safety_gates(None, None, p, T0, cfg)
    assert combine(g).state == INSUFFICIENT


def test_old_report_is_insufficient(cfg):
    g = scout.safety_gates(make_rug("M"), T0 - 4000, make_pair("M"), T0, cfg)
    assert combine(g).state == INSUFFICIENT


def test_garbage_values_are_insufficient(cfg):
    p = make_pair("M")
    p["liquidity"] = {"usd": "NaN"}
    assert verdict(cfg, pair=p).state == INSUFFICIENT
