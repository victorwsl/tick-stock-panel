from datetime import date

import polars as pl
import pytest

from app.config import settings
from app.plugins.local_parquet.provider import LocalParquetProvider
from app.services.personal_local_assets import import_etfs, import_sectors, seed_etfs
from app.services.personal_local_data import cache_dir


@pytest.fixture
def assets(tmp_path, monkeypatch):
    root = tmp_path / 'source' / 'raw'
    root.mkdir(parents=True)
    monkeypatch.setenv('SLQUANT_DATA_ROOT', str(root.parent))
    monkeypatch.setattr(settings, 'data_dir', tmp_path / 'trial')
    pl.DataFrame({'trade_date': ['20250107']}).write_parquet(root / 'daily_qfq.parquet')
    pl.DataFrame({'ts_code': ['000001.SZ', '000002.SZ']}).write_parquet(root / 'stock_basic.parquet')
    pl.DataFrame({'con_code': ['000001.SZ', '000001.SZ'], 'concept_name': ['AI PC', 'DRG/DIP']}).write_parquet(root / 'ths_concept_member.parquet')
    pl.DataFrame({'ts_code': ['000001.SZ'] * 2, 'l1_name': ['旧行业', '银行'],
                  'in_date': ['20200101', '20250103'], 'out_date': ['20250103', None]}).write_parquet(root / 'sw_member.parquet')
    frame = pl.DataFrame({'ts_code': ['510300.SH'] * 4,
        'trade_date': ['20250102', '20250103', '20250106', '20250107'],
        'open': [2.001, 2.002, 1.001, 1.002], 'high': [2.01, 2.01, 1.01, 1.01],
        'low': [2., 2., 1., 1.], 'close': [2.001, 2.002, 1.001, 1.002],
        'pre_close': [2., 2.001, 1.001, 1.001], 'vol': [100.] * 4, 'amount': [20.] * 4,
        'adj_factor': [1., 1., 2., 2.], 'qfq_close': [1.0005, 1.001, 1.001, 1.002],
        'asset_name': ['测试ETF'] * 4, 'list_date': ['20250102'] * 4})
    path = tmp_path / 'etf.parquet'
    frame.write_parquet(path)
    return root, path


def test_sector_snapshot_and_exact_concept_names(assets):
    from app.api.ext_data import _filter_dimension_member_rows
    from app.api.overview import _dimension_values as api_values
    from app.services.market_overview_builder import _dimension_values
    result = import_sectors()
    assert result['industry_groups'] == 1
    frame = pl.read_parquet(settings.data_dir / 'ext_data/personal_local_memberships/part.parquet')
    row = frame.filter(pl.col('symbol') == '000001.SZ').row(0, named=True)
    assert row['industry'] == '银行'
    for parse in [_dimension_values, api_values]:
        assert parse(row['concept']) == ['AI PC', 'DRG/DIP']
        assert parse('人工智能、机器人') == ['人工智能', '机器人']
    assert _filter_dimension_member_rows(frame, 'concept', 'AI PC').height == 1
    assert _filter_dimension_member_rows(frame, 'concept', 'AI').height == 0
    padded = frame.with_columns(pl.col('concept').str.replace(r'^', '  '))
    assert _filter_dimension_member_rows(padded, 'concept', 'AI PC').height == 1
    assert _filter_dimension_member_rows(frame, 'industry', '银行').height == 1


def test_etf_raw_price_units_event_and_repository(assets):
    _, path = assets
    seed_etfs([path])
    p = LocalParquetProvider()
    raw = p.get_daily(['510300.SH'], asset_type='etf')
    assert raw['close'][0] == 2.001
    assert raw['amount'][0] == 20000
    assert p.get_instruments('etf')[0]['ext']['tick_size'] == 0.001
    assert p.get_adj_factors(['510300.SH'], asset_type='etf').to_dicts() == [
        {'symbol': '510300.SH', 'trade_date': date(2025, 1, 6), 'ex_factor': 2.}]
    result = import_etfs()
    assert result['rows'] == 4
    frame = pl.read_parquet(str(settings.data_dir / 'kline_etf_enriched/**/*.parquet')).sort('date')
    assert frame['raw_close'].to_list() == [2.001, 2.002, 1.001, 1.002]
    assert frame['close'].to_list() == [1.0005, 1.001, 1.001, 1.002]
    assert not list((settings.data_dir / 'kline_daily').rglob('*.parquet'))


