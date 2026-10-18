import pandas as pd
import pytest

from app.backtest.graham_distributions import DistributionLedger


def position(factor):
    return {"X": dict(adj_factor=factor, last_bar_date="20200101", position_id="X:1",
                       qty=1000, pending_qty=0, proceeds=0.)}


@pytest.mark.parametrize("previous,current", [(12.081,12.0807),(1.0068,1.007),(1.4225,1.423)])
def test_declared_precision_records_equivalent_factors_without_cash_or_share_changes(previous,current):
    journal=[]
    ledger=DistributionLedger(pd.DataFrame(),journal,factor_decimals=3)
    holdings=position(previous)
    ledger.open("20200102",holdings,{"X":{"adj_factor":current}})
    assert holdings["X"]["qty"]==1000
    assert holdings["X"]["proceeds"]==0
    assert holdings["X"]["adj_factor"]==current
    assert journal[0]["event"]=="factor_precision_match"
    assert journal[0]["previous_factor"]==previous
    assert journal[0]["factor"]==current
    assert journal[0]["cash_delta"]==journal[0]["qty_delta"]==0


def test_missing_precision_declaration_preserves_strict_rejection():
    ledger=DistributionLedger(pd.DataFrame(),[])
    with pytest.raises(ValueError,match="Unexplained adjustment"):
        ledger.open("20200102",position(12.081),{"X":{"adj_factor":12.0807}})


def test_material_unexplained_change_still_rejected():
    ledger=DistributionLedger(pd.DataFrame(),[],factor_decimals=3)
    with pytest.raises(ValueError,match="Unexplained adjustment"):
        ledger.open("20200102",position(1.0068),{"X":{"adj_factor":1.02}})


def test_small_cash_dividend_keeps_full_precision_even_when_factor_key_unchanged():
    events=pd.DataFrame([dict(ts_code="X",div_proc="实施",record_date="20200101",
        ex_date="20200102",pay_date="20200102",div_listdate=None,
        cash_div_tax=.00012345,stk_div=0.,stk_bo_rate=0.)])
    journal=[]
    ledger=DistributionLedger(events,journal,factor_decimals=3)
    holdings=position(1.)
    ledger.record_close("20200101",holdings)
    ledger.open("20200102",holdings,{"X":{"adj_factor":1.000012345}})
    assert holdings["X"]["qty"]==1000
    assert holdings["X"]["proceeds"]==pytest.approx(.09876,abs=1e-12)
    assert ledger.pay_close("20200102")==pytest.approx(.09876,abs=1e-12)
    accrual=next(row for row in journal if row["event"]=="distribution_accrual")
    assert accrual["gross_dividend"]==pytest.approx(.12345,abs=1e-12)
    assert accrual["tax_reserve"]==pytest.approx(.02469,abs=1e-12)


def stock_entitlement(quantity=1500,rate=.5005427,policy="reject"):
    events=pd.DataFrame([dict(ts_code="X",div_proc="实施",record_date="20200101",
        ex_date="20200102",pay_date="20200102",div_listdate="20200102",
        cash_div_tax=.048245,stk_div=rate,stk_bo_rate=0.)])
    journal=[]
    ledger=DistributionLedger(events,journal,fractional_policy=policy)
    holdings=position(1.)
    holdings["X"]["qty"]=quantity
    ledger.record_close("20200101",holdings)
    return ledger,holdings,journal


def test_default_rejects_fractional_entitlement_for_large_holdings_too():
    ledger,holdings,_=stock_entitlement(quantity=150000)
    with pytest.raises(ValueError,match="Fractional stock distribution"):
        ledger.open("20200102",holdings,{"X":{"adj_factor":1.5}})


def test_explicit_floor_policy_records_remainder_without_cash_compensation():
    ledger,holdings,journal=stock_entitlement(policy="floor")
    ledger.open("20200102",holdings,{"X":{"adj_factor":1.5}})
    assert holdings["X"]["qty"]==2250
    assert holdings["X"]["pending_qty"]==0
    remainder=next(row for row in journal if row["event"]=="fractional_share_discard")
    assert remainder["discarded_qty"]==pytest.approx(.81405,abs=1e-10)
    assert remainder["allocated_qty"]==750
    assert remainder["cash_delta"]==remainder["qty_delta"]==0
    assert ledger.pay_close("20200102")==pytest.approx(57.894,abs=1e-10)


def test_floor_keeps_exact_integer_entitlement_despite_float_representation():
    import numpy as np
    ledger,holdings,journal=stock_entitlement(quantity=4000,rate=np.nextafter(.175,0),policy="floor")
    ledger.open("20200102",holdings,{"X":{"adj_factor":1.175}})
    assert holdings["X"]["qty"]==4700
    assert not any(row["event"]=="fractional_share_discard" for row in journal)


def test_input_policy_changes_invalidate_cache_and_are_hashed(tmp_path):
    import json
    from app.services.personal_graham import load_inputs

    frames={
        "daily":pd.DataFrame([dict(ts_code="X",trade_date="20210104",amount=1.)]),
        "basic":pd.DataFrame([dict(ts_code="X",trade_date="20210104")]),
        "names":pd.DataFrame([dict(ts_code="X",start_date="20200101")]),
        "calendar":pd.DataFrame([dict(trade_date="20210104",is_open=1)]),
    }
    for key in ["income","balance","cashflow"]:
        frames[key]=pd.DataFrame([dict(ts_code="X",report_type="1",ann_date="20210301",end_date="20201231")])
    paths={}
    for key,frame in frames.items():
        path=tmp_path/(key+".parquet")
        frame.to_parquet(path,index=False)
        paths[key]=str(path)
    folder=tmp_path/"graham_inputs"
    folder.mkdir()
    config=folder/"sources.json"
    config.write_text(json.dumps(dict(paths=paths)))
    first=load_inputs(tmp_path)
    assert first["factor_decimals"] is None
    assert first["fractional_policy"]=="reject"
    config.write_text(json.dumps(dict(paths=paths,factor_decimals=3,fractional_policy="floor")))
    second=load_inputs(tmp_path)
    assert second is not first
    assert second["factor_decimals"]==3
    assert second["fractional_policy"]=="floor"
    assert first["input_hashes"]["sources_config"]!=second["input_hashes"]["sources_config"]
