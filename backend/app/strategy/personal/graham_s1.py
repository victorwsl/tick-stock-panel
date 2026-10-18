"""Graham研究策略；PIT输入与月度组合由Panel统一研究接线提供。"""
import polars as pl

META = {'id': 'custom_graham_s1', 'name': 'S1 Graham净流动资产〔月调研究〕', 'description': 'NCAV=流动资产-总负债-少数股东权益；总市值≤NCAV×2/3；按NCAV/市值排序。优先权益未核验，严格模式缺字段不入选。 月末信号、次日开盘，20槽每槽5%，不足留现金；保留持仓按月调整目标权重。研究版：财报版本与多股类覆盖未验收，不承诺年化50%。', 'tags': ['个人研究', 'Graham', '月度调仓', '数据限制已披露'], 'asset_types': ['stock'], 'timeframes': ['1d'], 'personal_graham_variant': 's1', 'params': [{'id': 'strict_preferred', 'label': '严格优先权益（缺字段不入选）', 'type': 'bool', 'default': False}], 'scoring': {}, 'order_by': 'score', 'descending': True, 'limit': 20}
EXECUTION_BACKEND = "polars_expr"
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = ["graham_s1_selected"]
EXIT_SIGNALS = ["graham_monthly_removed"]
STOP_LOSS = None
MAX_HOLD_DAYS = None
RULES = """
1. NCAV=流动资产-总负债-少数股东权益；总市值≤NCAV×2/3；按NCAV/市值排序。优先权益未核验，严格模式缺字段不入选。
2. 仅使用信号日可得的本地历史市值、历史名称、已公告报表，禁止最新快照回填。
3. 日频估值、月度目标权重、次日开盘；停牌及限制价不假设能成交。
4. 原始价记账，分红及送转独立入账；期末未卖出只估值。
"""

def filter(df, params):
    if "graham_selected" not in df.columns:
        raise ValueError("请通过Panel的Graham研究接线运行，不能用未接财务的通用过滤器代替")
    return pl.col("graham_selected").fill_null(False)
