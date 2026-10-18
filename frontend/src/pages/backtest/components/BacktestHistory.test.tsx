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
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const host = document.createElement('div'), root = createRoot(host), onLoad = vi.fn()
  const render = async (current: StrategyBacktestResult | null, busy = pending) => {
    await act(async () => { root.render(<QueryClientProvider client={client}>
      <BacktestHistory result={current} pending={busy} onLoad={onLoad} />
    </QueryClientProvider>); await pause() })
    await act(async () => { await pause() })
  }
  await render(initial)
  return { host, onLoad, render, close: async () => { await act(async () => root.unmount()); client.clear() } }
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
      const select = ui.host.querySelector('select')!
      select.value = 'old'; select.dispatchEvent(new Event('change', { bubbles: true })); await pause()
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
