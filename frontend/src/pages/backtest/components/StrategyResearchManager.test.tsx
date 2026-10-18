// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type ResearchStrategyMetadata } from '@/lib/api'
import { StrategyResearchManager } from './StrategyResearchManager'

const strategies = [{ id: 'graham', name: '小市值基线', source: 'custom' }, { id: 'variant', name: '小市值改版', source: 'ai' }, { id: 'dividend', name: '红利增强', source: 'composite' }]
const metadata: Record<string, ResearchStrategyMetadata> = { graham: { favorite: true }, variant: { parent_id: 'graham', category: 'smallcap' } }
const pause = () => new Promise(resolve => setTimeout(resolve, 25))
afterEach(() => vi.restoreAllMocks())
async function mount(disabled = false) {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div'), root = createRoot(host), onSelect = vi.fn(), onChecked = vi.fn(), onEdit = vi.fn()
  function Harness() {
    const [selected, setSelected] = useState<string>('variant'), [checked, setChecked] = useState<string[]>([])
    return <StrategyResearchManager strategies={strategies} selected={selected} disabled={disabled} checked={checked}
      onSelect={id => { setSelected(id); onSelect(id) }} onCheckedChange={ids => { setChecked(ids); onChecked(ids) }} onEdit={onEdit} />
  }
  await act(async () => { root.render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>); await pause() })
  await act(async () => { await pause() })
  return { host, onSelect, onChecked, onEdit, close: async () => { await act(async () => root.unmount()); client.clear() } }
}
function button(host: HTMLElement, label: string) { return [...host.querySelectorAll('button')].find(item => item.textContent === label)! }
async function input(host: HTMLElement, label: string, value: string) {
  await act(async () => {
    const element = host.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true })); await pause()
  })
}

it('按组显示版本链，筛选批量勾选与当前策略选择各自独立', async () => {
  vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: metadata })
  const ui = await mount()
  try {
    expect(ui.host.textContent).toContain('版本链：小市值基线 → 当前')
    await input(ui.host, '搜索策略', '小市值')
    await act(async () => { button(ui.host, '勾选当前筛选').click(); await pause() })
    expect(ui.onChecked).toHaveBeenLastCalledWith(['graham', 'variant'])
    expect(ui.onSelect).not.toHaveBeenCalled()
    await act(async () => { (ui.host.querySelector('[aria-label="当前策略 小市值基线"]') as HTMLInputElement).click(); await pause() })
    expect(ui.onSelect).toHaveBeenCalledWith('graham')
    expect(ui.host.textContent).toContain('当前策略：小市值基线')
    await act(async () => { button(ui.host, '清空勾选').click(); await pause() })
    expect(ui.onChecked).toHaveBeenLastCalledWith([])
    await act(async () => { (ui.host.querySelector('input[type="checkbox"]') as HTMLInputElement).click(); await pause() })
    expect(ui.host.querySelector('[aria-label="当前策略 小市值改版"]')).toBeNull()
  } finally { await ui.close() }
})

it('修改资料只调用元数据接口；失败保留草稿；编辑按钮交给现有入口', async () => {
  vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: metadata })
  const save = vi.spyOn(api, 'updateResearchStrategyMetadata').mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ label: '新研究名称', parent_id: 'graham', category: 'smallcap' })
  const ui = await mount()
  try {
    expect(ui.host.querySelector('details')?.open).toBe(false)
    await act(async () => { (ui.host.querySelector('summary') as HTMLElement).click(); await pause() })
    expect(ui.host.querySelector('details')?.open).toBe(true)
    await input(ui.host, '策略研究名称', '新研究名称')
    const submit = async () => act(async () => { ui.host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await pause() })
    await submit()
    expect(ui.host.textContent).toContain('策略资料保存失败')
    expect((ui.host.querySelector('[aria-label="策略研究名称"]') as HTMLInputElement).value).toBe('新研究名称')
    await submit()
    expect(save).toHaveBeenLastCalledWith('variant', expect.objectContaining({ label: '新研究名称', parent_id: 'graham' }))
    expect(ui.host.textContent).toContain('新研究名称')
    await act(async () => { button(ui.host, '编辑策略规则').click(); await pause() })
    expect(ui.onEdit).toHaveBeenCalledOnce()
  } finally { await ui.close() }
})

