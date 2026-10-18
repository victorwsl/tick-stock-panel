"""Frozen factor studies: import verified evidence, then reconcile with Panel IC.

This service never loads outcome columns into the factor library or strategies.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from app.backtest.factor import FactorBacktestService
from app.services.fs_utils import atomic_write_text

HORIZONS = (10, 20, 60, 120, 250)
_VERIFY_LOCK = threading.Lock()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _root(data_dir: Path) -> Path:
    return data_dir / "research" / "factors"


def study_path(data_dir: Path, study_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", study_id):
        raise ValueError("实验标识无效")
    root = _root(data_dir).resolve()
    path = root / study_id
    if path.resolve().parent != root:
        raise ValueError("实验路径越界")
    if not (path / "study.json").is_file():
        raise FileNotFoundError("尚未导入该因子实验")
    return path


def _check_bundle(path: Path) -> dict:
    spec = _read(path / "study.json")
    for name, digest in spec["files"].items():
        if Path(name).name != name or _sha(path / name) != digest:
            raise ValueError("冻结实验文件校验失败，请重新导入")
    return spec


def import_study(data_dir: Path, short_run: Path, long_run: Path, study_id: str) -> dict:
    """Explicit CLI import; source experiments remain read-only, output is atomic."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", study_id):
        raise ValueError("实验标识无效")
    root = _root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / study_id
    if target.exists():
        raise ValueError("实验已存在，请使用新的实验标识")
    sm, lm = _read(short_run / "manifest.json"), _read(long_run / "manifest.json")
    if not _read(long_run / "label_reconciliation.json").get("passed"):
        raise ValueError("短中期标签尚未通过对账")
    short_files = ["resolved_spec.json", "ranked_candidates.parquet", "equity_daily.parquet", "curve_audit.csv"]
    short_spec = _read(short_run / "resolved_spec.json")
    factor_ids = short_spec["factors"]
    short_files += [f"factors/{factor}.parquet" for factor in factor_ids]
    long_files = ["labels.parquet", "daily_ic.csv", "ranking_summary.csv", "maturity.csv", "label_audit.csv"]
    sources = {}
    for source, manifest, names in [(short_run, sm, short_files), (long_run, lm, long_files)]:
        for name in names:
            digest = _sha(source / name)
            if manifest["outputs"].get(name) != digest:
                raise ValueError("来源文件与清单不符: " + name)
            sources[f"{source.name}/{name}"] = digest
    # Require proof that long-horizon evaluation consumed this exact ranking/factors.
    for name in ["ranked_candidates.parquet"] + [f"factors/{factor}.parquet" for factor in factor_ids]:
        if lm["inputs"].get(str((short_run / name).resolve())) != _sha(short_run / name):
            raise ValueError("两个实验并非同一冻结因子与排名")
    stage = root / (".import-" + uuid.uuid4().hex)
    stage.mkdir()
    try:
        catalog = []
        for factor in factor_ids:
            definition = short_spec["definitions"].get(factor)
            members = short_spec["bundles"].get(factor)
            catalog.append(dict(id=factor, name=definition[0] if definition else factor.replace("_traits", " 特征组合"),
                                formula=definition[1] if definition else "归一化等权：" + "、".join(members),
                                kind="单因子" if definition else "特征组合代理", members=members or []))
        values = pl.concat([pl.read_parquet(short_run / "factors" / (f + ".parquet")).with_columns(pl.lit(f).alias("factor_id")) for f in factor_ids])
        values.write_parquet(stage / "factors.parquet")
        ranks = pl.read_parquet(short_run / "ranked_candidates.parquet").filter(pl.col("rank") <= 5)
        ranks.write_parquet(stage / "ranked.parquet")
        shutil.copyfile(long_run / "labels.parquet", stage / "labels.parquet")
        pl.read_csv(long_run / "daily_ic.csv", schema_overrides={"trade_date": pl.String}).write_parquet(stage / "expected_ic.parquet")
        pl.read_csv(long_run / "ranking_summary.csv").write_parquet(stage / "ranking_summary.parquet")
        pl.read_csv(long_run / "maturity.csv").write_parquet(stage / "maturity.parquet")
        pl.read_csv(long_run / "label_audit.csv").write_parquet(stage / "label_audit.parquet")
        shutil.copyfile(short_run / "equity_daily.parquet", stage / "equity.parquet")
        pl.read_csv(short_run / "curve_audit.csv").write_parquet(stage / "curve_audit.parquet")
        files = {p.name: _sha(p) for p in stage.iterdir()}
        spec = dict(id=study_id, name="经典因子与CH特征组合 · 冻结候选验证", factors=catalog,
                    horizons=list(HORIZONS), periods=sorted(ranks["period"].unique().to_list()),
                    data_cutoff=lm["data_cutoff"], baseline="历史 rule_v1（不是当前生产打分）",
                    label_contract="T收盘信号→交易所次日开盘→T后第H个交易日收盘；超额基准399303.SZ",
                    fusion="95%历史基线＋5%冻结全市场月度分位分；并列取平均，缺失中性50",
                    limitations=["已查看历史样本，非独立样本外", "财报历史修订版本及供应商复权尚未完整核验",
                                 "FF3/FF5/CH3/CH4是特征代理，不是论文收益因子；未复现市场因子或官方Barra",
                                 "120/250日重叠严重，证据不足时只报描述统计", "收益增量未扣费，不等同账户收益"],
                    sources=sources, files=files)
        _write(stage / "study.json", spec)
        stage.rename(target)
        return spec
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def list_studies(data_dir: Path) -> list[dict]:
    out = []
    for path in sorted(_root(data_dir).glob("*/study.json")):
        if path.parent.name.startswith("."):
            continue
        spec = _read(path)
        checks = sorted((path.parent / "verifications").glob("*.json"))
        out.append({k: spec[k] for k in ("id", "name", "data_cutoff", "horizons", "periods")}
                   | {"verification": _read(checks[-1]) if checks else None})
    return out


