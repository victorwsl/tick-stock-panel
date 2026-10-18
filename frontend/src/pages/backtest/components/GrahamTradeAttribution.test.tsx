// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it } from 'vitest'
import type { StrategyBacktestTrade } from '@/lib/api'
import { grahamMarkers, GrahamTradeAttribution } from './GrahamTradeAttribution'

it('真实加仓明细去重，未平仓不画卖出；财务依据和盈亏分解可见', async () => {
  const t = { symbol: '600001.SH', entry_date: '2025-02-03', exit_date: '2025-02-28',
    exit_reason: 'open_position', entry_attribution: { rank: 1 },
    entry_fills: [{ date: '2025-02-03', signal_date: '2025-01-31', symbol: '600001.SH',
      side: 'buy', price: 10, quantity: 1000, fee: 12, attribution: { rank: 1, market_cap: 1e9,
      ncav_ratio: 1.6, ep: .1, roa: .04, report_period: '20240930', announce_date: '20241030' } }],
    pnl_decomposition: { price_and_costs: -12, dividend_net: 0 },
  } as unknown as StrategyBacktestTrade
  const markers = grahamMarkers([t, t])
  expect(markers).toHaveLength(2)
  expect(markers[0].date).toBe('2025-01-31')
  expect(markers[1].date).toBe('2025-02-03')
  expect(markers.some(m => m.kind === 'sell')).toBe(false)
  expect(markers[0].detail).toContain('报告期 20240930')
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div'); const root = createRoot(host)
  try {
    await act(async () => root.render(<GrahamTradeAttribution trades={[t]} />))
    expect(host.textContent).toContain('10.00亿')
    expect(host.textContent).toContain('期末仅估值')
    expect(host.textContent).toContain('20241030')
  } finally { await act(async () => root.unmount()) }
})


it('聚宽复盘显示前收市值和五日清仓，不误标成月末换股', () => {
  const a = { strategy_kind: 'joinquant_smallcap', rank: 2, market_cap: 2.1e9, anchor_date: '20230104' }
  const t = { symbol: '600001.SH', entry_date: '2026-08-03', exit_date: '2026-08-10',
    exit_signal_date: '2026-08-07', exit_reason: 'signal', entry_attribution: a,
    entry_fills: [{date: '2026-08-03', signal_date: '2026-07-31', symbol: '600001.SH',
      side: 'buy', price: 10, quantity: 1000, fee: 10, attribution: a}],
  } as unknown as StrategyBacktestTrade
  const m = grahamMarkers([t])
  expect(m.find(x => x.phase === 'signal' && x.kind === 'buy')?.label).toBe('五日调仓入选')
  expect(m.find(x => x.phase === 'signal' && x.kind === 'sell')?.label).toBe('五日周期清仓')
  expect(m[0].detail).toContain('前收总市值 21.00亿')
  expect(m[0].detail).not.toContain('NCAV')
})
