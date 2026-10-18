// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type StrategyBacktestResult } from '@/lib/api'
import { BacktestHistory } from './BacktestHistory'

const items = ['new', 'old'].map(id => ({ id, saved_at: '2026-10-18T01:00:00Z', name: id,
  start: '2025-01-01', end: '2025-12-31', trade_count: 3, asset_type: 'stock', mode: 'position' }))
const result = { history_id: 'new', run_id: 'new', config: { start: '2025-01-01', end: '2025-12-31' },
  strategy_info: { name: '趋势' }, trades: [{ symbol: '600000.SH' }], equity_curve: [{ value: 1.2 }] } as unknown as StrategyBacktestResult
const pause = () => new Promise(resolve => setTimeout(resolve, 30))
afterEach(() => vi.restoreAllMocks())

async function mount(initial: StrategyBacktestResult | null = null, pending = false) {
  if (!vi.isMockFunction(api.researchStrategyMetadata)) vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: {} })
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div'), root = createRoot(host), onLoad = vi.fn(), onDelete = vi.fn()
  const render = async (current: StrategyBacktestResult | null, busy = pending) => {
    await act(async () => { root.render(<QueryClientProvider client={client}>
      <BacktestHistory result={current} pending={busy} onLoad={onLoad} onDelete={onDelete} />
    </QueryClientProvider>); await pause() })
    await act(async () => { await pause() })
  }
  await render(initial)
  return { host, onLoad, onDelete, render, close: async () => { await act(async () => root.unmount()); client.clear() } }
}

it('自动恢复最新结果并能切换旧记录', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items, unavailable: 0 })
  const get = vi.spyOn(api, 'strategyBacktestHistoryResult').mockResolvedValue(result)
  const ui = await mount()
  try {
    expect(get.mock.calls[0][0]).toBe('new')
    expect(ui.onLoad).toHaveBeenCalledWith(result)
    await ui.render(result)
    get.mockResolvedValue({ ...result, history_id: 'old', run_id: 'old' })
    await act(async () => {
      const row = [...ui.host.querySelectorAll('button')].find(button => button.textContent?.startsWith('old'))!
      row.click(); await pause()
    })
    expect(ui.onLoad.mock.lastCall?.[0].history_id).toBe('old')
    expect(ui.host.textContent).toContain('查看原始配置')
  } finally { await ui.close() }
})

it('运行中不自动替换结果，保存失败明确提示', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items, unavailable: 0 })
  const get = vi.spyOn(api, 'strategyBacktestHistoryResult').mockResolvedValue(result)
  const ui = await mount(null, true)
  try {
    expect(get).not.toHaveBeenCalled()
    await ui.render({ ...result, history_id: undefined, history_error: '磁盘保存失败' }, false)
    expect(ui.host.textContent).toContain('磁盘保存失败')
    expect(get).not.toHaveBeenCalled()
  } finally { await ui.close() }
})

it('空记录和读取失败分别提示', async () => {
  const list = vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items: [], unavailable: 0 })
  const ui = await mount()
  try { expect(ui.host.textContent).toContain('尚无磁盘历史') } finally { await ui.close() }
  list.mockRejectedValue(new Error('offline'))
  const failed = await mount()
  try { expect(failed.host.textContent).toContain('历史记录读取失败') } finally { await failed.close() }
})


function button(host: HTMLElement, label: string) {
  return [...host.querySelectorAll('button')].find(item => item.textContent === label)!
}
async function input(host: HTMLElement, label: string, value: string) {
  await act(async () => {
    const element = host.querySelector(`[aria-label="${label}"]`) as HTMLInputElement
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true })); await pause()
  })
}

it('搜索和收藏筛选保留当前结果，资料保存失败保留草稿', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items: [{ ...items[0], favorite: true }, items[1]], unavailable: 0 })
  const save = vi.spyOn(api, 'updateStrategyBacktestHistory').mockRejectedValue(new Error('disk'))
  const ui = await mount(result)
  try {
    await input(ui.host, '搜索历史回测', 'old')
    expect(ui.host.querySelector('[aria-label="历史记录列表"]')?.textContent).not.toContain('new')
    expect(ui.host.textContent).toContain('查看原始配置')
    await act(async () => { (ui.host.querySelector('details')!.querySelector('summary') as HTMLElement).click(); await pause() })
    expect(ui.host.querySelector('details')?.open).toBe(true)
    await input(ui.host, '历史名称', '保留草稿')
    await act(async () => { ui.host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); await pause() })
    expect(save).toHaveBeenCalledWith('new', expect.objectContaining({ label: '保留草稿' }))
    expect(ui.host.textContent).toContain('历史资料保存失败')
    expect((ui.host.querySelector('[aria-label="历史名称"]') as HTMLInputElement).value).toBe('保留草稿')
    await input(ui.host, '搜索历史回测', '')
    await act(async () => { const box = ui.host.querySelector('input[type="checkbox"]') as HTMLInputElement; box.click(); await pause() })
    expect(ui.host.querySelector('[aria-label="历史记录列表"]')?.textContent).not.toContain('old')
  } finally { await ui.close() }
})