def _records(frame: pl.DataFrame) -> list[dict]:
    # Both NaN and +/-infinity are undefined financial statistics, never JSON numbers.
    floats = [name for name, dtype in frame.schema.items() if dtype.is_float()]
    return frame.with_columns([pl.when(pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in floats]).to_dicts()


def summary(data_dir: Path, study_id: str, horizon: int, k: int, cohort: str) -> dict:
    path = study_path(data_dir, study_id)
    spec = _check_bundle(path)
    r = pl.read_parquet(path / "ranking_summary.parquet").filter(
        (pl.col("horizon") == horizon) & (pl.col("k") == k) & (pl.col("cohort") == cohort))
    ic = pl.read_parquet(path / "expected_ic.parquet")
    if cohort == "common_250d_mature":
        mature = pl.read_parquet(path / "labels.parquet").filter(pl.col("label_end_250d") != "")["trade_date"].unique()
        ic = ic.filter(pl.col("trade_date").is_in(mature.implode()))
    periods = pl.read_parquet(path / "ranked.parquet").select("trade_date", "period").unique()
    ic = ic.filter(pl.col("horizon") == horizon).join(periods, on="trade_date", validate="m:1")
    ic = ic.group_by("factor_id", "period").agg(pl.col("rank_ic").mean(), pl.col("rank_ic").count().alias("ic_dates"))
    rows = []
    for factor in spec["factors"]:
        variants = ["soft5_" + factor["id"]]
        if factor["id"] in ("CH3_traits", "CH4_traits"):
            prefix = factor["id"].split("_")[0]
            variants += ["raw_" + prefix, "rank_" + prefix]
        for variant in variants:
            for period in spec["periods"]:
                metrics = r.filter((pl.col("variant_id") == variant) & (pl.col("period") == period))
                value = ic.filter((pl.col("factor_id") == factor["id"]) & (pl.col("period") == period))
                factor_ic = _records(value)[0] if value.height else {}
                if not variant.startswith("soft5_"):
                    factor_ic = {"rank_ic": None, "ic_dates": None}
                rows.append(factor | {"variant_id": variant, "period": period}
                            | (_records(metrics)[0] if metrics.height else {}) | factor_ic)
    return {"study": spec, "rows": rows, "maturity": _records(pl.read_parquet(path / "maturity.parquet")),
            "label_audit": _records(pl.read_parquet(path / "label_audit.parquet"))}


def detail(data_dir: Path, study_id: str, factor_id: str, horizon: int, k: int, cohort: str, period: str) -> dict:
    path = study_path(data_dir, study_id)
    spec = _check_bundle(path)
    if factor_id not in {f["id"] for f in spec["factors"]} or period not in spec["periods"]:
        raise ValueError("未知因子或样本窗口")
    variant = "soft5_" + factor_id
    ic = pl.read_parquet(path / "expected_ic.parquet").filter((pl.col("factor_id") == factor_id) & (pl.col("horizon") == horizon))
    periods = pl.read_parquet(path / "ranked.parquet").select("trade_date", "period").unique()
    ic = ic.join(periods, on="trade_date", validate="m:1").filter(pl.col("period") == period).sort("trade_date")
    if cohort == "common_250d_mature":
        mature = pl.read_parquet(path / "labels.parquet").filter(pl.col("label_end_250d") != "")["trade_date"].unique()
        ic = ic.filter(pl.col("trade_date").is_in(mature.implode()))
    curves = pl.read_parquet(path / "equity.parquet").filter((pl.col("period") == period) & pl.col("variant_id").is_in(["baseline", variant]))
    # Historical NAV only corresponds to Top3/10D and the original signal cohort.
    available = horizon == 10 and k == 3 and cohort == "own_mature" and curves["variant_id"].n_unique() == 2
    return {"ic": _records(ic), "curves": _records(curves.sort("trade_date")) if available else [],
            "curve_note": "十个资金子账户、持有10交易日、双边费15bp的研究组合；不是完整A股账户回测" if available else "当前选择尚无对应资金净值；不将重叠持有期收益连乘为净值。"}


def verify(data_dir: Path, study_id: str) -> dict:
    if not _VERIFY_LOCK.acquire(blocking=False):
        raise ValueError("另一个因子核验正在运行，请稍后重试")
    try:
        path = study_path(data_dir, study_id)
        _check_bundle(path)
        factors = pl.read_parquet(path / "factors.parquet")
        labels = pl.read_parquet(path / "labels.parquet")
        expected = pl.read_parquet(path / "expected_ic.parquet")
        if factors.select(pl.struct("trade_date", "ts_code", "factor_id").n_unique()).item() != factors.height:
            raise ValueError("因子表股票日重复")
        frames = []
        for fid in factors["factor_id"].unique().to_list():
            g = factors.filter(pl.col("factor_id") == fid).join(labels, on=["trade_date", "ts_code"], validate="1:1")
            for h in HORIZONS:
                panel = g.filter(pl.col("factor").is_finite() & pl.col(f"excess_{h}d").is_finite())
                panel = panel.filter(pl.len().over("trade_date") >= 10).rename({"trade_date": "date", f"excess_{h}d": "_next_return"})
                values = FactorBacktestService._calc_ic(panel, "factor").rename({"date": "trade_date", "ic": "panel_ic"})
                frames.append(values.with_columns(pl.lit(fid).alias("factor_id"), pl.lit(h).alias("horizon")))
        actual = pl.concat(frames)
        joined = expected.join(actual, on=["trade_date", "factor_id", "horizon"], how="full", coalesce=True, validate="1:1")
        finite_match = joined.filter(pl.col("rank_ic").is_finite().fill_null(False) != pl.col("panel_ic").is_finite().fill_null(False))
        delta = joined.select((pl.col("rank_ic") - pl.col("panel_ic")).abs().max()).item()
        if joined.height != expected.height or finite_match.height or delta is None or delta > 1e-10:
            raise ValueError("Panel Rank IC与冻结结果不一致；未发布通过状态")
        # Recompute paired Top1/3/5 effects from fixed selections, without label refills.
        ranks = pl.read_parquet(path / "ranked.parquet")
        selected = ranks.join(labels, on=["trade_date", "ts_code"], validate="m:1")
        reference = pl.read_parquet(path / "ranking_summary.parquet").filter(pl.col("cohort") == "own_mature")
        compared, max_delta = 0, 0.0
        for k in (1, 3, 5):
            for h in HORIZONS:
                g = selected.filter(pl.col("rank") <= k).group_by("trade_date", "period", "variant_id").agg(
                    pl.len().alias("n"), pl.col(f"excess_{h}d").count().alias("valid"), pl.col(f"excess_{h}d").mean().alias("excess"))
                g = g.with_columns(pl.when((pl.col("n") == k) & (pl.col("valid") == k)).then(pl.col("excess")).otherwise(None).alias("excess"))
                base = g.filter(pl.col("variant_id") == "baseline").select("trade_date", "period", pl.col("excess").alias("baseline"))
                value = g.join(base, on=["trade_date", "period"], validate="m:1").with_columns((pl.col("excess")-pl.col("baseline")).alias("actual_uplift"))
                value = value.group_by("period", "variant_id").agg(pl.col("actual_uplift").mean(), pl.col("actual_uplift").count().alias("actual_dates"))
                expected_group = reference.filter((pl.col("k") == k) & (pl.col("horizon") == h))
                value = expected_group.join(value, on=["period", "variant_id"], how="full", coalesce=True, validate="1:1")
                missing = value.filter(
                    pl.col("paired_dates").ne_missing(pl.col("actual_dates"))
                    | (pl.col("uplift").is_finite().fill_null(False) != pl.col("actual_uplift").is_finite().fill_null(False)))
                error = value.select((pl.col("uplift")-pl.col("actual_uplift")).abs().max()).item() or 0.
                if value.height != expected_group.height or missing.height or error > 1e-10:
                    raise ValueError("Panel TopK增量与冻结结果不一致")
                max_delta = max(max_delta, error)
                compared += value.height
        result = dict(bundle_sha256=_sha(path / "study.json"), panel_code_sha256=_sha(Path(__file__)), status="passed", at=datetime.now(UTC).isoformat(), ic_comparisons=joined.height,
                      max_ic_delta=float(delta), ranking_comparisons=compared, max_uplift_delta=float(max_delta),
                      scope="Panel独立统计复算通过；不代表账户撮合或样本外有效性通过")
        folder = path / "verifications"
        folder.mkdir(exist_ok=True)
        _write(folder / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json"), result)
        return result
    finally:
        _VERIFY_LOCK.release()
