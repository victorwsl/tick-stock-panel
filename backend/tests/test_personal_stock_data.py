from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.config import settings
from app.plugins.local_parquet.provider import LocalParquetProvider
from app.services.personal_local_data import import_stocks


@pytest.fixture
def stock_source(tmp_path, monkeypatch):
    root = tmp_path / 'source' / 'raw'
    root.mkdir(parents=True)
    monkeypatch.setenv('SLQUANT_DATA_ROOT', str(root.parent))
    monkeypatch.setattr(settings, 'data_dir', tmp_path / 'trial')
    dates = ['20250102', '20250103', '20250106', '20250107']
    raw = pl.DataFrame({'ts_code': ['000001.SZ'] * 4, 'trade_date': dates,
                        'open': [100., 102., 51., 52.], 'high': [101., 103., 52., 53.],
                        'low': [99., 101., 50., 51.], 'close': [100., 102., 51., 52.],
                        'vol': [100.] * 4, 'amount': [100.] * 4, 'pre_close': [100., 100., 51., 51.]})
    raw.write_parquet(root / 'daily.parquet')
    raw.write_parquet(root / 'daily_qfq.parquet')
    raw.with_columns(pl.lit('000300.SH').alias('ts_code')).write_parquet(root / 'index_daily.parquet')
    pl.DataFrame({'ts_code': ['000001.SZ'], 'symbol': ['000001'], 'name': ['平安银行'],
                  'list_date': ['19910403'], 'list_status': ['D']}).write_parquet(root / 'stock_basic.parquet')
    raw.select('ts_code', 'trade_date', 'close').with_columns(
        pl.lit(100.).alias('float_share'), pl.lit(1.).alias('turnover_rate')
    ).write_parquet(root / 'daily_basic.parquet')
    raw.select('ts_code', 'trade_date').with_columns(
        pl.Series('adj_factor', [1., 1., 2., 2.])).write_parquet(root / 'adj_factor.parquet')
    return root


def test_cumulative_factor_becomes_single_event(stock_source):
    p = LocalParquetProvider()
    events = p.get_adj_factors(['000001.SZ'])
    assert events.to_dicts() == [{'symbol': '000001.SZ', 'trade_date': date(2025, 1, 6), 'ex_factor': 2.}]
    assert p.get_daily(['000001.SZ'])['amount'][0] == 100000
    assert p.test_dataset('adj_factor', ['000001.SZ'])['rows'] == 1
    assert p.test_dataset('daily', ['000001.SZ'])['rows'] == 4
    assert p.get_instruments('stock')[0]['symbol'] == '000001.SZ'  # 退市维表仍保留
    assert p.get_adj_factors(['000001.SZ'], start_time='20250106').height == 1


def test_import_and_homepage_recompute(stock_source):
    from app.services.market_overview_builder import build_market_overview
    from app.tickflow.repository import DataStore, KlineRepository
    result = import_stocks()
    assert result['rows'] == 4
    assert result['source_unchanged']
    assert result['close_max_error'] == 0
    store = DataStore(settings.data_dir)
    repo = KlineRepository(store)
    try:
        repo.refresh_cache()
        df = pl.read_parquet(str(settings.data_dir / 'kline_daily_enriched' / '**' / '*.parquet')).sort('date')
        assert df['raw_close'].to_list() == [100., 102., 51., 52.]
        assert df['close'].to_list() == [50., 51., 51., 52.]
        assert df['turnover_rate'].to_list() == [1.] * 4
        home = build_market_overview(repo)
        assert home['breadth']['total'] == 1
        assert home['breadth']['up'] == 1
        assert home['trend']['above_ma5'] >= 0
        assert 'change_pct' in repo._enriched_cache.columns
        assert str(home['as_of']) == '2025-01-07'
    finally:
        store.db.close()


def test_missing_factor_refuses_import(stock_source):
    path = stock_source / 'adj_factor.parquet'
    pl.read_parquet(path).head(3).write_parquet(path)
    with pytest.raises(ValueError, match='缺少对应复权因子'):
        import_stocks()
    assert not (settings.data_dir / 'kline_daily').exists()


def test_raw_close_mismatch_refuses_import(stock_source):
    path = stock_source / 'daily_basic.parquet'
    pl.read_parquet(path).with_columns((pl.col('close') + 1).alias('close')).write_parquet(path)
    with pytest.raises(ValueError, match='不一致'):
        import_stocks()


