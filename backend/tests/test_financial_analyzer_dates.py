"""实际 Parquet 日期类型必须能走完整财务分析流程。"""
import json
from datetime import date, datetime

import polars as pl
import pytest

from app.services import ai_provider, financial_analyzer


def write_financial(data_dir, table, frame):
    path = data_dir / 'financials' / table / 'part.parquet'
    path.parent.mkdir(parents=True)
    frame.write_parquet(path)


def test_financial_dates_and_nonfinite_values_are_json_safe(tmp_path):
    write_financial(tmp_path, 'shares', pl.DataFrame({
        'symbol': ['605179.SH'], 'period_end': [date(2026, 10, 16)],
        'announce_date': [datetime(2026, 10, 16, 18, 0)],
        'float_shares': [float('nan')], 'total_shares': [float('inf')],
    }))
    fins = financial_analyzer._load_stock_financials(tmp_path, '605179.SH')
    prompt = financial_analyzer._build_user_prompt(fins, '605179.SH', '')
    assert '2026-10-16' in prompt
    row = fins['shares'][0]
    assert row['period_end'] == '2026-10-16'
    assert row['announce_date'] == '2026-10-16T18:00:00'
    assert row['float_shares'] is None and row['total_shares'] is None
    json.dumps(fins, allow_nan=False)


@pytest.mark.asyncio
async def test_complete_financial_stream_with_typed_share_dates(tmp_path, monkeypatch):
    write_financial(tmp_path, 'metrics', pl.DataFrame({
        'symbol': ['605179.SH'], 'period_end': ['2026-06-30'],
        'announce_date': ['2026-08-25'], 'roe': [5.],
    }))
    write_financial(tmp_path, 'shares', pl.DataFrame({
        'symbol': ['605179.SH'], 'period_end': [date(2026, 10, 16)],
        'announce_date': [date(2026, 10, 16)], 'float_shares': [400e6],
    }))
    captured = []
    async def model(messages, **kwargs):
        captured.extend(messages)
        yield '财务分析正文'
    monkeypatch.setattr(ai_provider, 'stream_ai_text', model)
    events = [json.loads(s) async for s in financial_analyzer.analyze_financials_stream(tmp_path, '605179.SH')]
    assert [e['type'] for e in events] == ['meta', 'delta', 'done']
    assert events[1]['content'] == '财务分析正文'
    assert '2026-10-16' in captured[1]['content']
