import type { FactorPredictionAudit, FactorBatchItem } from '@/lib/api'
import { fmtPct } from '@/lib/format'

export function FactorResearchSummary({ config, audit, quantiles, decay, items }: {
  config: Record<string, any>
  items?: FactorBatchItem[]
  audit?: FactorPredictionAudit
  quantiles?: { group: number; mean_return: number; n_dates: number }[]
  decay?: { horizon: number; ic_mean: number | null; n_dates: number }[]
}) {
  const audits = (items ?? []).flatMap(item => item.prediction_audit?.horizon ? [item.prediction_audit] : [])
  const horizon = config.prediction_horizon as number | undefined
  const rebalance = { daily: '日度', weekly: '周度', monthly: '月度' }[String(config.rebalance)] ?? String(config.rebalance ?? '—')
  return <div className="space-y-2 rounded-btn border border-border bg-surface/60 p-3 text-[11px] text-secondary">
    <p className="font-medium text-foreground">当前结果：{horizon ? `预测未来${horizon}交易日` : '旧版：预测期限随调仓变化'} · {rebalance}调仓 · {String(config.start ?? '')}～{String(config.end ?? '')}</p>
    {horizon && <p>预测口径：T日收盘确定因子，T+1市场交易日开盘至T+{horizon}交易日收盘。缺开盘价或到期价的样本不补值。</p>}
    {!!audits.length && <>
      <p>各因子有效IC {Math.min(...audits.map(a => a.n_ic_dates))}～{Math.max(...audits.map(a => a.n_ic_dates))}天 · 不重叠区间 {Math.min(...audits.map(a => a.nonoverlap_intervals))}～{Math.max(...audits.map(a => a.nonoverlap_intervals))}个</p>
      <p className="text-amber-500">{audits.filter(a => a.inference_status === 'descriptive_only').length} 个因子尚不足8个不重叠区间，仅作探索。实际行情 {audits[0].data_start}～{audits[0].data_end}；各因子的有效样本可能不同，单因子检验可查看明细。</p>
    </>}
    {audit?.horizon && <>
      <p>实际行情 {audit.data_start}～{audit.data_end} · 有效IC {audit.n_ic_dates}天 · 成熟样本 {audit.mature_rows.toLocaleString()}条 · 未成熟或缺价 {audit.missing_or_immature_rows.toLocaleString()}条</p>
      <p>不重叠观察区间 {audit.nonoverlap_intervals}个 · 最晚有效信号 {audit.last_mature_signal ?? '暂无'}。{audit.inference_status === 'descriptive_only' ? '当前仅作探索，未达到本项目8个不重叠区间的初筛门槛。' : '属于历史研究结果，尚需独立时间段验证。'}</p>
      {audit.warnings.map((warning, index) => <p key={index} className="text-amber-500">{warning}</p>)}
    </>}
    {!!decay?.length && <div className="flex flex-wrap gap-x-4 gap-y-1">
      {decay.map(item => <span key={item.horizon}>{item.horizon}日 Rank IC：{item.ic_mean == null ? '—' : fmtPct(item.ic_mean)}（{item.n_dates}天）</span>)}
    </div>}
    {!!quantiles?.length && <div className="overflow-x-auto">
      <table className="w-full text-right"><caption className="pb-1 text-left text-secondary">{horizon}日分组平均收益 · 每日先等权、再对日期取均值 · 毛收益，未复利</caption>
        <thead><tr>{quantiles.map(item => <th key={item.group} className="px-2 py-1 font-medium">Q{item.group}</th>)}</tr></thead>
        <tbody><tr>{quantiles.map(item => <td key={item.group} className="px-2 py-1 font-mono">{fmtPct(item.mean_return)}</td>)}</tr></tbody>
      </table>
    </div>}
    <p className="text-muted">分层净值、多空收益及回撤按{rebalance}调仓的收盘理论组合计算，与{horizon ? `${horizon}日` : '固定期限'}预测指标口径不同；调仓间隔内的波动未计入该回撤，不能作为实际账户回撤。</p>
  </div>
}
