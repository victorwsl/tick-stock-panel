// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { ActiveMvChart } from './ActiveMvChart'

const captured = vi.hoisted(() => ({ chart: {} as any }))
vi.mock('./EChartsCandlestick', () => ({ EChartsCandlestick: (props: any) => { captured.chart = props; return null } }))

it('显示真实日期与状态，手动刷新读取新数据，图表保留OHLC悬停信息', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const payload = { total: 1, latest: '2026-10-16', earliest: '2026-10-16', source_sha256: 'sample',
    unit: '原始刻度', rule: '收盘确认，次日生效', rows: [{ date: '2026-10-16', open: 100, high: 102,
      low: 98, close: 101, zx_white: 100, zx_yellow: 99, zx_long: 98, r1: .01, r3: .03, amv_state: 'probe' }] }
  const fetch = vi.spyOn(api, 'activeMv').mockResolvedValue(payload)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => {
      root.render(<QueryClientProvider client={client}><ActiveMvChart /></QueryClientProvider>)
      await new Promise(resolve => setTimeout(resolve, 20))
    })
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
    expect(host.textContent).toContain('试探')
    expect(host.textContent).toContain('2026-10-16')
    expect(captured.chart.data).toEqual(payload.rows)
    expect(captured.chart.showInfoBar).not.toBe(false)
    fetch.mockResolvedValue({ ...payload, rows: [{ ...payload.rows[0], amv_state: 'confirmed' }] })
    await act(async () => {
      host.querySelector('button')!.click()
      await new Promise(resolve => setTimeout(resolve, 20))
    })
    expect(host.textContent).toContain('确认')
    expect(fetch).toHaveBeenCalledTimes(2)
  } finally {
    await act(async () => root.unmount())
    client.clear()
    fetch.mockRestore()
  }
})
