import importlib.util
from pathlib import Path

import pandas as pd
import pytest

path = Path(__file__).resolve().parents[2]/'scripts/fetch_size_financials.py'
spec = importlib.util.spec_from_file_location('fetch_size_financials',path)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_retains_original_and_later_announcement_versions():
    frame = pd.DataFrame([
        ('A','20170301','20161231',10.,'0'),
        ('A','20180301','20161231',12.,'1'),
        ('B','20160301','20161231',13.,'1'),
    ],columns=fetch.FIELDS)
    clean, quarantine = fetch.normalize(frame)
    assert clean.ann_date.tolist() == ['20170301','20180301']
    assert clean.profit_dedt.tolist() == [10.,12.]
    assert quarantine.ts_code.tolist() == ['B']


def test_conflicting_same_announcement_is_not_resolved_by_update_flag():
    frame = pd.DataFrame([
        ('A','20170301','20161231',10.,'0'),
        ('A','20170301','20161231',12.,'1'),
        ('B','20170301','20161231',None,'1'),
    ],columns=fetch.FIELDS)
    clean, quarantine = fetch.normalize(frame)
    assert clean.ts_code.tolist() == ['A','B']
    assert clean.profit_dedt.isna().all()
    assert len(quarantine) == 2


def test_conflicting_latest_quarter_remains_missing_instead_of_falling_back():
    frame = pd.DataFrame([
        ('A','20170301','20161231',10.,'1'),
        ('A','20170420','20170331',20.,'0'),
        ('A','20170420','20170331',30.,'1'),
    ],columns=fetch.FIELDS)
    clean,_ = fetch.normalize(frame)
    latest = clean.sort_values(['end_date','ann_date']).drop_duplicates('ts_code',keep='last')
    assert latest.end_date.tolist() == ['20170331']
    assert latest.profit_dedt.isna().all()


def test_missing_earnings_field_is_an_error():
    with pytest.raises(ValueError,match='缺字段'):
        fetch.normalize(pd.DataFrame(columns=['ts_code','ann_date','end_date']))
