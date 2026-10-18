"""本地 Tushare 财报只读映射；保留每次公告，不把修订结果提前到原公告日。"""
from __future__ import annotations

import hashlib
import json
from datetime import date

import polars as pl

from app.config import settings
from app.plugins.local_parquet.provider import source_path
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

# 金额为元，每股指标为元/股，比率为百分点；不把 ROA 偷换成净利润/总资产。
TABLES = {
    'income': ('income', {
        'revenue': 'revenue', 'total_revenue': 'total_revenue', 'oper_cost': 'operating_cost',
        'operate_profit': 'operating_profit', 'total_profit': 'total_profit',
        'n_income': 'net_income', 'n_income_attr_p': 'net_income_attributable',
        'basic_eps': 'basic_eps', 'diluted_eps': 'diluted_eps',
    }),
    'balance_sheet': ('balancesheet', {
        'total_assets': 'total_assets', 'total_cur_assets': 'total_current_assets',
        'total_cur_liab': 'total_current_liabilities', 'total_liab': 'total_liabilities',
        'total_hldr_eqy_inc_min_int': 'total_equity', 'money_cap': 'cash_and_equivalents',
        'accounts_receiv': 'accounts_receivable', 'inventories': 'inventory',
        'total_hldr_eqy_exc_min_int': 'equity_attributable',
    }),
    'cash_flow': ('cashflow', {
        'n_cashflow_act': 'net_operating_cash_flow', 'n_cashflow_inv_act': 'net_investing_cash_flow',
        'n_cash_flows_fnc_act': 'net_financing_cash_flow', 'free_cashflow': 'free_cash_flow',
        'c_cash_equ_beg_period': 'cash_beginning', 'c_cash_equ_end_period': 'cash_ending',
    }),
    'metrics': ('fina_indicator', {
        'eps': 'eps_basic', 'bps': 'bps', 'ocfps': 'ocfps', 'roe': 'roe', 'roa': 'roa',
        'grossprofit_margin': 'gross_margin', 'netprofit_margin': 'net_margin',
        'or_yoy': 'revenue_yoy', 'netprofit_yoy': 'net_income_yoy',
        'debt_to_assets': 'debt_to_asset_ratio',
    }),
}


