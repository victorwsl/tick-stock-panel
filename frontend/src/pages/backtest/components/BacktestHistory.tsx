import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type BacktestHistoryItem, type StrategyBacktestResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPct } from '@/lib/format'
import { researchCategory, researchCategoryLabels, type ResearchCategory } from '../researchManagement'

const control = 'min-w-0 rounded border border-border bg-base px-2 py-1.5 disabled:opacity-50'

function HistoryMetadata({ item, disabled, onSave, saving, failed }: {
  item: BacktestHistoryItem; disabled: boolean; onSave: (patch: { label: string; note: string; favorite: boolean }) => void; saving: boolean; failed: boolean
}) {
  const [label, setLabel] = useState(item.label ?? '')
  const [note, setNote] = useState(item.note ?? '')
  const [favorite, setFavorite] = useState(!!item.favorite)
  return <details className="border-t border-border pt-2">
    <summary className="cursor-pointer text-secondary">编辑名称、备注与收藏</summary>
    <form className="mt-2 space-y-2" onSubmit={event => { event.preventDefault(); onSave({ label, note, favorite }) }}>
    <label className="grid gap-1">历史名称<input aria-label="历史名称" className={`${control} w-full`} maxLength={120} disabled={disabled} value={label} placeholder={item.name} onChange={event => setLabel(event.target.value)} /></label>
    <label className="grid gap-1">历史备注<textarea aria-label="历史备注" className={`${control} w-full`} rows={2} maxLength={4000} disabled={disabled} value={note} onChange={event => setNote(event.target.value)} /></label>
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5"><input type="checkbox" disabled={disabled} checked={favorite} onChange={event => setFavorite(event.target.checked)} />收藏这次回测</label>
      <button type="submit" disabled={disabled} className="rounded border border-border px-2 py-1.5 text-accent disabled:opacity-50">{saving ? '保存中…' : '保存历史资料'}</button>
    </div>
    {failed && <p role="alert" className="text-danger">历史资料保存失败，修改仍保留，请重试。</p>}
    </form>
  </details>
}

