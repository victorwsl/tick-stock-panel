"""Explicit ingestion only: repo-owned Tushare access, throttling and raw cache.

Adapted from TradingSimple's tushare_api/fundamentals access flow. No SDK token
file, settings import, shell modification or network request occurs on import.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx

from app.services.fs_utils import atomic_write_text
from app.services.mining_process_lock import MiningProcessLock

OFFICIAL = "https://api.tushare.pro"
DEFAULT_ENDPOINT = "https://fast.xiaodefa.cn"
ENDPOINTS = (OFFICIAL, DEFAULT_ENDPOINT, "https://tt.xiaodefa.cn")
ENV_NAMES = ("TUSHARE_TOKEN", "tushare_token", "TUSHARE_TOKEN_FILE", "TUSHARE_API_URL", "tushare_api_url")
# Raw datasets stay separated by asset. Reaching a cap is not proof of completeness.
DATASETS = {
    "daily": ("stock", 6000), "adj_factor": ("stock", 6000),
    "daily_basic": ("stock", 6000), "stock_basic": ("stock", 6000),
    "index_daily": ("index", 6000),
    "fund_daily": ("etf", 5000), "fund_adj": ("etf", 2000),
    "etf_basic": ("etf", 5000), "fund_div": ("etf", 1000),
    "trade_cal": ("calendar", 1000),
    "fina_indicator_vip": ("financial", 5000),
}


def get_token():
    value = os.environ.get("TUSHARE_TOKEN") or os.environ.get("tushare_token")  # noqa: SIM112
    if not value and os.environ.get("TUSHARE_TOKEN_FILE"):
        path = Path(os.environ["TUSHARE_TOKEN_FILE"]).expanduser()
        if path.is_file():
            value = path.read_text().strip()
            for line in value.splitlines():
                if "=" in line and line.split("=", 1)[0].strip() in {"TUSHARE_TOKEN", "tushare_token", "TOKEN", "token"}:
                    value = line.split("=", 1)[1].strip()
                    break
    value = (value or "").strip().strip("\"'")
    if not value:
        raise ValueError("未配置Tushare凭证；本地已有缓存仍可读取")
    return value


def get_endpoint():
    endpoint = os.environ.get("TUSHARE_API_URL") or os.environ.get("tushare_api_url") or DEFAULT_ENDPOINT  # noqa: SIM112
    endpoint = endpoint.strip().rstrip("/")
    if endpoint not in ENDPOINTS:
        raise ValueError("Tushare地址尚未核对；仅支持已配置的HTTPS访问地址")
    return endpoint


def load_env(path):
    from dotenv import dotenv_values
    for name, value in dotenv_values(path).items():
        if name in ENV_NAMES and value and name not in os.environ:
            os.environ[name] = value


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class FetchError(RuntimeError):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(f"Tushare采集未完成：{kind}；有效分块已保留")


class RateLimiter:
    def __init__(self, max_calls_per_min=30, *, sleep=time.sleep, clock=time.monotonic):
        if not 1 <= max_calls_per_min <= 120:
            raise ValueError("每分钟调用预算须为1至120")
        self.interval = max(0.5, 60 / max_calls_per_min)
        self._last = None
        self._lock = threading.Lock()
        self._sleep, self._clock = sleep, clock

    def wait(self):
        with self._lock:
            if self._last is not None:
                self._sleep(max(0, self.interval - (self._clock() - self._last)))
            self._last = self._clock()


class TushareClient:
    def __init__(self, token=None, *, endpoint=None, http=None, max_calls_per_min=30):
        endpoint = (endpoint or get_endpoint()).rstrip("/")
        if endpoint not in ENDPOINTS or (token is not None and not token.strip()):
            raise ValueError("仅接受经TLS验证的已核对Tushare地址及已有凭证")
        self.endpoint = endpoint
        self._token = get_token() if token is None else token
        self._http = http
        self.limiter = RateLimiter(max_calls_per_min)

    def request_once(self, api, params, fields):
        body = {"api_name": api, "token": self._token, "params": params, "fields": fields}
        try:
            if self._http is None:
                with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                    response = client.post(self.endpoint, json=body)
            else:
                response = self._http.post(self.endpoint, json=body)
        except httpx.RequestError:
            raise FetchError("transient") from None
        if response.status_code == 429:
            raise FetchError("rate_limit")
        if response.status_code in (401, 403):
            raise FetchError("permission")
        if response.status_code >= 500:
            raise FetchError("transient")
        if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
            raise FetchError("protocol")
        try:
            payload = response.json()
        except ValueError:
            raise FetchError("protocol") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise FetchError("protocol")
        if payload["code"] != 0:
            message = str(payload.get("msg") or "").lower()
            if any(s in message for s in ("每分钟", "频次", "频率", "rate limit", "too many")):
                raise FetchError("rate_limit")
            if payload["code"] == 2002 or any(s in message for s in ("token", "权限", "积分", "permission", "授权")):
                raise FetchError("permission")
            raise FetchError("api_error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FetchError("protocol")
        if self._token in encode(payload) or "token" in data.get("fields", []):
            raise FetchError("unsafe_response")
        return payload

    def query(self, api, params, fields):
        for attempt in range(3):
            self.limiter.wait()
            try:
                return self.request_once(api, params, fields)
            except FetchError as exc:
                if exc.kind not in ("transient", "rate_limit") or attempt == 2:
                    raise
                time.sleep(2 ** attempt)


def quarter_end_periods(start, end):
    import pandas as pd
    start, end = pd.Timestamp(str(start).replace("-", "")), pd.Timestamp(str(end).replace("-", ""))
    if start > end or any(d.strftime("%m%d") not in {"0331", "0630", "0930", "1231"} for d in (start, end)):
        raise ValueError("财务首尾须为有序季度末")
    return [p.end_time.strftime("%Y%m%d") for p in pd.period_range(start, end, freq="Q")]


def frame_from_payload(payload, fields):
    import pandas as pd
    data = payload["data"]
    columns, items = data.get("fields"), data.get("items")
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
            or len(set(columns)) != len(columns) or not set(fields.split(",")) <= set(columns)
            or not isinstance(items, list) or any(not isinstance(r, list) or len(r) != len(columns) for r in items)):
        raise ValueError("Tushare响应缺字段或行结构错误")
    return pd.DataFrame(items, columns=columns)


def fetch_period_pages(client, period, fields, page_size=5000):
    import pandas as pd
    if not 1 <= page_size <= 5000:
        raise ValueError("财务分页大小须为1至5000")
    quarter_end_periods(period, period)
    frames, seen = [], set()
    for offset in range(0, page_size * 1000, page_size):
        payload = client.query("fina_indicator_vip", {"period": period, "limit": page_size, "offset": offset}, ",".join(fields))
        frame = frame_from_payload(payload, ",".join(fields))
        if frame.empty:
            break
        if len(frame) > page_size or not frame.end_date.astype(str).eq(period).all():
            raise ValueError("季度数据过量或报告期错位")
        signature = sha256(encode(sorted(encode(row) for row in payload["data"]["items"])).encode()).hexdigest()
        if signature in seen:
            raise ValueError("接口分页重复，停止以避免漏数或无限循环")
        seen.add(signature)
        frames.append(frame)
        if len(frame) < page_size:
            break
    else:
        raise ValueError("财务分页超过预算，未标为完整")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(fields))


def persist_request(root, asset, api, params, fields, *, client=None):
    """Immutable exact-request raw cache; cached reads need no token or network.

    Financial/coverage validation remains a separate step before canonical import.
    Scope is deliberately explicit: a missing row is never guessed to be a halt.
    """
    if api not in DATASETS or DATASETS[api][0] != asset or not isinstance(params, dict):
        raise ValueError("数据集与资产类别不符")
    if not re.fullmatch(r"[a-zA-Z0-9_,]+", fields) or not fields or len(set(fields.split(","))) != len(fields.split(",")):
        raise ValueError("字段列表不合法")
    allowed = {"ts_code", "trade_date", "start_date", "end_date", "exchange", "period", "list_status", "market"}
    if set(params) - allowed or any(not isinstance(v, str) for v in params.values()):
        raise ValueError("请求参数不合法；凭证只从环境读取")
    endpoint = client.endpoint if client is not None else get_endpoint()
    request = {"asset_type": asset, "api": api, "params": params, "fields": fields, "source": endpoint}
    ident = sha256(encode(request).encode()).hexdigest()
    root = Path(root).expanduser().absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("采集目录不能使用软链接")
    stage = root / asset / api / ident
    if any(p.is_symlink() for p in (stage, *stage.parents)) or (stage.exists() and any(p.is_symlink() for p in stage.iterdir())):
        raise ValueError("采集目录不能使用软链接")
    stage.mkdir(parents=True, exist_ok=True)
    lock = MiningProcessLock(stage)
    lock.acquire()
    try:
        receipt, path = stage / "receipt.json", stage / "data.parquet"
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if (saved.get("schema_version") != 1 or saved.get("status") not in ("staged", "empty_unverified")
                    or saved.get("request") != request or not path.is_file()
                    or sha256(path.read_bytes()).hexdigest() != saved.get("sha256")):
                raise ValueError("本地采集缓存损坏或请求不符，拒绝覆盖")
            return path, saved, True
        client = client or TushareClient()
        payload = client.query(api, params, fields)
        frame = frame_from_payload(payload, fields)
        if len(frame) >= DATASETS[api][1]:
            raise ValueError("响应达到截断门槛，请缩小日期/证券范围；未标为完整缓存")
        for param, column in (("ts_code", "ts_code"), ("trade_date", "trade_date"), ("period", "end_date"), ("exchange", "exchange")):
            if params.get(param) and (column not in frame or not frame[column].astype(str).eq(params[param]).all()):
                raise ValueError("返回代码、日期或交易所与请求不符")
        column = "cal_date" if api == "trade_cal" else "trade_date"
        if params.get("start_date") or params.get("end_date"):
            if column not in frame or any(not re.fullmatch(r"\d{8}", str(v)) for v in frame[column]):
                raise ValueError("响应缺少有效范围日期")
            if ((params.get("start_date") and frame[column].lt(params["start_date"]).any())
                    or (params.get("end_date") and frame[column].gt(params["end_date"]).any())):
                raise ValueError("返回日期越界")
        keys = [c for c in ("ts_code", "trade_date") if c in frame]
        if api == "trade_cal":
            keys = [c for c in ("exchange", "cal_date") if c in frame]
        # Keep distinct financial announcement versions; ordinary daily keys must be unique.
        if frame.duplicated().any() or (api in ("daily", "adj_factor", "daily_basic", "index_daily", "fund_daily", "fund_adj", "trade_cal")
                                        and keys and frame.duplicated(keys).any()):
            raise ValueError("响应存在重复行，拒绝缓存")
        temp = path.with_suffix(".parquet.tmp")
        frame.to_parquet(temp, index=False)
        temp.replace(path)
        saved = {"schema_version": 1, "request": request, "rows": len(frame),
                 "status": "staged" if len(frame) else "empty_unverified",
                 "ready_for_backtest": False, "fetched_at": datetime.now(UTC).isoformat(),
                 "sha256": sha256(path.read_bytes()).hexdigest()}
        atomic_write_text(receipt, encode(saved))
        return path, saved, False
    finally:
        lock.release()
