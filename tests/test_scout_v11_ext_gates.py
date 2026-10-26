"""Scout gates v1.1 (rules r7): Token-2022 transfer-fee + transfer-restriction gates."""
import pytest

from memedesk import scout
from memedesk.gates import ALLOW, BLOCK, INSUFFICIENT, combine
from conftest import T0, SPL_TOKEN, clean_extensions, make_pair, make_rug

FEE_AUTH = "F3awJjA1nMMhDN8eC47MQh6v9vvW8z4QKBcLFb9svunP"


def fee_cfg(bps, cfg_auth=None, older=None):
    return {"transferFeeConfigAuthority": cfg_auth, "withdrawWithheldAuthority": FEE_AUTH, "withheldAmount": 0,
            "olderTransferFee": {"epoch": 1068, "maximumFee": 10**15, "transferFeeBasisPoints": bps if older is None else older},
            "newerTransferFee": {"epoch": 1068, "maximumFee": 10**15, "transferFeeBasisPoints": bps}}


def ext(**kw):
    e = clean_extensions()
    e.update(kw)
    return e


def run(cfg, rug):
    g = scout.safety_gates(rug, T0, make_pair("M"), T0, cfg)
    return combine(g), {x.name: x for x in g}


def test_clean_token2022_allows(cfg):
    v, g = run(cfg, make_rug("M"))
    assert v.state == ALLOW and g["transfer_fee"].state == ALLOW and g["transfer_restriction"].state == ALLOW


# ---- transfer fee -----------------------------------------------------------------------
def test_fee_0_bps_authority_revoked_allows(cfg):
    v, g = run(cfg, make_rug("M", ext=ext(transferFeeConfig=fee_cfg(0))))
    assert v.state == ALLOW and g["transfer_fee"].state == ALLOW


def test_fee_300_bps_blocks(cfg):  # TRUMPDOLLAR shape (snapshots 589/616)
    v, g = run(cfg, make_rug("M", ext=ext(transferFeeConfig=fee_cfg(300))))
    assert v.state == BLOCK and v.name == "transfer_fee" and g["transfer_fee"].value == 300


def test_fee_on_older_schedule_only_blocks(cfg):
    v, _ = run(cfg, make_rug("M", ext=ext(transferFeeConfig=fee_cfg(0, older=50))))
    assert v.state == BLOCK and v.name == "transfer_fee"


def test_fee_authority_set_blocks_even_at_0_bps(cfg):
    v, _ = run(cfg, make_rug("M", ext=ext(transferFeeConfig=fee_cfg(0, cfg_auth=FEE_AUTH))))
    assert v.state == BLOCK and v.name == "transfer_fee" and "authority" in v.reason


def test_simple_transferfee_field_is_ignored(cfg):
    # RugCheck's top-level transferFee.pct reads 0 for a 300 bps token: the extension wins
    rug = make_rug("M", ext=ext(transferFeeConfig=fee_cfg(300)))
    rug["transferFee"] = {"pct": 0, "maxAmount": 0, "authority": "11111111111111111111111111111111"}
    assert run(cfg, rug)[0].state == BLOCK
    clean = make_rug("M")
    clean["transferFee"] = {"pct": 5, "maxAmount": 1, "authority": FEE_AUTH}
    assert run(cfg, clean)[0].state == ALLOW


def test_missing_token_extensions_is_insufficient(cfg):
    rug = make_rug("M")
    rug.pop("token_extensions")
    v, _ = run(cfg, rug)
    assert v.state == INSUFFICIENT and v.name == "token2022_ext"
    rug["token_extensions"] = None
    assert run(cfg, rug)[0].state == INSUFFICIENT


def test_missing_transfer_fee_field_is_insufficient(cfg):
    e = clean_extensions(); e.pop("transferFeeConfig")
    v, _ = run(cfg, make_rug("M", ext=e))
    assert v.state == INSUFFICIENT and v.name == "token2022_ext"


@pytest.mark.parametrize("drop", ["newerTransferFee", "transferFeeConfigAuthority"])
def test_missing_fee_subfield_is_insufficient(cfg, drop):
    f = fee_cfg(0); f.pop(drop)
    assert run(cfg, make_rug("M", ext=ext(transferFeeConfig=f)))[0].state == INSUFFICIENT


