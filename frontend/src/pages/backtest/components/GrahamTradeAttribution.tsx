import type { StrategyBacktestTrade } from '@/lib/api'
import type { ChartMarker } from '@/components/EChartsCandlestick'

type Snapshot = Record<string, number | string | null>
type Fill = { date: string; signal_date: string; symbol: string; side: string; price: number; quantity: number; fee: number; attribution?: Snapshot }
type ResearchTrade = StrategyBacktestTrade & {
  entry_attribution?: Snapshot
  entry_fills?: Fill[]
  pnl_decomposition?: { price_and_costs: number; dividend_net: number }
}
export function isGrahamTrade(t: StrategyBacktestTrade): boolean {
  return !!(t as ResearchTrade).entry_attribution
}
function value(x: unknown, kind = 'number'): string {
  if (x == null || !Number.isFinite(Number(x))) return '未提供'
  const n = Number(x)
  if (kind === 'money') return `${(n / 1e8).toFixed(2)}亿`
  if (kind === 'percent') return `${(n * 100).toFixed(2)}%`
  if (kind === 'yuan') return `${n.toFixed(2)}元`
  return n.toFixed(3)
}
export function grahamMarkers(trades: StrategyBacktestTrade[]): ChartMarker[] {
  const markers = new Map<string, ChartMarker>()
  for (const original of trades) {
    const t = original as ResearchTrade
    if (!t.entry_attribution) continue
    for (const f of t.entry_fills ?? []) {
      const a = f.attribution ?? t.entry_attribution
      const jq = a.strategy_kind === 'joinquant_smallcap'
      const basis = jq ? `市值排名 ${a.rank ?? '—'}；前收总市值 ${value(a.market_cap, 'money')}；调仓锚点 ${a.anchor_date ?? '—'}；前收市值选股、下一开盘执行` : `排名 ${a.rank ?? '—'}；总市值 ${value(a.market_cap, 'money')}；NCAV/市值 ${value(a.ncav_ratio)}；E/P ${value(a.ep, 'percent')}；ROA ${value(a.roa, 'percent')}；报告期 ${a.report_period ?? '—'}；公告日 ${a.announce_date ?? '—'}`
      markers.set(`signal:${f.signal_date}:${f.symbol}`, { date: f.signal_date, kind: 'buy', phase: 'signal', label: jq ? '五日调仓入选' : '月末入选', detail: basis })
      markers.set(`buy:${f.date}:${f.symbol}:${f.quantity}:${f.price}`, { date: f.date, kind: 'buy', phase: 'fill', label: '买入/加仓', detail: `原始成交价 ${value(f.price, 'yuan')}；${f.quantity}股；费用 ${value(f.fee, 'yuan')}\n${basis}` })
    }
    if (t.exit_reason === 'open_position') continue
    const jq = t.entry_attribution.strategy_kind === 'joinquant_smallcap'
    const reason = jq ? '五日周期清仓' : t.exit_reason === 'rebalance' ? '恢复目标权重' : '月调未入选'
    if (t.exit_signal_date) markers.set(`exit-signal:${t.exit_signal_date}:${t.symbol}`, { date: t.exit_signal_date, kind: 'sell', phase: 'signal', label: reason, detail: `${reason}，收盘确认后下一交易日开盘尝试执行` })
    markers.set(`sell:${t.exit_date}:${t.symbol}`, { date: t.exit_date, kind: 'sell', phase: 'fill', label: t.exit_reason === 'rebalance' ? '减仓' : '卖出', detail: `原始成交价 ${value(t.exit_price, 'yuan')}；${t.shares ?? '—'}股；${reason}；受阻 ${t.blocked_exit_days ?? 0} 日` })
  }
  return [...markers.values()].sort((a, b) => a.date.localeCompare(b.date))
}
export function GrahamTradeAttribution({ trades }: { trades: StrategyBacktestTrade[] }) {
  const selected = trades.filter(isGrahamTrade) as ResearchTrade[]
  if (!selected.length) return null
  const jq = selected[0].entry_attribution?.strategy_kind === 'joinquant_smallcap'
  const rows = new Map<string, Fill>()
  for (const t of selected) for (const f of t.entry_fills ?? []) rows.set(`${f.date}:${f.symbol}:${f.quantity}:${f.price}`, f)
  return <section className="mt-3 space-y-2 border-t border-border pt-3 text-xs text-secondary" aria-label="入选依据与盈亏分解">
    <p className="font-medium text-foreground">入选依据与盈亏分解</p>
    <p className="text-muted">买卖价为未复权真实价格；图表沿用Panel复权K线。入选依据只使用信号日信息，是规则解释，不是因果证明。加仓后持仓成本按加权成本展示。</p>
    {jq && <p>聚宽复现仅按前收市值排序；调仓锚点起每5个交易日清仓再买。</p>}
    <div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-[11px]">
      <thead className="text-muted"><tr>{(jq ? ['信号 / 成交日', '排名', '前收总市值', '调仓锚点', '成交价 / 股数'] : ['信号 / 成交日', '排名', '总市值', 'NCAV / 市值', 'E/P', 'ROA', '规模 / 价值 / 质量贡献', '报告期 / 公告日', '成交价 / 股数']).map(s => <th key={s} className="px-2 py-1 font-normal">{s}</th>)}</tr></thead>
      <tbody>{[...rows.values()].map((f, i) => { const a = f.attribution ?? {}; return <tr key={i} className="border-t border-border/50">
        <td className="px-2 py-1">{f.signal_date} / {f.date}</td><td className="px-2 py-1">{a.rank}</td>
        <td className="px-2 py-1">{value(a.market_cap, 'money')}</td>
        {jq ? <td className="px-2 py-1">{a.anchor_date ?? '—'}</td> : <><td className="px-2 py-1">{value(a.ncav_ratio)}</td>
        <td className="px-2 py-1">{value(a.ep, 'percent')}</td><td className="px-2 py-1">{value(a.roa, 'percent')}</td>
        <td className="px-2 py-1">{value(a.size_contribution)} / {value(a.value_contribution)} / {value(a.quality_contribution)}</td>
        <td className="px-2 py-1">{a.report_period ?? '—'} / {a.announce_date ?? '—'}</td></>}
        <td className="px-2 py-1">{value(f.price, 'yuan')} / {f.quantity}</td>
      </tr> })}</tbody>
    </table></div>
    {selected.length === 1 && selected[0].pnl_decomposition && <p>
      价格变动及交易费用：{value(selected[0].pnl_decomposition.price_and_costs, 'yuan')}；分红净额：{value(selected[0].pnl_decomposition.dividend_net, 'yuan')}。
      {selected[0].exit_reason === 'open_position' ? '期末仅估值，尚未卖出。' : '退出原因：' + (jq ? '五日周期清仓。' : selected[0].exit_reason === 'rebalance' ? '恢复目标权重。' : '月调未入选。')}
    </p>}
  </section>
}
