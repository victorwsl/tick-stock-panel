// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type FactorBacktestResult } from '@/lib/api'
import { FactorBacktest } from './FactorBacktest'
import { factorResultCandidate } from './researchCandidates'

vi.mock('@/lib/api', () => ({ api: { factorColumns: vi.fn(), factorRun: vi.fn() } }))
vi.mock('./charts/FactorICChart', () => ({ FactorICChart: () => null }))
vi.mock('./charts/FactorGroupNavChart', () => ({ FactorGroupNavChart: () => null }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
const fixture: FactorBacktestResult = {
  run_id: 'saved', config: { factor_name: 'momentum_20d', prediction_horizon: 250, rebalance: 'monthly', start: '2023-01-03', end: '2026-10-16' },
  ic_mean: null, ic_std: null, ir: null, ic_win_rate: null, ic_series: [], group_stats: [], group_nav: [],
  long_short_stats: {}, long_short_nav: [], elapsed_ms: 1, n_symbols: 5, n_dates: 800, error: null,
}
beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  vi.mocked(api.factorColumns).mockResolvedValue({ columns: [{ id: 'momentum_20d', label: '20日动量', group: '动量', desc: '' }] } as never)
  vi.mocked(api.factorRun).mockResolvedValue(fixture)
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) }) }
it('单因子提交独立预测期限，并始终用运行配置标注旧结果', async () => {
  await act(async () => root.render(<QueryClientProvider client={client}><FactorBacktest /></QueryClientProvider>))
  await settle()
  const horizon = host.querySelector<HTMLSelectElement>('[aria-label="预测期限"]')!
  await act(async () => { horizon.value = '250'; horizon.dispatchEvent(new Event('change', { bubbles: true })) })
  const button = Array.from(host.querySelectorAll('button')).find(button => button.textContent?.includes('开始因子分析'))!
  await act(async () => button.click()); await settle()
  expect(api.factorRun).toHaveBeenCalledWith(expect.objectContaining({ prediction_horizon: 250, rebalance: 'monthly' }))
  expect(host.textContent).toContain('当前结果：预测未来250交易日')
  await act(async () => { horizon.value = '20'; horizon.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(host.textContent).toContain('当前结果：预测未来250交易日')
  expect(host.textContent).toContain('暂无有效IC')
})
it('保存候选时保留预测期限和标签口径', () => {
  const candidate = factorResultCandidate({ ...fixture, config: { ...fixture.config, prediction_label: 'next_exchange_open_to_hth_close' } }, '动量')
  expect(candidate.config).toMatchObject({ prediction_horizon: 250, prediction_label: 'next_exchange_open_to_hth_close' })
})

it('从批量结果进入时继承期限、区间、标的和调仓参数', async () => {
  await act(async () => root.render(<QueryClientProvider client={client}><FactorBacktest initialFactorName="momentum_20d" initialConfig={{ prediction_horizon: 20, start: '2024-01-02', end: '2025-12-31', rebalance: 'weekly', symbols: ['600000.SH'], n_groups: 3 }} /></QueryClientProvider>))
  await settle()
  const button = Array.from(host.querySelectorAll('button')).find(button => button.textContent?.includes('开始因子分析'))!
  await act(async () => button.click()); await settle()
  expect(api.factorRun).toHaveBeenCalledWith(expect.objectContaining({ prediction_horizon: 20, start: '2024-01-02', end: '2025-12-31', rebalance: 'weekly', symbols: ['600000.SH'], n_groups: 3 }))
})
