import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, type FactorBatchResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function FactorBatchHistory({ result, pending, onLoad }: {
  result: FactorBatchResult | null
  pending: boolean
  onLoad: (result: FactorBatchResult) => void
}) {
  const history = useQuery({ queryKey: QK.factorBatchHistory, queryFn: api.factorBatchHistory, refetchOnMount: 'always' })
  const [selected, setSelected] = useState('')
  const restored = useRef(false)
  const generation = useRef(0)
  const load = useMutation({
    mutationFn: async (id: string) => {
      const token = generation.current
      return { token, result: await api.factorBatchHistoryResult(id) }
    },
    onSuccess: loaded => { if (loaded.token === generation.current) onLoad(loaded.result) },
  })
  useEffect(() => {
    // A newly started run takes precedence over automatic history restoration.
    if (pending) { restored.current = true; generation.current += 1; load.reset() }
  }, [pending])
  useEffect(() => {
    if (!result?.history_id) return
    restored.current = true
    setSelected(result.history_id)
    void history.refetch()
  }, [result?.history_id])
  useEffect(() => {
    if (restored.current || pending || result || !history.data?.items.length) return
    restored.current = true
    const id = history.data.items[0].id
    setSelected(id)
    load.mutate(id)
  }, [history.data, pending, result])
  return <div className="m-3 space-y-2 rounded-btn border border-border bg-surface/60 p-3 text-xs">
    <div className="flex flex-wrap items-center gap-2">
      <label htmlFor="factor-batch-history" className="font-medium">因子筛选历史</label>
      <select id="factor-batch-history" value={selected} disabled={pending || load.isPending}
        className="min-w-0 flex-1 rounded border border-border bg-base p-1.5"
        onChange={event => { const id = event.target.value; setSelected(id); restored.current = true; if (id) load.mutate(id) }}>
        <option value="">{history.isPending ? '正在读取…' : '选择已保存的筛选结果'}</option>
        {(history.data?.items ?? []).map(item => <option key={item.id} value={item.id}>
          {new Date(item.saved_at).toLocaleString('zh-CN', { hour12: false })} · {item.asset_type === 'etf' ? 'ETF' : item.asset_type === 'index' ? '指数' : '股票'} · {item.start}～{item.end}
        </option>)}
      </select>
      <button type="button" disabled={pending || load.isPending} className="text-accent disabled:opacity-40"
        onClick={() => { void history.refetch(); if (selected && load.isError) load.mutate(selected) }}>刷新历史</button>
    </div>
    {load.isPending && <p role="status">正在恢复因子筛选结果…</p>}
    {(history.isError || load.isError) && <p role="alert" className="text-danger">历史读取失败，请点击“刷新历史”重试。</p>}
    {!!history.data?.unavailable && <p role="alert" className="text-danger">{history.data.unavailable} 份历史摘要读取失败，请检查本地文件。</p>}
    {!history.isPending && !history.isError && !history.data?.items.length && <p className="text-muted">尚无保存记录。此后筛选完成会自动保存；此前刷新丢失的结果需要重跑。</p>}
    {result?.history_error && <p role="alert" className="text-danger">{result.history_error}</p>}
    {result && <details className="text-secondary">
      <summary className="cursor-pointer">当前结果：{result.results.length} 个因子 · {String(result.config.start)}～{String(result.config.end)} · {result.history_id ? '已保存到本地' : '尚未保存'} · 原始配置</summary>
      <p className="py-1">预测期限：{result.config.prediction_horizon ? `${result.config.prediction_horizon}交易日` : '旧版随调仓变化'}。表格来自这次筛选，左侧参数用于下一次运行。</p>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(result.config, null, 2)}</pre>
    </details>}
  </div>
}
