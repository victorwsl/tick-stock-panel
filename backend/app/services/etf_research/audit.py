"""Offline ETF gap inventory. No provider, application startup, data repair or network."""
import json
import math
import re
from collections import Counter
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

import polars as pl

from .overview import U0

# Canonical events are sparse ratios, unlike daily cumulative source factors.
DAILY = {"kline_etf_daily": ("symbol", "date"), "kline_etf_enriched": ("symbol", "date"),
         "fund_daily": ("ts_code", "trade_date"), "fund_adj": ("ts_code", "trade_date")}
EVENTS = "adj_factor_etf"


def day(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        if re.fullmatch(r"[0-9]{8}", value):
            value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
    raise ValueError("日期必须为明确的交易日期，不能猜测或填补")


def members(registry):
    result = {"U0": [{"symbol": s, "asset_type": "etf", "effective_from": None,
                      "effective_to": None, "reason": "原版固定六只"} for s in U0]}
    if not isinstance(registry, dict) or set(registry) - {"U1", "U2"}:
        raise ValueError("扩展池仅接受预登记的U1/U2，不可改写U0")
    for key in ("U1", "U2"):
        rows = registry.get(key, [])
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("预登记名单须为至多100只ETF的列表")
        seen = set()
        result[key] = []
        for row in rows:
            if set(row) != {"symbol", "asset_type", "effective_from", "effective_to", "reason"}:
                raise ValueError("预登记须有代码、ETF身份、生效首尾日期与理由")
            s = row["symbol"]
            if row["asset_type"] != "etf" or not isinstance(s, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", s) or s in seen:
                raise ValueError("候选仅接受不重复的ETF声明，股票或指数不可混入")
            if not isinstance(row["reason"], str) or not row["reason"].strip():
                raise ValueError("预登记理由不能为空")
            first = day(row["effective_from"])
            last = day(row["effective_to"]) if row["effective_to"] else None
            if last and first > last:
                raise ValueError("候选生效首尾日期倒置")
            result[key].append({**row, "effective_from": first, "effective_to": last})
            seen.add(s)
    return result


def ranges(days):
    result = []
    for d in sorted(days):
        if result and d == day(result[-1]["end"]) + timedelta(days=1):
            result[-1]["end"] = d.isoformat()
            result[-1]["days"] += 1
        else:
            result.append({"start": d.isoformat(), "end": d.isoformat(), "days": 1})
    return result


def audit(frames, calendar, identities, start, end, *, registry=None, suspensions=()):
    """Dates without calendar evidence stay unknown; absence never proves suspension.

    delist_date means the first date no longer listed. Registry effective_to is inclusive.
    Identity is a current declared local record, not proof of historical point-in-time eligibility.
    """
    start, end = day(start), day(end)
    if start > end or (end - start).days > 36600:
        raise ValueError("审计日期倒置或跨度超过100年")
    universes = members(registry if registry is not None else {})
    selected = {r["symbol"] for rows in universes.values() for r in rows}
    cal = {}
    for row in calendar:
        d = day(row["trade_date"])
        if row["is_open"] not in (0, 1) or d in cal:
            raise ValueError("交易日历状态不合法或日期重复")
        cal[d] = row["is_open"]
    if not cal:
        raise ValueError("没有交易日历，不能用星期几推断交易日")
    opens = sorted(d for d, opened in cal.items() if opened and start <= d <= end)
    ids = {}
    for row in identities:
        s = row["symbol"]
        if s not in selected:
            continue
        kinds = [row[k] for k in ("type", "asset_type") if k in row]
        if not kinds or any(k != "etf" for k in kinds) or s in ids:
            raise ValueError("候选ETF身份冲突、重复或混入股票/指数")
        ids[s] = row
    halts = {}
    for row in suspensions:
        if (row.get("asset_type") != "etf" or row.get("status") != "suspended"
                or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()):
            raise ValueError("停牌须有明确的ETF身份、日期、状态和来源证据")
        key = (row["symbol"], day(row["date"]))
        if key in halts or key[0] not in ids:
            raise ValueError("停牌证据重复或ETF身份未确认")
        halts[key] = row["evidence"]
    indexed = {}
    for table, (symbol_col, date_col) in {**DAILY, EVENTS: ("symbol", "trade_date")}.items():
        data = {s: [] for s in selected}
        for row in frames.get(table, []):
            s = row[symbol_col]
            if s not in selected:
                continue
            d = day(row[date_col])
            if table in (EVENTS, "fund_adj"):
                factor = row["ex_factor" if table == EVENTS else "adj_factor"]
                if type(factor) not in (int, float) or not math.isfinite(factor) or factor <= 0:
                    raise ValueError("复权因子必须为有限正数，不得补零")
            if start <= d <= end:
                data[s].append(d)
        indexed[table] = data
    gaps, items = [], []
    for universe, rows in universes.items():
        for member in rows:
            s = member["symbol"]
            identity = ids.get(s, {})
            listing = day(identity["listing_date"]) if identity.get("listing_date") else None
            delisting = day(identity["delist_date"]) if identity.get("delist_date") else None
            if listing and delisting and listing >= delisting:
                raise ValueError("上市和退市日期冲突")
            excluded = {"not_listed": 0, "terminated": 0, "outside_membership": 0}
            expected = []
            for d in opens:
                if member["effective_from"] and (d < member["effective_from"] or (member["effective_to"] and d > member["effective_to"])):
                    excluded["outside_membership"] += 1
                elif listing and d < listing:
                    excluded["not_listed"] += 1
                elif delisting and d >= delisting:
                    excluded["terminated"] += 1
                else:
                    expected.append(d)
            expected_set = set(expected)
            stats = {}
            for table in (*DAILY, EVENTS):
                dates = indexed[table][s]
                counts = Counter(dates)
                stats[table] = {"rows": len(dates), "distinct_dates": len(counts),
                                "first": min(dates).isoformat() if dates else None,
                                "last": max(dates).isoformat() if dates else None,
                                "duplicate_rows": len(dates) - len(counts),
                                "calendar_unconfirmed_rows": sum(cal.get(d) != 1 for d in dates),
                                "known_closed_rows": sum(cal.get(d) == 0 for d in dates),
                                "calendar_unknown_rows": sum(d not in cal for d in dates),
                                "outside_expected_rows": sum(d not in expected_set for d in dates)}
                if table == EVENTS:
                    stats[table]["semantics"] = "sparse_events_completeness_unknown"
                    continue
                missing = [d for d in expected if d not in counts]
                stats[table]["required_start"] = expected[0].isoformat() if expected else None
                stats[table]["required_end"] = expected[-1].isoformat() if expected else None
                stats[table]["expected_sessions"] = len(expected)
                stats[table]["missing_sessions"] = len(missing)
                # Position refers to observed rows within the eligible audit window.
                observed = sorted(set(dates) & expected_set)
                previous = None
                for pos, d in enumerate(expected):
                    if d in counts:
                        previous = None
                        continue
                    evidence = halts.get((s, d), "") if table != "fund_adj" else ""
                    reason = "identity_unconfirmed" if not identity else "confirmed_suspension" if evidence else "unknown"
                    position = "entire" if not observed else "leading" if d < observed[0] else "trailing" if d > observed[-1] else "internal"
                    if previous and previous[0] == pos - 1 and previous[1]["reason"] == reason and previous[1]["position"] == position and previous[1]["evidence"] == evidence:
                        gap = previous[1]
                        gap["end"], gap["sessions"] = d.isoformat(), gap["sessions"] + 1
                    else:
                        gap = {"universe": universe, "symbol": s, "asset_type": "etf", "dataset": table,
                               "start": d.isoformat(), "end": d.isoformat(), "sessions": 1,
                               "position": position, "reason": reason, "evidence": evidence}
                        gaps.append(gap)
                    previous = (pos, gap)
            items.append({"universe": universe, "symbol": s, "asset_type": "etf",
                          "identity_status": "declared_etf" if identity else "unknown",
                          "listing_date": listing.isoformat() if listing else None,
                          "delist_date": delisting.isoformat() if delisting else None,
                          "lifecycle_status": "declared" if listing else "unknown",
                          "excluded_sessions": excluded, "datasets": stats})
    natural = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {"schema_version": 1, "asset_type": "etf", "module": "etf_research",
            "start": start.isoformat(), "end": end.isoformat(), "ready_for_backtest": False,
            "universes": {k: {"status": "registered" if v else "not_registered", "members": v} for k, v in universes.items()},
            "calendar": {"open_sessions": len(opens), "known_closed_days": sum(cal.get(d) == 0 for d in natural),
                         "unknown_days": sum(d not in cal for d in natural),
                         "status": "partial_or_unknown" if any(d not in cal for d in natural) else "fully_declared",
                         "unknown_ranges": ranges(d for d in natural if d not in cal)},
            "items": items, "gaps": gaps,
            "limitations": ["仅只读盘点，不下载、不补值、不发布可回测版本。", "缺行不等于停牌；无证据保持未知。",
                            "本地维表为当前声明，尚未核验历史身份、终止上市和存续期；未知身份不得直接用于补数。",
                            "缺口只覆盖已声明的交易日；日历未知区间不代表没有缺行。",
                            "稀疏事件不按日查缺；完整性和复权一致性留待C09验收。",
                            "U1/U2没有预登记名单时不自动选择历史赢家。"]}


def safe_path(path):
    p = Path(path).expanduser().absolute()
    if ".." in p.parts or any(q.is_symlink() for q in (p, *p.parents)):
        raise ValueError("输入输出路径不能含软链接或上级跳转")
    return p


def digest(path):
    with path.open("rb") as f:
        return sha256(f.read()).hexdigest()


def load_audit(data_dir, calendar_file, start, end, *, registry_file=None, suspension_file=None):
    """Read explicit canonical ETF partitions and their local source cache, never index fallback.

    Every consumed file is fingerprinted before and after the audit; additions/removals
    to consumed directory listings also invalidate it. This is an audit, not a data snapshot.
    """
    root, calendar_file = safe_path(data_dir), safe_path(calendar_file)
    if not root.is_dir() or not calendar_file.is_file():
        raise ValueError("ETF数据根目录或交易日历不存在")
    folders = {name: root / name for name in ("instruments_etf", *list(DAILY)[:2], EVENTS)}
    folders.update({name: root / "source_cache" / name for name in ("fund_daily", "fund_adj")})

    def discover(folder):
        folder = safe_path(folder)
        if not folder.exists():
            return []
        if any(p.is_symlink() for p in folder.rglob("*")):
            raise ValueError("数据分区内不能含软链接")
        return sorted(safe_path(p) for p in folder.rglob("*.parquet"))

    groups = {name: discover(folder) for name, folder in folders.items()}
    groups["calendar"] = [calendar_file]
    optional = {"registry": registry_file, "suspensions": suspension_file}
    for name, path in optional.items():
        if path is not None:
            groups[name] = [safe_path(path)]
    all_files = sorted({p for files in groups.values() for p in files})
    before = {str(p): digest(p) for p in all_files}

    def read(name):
        files = groups.get(name, [])
        if not files:
            return []
        frame = pl.scan_parquet(files)
        if name in DAILY:
            cols = list(DAILY[name]) + (["adj_factor"] if name == "fund_adj" else [])
        elif name == EVENTS:
            cols = ["symbol", "trade_date", "ex_factor"]
        elif name == "instruments_etf":
            available = frame.collect_schema().names()
            cols = [c for c in ("symbol", "type", "asset_type", "listing_date", "delist_date") if c in available]
        elif name == "calendar":
            cols = ["trade_date", "is_open"]
        else:
            cols = ["symbol", "date", "asset_type", "status", "evidence"]
        return frame.select(cols).collect().to_dicts()

    registry = json.loads(groups["registry"][0].read_text()) if registry_file else {}
    report = audit({name: read(name) for name in (*DAILY, EVENTS)}, read("calendar"),
                   read("instruments_etf"), start, end, registry=registry, suspensions=read("suspensions"))
    for name, folder in folders.items():
        if discover(folder) != groups[name]:
            raise ValueError("审计期间数据文件列表发生变化，请等待导入结束后重试")
    if before != {str(p): digest(p) for p in all_files}:
        raise ValueError("审计期间输入内容发生变化，请重试")
    report["sources"] = {"data_dir": str(root), "files": before,
                         "missing_tables": [k for k in (*DAILY, EVENTS, "instruments_etf") if not groups[k]]}
    return report


def write_report(report, output, *, data_dir, protected_files=()):
    """New audit directory only. manifest.json is written last; incomplete output is not success."""
    import csv

    output, root = safe_path(output), safe_path(data_dir)
    allowed = root / "research" / "etf" / "data_audit"
    if (root == output or root.is_relative_to(output)
            or (output.is_relative_to(root) and not output.is_relative_to(allowed))):
        raise ValueError("报告只能写到新的审计目录，不得覆盖在用数据")
    for p in protected_files:
        p = safe_path(p)
        if output == p or p.is_relative_to(output) or output.is_relative_to(p.parent):
            raise ValueError("报告目录不能覆盖输入文件及其目录")
    output.mkdir(parents=True, exist_ok=False)
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    with (output / "gaps.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["universe", "symbol", "asset_type", "dataset", "start", "end", "sessions", "position", "reason", "evidence"])
        writer.writeheader()
        writer.writerows(report["gaps"])
    payload = {"status": "audit_only", "ready_for_backtest": False,
               "files": {name: digest(output / name) for name in ("audit.json", "gaps.csv")}}
    # Exclusive creation ensures a previous completed run can never be overwritten.
    pending = output / "manifest.json.tmp"
    with pending.open("x") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    pending.rename(output / "manifest.json")
    return payload
