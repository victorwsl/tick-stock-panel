import json
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.personal_factor_research import router
from app.services import personal_factor_research as research


@pytest.fixture()
def study(tmp_path):
    short, long, data = (tmp_path / p for p in ("short", "long", "data"))
    (short / "factors").mkdir(parents=True)
    long.mkdir()
    dates = ["20250102", "20250103", "20250701", "20250702"]
    keys = [(d, f"s{i:02}", i) for d in dates for i in range(30)]
    factors = pl.DataFrame([dict(trade_date=d, ts_code=s, factor=float(i)) for d, s, i in keys])
    factors.write_parquet(short / "factors/x.parquet")
    labels = pl.DataFrame([dict(trade_date=d, ts_code=s,
                               **{f"excess_{h}d": i / 100 if h != 250 or d != dates[-1] else None for h in research.HORIZONS},
                               label_end_250d="20260102" if d != dates[-1] else "") for d, s, i in keys])
    labels.write_parquet(long / "labels.parquet")
    ranks = pl.DataFrame([dict(trade_date=d, ts_code=s, variant_id=v, rank=(i+1 if v == "baseline" else 30-i),
                               period="p1" if d < "20250701" else "p2")
                          for d, s, i in keys for v in ("baseline", "soft5_x")])
    ranks.write_parquet(short / "ranked_candidates.parquet")
    expected = pl.DataFrame([dict(trade_date=d, factor_id="x", horizon=h, rank_ic=1., rows=30)
                             for d in dates for h in research.HORIZONS if h != 250 or d != dates[-1]])
    expected.write_csv(long / "daily_ic.csv")
    summary = pl.DataFrame([dict(cohort=cohort, period=p, variant_id=v, k=k, horizon=h,
                                paired_dates=1 if h == 250 and p == "p2" else 2,
                                nonoverlap_intervals=1, uplift=0. if v == "baseline" else .3-k*.01,
                                p_holm=None, inference="descriptive_only_short_history")
                            for cohort in ("own_mature", "common_250d_mature") for p in ("p1", "p2")
                            for v in ("baseline", "soft5_x") for k in (1,3,5) for h in research.HORIZONS])
    summary.write_csv(long / "ranking_summary.csv")
    pl.DataFrame({"period":["p1"], "horizon":[10]}).write_csv(long / "maturity.csv")
    pl.DataFrame({"horizon":[250], "status":["immature"], "rows":[30]}).write_csv(long / "label_audit.csv")
    pl.DataFrame([dict(trade_date=d,period=p,variant_id=v,nav=1.,drawdown=0.)
                  for d,p in zip(dates,["p1","p1","p2","p2"],strict=True) for v in ("baseline","soft5_x")]).write_parquet(short / "equity_daily.parquet")
    pl.DataFrame({"status":["passed"]}).write_csv(short / "curve_audit.csv")
    (short / "resolved_spec.json").write_text(json.dumps({"factors":["x"],"definitions":{"x":["测试因子","i"]},"bundles":{}}))
    (short / "manifest.json").write_text(json.dumps({"outputs":{str(p.relative_to(short)):research._sha(p) for p in short.rglob('*') if p.is_file()}}))
    (long / "label_reconciliation.json").write_text('{"passed":true}')
    (long / "manifest.json").write_text(json.dumps({"data_cutoff":"20261016", "outputs":{p.name:research._sha(p) for p in long.iterdir() if p.is_file()}, "inputs":{str((short/p).resolve()):research._sha(short/p) for p in ("ranked_candidates.parquet","factors/x.parquet")}}))
    research.import_study(data, short, long, "test")
    return data, short, long


def test_panel_ic_and_topk_reconciliation(study):
    data, _, _ = study
    result = research.verify(data,"test")
    assert result["status"] == "passed"
    assert result["ic_comparisons"] == 19
    assert result["ranking_comparisons"] == 60
    assert result["max_ic_delta"] < 1e-10
    assert research.list_studies(data)[0]["verification"] == result
    rows = research.summary(data,"test",250,3,"own_mature")["rows"]
    assert rows[0]["uplift"] == pytest.approx(.27)


def test_no_long_curve_or_top1_curve_substitution(study):
    data, _, _ = study
    assert research.detail(data,"test","x",10,3,"own_mature","p1")["curves"]
    assert not research.detail(data,"test","x",120,3,"own_mature","p1")["curves"]
    assert not research.detail(data,"test","x",10,1,"own_mature","p1")["curves"]
    assert not research.detail(data,"test","x",10,3,"common_250d_mature","p1")["curves"]


def test_common_dates_exclude_immature_250_without_price_survivor_filter(study):
    data, _, _ = study
    all_ic = research.detail(data,"test","x",10,3,"own_mature","p2")["ic"]
    common_ic = research.detail(data,"test","x",10,3,"common_250d_mature","p2")["ic"]
    assert len(all_ic) == 2 and len(common_ic) == 1


def test_tampered_bundle_rejected(study):
    data, _, _ = study
    path = research.study_path(data,"test")
    with (path / "labels.parquet").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="校验失败"):
        research.verify(data,"test")


def test_no_overwrite_and_wrong_source_link_rejected(study):
    data, short, long = study
    with pytest.raises(ValueError, match="已存在"):
        research.import_study(data,short,long,"test")
    receipt = json.loads((long/'manifest.json').read_text())
    receipt['inputs'] = {}
    (long/'manifest.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="同一冻结"):
        research.import_study(data,short,long,"other")
    assert not (data/'research/factors/other').exists()


def test_path_escape_and_missing_data(tmp_path):
    with pytest.raises(ValueError):
        research.study_path(tmp_path,"../outside")
    assert research.list_studies(tmp_path) == []


def test_api_success_invalid_parameters_and_missing(study):
    data, _, _ = study
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data))
    with TestClient(app) as client:
        assert client.get('/api/custom/factor-research').json()['studies'][0]['id'] == 'test'
        assert client.get('/api/custom/factor-research/test?horizon=250&k=3').status_code == 200
        assert client.get('/api/custom/factor-research/test?horizon=999').status_code == 422
        assert client.get('/api/custom/factor-research/test?cohort=unknown').status_code == 422
        assert client.get('/api/custom/factor-research/missing').status_code == 404
        assert client.get('/api/custom/factor-research/test/detail?factor=unknown&period=p1').status_code == 400
        assert client.post('/api/custom/factor-research/test/verify').json()['status'] == 'passed'


@pytest.mark.parametrize("change", ["missing_group", "undefined_stat"])
def test_topk_reconciliation_rejects_missing_groups_and_statistics(study, change):
    data, _, _ = study
    path = research.study_path(data, "test")
    summary = pl.read_parquet(path / "ranking_summary.parquet")
    target = ((pl.col("cohort") == "own_mature") & (pl.col("horizon") == 10)
              & (pl.col("k") == 1) & (pl.col("period") == "p1")
              & (pl.col("variant_id") == "soft5_x"))
    if change == "missing_group":
        summary = summary.filter(~target)
    else:
        summary = summary.with_columns(pl.when(target).then(None).otherwise(pl.col("uplift")).alias("uplift"))
    summary.write_parquet(path / "ranking_summary.parquet")
    # Keep the bundle valid so the independent statistical check must catch the error.
    spec = json.loads((path / "study.json").read_text())
    spec["files"]["ranking_summary.parquet"] = research._sha(path / "ranking_summary.parquet")
    (path / "study.json").write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="TopK增量"):
        research.verify(data, "test")
