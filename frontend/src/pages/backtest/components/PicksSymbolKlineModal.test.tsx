// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { PicksSymbolKlineModal } from './PicksSymbolKlineModal'
import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'

const captured = vi.hoisted(() => ({ props: {} as any, names: {} }))
vi.mock('@/components/StockPanel', () => ({ StockPanel: (props: any) => { captured.props = props; return null } }))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => captured.names }))

it('回放保留触发日与成交日差异，合并同日记录，区分期末退出和待卖', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const trade = {
    symbol: '600000.SH', entry_date: '2025-01-03', entry_signal_date: '2025-01-02',
    entry_price: 10, exit_date: '2025-01-10', exit_signal_date: '2025-01-08',
    exit_price: 11, exit_reason: 'signal', exit_signal_id: 'zx_below_white', blocked_exit_days: 1,
  } as StrategyBacktestTrade
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<PicksSymbolKlineModal
      symbol="600000.SH" periodStart="2025-01-01" periodEnd="2025-02-01" onClose={() => {}}
      result={{ trades: [trade, trade, { ...trade, entry_signal_date: null,
        exit_date: '2025-01-31', exit_reason: 'end' },
        { ...trade, exit_date: '2025-01-30', exit_reason: 'pending_exit' }], per_symbol_stats: [] } as unknown as StrategyBacktestResult}
    />))
    const markers = captured.props.markers
    expect(markers.find((m: any) => m.date === '2025-01-02').label).toBe('买信号 ×3')
    expect(markers.find((m: any) => m.date === '2025-01-03').label).toBe('买入 ×4')
    expect(markers.find((m: any) => m.date === '2025-01-10').label).toBe('卖·跌破白线 ×2')
    expect(markers.find((m: any) => m.date === '2025-01-10').detail).toContain('受阻1天')
    expect(markers.some((m: any) => m.date === '2025-01-30' && m.phase === 'fill')).toBe(false)
    expect(markers.find((m: any) => m.date === '2025-01-31').label).toBe('卖·期末退出')
    expect(markers.filter((m: any) => m.kind === 'sell' && m.phase === 'signal')).toHaveLength(1)
    expect(markers.find((m: any) => m.kind === 'sell' && m.phase === 'signal').label).toContain('待卖')
  } finally { await act(async () => root.unmount()) }
})
