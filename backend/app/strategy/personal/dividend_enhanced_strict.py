"""原版核对入口：缺少等价数据时拒绝产生曲线。"""

META = {
    'id': 'custom_dividend_enhanced_strict',
    'name': '红利增强〔原版核对·数据待齐〕',
    'description': '原文件真实启用月线MACD、两路月调、昨日涨停保护、11:00开板处理及部分511880防御逻辑。平台Beta和单季度财务口径未对拍，盘中数据缺失；此入口明确阻止完整收益回测，不以日线或普通ROE替代。另有独立日频本地研究版本。',
    'tags': ['个人研究', '红利', '原版核对', '数据待齐'],
    'asset_types': ['stock'], 'timeframes': ['1d'],
    'personal_dividend_variant': 'strict',
    'params': [], 'scoring': {}, 'order_by': 'score', 'descending': True, 'limit': 5,
}
EXECUTION_BACKEND = 'polars_expr'
BASIC_FILTER = {'enabled': False}
ENTRY_SIGNALS = ['dividend_selected']
EXIT_SIGNALS = ['dividend_monthly_removed']
STOP_LOSS = None
MAX_HOLD_DAYS = None
RULES = '''
原版的动态数据、因子定义和盘中成交尚未对齐；在严格模式拒绝生成伪等价结果。
源码注释不作为执行规则：止损、动量、三年分红、熊市降仓默认关闭；11点开板处理已注册。
'''


def filter(df, params):
    raise ValueError('严格原版缺少已核对的数据与盘中成交能力，禁止通用回测回退')