def test_garbage_fee_bps_is_insufficient(cfg):
    f = fee_cfg(0); f["newerTransferFee"]["transferFeeBasisPoints"] = "NaN"
    assert run(cfg, make_rug("M", ext=ext(transferFeeConfig=f)))[0].state == INSUFFICIENT


def test_legacy_spl_unchanged(cfg):
    v, g = run(cfg, make_rug("M", program=SPL_TOKEN))
    assert v.state == ALLOW and g["transfer_fee"].state == ALLOW and "legacy" in g["transfer_fee"].reason


def test_unknown_token_program_is_insufficient(cfg):
    rug = make_rug("M"); rug.pop("tokenProgram")
    v, _ = run(cfg, rug)
    assert v.state == INSUFFICIENT and v.name == "token2022_ext"


# ---- transfer restrictions --------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("permanentDelegate", {"delegate": "Deleg111"}),
    ("permanentDelegate", "Deleg111"),
    ("transferHook", {"authority": "Auth111", "programId": "HookProg111"}),
    ("defaultAccountState", {"accountState": "frozen"}),
    ("defaultAccountState", "frozen"),
    ("nonTransferable", True),
    ("pausableConfig", {"authority": "Pause111", "paused": False}),
    ("mintCloseAuthority", {"closeAuthority": "Close111"}),
    ("mintCloseAuthority", "Close111"),
])
def test_each_restriction_extension_blocks(cfg, field, value):
    v, _ = run(cfg, make_rug("M", ext=ext(**{field: value})))
    assert v.state == BLOCK and v.name == "transfer_restriction" and field in v.reason


@pytest.mark.parametrize("pid", [None, "", "11111111111111111111111111111111"])
def test_transfer_hook_without_program_id_allows(cfg, pid):
    v, g = run(cfg, make_rug("M", ext=ext(transferHook={"authority": "Auth111", "programId": pid})))
    assert v.state == ALLOW and g["transfer_restriction"].state == ALLOW


@pytest.mark.parametrize("field,value", [
    ("defaultAccountState", {"accountState": "initialized"}),
    ("permanentDelegate", {"delegate": None}),
    ("pausableConfig", {"authority": None, "paused": False}),
    ("mintCloseAuthority", {"closeAuthority": None}),
])
def test_restriction_extension_present_but_unset_allows(cfg, field, value):
    assert run(cfg, make_rug("M", ext=ext(**{field: value})))[0].state == ALLOW


@pytest.mark.parametrize("drop", ["permanentDelegate", "transferHook", "defaultAccountState",
                                  "nonTransferable", "pausableConfig", "mintCloseAuthority"])
def test_missing_restriction_field_is_insufficient(cfg, drop):
    e = clean_extensions(); e.pop(drop)
    v, _ = run(cfg, make_rug("M", ext=e))
    assert v.state == INSUFFICIENT and v.name == "token2022_ext" and drop in v.reason


def test_transfer_hook_missing_program_id_is_insufficient(cfg):
    v, _ = run(cfg, make_rug("M", ext=ext(transferHook={"authority": "Auth111"})))
    assert v.state == INSUFFICIENT and v.name == "token2022_ext"


def test_baseline_book_stays_unfiltered(cfg, conn, clock, feed):
    """A fee/hook token is blocked by Scout but the no-filter baseline may still buy it."""
    from memedesk.engine import run_cycle
    from conftest import good_token
    m = good_token(feed, "FEEMINT")
    feed.rugs[m] = make_rug(m, ext=ext(transferFeeConfig=fee_cfg(300),
                                       transferHook={"authority": "A", "programId": "HookProg111"}))
    run_cycle(conn, cfg, feed, clock=clock)
    scout_rows = conn.execute("SELECT state, reason_code FROM decisions WHERE book='desk' AND stage='scout' AND mint=?", (m,)).fetchall()
    assert scout_rows and all(r[0] == BLOCK and r[1] in ("transfer_fee", "transfer_restriction") for r in scout_rows)
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE book='baseline' AND mint=?", (m,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM positions WHERE book='desk' AND mint=?", (m,)).fetchone()[0] == 0
