import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EChartsCandlestick } from './EChartsCandlestick'

const labels: Record<string, string> = { risk_off: '防守', probe: '试探', confirmed: '确认' }

export function ActiveMvChart({ start, end }: { start?: string; end?: string }) {
  const [limit, setLimit] = useState(250)
  const query = useQuery({ queryKey: QK.activeMv(start, end, limit),
    queryFn: () => api.activeMv(start, end, limit), refetchInterval: 60_000 })
  const data = query.data
  const latest = data?.rows.at(-1)
  return <section className="rounded-card border border-border bg-surface p-3 space-y-2">
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <h2 className="font-semibold text-foreground">活跃市值 · 0AMV</h2>
      {latest && <span className="text-secondary">{latest.date} · {labels[latest.amv_state] ?? '缺数据'} · 收盘 {latest.close.toFixed(2)}</span>}
      {!start && !end && <select aria-label="活跃市值显示范围" value={limit} onChange={e => setLimit(Number(e.target.value))}
        className="rounded border border-border bg-base p-1 text-secondary">
        <option value={250}>近一年</option><option value={1000}>近四年</option><option value={0}>全部</option>
      </select>}
      <button className="ml-auto text-accent" onClick={() => query.refetch()} disabled={query.isFetching}>刷新本地数据</button>
    </div>
    {query.isPending && <p className="text-xs text-muted">读取本地活跃市值…</p>}
    {query.isError && <p className="text-xs text-danger">读取失败：{query.error.message}</p>}
    {data && !data.rows.length && <p className="text-xs text-muted">所选区间没有0AMV数据；回测期间缺数据将禁止新开仓。</p>}
    {data && data.rows.length > 0 && <>
      <p className="text-[11px] text-muted">数据截止 {data.latest} · {data.total.toLocaleString()} 条 · {data.unit} · 白黄线仅供观察</p>
      <EChartsCandlestick data={data.rows} symbol="0AMV" height={320}
        activeIndicators={[]} visibleBars="all" />
      <p className="text-[11px] text-muted">{data.rule}。只控制新仓；防守不等于强制平仓。历史补录可回测，不代表当时实际接收过数据。</p>
    </>}
  </section>
}
