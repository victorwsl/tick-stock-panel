// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type FactorBatchItem } from '@/lib/api'
import { FactorDiscovery } from './FactorDiscovery'

vi.mock('@/lib/api', () => ({ api: {
  factorColumns: vi.fn(), watchlistList: vi.fn(), watchlistGroups: vi.fn(), factorBatch: vi.fn(),
  factorBatchHistory: vi.fn(), factorBatchHistoryResult: vi.fn(),
} }))
vi.mock('./FactorBacktest', () => ({ FactorBacktest: () => null }))
vi.mock('./AutoMiningDialog', () => ({ AutoMiningDialog: () => null }))
vi.mock('./AddFactorSignalDialog', () => ({ AddFactorSignalDialog: () => null }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const results: FactorBatchItem[] = [
  ['负大', -.0305], ['正小', .0253], ['缺失', null], ['负小', -.0233], ['零', 0], ['正大', .04],
].map(([name, value]) => ({
  factor_name: name as string, label: name as string, group: '动量',
  ic_mean: value as number | null, ir: value as number | null,
  t_newey_west: value as number | null, ic_win_rate: value as number | null,
  long_short_return: value as number | null, long_short_max_drawdown: value as number | null,
  n_symbols: 100, n_dates: 60, elapsed_ms: 1, error: null,
}))

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })
  vi.mocked(api.factorColumns).mockResolvedValue({ columns: [{ id: 'x', label: '测试因子', group: '动量', desc: '' }] } as never)
  vi.mocked(api.watchlistList).mockResolvedValue({ symbols: [] } as never)
  vi.mocked(api.factorBatchHistory).mockResolvedValue({ items: [], unavailable: 0 })
  vi.mocked(api.factorBatch).mockResolvedValue({ run_id: 'test', config: {}, results, n_symbols: 100, n_dates: 60, elapsed_ms: 1, error: null })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); host.remove()
})
async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
}
async function renderResults() {
  await act(async () => root.render(<QueryClientProvider client={client}><FactorDiscovery /></QueryClientProvider>))
  await settle()
  const run = Array.from(host.querySelectorAll('button')).find(button => button.textContent === '筛选 1 个因子')!
  expect(run.disabled).toBe(false)
  await act(async () => run.click())
  await settle()
}
const rowNames = () => Array.from(host.querySelectorAll('tbody tr')).map(row =>
  row.querySelector('td:nth-child(2) .font-medium')?.textContent?.replace(/[↑↓]/g, '').trim())
const descending = ['正大', '正小', '零', '负小', '负大', '缺失']
const ascending = ['负大', '负小', '零', '正小', '正大', '缺失']

it.each(['预测力 IC', '稳定度 IR', 't 值(NW)', '预测日占比', '多空收益', '最大回撤'])(
  '%s 按带符号数值切换升降序，零值正常且缺失值始终置后', async label => {
    await renderResults()
    expect(rowNames()).toEqual(descending)
    const header = Array.from(host.querySelectorAll<HTMLButtonElement>('thead button')).find(button => button.textContent === label)!
    if (label !== '预测力 IC') await act(async () => header.click())
    expect(header.closest('th')?.getAttribute('aria-sort')).toBe('descending')
    expect(rowNames()).toEqual(descending)
    await act(async () => header.click())
    expect(header.closest('th')?.getAttribute('aria-sort')).toBe('ascending')
    expect(rowNames()).toEqual(ascending)
    await act(async () => header.click())
    expect(rowNames()).toEqual(descending)
    expect(results.map(item => item.label)).toEqual(['负大', '正小', '缺失', '负小', '零', '正大'])
  },
)

it('刷新后恢复历史表格与排序，不重新计算因子', async () => {
  const saved = { history_id: 'saved', run_id: 'test', config: { start: '2025-01-01', end: '2025-06-30' }, results, n_symbols: 100, n_dates: 60, elapsed_ms: 1, error: null }
  vi.mocked(api.factorBatchHistory).mockResolvedValue({ items: [{ id: 'saved', saved_at: '2026-10-18T00:00:00Z', start: '2025-01-01', end: '2025-06-30', asset_type: 'stock' }], unavailable: 0 })
  vi.mocked(api.factorBatchHistoryResult).mockResolvedValue(saved)
  await act(async () => root.render(<QueryClientProvider client={client}><FactorDiscovery /></QueryClientProvider>))
  await settle(); await settle(); await settle()
  expect(rowNames()).toEqual(descending)
  expect(host.textContent).toContain('已保存到本地')
  expect(host.textContent).toContain('2025-01-01～2025-06-30')
  expect(api.factorBatch).not.toHaveBeenCalled()
})

it('默认60日预测和月度调仓，切换年度预测后保留独立调仓参数', async () => {
  await renderResults()
  expect(api.factorBatch).toHaveBeenLastCalledWith(expect.objectContaining({ prediction_horizon: 60, rebalance: 'monthly' }))
  const horizon = host.querySelector<HTMLSelectElement>('[aria-label="预测期限"]')!
  await act(async () => { horizon.value = '250'; horizon.dispatchEvent(new Event('change', { bubbles: true })) })
  const quick = Array.from(host.querySelectorAll('button')).find(button => button.textContent === '快速筛选 · 8年')!
  await act(async () => quick.click())
  const run = Array.from(host.querySelectorAll('button')).find(button => button.textContent === '筛选 1 个因子')!
  await act(async () => run.click()); await settle()
  expect(api.factorBatch).toHaveBeenLastCalledWith(expect.objectContaining({ prediction_horizon: 250, rebalance: 'monthly', start: expect.stringMatching(new RegExp(`^${new Date().getUTCFullYear() - 8}-`)) }))
})

it('短年度样本即使IC及t较大，也不能显示为有效因子', async () => {
  const item = { ...results[0], ic_mean: .08, ir: 1, t_newey_west: 4, q_value: .01,
    prediction_audit: { horizon: 250, n_ic_dates: 300, mature_rows: 2400, missing_or_immature_rows: 2000,
      nonoverlap_intervals: 2, data_start: '2023-01-03', data_end: '2026-10-16', last_mature_signal: '2025-10-01',
      inference_status: 'descriptive_only' as const, warnings: ['年度成熟历史不足'] } }
  vi.mocked(api.factorBatch).mockResolvedValue({ run_id: 'x', config: { prediction_horizon: 250, rebalance: 'monthly' }, results: [item], n_symbols: 8, n_dates: 550, elapsed_ms: 1, error: null })
  await renderResults()
  expect(host.querySelector('tbody tr')?.textContent).toContain('边缘')
  expect(host.textContent).toContain('1 个因子尚不足8个不重叠区间')
})
