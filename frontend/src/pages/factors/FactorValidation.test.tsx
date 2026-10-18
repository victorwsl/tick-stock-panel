// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { FactorValidation } from './FactorValidation'

vi.mock('@/lib/api', () => ({ api: {
  factorResearchStudies: vi.fn(), factorResearchSummary: vi.fn(),
  factorResearchDetail: vi.fn(), factorResearchVerify: vi.fn(),
} }))
vi.mock('../backtest/charts/useECharts', () => ({ useECharts: () => ({ current: null }) }))

let host: HTMLDivElement
let root: Root
let client: QueryClient
const fixture = {
  study: { id: 's', name: '研究', periods: ['2025H2_frozen'], baseline: '历史rule_v1', label_contract: '次日开盘', fusion: '5%融合', limitations: ['非独立样本外'], factors: [{ id: 'low_vol', name: '低波动' }] },
  rows: [{ id: 'low_vol', name: '低波动', formula: '−60日收益标准差', kind: '单因子', variant_id: 'soft5_low_vol', period: '2025H2_frozen', rank_ic: .063, uplift: .013165, paired_dates: 83, nonoverlap_intervals: 9, p_holm: .06, inference: 'HAC_available' }],
  maturity: [], label_audit: [],
}

beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } })
  vi.mocked(api.factorResearchStudies).mockResolvedValue({ studies: [{ id: 's', name: '研究', periods: ['2025H2_frozen'], horizons: [10,250], data_cutoff: '20261016', verification: null }] })
  vi.mocked(api.factorResearchSummary).mockResolvedValue(fixture as never)
  vi.mocked(api.factorResearchDetail).mockResolvedValue({ ic: [{ trade_date: '20250701', rank_ic: .06 }], curves: [], curve_note: '当前配置尚无资金净值' })
})
afterEach(async () => {
  await act(async () => root.unmount())
  client.clear(); host.remove()
})
async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}><FactorValidation /></QueryClientProvider>))
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
}

it('以百分点展示增量，并明确资金净值尚未构造', async () => {
  await render()
  expect(host.textContent).toContain('+1.32pp')
  expect(host.textContent).toContain('0.063')
  expect(host.textContent).toContain('当前配置尚无资金净值')
  expect(host.textContent).toContain('非独立样本外')
})

it('切换250日时发出新上下文请求，不把旧期限数字当成新结果', async () => {
  await render()
  vi.mocked(api.factorResearchSummary).mockImplementation(() => new Promise(() => {}))
  vi.mocked(api.factorResearchDetail).mockImplementation(() => new Promise(() => {}))
  await act(async () => {
    const select = host.querySelector<HTMLSelectElement>('[aria-label="收益观察期限"]')!
    select.value = '250'; select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(api.factorResearchSummary).toHaveBeenLastCalledWith('s', 250, 3, 'own_mature')
  expect(host.textContent).not.toContain('+1.32pp')
  expect(host.textContent).toContain('正在读取当前期限')
})

it('无导入数据时提供入口说明并禁用复算', async () => {
  vi.mocked(api.factorResearchStudies).mockResolvedValue({ studies: [] })
  await render()
  expect(host.textContent).toContain('尚无冻结实验')
  const button = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('Panel独立复算'))!
  expect(button.disabled).toBe(true)
  expect(api.factorResearchSummary).not.toHaveBeenCalled()
})

it('读取失败时展示错误和重试，不展示伪造的空结果', async () => {
  vi.mocked(api.factorResearchStudies).mockRejectedValue(new Error('数据目录不可读'))
  await render()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('数据目录不可读')
  expect(host.textContent).toContain('重试读取')
  expect(host.textContent).not.toContain('+1.32pp')
})
