"""Explicit Tushare ingestion into a private staging run, never canonical data."""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

from app.services.fs_utils import atomic_write_text
from app.services.mining_process_lock import MiningProcessLock
from app.services.personal_tushare import ENDPOINTS, OFFICIAL, FetchError
from app.services.personal_tushare import TushareClient as RawTushareClient

from .audit import day, digest, safe_path
from .overview import U0

APIS = {
    "fund_daily": {"fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount", "cap": 5000, "date": "trade_date"},
    "fund_adj": {"fields": "ts_code,trade_date,adj_factor", "cap": 2000, "date": "trade_date"},
    "trade_cal": {"fields": "exchange,cal_date,is_open,pretrade_date", "cap": 1000, "date": "cal_date"},
    "etf_basic": {"fields": "ts_code,csname,list_date,list_status,index_code,index_name,exchange,etf_type", "cap": 5000, "date": None},
    "fund_div": {"fields": "ts_code,ann_date,imp_anndate,div_proc,record_date,ex_date,pay_date,div_cash,base_unit", "cap": 1000, "date": None},
}
# Calendar/dividend caps are conservative local guards, not claimed vendor limits.


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def key(value):
    return sha256(encode(value).encode()).hexdigest()


def spec(api, symbol, start=None, end=None):
    if api not in APIS:
        raise ValueError("不支持的采集接口")
    if api == "trade_cal":
        valid = symbol in ("SSE", "SZSE")
    else:
        valid = isinstance(symbol, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol)
    if not valid:
        raise ValueError("采集代码或交易所不合法")
    if APIS[api]["date"]:
        start, end = day(start), day(end)
        if start > end or (end - start).days > 36600:
            raise ValueError("采集日期范围不合法")
        start, end = start.isoformat(), end.isoformat()
    elif start is not None or end is not None:
        raise ValueError("身份和分红接口不支持日期范围过滤，不得虚构参数")
    return {"api": api, "symbol": symbol, "start": start, "end": end}



def split_request(request):
    if not request["start"] or request["start"] == request["end"]:
        raise ValueError("请求不能按日期拆分")
    first, last = day(request["start"]), day(request["end"])
    mid = first + (last - first) // 2
    return [spec(request["api"], request["symbol"], first, mid),
            spec(request["api"], request["symbol"], mid + timedelta(days=1), last)]


def make_plan(report, audit_sha256, phase):
    if (report.get("asset_type") != "etf" or report.get("module") != "etf_research"
            or report.get("schema_version") != 1 or phase not in ("probe", "gaps")):
        raise ValueError("仅接受C07 ETF审计与probe/gaps阶段")
    universes = report["universes"]
    if [r["symbol"] for r in universes["U0"]["members"]] != list(U0):
        raise ValueError("审计的U0名单不完整，不能缩池")
    symbols = list(U0)
    for universe in universes.values():
        for row in universe["members"]:
            if row["asset_type"] != "etf":
                raise ValueError("股票或指数不能进入ETF采集计划")
            if row["symbol"] not in symbols:
                symbols.append(row["symbol"])
    requests = []
    if phase == "probe":
        end = day(report["end"])
        start = max(day(report["start"]), end - timedelta(days=6))
        requests = [spec("fund_daily", U0[0], start, end), spec("fund_adj", U0[0], start, end),
                    spec("etf_basic", U0[0]), spec("trade_cal", "SSE", start, end), spec("fund_div", U0[0])]
    else:
        requests.extend(spec("etf_basic", s) for s in symbols)
        # Merge unknown calendar evidence by year; retain overlap for C09 comparison.
        years = {}
        for interval in report["calendar"]["unknown_ranges"]:
            first, last = day(interval["start"]), day(interval["end"])
            for year in range(first.year, last.year + 1):
                a, b = max(first, date(year, 1, 1)), min(last, date(year, 12, 31))
                old = years.get(year, (a, b))
                years[year] = min(old[0], a), max(old[1], b)
        for exchange in ("SSE", "SZSE"):
            for a, b in years.values():
                requests.append(spec("trade_cal", exchange, a, b))
        for gap in report["gaps"]:
            if gap["dataset"] in ("fund_daily", "fund_adj") and gap["reason"] == "unknown":
                if gap["symbol"] not in symbols or gap["asset_type"] != "etf":
                    raise ValueError("审计缺口资产不匹配")
                a, end = day(gap["start"]), day(gap["end"])
                while a <= end:
                    b = min(end, a + timedelta(days=365))
                    requests.append(spec(gap["dataset"], gap["symbol"], a, b))
                    a = b + timedelta(days=1)
        requests.extend(spec("fund_div", s) for s in symbols)
    requests = list({key(r): r for r in requests}.values())
    return {"schema_version": 1, "asset_type": "etf", "phase": phase,
            "audit_sha256": audit_sha256, "symbols": symbols, "requests": requests}