def test_etf_rejects_adjusted_as_raw_and_missing_factor(assets):
    _, path = assets
    original = pl.read_parquet(path)
    original.with_columns(pl.col('qfq_close').alias('close')).write_parquet(path)
    with pytest.raises(ValueError, match='校验失败'):
        seed_etfs([path])
    original.write_parquet(path)
    seed_etfs([path])
    af = cache_dir() / 'fund_adj/510300.SH.parquet'
    pl.read_parquet(af).head(3).write_parquet(af)
    with pytest.raises(ValueError, match='缺少复权因子'):
        import_etfs()


@pytest.mark.asyncio
async def test_personal_codex_reuses_auth_without_copying(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.services import ai_provider as a
    monkeypatch.setattr(settings, 'personal_local_only', True)
    monkeypatch.setattr(a, '_codex_base_command', lambda: ['codex'])
    monkeypatch.setattr(a, '_read_codex_config', lambda: {})
    monkeypatch.setattr(a, '_codex_home', lambda: tmp_path)
    monkeypatch.setattr(a.subprocess, 'run', lambda *args, **kw: SimpleNamespace(returncode=0, stdout=b'--ignore-user-config'))
    def forbidden(*args):
        raise AssertionError('不能复制登录凭证')
    monkeypatch.setattr(a, '_prepare_codex_home', forbidden)
    captured = {}
    def run(args, prompt, env, timeout):
        captured.update(args=args, env=env)
        return 0, b'OK', b''
    monkeypatch.setattr(a, '_run_codex_process', run)
    result = await a._run_codex_cli([{'role': 'user', 'content': 'OK'}], max_tokens=None, timeout=1, model_override='')
    assert result == 'OK'
    assert captured['env']['CODEX_HOME'] == str(tmp_path)
    assert '--ignore-user-config' in captured['args'] and '--ephemeral' in captured['args']
    assert '--model' not in captured['args']


@pytest.mark.asyncio
async def test_codex_probe_requires_actual_response(monkeypatch):
    from app.services import ai_provider as a
    monkeypatch.setattr(a, 'codex_cli_available', lambda: True)
    captured = {}
    async def run(messages, **kwargs):
        captured.update(kwargs)
        return 'OK'
    monkeypatch.setattr(a, '_run_codex_cli', run)
    assert await a.probe_ai_config(provider='codex_cli', api_key='', base_url='', model='') == 'OK'
    assert captured['model_override'] == ''


@pytest.mark.parametrize('automatic', [False, True])
def test_etf_mining_default_stamp_tax_is_zero(automatic):
    from app.api.mining import MiningAutoStartRequest, MiningStartRequest
    cls = MiningAutoStartRequest if automatic else MiningStartRequest
    fields = {} if automatic else {'factor_names': ['momentum_20d']}
    assert cls(asset_type='etf', **fields).stamp_tax_pct == 0.0
    assert cls(asset_type='stock', **fields).stamp_tax_pct == 0.0005
    assert cls(asset_type='etf', stamp_tax_pct=0.001, **fields).stamp_tax_pct == 0.001


@pytest.mark.parametrize('column', ['close', 'adj_factor'])
def test_etf_seed_rejects_conflicting_cache_without_writes(assets, column):
    _, path = assets
    seed_etfs([path])
    cached = sorted(cache_dir().rglob('*'))
    before = {p: p.read_bytes() for p in cached if p.is_file()}
    frame = pl.read_parquet(path).with_columns((pl.col(column) * 1.1).alias(column))
    # 新输入自身满足复权关系，但与已有缓存矛盾，不能静默保留旧值。
    frame = frame.with_columns((pl.col('close') * pl.col('adj_factor') /
        pl.col('adj_factor').last().over('ts_code')).alias('qfq_close'))
    frame.write_parquet(path)
    with pytest.raises(ValueError, match=r'缓存.*冲突'):
        seed_etfs([path])
    assert {p: p.read_bytes() for p in before} == before
