"""知行回调再转强 v0：信号核验用，不代表完整分批执行系统。"""
import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    make_signal_matrix,
    valid_ewm_adjust_false,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_min,
    valid_rolling_sum,
    valid_shift,
)

META = {
    "id": "personal_zhixing_pullback_v0",
    "name": "知行回调再转强 v0〔信号试验〕",
    "description": "双EMA10白线、MA14/28/57/114黄线。回调后收复白线并突破前日高点，限制乖离和计划风险。注意：当前只验证信号及整仓退出；半仓预警仅记录，未模拟分批成交；开盘盈亏比复算和公司行动账本未验收。禁止据此实盘。",
    "tags": ["个人研究", "知行", "未完成执行验收"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {"id": "max_bias_pct", "label": "距白线最大乖离%", "type": "float", "default": 6., "min": 1., "max": 12., "step": 1.},
        {"id": "min_rr", "label": "收盘计划盈亏比下限（非成交值）", "type": "float", "default": 2., "min": 1., "max": 4., "step": .5},
        {"id": "max_risk_pct", "label": "计划止损距离上限%", "type": "float", "default": 8., "min": 2., "max": 12., "step": 1.},
        {"id": "volume_multiple", "label": "近20日曾有5均量/前5日60均量倍数", "type": "float", "default": 2., "min": 1., "max": 4., "step": .2},
    ],
    "scoring": {}, "order_by": "score", "descending": True, "limit": 50,
}
EXECUTION_BACKEND = "matrix_native"
BASIC_FILTER = {"enabled": False}  # 不偷偷叠加当前ST名称、市值或另一套评分。
ENTRY_SIGNALS = ["zx_pullback_restrength"]
EXIT_SIGNALS = ["zx_below_white"]
STOP_LOSS = None  # v0 不冒充已按入场计划挂出结构止损。
MAX_HOLD_DAYS = None


def compute_features(market, params):
    """所有窗口均只含当时及以前的有效交易日；输出可逐笔复查。"""
    c, h, lo, v = market.close, market.high, market.low, market.volume
    valid = market.tradable.astype(bool) & np.isfinite(c) & (v > 0)
    def shift(x, n=1):
        return valid_shift(x, periods=n, valid_mask=valid)
    def mean(x, n):
        return valid_rolling_mean(x, valid, n)
    def minimum(x, n):
        return valid_rolling_min(x, valid, n)
    def maximum(x, n):
        return valid_rolling_max(x, valid, n)
    def total(x, n):
        return valid_rolling_sum(x, valid, n)
    def ema(x, alpha):
        return valid_ewm_adjust_false(x, valid, alpha=alpha)
    white = ema(ema(c, 2/11), 2/11)
    yellow = sum(mean(c, n) for n in (14, 28, 57, 114)) / 4
    ln, hn = minimum(lo, 9), maximum(h, 9)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsv = np.where(hn > ln, (c-ln)/(hn-ln)*100, 50.)
    # K/D 以首个完整9日RSV为种子；114日预热后与早期播种差异已衰减。
    rsv = np.where(np.isfinite(hn), rsv, np.nan)
    k = ema(rsv, 1/3)
    d = ema(k, 1/3)
    j = 3*k-2*d
    prev = shift(c)
    up = np.where(c > prev, v, 0.)
    down = np.where(c < prev, v, 0.)
    with np.errstate(divide="ignore", invalid="ignore"):
        volume_ratio = mean(v, 5) / shift(mean(v, 60), 5)
        amplitude = maximum(h, 60) / minimum(lo, 60) - 1
    stop = shift(minimum(lo, 10)) * .995
    target = shift(maximum(h, 60))  # 只使用已知前高，不用未来最高点。
    risk = c-stop
    with np.errstate(divide="ignore", invalid="ignore"):
        rr = (target-c)/risk
        bias = c/white-1
        risk_pct = risk/c
    trend = (white > yellow) & (yellow >= shift(yellow, 5))
    pullback = minimum(j, 10) <= 20
    turn = (j > shift(j)) & (c > shift(h)) & (c > white) & (c > prev)
    flow = (total(up, 10) > total(down, 10)) & (total(up, 20) > total(down, 20))
    burst = maximum(volume_ratio, 20) >= float(params.get("volume_multiple", 2.))
    entry = valid & trend & pullback & turn & flow & burst & (amplitude < .8)
    entry &= (bias >= 0) & (bias <= float(params.get("max_bias_pct", 6.))/100)
    entry &= (risk > 0) & (risk_pct <= float(params.get("max_risk_pct", 8.))/100)
    entry &= (rr >= float(params.get("min_rr", 2.))) & (target > c)
    slope = white-shift(white, 3)
    top_heavy_bear = (h >= shift(maximum(h, 20))*.97) & (c < market.open) & (c < prev)
    top_heavy_bear &= v >= shift(mean(v, 20))*2
    consecutive_down = total((c < prev).astype(np.float32), 3) >= 3
    warning = ((slope < shift(slope)) & top_heavy_bear) | ((slope <= 0) & consecutive_down)
    return dict(white=white, yellow=yellow, j=j, stop=stop, target=target, planned_rr=rr,
                entry=entry, exit=valid & (c < white), trim_warning=valid & warning,
                volume_ratio=volume_ratio, bias=bias)


class ZhixingPullback:
    def required_fields(self):
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params):
        return 200  # 固定播种缓冲，不能把预热收益算进回测。

    def compute_signals(self, market: MarketDataMatrix, params: dict):
        features = compute_features(market, params)
        entry, exit_ = features["entry"], features["exit"]
        return make_signal_matrix(market.shape,
            entry=entry.astype(np.uint8), exit=exit_.astype(np.uint8),
            score=np.zeros(market.shape, dtype=np.float32),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=tuple(ENTRY_SIGNALS), exit_signal_ids=tuple(EXIT_SIGNALS))


MATRIX_STRATEGY = ZhixingPullback()
