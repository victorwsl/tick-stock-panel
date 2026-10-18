"""只读本地行情和显式采集缓存，不联网，不用前复权价冒充成交价。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.data_providers.normalizer import normalize_daily

_NAMES = {
    '000001.SH': '上证指数', '000300.SH': '沪深300', '000510.SH': '中证A500',
    '000688.SH': '科创50', '000680.SH': '科创综指', '000852.SH': '中证1000',
    '000905.SH': '中证500', '399001.SZ': '深证成指', '399006.SZ': '创业板指',
    '399303.SZ': '国证2000',
}


def source_path() -> Path:
    root = os.environ.get('SLQUANT_DATA_ROOT', '').strip()
    if not root:
        raise ValueError('请配置 SLQUANT_DATA_ROOT，指向现有行情根目录')
    return Path(root).expanduser().resolve() / 'raw' / 'index_daily.parquet'


def availability() -> tuple[bool, str]:
    try:
        return (True, '本地日线') if source_path().is_file() else (False, '缺少 raw/index_daily.parquet')
    except ValueError as exc:
        return False, str(exc)


def _dates(df, column='trade_date'):
    if df.schema[column] == pl.Date:
        return df
    return df.with_columns(pl.col(column).cast(pl.String).str.strptime(pl.Date, '%Y%m%d'))


def _window(df, start, end, column='date'):
    for value, lower in [(start, True), (end, False)]:
        if value is None:
            continue
        bound = value.date() if isinstance(value, datetime) else value
        if not isinstance(bound, date):
            bound = datetime.strptime(str(bound).replace('-', '')[:8], '%Y%m%d').date()
        df = df.filter(pl.col(column) >= bound if lower else pl.col(column) <= bound)
    return df


def _unique(df):
    if df.select(pl.struct('ts_code', 'trade_date').is_duplicated().any()).item():
        raise ValueError('本地日线键重复，未自动挑选或覆盖')
    return df


@dataclass
class LocalConfig:
    name: str = 'local_parquet'
    display_name: str = '本地 Parquet'
    datasets: dict = field(default_factory=lambda: {'daily': {}, 'adj_factor': {}, 'financial': {}})
    path: None = None


class LocalParquetProvider:
    name = 'local_parquet'
    builtin = True

    def __init__(self, stock_root: Path | None = None):
        self.config = LocalConfig()
        self.stock_root = stock_root.resolve() if stock_root is not None else None

    def close(self) -> None:
        pass

    def _stock(self, table):
        from app.services.personal_local_data import cache_dir
        if self.stock_root is not None and table in {'daily', 'adj_factor'}:
            original = self.stock_root / (table + '.parquet')
            if not original.is_file():
                raise ValueError(f'指定原始行情快照缺少 {table}，未回退其他来源')
            return _unique(pl.read_parquet(original))
        original = source_path().with_name(table + '.parquet')
        if original.is_file():
            return _unique(pl.read_parquet(original))
        paths = sorted((cache_dir() / table).glob('*.parquet'))
        if not paths:
            raise ValueError(f'缺少本地 {table}；请先补数，不使用 daily_qfq 冒充原始价')
        return _unique(pl.read_parquet(paths))

    def _read(self):
        from app.services.personal_local_data import cache_dir
        if not source_path().is_file():
            raise FileNotFoundError('缺少本地指数日线；不会回退在线数据')
        base = _unique(pl.read_parquet(source_path()))
        extra = sorted((cache_dir() / 'index').glob('*.parquet'))
        if extra:
            new = _unique(pl.concat([pl.read_parquet(p) for p in extra], how='diagonal_relaxed'))
            base = pl.concat([base.join(new.select('ts_code', 'trade_date'),
                                        on=['ts_code', 'trade_date'], how='anti'), new], how='diagonal_relaxed')
        return base

    def get_financials(self, table, symbols, latest_only=True):
        from app.services.personal_local_financials import read_financial
        return read_financial(table, symbols, latest_only=latest_only)

    def get_instruments(self, asset_type='stock') -> list[dict]:
        if asset_type == 'stock':
            frame = pl.read_parquet(source_path().with_name('stock_basic.parquet'))
            if frame['ts_code'].n_unique() != frame.height:
                raise ValueError('股票名称维表代码重复')
            # 包含已退市证券；不以当前上市状态筛历史池。股本不回填最新值。
            return [{'symbol': r['ts_code'], 'code': r['symbol'],
                     'exchange': r['ts_code'].split('.')[1], 'name': r['name'],
                     'type': 'stock', 'region': 'CN', 'ext': {'listing_date':
                     datetime.strptime(r['list_date'], '%Y%m%d').date().isoformat() if r['list_date'] else None}}
                    for r in frame.to_dicts()]
        if asset_type == 'etf':
            from app.services.personal_local_data import cache_dir
            frame = pl.read_parquet(cache_dir() / 'fund_basic.parquet')
            return [{'symbol': r['ts_code'], 'code': r['ts_code'].split('.')[0],
                     'exchange': r['ts_code'].split('.')[1], 'name': r['name'], 'type': 'etf', 'region': 'CN',
                     'ext': {'listing_date': datetime.strptime(r['list_date'], '%Y%m%d').date().isoformat() if r['list_date'] else None,
                             'tick_size': 0.001}} for r in frame.to_dicts()]
        if asset_type != 'index':
            raise ValueError('当前接通股票、ETF和指数日线')
        return [{'symbol': s, 'code': s.split('.')[0], 'exchange': s.split('.')[1],
                 'name': _NAMES.get(s, s), 'type': 'index', 'region': 'CN'}
                for s in sorted(self._read()['ts_code'].unique().to_list())]

    def get_active_mv(self):
        """真实0AMV独立数据集：只读，不冒充可交易指数，不转换未知单位。"""
        path = source_path().with_name('compass_0amv.parquet')
        if not path.is_file():
            raise ValueError('缺少本地 raw/compass_0amv.parquet，请先同步Windows导出数据')
        frame = _dates(pl.read_parquet(path)).rename({'trade_date': 'date'})
        required = {'date', 'open', 'high', 'low', 'close'}
        if not required <= set(frame.columns):
            raise ValueError('0AMV缺少日期或OHLC列，不能伪造K线')
        if frame['date'].null_count() or frame['date'].is_duplicated().any():
            raise ValueError('0AMV日期为空或重复，请核对源数据')
        values = [pl.col(c).cast(pl.Float64, strict=True) for c in ('open', 'high', 'low', 'close')]
        frame = frame.with_columns(values)
        bad = frame.filter(
            pl.any_horizontal([pl.col(c).is_null() | ~pl.col(c).is_finite() | (pl.col(c) <= 0)
                               for c in ('open', 'high', 'low', 'close')])
            | (pl.col('low') > pl.min_horizontal('open', 'close'))
            | (pl.col('high') < pl.max_horizontal('open', 'close'))
        )
        if bad.height:
            raise ValueError(f'0AMV有{bad.height}条无效OHLC，未自动修正')
        return frame.sort('date')

    def get_daily(self, symbols, start_time=None, end_time=None, asset_type='stock', on_chunk_done=None):
        if asset_type not in {'stock', 'index', 'etf'}:
            raise ValueError('当前接通股票、ETF和指数日线')
        if not symbols:
            raise ValueError('请明确指定证券代码')
        df = self._read() if asset_type == 'index' else self._stock('fund_daily' if asset_type == 'etf' else 'daily')
        required = {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'amount'}
        if not required <= set(df.columns):
            raise ValueError(f'本地日线缺列：{sorted(required - set(df.columns))}')
        absent = set(symbols) - set(df['ts_code'].unique().to_list())
        if absent:
            raise ValueError(f'本地没有这些指数/股票/ETF：{sorted(absent)}')
        df = _window(_dates(df.filter(pl.col('ts_code').is_in(symbols))).rename({'trade_date': 'date'}), start_time, end_time)
        df = df.rename({'ts_code': 'symbol', 'vol': 'volume'}).with_columns((pl.col('amount') * 1000).alias('amount'))
        if 'pre_close' in df.columns:
            df = df.rename({'pre_close': 'exchange_pre_close'})
        result = normalize_daily(df).sort(['symbol', 'date'])  # 手保持不变，千元转元
        if on_chunk_done:
            on_chunk_done(len(symbols), len(symbols))
        return result

    def get_adj_factors(self, symbols, start_time=None, end_time=None, asset_type='stock'):
        if asset_type not in {'stock', 'etf'}:
            raise ValueError('复权因子仅用于股票和ETF')
        frame = _dates(self._stock('fund_adj' if asset_type == 'etf' else 'adj_factor')).filter(pl.col('ts_code').is_in(symbols))
        if frame.filter(pl.col('adj_factor').is_null() | ~pl.col('adj_factor').is_finite() | (pl.col('adj_factor') <= 0)).height:
            raise ValueError('复权因子必须是正的有限值')
        # Tushare是累计因子；TSP需要每次事件比值。先差分，再裁日期。
        events = frame.sort(['ts_code', 'trade_date']).with_columns(
            (pl.col('adj_factor') / pl.col('adj_factor').shift(1).over('ts_code')).fill_null(1.).alias('ex_factor')
        ).filter((pl.col('ex_factor') - 1).abs() > 1e-12).rename({'ts_code': 'symbol'})
        return _window(events, start_time, end_time, 'trade_date').select('symbol', 'trade_date', 'ex_factor')

    def test_dataset(self, dataset, symbols=None) -> dict:
        from app.services.personal_local_data import cache_dir
        meta_path = cache_dir() / 'fund_basic.parquet'
        etfs = set(pl.read_parquet(meta_path)['ts_code']) if meta_path.exists() else set()
        asset = 'etf' if symbols and all(s in etfs for s in symbols) else 'stock'
        if dataset == 'financial':
            df = self.get_financials('metrics', symbols or ['600519.SH'])
        elif dataset == 'adj_factor':
            df = self.get_adj_factors(symbols or ['000001.SZ'], asset_type=asset)
        elif dataset == 'daily':
            selected = symbols or ['000300.SH']
            df = self.get_daily(selected, asset_type='index' if all(s in _NAMES for s in selected) else asset)
        else:
            return {'provider': self.name, 'dataset': dataset, 'error': '该能力尚未接入'}
        return {'provider': self.name, 'dataset': dataset, 'rows': df.height,
                'columns': df.columns, 'preview': df.head(3).to_dicts()}