class TushareClient(RawTushareClient):
    def __init__(self, token, *, endpoint=OFFICIAL, http=None):
        super().__init__(token, endpoint=endpoint, http=http)

    def request(self, request):
        api, symbol = request["api"], request["symbol"]
        params = {"exchange" if api == "trade_cal" else "ts_code": symbol}
        if request["start"]:
            params.update(start_date=request["start"].replace("-", ""), end_date=request["end"].replace("-", ""))
        # The C08 journal owns its retry/call budget and persists each attempt.
        return self.request_once(api, params, APIS[api]["fields"])


def validate(payload, request):
    data = payload.get("data")
    if payload.get("code") != 0 or not isinstance(data, dict):
        raise ValueError("响应结构不合法")
    fields, rows = data.get("fields"), data.get("items")
    if (not isinstance(fields, list) or len(set(fields)) != len(fields)
            or not set(APIS[request["api"]]["fields"].split(",")).issubset(fields) or not isinstance(rows, list)):
        raise ValueError("接口缺字段或字段重复")
    seen = set()
    api = request["api"]
    for values in rows:
        if not isinstance(values, list) or len(values) != len(fields):
            raise ValueError("行字段数量不符")
        row = dict(zip(fields, values, strict=True))
        if row["exchange" if api == "trade_cal" else "ts_code"] != request["symbol"]:
            raise ValueError("响应代码或交易所错位")
        column = APIS[api]["date"]
        if column:
            d = day(row[column])
            if not day(request["start"]) <= d <= day(request["end"]):
                raise ValueError("接口重复页或日期越界")
            primary = d
        elif api == "etf_basic":
            if row["list_status"] not in ("L", "D", "P") or row["exchange"] != request["symbol"].split(".")[1]:
                raise ValueError("ETF身份响应不合法")
            primary = row["ts_code"]
        else:
            primary = (row["ann_date"], row["ex_date"], row["pay_date"], row["div_proc"])
        if primary in seen:
            raise ValueError("响应主键重复，不能覆盖或忽略")
        seen.add(primary)
        if api == "trade_cal" and str(row["is_open"]) not in ("0", "1"):
            raise ValueError("日历状态不合法")
    if (api == "trade_cal" and rows and len(rows) < APIS[api]["cap"]
            and len(seen) != (day(request["end"]) - day(request["start"])).days + 1):
        raise ValueError("日历未包含全部自然日，不能标为完成")
    return len(rows)