def read_financial(table, symbols=None, *, latest_only=False):
    if table == 'shares':
        # 历史股本已经由按日 daily_basic 导入；不能用最新股本覆盖。
        path = settings.data_dir / 'financials/shares/part.parquet'
        frame = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    else:
        if table not in TABLES:
            raise ValueError(f'不支持的财务表：{table}')
        name, fields = TABLES[table]
        path = source_path().with_name(name + '.parquet')
        if not path.exists():
            raise ValueError(f'缺少本地财务表：{path.name}；不会在线回退')
        raw = pl.read_parquet(path)
        required = {'ts_code', 'ann_date', 'end_date'}
        if not required <= set(raw.columns):
            raise ValueError(f'{name} 缺少日期或代码列')
        if 'report_type' in raw.columns:
            raw = raw.filter(pl.col('report_type') == '1')  # 合并累计原始报表
        if symbols is not None:
            raw = raw.filter(pl.col('ts_code').is_in(symbols))
        # 实际公告日优先，缺失才用公告日；不拿报告期当公告日。
        announcement = pl.coalesce('f_ann_date', 'ann_date') if 'f_ann_date' in raw.columns else pl.col('ann_date')
        frame = raw.select(
            pl.col('ts_code').alias('symbol'),
            pl.col('end_date').str.strptime(pl.Date, '%Y%m%d').alias('period_end'),
            announcement.str.strptime(pl.Date, '%Y%m%d').alias('announce_date'),
            *[(pl.col(src).cast(pl.Float64) if src in raw.columns else pl.lit(None, dtype=pl.Float64))
              .alias(dst) for src, dst in fields.items()],
        )
        if frame.select(pl.any_horizontal(pl.col('symbol', 'period_end', 'announce_date').is_null()).any()).item():
            raise ValueError(f'{name} 含缺失公告日或代码，未导入')
        frame = frame.filter(pl.col('announce_date') >= pl.col('period_end'))
        frame = frame.filter(pl.col('announce_date') <= date.today()).with_columns(
            pl.col('period_end', 'announce_date').dt.strftime('%Y-%m-%d'),
            pl.lit('local_tushare').alias('source'),
        )
        numeric = list(fields.values())
        frame = frame.with_columns([
            pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in numeric
        ]).unique(maintain_order=True)
        if frame.select(pl.struct('symbol', 'period_end', 'announce_date').is_duplicated().any()).item():
            raise ValueError(f'{name} 同次公告存在冲突版本，未自动挑选')
    if table == 'metrics' and not frame.is_empty():
        # 指标原表缺资产负债率：只取同报告期且当时已经公告的资产负债表。
        balance = read_financial('balance_sheet', symbols)
        if not balance.is_empty():
            right = balance.select('symbol', 'period_end',
                pl.col('announce_date').str.to_date().alias('_bs_date'),
                pl.when(pl.col('total_assets') > 0)
                  .then(100 * pl.col('total_liabilities') / pl.col('total_assets'))
                  .otherwise(None).alias('_debt_ratio')).sort('_bs_date')
            frame = frame.with_columns(pl.col('announce_date').str.to_date().alias('_metric_date'))
            frame = frame.sort('_metric_date').join_asof(
                right, left_on='_metric_date', right_on='_bs_date',
                by=['symbol', 'period_end'], strategy='backward', check_sortedness=False,
            ).with_columns(pl.coalesce('debt_to_asset_ratio', '_debt_ratio').alias('debt_to_asset_ratio'))
            frame = frame.drop('_metric_date', '_bs_date', '_debt_ratio')
    if frame.is_empty():
        return frame
    if symbols is not None:
        frame = frame.filter(pl.col('symbol').is_in(symbols))
    frame = frame.sort(['symbol', 'period_end', 'announce_date'], descending=[False, True, True])
    return frame.unique('symbol', keep='first', maintain_order=True) if latest_only else frame


def import_financials():
    if not settings.personal_local_only:
        raise ValueError('此导入仅用于隔离个人试用')
    frames = {table: read_financial(table) for table in TABLES}  # 全部先校验，再写工作台副本
    sources = {table: source_path().with_name(name + '.parquet') for table, (name, _) in TABLES.items()}
    hashes = {table: hashlib.sha256(path.read_bytes()).hexdigest() for table, path in sources.items()}
    excluded = {}
    for table, path in sources.items():
        raw = pl.read_parquet(path)
        announcement = pl.coalesce('f_ann_date', 'ann_date') if 'f_ann_date' in raw.columns else pl.col('ann_date')
        bad = raw.filter(announcement < pl.col('end_date'))
        excluded[table] = bad.select('ts_code', 'ann_date', 'end_date').to_dicts()
    result = {'excluded_invalid_dates': excluded, 'tables': {}, 'units': '金额元；每股元/股；比率百分点',
              'source_sha256': hashes, 'source_unchanged': False,
              'pit_limit': '公告版本保留；供应商未留存的早期原始版本无法还原，尚未验收财务因子历史回测。'}
    for table, frame in frames.items():
        atomic_write_parquet(frame, settings.data_dir / 'financials' / table / 'part.parquet')
        result['tables'][table] = {
            'rows': frame.height, 'symbols': frame['symbol'].n_unique(),
            'period_start': frame['period_end'].min(), 'period_end': frame['period_end'].max(),
            'announcement_end': frame['announce_date'].max(),
            'multiple_versions': frame.height - frame.unique(['symbol', 'period_end']).height,
        }
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[table] for table, path in sources.items()):
        raise ValueError('导入期间原始财务文件发生变化，请重试核验')
    result['source_unchanged'] = True
    from app.services import preferences
    preferences.save({'financial_data_provider': 'local_parquet'})
    atomic_write_text(settings.data_dir / 'personal_import_financial.json', json.dumps(result, ensure_ascii=False, indent=2))
    return result
