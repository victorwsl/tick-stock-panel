"""Read-only coverage of canonical ETF partitions; never use index fallback."""
from datetime import date

U0 = ("510050.SH", "510880.SH", "159915.SZ", "510300.SH", "510500.SH", "513100.SH")
TABLES = ("kline_etf_daily", "kline_etf_enriched", "adj_factor_etf")


def coverage(repo, symbols: str | None, start: date, end: date) -> dict:
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    selected = U0 if symbols is None else tuple(symbols.split(","))
    if not selected or len(selected) > 60 or len(set(selected)) != len(selected):
        raise ValueError("候选ETF数量须为1至60且不能重复")
    if any(not s or s != s.strip() or len(s) > 20 for s in selected):
        raise ValueError("ETF代码格式错误")
    tables = {r[0] for r in repo.execute_all("SELECT table_name FROM information_schema.tables")}
    identities = {}
    if "instruments_etf" in tables:
        cols = [r[0] for r in repo.execute_all("DESCRIBE instruments_etf")]
        fields = [c for c in ("symbol", "type", "asset_type") if c in cols]
        if "symbol" not in fields:
            raise ValueError("ETF身份表缺少symbol")
        for row in repo.execute_all("SELECT " + ",".join(fields) + " FROM instruments_etf"):
            item = dict(zip(fields, row, strict=True))
            symbol = item["symbol"]
            valid = all(item[c] == "etf" for c in fields if c != "symbol")
            identities[symbol] = valid if symbol not in identities else False
    if symbols is not None and any(not identities.get(s, False) for s in selected):
        raise ValueError("ETF身份未确认，不能使用股票、指数或未知代码")
    counts = {}
    for table in TABLES:
        if table in tables:
            day = "trade_date" if table == "adj_factor_etf" else "date"
            rows = repo.execute_all(
                f"SELECT symbol,count(*),count(DISTINCT {day}),min({day}),max({day}) FROM {table} "
                f"WHERE symbol IN (SELECT unnest(?)) AND {day} >= ? AND {day} <= ? GROUP BY symbol",
                [list(selected), start, end],
            )
            counts[table] = {r[0]: {"rows": r[1], "dates": r[2], "first": r[3], "last": r[4]} for r in rows}
    return {
        "asset_type": "etf", "module": "etf_research", "dataset_id": None,
        "ready_for_backtest": False, "source": "current_local_etf_partitions",
        "start": start, "end": end,
        "limitations": "仅观察本地条数与首尾；复权事件为稀疏记录，不能按日判断缺口；复权与交易规则尚未验收，不能作为已发布回测版本。",
        "items": [{"symbol": s, "identity_confirmed": identities.get(s, False),
                   "tables": {t: {"available": t in tables, **counts.get(t, {}).get(s, {"rows": 0, "dates": 0, "first": None, "last": None})} for t in TABLES}}
                  for s in selected],
    }