def run_fetch(data_dir, fetch_id, plan, client, *, max_calls=5, sleep=time.sleep, interval=2.1, probe_dir=None):
    """Content-addressed raw responses + atomic request journal, guarded by a process lock.

    max_calls counts retries too. Empty responses are preserved but never certify coverage.
    This only validates transport/scope; financial data validation belongs to C09.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", fetch_id):
        raise ValueError("暂存编号不合法")
    if (set(plan) != {"schema_version", "asset_type", "phase", "audit_sha256", "symbols", "requests"}
            or plan["asset_type"] != "etf" or plan["schema_version"] != 1
            or plan["phase"] not in ("probe", "gaps") or not re.fullmatch(r"[a-f0-9]{64}", plan["audit_sha256"])
            or not 1 <= max_calls <= 500 or len(plan["requests"]) > 1000):
        raise ValueError("ETF采集计划不合法")
    for r in plan["requests"]:
        if r != spec(**r) or (r["api"] != "trade_cal" and r["symbol"] not in plan["symbols"]):
            raise ValueError("请求与ETF计划不符")
    source = getattr(client, "endpoint", OFFICIAL)
    if source not in ENDPOINTS:
        raise ValueError("采集数据源尚未核对")
    if plan["phase"] == "gaps":
        if probe_dir is None:
            raise ValueError("须先完成单基金小请求，再扩展缺口下载")
        verify_probe(probe_dir, plan, source=source)
    elif any(r["symbol"] not in (U0[0], "SSE") or (r["start"] and (day(r["end"]) - day(r["start"])).days > 6) for r in plan["requests"]):
        raise ValueError("小请求仅限U0首只及7个自然日，不可跳过试调")
    root = safe_path(data_dir)
    stage = safe_path(root / "etf_research" / "staging" / fetch_id)
    if stage.exists() and any(p.is_symlink() for p in stage.rglob("*")):
        raise ValueError("暂存目录不能包含软链接")
    stage.mkdir(parents=True, exist_ok=True)
    lock = MiningProcessLock(stage)
    lock.acquire()
    try:
        plan_path, journal = stage / "plan.json", stage / "requests.json"
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            raise ValueError("断点续传计划发生变化，须使用新的暂存编号")
        if not plan_path.exists():
            atomic_write_text(plan_path, encode(plan))
        state = json.loads(journal.read_text()) if journal.exists() else {"records": {}, "source": source}
        if state.get("source", OFFICIAL) != source:
            raise ValueError("断点续传数据源发生变化，须使用新的暂存编号")
        state["source"] = source
        atomic_write_text(journal, encode(state))
        records = state["records"]
        (stage / "raw").mkdir(exist_ok=True)
        blocked, calls = set(), 0

        def save():
            atomic_write_text(journal, encode(state))

        def visit(request):
            nonlocal calls
            ident = key(request)
            old = records.get(ident)
            if old and old.get("file"):
                path = safe_path(stage / old["file"])
                if not path.is_relative_to(stage / "raw") or digest(path) != old["sha256"]:
                    raise ValueError("暂存分块哈希或路径不符，拒绝续传")
                if old["request"] != request:
                    raise ValueError("暂存请求清单冲突")
                count = validate(json.loads(path.read_text()), request)
                if ((old["status"] == "fetched" and not 0 < count < APIS[request["api"]]["cap"])
                        or (old["status"] == "empty_unverified" and count != 0)):
                    raise ValueError("已保存分块状态与行数不符")
                if old["status"] in ("fetched", "empty_unverified"):
                    return
                if old["status"] == "split":
                    if count < APIS[request["api"]]["cap"] or old["children"] != split_request(request):
                        raise ValueError("暂存拆分区间不完整或被改写")
                    for child in old["children"]:
                        visit(child)
                    return
            if calls >= max_calls or request["api"] in blocked:
                return
            # Unknown extensions must first be confirmed by the ETF-specific endpoint.
            if request["api"] in ("fund_daily", "fund_adj", "fund_div") and request["symbol"] not in U0:
                identity = records.get(key(spec("etf_basic", request["symbol"])), {})
                if identity.get("status") != "fetched":
                    return
            record = {"request": request, "fields": APIS[request["api"]]["fields"], "status": "running", "attempts": list((old or {}).get("attempts", []))}
            records[ident] = record
            for attempt in range(3):
                if calls >= max_calls:
                    record["status"] = "pending"
                    save()
                    return
                sleep(interval + (2 ** attempt if attempt else 0))
                calls += 1
                record["attempts"].append({"at": datetime.now(UTC).isoformat(), "status": "started"})
                save()
                try:
                    payload = client.request(request)
                except FetchError as exc:
                    record["attempts"][-1]["status"] = exc.kind
                    record["error"] = exc.kind
                    record["status"] = "blocked" if exc.kind == "permission" else "failed"
                    save()
                    if exc.kind == "permission":
                        blocked.add(request["api"])
                    if exc.kind not in ("transient", "rate_limit") or attempt == 2:
                        return
                    continue
                signature = key(payload)
                target = safe_path(stage / "raw" / f"{signature}.json")
                if target.exists() and digest(target) != signature:
                    raise ValueError("原响应分块哈希不符")
                if not target.exists():
                    atomic_write_text(target, encode(payload))
                record.update(file=str(target.relative_to(stage)), sha256=signature,
                              rows=len((payload.get("data") or {}).get("items") or []), fetched_at=datetime.now(UTC).isoformat())
                record["attempts"][-1]["status"] = "received"
                try:
                    count = validate(payload, request)
                except (ValueError, TypeError, KeyError):
                    record.update(status="failed", error="response_scope_or_schema")
                    save()
                    return
                if count >= APIS[request["api"]]["cap"]:
                    if not request["start"] or request["start"] == request["end"]:
                        record.update(status="failed", error="saturated_unsplittable")
                    else:
                        record.update(status="split", children=split_request(request))
                    save()
                    if record["status"] == "split":
                        for child in record["children"]:
                            visit(child)
                    return
                record["status"] = "fetched" if count else "empty_unverified"
                record.pop("error", None)
                save()
                return
        for request in plan["requests"]:
            visit(request)
        needed = {key(r) for r in plan["requests"]}
        for record in records.values():
            needed.update(key(c) for c in record.get("children", []))
        statuses = [records.get(k, {}).get("status", "pending") for k in needed]
        status = ("blocked" if "blocked" in statuses else "failed" if "failed" in statuses else
                  "partial" if any(s not in ("split", "fetched", "empty_unverified") for s in statuses) else
                  "needs_review" if "empty_unverified" in statuses else "staged_complete")
        state.update(status=status, ready_for_backtest=False, asset_type="etf", calls_this_run=calls,
                     plan_sha256=key(plan), source=source,
                     limitations="仅暂存；字段范围已核对，OHLC/复权/分红/历史身份与完整性尚待C09核验。")
        save()
        return state
    finally:
        lock.release()


def verify_probe(probe_dir, plan, *, source=OFFICIAL):
    root = safe_path(probe_dir)
    probe = json.loads(safe_path(root / "plan.json").read_text())
    receipt = json.loads(safe_path(root / "requests.json").read_text())
    if (probe.get("phase") != "probe" or probe.get("audit_sha256") != plan["audit_sha256"]
            or probe.get("symbols") != plan["symbols"] or receipt.get("status") != "staged_complete"
            or receipt.get("source") != source or receipt.get("plan_sha256") != key(probe)
            or {r["api"] for r in probe["requests"]} != set(APIS)):
        raise ValueError("小请求尚未全部通过或与当前审计计划不一致")
    for r in probe["requests"]:
        if (r != spec(**r) or r["symbol"] not in (U0[0], "SSE")
                or (r["start"] and (day(r["end"]) - day(r["start"])).days > 6)):
            raise ValueError("小请求证券不匹配")
        record = receipt["records"].get(key(r), {})
        path = safe_path(root / record.get("file", "missing"))
        if (record.get("status") != "fetched" or record.get("request") != r
                or not path.is_relative_to(root / "raw") or digest(path) != record["sha256"]):
            raise ValueError("小请求原响应哈希或状态不符")
        if validate(json.loads(path.read_text()), r) <= 0:
            raise ValueError("空响应不能作为小请求通过的证据")
