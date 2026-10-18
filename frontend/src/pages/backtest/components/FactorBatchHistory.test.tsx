// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type FactorBatchResult } from '@/lib/api'
import { FactorBatchHistory } from './FactorBatchHistory'

vi.mock('@/lib/api', () => ({ api: { factorBatchHistory: vi.fn(), factorBatchHistoryResult: vi.fn() } }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
const onLoad = vi.fn()
const saved = { history_id: 'a', run_id: 'r', config: { start: '2025-01-01', end: '2025-06-30' }, results: [], elapsed_ms: 1, n_symbols: 100, n_dates: 60, error: null } satisfies FactorBatchResult
beforeEach(() => {
  vi.resetAllMocks()
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div'); document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  vi.mocked(api.factorBatchHistory).mockResolvedValue({ items: [{ id: 'a', saved_at: '2026-10-18T01:00:00Z', start: '2025-01-01', end: '2025-06-30', asset_type: 'stock' }], unavailable: 0 })
  vi.mocked(api.factorBatchHistoryResult).mockResolvedValue(saved)
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })
async function render(pending = false, result: FactorBatchResult | null = null) {
  await act(async () => root.render(<QueryClientProvider client={client}><FactorBatchHistory result={result} pending={pending} onLoad={onLoad} /></QueryClientProvider>))
}
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 40)) }) }

it('重新进入时从服务端恢复最新结果', async () => {
  await render(); await settle(); await settle()
  expect(api.factorBatchHistoryResult).toHaveBeenCalledWith('a')
  expect(onLoad).toHaveBeenCalledWith(saved)
})
it('手动选择历史后读取相应记录', async () => {
  await render(false, saved); await settle()
  const select = host.querySelector('select')!
  await act(async () => { select.value = 'a'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await settle()
  expect(onLoad).toHaveBeenCalledWith(saved)
  expect(host.textContent).toContain('已保存到本地')
})
it('历史尚在读取时启动新运行，旧响应不能覆盖新运行', async () => {
  let finish!: (value: FactorBatchResult) => void
  vi.mocked(api.factorBatchHistoryResult).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  await render(); await settle()
  await render(true)
  await act(async () => finish(saved)); await settle()
  expect(onLoad).not.toHaveBeenCalled()
})
it('无历史时明确告知，读取失败提供重试入口', async () => {
  vi.mocked(api.factorBatchHistory).mockRejectedValue(new Error('offline'))
  await render(); await settle()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('历史读取失败')
  vi.mocked(api.factorBatchHistory).mockResolvedValue({ items: [], unavailable: 0 })
  await act(async () => host.querySelector('button')!.click()); await settle()
  expect(host.textContent).toContain('此前刷新丢失的结果需要重跑')
})
