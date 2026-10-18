"""回调试仓的持仓规则；只在实际成交后启动三日时钟。"""
from dataclasses import dataclass

import numpy as np


@dataclass
class PullbackTradeRules:
    stop: np.ndarray
    target: np.ndarray
    white: np.ndarray
    max_risk: float = .03
    min_rr: float = 3.
    check_days: int = 3
    min_gain: float = .05

    def entry_terms(self, signal_time, asset, price, buy_cost, sell_cost):
        if signal_time < 0:
            return None, 'buy_planned_risk'
        stop = float(self.stop[signal_time, asset])
        target = float(self.target[signal_time, asset])
        cost = price * (1 + buy_cost)
        loss = cost - stop * (1 - sell_cost)
        reward = target * (1 - sell_cost) - cost
        risk = loss / cost if cost > 0 else np.nan
        if not (np.isfinite(stop) and np.isfinite(target) and 0 < stop < price and 0 < risk <= self.max_risk + 1e-8):
            return None, 'buy_planned_risk'
        rr = reward / loss
        if rr < self.min_rr:
            return None, 'buy_planned_rr'
        return dict(planned_stop=stop, planned_target=target, planned_rr=rr,
                    planned_risk_pct=risk, three_day_checked=False, below_white=False), ''

    def close_reason(self, pos, time_id, asset, close, tradable):
        if not tradable or not np.isfinite(close) or close <= 0:
            return None
        age = time_id - pos['entry_time']
        if age < self.check_days:
            return None
        if not pos['three_day_checked']:
            pos['three_day_checked'] = True
            # 5%是相对实际成交价的价格涨幅，不是扣费收益。
            if close / pos['entry_price'] - 1 < self.min_gain - 1e-7:
                return 'three_day_weak'
        white = float(self.white[time_id, asset])
        if not np.isfinite(white):
            return None
        below = close < white
        failed = pos['below_white'] and below
        pos['below_white'] = below
        return 'white_not_reclaimed' if failed else None
