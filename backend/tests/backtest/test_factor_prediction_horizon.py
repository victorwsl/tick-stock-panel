from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from app.api.backtest import FactorBacktestRequest, FactorBatchRequest
from app.backtest.factor import FactorBacktestService, FactorBatchConfig, FactorConfig


def market(n=340):
    dates=[]
    day=date(2023, 1, 2)
    while len(dates) < n:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    rows=[]
    for index, day in enumerate(dates):
        for stock in range(8):
            close=float(10 * np.exp((.0002 + stock*.0001)*index + .025*np.sin(index/17 + stock)))
            rows.append({'symbol': str(stock), 'date': day, 'open': close*1.02,
                         'high': close*1.03, 'low': close*.99, 'close': close,
                         'volume': 1000., 'amount': 10000., 'turnover_rate': float(stock)})
    return pl.DataFrame(rows), dates


class Engine:
    def __init__(self, panel):
        self.panel=panel
        self.calls=0

    def load_panel(self, symbols, start, end, columns, asset_type):
        self.calls += 1
        return self.panel.filter(pl.col('date').is_between(start, end)).select(columns)


def config(dates, h=60, rebalance='monthly'):
    return FactorConfig(factor_name='turnover_rate', symbols=None, start=dates[0], end=dates[-1],
                        rebalance=rebalance, n_groups=5, prediction_horizon=h)


@pytest.mark.parametrize('h', [20,60,250])
def test_next_open_label_matches_hand_calculation_and_is_mature(h):
    panel, dates=market()
    labeled=FactorBacktestService._attach_prediction_returns(panel, config(dates,h), trading_dates=dates)
    stock=labeled.filter(pl.col('symbol')=='0').sort('date')
    assert stock[f'_prediction_return_{h}d'][0] == pytest.approx(stock['close'][h]/stock['open'][1]-1)
    assert stock[f'_prediction_return_{h}d'].null_count() == h
    assert stock[f'_prediction_return_{h}d'][-1] is None


def test_missing_entry_does_not_shift_to_next_available_bar():
    panel,dates=market()
    panel=panel.filter(~((pl.col('symbol')=='0') & (pl.col('date')==dates[1])))
    result=FactorBacktestService._attach_prediction_returns(panel,config(dates),trading_dates=dates)
    assert result.filter((pl.col('symbol')=='0') & (pl.col('date')==dates[0]))['_prediction_return_60d'][0] is None
    assert result.filter((pl.col('symbol')=='1') & (pl.col('date')==dates[0]))['_prediction_return_60d'][0] is not None


def test_horizon_ic_is_independent_of_rebalance_and_quantiles_are_not_compounded():
    panel,dates=market(160)
    service=FactorBacktestService(Engine(panel))
    monthly=service.run(config(dates))
    daily=service.run(config(dates,rebalance='daily'))
    assert monthly.ic_series == daily.ic_series
    assert monthly.ic_mean == daily.ic_mean
    assert monthly.prediction_audit['n_ic_dates'] == 100
    assert monthly.prediction_audit['mature_rows'] == 8*100
    assert monthly.prediction_audit['nonoverlap_intervals'] == 2
    assert monthly.prediction_audit['inference_status'] == 'descriptive_only'
    assert len(monthly.horizon_quantiles) == 5
    assert [item['horizon'] for item in monthly.ic_decay] == [20,60,250]
    assert monthly.config['prediction_horizon'] == 60
    assert monthly.group_nav != daily.group_nav


def test_batch_preserves_horizon_and_does_not_fall_back_to_naive_significance():
    panel,dates=market(90)
    engine=Engine(panel)
    batch=FactorBacktestService(engine).run_batch(FactorBatchConfig(
        factor_names=['turnover_rate'],symbols=None,start=dates[0],end=dates[-1],prediction_horizon=60))
    item=batch.results[0]
    assert item.error is None
    assert item.prediction_audit['n_ic_dates'] == 30
    assert item.t_naive is not None
    assert item.t_newey_west is None
    assert item.p_value is None
    assert item.q_value is None
    assert batch.config['prediction_horizon'] == 60
    assert engine.calls == 1


def test_batch_hac_lag_tracks_selected_horizon():
    panel,dates=market(340)
    batch=FactorBacktestService(Engine(panel)).run_batch(FactorBatchConfig(
        factor_names=['turnover_rate'],symbols=None,start=dates[0],end=dates[-1],prediction_horizon=60))
    assert batch.results[0].nw_lag == 59
    assert batch.results[0].t_newey_west is not None


def test_immature_annual_labels_stay_empty_and_truncation_does_not_leak():
    panel,dates=market(120)
    cfg=config(dates,250)
    result=FactorBacktestService(Engine(panel)).run(cfg)
    assert result.ic_mean is None
    assert result.ic_series == []
    assert result.horizon_quantiles == []
    assert result.prediction_audit['mature_rows'] == 0
    short=config(dates[:80],60)
    labeled=FactorBacktestService._attach_prediction_returns(panel,short,trading_dates=dates)
    assert labeled.filter(pl.col('date')>dates[19])['_prediction_return_60d'].null_count() == labeled.filter(pl.col('date')>dates[19]).height


def test_old_config_and_request_keep_legacy_behavior():
    panel,dates=market(90)
    cfg=replace(config(dates),prediction_horizon=None)
    result=FactorBacktestService(Engine(panel)).run(cfg)
    assert result.prediction_audit == {}
    assert result.config['prediction_horizon'] is None
    assert [item['horizon'] for item in result.ic_decay] == [1,3,5]
    assert FactorBacktestRequest(factor_name='turnover_rate').prediction_horizon is None
    assert FactorBatchRequest(factor_names=['turnover_rate'],prediction_horizon=250).prediction_horizon == 250
    with pytest.raises(ValidationError):
        FactorBatchRequest(factor_names=['turnover_rate'],prediction_horizon=21)
