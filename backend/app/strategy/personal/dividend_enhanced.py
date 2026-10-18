"""本地红利增强研究入口；不导入jqdata/jqfactor，不接受通用信号回退。"""
import polars as pl

META = {
    'id': 'custom_dividend_enhanced_daily_local',
    'name': '红利增强〔日频本地口径·非原版〕',
    'description': '月调：高股息低Beta＋红利价值，沪深300完成月线MACD切换1+4/4+1。Beta为本地252日OLS，财务为累计扣非ROE/同比，非聚宽等价口径。关闭11点开板与防御ETF，现金留存；保留旧仓不重置等权。原始价、次日开盘、共用现金与分红账本。请设5持仓、等权、次日开盘，清空额外覆盖及代码范围。',
    'tags': ['个人研究', '红利', '月调', '非原版等价'],
    'asset_types': ['stock'], 'timeframes': ['1d'],
    'personal_dividend_variant': 'daily_local',
    'params': [], 'scoring': {}, 'order_by': 'score', 'descending': True, 'limit': 5,
}
EXECUTION_BACKEND = 'polars_expr'
BASIC_FILTER = {'enabled': False}
ENTRY_SIGNALS = ['dividend_selected']
EXIT_SIGNALS = ['dividend_monthly_removed']
STOP_LOSS = None
MAX_HOLD_DAYS = None
RULES = '''
1. 规则冻结于本地resolved_spec.json，日频本地版不等于聚宽原版。
2. 低Beta路线先按股息率选前10%且>3%，再Beta较低半数，取1/4只；价值路线先筛财务，再按股息率选前10%且>3%，取4/1只。
3. 月线MACD只用完成月线，财务严格晚于公告日生效；不在数据缺失时跳过过滤。
4. 仅新票按目标数分配，旧持仓保留，未投现金不假设货币ETF收益。
5. 研究用明确本地因子和成交假设，不能宣传无未来偏差或养老金安全保证。
'''


def filter(df, params):
    if 'dividend_selected' not in df.columns:
        raise ValueError('红利策略需要本地PIT与月调接线，不允许通用信号回退')
    return pl.col('dividend_selected').fill_null(False)
