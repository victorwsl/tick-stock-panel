"""趋势中回调试仓：不等待再转强，买后以三日强度筛选。"""
import numpy as np

from app.backtest.matrix import (
    make_signal_matrix,
    valid_rolling_max,
    valid_rolling_min,
    valid_rolling_sum,
    valid_shift,
)
from app.backtest.pullback_rules import PullbackTradeRules
from app.strategy.personal.zhixing_pullback import compute_features as baseline_features

META = {
    'id': 'personal_zhixing_early_v0',
    'name': '知行回调试仓 v0〔三日验证〕',
    'description': '上升趋势中J≤20且守住黄线时买，不等突破前日高点。次日开盘复算含成本风险≤3%、前高计划盈亏比≥3；结构止损。成交日D0，D3收盘涨幅<5%则D4开盘退出；通过后跌破白线，下一交易日仍未站回则再下一开盘退出。研究胜率50%及实际平均盈亏比≥3是目标，非保证。完整A股账户账本仍未验收。',
    'tags': ['个人研究', '知行', '三日验证', '未完成执行验收'],
    'asset_types': ['stock', 'etf'], 'timeframes': ['1d'],
    'params': [
        {'id': 'max_risk_pct', 'label': '含成本计划风险上限%', 'type': 'float', 'default': 3., 'min': 1., 'max': 5., 'step': .5},
        {'id': 'min_rr', 'label': '含成本计划盈亏比下限', 'type': 'float', 'default': 3., 'min': 2., 'max': 6., 'step': .5},
        {'id': 'volume_multiple', 'label': '近20日曾放量倍数', 'type': 'float', 'default': 2., 'min': 1., 'max': 4., 'step': .2},
    ],
    'scoring': {}, 'order_by': 'score', 'descending': True, 'limit': 50,
}
EXECUTION_BACKEND = 'matrix_native'
BASIC_FILTER = {'enabled': False}
ENTRY_SIGNALS = ['zx_early_pullback']
EXIT_SIGNALS = []  # 退出依赖实际持仓，不从候选信号日假造买入。
STOP_LOSS = None
MAX_HOLD_DAYS = None


def compute_features(market, params):
    f = baseline_features(market, params)
    valid = market.tradable.astype(bool) & np.isfinite(market.close) & (market.volume > 0)
    stop = valid_rolling_min(market.low, valid, 10) * .995
    c = market.close
    with np.errstate(divide='ignore', invalid='ignore'):
        risk = (c - stop) / c
        rr = (f['target'] - c) / (c - stop)
    previous = valid_shift(c, periods=1, valid_mask=valid)
    up = np.where(c > previous, market.volume, 0.)
    down = np.where(c < previous, market.volume, 0.)
    flow = (valid_rolling_sum(up, valid, 10) > valid_rolling_sum(down, valid, 10))
    flow &= valid_rolling_sum(up, valid, 20) > valid_rolling_sum(down, valid, 20)
    trend = (f['white'] > f['yellow']) & (f['yellow'] >= valid_shift(f['yellow'], periods=5, valid_mask=valid))
    burst = valid_rolling_max(f['volume_ratio'], valid, 20) >= float(params.get('volume_multiple', 2.))
    amplitude = valid_rolling_max(market.high, valid, 60) / valid_rolling_min(market.low, valid, 60) - 1
    entry = valid & trend & flow & burst & (amplitude < .8)
    entry &= (f['j'] <= 20) & (c >= f['yellow']) & (c <= f['white'] * 1.02)
    entry &= (risk > 0) & (risk <= float(params.get('max_risk_pct', 3.)) / 100)
    entry &= rr >= float(params.get('min_rr', 3.))
    return {**f, 'entry': entry, 'stop': stop, 'planned_rr': rr}


class ZhixingEarly:
    def required_fields(self):
        return frozenset({'open', 'high', 'low', 'close', 'volume'})

    def required_warmup_bars(self, params):
        return 200

    def compute_signals(self, market, params):
        entry = compute_features(market, params)['entry']
        return make_signal_matrix(market.shape, entry=entry.astype(np.uint8),
                                  entry_signal_ids=tuple(ENTRY_SIGNALS),
                                  entry_signal_code=np.where(entry, 0, -1).astype(np.int16))

    def build_pullback_trade_rules(self, market, params, start, stop):
        f = compute_features(market, params)
        return PullbackTradeRules(f['stop'][start:stop], f['target'][start:stop],
                                  f['white'][start:stop],
                                  max_risk=float(params.get('max_risk_pct', 3.)) / 100,
                                  min_rr=float(params.get('min_rr', 3.)))


MATRIX_STRATEGY = ZhixingEarly()