it('运行中禁止选择与修改，资料读取失败可以重试', async () => {
  const get = vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValueOnce({ items: metadata }).mockRejectedValue(new Error('offline'))
  const ui = await mount(true)
  try {
    expect((ui.host.querySelector('input[type="radio"]') as HTMLInputElement).disabled).toBe(true)
    expect(button(ui.host, '保存策略资料').disabled).toBe(true)
    expect(button(ui.host, '勾选当前筛选').disabled).toBe(true)
  } finally { await ui.close() }
  const failed = await mount()
  try {
    expect(failed.host.textContent).toContain('策略资料读取失败')
    expect(failed.host.querySelector('form')).toBeNull()
    get.mockResolvedValue({ items: metadata })
    await act(async () => { button(failed.host, '重试').click(); await pause() })
    expect(failed.host.querySelector('form')).not.toBeNull()
  } finally { await failed.close() }
})


it('按分类筛选后只批量勾选该分支，当前策略和已有勾选保留', async () => {
  vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: metadata })
  const ui = await mount()
  try {
    await act(async () => {
      const select = ui.host.querySelector('[aria-label="筛选策略分类"]') as HTMLSelectElement
      select.value = 'dividend'; select.dispatchEvent(new Event('change', { bubbles: true })); await pause()
    })
    expect(ui.host.querySelector('[aria-label="当前策略 小市值基线"]')).toBeNull()
    expect(ui.host.querySelector('[aria-label="当前策略 红利增强"]')).not.toBeNull()
    expect(ui.host.textContent).toContain('当前策略：小市值改版')
    await act(async () => { button(ui.host, '勾选当前筛选').click(); await pause() })
    expect(ui.onChecked).toHaveBeenLastCalledWith(['dividend'])
    await act(async () => {
      const select = ui.host.querySelector('[aria-label="筛选策略分类"]') as HTMLSelectElement
      select.value = 'smallcap'; select.dispatchEvent(new Event('change', { bubbles: true })); await pause()
    })
    await act(async () => { button(ui.host, '勾选当前筛选').click(); await pause() })
    expect(ui.onChecked).toHaveBeenLastCalledWith(['dividend', 'graham', 'variant'])
    expect(ui.onSelect).not.toHaveBeenCalled()
  } finally { await ui.close() }
})


it('保留自定义、AI和叠加来源筛选，并与研究分类共同过滤', async () => {
  vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: metadata })
  const ui = await mount()
  const select = async (label: string, value: string) => act(async () => {
    const element = ui.host.querySelector(`[aria-label="${label}"]`) as HTMLSelectElement
    element.value = value
    element.dispatchEvent(new Event('change', { bubbles: true }))
    await pause()
  })
  try {
    for (const [source, id] of [['custom', 'graham'], ['ai', 'variant'], ['composite', 'dividend']]) {
      await select('筛选策略来源', source)
      expect(ui.host.querySelectorAll('input[type="radio"]')).toHaveLength(1)
      expect((ui.host.querySelector('input[type="radio"]') as HTMLInputElement).getAttribute('aria-label'))
        .toBe(`当前策略 ${strategies.find(item => item.id === id)!.name}`)
    }
    await select('筛选策略分类', 'smallcap')
    expect(ui.host.querySelectorAll('input[type="radio"]')).toHaveLength(0)
    await select('筛选策略来源', 'ai')
    await act(async () => { button(ui.host, '勾选当前筛选').click(); await pause() })
    expect(ui.onChecked).toHaveBeenLastCalledWith(['variant'])
  } finally { await ui.close() }
})