it('确认后才删除，失败保留记录，成功通知页面且不会自动恢复其他记录', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items, unavailable: 0 })
  const get = vi.spyOn(api, 'strategyBacktestHistoryResult').mockResolvedValue(result)
  const remove = vi.spyOn(api, 'deleteStrategyBacktestHistory').mockRejectedValueOnce(new Error('disk')).mockResolvedValue({ id: 'new' })
  const ui = await mount(result)
  try {
    await act(async () => { button(ui.host, '删除这次回测').click(); await pause() })
    expect(remove).not.toHaveBeenCalled()
    await act(async () => { button(ui.host, '确认删除').click(); await pause() })
    expect(ui.host.textContent).toContain('删除失败，原记录和当前结果仍保留')
    expect(ui.onDelete).not.toHaveBeenCalled()
    await act(async () => { button(ui.host, '确认删除').click(); await pause() })
    expect(ui.onDelete).toHaveBeenCalledWith('new')
    expect(ui.host.querySelector('[aria-label="历史记录列表"]')?.textContent).not.toContain('new')
    await ui.render(null)
    expect(get).not.toHaveBeenCalled()
  } finally { await ui.close() }
})

it('较早的异步加载不能覆盖后选记录，删除中的加载不能恢复已删结果', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items, unavailable: 0 })
  let finishOld!: (value: StrategyBacktestResult) => void
  const get = vi.spyOn(api, 'strategyBacktestHistoryResult').mockImplementation(id => id === 'old'
    ? new Promise(resolve => { finishOld = resolve }) : Promise.resolve(result))
  vi.spyOn(api, 'deleteStrategyBacktestHistory').mockResolvedValue({ id: 'old' })
  const ui = await mount(result)
  try {
    const row = (id: string) => [...ui.host.querySelectorAll('[aria-pressed]')].find(element => element.textContent?.startsWith(id)) as HTMLButtonElement
    await act(async () => { row('old').click(); await pause() })
    await act(async () => { row('new').click(); await pause() })
    await act(async () => { finishOld({ ...result, history_id: 'old' }); await pause() })
    expect(ui.onLoad).toHaveBeenCalledTimes(1)
    expect(ui.onLoad).toHaveBeenLastCalledWith(result)
    await act(async () => { row('old').click(); await pause() })
    await act(async () => { button(ui.host, '删除这次回测').click(); await pause() })
    await act(async () => { button(ui.host, '确认删除').click(); await pause() })
    await act(async () => { finishOld({ ...result, history_id: 'old' }); await pause() })
    expect(ui.onLoad).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledTimes(3)
  } finally { await ui.close() }
})

it('回测运行中禁用历史加载、资料保存和删除', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items, unavailable: 0 })
  const ui = await mount(result, true)
  try {
    expect(button(ui.host, '删除这次回测').disabled).toBe(true)
    expect(button(ui.host, '保存历史资料').disabled).toBe(true)
    expect((ui.host.querySelector('[aria-pressed]') as HTMLButtonElement).disabled).toBe(true)
  } finally { await ui.close() }
})


it('历史按策略分类筛选兼容旧摘要名称，显示百分比与缺值', async () => {
  vi.spyOn(api, 'strategyBacktestHistory').mockResolvedValue({ items: [
    { ...items[0], name: '自定义基线', strategy_id: 'new', stats: { total_return: 0.12, max_drawdown: -0.03, win_rate: 0.5 } },
    { ...items[1], name: '红利增强' },
  ], unavailable: 0 })
  vi.spyOn(api, 'researchStrategyMetadata').mockResolvedValue({ items: { new: { category: 'smallcap' } } })
  const ui = await mount(result)
  try {
    const listing = () => ui.host.querySelector('[aria-label="历史记录列表"]')!.textContent
    expect(listing()).toContain('累计收益 +12.00%')
    expect(listing()).toContain('最大回撤 -3.00%')
    expect(listing()).toContain('胜率 +50.00%')
    expect(listing()).toContain('累计收益 —')
    await act(async () => {
      const select = ui.host.querySelector('[aria-label="历史策略分类"]') as HTMLSelectElement
      select.value = 'dividend'; select.dispatchEvent(new Event('change', { bubbles: true })); await pause()
    })
    expect(listing()).toContain('红利增强')
    expect(listing()).not.toContain('自定义基线')
    await act(async () => {
      const select = ui.host.querySelector('[aria-label="历史策略分类"]') as HTMLSelectElement
      select.value = 'smallcap'; select.dispatchEvent(new Event('change', { bubbles: true })); await pause()
    })
    expect(listing()).toContain('自定义基线')
    expect(ui.host.querySelector('details')?.open).toBe(false)
  } finally { await ui.close() }
})
