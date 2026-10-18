import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type StrategyBacktestResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function BacktestHistory({ result, pending, onLoad }: {
  result: StrategyBacktestResult | null
  pending: boolean
  onLoad: (result: StrategyBacktestResult) => void
}) {
  const history = useQuery({ queryKey: QK.backtestHistory, queryFn: api.strategyBacktestHistory,
    refetchOnMount: 'always' })
  const [selected, setSelected] = useState('')
  const restored = useRef(false)
  const load = useMutation({ mutationFn: api.strategyBacktestHistoryResult, onSuccess: loaded => onLoad(loaded) })
  useEffect(() => {
    if (result?.history_id) {
      setSelected(result.history_id)
      restored.current = true
      void history.refetch()
    }
  }, [result?.history_id])
  useEffect(() => {
    // Restore once per visit; a later list refresh must never replace a chosen result.
    if (restored.current || pending || result || !history.data?.items.length) return
    restored.current = true
    const id = history.data.items[0].id
    setSelected(id)
    load.mutate(id)
  }, [history.data, pending, result])
  return <div className="space-y-2 rounded-btn border border-border bg-surface/60 p-3 text-xs">
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor="backtest-history" className="font-medium">历史回测</label>
      <select id="backtest-history" value={selected} disabled={pending || load.isPending}
        className="min-w-0 flex-1 rounded border border-border bg-base p-1.5"
        onChange={e => { setSelected(e.target.value); if (e.target.value) load.mutate(e.target.value) }}>
        <option value="">{history.isPending ? '正在读取…' : '选择已保存的回测'}</option>
        {(history.data?.items ?? []).map(item => <option key={item.id} value={item.id}>
          {new Date(item.saved_at).toLocaleString('zh-CN', { hour12: false })} · {item.name} · {item.asset_type === 'etf' ? 'ETF' : '股票'} · {item.mode === 'full' ? '全量' : '仓位'} · {item.start}～{item.end} · {item.trade_count}笔
        </option>)}
      </select>
      <button type="button" onClick={() => { void history.refetch(); if (selected && load.isError) load.mutate(selected) }}
        className="text-accent">刷新</button>
    </div>
    {load.isPending && <p role="status">正在恢复曲线和逐笔交易…</p>}
    {(history.isError || load.isError) && <p role="alert" className="text-danger">历史记录读取失败，请刷新重试；已有结果未清除。</p>}
    {!!history.data?.unavailable && <p role="alert" className="text-warning">{history.data.unavailable}份历史摘要读取失败，请检查本地文件。</p>}
    {result?.history_error && <p role="alert" className="text-danger">{result.history_error}</p>}
    {!history.isPending && !history.isError && !history.data?.items.length && <p className="text-muted">尚无磁盘历史。本次更新后，完成的策略回测自动保存；此前只存在浏览器或临时缓存的结果不保证能恢复。</p>}
    {result && <details className="text-secondary">
      <summary className="cursor-pointer">当前结果：{result.strategy_info?.name || result.config.strategy_id} · {result.config.start}～{result.config.end} · {result.trades.length}笔 · {result.history_id ? '已保存到本地' : '尚未保存'} · 查看原始配置</summary>
      <p className="py-1">下方曲线及交易来自这次回测；左侧表单用于下一次运行，修改参数不会改变历史结果。</p>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(result.config, null, 2)}</pre>
    </details>}
  </div>
}
