from datetime import date

import polars as pl
import pytest

from app.config import settings
from app.services.personal_local_financials import read_financial


@pytest.fixture
def source(tmp_path, monkeypatch):
    raw = tmp_path / 'source/raw'
    raw.mkdir(parents=True)
    monkeypatch.setenv('SLQUANT_DATA_ROOT', str(raw.parent))
    monkeypatch.setattr(settings, 'data_dir', tmp_path / 'trial')
    return raw


def test_actual_announcement_versions_units_and_display(source):
    from app.services.financial_sync import get_financial_df
    raw = pl.DataFrame({'ts_code': ['600001.SH'] * 2, 'end_date': ['20241231'] * 2,
        'ann_date': ['20250301'] * 2, 'f_ann_date': ['20250301', '20250401'],
        'report_type': ['1'] * 2, 'revenue': [1e8, 2e8]})
    raw.write_parquet(source / 'income.parquet')
    frame = read_financial('income')
    assert frame.height == 2
    assert frame['announce_date'].to_list() == ['2025-04-01', '2025-03-01']
    assert frame['revenue'].to_list() == [2e8, 1e8]
    assert frame['operating_cost'].null_count() == 2
    path = settings.data_dir / 'financials/income/part.parquet'
    path.parent.mkdir(parents=True)
    frame.write_parquet(path)
    display = get_financial_df(settings.data_dir, 'income')
    assert display.height == 1 and display['revenue'][0] == 2e8
    assert pl.read_parquet(path).height == 2
    assert read_financial('income', ['600001.SH'], latest_only=True).height == 1
    assert read_financial('income', []).is_empty()


def test_conflicting_announcement_fails_closed(source):
    pl.DataFrame({'ts_code': ['600001.SH'] * 2, 'end_date': ['20241231'] * 2,
        'ann_date': ['20250301'] * 2, 'revenue': [1., 2.]}).write_parquet(source / 'income.parquet')
    with pytest.raises(ValueError, match='冲突版本'):
        read_financial('income')


def test_metrics_no_future_balance_ratio_and_invalid_date(source):
    pl.DataFrame({'ts_code': ['600001.SH'] * 3, 'end_date': ['20241231'] * 2 + ['20251231'],
        'ann_date': ['20250301', '20250401', '20250101'],
        'roe': [15., 16., 999.]}).write_parquet(source / 'fina_indicator.parquet')
    pl.DataFrame({'ts_code': ['600001.SH'], 'end_date': ['20241231'],
        'ann_date': ['20250315'], 'total_assets': [100.], 'total_liab': [40.]}).write_parquet(source / 'balancesheet.parquet')
    frame = read_financial('metrics').sort('announce_date')
    assert frame.height == 2
    assert frame['roe'].to_list() == [15., 16.]
    assert frame['debt_to_asset_ratio'].to_list() == [None, 40.]
    from app.backtest.fundamentals import attach_fundamental_factors, load_fundamental_snapshot
    path = settings.data_dir / 'financials/metrics/part.parquet'
    path.parent.mkdir(parents=True)
    frame.write_parquet(path)
    panel = pl.DataFrame({'symbol': ['600001.SH'] * 3,
        'date': [date(2025, 3, 1), date(2025, 3, 2), date(2025, 4, 2)], 'close': [10.] * 3})
    result = attach_fundamental_factors(panel, load_fundamental_snapshot(settings.data_dir), ['roe_latest'])
    assert result['roe_latest'].to_list() == [None, 15., 16.]


def test_missing_source_no_network(source):
    with pytest.raises(ValueError, match='不会在线回退'):
        read_financial('income')


@pytest.mark.asyncio
async def test_pinned_codex_model_reaches_command(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.services import ai_provider as a
    monkeypatch.setattr(settings, 'personal_local_only', True)
    monkeypatch.setattr(a, '_codex_base_command', lambda: ['codex'])
    monkeypatch.setattr(a, '_read_codex_config', lambda: {})
    monkeypatch.setattr(a, '_codex_home', lambda: tmp_path)
    monkeypatch.setattr(a, 'current_ai_model', lambda: 'gpt-6.1-sol')
    monkeypatch.setattr(a, 'current_codex_reasoning_effort', lambda: 'xhigh')
    monkeypatch.setattr(a.subprocess, 'run', lambda *args, **kw: SimpleNamespace(returncode=0, stdout=b'--ignore-user-config'))
    captured = {}
    def run(args, prompt, env, timeout):
        captured['args'] = args
        return 0, b'OK', b''
    monkeypatch.setattr(a, '_run_codex_process', run)
    assert await a._run_codex_cli([{'role': 'user', 'content': 'OK'}], max_tokens=None, timeout=1) == 'OK'
    args = captured['args']
    assert args[args.index('--model') + 1] == 'gpt-6.1-sol'
    assert any(arg.replace(' ', '') == 'model_reasoning_effort="xhigh"' for arg in args)


def test_manual_local_sync_preserves_versions_and_shares(source, monkeypatch):
    from app.data_providers import custom
    from app.services import financial_sync, preferences
    from app.tickflow.capabilities import CapabilitySet
    monkeypatch.setattr(settings, 'personal_local_only', True)
    monkeypatch.setattr(preferences, 'get_financial_provider', lambda: 'local_parquet')
    from app.plugins.local_parquet.provider import LocalParquetProvider
    monkeypatch.setattr(custom, 'get_provider', lambda name: LocalParquetProvider())
    pl.DataFrame({'ts_code': ['600001.SH'] * 2, 'end_date': ['20241231'] * 2,
        'ann_date': ['20250301', '20250401'], 'revenue': [1., 2.]}).write_parquet(source / 'income.parquet')
    assert financial_sync._sync_history_table_for_symbols('income', [], settings.data_dir, CapabilitySet()) == 2
    path = settings.data_dir / 'financials/income/part.parquet'
    assert pl.read_parquet(path).height == 2
    share_path = settings.data_dir / 'financials/shares/part.parquet'
    share_path.parent.mkdir(parents=True)
    pl.DataFrame({'symbol': ['600001.SH'], 'period_end': ['2025-03-01'], 'float_shares': [100.]}).write_parquet(share_path)
    before = share_path.read_bytes()
    assert financial_sync._sync_history_table_for_symbols('shares', [], settings.data_dir, CapabilitySet()) == 1
    assert share_path.read_bytes() == before


def test_local_financial_probe_uses_financial_table(source):
    from app.plugins.local_parquet.provider import LocalParquetProvider
    pl.DataFrame({'ts_code': ['600001.SH'], 'end_date': ['20241231'],
        'ann_date': ['20250301'], 'roe': [15.]}).write_parquet(source / 'fina_indicator.parquet')
    pl.DataFrame({'ts_code': ['600001.SH'], 'end_date': ['20241231'],
        'ann_date': ['20250301'], 'total_assets': [100.], 'total_liab': [40.]}).write_parquet(source / 'balancesheet.parquet')
    probe = LocalParquetProvider().test_dataset('financial', ['600001.SH'])
    assert probe['rows'] == 1 and 'roe' in probe['columns']
