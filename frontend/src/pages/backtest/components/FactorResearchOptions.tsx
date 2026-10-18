export type PredictionHorizon = 20 | 60 | 250 | null

export function FactorResearchOptions({ horizon, onHorizon, onRange }: {
  horizon: PredictionHorizon
  onHorizon: (value: PredictionHorizon) => void
  onRange: (years: number) => void
}) {
  const quick = { 20: 3, 60: 5, 250: 8 }[horizon ?? 20]
  const full = { 20: 10, 60: 12, 250: 15 }[horizon ?? 20]
  return <div className="space-y-2 rounded-btn border border-border bg-surface p-2.5">
    <label className="block text-xs font-medium text-secondary">
      预测期限
      <select aria-label="预测期限" value={horizon ?? ''} onChange={event => onHorizon(event.target.value ? Number(event.target.value) as PredictionHorizon : null)}
        className="mt-1.5 w-full rounded-input border border-border bg-base px-2.5 py-1.5 text-xs focus:border-accent focus:outline-none">
        <option value={20}>未来20交易日 · 约1个月</option>
        <option value={60}>未来60交易日 · 约1季度</option>
        <option value={250}>未来250交易日 · 约1年</option>
        <option value="">旧版口径 · 预测期限随调仓</option>
      </select>
    </label>
    <p className="text-[11px] leading-relaxed text-muted">{horizon ? `IC检验次日开盘至第${horizon}个交易日收盘的收益。调仓频率独立控制理论组合换仓。` : '旧版口径用本次至下次调仓日的收盘收益计算IC，仅供复现旧记录。'}</p>
    <div className="flex flex-wrap gap-1.5">
      <button type="button" onClick={() => onRange(quick)} className="rounded-btn border border-border px-2 py-1 text-[11px] text-secondary hover:text-accent">快速筛选 · {quick}年</button>
      <button type="button" onClick={() => onRange(full)} className="rounded-btn border border-border px-2 py-1 text-[11px] text-secondary hover:text-accent">仔细验证 · {full}年</button>
    </div>
    <p className="text-[10px] leading-relaxed text-muted">按钮仅设置建议区间，实际以本地行情为准。完整期限未到的信号不计入IC；样本外验证仍需另留时间段。</p>
  </div>
}
