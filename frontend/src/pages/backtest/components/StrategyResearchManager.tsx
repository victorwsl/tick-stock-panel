import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type ResearchStrategyMetadata } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { researchCategory, researchCategoryLabels, type ResearchCategory } from '../researchManagement'

type Strategy = { id: string; name: string; source?: string; timeframes?: string[] }
type Props = {
  strategies: Strategy[]
  selected: string | null
  onSelect: (id: string) => void
  disabled?: boolean
  checked: string[]
  onCheckedChange: (ids: string[]) => void
  onEdit?: () => void
}
const control = 'min-w-0 rounded border border-border bg-base px-2 py-1.5 disabled:opacity-50'

function MetadataEditor({ strategy, strategies, metadata, disabled, onEdit }: {
  strategy: Strategy; strategies: Strategy[]; metadata: ResearchStrategyMetadata; disabled?: boolean; onEdit?: () => void
}) {
  const client = useQueryClient()
  const [draft, setDraft] = useState<ResearchStrategyMetadata>({ ...metadata, category: researchCategory(strategy.id, strategy.name, metadata) })
  const save = useMutation({
    mutationFn: () => api.updateResearchStrategyMetadata(strategy.id, draft),
    onSuccess: item => client.setQueryData<{ items: Record<string, ResearchStrategyMetadata> }>(QK.researchStrategyMetadata,
      previous => ({ items: { ...previous?.items, [strategy.id]: item } })),
  })
  const busy = disabled || save.isPending
  return <div className="space-y-2 border-t border-border pt-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="font-medium break-all">当前策略：{strategy.name}</span>
      {onEdit && <button type="button" disabled={busy} className="text-accent disabled:opacity-50" onClick={onEdit}>编辑策略规则</button>}
    </div>
    <details>
      <summary className="cursor-pointer text-secondary">标注与父版本</summary>
      <form className="mt-2 space-y-2" onSubmit={event => { event.preventDefault(); save.mutate() }}>
    <p className="text-secondary">新规则先到「策略」页创建独立策略，再在这里关联父版本；编辑当前规则会影响以后运行，历史记录不变。</p>
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      <label className="grid gap-1">研究名称<input aria-label="策略研究名称" maxLength={120} className={control} disabled={busy} value={draft.label ?? ''} placeholder={strategy.name}
        onChange={event => setDraft({ ...draft, label: event.target.value })} /></label>
      <label className="grid gap-1">分类<select aria-label="策略分类" className={control} disabled={busy} value={draft.category}
        onChange={event => setDraft({ ...draft, category: event.target.value as ResearchCategory })}>
        {Object.entries(researchCategoryLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label className="grid gap-1 sm:col-span-2">父版本<select aria-label="策略父版本" className={control} disabled={busy} value={draft.parent_id ?? ''}
        onChange={event => setDraft({ ...draft, parent_id: event.target.value || null })}>
        <option value="">无父版本</option>
        {metadata.parent_id && !strategies.some(item => item.id === metadata.parent_id) && <option value={metadata.parent_id}>{metadata.parent_id}（策略已不可用）</option>}
        {strategies.filter(item => item.id !== strategy.id).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
    </div>
    <label className="grid gap-1">研究备注<textarea aria-label="策略研究备注" className={control} disabled={busy} rows={2} maxLength={4000} value={draft.note ?? ''}
      onChange={event => setDraft({ ...draft, note: event.target.value })} /></label>
    <div className="flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5"><input type="checkbox" disabled={busy} checked={!!draft.favorite}
        onChange={event => setDraft({ ...draft, favorite: event.target.checked })} />收藏当前策略</label>
      <button type="submit" disabled={busy} className="rounded border border-border px-2 py-1.5 text-accent disabled:opacity-50">{save.isPending ? '保存中…' : '保存策略资料'}</button>
      {save.isSuccess && <span role="status" className="text-secondary">已保存</span>}
    </div>
    {save.isError && <p role="alert" className="text-danger">策略资料保存失败，修改仍保留，请重试。{save.error instanceof Error ? save.error.message : ''}</p>}
      </form>
    </details>
  </div>
}

export function StrategyResearchManager({ strategies, selected, onSelect, disabled, checked, onCheckedChange, onEdit }: Props) {
  const metadata = useQuery({ queryKey: QK.researchStrategyMetadata, queryFn: api.researchStrategyMetadata })
  const [search, setSearch] = useState('')
  const [favorites, setFavorites] = useState(false)
  const [categoryFilter, setCategoryFilter] = useState<ResearchCategory | ''>('')
  const [sourceFilter, setSourceFilter] = useState('')
  const items = metadata.data?.items ?? {}
  const query = search.trim().toLowerCase()
  const visible = strategies.filter(item => (!categoryFilter || researchCategory(item.id, item.name, items[item.id]) === categoryFilter)
    && (!sourceFilter || item.source === sourceFilter)
    && (!favorites || items[item.id]?.favorite)
    && `${item.name} ${item.id} ${items[item.id]?.label ?? ''} ${items[item.id]?.note ?? ''}`.toLowerCase().includes(query))
  const current = strategies.find(item => item.id === selected)
  const parentChain = (id: string) => {
    const names: string[] = [], seen = new Set([id])
    let parent = items[id]?.parent_id
    while (parent && !seen.has(parent)) {
      seen.add(parent)
      names.unshift(items[parent]?.label || strategies.find(item => item.id === parent)?.name || parent)
      parent = items[parent]?.parent_id
    }
    return names.join(' → ')
  }
  return <section aria-label="策略管理" className="min-w-0 space-y-3 text-xs">
    <div className="flex items-center justify-between gap-2"><h3 className="font-medium">策略管理</h3><span className="text-secondary">已勾选 {checked.length} 项</span></div>
    <input aria-label="搜索策略" className={`${control} w-full`} placeholder="搜索策略、名称或备注" value={search} onChange={event => setSearch(event.target.value)} />
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <select aria-label="筛选策略分类" className={control} value={categoryFilter} onChange={event => setCategoryFilter(event.target.value as ResearchCategory | '')}>
        <option value="">全部分类</option>
        {Object.entries(researchCategoryLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      <select aria-label="筛选策略来源" className={control} value={sourceFilter} onChange={event => setSourceFilter(event.target.value)}>
        <option value="">全部来源</option><option value="custom">自定义</option><option value="ai">AI</option><option value="composite">叠加</option>
      </select>
      <label className="flex items-center gap-1.5"><input type="checkbox" checked={favorites} onChange={event => setFavorites(event.target.checked)} />仅收藏</label>
      <button type="button" className="text-accent disabled:opacity-50" disabled={disabled || !visible.length}
        onClick={() => onCheckedChange([...new Set([...checked, ...visible.map(item => item.id)])])}>勾选当前筛选</button>
      <button type="button" className="text-secondary disabled:opacity-50" disabled={disabled || !checked.length} onClick={() => onCheckedChange([])}>清空勾选</button>
    </div>
    {metadata.isPending && <p role="status" className="text-secondary">正在读取策略资料…</p>}
    {metadata.isError && <div role="alert" className="text-danger">策略资料读取失败。<button type="button" className="ml-2 text-accent" onClick={() => { void metadata.refetch() }}>重试</button></div>}
    <div className="max-h-72 space-y-3 overflow-y-auto">
      {(Object.keys(researchCategoryLabels) as ResearchCategory[]).map(category => {
        const group = visible.filter(item => researchCategory(item.id, item.name, items[item.id]) === category)
        return group.length > 0 && <div key={category}>
          <h4 className="mb-1 text-secondary">{researchCategoryLabels[category]} · {group.length}</h4>
          <div className="divide-y divide-border">
            {group.map(item => <div key={item.id} className={`flex min-w-0 items-start gap-2 rounded px-1 py-2 ${selected === item.id ? 'bg-accent/10' : 'hover:bg-surface'}`}>
              <input type="checkbox" aria-label={`批量选择 ${item.name}`} className="mt-0.5" disabled={disabled} checked={checked.includes(item.id)}
                onChange={event => onCheckedChange(event.target.checked ? [...new Set([...checked, item.id])] : checked.filter(id => id !== item.id))} />
              <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-2">
                <input type="radio" name="research-current-strategy" aria-label={`当前策略 ${item.name}`} className="mt-0.5" disabled={disabled} checked={selected === item.id} onChange={() => onSelect(item.id)} />
                <span className="min-w-0 flex-1 break-words"><span className="font-medium">{items[item.id]?.label || item.name}</span>
                  {items[item.id]?.favorite && <span className="ml-2 text-accent">已收藏</span>}
                  {items[item.id]?.label && <span className="mt-0.5 block text-secondary">{item.name}</span>}
                  {parentChain(item.id) && <span className="mt-0.5 block text-secondary">版本链：{parentChain(item.id)} → 当前</span>}
                </span>
              </label>
            </div>)}
          </div>
        </div>
      })}
      {!visible.length && <p className="py-2 text-secondary">{strategies.length ? '没有匹配的策略，请调整搜索或收藏筛选。' : '暂无策略，请先在设置中添加。'}</p>}
    </div>
    {current && metadata.data && <MetadataEditor key={`${current.id}:${JSON.stringify(items[current.id])}`} strategy={current} strategies={strategies}
      metadata={items[current.id] ?? {}} disabled={disabled} onEdit={onEdit} />}
  </section>
}