export function BacktestHistory({ result, pending, onLoad, onDelete }: {
  result: StrategyBacktestResult | null
  pending: boolean
  onLoad: (result: StrategyBacktestResult) => void
  onDelete?: (id: string) => void
}) {
  const client = useQueryClient()
  const history = useQuery({ queryKey: QK.backtestHistory, queryFn: api.strategyBacktestHistory, refetchOnMount: 'always' })
  const metadata = useQuery({ queryKey: QK.researchStrategyMetadata, queryFn: api.researchStrategyMetadata })
  const [category, setCategory] = useState<ResearchCategory | ''>('')
  const [selected, setSelected] = useState('')
  const [search, setSearch] = useState('')
  const [asset, setAsset] = useState('')
  const [strategy, setStrategy] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [favorites, setFavorites] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState('')
  const restored = useRef(false)
  const version = useRef(0)
  const active = useRef(true)
  const busy = useRef(pending)
  busy.current = pending
  const load = useMutation({
    mutationFn: async ({ id, generation }: { id: string; generation: number }) => ({ loaded: await api.strategyBacktestHistoryResult(id), generation }),
    onSuccess: ({ loaded, generation }) => { if (active.current && !busy.current && version.current === generation) onLoad(loaded) },
  })
  const choose = (id: string) => {
    restored.current = true
    setSelected(id)
    setConfirmDelete('')
    load.mutate({ id, generation: ++version.current })
  }
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: { label: string; note: string; favorite: boolean } }) => api.updateStrategyBacktestHistory(id, patch),
    onSuccess: async item => {
      await client.cancelQueries({ queryKey: QK.backtestHistory })
      client.setQueryData<Awaited<ReturnType<typeof api.strategyBacktestHistory>>>(QK.backtestHistory,
        previous => previous && ({ ...previous, items: previous.items.map(old => old.id === item.id ? item : old) }))
    },
  })
  const remove = useMutation({
    mutationFn: api.deleteStrategyBacktestHistory,
    onSuccess: async (_, id) => {
      await client.cancelQueries({ queryKey: QK.backtestHistory })
      client.setQueryData<Awaited<ReturnType<typeof api.strategyBacktestHistory>>>(QK.backtestHistory,
        previous => previous && ({ ...previous, items: previous.items.filter(item => item.id !== id) }))
      setSelected(current => current === id ? '' : current)
      setConfirmDelete('')
      onDelete?.(id)
    },
  })
  useEffect(() => { active.current = true; return () => { active.current = false; version.current++ } }, [])
  useEffect(() => { if (pending) version.current++ }, [pending])
  useEffect(() => {
    if (result?.history_id) {
      version.current++
      setSelected(result.history_id)
      restored.current = true
      void history.refetch()
    }
  }, [result?.history_id])
  useEffect(() => {
    // Restore once per visit; later list refreshes never replace a user selection.
    if (restored.current || pending || result || !history.data?.items.length) return
    choose(history.data.items[0].id)
  }, [history.data, pending, result])
  const items = history.data?.items ?? []
  const query = search.trim().toLowerCase()
  const visible = items.filter(item => (!category || researchCategory(item.strategy_id ?? '', item.name, metadata.data?.items[item.strategy_id ?? '']) === category)
    && (!asset || item.asset_type === asset)
    && (!strategy || (item.strategy_id || item.name) === strategy)
    && (!from || new Date(item.saved_at).toLocaleDateString('sv-SE') >= from) && (!to || new Date(item.saved_at).toLocaleDateString('sv-SE') <= to)
    && (!favorites || item.favorite)
    && `${item.name} ${item.strategy_id ?? ''} ${item.label ?? ''} ${item.note ?? ''}`.toLowerCase().includes(query))
  const selectedItem = items.find(item => item.id === selected)
  const editingDisabled = pending || remove.isPending || update.isPending
  const strategies = [...new Map(items.map(item => [item.strategy_id || item.name, item.name])).entries()]
  return <section aria-label="历史回测" className="min-w-0 space-y-2 rounded-btn border border-border bg-surface/60 p-3 text-xs">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-medium">历史回测 <span className="font-normal text-secondary">{visible.length}/{items.length}</span></h3>
      <button type="button" disabled={remove.isPending} onClick={() => { void history.refetch(); void metadata.refetch(); if (selected && load.isError && !pending) choose(selected) }} className="text-accent disabled:opacity-50">刷新</button>
    </div>
    <input aria-label="搜索历史回测" className={`${control} w-full`} value={search} placeholder="搜索名称、策略或备注" onChange={event => setSearch(event.target.value)} />
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <select aria-label="历史策略分类" className={control} value={category} onChange={event => setCategory(event.target.value as ResearchCategory | '')}>
        <option value="">全部分类</option>{Object.entries(researchCategoryLabels).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </select>
      <select aria-label="历史资产类型" className={control} value={asset} onChange={event => setAsset(event.target.value)}><option value="">全部类型</option><option value="stock">股票</option><option value="etf">ETF</option></select>
      <select aria-label="历史策略" className={control} value={strategy} onChange={event => setStrategy(event.target.value)}><option value="">全部策略</option>{strategies.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
      <label className="grid min-w-0 gap-1 text-secondary">保存日期从<input aria-label="历史保存开始日期" type="date" className={`${control} w-full text-foreground`} value={from} onChange={event => setFrom(event.target.value)} /></label>
      <label className="grid min-w-0 gap-1 text-secondary">保存日期至<input aria-label="历史保存结束日期" type="date" className={`${control} w-full text-foreground`} value={to} onChange={event => setTo(event.target.value)} /></label>
    </div>
    <label className="flex items-center gap-1.5"><input type="checkbox" checked={favorites} onChange={event => setFavorites(event.target.checked)} />仅收藏的回测</label>
    <div className="max-h-64 divide-y divide-border overflow-y-auto" aria-label="历史记录列表">
      {visible.map(item => <button type="button" key={item.id} aria-pressed={selected === item.id} disabled={pending || remove.isPending}
        className={`block w-full min-w-0 rounded px-2 py-2 text-left disabled:opacity-50 ${selected === item.id ? 'bg-accent/10' : 'hover:bg-surface'}`} onClick={() => choose(item.id)}>
        <span className="block break-words font-medium">{item.label || item.name}{item.favorite && <span className="ml-2 text-accent">已收藏</span>}</span>
        <span className="mt-0.5 block break-words text-secondary">{new Date(item.saved_at).toLocaleString('zh-CN', { hour12: false })} · {item.asset_type === 'etf' ? 'ETF' : '股票'} · {item.mode === 'full' ? '全量' : '仓位'} · {item.trade_count}笔</span>
        <span className="block text-secondary">{item.start}～{item.end}</span>
        <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 tabular-nums text-secondary">
          <span>累计收益 {fmtPct(item.stats?.total_return)}</span><span>最大回撤 {fmtPct(item.stats?.max_drawdown)}</span><span>胜率 {fmtPct(item.stats?.win_rate)}</span>
        </span>
        {item.note && <span className="mt-1 block truncate text-secondary">{item.note}</span>}
        {item.metadata_error && <span className="block text-warning">资料读取失败：{item.metadata_error}</span>}
      </button>)}
    </div>
    {load.isPending && <p role="status">正在恢复曲线和逐笔交易…</p>}
    {(history.isError || load.isError) && <p role="alert" className="text-danger">历史记录读取失败，请刷新重试；已有结果未清除。</p>}
    {category && metadata.isError && <p role="alert" className="text-warning">策略分类资料读取失败，暂按策略名称分类。可刷新重试。</p>}
    {!!history.data?.unavailable && <p role="alert" className="text-warning">{history.data.unavailable}份历史摘要读取失败，请检查本地文件。</p>}
    {result?.history_error && <p role="alert" className="text-danger">{result.history_error}</p>}
    {history.isPending && <p role="status" className="text-secondary">正在读取历史记录…</p>}
    {!history.isPending && !history.isError && !items.length && <p className="text-secondary">尚无磁盘历史。本次更新后，完成的策略回测自动保存；此前只存在浏览器或临时缓存的结果不保证能恢复。</p>}
    {!!items.length && !visible.length && <p className="text-secondary">没有匹配的历史记录，请调整筛选条件。</p>}
    {selectedItem && <>
      <HistoryMetadata key={`${selectedItem.id}:${JSON.stringify([selectedItem.label, selectedItem.note, selectedItem.favorite])}`} item={selectedItem} disabled={editingDisabled} saving={update.isPending}
        failed={update.isError && update.variables?.id === selectedItem.id} onSave={patch => update.mutate({ id: selectedItem.id, patch })} />
      {confirmDelete === selectedItem.id ? <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2">
        <p className="w-full text-danger">删除“{selectedItem.label || selectedItem.name}”的本地回测记录？曲线和交易明细将一并删除，无法恢复。</p>
        <button type="button" disabled={editingDisabled} className="rounded border border-danger px-2 py-1.5 text-danger disabled:opacity-50" onClick={() => { ++version.current; restored.current = true; remove.mutate(selectedItem.id) }}>确认删除</button>
        <button type="button" disabled={remove.isPending} className="px-2 py-1.5 text-secondary" onClick={() => setConfirmDelete('')}>取消</button>
      </div> : <button type="button" disabled={editingDisabled} className="text-danger disabled:opacity-50" onClick={() => setConfirmDelete(selectedItem.id)}>删除这次回测</button>}
    </>}
    {remove.isError && <p role="alert" className="text-danger">删除失败，原记录和当前结果仍保留，请重试。</p>}
    {result && <details className="text-secondary">
      <summary className="cursor-pointer break-words">当前结果：{result.strategy_info?.name || result.config.strategy_id} · {result.config.start}～{result.config.end} · {result.trades.length}笔 · {result.history_id ? '已保存到本地' : '尚未保存'} · 查看原始配置</summary>
      <p className="py-1">下方曲线及交易来自这次回测；左侧表单用于下一次运行，修改参数不会改变历史结果。</p>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px]">{JSON.stringify(result.config, null, 2)}</pre>
    </details>}
  </section>
}