def test_invalid_and_duplicate_factors(stock_source):
    path = stock_source / 'adj_factor.parquet'
    df = pl.read_parquet(path)
    df.with_columns(pl.lit(0.).alias('adj_factor')).write_parquet(path)
    with pytest.raises(ValueError, match='正的有限值'):
        LocalParquetProvider().get_adj_factors(['000001.SZ'])
    pl.concat([df, df]).write_parquet(path)
    with pytest.raises(ValueError, match='键重复'):
        LocalParquetProvider().get_adj_factors(['000001.SZ'])


def test_index_cache_accepts_integer_and_float_volume(stock_source):
    from app.services.personal_local_data import cache_dir
    path = cache_dir() / 'index'
    path.mkdir(parents=True)
    frame = pl.read_parquet(stock_source / 'index_daily.parquet')
    frame.with_columns(pl.col('vol').cast(pl.Int64)).write_parquet(path / 'one.parquet')
    frame.with_columns(pl.lit('000680.SH').alias('ts_code')).write_parquet(path / 'two.parquet')
    df = LocalParquetProvider().get_daily(['000300.SH', '000680.SH'], asset_type='index')
    assert df.height == 8
    assert df['volume'].to_list() == [100.] * 8


def test_ex_rights_reference_prevents_false_positive_return():
    from app.indicators.pipeline import compute_indicators
    df = pl.DataFrame({'symbol': ['601211.SH'] * 2,
                       'date': [date(2026, 10, 15), date(2026, 10, 16)],
                       'close': [16.64940766800689, 16.65], 'raw_close': [16.95, 16.65],
                       'exchange_pre_close': [16.95, 16.65]})
    out = compute_indicators(df, needed={'change_pct'})
    assert out['change_pct'][1] == 0.
    fallback = compute_indicators(df.drop('exchange_pre_close'), needed={'change_pct'})
    assert fallback['change_pct'][1] > 0.


def test_explicit_stock_snapshot_is_read_only_and_does_not_fallback(stock_source, tmp_path):
    snap = tmp_path / 'snapshot'
    snap.mkdir()
    frame = pl.read_parquet(stock_source / 'daily.parquet')
    frame.head(2).write_parquet(snap / 'daily.parquet')
    before = (snap / 'daily.parquet').read_bytes()
    provider = LocalParquetProvider(stock_root=snap)
    assert provider._stock('daily').height == 2
    assert (snap / 'daily.parquet').read_bytes() == before
    with pytest.raises(ValueError, match='未回退'):
        provider._stock('adj_factor')


def test_snapshot_price_conflict_requires_two_consistent_checks(stock_source):
    basic = stock_source / 'daily_basic.parquet'
    frame = pl.read_parquet(basic).with_columns(
        pl.when(pl.col('trade_date') == '20250102').then(pl.col('close') + .1).otherwise(pl.col('close')).alias('close'))
    frame.write_parquet(basic)
    result = import_stocks(stock_root=stock_source)
    assert len(result['quarantined_daily_basic']) == 1
    assert result['quarantined_daily_basic'][0]['corroborated']
    assert result['source_unchanged']
    # 未复权日线不改，财务股本留下缺口。
    shares = pl.read_parquet(settings.data_dir / 'financials/shares/part.parquet')
    assert shares.height == 3
    raw = pl.read_parquet(stock_source / 'daily.parquet')
    raw.with_columns(pl.when(pl.col('trade_date') == '20250103').then(88.).otherwise(pl.col('pre_close')).alias('pre_close')).write_parquet(stock_source / 'daily.parquet')
    with pytest.raises(ValueError, match='缺少一致复核证据'):
        import_stocks(stock_root=stock_source)


def test_narrower_reload_cannot_erase_full_shares_history(stock_source):
    result = import_stocks()
    assert result['rows'] == 4
    source = stock_source / 'daily.parquet'
    pl.read_parquet(source).tail(3).write_parquet(source)
    with pytest.raises(ValueError, match='短于现有历史'):
        import_stocks()
    assert pl.read_parquet(settings.data_dir / 'financials/shares/part.parquet').height == 4


def test_relative_snapshot_path_survives_final_source_verification(stock_source, monkeypatch):
    monkeypatch.chdir(stock_source.parent)
    result = import_stocks(stock_root=Path('raw'))
    assert result['source_unchanged']
    assert str(stock_source / 'daily.parquet') in result['source_sha256']
