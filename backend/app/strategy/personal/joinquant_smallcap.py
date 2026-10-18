"""原聚宽小市值策略的日频本地适配，执行由已有现金/权益账本承担。"""
import polars as pl

META = {
    'id': 'custom_joinquant_smallcap',
    'name': '聚宽小市值20—30亿〔五日轮换〕',
    'description': '前收历史总市值20—30亿元，由小到大取开盘未停牌代理前三；每5交易日先清仓再买，仍入选也重新成交。无额外ST、盈利或成长过滤。日频本地适配，开盘缺报价代理停牌；此前成交额1%容量限制，与聚宽并非逐笔等同。',
    'tags': ['个人研究', '聚宽复现', '小市值', '五日调仓'],
    'asset_types': ['stock'], 'timeframes': ['1d'],
    'personal_joinquant_smallcap': True,
    'params': [{'id':'anchor_date','label':'首次调仓锚点（交易日）','type':'date','default':'2023-01-04'}],
    'scoring': {}, 'order_by':'score','descending':True,'limit':3,
}
EXECUTION_BACKEND = 'polars_expr'
BASIC_FILTER = {'enabled': False}
ENTRY_SIGNALS = ['jq_smallcap_selected']
EXIT_SIGNALS = ['jq_five_session_exit']
STOP_LOSS = None
MAX_HOLD_DAYS = None
RULES = '''
1. 以前一交易日总市值筛20—30亿元，市值升序、代码升序打破同分；不开当前股票列表回填历史。
2. 调仓锚点起每5个市场交易日开盘先全部卖出，再按卖出后空名额分配可用现金。
3. 开盘正报价代理未停牌，排序后过滤停牌代理取前三；涨停买不到不递补。
4. 卖不出占名额和现金；未成交订单当天取消，下一调仓周期再尝试，不每日强制重试。
5. 不读取当天收盘、高低、全日成交量批准开盘成交；原始价记账、最低佣金及权益单列。
'''


def filter(df, params):
    if 'jq_selected' not in df.columns:
        raise ValueError('请通过Panel聚宽复现接线运行，不能以最新市值过滤替代历史组合')
    return pl.col('jq_selected').fill_null(False)
